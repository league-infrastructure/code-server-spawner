
import os
import sqlite3
from pathlib import Path

import sqlitedict
from flask import Flask, current_app, g

from jtlutil.flask.flaskapp import *


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.db_path)
        g.db.row_factory = sqlite3.Row  # Optional: Return rows as dictionaries
    return g.db


def initialize_database(path: Path):
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    #cursor.execute('''
    #    CREATE TABLE IF NOT EXISTS db_user ()
    #''')
    conn.commit()
    conn.close()
        
def init_app(file: str | Path = None):

    from jtlutil.flask.auth import auth_bp, load_user
    
    # Initialize Flask application
    app = Flask(__name__)

    app.register_blueprint(auth_bp)

    app.login_manager =  auth_bp.login_manager
    app.load_user = load_user

    #configure_config(app)
    configure_config_tree(app)

    # Initialize logger
    init_logger(app)
    
    app_dir, db_dir = configure_app_dir(app)
    
    setup_sqlite_sessions(app)
    
    # A key value store, built on top of sqlite
    kv_db_path = app_dir / "db" / "kv.db"
    app.kvstore = sqlitedict.SqliteDict(kv_db_path, tablename="kv", autocommit=True)
    
    # A regular sql database. For this database, we need to open and
    # close per request. 
    app.db_path =  app_dir / "db" / "app.db"
    initialize_database(app.db_path)
    

    return app

app = init_app()
from routes import *

if __name__ == "__main__":
    app.run(debug = True, host = "0.0.0.0")