import logging
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from time import sleep
from typing import Any, Dict, List, Optional, cast
from urllib.parse import urlparse

import docker
import paramiko
import pytz
import requests
import json
from time import time

from slugify import slugify


from cspawn.docker.manager import ServicesManager, logger
from cspawn.docker.proc import Service
from cspawn.main.models import db, User
from .models import CodeHost, HostImage

logger = logging.getLogger("cspawn.docker")


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
    def hostname_url(self):
        """Return the URL of the hostname."""
        return f"https://{self.hostname}"

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
        try:
            response = requests.get(self.hostname_url, timeout=10)
            logger.debug(
                "Response from %s: %s", self.hostname_url, response.status_code
            )
            return response.status_code in [200, 302]
        except requests.exceptions.SSLError:
            logger.debug(
                "SSL error encountered when connecting to %s", self.hostname_url
            )
            return False
        except requests.exceptions.RequestException as e:
            logger.debug("Error checking server status to %s: %s", self.hostname_url, e)
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

        if is_running and rec.state != "running":
            self.sync_to_db()  # Sets state=running and also container_id
        elif is_ready and rec.app_state != "ready":
            self.sync_to_db(check_ready=True)

        return is_ready

    def sync_to_db(self, check_ready=False):
        """Sync the service to the database."""

        ch = CodeHost.query.filter_by(service_id=self.id).first()

        m = self.to_model()

        if check_ready:
            if self.is_ready:
                m.app_state = "ready"

        if ch:
            for key, value in m.__dict__.items():
                if key != "_sa_instance_state":
                    setattr(ch, key, value)
            db.session.commit()
        else:
            db.session.add(m)
            db.session.commit()

    def to_model(self, no_container=False) -> CodeHost:
        """Return a CodeHost model instance.

        Args:
            no_container (bool): If True, do not include the container. Use this
            when creating new services and container isn't set initially. 

        """

        username = self.labels.get("jtl.codeserver.username")
        user: User = User.query.filter_by(username=username).first()

        if not user:
            user = User.query.get(0)

        image: HostImage = HostImage.query.filter_by(image_uri=self.image).first()

        if no_container:
            c = None
        else:
            try:
                c = next(self.containers)
            except (KeyError, StopIteration):
                logger.error("CodeHost.to_model(): No container found for service %s", self.name)
                c = None

        return CodeHost(
            user_id=user.id,
            service_id=self.id,
            service_name=self.name,
            container_id=c.id if c else None,
            container_name=c.name if c else None,
            state=self.status,
            host_image_id=image.id if image else None,
            node_id=c.node.id if c else None,
            node_name=c.node.attrs["Description"]["Hostname"] if c else None,
            public_url=self.hostname_url,
            password=self.labels.get("jtl.codeserver.password"),
            labels=json.dumps(self.labels),
            created_at=datetime.fromisoformat(
                self.labels.get("jtl.codeserver.start_time")
            ),
        )


def define_cs_container(
    config,
    image,
    username,
    hostname_template,
    repo=None,
    syllabus=None,
    env_vars=None,
    port=None,
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

    env_vars = env_vars or {}

    container_name = name = slugify(username)

    password = "code4life"

    hostname = hostname_template.format(username=container_name)

    if repo:
        clone_dir = os.path.basename(repo)
        if clone_dir.endswith(".git"):
            clone_dir = clone_dir[:-4]
        workspace_folder = f"/workspace/{clone_dir}"
    else:
        workspace_folder = "/workspace"

    _env_vars = {
        "WORKSPACE_FOLDER": workspace_folder,
        "PASSWORD": password,
        "DISPLAY": ":0",
        "VNC_URL": f"https://{hostname}/vnc/",
        "KST_REPORTING_URL": config.KST_REPORTING_URL,
        "KST_CONTAINER_ID": name,
        "KST_REPORT_RATE": (
            config.KST_REPORT_RATE if hasattr(config, "KST_REPORT_RATE") else 30
        ),
        "CS_DISABLE_GETTING_STARTED_OVERRIDE": "1",  # Disable the getting started page
        "INITIAL_GIT_REPO": repo,
        "JTL_SYLLABUS": syllabus,
    }

    env_vars = {**_env_vars, **env_vars}

    labels = {
        "jtl": "true",
        "jtl.codeserver": "true",
        "jtl.codeserver.username": username,
        "jtl.codeserver.password": password,
        "jtl.codeserver.start_time": datetime.now(
            pytz.timezone("America/Los_Angeles")
        ).isoformat(),
        "caddy": hostname,
        # WebSocket Handling
        "caddy.@ws.0_header": "Connection *Upgrade*",
        "caddy.@ws.1_header": "Upgrade websocket",
        "caddy.@ws.2_header": "Origin {http.request.header.Origin}",
        # "caddy.@ws.3_header": "Sec-WebSocket-Protocol {http.request.header.Sec-WebSocket-Protocol}",
        # "caddy.@ws.4_header": "Sec-WebSocket-Version {http.request.header.Sec-WebSocket-Version}",
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
        "caddy.2_route.handle.reverse_proxy": "{{upstreams 8080}}",
    }

    # This part sets up a port redirection for development, where we don't have
    # a reverse proxy in front of the container.

    internal_port = "8080"

    if port is True:
        ports = [internal_port]
    elif port is not None and port is not False:
        ports = [f"{port}:{internal_port}"]
    else:
        ports = None

    return {
        "name": container_name,
        "image": image,
        "labels": labels,
        "environment": env_vars,
        "ports": ports,
        "network": ["caddy", "jtlctl"],
        "mounts": [f"{str(Path(config.USER_DIRS)/slugify(username))}:/workspace"],
    }


class CodeServerManager(ServicesManager):

    service_class = CSMService

    def __init__(
        self,
        app: Any,
        network: List[str] = None,
        env: Dict[str, str] = None,
        labels: Dict[str, str] = None,
    ):
        """
        Initialize the CodeServerManager.

        Args:
            app: Application instance.
        """
        from cspawn.apptypes import App

        app = cast(App, app)

        self.config = app.app_config

        def _hostname_f(node_name):
            return app.app_config["NODE_HOSTNAME_TEMPLATE"].format(nodename=node_name)

        super().__init__(
            docker.DockerClient(base_url=self.config.DOCKER_URI),
            env=env,
            network=network,
            labels=labels,
            hostname_f=_hostname_f,
        )

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
            logger.info(
                "Creating directory %s on remote host %s", user_dir, parsed_uri.hostname
            )

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(parsed_uri.hostname, username=parsed_uri.username)

            _, stdout, stderr = ssh.exec_command(
                f"mkdir -p {user_dir} && chown -R {user_id}:{user_id} {user_dir}"
            )
            exit_status = stdout.channel.recv_exit_status()

            if exit_status != 0:
                logger.error(
                    "Failed to create directory %s on remote host: %s",
                    user_dir,
                    stderr.read().decode(),
                )
            ssh.close()
        else:
            logger.info("Creating directory %s on local machine", user_dir)
            os.makedirs(user_dir, exist_ok=True)
            os.system(f"chown -R {user_id}:{user_id} {user_dir}")

        return user_dir

    def new_cs(self, user: User, image=None, repo=None, syllabus=None):
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

        container_def = define_cs_container(
            config=self.config,
            username=username,
            image=image,
            hostname_template=self.config.HOSTNAME_TEMPLATE,
            repo=repo,
            syllabus=syllabus,
        )

        existing_ch = CodeHost.query.filter_by(service_name=username).first()
        if existing_ch:
            logger.info("CodeHost record for %s already exists", username)
            return self.get(existing_ch.service_id)

        # import yaml
        # logger.debug(f"Container Definition\n {yaml.dump(container_def)}")

        self.make_user_dir(username)

        # For later, maybe there are other mounts.
        # for m in container_def.get('mounts', []):
        #    host_dir, container_dir = m.split(':')

        try:
            s: CSMService = self.run(**container_def)
        except docker.errors.APIError as e:
            if e.response.status_code == 409:
                logger.error("Container for %s already exists: %s", username, e)
                return self.get_by_username(username)
            else:
                logger.error("Error creating container: %s", e)
                return None

        ch: CodeHost = s.to_model(no_container=True)
        db.session.add(ch)
        db.session.commit()

        return s

    def list(
        self, filters: Optional[Dict[str, Any]] = {"label": "jtl.codeserver"}
    ) -> List[CSMService]:
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

        service_ids = {ch.service_id for ch in CodeHost.query.all()}

        in_db = {ch.service_id for ch in CodeHost.query.all()}
        in_swarm = {s.id for s in self.list()}

        not_in_db = in_swarm - in_db
        not_in_swarm = in_db - in_swarm

        # Mark the missing services as missing in action
        for service_id in not_in_db:
            ch = CodeHost.query.filter_by(service_id=service_id).first()
            if ch:
                ch.state = "mia"  # "Missing in Action"
                ch.app_state = "mia"
            db.session.commit()

        # Get the CodeHosts records that have state != 'running' or 'app_state' != 'ready'
        not_ready_hosts = CodeHost.query.filter(
            (CodeHost.state != 'running') | (CodeHost.app_state != 'ready')
        ).all()

        # Update remaining services.
        for ch in not_ready_hosts:
            s: CSMService = self.get(ch.service_id)
            logger.info("Syncing service %s", s.name)
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
        Get a Code Server instance by username.

        Args:
            username (str): Username for the instance.

        Returns:
            CSMService: Code Server instance.
        """
