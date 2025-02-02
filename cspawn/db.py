import sqlite3
import json
import docker
from datetime import datetime 

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from flask import current_app   

retry_on_db_lock = retry(
    retry=retry_if_exception_type(sqlite3.OperationalError),
    wait=wait_exponential(multiplier=0.1, min=0.2, max=10),
    stop=stop_after_attempt(5),
    before_sleep=lambda retry_state: current_app.logger.warning(f"Retrying db operation: {retry_state.attempt_number}")
)

def create_keystroke_tables(conn):
    
    
    conn.execute("""
       CREATE TABLE IF NOT EXISTS user_accounts (
           username TEXT PRIMARY KEY NOT NULL,
           password TEXT NOT NULL,
           createTime TEXT NOT NULL    
       )
    """)
    conn.commit()


   


#@retry_on_db_lock
def insert_user_account(conn, username: str, password: str, create_time: datetime):
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_accounts (username, password, createTime)
        VALUES (?, ?, ?)
    ''', (username, password, create_time.isoformat()))
    conn.commit()

def get_user_account(conn, username):
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM user_accounts WHERE username = ?
    ''', (username,))
    return cursor.fetchone()

