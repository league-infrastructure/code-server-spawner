from pydantic import BaseModel
from typing import Optional
from pymongo import MongoClient
from pymongo.collection import Collection
from datetime import datetime, timezone
from typing import List, Dict


class FileStat(BaseModel):
    keystrokes: int
    lastModified: str


class KeystrokeReport(BaseModel):
    timestamp: str
    containerID: str
    serviceID: str
    serviceName: str
    instanceId: str
    keystrokes: int
    average30m: float
    reportingRate: int
    fileStats: dict[str, FileStat]
    containerName: str

    model_config = {"from_attributes": True}


class KsSummary(BaseModel):
    timestamp: str
    containerName: str  # Primary key
    average30m: float
    heartbeatAgo: int

    model_config = {"from_attributes": True}
