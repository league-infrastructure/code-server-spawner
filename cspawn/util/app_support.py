import logging
import os
from datetime import datetime
from collections import namedtuple

from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


from flask_session import Session
from flask_pymongo import PyMongo
from sqlalchemy.exc import ProgrammingError

from cspawn.util.config import get_config


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
    except Exception:
        return seconds


def is_running_under_gunicorn():
    """Return true if the app is running under Gunicorn,
    which implies it is in production"""

    return "gunicorn" in os.environ.get("SERVER_SOFTWARE", "") or "gunicorn" in os.environ.get("GUNICORN_CMD_ARGS", "")


def get_payload(request) -> dict:
    """Get the payload from the request, either from the form or json"""

    if request.content_type == "application/json":
        payload = request.get_json()
    else:
        payload = request.form.to_dict()

    # add date/time in iso format
    payload["_created"] = datetime.now().isoformat()

    return payload


def init_logger(app, log_level=None):
    """Initialize the logger for the app, either production or debug"""

    if log_level is not None:
        app.logger.setLevel(log_level)
        app.logger.debug("Logger initialized for debug")

    elif is_running_under_gunicorn():
        gunicorn_logger = logging.getLogger("gunicorn.error")
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)
        app.logger.debug("Logger initialized for gunicorn")

    else:
        # logging.basicConfig(level=logging.INFO)
        app.logger.setLevel(logging.INFO)
        app.logger.debug("Logger initialized for flask")


def configure_config_tree(config_dir: str | Path, deploy: str) -> Dict[str, Any]:
    # Determine if we're running in production or development

    config = get_config(config_dir, deploy=deploy)

    # Resolve the path to the secrets file
    if "SECRETS_FILE_NAME" in config:
        config["SECRETS_FILE"] = (Path(config["__CONFIG_PATH"]).parent / config["SECRETS_FILE_NAME"]).resolve()

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = config.get("OAUTHLIB_INSECURE_TRANSPORT", "")

    return config


def configure_app_dir(app):
    # Configure the appdir

    app.app_config.app_dir = app_dir = Path(app.app_config.APP_DIR)

    if not app_dir.exists():
        app_dir.mkdir(parents=True)

    app.app_config.data_dir = Path(app.app_config.DATA_DIR)

    app.app_config.db_dir = db_dir = app.app_config.data_dir / "db"

    if not db_dir.exists():
        db_dir.mkdir(parents=True)

    return app_dir, db_dir


def setup_sessions(app, devel=False, session_expire_time=60 * 60 * 24 * 1):
    """
    Sets up SQLite-backed sessions for a Flask app.

    Args:
        app (Flask): The Flask app instance.
        devel (bool): Flag to indicate whether the app is in development mode.
        session_expire_time (int): Session expiration time in seconds (default is 1 day).
    """
    # Setup sessions
    app.config["SESSION_TYPE"] = "sqlalchemy"
    app.config["SESSION_SQLALCHEMY"] = app.db

    # Set session expiration time
    app.config["PERMANENT_SESSION_LIFETIME"] = session_expire_time
    app.config["SESSION_CLEANUP_N_REQUESTS"] = 100
    app.config["SESSION_SERIALIZATION_FORMAT"] = "json"

    # Adjust cookie security based on the environment
    if devel:
        # Development settings
        app.config["SESSION_COOKIE_SECURE"] = False  # Allow cookies over HTTP
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Prevent cross-site issues
    else:
        # Production settings
        app.config["SESSION_COOKIE_SECURE"] = True  # Require HTTPS for cookies
        app.config["SESSION_COOKIE_SAMESITE"] = "None"  # Allow cross-site cookies if needed

    Session(app)  # Initialize the session


def setup_database(app):
    from cspawn.models import User

    with app.app_context():

        
        try:
            app.root_user = User.create_root_user(app)
        except ProgrammingError as e:
            app.logger.error(f"Error creating root user")
            


def setup_mongo(app):
    # Configure MongoDB
    app.config["MONGO_URI"] = app.app_config["MONGO_URI"]
    # app.config["MONGO_DBNAME"] = "codeserv"

    mongo = PyMongo(app)

    db_name = "codeserv"
    if db_name not in mongo.cx.list_database_names():
        app.logger.info(f"Creating database '{db_name}'")

    collections = ["telem"]

    for collection in collections:
        if collection not in mongo.cx[db_name].list_collection_names():
            app.logger.info(f"Creating collection '{collection}' in database '{db_name}'")
            mongo.cx[db_name].create_collection(collection)

    # Create a named tuple to hold MongoDB references
    MongoReferences = namedtuple("MongoReferences", ["client", "codeserv"] + collections)

    # Assign the MongoDB client and database references to the app
    app.mongo = MongoReferences(
        client=mongo.cx,
        codeserv=mongo.cx[db_name],
        **{collection: mongo.cx[db_name][collection] for collection in collections},
    )


def insert_query_arg(url, key, value):
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    query_params[key] = value
    new_query_string = urlencode(query_params, doseq=True)
    return urlunparse(parsed_url._replace(query=new_query_string))


def role_from_email(config, email):
    """Determine the role of the user based on the email address.

    The config has these variables:

    ADMIN_EMAILS='["eric.busboom@jointheleague.org", "admin@jointheleague.org", "it@jointheleague.org"]'
    INSTRUCTOR_EMAIL_REXEX='^[^@]+@jointheleague\.org$'
    STUDENT_EMAIL_REGEX='^[^@]+@students\.jointheleague\.org$'

    """
    import json
    import re

    if not email:
        return "public"

    if email in json.loads(config["ADMIN_EMAILS"]):
        return "admin"
    elif re.match(config["INSTRUCTOR_EMAIL_REXEX"], email):
        return "instructor"
    elif re.match(config["STUDENT_EMAIL_REGEX"], email):
        return "student"
    else:
        return "public"


def set_role_from_email(app, user):
    """Set the role of the user based on the email address."""

    config = app.app_config

    role = role_from_email(config, user.email)

    if role == "admin":
        user.is_admin = True
        user.is_instructor = True
        user.is_student = True
    elif role == "instructor":
        user.is_instructor = True
    elif role == "student":
        user.is_student = True
    else:
        pass
