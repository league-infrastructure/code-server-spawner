"""
Initialize the Application
"""

import logging
import signal
import sys
import uuid
from typing import cast

from flask import Flask, current_app, g, request, session
from flask_bootstrap import Bootstrap5
from flask_dance.contrib.google import make_google_blueprint
from flask_font_awesome import FontAwesome
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import OperationalError, ProgrammingError
from werkzeug.middleware.proxy_fix import ProxyFix

from cspawn.__version__ import __version__ as version
from cspawn.cs_docker.csmanager import CodeServerManager
from cspawn.util.app_support import (configure_app_dir, configure_config_tree,
                                     human_time_format, init_logger,
                                     is_running_under_gunicorn, setup_database,
                                     setup_sessions)

logging.getLogger("flask_dance.consumer.oauth2").setLevel(logging.DEBUG)

default_context = {"version": version}

GOOGLE_LOGIN_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class App(Flask):
    app_config: dict
    db: SQLAlchemy
    csm: CodeServerManager
    bootstrap: Bootstrap5
    font_awesome: FontAwesome


def cast_app(app: Flask) -> App:
    return cast(App, app)


def resolve_deployment(deployment: str) -> str:
    import os

    if deployment is not None:
        return deployment

    if jtl_deploy := os.getenv("JTL_DEPLOYMENT"):
        return jtl_deploy

    if is_running_under_gunicorn():
        return "prod"

    return "devel"

def ensure_session():


    if (request.endpoint and 'static' in request.endpoint) or \
       "cron" in request.path or "telem" in request.path:
        return

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        current_app.logger.info(f"New session created with ID: {session['session_id']} for {request.path}")
    else:
        pass

   



def init_app(config_dir=None, deployment=None, log_level=None) -> App:
    """Initialize Flask application"""

    from .admin import admin_bp
    from .auth import auth_bp
    from .main import main_bp
    from .models import db

    app = cast(App, Flask(__name__))

    deployment = resolve_deployment(deployment)

    # Register the filter with Flask or Jinja2
    app.jinja_env.filters["human_time"] = human_time_format

    config = configure_config_tree(config_dir, deploy=deployment)
    app.secret_key = config["SECRET_KEY"]
    app.app_config = config

    # Initialize logger
    init_logger(app, log_level=log_level)

    app_dir, db_dir = configure_app_dir(app)
    app.logger.debug(f"App dir: {app_dir} DB dir: {db_dir}. CONFIGS: {app.app_config['__CONFIG_PATH']}")

    # Blueprints

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)  # So goggle oauth will use https behind proxy

    google_bp = make_google_blueprint(
        scope=GOOGLE_LOGIN_SCOPES,
        reprompt_select_account=True,
        client_id=app.app_config["GOOGLE_CLIENT_ID"],
        client_secret=app.app_config["GOOGLE_CLIENT_SECRET"],
        #storage=SQLAlchemyStorage(OAuth, db.session, user=current_user),
        redirect_to="auth.google_login",
    )
    app.register_blueprint(google_bp, url_prefix="/oauth/")

    app.register_blueprint(main_bp, url_prefix="/")
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")

    app.bootstrap = Bootstrap5(app)
    app.font_awesome = FontAwesome(app)

    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # Configure PostgreSQL database

    app.config["SQLALCHEMY_DATABASE_URI"] = app.app_config["DATABASE_URI"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.db = db
    app.db.init_app(app)
    app.migrate = Migrate(app, db)

    try:
        setup_database(app)
        setup_sessions(app)

        app.csm = CodeServerManager(app)

    except (OperationalError, ProgrammingError) as e:
        app.logger.error(f"Database error: {e}")
        app.logger.error("Error configuraing databse; No Database.")

        if is_running_under_gunicorn():
            app.logger.critical("Fatal error: running under gunicorn without a database.")
            raise e
        raise e

    #setup_mongo(app)

    @app.before_request
    def before_request():
        ensure_session()

    # app.load_user(current_app)




    @app.teardown_appcontext
    def close_db(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @login_manager.user_loader
    def load_user(user_id):
        from cspawn.models import User

        return User.query.get(user_id)

    def handle_shutdown(*args):
        print("Shutting down gracefully...")
        db.session.remove()
        db.engine.dispose()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)

    return app
