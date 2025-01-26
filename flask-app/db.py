import sqlite3
import json


def create_keystroke_tables(path):
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS keystroke_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            containerId TEXT NOT NULL,
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
            containerId TEXT NOT NULL,
            instanceId TEXT NOT NULL,
            filename TEXT NOT NULL,
            keystrokes INTEGER NOT NULL,
            lastModified TEXT NOT NULL,
            FOREIGN KEY (keystroke_data_id) REFERENCES keystroke_data(id)
        )
    ''')
    
    conn.commit()

def insert_keystroke_data(conn, data):
    cursor = conn.cursor()
    
    if data['keystrokes'] == 0:
        return
    
    cursor.execute('''
        INSERT INTO keystroke_data (timestamp, containerID, instanceId, keystrokes, average30m, reportingRate, fileStats)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['timestamp'],
        data['containerID'],
        data['instanceId'],
        data['keystrokes'],
        data['average30m'],
        data['reportingRate'],
        json.dumps(data['fileStats'])
    ))
    
    keystroke_data_id = cursor.lastrowid
    
    for filename, stats in data['fileStats'].items():
        cursor.execute('''
            INSERT INTO files (keystroke_data_id, containerId, instanceId, filename, keystrokes, lastModified)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            keystroke_data_id,
            data['containerID'],
            data['instanceId'],
            filename,
            stats['keystrokes'],
            stats['lastModified']
        ))
    
    
    conn.commit()
