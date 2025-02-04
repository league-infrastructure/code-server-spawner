import os
import sqlite3
from pathlib import Path


from flask import Flask, current_app, g
from jinja2 import Environment

from jtlutil.flask.flaskapp import *
from .db import UserAccounts
from .control import CodeServerManager


from flask_pymongo import PyMongo

CI_FILE = "container_info.json"



def human_time_format(seconds):

    try:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes, seconds = divmod(seconds, 60)
            return f"{int(minutes)}m {int(seconds)}s"
        else:
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{int(hours)}h {int(minutes)}m"
    except Exception as e:
        current_app.logger.error(f"Error in human_time_format: {e}")
        return seconds
    
    
def init_app(file: str | Path = None, log_level=None, config_dir=None) -> Flask:

    from jtlutil.flask.auth import auth_bp, load_user

    # Initialize Flask application
    app = Flask(__name__)


    @app.teardown_appcontext
    def close_db(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # Register the filter with Flask or Jinja2
    app.jinja_env.filters["human_time"] = human_time_format

    app.register_blueprint(auth_bp)

    app.login_manager = auth_bp.login_manager
    app.load_user = load_user
    app.login_manager.login_view = "login"

    # configure_config(app)
    
    config = configure_config_tree(config_dir)
        # Set the Flask secret key
    app.secret_key = config["SECRET_KEY"]
    app.app_config = config

    # Initialize logger
    init_logger(app, log_level=log_level)

    app_dir, db_dir = configure_app_dir(app)

    
    app.logger.info(f"App dir: {app_dir} DB dir: {db_dir}. CONFIGS: {app.app_config['__CONFIG_PATH']}")


    app.config["MONGO_URI"] = app.app_config["MONGO_URL"]
    app.config['CSM_MONGO_DB_NAME'] = 'code-spawner'
    app.mongodb = PyMongo(app)

    setup_sessions(app)

    app.csm = CodeServerManager(app)

    app.ua = UserAccounts(app)

    return app
