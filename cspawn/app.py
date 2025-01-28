import os
import sqlite3
from pathlib import Path

import sqlitedict
from flask import Flask, current_app, g
from jinja2 import Environment

from jtlutil.flask.flaskapp import *
from .db import create_keystroke_tables


CI_FILE = "container_info.json"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.db_path)
        g.db.row_factory = sqlite3.Row  # Optional: Return rows as dictionaries
    return g.db


def initialize_database(path: Path):

    conn = sqlite3.connect(path)

    create_keystroke_tables(conn)

    conn.commit()
    conn.close()


def init_app(file: str | Path = None):

    from jtlutil.flask.auth import auth_bp, load_user

    # Initialize Flask application
    app = Flask(__name__)

    app.register_blueprint(auth_bp)

    app.login_manager = auth_bp.login_manager
    app.load_user = load_user
    app.login_manager.login_view = "login"

    # configure_config(app)
    configure_config_tree(app)

    # Initialize logger
    init_logger(app)

    app_dir, db_dir = configure_app_dir(app)

    app.logger.info(f"App dir: {app_dir}")
    app.logger.info(f"DB dir: {db_dir}")

    setup_sqlite_sessions(app)

    # A key value store, built on top of sqlite
    kv_db_path = db_dir / "kv.db"
    app.kvstore = sqlitedict.SqliteDict(kv_db_path, tablename="kv", autocommit=True)

    # A regular sql database. For this database, we need to open and
    # close per request.
    app.db_path = db_dir / "app.db"

    initialize_database(app.db_path)

    app.user_db_path = db_dir / "users.db"

    return app


app = init_app()


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


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


# Register the filter with Flask or Jinja2
app.jinja_env.filters["human_time"] = human_time_format

from .routes.main import *
from .routes.cron import *
from .routes.auth import *

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
