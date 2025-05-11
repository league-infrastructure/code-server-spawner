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
    average1m: float
    sysMemory: int | None
    processMemory: int
    reportingRate: int
    fileStats: Dict[str, FileStat]
    completions: List[int|str]
    image: str
    repo: str
    syllabus: str
    class_id: int
    username: str
