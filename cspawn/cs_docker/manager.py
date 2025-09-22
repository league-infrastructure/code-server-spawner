import datetime
import logging
from typing import Any, Dict, List, Optional

import docker

from .proc import Container, Service

logger = logging.getLogger("cspawn.docker")


class DockerManager:
    """Base class for managing both Docker Containers and Services."""

    def __init__(
        self,
        client: Any,
        env: Optional[Dict[str, str]] = None,
        network: Optional[List[str]] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Initialize the process group.
        :param client: Docker client instance.
        """
        self.client = client
        self.env = env or {}
        self.network = network or []
        self.labels = labels or {}

        self.info = self.client.info()

    @property
    def name(self):
        return self.info["Name"]

    @property
    def hostname(self):
        return self.client.api._custom_adapter.ssh_params["hostname"]

    @property
    def username(self):
        return self.client.api._custom_adapter.ssh_params["username"]

    def run(self, name: str, image: str, **kwargs: Any) -> Any:
        """Create and run a new process (container or service)."""
        raise NotImplementedError

    def get(self, name_or_id: str) -> Any:
        """Retrieve a process (container or service) by name or ID."""
        raise NotImplementedError

    def list(self) -> List[Any]:
        """List all processes (containers or services)."""
        raise NotImplementedError

    def combine_lists(self, list1: List[str], list2: List[str]) -> List[str]:
        """Combine two lists."""
        return list(set(list1 + list2))

    def combine_dicts(
        self, dict1: Dict[str, str], dict2: Dict[str, str]
    ) -> Dict[str, str]:
        """Combine two dictionaries."""
        combined = dict1.copy()
        combined.update(dict2)
        return combined

    def ensure_network(
        self,
        name: str,
        driver: str = None,
        internal: bool = False,
        ingress: bool = False,
    ) -> None:
        """Ensure a network exists.

        :param name: Name of the network.
        :param driver: Network driver to use.
        :param internal: Restrict network to internal use.
        :param ingress: Create an ingress network.
        """

        driver = driver or "bridge"

        try:
            self.client.networks.get(name)
        except docker.errors.NotFound:
            self.client.networks.create(
                name, driver=driver, internal=internal, ingress=ingress
            )

    @property
    def events(self):
        yield from self.client.events(decode=True)


class ContainersManager(DockerManager):
    """Manages Docker Containers with a consistent interface."""

    def run(
        self,
        image: str,
        name: str = None,
        labels: Dict[str, str] = {},
        environment: Dict[str, str] = {},
        ports: Dict[str, str] = {},
        volumes: Dict[str, str] = {},
        network: Optional[str] = [],
        restart_policy: Optional[str] = None,
        **kwargs: Dict[str, Any],
    ) -> Container:
        """
        Run a new container.
        :param name: Name of the container.
        :param image: Docker image to use.
        :param labels: Labels to apply to the container.
        :param environment: Environment variables for the container.
        :param ports: Port mappings for the container.
        :param volumes: Volume mappings for the container.
        :param network: Network for the container.
        :param restart_policy: Restart policy for the container.
        :param kwargs: Additional parameters.
        :return: A Container object.
        """

        container = self.client.containers.run(
            image=image,
            name=name,
            detach=True,
            labels=self.combine_dicts(self.labels, labels),
            environment=self.combine_dicts(self.env, environment),
            ports=ports,
            volumes=volumes,
            restart_policy=restart_policy,
            **kwargs,
        )

        network = self.combine_lists(self.network, network)
        # Connect to additional networks if specified
        for net in network or []:
            network = self.client.networks.get(net)
            network.connect(container)

        return Container(self, container)

    def get(self, name_or_id: str) -> Any:
        """Retrieve a container by name or ID."""
        container = self.client.containers.get(name_or_id)
        return Container(self, container)

    def list(
        self, filters: Dict = None, all=False, status=None, **kwargs: Any
    ) -> List[Any]:
        """List all containers."""
        return [
            Container(self, cont)
            for cont in self.client.containers.list(filters=filters, all=all, **kwargs)
        ]

    def only_one(self, filters: Dict, reset: bool = False) -> None:
        """Ensure only one container is running."""

        containers = self.list(filters=filters, all=True)

        if containers:
            logger.debug(f"Found {len(containers)} containers")
            first, rest = containers[0], containers[1:]

            for container in rest:  # Delete all but the first
                logger.debug(f"Removing container {container.name}")
                container.remove(force=True)

            if reset:
                logger.debug(f"Destroying container {first.name} because reset is True")
                first.remove(force=True)

            elif first.status == "exited":
                logger.debug(f"Starting container {first.name}")
                first.start()
                return first
            else:
                logger.debug(f"Container {first.name} is already running")
                return first

        return None  # Didn't find anything, or killed the one we found.

    def simple_stats(self, filters: Dict = None):
        """Get stats for all containers."""
        for c in self.list(filters=filters):
            ss = c.simple_stats
            ss["node_name"] = self.name
            yield ss


class ServicesManager(DockerManager):
    """Manages Docker Services (Swarm mode) with a consistent interface."""

    service_class = Service

    def __init__(
        self,
        client: Any,
        env: Dict[str, str] = None,
        network: List[str] = None,
        labels: Dict[str, str] = None,
        hostname_f=None,
    ) -> None:
        """
        Initialize the services manager.
        :param client: Docker client instance.
        """


        super().__init__(client, env, network, labels)

        self.hostname_f = hostname_f or (lambda x: x)


    def _node_manager(self, node_name):
        """Return a ContainersManager for a specific node."""
        import docker

        all_nodes = self.client.nodes.list()

        if len(all_nodes) == 1:
             # Only one node in the swarm, so we can use the local client
            return ContainersManager(self.client)
            
        else:
            node_host = self.hostname_f(node_name)
            base_url=f"ssh://root@{node_host}"
            try:

                return ContainersManager(docker.DockerClient(base_url=base_url))
            except Exception as e:
                logger.error(f"Failed to create Docker client for {node_name}, base_url: {base_url}, error: {e}")
                return None

    @property
    def nodes(self):
        for n in self.client.nodes.list():
            node_name = n.attrs["Description"]["Hostname"]

            yield self._node_manager(node_name)

    def run(
        self,
        image: str,
        name: str = None,
        maxreplicas: int = 1,
        labels: Dict[str, str] = {},
        environment: Dict[str, str] = {},
        mounts: List[str] = [],
        network: List[str] = [],
        restart_policy: Optional[str] = None,
        **kwargs: Any,
    ) -> Service:
        """
        Create a new service.
        :param name: Name of the service.
        :param image: Docker image to use.
        :param labels: Labels to apply to the service.
        :param environment: Environment variables for the service.
        :param mounts: Mounts for the service.
        :param networks: Networks for the service.
        :param restart_policy: Restart policy for the service.
        :param kwargs: Additional parameters.
        :return: A Service object.
        """

 
        if  "ports" in kwargs:
            logger.info(f"ServicesManager.run: ports: {kwargs['ports']}")
            # Build a Swarm EndpointSpec with explicit published/target ports
            try:
                from docker.types import EndpointSpec
            except Exception:
                EndpointSpec = None  # type: ignore

            port_configs = []

            def add_port(published: int, target: int, protocol: str = "tcp", mode: str = "ingress"):
                # docker SDK accepts dicts with these keys inside EndpointSpec(ports=[...])
                port_configs.append(
                    {
                        "Protocol": protocol,
                        "PublishedPort": int(published),
                        "TargetPort": int(target),
                        "PublishMode": mode,
                    }
                )

            ports_val = kwargs["ports"]
            if isinstance(ports_val, list):
                for item in ports_val:
                    if isinstance(item, str) and ":" in item:
                        host_port, container_port = item.split(":", 1)
                        add_port(int(host_port), int(container_port))
            elif isinstance(ports_val, dict):
                # Allow dict form {host: container}
                for host_port, container_port in ports_val.items():
                    add_port(int(host_port), int(container_port))

            if EndpointSpec and port_configs:
                kwargs["endpoint_spec"] = EndpointSpec(ports=port_configs)
            else:
                logger.warning("No valid port mappings found; ports will not be published")

            # Remove raw 'ports' from kwargs; Swarm uses endpoint_spec
            del kwargs["ports"]

        network = self.combine_lists(self.network, network)

        # Convert environment dict to list of key=value strings
        env = self.combine_dicts(self.env, environment)
        env_list = [f"{key}={value}" for key, value in env.items()]

        # No placement constraints: allow the scheduler to place services on any node
        kwargs.pop("placement", None)

        service = self.client.services.create(
            image=image,
            name=name,
            maxreplicas=maxreplicas,
            labels=self.combine_dicts(self.labels, labels),
            container_labels=self.combine_dicts(self.labels, labels),
            env=env_list,
            mounts=mounts,
            networks=network,
            restart_policy=restart_policy,
            **kwargs,
        )
        return self.service_class(self, service)

    def get(self, name_or_id: str) -> Any:
        """Retrieve a service by name or ID."""
        try:
            service = self.client.services.get(name_or_id)
            return self.service_class(self, service)
        except docker.errors.NotFound:
            return None

    def list(
        self, filters: Dict = None, status: bool = False, all=None, **kwargs: Any
    ) -> List[Any]:
        """List all services."""
        return [
            self.service_class(self, svc)
            for svc in self.client.services.list(
                filters=filters, status=status, **kwargs
            )
        ]

    @property
    def containers(self):
        for s in self.list():
            for ci in s.containers_info():
                yield ci

    def simple_stats(self, filters: Dict = None):
        """Get stats for all services."""

        for n in self.nodes:
            yield from n.simple_stats(filters=filters)

    def only_one(self, filters: Dict, reset: bool = False) -> None:
        """Ensure only one service is running."""

        services = self.list(filters=filters, all=True)

        if services:
            logger.debug(f"Found {len(services)} services")
            first, rest = services[0], services[1:]

            for service in rest:  # Remove all but the first
                logger.debug(f"Removing service {service.name}")
                service.remove()

            if reset:
                logger.debug(f"Destroying service {first.name} because reset is True")
                first.remove()
            else:
                logger.debug(f"Service {first.name} is already running")
                return first

        return None  # Didn't find anything, or killed the one we found.

    def ensure_network(
        self,
        name: str,
        driver: str = None,
        internal: bool = False,
        ingress: bool = False,
    ) -> None:
        """Ensure a network exists.

        :param name: Name of the network.
        :param driver: Network driver to use.
        :param internal: Restrict network to internal use.
        :param ingress: Create an ingress network.
        """

        driver = driver or "overlay"

        super().ensure_network(name, driver=driver, internal=internal, ingress=ingress)
