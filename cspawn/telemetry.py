# Like models,  but for MongoDB objects.
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from bson import ObjectId
from datetime import datetime


class FileStat(BaseModel):
    keystrokes: int
    lastModified: datetime


class TelemetryReport(BaseModel):
    _id: ObjectId
    timestamp: datetime
    instanceId: str
    keystrokes: int
    average30m: float
    average5m: float
    sysMemory: int
    processMemory: int
    reportingRate: int
    fileStats: Dict[str, FileStat]
    completions: List[int]
    image: str
    repo: str
    syllabus: str
    class_id: str
    username: str
