import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


import socket
from xml.sax import handler
import docker
from docker import DockerClient
import paramiko
import pytz
import requests
from slugify import slugify

from cspawn.cs_docker.manager import ServicesManager, logger
from cspawn.cs_docker.proc import Service, Container
from cspawn.models import CodeHost, User, HostState, db
from cspawn.util.auth import basic_auth_hash, random_string
from cspawn.util.exceptions import DockerException

from ..models import ClassProto, Class

logger = logging.getLogger("cspawn.docker")  # noqa: F811



    
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



        return CodeHost(
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


   


def define_cs_container(
    config,
    username,
    class_,
    image,
    hostname_template,
    repo=None,
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
        clone_dir = os.path.basename(repo)
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
        #vnc_url = public_url_no_auth + "vnc/?scale=true"
        vnc_url = public_url_no_auth + "proxy/6080/vnc_lite.html?path=proxy/6080/websockify&scale=true"
        ports = None
        
    else:
        # Running in development mode

        public_url = f"http://{username}:{password}@{hostname}/"
        public_url_no_auth = f"http://{hostname}/"

        vnc_hostname = hostname_template.format(username=container_name, port=available_ports[1])
        vnc_url = f"http://{vnc_hostname}/?scale=true"

        internal_port = config.CODESERVER_PORT
        internal_vnc_port = 6080
        ports = [f"{available_ports[0]}:{internal_port}", f"{available_ports[1]}:{internal_vnc_port}"]

       
    
    try:
        
        _env_vars = {
            "WORKSPACE_FOLDER": workspace_folder,
            "PASSWORD": password,
            "DISPLAY": ":0",
            "JTL_USERNAME": username,
            "JTL_CLASS_ID": str(class_.id) if class_ else None,
            "JTL_VNC_URL": vnc_url,
            "JTL_PUBLIC_URL": public_url,
            "JTL_SYLLABUS": syllabus,
            "JTL_IMAGE_URI": image,
            "JTL_REPO": repo,
            "JTL_CODESERVER_URL": public_url,
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
        # VNC Proxy
        "caddy.1_route.handle": "/vnc/*",
        "caddy.1_route.handle_path": "/vnc/*",
        "caddy.1_route.handle_path.reverse_proxy": "{{upstreams 6080}}",
        # General Reverse Proxy
        "caddy.2_route.handle": "/*",
        "caddy.2_route.handle.reverse_proxy": "{{upstreams 80}}",
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

        Args:
            user (User): User instance.
            image (str, optional): Docker image to use.
            repo (str, optional): Git repository to clone.
            syllabus (str, optional): Syllabus for the Code Server instance.

        Returns:
            CSMService: New Code Server instance.
        """

        username = user.username

        assert isinstance(proto, ClassProto)

        container_def = define_cs_container(
            config=self.config,
            username=username,
            class_=class_,
            image=proto.image_uri,
            hostname_template=self.config.HOSTNAME_TEMPLATE,
            repo=proto.repo_uri,
            syllabus=proto.syllabus_path,
            available_ports=self.get_unused_port(2),
        )

        existing_ch = CodeHost.query.filter_by(service_name=username).first()
        if existing_ch:
            logger.info("CodeHost record for %s already exists", username)
            return self.get(existing_ch.service_id), existing_ch

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

        try:
            logger.debug("Running container")
            s: CSMService = self.run(**container_def)

        except docker.errors.APIError as e:
            if e.response.status_code == 409:
                logger.error("Container for %s already exists: %s", username, e)
                s = self.get_by_username(username)
                if not s:
                    logger.error("Error getting existing container for username %s ", username)

                    return None, None
            else:
    
                logger.error("Error creating container: %s", e)
                return None, None

        logger.debug("Committing model")
        ch: CodeHost = s.to_model(no_container=True)
        ch.proto_id = proto.id

        db.session.add(ch)
        db.session.commit()

        logger.info("Created new Code Server instance for %s", username)
        return s, ch

    def stop_cs(self, username):
        """Stop a Code Server instance by username."""

        s = self.get_by_username(username)
        if s:
            s.stop()

    def get(self, service_id: str | CodeHost) -> CSMService:
        if isinstance(service_id, CodeHost):
            service_id = service_id.service_id

        return super().get(service_id)

    def list(self, filters: Optional[Dict[str, Any]] = {"label": "jtl.codeserver"}) -> List[CSMService]:
        """
        List all Code Server instances, from the Docker API.

        Args:
            filters (Optional[Dict[str, Any]]): Filters to apply.


        """
        return super().list(filters=filters)

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

        # Update remaining services.
        logger.info(f"Syncing not-ready hosts: {len(not_ready_hosts)}")
        for ch in not_ready_hosts:
            if ch.state == HostState.MIA.value:
                continue
            s: CSMService = self.get(ch.service_id)
            logger.info("Syncing service %s", s.name)
            s.sync_to_db(check_ready=check_ready)

        logger.info(f"Syncing not-in-db hosts: {len(not_in_db)}")
        # Create the missing services
        for service_id in not_in_db:
            s: CSMService = self.get(service_id)

            s.sync_to_db(check_ready=check_ready)

    def remove_all(self):
        """Remove all Code Server instances."""
        for c in self.list():
            logger.info("Removing container %s (%s)", c.name, c.id)
            self.repo.remove_by_id(c.id)
            c.remove()

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

        for service in self.list():
       
            if service.username == username:
                return service
        return None
