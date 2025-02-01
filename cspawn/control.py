import docker

from pymongo import MongoClient
from typing import List, Dict, Any, Optional
from jtlutil.docker.manager import DbServicesManager
from jtlutil.docker.dctl import define_cs_container


class CodeServerManager(DbServicesManager):
    
    def __init__(self, config):
        
        self.config = config

        self.mongo_client = MongoClient(config.MONGO_URL)
        self.docker_client = docker.DockerClient(base_url=config.DOCKER_SSH_URI)
        
        def _hostname_f(node_name):
            return f"{node_name}.jointheleague.org"
        
    
        super().__init__(self.docker_client,
                         hostname_f=_hostname_f, mongo_client=self.mongo_client)
    
    
    def new_cs(self, username, image=None):
 
        container_def = define_cs_container(self.config, 
                                            image or self.config.IMAGES_PYTHONCS,
                                            username)
        
        c = self.run(**container_def)

        self.repo.update(c)


    def remove_all(self):
        for c in self.list(filter={"label":"jtl.codeserver"}):
            c.remove()