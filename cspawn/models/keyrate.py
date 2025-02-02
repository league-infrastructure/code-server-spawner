from pydantic import BaseModel
from typing import Optional


class FileStat(BaseModel):
    keystrokes: int
    lastModified: str


class KeystrokeReport(BaseModel):
    timestamp: str
    containerID: str
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
    seconds_since_report: int


    model_config = {"from_attributes": True}




