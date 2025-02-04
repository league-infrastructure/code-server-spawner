import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

import docker
from flask import current_app
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.collection import Collection
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)
from pymongo.database import Database as MongoDatabase

from cspawn.models.keyrate import KsSummary

retry_on_db_lock = retry(
    retry=retry_if_exception_type(sqlite3.OperationalError),
    wait=wait_exponential(multiplier=0.1, min=0.2, max=10),
    stop=stop_after_attempt(5),
    before_sleep=lambda retry_state: current_app.logger.warning(f"Retrying db operation: {retry_state.attempt_number}")
)



class UserAccount(BaseModel):
    username: str
    password: str
    createTime: datetime

   
class UserAccounts:
    def __init__(self, app):
        
        self.mongo_client = app.mongodb.cx
        
        # Shares a database wi the CSM
        self.db = self.mongo_client[app.config['CSM_MONGO_DB_NAME']]

        self.collection: Collection = self.db["up_users"] # "user/password users"
        self.collection.create_index("username", unique=True)

    def insert_user_account(self, username: str, password: str, create_time: datetime):
        user_account = {
            "username": username,
            "password": password,
            "createTime": create_time.isoformat()
        }
        self.collection.insert_one(user_account)

    def get_user_account(self, username: str) -> Optional[Dict]:
        return self.collection.find_one({"username": username})



class KeyrateDBHandler:
    def __init__(self, mongo_db: MongoDatabase):
        
        assert isinstance(mongo_db, MongoDatabase)
        
        self.db = mongo_db
        self.collection: Collection = self.db["keyrate"]
        self.collection.create_index("serviceID")
        self.collection.create_index("timestamp")

    def add_report(self, report: Dict):
        self.collection.insert_one(report)

    def summarize_latest(self, services: Optional[List[str]] = None) -> List[KsSummary]:
        pipeline = [
            {"$sort": {"timestamp": -1}},
            {"$group": {
                "_id": "$serviceID",
                "latestReport": {"$first": "$$ROOT"}
            }}
        ]
        if services:
            pipeline.insert(0, {"$match": {"serviceID": {"$in": services}}})

        results = self.collection.aggregate(pipeline)
        summaries = []
        now = datetime.now(timezone.utc)

        for result in results:
            report = result["latestReport"]
            timestamp = datetime.fromisoformat(report["timestamp"])
            heartbeat_ago = int((now - timestamp).total_seconds())
            summary = KsSummary(
                timestamp=report["timestamp"],
                containerName=report["containerName"],
                average30m=report["average30m"],
                heartbeatAgo=heartbeat_ago
            )
            summaries.append(summary)

        return summaries
    
    def delete_all(self):
        self.collection.delete_many({})
        
    def __len__(self):
        return self.collection.count_documents({})


