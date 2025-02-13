from pydantic import BaseModel
from typing import Optional
from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError
from typing import Union
from datetime import datetime


class ServiceTelemetry(BaseModel):
    id: str
    name: str
    created: datetime
    
    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}  # Ensures ISO 8601 format


class DockerContainerStats(BaseModel):
    service_id: Optional[str] = None
    service_name: Optional[str] = None
    container_id: str
    container_name: Optional[str] = None
    node_id: Optional[str] = None
    node_name: Optional[str] = None
    state: Optional[str] = None
    timestamp: Optional[datetime] = None
    
    hostname: Optional[str] = None
    username: Optional[str] = None
    memory_usage: Optional[int] = None
    last_stats: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None # last time the container service reported being alive
    last_utilization: Optional[datetime] = None # last time the container service reported utilization
    utilization_1: Optional[float] = None # Arbitrary measure of how busy the container is
    utilization_2: Optional[float] = None # Arbitrary measure of how busy the container is
    
    data: Optional[dict] = None
    labels: Optional[dict] = None
    
    @property
    def password(self):
         return self.labels['jt.codeserver.password'] 
    
    @property
    def heart_beat_ago(self):
        try:
            return datetime.now().astimezone() - self.last_heartbeat
        except:
            return None
        
    @property
    def seconds_since_last_stats(self):
        try:
            return (datetime.now().astimezone() - self.last_stats).total_seconds()
        except:
            return None
    
class DockerContainerStatsRepository:
    def __init__(self, db: MongoDatabase ):
        
        assert isinstance(db, MongoDatabase)
        
        self.db = db
        self.collection = self.db['container_stats']
        
        #self.collection.create_index("name", unique=True)
        self.collection.create_index("container_id", unique=True)
        

    def update(self, record: Union[dict, DockerContainerStats]):
        
        if not isinstance(record, dict):
            record = record.model_dump(exclude_none=False)
        else:
            record = DockerContainerStats(**record).model_dump(exclude_none=False)


        record['last_heartbeat'] = datetime.now().astimezone().isoformat() # set this for every record
        
        if record.get('utilization_1') is not None or record.get('utilization_2') is not None:
            record['last_utilization'] = datetime.now().astimezone().isoformat()
        
        if record['memory_usage'] is not None:
            record['last_stats'] = datetime.now().astimezone().isoformat()
        

        if record['username'] is None:
            record['username'] = record['labels'].get('jtl.codeserver.username')

        try:
            if 'container_id' not in record:
                raise ValueError("Record must have an 'container_id' value")
            
            self.collection.insert_one(record)
        except (DuplicateKeyError, ValueError) as e:
           
            record.pop('_id', None)
            
            # Never update field values to None
            update_fields = {k: v for k, v in record.items() if v is not None}
            
        
            self.collection.update_one(
                {"container_id": record["container_id"]},
                {"$set": update_fields}
            )

    def mark_all_unknown(self):
        """Mark all running containers as unknown, so we can find the ones that are no longer running."""
       
        self.collection.update_many(
            {"state": {"$ne": "unknown"}},
            {"$set": {"state": "unknown"}}
        )
       
    def remove_unknown(self):
        """Remove all containers that are in the unknown state."""
        self.collection.delete_many({"state": "unknown"})

    def find_by_service_id(self, service_id: str) -> Optional[DockerContainerStats]:
        result = self.collection.find_one({"service_id": service_id})
        if result:
            return DockerContainerStats(**result)
        return None
    
    
    def find_by_hostname(self, hostname: str) -> Optional[DockerContainerStats]:
        result = self.collection.find_one({"hostname": hostname})
        if result:
            return DockerContainerStats(**result)
        return None 
    
    def find_by_username_label(self, username: str) -> Optional[DockerContainerStats]:

       
        result = self.collection.find_one({"username": username})
        if result:
            return DockerContainerStats(**result)
        return None 
    
    def remove_by_id(self, service_id: str):
        self.collection.delete_one({"service_id": service_id})
    
    @property
    def all(self):
        return [DockerContainerStats(**r) for r in self.collection.find()]
    
    def delete(self, id: str):
        self.collection.delete_one({"id": id})
        
        
    def delete_all(self):
      
        self.collection.delete_many({})