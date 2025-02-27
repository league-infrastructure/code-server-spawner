"""
Initialize the Application
"""

from flask import Flask, g
from flask_bootstrap import Bootstrap5
from flask_dance.contrib.google import make_google_blueprint
from flask_font_awesome import FontAwesome
from flask_login import LoginManager
from flask_pymongo import PyMongo

from cspawn.__version__ import __version__ as version

from .admin import admin_bp
from .auth import auth_bp
from .hosts import hosts_bp
from .hosts.control import CodeServerManager
from .main import main_bp
from .main.models import db
from .util import (configure_app_dir, configure_config_tree, human_time_format,
                   init_logger, setup_sessions)

default_context = {
    "version": version,
}

GOOGLE_LOGIN_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def init_app(config_dir=None, log_level=None, sqlfile=None) -> Flask:
    # Initialize Flask application
    app = Flask(__name__)

    @app.teardown_appcontext
    def close_db(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # Register the filter with Flask or Jinja2
    app.jinja_env.filters["human_time"] = human_time_format

    config = configure_config_tree(config_dir)
    app.secret_key = config["SECRET_KEY"]
    app.app_config = config

    # Initialize logger
    init_logger(app, log_level=log_level)

    app_dir, db_dir = configure_app_dir(app)

    app.logger.info(f"App dir: {app_dir} DB dir: {db_dir}. CONFIGS: {app.app_config['__CONFIG_PATH']}")

    app.config["MONGO_URI"] = app.app_config["MONGO_URL"]
    app.config["CSM_MONGO_DB_NAME"] = "code-spawner"
    app.mongodb = PyMongo(app)

    # Configure PostgreSQL database
    if sqlfile is not None:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlfile}"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = app.app_config["POSTGRES_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.db = db
    app.db.init_app(app)

    setup_sessions(app)

    app.csm = CodeServerManager(app)

    # Configure Google OAuth

    from flask_dance.consumer.storage.sqla import SQLAlchemyStorage

    google_bp = make_google_blueprint(
        scope=GOOGLE_LOGIN_SCOPES,
        reprompt_select_account=True,
        client_id=app.app_config["GOOGLE_CLIENT_ID"],
        client_secret=app.app_config["GOOGLE_CLIENT_SECRET"],
        # storage=SQLAlchemyStorage(OAuth, db.session, user=current_user),
        redirect_to="auth.google_login",
    )
    app.register_blueprint(google_bp, url_prefix="/oauth/")

    app.register_blueprint(main_bp, url_prefix="/")
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(hosts_bp, url_prefix="/host")

    app.bootstrap = Bootstrap5(app)
    app.font_awesome = FontAwesome(app)

    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id):
        from cspawn.main.models import User

        return User.query.get(user_id)

    return app
