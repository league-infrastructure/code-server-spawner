import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import sleep
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import docker
import paramiko
import pytz
import requests
from pymongo.collection import Collection
from pymongo.database import Database as MongoDatabase
from slugify import slugify

from cspawn.docker.manager import DbServicesManager
from cspawn.docker.proc import Service
from cspawn.main.models import User

logger = logging.getLogger("cspawnctl")


class CSMService(Service):
    """
    A service class for managing Code Server instances.
    """

    def stop(self):
        """Remove the process."""
        self.manager.repo.remove_by_id(self.id)
        self.remove()

    @property
    def hostname(self):
        """Return the hostname of the service."""
        return self.labels.get("caddy")

    @property
    def hostname_url(self):
        """Return the URL of the hostname."""
        return f"https://{self.hostname}"

    def update(self, **kwargs):
        """Update the service with the given keyword arguments."""
        self.reload()
        ci = list(self.containers_info())[0]
        ci.update(kwargs)
        self.manager.repo.update(ci)

    def is_ready(self):
        """Check if the server is ready by making a request to it."""
        try:
            response = requests.get(self.hostname_url, timeout=10)
            logger.debug("Response from %s: %s", self.hostname_url, response.status_code)
            return response.status_code in [200, 302]
        except requests.exceptions.SSLError:
            logger.debug("SSL error encountered when connecting to %s", self.hostname_url)
            return False
        except requests.exceptions.RequestException as e:
            logger.debug("Error checking server status to %s: %s", self.hostname_url, e)
            return False

    def wait_until_ready(self, timeout=60):
        """Wait until the server is ready or the timeout is reached."""
        from time import time

        start_time = time()

        while True:
            self.update(state="starting")
            wait_time = time() - start_time
            if self.is_ready():
                logger.info("Service %s is ready, time elapsed: %s", self.name, wait_time)
                break

            sleep(0.5)
            logger.info("Waiting for %s to start, time elapsed: %s", self.name, wait_time)

            if wait_time > timeout:
                logger.info("Service %s failed to start after 40 seconds", self.name)
                break

        self.update()
        return wait_time


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
        "VNC_URL": "http://localhost:6080",
        "KST_REPORTING_URL": config.KST_REPORTING_URL,
        "KST_CONTAINER_ID": name,
        "KST_REPORT_RATE": (config.KST_REPORT_RATE if hasattr(config, "KST_REPORT_RATE") else 30),
        "CS_DISABLE_GETTING_STARTED_OVERRIDE": "1",  # Disable the getting started page
        "INITIAL_GIT_REPO": repo,
        "JTL_SYLLABUS": syllabus,
    }

    env_vars = {**_env_vars, **env_vars}

    labels = {
        "jtl": "true",
        "jtl.codeserver": "true",
        "jtl.codeserver.username": username,
        "jt.codeserver.password": password,
        "jtl.codeserver.start_time": datetime.now(pytz.timezone("America/Los_Angeles")).isoformat(),
        "caddy": hostname,
        "caddy.@ws.0_header": "Connection *Upgrade*",
        "caddy.@ws.1_header": "Upgrade websocket",
        "caddy.0_route.handle": "/websockify*",
        "caddy.0_route.handle.reverse_proxy": "@ws {{upstreams 6080}}",
        "caddy.1_route.handle": "/vnc/*",
        "caddy.1_route.handle_path": "/vnc/*",
        "caddy.1_route.handle_path.reverse_proxy": "{{upstreams 6080}}",
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


class KeyrateDBHandler:
    def __init__(self, mongo_db: MongoDatabase):
        """
        Initialize the KeyrateDBHandler.

        Args:
            mongo_db (MongoDatabase): MongoDB database instance.
        """
        assert isinstance(mongo_db, MongoDatabase)

        self.db = mongo_db
        self.collection: Collection = self.db["keyrate"]
        self.collection.create_index("serviceID")
        self.collection.create_index("timestamp")

    def add_report(self, report: Dict):
        """
        Add a report to the database.

        Args:
            report (Dict): Report data.
        """
        self.collection.insert_one(report)

    def summarize_latest(self, services: Optional[List[str]] = None):
        """
        Summarize the latest reports for the given services.

        Args:
            services (Optional[List[str]]): List of service IDs to summarize.

        Yields:
            dict: Latest report for each service.
        """
        pipeline = [
            ({"$match": {"serviceID": {"$in": services}}} if services else {"$match": {}}),
            {"$sort": {"timestamp": -1}},
            {"$group": {"_id": "$serviceID", "latestReport": {"$first": "$$ROOT"}}},
        ]

        results = self.collection.aggregate(pipeline)

        now = datetime.now(timezone.utc)

        for result in results:
            report = result["latestReport"]
            timestamp = datetime.fromisoformat(report["timestamp"])
            heartbeat_ago = int((now - timestamp).total_seconds())

            yield report

    def delete_all(self):
        """Delete all reports from the database."""
        self.collection.delete_many({})

    def __len__(self):
        """Return the number of reports in the database."""
        return self.collection.count_documents({})


class CodeServerManager(DbServicesManager):

    service_class = CSMService

    def __init__(self, app):
        """
        Initialize the CodeServerManager.

        Args:
            app: Application instance.
        """
        self.config = app.app_config

        self.mongo_client = app.mongodb.cx

        self.docker_client = docker.DockerClient(base_url=self.config.DOCKER_URI)
        self.mongo_db = self.mongo_client[app.config["CSM_MONGO_DB_NAME"]]

        def _hostname_f(node_name):
            return f"{node_name}.jointheleague.org"

        super().__init__(self.docker_client, hostname_f=_hostname_f, mongo_db=self.mongo_db)

    @property
    @lru_cache()
    def keyrate(self):
        """Return the KeyrateDBHandler instance."""
        return KeyrateDBHandler(self.mongo_db)

    def make_user_dir(self, username):
        """
        Create a user directory.

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

            _, stdout, stderr = ssh.exec_command(f"mkdir -p {user_dir} && chown -R {user_id}:{user_id} {user_dir}")
            exit_status = stdout.channel.recv_exit_status()

            if exit_status != 0:
                logger.error("Failed to create directory %s on remote host: %s", user_dir, stderr.read().decode())
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

        # import yaml
        # logger.debug(f"Container Definition\n {yaml.dump(container_def)}")

        self.make_user_dir(username)

        # For later, maybe there are other mounts.
        # for m in container_def.get('mounts', []):
        #    host_dir, container_dir = m.split(':')

        try:
            s = self.run(**container_def)
        except docker.errors.APIError as e:
            if e.response.status_code == 409:
                logger.error("Container for %s already exists: %s", username, e)
                return self.get_by_username(username)
            else:
                logger.error("Error creating container: %s", e)
                return None

        # Wait for there to be a container ID
        while True:
            s.reload()
            try:
                ci = list(s.containers_info())[0]
            except IndexError:
                sleep(1)
                continue

            if ci["container_id"] is not None:
                break
            sleep(0.5)

        s.update()

        return s

    def list(
        self, filters: Optional[Dict[str, Any]] = {"label": "jtl.codeserver"}
    ) -> List[docker.models.containers.Container]:
        """
        List all Code Server instances.

        Args:
            filters (Optional[Dict[str, Any]]): Filters to apply.

        Returns:
            List[docker.models.containers.Container]: List of containers.
        """
        return super().list(filters=filters)

    def containers_list_cached(self):
        """Return the cached list of containers."""
        from cspawn.docker.db import DockerContainerStats

        return self.repo.all

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
        r = self.repo.find_by_hostname(username)

        if r:
            return self.get(r.service_id)
        else:
            return None

    def get_by_username(self, username):
        """
        Get a Code Server instance by username.

        Args:
            username (str): Username for the instance.

        Returns:
            CSMService: Code Server instance.
        """
        r = self.repo.find_by_username_label(username)

        if r:
            return self.get(r.service_id)
        else:
            return None
