from paramiko.ssh_exception import NoValidConnectionsError
import logging
from typing import Any, List, Generator
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
    def env(self) -> dict:
        """Return the environment variables of the process."""
        return self._object.attrs.keys()
        return self._object.attrs['Spec']['TaskTemplate']['ContainerSpec']['Env']

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
    def containers(self) -> Generator[Container, None, None]:
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
            except (NoValidConnectionsError, ConnectionError, OSError) as e:
                logger.error(
                    f"Error connecting to node {node_name} for container {t}: {e}"
                )
                continue

            if n_manager is None:
                logger.error(
                    f"Node manager is None for node {node_name}, skipping container {t}"
                )
                continue

            # The actual Docker inspect call tunnels over SSH to the node. A
            # transient blip can leave a half-open tunnel that broken-pipes on
            # every reuse even though the node is healthy and a *fresh* ssh
            # connects fine. Rebuild the node manager (which constructs a new
            # ssh subprocess) and retry once before giving up on the container.
            cont = None
            for attempt in (1, 2):
                try:
                    cont = n_manager.get(container_id)
                    break
                except (ConnectionError, OSError) as e:
                    if attempt == 1:
                        logger.warning(
                            f"Stale connection to node {node_name} inspecting "
                            f"{container_id}; rebuilding and retrying: {e}"
                        )
                        try:
                            n_manager = self.manager._node_manager(node_name)
                        except (NoValidConnectionsError, ConnectionError, OSError) as e2:
                            logger.error(
                                f"Rebuild of node manager for {node_name} failed: {e2}"
                            )
                            n_manager = None
                        if n_manager is None:
                            break
                    else:
                        logger.error(
                            f"Error inspecting container {container_id} on node "
                            f"{node_name} after retry: {e}"
                        )
            if cont is None:
                continue

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
        """Tasks for this service that have a container, ordered so the task
        swarm currently wants running comes first (newest as tiebreak).

        Swarm retains historical Shutdown tasks after a reschedule, so a 1/1
        service can have several container-bearing tasks. Without this ordering,
        consumers that take the first task (`status`, `_get_single_task`,
        `next(self.containers)` in `to_model`) could latch onto a stale Shutdown
        task and mislabel a live service as 'shutdown' on the wrong node.
        """

        tasks = []
        for t in self._object.tasks():
            try:
                if t["Status"]["ContainerStatus"]["ContainerID"]:
                    tasks.append(t)
            except KeyError:
                # A task with no ContainerStatus is almost always one Swarm has
                # accepted but not yet scheduled/started (state new/pending/
                # assigned) — expected churn right after services.create(), and
                # the dominant case during a concurrent `test start`. Only a task
                # that actually failed/rejected is worth surfacing loudly; the
                # rest is normal startup timing, so log at DEBUG.
                state = (t.get("Status", {}) or {}).get("State", "unknown")
                if state in ("failed", "rejected"):
                    err = (t.get("Status", {}) or {}).get("Err", "")
                    logger.error(
                        f"Task {t['ID']} in service {self.name} {state} before "
                        f"getting a ContainerStatus: {err}"
                    )
                else:
                    logger.debug(
                        f"Task {t['ID']} in service {self.name} has no ContainerStatus "
                        f"yet (state={state}); not scheduled/started — skipping."
                    )
                continue

        def _rank(t):
            live = t.get("DesiredState") == "running" or t["Status"]["State"] == "running"
            return (live, t["Status"].get("Timestamp", ""))

        tasks.sort(key=_rank, reverse=True)
        yield from tasks

    def _get_single_task(self):
        """Fetch the single task associated with this service."""
        try:
            return next(self.container_tasks)
        except StopIteration:
            # No container-bearing task yet — normal immediately after create
            # while Swarm schedules the container. Callers handle None.
            logger.debug(f"No running task yet for service {self.name}")
            return None


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
