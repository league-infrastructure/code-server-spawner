import docker

from pymongo import MongoClient
from typing import List, Dict, Any, Optional
from jtlutil.docker.manager import DbServicesManager
from jtlutil.docker.proc import Service
from jtlutil.docker.dctl import define_cs_container
from time import sleep
from flask import current_app
import requests

import logging
logger = logging.getLogger('cspawnctl')

class CSMService(Service):
    
    def stop(self):
        """Remove the process."""
        self.manager.repo.remove_by_id(self.id)
        self.remove()
        

        
    @property   
    def hostname(self):
        return self.labels.get('caddy')
        
        
    @property
    def hostname_url(self):
        return f"https://{self.hostname}"
        
    def update(self, **kwargs):
        self.reload()
        ci = list(self.containers_info())[0]
        ci.update(kwargs)
        self.manager.repo.update(ci)
        
        
    def is_ready(self):
        """Check if the server is ready by making a request to it."""
    
        try:
            response = requests.get(self.hostname_url)
            logger.debug(f"Response from {self.hostname_url}: {response.status_code}")
            return response.status_code in [200, 302]
        except requests.exceptions.SSLError:
            logger.debug(f"SSL error encountered when connecting to {self.hostname_url}")
            return False
        except requests.exceptions.RequestException as e:
            logger.debug(f"Error checking server statusto {self.hostname_url}: {e}")
            return False

    def wait_until_ready(self, timeout=60):
        from time import time
        
        start_time = time()
        
        while True:
            self.update(state='starting')
            wait_time = time() - start_time    
            if self.is_ready():
                logger.info(f"Service {self.name} is ready, time elapsed: {wait_time}")
                break
            
            sleep(3)
            logger.info(f"Waiting for {self.name} to start, time elapsed: {wait_time}")
            
            if wait_time > timeout:
                logger.info(f"Service {self.name} failed to start after 40 seconds")
                break

        self.update()
        return wait_time

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

        # Wait for there to be a container ID
        while True:
            ci = list(s.containers_info())[0]
            if ci['container_id'] is not None:
                break
            sleep(.5)
            s.reload()
            
        
        s.update()
        
        return s

    def list(self, filters: Optional[Dict[str, Any]] = {"label":"jtl.codeserver"}) -> List[docker.models.containers.Container]:
        return super().list(filters=filters)

    def containers_list_cached(self):
        from jtlutil.docker.db import DockerContainerStats
        
        return self.repo.all
         

    def remove_all(self):
        for c in self.list(filter={"label":"jtl.codeserver"}):
            self.repo.remove_by_id(c.service_id)
            c.remove()
            
            

    