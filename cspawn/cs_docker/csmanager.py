import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from xml.sax import handler

import paramiko
import pytz
import requests
from slugify import slugify

import docker
from cspawn.cs_docker.manager import ServicesManager, logger
from cspawn.cs_docker.proc import Container, Service
from cspawn.cs_github.repo import CodeHostRepo, GithubOrg, StudentRepo
from cspawn.models import CodeHost, HostState, User, db
from cspawn.util.auth import basic_auth_hash, random_string
from cspawn.util.exceptions import DockerException
from docker import DockerClient

from ..models import Class, ClassProto

logger = logging.getLogger("cspawn.docker")  # noqa: F811


@dataclass
class StopResult:
    """Outcome of a single `CodeServerManager.stop_host()` call.

    Every step (push, stop, delete) is independently best-effort — a failure
    in one step is recorded here but never prevents the remaining steps from
    running.
    """

    service_name: str
    pushed: bool = False
    push_error: Optional[str] = None
    stopped: bool = False
    stop_error: Optional[str] = None
    deleted: bool = False
    skipped_push_mia: bool = False


class CSMService(Service):
    """
    A service class for managing Code Server instances.
    """

    def stop(self):
        """Remove the process."""

        self.remove()

    @property
    def hostname(self):
        """Return the hostname of the service."""
        return self.labels.get("caddy")

    @property
    def username(self):
        """Return the username of the service."""
        return self.labels.get("jtl.codeserver.username")

    @property
    def password(self):
        """Return the password of the service."""
        return self.labels.get("jtl.codeserver.password")

    @property
    def public_url(self):
        """Return the URL of the hostname."""
        return self.labels.get("jtl.codeserver.public_url")

    @property
    def repo(self):
        """Return the repository for the service."""
        return self.labels.get("INITIAL_GIT_REPO")

    def update(self, **kwargs):
        """Update the service with the given keyword arguments."""

        code_host = CodeHost.query.filter_by(service_id=self.id).first()
        if code_host:
            for key, value in kwargs.items():
                setattr(code_host, key, value)
                db.session.commit()

            db.session.commit()

    @property
    def is_ready(self):
        """Check if the server is ready by making a request to it."""
        logger.setLevel(logging.DEBUG)
        try:
            response = requests.get(self.public_url, timeout=10)
            logger.debug("Response from %s: %s", self.public_url, response.status_code)
            return response.status_code in [200, 302]
        except requests.exceptions.SSLError:
            logger.debug("SSL error encountered when connecting to %s", self.public_url)
            return False
        except requests.exceptions.RequestException as e:
            logger.debug("Error checking server status to %s: %s", self.public_url, e)
            return False
        


    @property
    def is_running(self):
        """Check if the service is running, although it may not be ready to service web requests"""
        return self.status == "running"

    @property
    def rec(self):
        """Return the database record for this service."""

        return CodeHost.query.filter_by(service_id=self.id).first()

    def check_ready(self):
        is_ready = self.is_ready  # Container is running
        is_running = self.is_running  # web-app is running
        rec = self.rec

        if is_running and rec.state != HostState.RUNNING.value:
            self.sync_to_db()  # Sets state=running and also container_id
        elif is_ready and rec.app_state != HostState.READY.value:
            self.sync_to_db(check_ready=True)

        return is_ready

    def sync_to_db(self, check_ready=False) -> CodeHost:
        """Sync the service to the database."""

        ch = CodeHost.query.filter_by(service_id=self.id).first()

        m = self.to_model()

        if check_ready:
            if self.is_ready:
                m.app_state = HostState.READY.value

        if ch:
            for key, value in m.__dict__.items():
                if key != "_sa_instance_state":
                    setattr(ch, key, value)
            db.session.commit()
        else:
            db.session.add(m)
            try:
                db.session.commit()
            except Exception as e:
                logger.error("Error committing CodeHost record: %s", e)
                db.session.rollback()

        return ch

    def to_model(self, no_container=False) -> CodeHost:
        """Return a CodeHost model instance.

        Args:
            no_container (bool): If True, do not include the container. Use this
            when creating new services and container isn't set initially.

        """

        username = self.labels.get("jtl.codeserver.username")
        user: User = User.query.filter_by(username=username).first()

        if not user:
            user = User.query.get(0)  # Get the root user

        c: Container = None

        if not no_container:

            try:

                c:Container = next(self.containers) # There is nearly always only one.

            except (KeyError, StopIteration):
                logger.error("CodeHost.to_model(): No container found for service %s", self.name)
                c = None
            except (ConnectionError, OSError) as e:
                # The node hosting this container is unreachable (broken pipe,
                # connection reset, etc.). Treat it as no container rather than
                # letting it abort the caller's whole sync/purge loop.
                logger.error(
                    "CodeHost.to_model(): node unreachable for service %s: %s", self.name, e
                )
                c = None

        # Get class_id from labels and validate it exists in database
        class_id = self.labels.get("jtl.codeserver.class_id")
        if class_id and class_id != "-1":
            try:
                class_id = int(class_id)
                # Check if the class exists in the database
                class_exists = db.session.query(db.exists().where(Class.id == class_id)).scalar()
                if not class_exists:
                    logger.error(f"Class with ID {class_id} does not exist, setting to None")
                    class_id = None
            except (ValueError, TypeError):
                logger.error(f"Invalid class_id in labels: {class_id}, setting to None")
                class_id = None
        else:
            class_id = None



        model_kwargs = dict(
            user_id=user.id,
            service_id=self.id,
            service_name=self.name,
            container_id=c.id if c else None,
            container_name=c.name if c else None,
            class_id=class_id,
            state=self.status,
            node_id=c.node.id if c else None,
            node_name=c.node.attrs["Description"]["Hostname"] if c else None,
            public_url=self.public_url,
            password=self.labels.get("jtl.codeserver.password"),
            labels=json.dumps(self.labels),
            created_at=datetime.fromisoformat(self.labels.get("jtl.codeserver.start_time")),
        )

        # No live container was resolved (any of the branches above). If its
        # task's node has actually been removed from the Swarm, `self.status`
        # would still report the Swarm-stale "running" state forever — override
        # it to MIA. A fresh service with no task yet (`no_container=True`, or
        # an empty `container_tasks`) or a transient connection blip must NOT
        # be flagged this way, so `app_state` is only ever added to the kwargs
        # here — never unconditionally — to avoid clobbering an existing row's
        # `app_state` (e.g. "ready") back to None on a routine resync.
        if c is None and not no_container and self.node_missing:
            logger.error(
                "CodeHost.to_model(): service %s has no live container and "
                "its task's node no longer exists in the Swarm; marking MIA",
                self.name,
            )
            model_kwargs["state"] = HostState.MIA.value
            model_kwargs["app_state"] = HostState.MIA.value

        return CodeHost(**model_kwargs)


def hostname_type(hostname):
    """Determines if a hostname is a public or local name or ip address."""

    if ":" in hostname:
        hostname, port = hostname.split(":", 1)
    else:
        port = None

    # If the hostname is an unroutable IP address ( it starts with 10.0, 192.168 or 172. ), it is local
    if hostname.startswith("10.") or hostname.startswith("192.168.") or hostname.startswith("172."):
        return "local"
    
    # If it ends with .local, it is local
    if hostname.endswith(".local"):
        return "local"
    
    #  if it does not have a dot in it at all, it is local
    if "." not in hostname:
        return "local"
    
    return "public"


def _truthy(value, default: bool) -> bool:
    """Coerce a config value to bool ('true'/'1'/'yes' -> True), mirroring
    `cs_docker/autoscale.py`'s existing `_cfg_bool()` idiom.

    Unlike `_cfg_bool` (which reads `cfg.get(key)` itself), this takes an
    already-fetched value: `self.config` (a `cspawn.util.config.Config`)
    supports `getattr(config, KEY, default)`, but that default only fires
    when the attribute is *absent* -- a present-but-string config value
    (e.g. `PIN_HOSTS_TO_NODE=false` loaded from a `public.env` file) is
    always truthy as a plain Python object, so it still needs parsing here.
    A bool value (the `getattr(...)` default itself, or a real bool config)
    is returned unchanged.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def define_cs_container(
    config,
    username,
    class_,
    image,
    hostname_template,
    repo: StudentRepo = None,
    syllabus=None,
    env_vars=None,
    port=None,
    password=None,
    available_ports=None,
):
    """
    Define the container configuration for a Code Server instance.

    Args:
        config: Configuration object.
        image: Docker image to use.
        username: Username for the Code Server instance.
        hostname_template: Template for the hostname.
        repo: Git repository to clone.
        syllabus: Syllabus for the Code Server instance.
        env_vars: Environment variables for the container.
        port: Port to expose.

    Returns:
        dict: Container configuration.
    """
    # Create the container

    if password is None:
        password = random_string(16)

    env_vars = env_vars or {}

    container_name = name = slugify(username)

    hashed_pw = basic_auth_hash(password)

    
    if repo:
        clone_dir = repo.upstream_name
        if clone_dir.endswith(".git"):
            clone_dir = clone_dir[:-4]
        workspace_folder = f"/workspace/{clone_dir}"
    else:
        workspace_folder = "/workspace"


    # This part sets up a port redirection for development, where we don't have
    # a reverse proxy in front of the container.

    hostname = hostname_template.format(username=container_name, port=available_ports[0])

    if hostname_type(hostname) == "public":
        
        public_url = f"https://{username}:{password}@{hostname}/"
        public_url_no_auth = f"https://{hostname}/"
        # VNC client URL path appended after https://{host}/. Externalized to
        # config (VNC_URL_PATH) so it survives deployment switches; the default
        # below is the direct-Caddy-websockify route with auto-reconnect (see
        # config/*/public.env for the old /proxy/6080 value and rationale).
        vnc_path = config.get(
            "VNC_URL_PATH",
            "vnc/vnc.html?autoconnect=true&reconnect=true&reconnect_delay=1000&resize=scale&quality=0&compression=9&path=websockify",
        )
        vnc_url = public_url_no_auth + vnc_path
        ports = None
    else:
        # Running in development mode

        public_url = f"http://{username}:{password}@{hostname}/"
        public_url_no_auth = f"http://{hostname}/"

        vnc_hostname = hostname_template.format(username=container_name, port=available_ports[1])
        vnc_url = f"http://{vnc_hostname}/vnc_lite.html?scale=true"

        internal_port = config.CODESERVER_PORT
        internal_vnc_port = 6080
        ports = [f"{available_ports[0]}:{internal_port}", f"{available_ports[1]}:{internal_vnc_port}"]
       

    host_uuid = str(uuid.uuid4())

    try:
        
        _env_vars = {
            "WORKSPACE_FOLDER": workspace_folder,
            "PASSWORD": password,
            "DISPLAY": ":0",
            "JTL_USERNAME": username,
            "JTL_CLASS_ID": str(class_.id) if class_ else None,
            "JTL_VNC_URL": vnc_url,
            "JTL_PUBLIC_URL": public_url,
            "JTL_SYLLABUS": syllabus or '',
            "JTL_IMAGE_URI": image,
            "JTL_REPO": repo.html_url if repo else '',
            "JTL_UPSTREAM_REPO": repo.upstream_url if repo else '',
            "JTL_CODESERVER_URL": public_url,
            "JTL_HOST_UUID": host_uuid,
            "JTL_SPAWNER_URL": config.INTERNAL_CODESERVER_URL,
            "KST_REPORTING_URL": config.KST_REPORTING_URL,
            "KST_REPORT_DIR": config.KST_REPORT_DIR,
            "KST_CONTAINER_ID": name,
            "KST_REPORT_INTERVAL": (config.KST_REPORT_INTERVAL if hasattr(config, "KST_REPORT_INTERVAL") else 30),
            "CS_DISABLE_GETTING_STARTED_OVERRIDE": "1",  # Disable the getting started page
            "GITHUB_TOKEN": config.GITHUB_TOKEN,  # For student access to their repo
        }

        
    except (KeyError, AttributeError) as e:
        raise KeyError(f"Missing configuration key: {e}")

    env_vars = {**_env_vars, **env_vars}

    labels = {
        "jtl": "true",
        "jtl.codeserver": "true",
        "jtl.codeserver.username": username,
        "jtl.codeserver.password": password,
        "jtl.codeserver.public_url": public_url,
        "jtl.codeserver.class_id": str(class_.id) if class_ else None,
        "jtl.codeserver.host_uuid": host_uuid,
        "jtl.codeserver.start_time": datetime.now(pytz.timezone("America/Los_Angeles")).isoformat(),
        "caddy": hostname,
        # WebSocket Handling
        "caddy.@ws.0_header": "Connection *Upgrade*",
        "caddy.@ws.1_header": "Upgrade websocket",
        "caddy.@ws.2_header": "Origin {http.request.header.Origin}",
        # WebSocket Reverse Proxy with HTTP/1.1
        "caddy.0_route.handle": "/websockify*",
        "caddy.0_route.handle.reverse_proxy": "@ws {{upstreams 6080}}",
        "caddy.0_route.handle.reverse_proxy.transport": "http",
        "caddy.0_route.handle.reverse_proxy.transport.versions": "1.1",
        # Keep established VNC websockets open across Caddy config reloads;
        # caddy-docker-proxy reloads on every service create/remove, which
        # otherwise closes every open websocket in the fleet (close 1001).
        "caddy.0_route.handle.reverse_proxy.stream_close_delay": "4h",
        # VNC Proxy
        "caddy.1_route.handle": "/vnc/*",
        "caddy.1_route.handle_path": "/vnc/*",
        "caddy.1_route.handle_path.reverse_proxy": "{{upstreams 6080}}",
        # General Reverse Proxy
        "caddy.2_route.handle": "/*",
        "caddy.2_route.handle.reverse_proxy": "{{upstreams 80}}",
        # code-server's own websocket must also survive reloads
        "caddy.2_route.handle.reverse_proxy.stream_close_delay": "4h",
        f"caddy.basic_auth.{username}": hashed_pw,
    }





    d =  {
        "name": container_name,
        "image": image,
        "labels": labels,
        "environment": env_vars,
        "ports": ports,
        "network": ["caddy", "jtlctl"]

    }

    # Optional Swarm placement constraints (e.g. ["node.role != manager"] to keep
    # code-server services off manager nodes). Read from config so each deployment
    # can set its own policy; absent/empty means no constraint (scheduler picks any node).
    placement_constraints = getattr(config, "PLACEMENT_CONSTRAINTS", None)
    if placement_constraints:
        if isinstance(placement_constraints, str):
            placement_constraints = [
                c.strip() for c in placement_constraints.split(",") if c.strip()
            ]
        d["constraints"] = list(placement_constraints)

    if config.USER_DIRS:
        d["mounts"] = [f"{str(Path(config.USER_DIRS) / slugify(username))}:/workspace"]

    return d   



class CodeServerManager(ServicesManager):
    service_class = CSMService

    def __init__(self, app: Any, network: List[str] = None, env: Dict[str, str] = None, labels: Dict[str, str] = None):
        """
        Initialize the CodeServerManager.

        Args:
            app: Application instance.
        """

        self.config = app.app_config
        self.app = app

        # Save this for convenience. You'd think that we could get this later from the 
        # client ( self.client.api.base_url), but the docker client constructor will
        # change it, so "unix:"" -> "htt+docker://localhost"
        self.docker_uri = self.config.DOCKER_URI

        def _hostname_f(node_name):
            return app.app_config["NODE_HOSTNAME_TEMPLATE"].format(nodename=node_name)

   
        try:
            c = DockerClient(base_url=self.docker_uri, use_ssh_client=True, timeout=10)
           
        except Exception as e:
            logger.error("Error connecting to Docker daemon at %s: %s", self.docker_uri, e)
            raise DockerException(f"Error connecting to Docker {self.docker_uri}: {e}")

        super().__init__(
            c,
            env=env,
            network=network,
            labels=labels,
            hostname_f=_hostname_f,
        )

        # Limit concurrent SSH connections to the Docker swarm manager.
        # BoundedSemaphore prevents sshd MaxStartups from dropping connections
        # when many threads call Docker-touching methods simultaneously.
        #
        # Deadlock-avoidance pattern: public methods (list, get, get_by_username,
        # new_cs) acquire this semaphore exactly once. _list_raw and any
        # _*_raw helpers are semaphore-free so they can be called safely from
        # within a block that already holds the semaphore (e.g., the 409
        # recovery path inside _new_cs_inner, which runs while new_cs holds
        # the semaphore).  Never call a semaphore-guarded public method from
        # inside another guarded method — use the _raw variant instead.
        concurrency = int(self.config.get("DOCKER_SSH_CONCURRENCY", 4))
        self._docker_sem = threading.BoundedSemaphore(concurrency)

    def get_unused_port(self, n=1, extra_ports=[]):
        import random

        """Find an unused port in the range 25000-30000."""

        if n == 1:
            public_urls = {urlparse(ch.public_url).port for ch in CodeHost.query.all() if ch.public_url}
            public_urls.update(extra_ports)

            while True:
                port = random.randint(25000, 30000)
                if port not in public_urls:
                    return port
                
        else:
            ports = []
            while len(ports) < n:
                port = self.get_unused_port(extra_ports=ports)
                if port not in ports:
                    ports.append(port)
            
            return ports
    

    def make_user_dir(self, username):
        """
        Create a user directory.

        This will ssh to the docker swarm manager with the same credentials as
        the docker URI and create a directory for the user, which assumes that the
        docker swarm manager has the user directories NFS mounted.

        Args:
            username (str): Username for the directory.

        Returns:
            Path: Path to the user directory.
        """

        user_dir = Path(self.config["USER_DIRS"]) / slugify(username)
        user_id = self.config["USERID"]

        parsed_uri = urlparse(self.config["DOCKER_URI"])

        if parsed_uri.scheme == "ssh":
            logger.info("Creating directory %s on remote host %s", user_dir, parsed_uri.hostname)

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(parsed_uri.hostname, username=parsed_uri.username)

            _, stdout, stderr = ssh.exec_command(f"mkdir -p {user_dir}")

            _, stdout, stderr = ssh.exec_command(f"chown  {user_id}:{user_id} {user_dir}")

            exit_status = stdout.channel.recv_exit_status()

            if exit_status != 0:
                logger.error("Failed to create directory %s on remote host: %s", user_dir, stderr.read().decode())

            ssh.close()

        else:
            try:
                
                os.makedirs(user_dir, exist_ok=True)
                os.system(f"chown -R {user_id}:{user_id} {user_dir}")
                logger.info("Creating directory %s on local machine", user_dir)
            except OSError as e:
                logger.error("Failed to create directory %s on local machine: %s", user_dir, e)

        return user_dir

    def new_cs(self, user: User, proto: ClassProto, class_: Class):
        """
        Create a new Code Server instance.

        Acquires the SSH semaphore and delegates to _new_cs_inner. The inner
        method uses _list_raw / _get_by_username_raw for any Docker calls on
        the 409-recovery path so the semaphore is never acquired twice.

        Args:
            user (User): User instance.
            proto (ClassProto): Class prototype.
            class_ (Class): Class instance.

        Returns:
            tuple[CSMService, CodeHost]: New Code Server instance and DB record.
        """
        self._docker_sem.acquire()
        try:
            return self._new_cs_inner(user, proto, class_)
        finally:
            self._docker_sem.release()

    def _get_by_username_raw(self, username):
        """
        Look up a running service by username without acquiring the semaphore.

        Safe to call from inside a semaphore-guarded block (e.g. _new_cs_inner).
        """
        username = slugify(username)
        for service in self._list_raw():
            if service.username == username:
                return service
        return None

    def _new_cs_inner(self, user: User, proto: ClassProto, class_: Class):
        """
        Body of new_cs — called with the SSH semaphore already held.

        All Docker-touching calls inside here use _list_raw / _get_by_username_raw
        (not the public semaphore-guarded wrappers) to prevent reentrant deadlock.
        """

        username = user.username

        assert isinstance(proto, ClassProto)


        gorg = GithubOrg.new_org(self.app)
        student_repo = gorg.fork(proto.repo_uri, username)

        container_def = define_cs_container(
            config=self.config,
            username=username,
            class_=class_,
            image=proto.image_uri,
            hostname_template=self.config.HOSTNAME_TEMPLATE,
            repo=student_repo,
            syllabus=proto.syllabus_path,
            available_ports=self.get_unused_port(2),
        )

        existing_ch = CodeHost.query.filter_by(service_name=username).first()
        if existing_ch:
            logger.info("CodeHost record for %s already exists", username)
            return super().get(existing_ch.service_id), existing_ch

        # import yaml
        # logger.debug(f"Container Definition\n {yaml.dump(container_def)}")
        # Only attempt to create a user directory if USER_DIRS is configured.
        # In dev (USER_DIRS empty), skip to avoid unnecessary SSH (Paramiko) connections.
        if self.config.USER_DIRS:
            self.make_user_dir(username)
        else:
            logger.debug("USER_DIRS not set; skipping remote user dir creation for %s", username)

        # For later, maybe there are other mounts.
        # for m in container_def.get('mounts', []):
        #    host_dir, container_dir = m.split(':')

        # Resolved by the pin block below (success path only); stays None on
        # the 409-recovery path, on the flag being off, or on any resolve/pin
        # failure -- feeds CodeHost.node_name below with no special-casing.
        node_fqdn = None

        try:
            logger.debug("Running container")
            s: CSMService = self.run(**container_def)

            # Sprint 014, Approach B: pin the newly created host to the node
            # Swarm's own scheduler just placed it on, so Swarm can never
            # migrate it later (a node in trouble would otherwise reschedule
            # -- and potentially cascade -- this host onto a neighbour).
            # Best-effort only: a failure here must never fail or delay
            # returning the new host, so this block guards all its own
            # exceptions. It must stay inside this `try:` (still before the
            # `except docker.errors.APIError` below) but must never let one
            # of its own APIError-family exceptions (e.g. from
            # `_pin_service_to_node`'s `svc.update()`) escape into that
            # handler, which exists solely for `self.run()`'s own
            # 409-already-exists race and would misinterpret a pin failure
            # as "service already exists".
            if _truthy(getattr(self.config, "PIN_HOSTS_TO_NODE", True), True):
                try:
                    from cspawn.cli.node import _pin_service_to_node, _resolve_task_node_fqdn

                    placement_timeout_s = float(
                        getattr(self.config, "PIN_HOST_PLACEMENT_TIMEOUT_S", 10.0)
                    )
                    node_fqdn = _resolve_task_node_fqdn(
                        self.client, s.o, timeout=placement_timeout_s, log=logger
                    )
                    if node_fqdn:
                        _pin_service_to_node(s.o, node_fqdn)
                    else:
                        logger.warning(
                            "Could not resolve placement node for %s in time; host not pinned",
                            username,
                        )
                except Exception as e:
                    logger.warning("Failed to pin new host %s to its node: %s", username, e)

        except docker.errors.APIError as e:
            if e.response.status_code == 409:
                logger.error("Container for %s already exists: %s", username, e)
                # Use the raw (non-semaphore) lookup: we already hold the
                # semaphore from new_cs, so calling public get_by_username
                # here would deadlock.
                s = self._get_by_username_raw(username)
                if not s:
                    logger.error("Error getting existing container for username %s ", username)

                    return None, None
            else:

                logger.error("Error creating container: %s", e)
                return None, None

        logger.debug("Committing model")
        ch: CodeHost = s.to_model(no_container=True)
        ch.proto_id = proto.id
        # Populate node_name immediately from the resolved placement (rather
        # than leaving it None until the next sync()/to_model() container
        # resolution). Stays None exactly as today when unresolved/disabled --
        # no regression. No retroactive backfill: this only ever runs for a
        # host created by this call, never for a pre-existing CodeHost row.
        ch.node_name = node_fqdn

        # The swarm service may already have a CodeHost row (e.g. a prior start
        # that raced or left a stale record). Reuse it instead of inserting a
        # duplicate, which would violate the unique service_id constraint.
        existing = CodeHost.query.filter_by(service_id=ch.service_id).first()
        if existing:
            logger.info("Reusing existing CodeHost row for service_id %s", ch.service_id)
            existing.service_name = ch.service_name
            existing.user_id = ch.user_id
            existing.proto_id = proto.id
            existing.class_id = ch.class_id
            existing.state = ch.state
            existing.public_url = ch.public_url
            existing.password = ch.password
            existing.labels = ch.labels
            existing.node_name = ch.node_name
            db.session.commit()
            return s, existing

        db.session.add(ch)
        db.session.commit()

        logger.info("Created new Code Server instance for %s", username)
        return s, ch

    def stop_cs(self, username):
        """Stop a Code Server instance by username."""

        s = self.get_by_username(username)
        if s:
            s.stop()

    def stop_host(self, code_host: CodeHost, *, push: bool = True, branch: str = "master") -> StopResult:
        """Push, stop, and delete a single CodeHost — the choke point every

        stop path should call. Each step (push / stop / delete) is
        independently best-effort: a failure in one step is logged and
        recorded on the returned `StopResult`, but never prevents the
        remaining steps from running, and this method itself never raises.

        Sequence:
          1. Push the host's local git state to GitHub, unless `push=False`
             or the host is MIA (`code_host.is_mia`), in which case the push
             is skipped cleanly (`skipped_push_mia=True`, INFO log).
          2. Stop the live Swarm service, if any. A missing/already-gone
             service (`self.get()` returns None) counts as a successful stop
             — the goal state, "no live service," already holds.
          3. Delete the `CodeHost` DB row and commit, rolling back on
             failure.

        Args:
            code_host: The CodeHost row to stop. Must already be persisted.
            push: If False, never calls `CodeHostRepo.push`, regardless of
                `code_host.is_mia`.
            branch: Branch to push to, forwarded to `CodeHostRepo.push`.

        Returns:
            A populated `StopResult` describing what happened at each step.
        """
        service_name = code_host.service_name
        result = StopResult(service_name=service_name)

        # 1. Push — best-effort, skipped cleanly for MIA hosts.
        if push:
            if code_host.is_mia:
                result.skipped_push_mia = True
                logger.info(
                    "Skipping push for %s: host is MIA", service_name
                )
            else:
                try:
                    CodeHostRepo(code_host, self.app).push(branch=branch)
                    result.pushed = True
                except Exception as e:
                    result.push_error = str(e)
                    logger.error("Push failed for %s: %s", service_name, e)

        # 2. Stop the live Swarm service — best-effort. A missing service is
        #    treated as an already-successful stop.
        try:
            service = self.get(code_host)
            if service is not None:
                service.stop()
            result.stopped = True
        except Exception as e:
            result.stop_error = str(e)
            logger.error("Stop failed for %s: %s", service_name, e)

        # 3. Delete the CodeHost DB row — best-effort, with rollback on failure.
        try:
            db.session.delete(code_host)
            db.session.commit()
            result.deleted = True
        except Exception as e:
            db.session.rollback()
            logger.error("DB delete failed for %s: %s", service_name, e)

        return result

    def _list_raw(self, filters: Optional[Dict[str, Any]] = {"label": "jtl.codeserver"}) -> List[CSMService]:
        """
        Call the parent list() without acquiring the SSH semaphore.

        Use this inside any block that already holds self._docker_sem to avoid
        reentrant deadlock.  External callers should use the public list() instead.
        """
        return super().list(filters=filters)

    def get(self, service_id: str | CodeHost) -> CSMService:
        if isinstance(service_id, CodeHost):
            service_id = service_id.service_id

        self._docker_sem.acquire()
        try:
            return super().get(service_id)
        finally:
            self._docker_sem.release()

    def list(self, filters: Optional[Dict[str, Any]] = {"label": "jtl.codeserver"}) -> List[CSMService]:
        """
        List all Code Server instances, from the Docker API.

        Args:
            filters (Optional[Dict[str, Any]]): Filters to apply.
        """
        self._docker_sem.acquire()
        try:
            return self._list_raw(filters=filters)
        finally:
            self._docker_sem.release()

    def list_db(self):
        """Return the code_host records."""

        return CodeHost.query.all()

    def sync(self, check_ready=False):
        """Sync the database with the Docker API."""

        in_db = {ch.service_id for ch in CodeHost.query.all()}
        in_swarm = {s.id for s in self.list()}

        not_in_db = in_swarm - in_db
        not_in_swarm = in_db - in_swarm

        # Mark the missing services as missing in action
        logger.info(f"Services in db but not in swarm: {len(not_in_swarm)}")
        for service_id in not_in_swarm:
            ch = CodeHost.query.filter_by(service_id=service_id).first()
            if ch:
                ch.state = HostState.MIA.value  # "Missing in Action"
                ch.app_state = HostState.MIA.value
            db.session.commit()

        # Get the CodeHosts records that have state != 'running' or 'app_state' != 'ready'
        not_ready_hosts = CodeHost.query.filter(
            (CodeHost.state != HostState.RUNNING.value) | (CodeHost.app_state != HostState.READY.value)
        ).all()

        # Update remaining services. Isolate per-host failures: a single
        # unreachable host or a transient SSH/docker drop must not abort the
        # whole sync (which would leave the rest of the DB stale and crash
        # callers like `host purge`).
        logger.info(f"Syncing not-ready hosts: {len(not_ready_hosts)}")
        for ch in not_ready_hosts:
            if ch.state == HostState.MIA.value:
                continue
            try:
                s: CSMService = self.get(ch.service_id)
                logger.info("Syncing service %s", s.name)
                s.sync_to_db(check_ready=check_ready)
            except Exception as e:
                logger.warning("Skipping host %s during sync: %s", ch.service_name, e)

        logger.info(f"Syncing not-in-db hosts: {len(not_in_db)}")
        # Create the missing services
        for service_id in not_in_db:
            try:
                s: CSMService = self.get(service_id)
                s.sync_to_db(check_ready=check_ready)
            except Exception as e:
                logger.warning("Skipping service %s during sync: %s", service_id, e)

    def unsettled_hosts(self) -> list:
        """Return CodeHost rows that are NOT in a terminal/known state.

        A host is "settled" once it is either fully up (app_state == READY) or
        definitively gone (state/app_state == MIA). Everything else — UNKNOWN,
        STARTING, or container-RUNNING-but-app-not-READY — is still converging
        and is worth re-syncing. This is the set `sync_converge` keeps chasing.
        """
        rows = CodeHost.query.all()
        unsettled = []
        for ch in rows:
            if ch.is_mia:
                continue
            if ch.app_state == HostState.READY.value:
                continue
            unsettled.append(ch)
        return unsettled

    def sync_converge(self, *, max_passes: int = 8, deadline_s: float = 90.0,
                      initial_delay: float = 2.0, max_delay: float = 12.0):
        """Sync repeatedly, focusing on hosts in an unknown/transient state,
        until they all settle (READY or MIA) or a deadline is hit.

        A single `sync()` is a snapshot: a host caught mid-start stays UNKNOWN/
        STARTING in the DB until the next manual sync. This drives state to
        convergence — each pass runs the normal reconciliation (with readiness
        probing) and then, if any hosts are still unsettled, waits (backoff) and
        syncs again. Bounded by both pass count and wall-clock so it can run
        safely on a cron without ever hanging.

        Returns a dict summary: {passes, settled, unsettled, unsettled_names}.
        Time/delay must be injected via callers' clock in tests; here we use
        time.monotonic()/time.sleep (the workflow-script clock restrictions do
        not apply to application code).
        """
        start = time.monotonic()
        passes = 0
        remaining: list = []
        while passes < max_passes:
            passes += 1
            self.sync(check_ready=True)
            remaining = self.unsettled_hosts()
            logger.info(
                "sync_converge pass %d/%d: %d host(s) still unsettled",
                passes, max_passes, len(remaining),
            )
            if not remaining:
                break
            if time.monotonic() - start >= deadline_s:
                logger.warning(
                    "sync_converge hit deadline (%.0fs) with %d unsettled host(s): %s",
                    deadline_s, len(remaining),
                    ", ".join(ch.service_name for ch in remaining),
                )
                break
            # Backoff between passes; cap so a long deadline still polls steadily.
            delay = min(initial_delay * (1.5 ** (passes - 1)), max_delay)
            # Don't sleep past the deadline.
            delay = min(delay, max(0.0, deadline_s - (time.monotonic() - start)))
            if delay > 0:
                time.sleep(delay)

        settled_names = [ch.service_name for ch in remaining]
        return {
            "passes": passes,
            "settled": CodeHost.query.count() - len(remaining),
            "unsettled": len(remaining),
            "unsettled_names": settled_names,
        }

    def remove_all(self, *, push: bool = True) -> List[StopResult]:
        """Stop and remove every CodeHost row via `stop_host()`.

        Args:
            push: Forwarded to `stop_host()` for every row; set False to
                skip pushing student work before removal.

        Returns:
            One `StopResult` per `CodeHost` row processed.
        """
        results = []
        for ch in CodeHost.query.all():
            logger.info("Removing code host %s (id=%s)", ch.service_name, ch.id)
            results.append(self.stop_host(ch, push=push))
        return results

    def get_by_hostname(self, username):
        """
        Get a Code Server instance by hostname.

        Args:
            username (str): Username for the instance.

        Returns:
            CSMService: Code Server instance.
        """

    def get_by_username(self, username):
        """
        Get a Code Server docker instance by username.

        Args:
            username (str): Username for the instance.

        Returns:
            CSMService: Code Server instance.
        """

        username = slugify(username)

        self._docker_sem.acquire()
        try:
            # Use _list_raw (not public list) to avoid acquiring the semaphore twice.
            for service in self._list_raw():
                if service.username == username:
                    return service
            return None
        finally:
            self._docker_sem.release()
