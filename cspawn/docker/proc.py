


class ProcessBase:
    """Base class for both Container and Service objects."""
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
    def o(self):
        return self._object
    
    @property
    def id(self):
        """Return the ID of the process."""
        return self._object.id
        
    @property
    def attrs(self):
        """Return all attributes of the process."""
        return self._object.attrs

    @property
    def name(self):
        """Return the name of the process."""
        # Default to the name key in attributes or a placeholder.
        return self._object.attrs.get("Name", "Unnamed")

    @property
    def status(self):
        """Return the status of the process."""
        raise NotImplementedError("Subclasses must implement the status property")

    def reload(self):
        """Reload the object and refresh all its data."""
        self._object.reload()

class Container(ProcessBase):
    """Represents a single Docker container."""
    
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
     
        mem = self.stats['memory_stats']['usage']
        return {
            'container_id': self.o.id,
            'state':self.o.status,
            'container_name': self.o.name,
            'memory_usage': mem,
            'hostname': self.o.labels.get('caddy')
        }


    def remove(self):
        """Remove the process."""
        self._object.remove(force=True)


    def stop(self):
        """Remove the process."""
        self._object.stop()

class Service(ProcessBase):
    """Represents a single Docker service (for Swarm mode)."""
    
    def _get_single_task(self):
        """Fetch the single task associated with this service."""
        tasks = self._object.tasks()
        if tasks:
            return tasks[0]  # Assuming only one task per service as specified.
        return None
    
    
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


    def containers_info(self):
        
        for t in self.tasks:
            
            labels = t['Spec']['ContainerSpec']['Labels']
            hostname = labels.get('caddy')
            labels = {k: v for k, v in labels.items() if not k.startswith('caddy')}
            
            import pprint
            pprint(t)
            
            yield {
                'service_id': self.id,
                'service_name': self.name,
                'container_id':t["Status"].get("ContainerStatus", {}).get("ContainerID"),
                'node_id': t.get('NodeID'),
                'state': t['Status']['State'],
                'hostname': hostname,
                'timestamp': t['Status']['Timestamp'],
                'labels': labels,
                
            }
    

    @property
    def tasks(self):
        """Return the tasks associated with the service."""
        return self._object.tasks()

    @property
    def labels(self):
        """Return the labels associated with the container."""
        return self._object.attrs['Spec']['Labels']

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
