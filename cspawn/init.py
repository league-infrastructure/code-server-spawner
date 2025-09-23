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

    config = configure_config_tree(config_dir, deploy=deployment)
    app.secret_key = config["SECRET_KEY"]
    app.app_config = config

    # Initialize logger
   
    init_logger(app, log_level=log_level)

    try:
        app_dir, db_dir = configure_app_dir(app)
    except AttributeError as e:
        app.logger.error(f"Error configuring app directory deployment {deployment}\nconfig_path: {config['__CONFIG_PATH']}\n {e}")
        raise AttributeError(f"Error configuring app directory for deployment = {deployment}\nconfig_path: {config['__CONFIG_PATH']}\n {e}")

    app.logger.info(f"Starting app in {deployment} mode")
    app.logger.info('Logging initialized. level={log_level}')
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
    
    # Configure connection pooling for multi-worker environment
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_size": 5,  # Number of connections to maintain in pool
        "pool_timeout": 20,  # Timeout for getting connection from pool
        "pool_recycle": 3600,  # Recycle connections after 1 hour
        "max_overflow": 10,  # Additional connections beyond pool_size
        "pool_pre_ping": True,  # Validate connections before use
    }

    app.db = db
    app.db.init_app(app)
    app.migrate = Migrate(app, db)

    try:
        setup_database(app)
        # Use dev-friendly session cookies when running in development to avoid CSRF mismatches

        setup_sessions(app, devel=(deployment == "devel"))

        app.csm = CodeServerManager(app)
        
        app.logger.info(f"Application initialized successfully in {deployment} mode")

    except (OperationalError, ProgrammingError) as e:
        app.logger.error(f"Database error during initialization: {e}")
        app.logger.error("Error configuring database; No Database available.")

        if is_running_under_gunicorn():
            app.logger.critical("Fatal error: running under gunicorn without a database.")
            raise e
        raise e
    except Exception as e:
        app.logger.error(f"Unexpected error during app initialization: {e}")
        raise e

    #setup_mongo(app)
    
    @app.before_request
    def before_request():
        ensure_session()

    # app.load_user(current_app)


    @app.teardown_appcontext
    def close_db(exception):
        # Properly clean up database connections after each request
        try:
            app.db.session.remove()
        except Exception as e:
            app.logger.warning(f"Error during database cleanup: {e}")
        
        db_ref = g.pop("db", None)
        if db_ref is not None:
            try:
                db_ref.close()
            except Exception as e:
                app.logger.warning(f"Error closing database reference: {e}")

    @login_manager.user_loader
    def load_user(user_id):
        from cspawn.models import User

        return User.query.get(user_id)

    # Only set up signal handlers when not running under Gunicorn
    # Gunicorn handles worker lifecycle management itself
    if not is_running_under_gunicorn():
        def handle_shutdown(*args):
            print("Shutting down gracefully...")
            db.session.remove()
            db.engine.dispose()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_shutdown)

    # Register the filter with Flask or Jinja2
    app.jinja_env.filters["human_time"] = human_time_format

    return app
