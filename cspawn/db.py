import sqlite3
import json
import docker
from datetime import datetime 

from jtlutil.docker.dctl import container_state
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from flask import current_app   

retry_on_db_lock = retry(
    retry=retry_if_exception_type(sqlite3.OperationalError),
    wait=wait_exponential(multiplier=0.1, min=0.2, max=10),
    stop=stop_after_attempt(5),
    before_sleep=lambda retry_state: current_app.logger.warning(f"Retrying db operation: {retry_state.attempt_number}")
)

def create_keystroke_tables(conn):

    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS keystroke_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            containerName TEXT NOT NULL,
            instanceId TEXT NOT NULL,
            keystrokes INTEGER NOT NULL,
            average30m REAL NOT NULL,
            reportingRate INTEGER NOT NULL,
            fileStats TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keystroke_data_id INTEGER NOT NULL,
            containerName TEXT NOT NULL,
            instanceId TEXT NOT NULL,
            filename TEXT NOT NULL,
            keystrokes INTEGER NOT NULL,
            lastModified TEXT NOT NULL,
            FOREIGN KEY (keystroke_data_id) REFERENCES keystroke_data(id)
        )
    ''')
    
    conn.execute("""
       CREATE TABLE IF NOT EXISTS ks_summary (
           timestamp TEXT NOT NULL,
           containerName TEXT PRIMARY KEY,
           average30m REAL NOT NULL,
           seconds_since_report INTEGER NOT NULL
       )
   """)
    
    
    conn.execute("""
       CREATE TABLE IF NOT EXISTS heartbeat (
           containerName TEXT PRIMARY KEY,
           instanceId TEXT NOT NULL,
           lastHeartbeat TEXT NOT NULL
       )
   """)
    

    conn.execute("""
       CREATE TABLE  IF NOT EXISTS container_state (
           containerId TEXT PRIMARY KEY,
           state TEXT NOT NULL,
           containerName TEXT NOT NULL,
           memory_usage INTEGER NOT NULL,
           hostname TEXT NOT NULL,
           port INTEGER
       )
    """)
    conn.commit()
    
    
    conn.execute("""
       CREATE TABLE IF NOT EXISTS user_accounts (
           username TEXT PRIMARY KEY NOT NULL,
           password TEXT NOT NULL,
           createTime TEXT NOT NULL    
       )
    """)
    conn.commit()


   
   
def insert_keystroke_data(conn, data):
    cursor = conn.cursor()
    
    if data['keystrokes'] == 0:
        return
    
    cursor.execute('''
        INSERT INTO keystroke_data (timestamp, containerName, instanceId, keystrokes, average30m, reportingRate, fileStats)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['timestamp'],
        data['containerName'],
        data['instanceId'],
        data['keystrokes'],
        data['average30m'],
        data['reportingRate'],
        json.dumps(data['fileStats'])
    ))
    
    keystroke_data_id = cursor.lastrowid
    
    for filename, stats in data['fileStats'].items():
        cursor.execute('''
            INSERT INTO files (keystroke_data_id, containerName, instanceId, filename, keystrokes, lastModified)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            keystroke_data_id,
            data['containerID'], # the extension calls this containerID, but its actually the name
            data['instanceId'],
            filename,
            stats['keystrokes'],
            stats['lastModified']
        ))
    
    
    conn.commit()

def update_container_state(conn, d):
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM container_state")
    
    for container in d:
        cursor.execute('''
            INSERT INTO container_state (containerId, state, containerName, memory_usage, hostname, port)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            container['id'],
            container['state'],
            container['name'],
            container.get('memory_usage'),
            container['hostname'],
            container['port']
        ))
    
    conn.commit()


def update_container_status(conn, container_name, instance_id, heartbeat):
   conn.execute("""
       INSERT INTO heartbeat (containerName, instanceId, lastHeartbeat)
       VALUES (?, ?, ?)
       ON CONFLICT(containerName) 
       DO UPDATE SET
           instanceId = excluded.instanceId,
           lastHeartbeat = excluded.lastHeartbeat
   """, (container_name, instance_id, heartbeat))
   conn.commit()

def update_container_metrics(conn):
   conn.execute("DELETE FROM ks_summary")
   conn.execute("""
       INSERT INTO ks_summary
       SELECT 
           timestamp,
           containerName,
           average30m,
           ROUND((strftime('%s','now') - strftime('%s',timestamp)))
       FROM keystroke_data 
       WHERE (containerName, timestamp) IN (
           SELECT containerName, MAX(timestamp)
           FROM keystroke_data
           GROUP BY containerName
       )
   """)
   conn.commit()


def join_container_info(conn):
   rows = conn.execute("""
       SELECT 
           cs.containerName,
           cs.containerId,
           cs.state,
           cs.memory_usage,
           cs.hostname,
           cs.port, 
           h.instanceId,
           h.lastHeartbeat,
           ks.average30m,
           ks.seconds_since_report
       FROM container_state cs
       LEFT JOIN heartbeat h ON cs.containerName = h.containerName
       LEFT JOIN ks_summary ks ON cs.containerName = ks.containerName
   """).fetchall()
   
   return [dict(row) for row in rows]

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

def update_container_info(app, db):
    from pathlib import Path
    from cspawn.app import   CI_FILE
    
    update_container_metrics(db)
    
    client = docker.DockerClient(base_url=app.app_config.SSH_URI )
    update_container_state(db,container_state(client))
    
    d = join_container_info(db)
    
    (Path(app.app_config.DATA_DIR) / CI_FILE).write_text(json.dumps(d))


