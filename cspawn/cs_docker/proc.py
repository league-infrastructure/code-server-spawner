from paramiko.ssh_exception import NoValidConnectionsError
import logging
from typing import Any
from docker.client import DockerClient
from docker.models.containers import Container as DockerContainer
from docker.models.services import Service as DockerService
logger = logging.getLogger("cspawn.docker")


class ProcessBase:
    """Base class for both Container and Service objects."""

    client: DockerClient
    _object: DockerContainer | DockerService

    def __init__(self, manager, obj):
        """
        Initialize a process object.
        :param client: Docker client instance.
        :param obj: The low-level container or service object from Docker SDK.
        """
        self.manager = manager
        self.client = manager.client
        self._object = obj

    def reload(self):
        """Reload the object and refresh all its data."""
        self._object.reload()

    def remove(self):
        """Remove the process."""
        self._object.remove()

    @property
    def o(self) ->  DockerContainer | DockerService:
        return self._object

    @property
    def id(self) -> str:
        """Return the ID of the process."""
        return self._object.id

    @property
    def attrs(self) -> dict:
        """Return all attributes of the process."""
        return self._object.attrs

    @property
    def name(self):
        """Return the name of the process."""
        # Default to the name key in attributes or a placeholder.
        return self._object.attrs.get("Name", "Unnamed")

    @property
    def status(self) -> str:
        """Return the status of the process."""
        raise NotImplementedError("Subclasses must implement the status property")

    def reload(self):
        """Reload the object and refresh all its data."""
        self._object.reload()


class Container(ProcessBase):
    """Represents a single Docker container."""

    node = None

    def __init__(self, manager, obj, node=None):
        self.node = node
        super().__init__(manager, obj)

    def start(self):
        """Start the container."""
        self._object.start()

    @property
    def labels(self):
        """Return the labels associated with the container."""
        return self._object.labels

    @property
    def status(self):
        """Return the current status of the container."""
        return self._object.status

    @property
    def stats(self):
        """Get container stats."""
        return self._object.stats(stream=False)

    @property
    def simple_stats(self):
        mem = self.stats["memory_stats"]["usage"]
        return {
            "container_id": self.o.id,
            "state": self.o.status,
            "container_name": self.o.name,
            "memory_usage": mem,
            "hostname": self.o.labels.get("caddy"),
        }

    def remove(self):
        """Remove the process."""
        self._object.remove(force=True)

    def stop(self):
        """Remove the process."""
        self._object.stop()

    def node_id(self):
        return self.node.attrs.get("NodeID")

    def node_name(self):
        return self.node.attrs.get("Description", {}).get("Hostname")


class Service(ProcessBase):
    """Represents a single Docker service (for Swarm mode)."""



    def start(self):
        """Starting a service is not typically required (it auto-runs)."""
        raise NotImplementedError("Services do not support explicit start()")

    def stats(self):
        """Service stats are obtained from the associated task/container."""
        task = self._get_single_task()
        if task:
            container_id = task["Status"].get("ContainerStatus", {}).get("ContainerID")
            if container_id:
                container = self.client.containers.get(container_id)
                return container.stats(stream=False)

    @property
    def container_states(self):
        """Return the container states associated with the service."""
        try:
            return {
                t["Status"]["ContainerStatus"]["ContainerID"]: t["Status"]["State"]
                for t in self.container_tasks
            }
        except KeyError:
            return {}

    @property
    def containers(self):
        """Return the containers associated with the service."""

        for t in self.container_tasks:
            node_id = t["NodeID"]
            node = self.manager.client.nodes.get(node_id)

            node_name = node.attrs.get("Description", {}).get("Hostname")

            container_id = t["Status"]["ContainerStatus"]["ContainerID"]

            if not container_id:

                logger.error(
                    f"Container ID not found for task {t['ID']} in service {self.name}"
                )

                if t['Status']['State'] in ('failed', 'rejected'):
                    logger.error(
                        f"Task {t['ID']} in service {self.name} has state {t['Status']['State'] }. " +
                        f"Container ID: \"{container_id}\"" +
                        "Error:" + t['Status']['Err'] if 'Err' in t['Status'] else ''

                    )
                continue


            try:
                n_manager = self.manager._node_manager(node_name)
            except NoValidConnectionsError as e:
                logger.error(
                    f"Error connecting to node {node_name} for container {t}: {e}"
                )
                continue

            cont: Container = n_manager.get(container_id)
           
            cont.node = node

            yield cont

    def containers_info(self):
        for t in self.container_tasks:
            labels = t["Spec"]["ContainerSpec"]["Labels"]
            hostname = labels.get("caddy")
            labels = {k: v for k, v in labels.items() if not k.startswith("caddy")}

            yield {
                "service_id": self.id,
                "service_name": self.name,
                "container_id": t["Status"]
                .get("ContainerStatus", {})
                .get("ContainerID"),
                "node_id": t.get("NodeID"),
                "state": t["Status"]["State"],
                "image_uri": t["Spec"]["ContainerSpec"]["Image"],
                "hostname": hostname,
                "timestamp": t["Status"]["Timestamp"],
                "labels": labels,
            }

    @property
    def tasks(self):
        """Return the tasks associated with the service."""
        return self._object.tasks()

    @property
    def running_tasks(self):
        """Return the tasks associated with the service."""

        for t in self._object.tasks():
            if t['Status']['State'] == "running":
                yield t

    @property
    def container_tasks(self):
        """Return the tasks associated with the service."""

        for t in self._object.tasks():
            container_id = t["Status"]["ContainerStatus"]["ContainerID"]
            if container_id:
                yield t

    def _get_single_task(self):
        """Fetch the single task associated with this service."""
        return next(self.container_tasks)


    @property
    def labels(self) -> dict:
        """Return the labels associated with the container."""
        return self._object.attrs["Spec"]["Labels"]

    @property
    def env(self) -> dict:
        """Return the labels associated with the container."""
        env_lines = self._object.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"]
        return dict([tuple(e.split("=", 1)) for e in env_lines])

    @property
    def image(self) -> str:
        return self.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"]

    @property
    def status(self):
        """Return the status of the service based on its task."""
        task = self._get_single_task()
        if task:
            return task["Status"]["State"]
        return "unknown"

    @property
    def name(self):
        """Return the name of the service."""
        return self._object.attrs.get("Spec", {}).get("Name", "Unnamed")

    def stop(self):
        """Remove the process."""
        self._object.remove()
