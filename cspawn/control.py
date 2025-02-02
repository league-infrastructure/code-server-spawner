import docker

from pymongo import MongoClient
from typing import List, Dict, Any, Optional
from jtlutil.docker.manager import DbServicesManager
from jtlutil.docker.proc import Service
from jtlutil.docker.dctl import define_cs_container
from time import sleep

class CSMService(Service):
    
    pass

class CodeServerManager(DbServicesManager):
    
    service_class = CSMService
    
    def __init__(self, app):
        
        self.config = app.app_config

        self.mongo_client = app.mongodb.cx
        
        self.docker_client = docker.DockerClient(base_url=self.config.DOCKER_SSH_URI)
        
        def _hostname_f(node_name):
            return f"{node_name}.jointheleague.org"
        
    
        super().__init__(self.docker_client,
                         hostname_f=_hostname_f, mongo_client=self.mongo_client)
    
    
    def new_cs(self, username, image=None):
 
        container_def = define_cs_container(self.config, 
                                            image or self.config.IMAGES_PYTHONCS,
                                            username,
                                            self.config.HOSTNAME_TEMPLATE)
        
        s = self.run(**container_def)

        while True:
            ci = list(s.containers_info())[0]
            if ci['container_id'] is not None:
                break
            sleep(.5)
            s.reload()
            
        
        self.repo.update(ci) 
        
        return s

    def list(self, filters: Optional[Dict[str, Any]] = {"label":"jtl.codeserver"}) -> List[docker.models.containers.Container]:
        return super().list(filters=filters)

    def remove_all(self):
        for c in self.list(filter={"label":"jtl.codeserver"}):
            self.repo.remove_by_id(c.id)
            c.remove()