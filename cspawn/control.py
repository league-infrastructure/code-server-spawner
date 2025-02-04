import docker

from pymongo import MongoClient
from typing import List, Dict, Any, Optional
from jtlutil.docker.manager import DbServicesManager
from jtlutil.docker.proc import Service

from time import sleep
from flask import current_app
from cspawn.db import KeyrateDBHandler
from functools import lru_cache
import requests
from slugify import slugify
from datetime import datetime
import pytz

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
            
            sleep(.5)
            logger.info(f"Waiting for {self.name} to start, time elapsed: {wait_time}")
            
            if wait_time > timeout:
                logger.info(f"Service {self.name} failed to start after 40 seconds")
                break

        self.update()
        return wait_time

def define_cs_container(config, image, username, hostname_template, env_vars={}, port=None):
    # Create the container
    
    container_name = name = slugify(username)
 
    password = "code4life"
    
    hostname = hostname_template.format(username=container_name)

    _env_vars = {
        "PASSWORD": password,
        "DISPLAY": ":0",
        "VNC_URL": f"https://{hostname}/vnc/",
        "KST_REPORTING_URL": config.KST_REPORTING_URL,
        "KST_CONTAINER_ID": name,
		"KST_REPORT_RATE": config.KST_REPORT_RATE if hasattr(config, "KST_REPORT_RATE") else 30,
        "CS_DISABLE_GETTING_STARTED_OVERRIDE": "1" # Disable the getting started page
    }
    
    env_vars = {**_env_vars, **env_vars}
    
    labels = {
        "jtl": 'true', 
        "jtl.codeserver": 'true',  
        "jtl.codeserver.username": username,
        "jt.codeserver.password": password,
        "jtl.codeserver.start_time": datetime.now(pytz.timezone('America/Los_Angeles')).isoformat(),
                
        "caddy": hostname,
        "caddy.@ws.0_header": "Connection *Upgrade*",
        "caddy.@ws.1_header": "Upgrade websocket",
        "caddy.0_route.handle": "/websockify*",
        "caddy.0_route.handle.reverse_proxy": "@ws {{upstreams 6080}}",
        "caddy.1_route.handle": "/vnc/*",
        "caddy.1_route.handle_path": "/vnc/*",
        "caddy.1_route.handle_path.reverse_proxy": "{{upstreams 6080}}",
        "caddy.2_route.handle": "/*",
        "caddy.2_route.handle.reverse_proxy": "{{upstreams 8080}}"
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
        "network" : ["caddy", "jtlctl"],
        
    }

class CodeServerManager(DbServicesManager):
    
    service_class = CSMService
    
    def __init__(self, app):
        
        self.config = app.app_config

        self.mongo_client = app.mongodb.cx
        
        self.docker_client = docker.DockerClient(base_url=self.config.DOCKER_SSH_URI)
        self.mongo_db = self.mongo_client[app.config['CSM_MONGO_DB_NAME']]
        
        def _hostname_f(node_name):
            return f"{node_name}.jointheleague.org"
        
    
        super().__init__(self.docker_client,
                         hostname_f=_hostname_f, mongo_db=self.mongo_db)
    
    
    @property
    @lru_cache()
    def keyrate(self):
        return KeyrateDBHandler(self.mongo_db)
    
    
    def new_cs(self, username, image=None):
 
        container_def = define_cs_container(self.config, 
                                            image or self.config.IMAGES_PYTHONCS,
                                            username,
                                            self.config.HOSTNAME_TEMPLATE)
        
        try:
            s = self.run(**container_def)
        except docker.errors.APIError as e:
            if e.response.status_code == 409:
                logger.error(f"Container for {username} already exists: {e}")
                return self.get_by_username(username)
            else:
                logger.error(f"Error creating container: {e}")
                return None

        # Wait for there to be a container ID
        while True:
            s.reload()
            try:
                ci = list(s.containers_info())[0]
            except IndexError:
                sleep(1)               
                continue
            
            if ci['container_id'] is not None:
                break
            sleep(.5)
         
            
        
        s.update()
        
        return s

    def list(self, filters: Optional[Dict[str, Any]] = {"label":"jtl.codeserver"}) -> List[docker.models.containers.Container]:
        return super().list(filters=filters)

    def containers_list_cached(self):
        from jtlutil.docker.db import DockerContainerStats
        
        return self.repo.all
         

    def remove_all(self):
        for c in self.list():
            logger.info(f"Removing container {c.name} ({c.id})")
            self.repo.remove_by_id(c.id)
            c.remove()
            
            
    def get_by_hostname(self, username):
        r =  self.repo.find_by_hostname(username)
        
        if r:
            return self.get(r.service_id)
        else:
            return None
    
    def get_by_username(self, username):
     
        r =  self.repo.find_by_username_label(username)
        
        if r:
            return self.get(r.service_id)
        else:
            return None
    