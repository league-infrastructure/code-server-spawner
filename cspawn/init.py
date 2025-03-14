"""
Initialize the Application
"""

from .docker.csmanager import CodeServerManager
from .util.app_support import is_running_under_gunicorn

from typing import cast

from flask import Flask, g
from flask_bootstrap import Bootstrap5
from flask_dance.consumer.storage.sqla import SQLAlchemyStorage
from flask_dance.contrib.google import make_google_blueprint
from flask_font_awesome import FontAwesome
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import OperationalError, ProgrammingError
from werkzeug.middleware.proxy_fix import ProxyFix

from cspawn.__version__ import __version__ as version


from .util.app_support import (
    configure_app_dir,
    configure_config_tree,
    human_time_format,
    init_logger,
    setup_database,
    setup_sessions,
    setup_mongo,
)

default_context = {
    "version": version,
}

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


def init_app(config_dir=None, log_level=None, sqlfile=None, deployment=None) -> App:
    """Initialize Flask application"""

    from .models import db
    from .admin import admin_bp
    from .auth import auth_bp
    from .main import main_bp

    app = cast(App, Flask(__name__))

    # Register the filter with Flask or Jinja2
    app.jinja_env.filters["human_time"] = human_time_format

    config = configure_config_tree(config_dir, jtl_deployment=deployment)
    app.secret_key = config["SECRET_KEY"]
    app.app_config = config

    # Initialize logger
    init_logger(app, log_level=log_level)

    app_dir, db_dir = configure_app_dir(app)
    app.logger.debug(
        f"App dir: {app_dir} DB dir: {db_dir}. CONFIGS: {app.app_config['__CONFIG_PATH']}"
    )

    # Blueprints

    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_proto=1
    )  # So goggle oauth will use https behind proxy

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

    app.bootstrap = Bootstrap5(app)
    app.font_awesome = FontAwesome(app)

    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # Configure PostgreSQL database

    if sqlfile is not None:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlfile}"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = app.app_config["POSTGRES_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.db = db
    app.db.init_app(app)

    try:
        setup_database(app)
        setup_sessions(app)

        app.csm = CodeServerManager(app)

        app.migrate = Migrate(app, db)
    except (OperationalError, ProgrammingError) as e:
        app.logger.debug(f"Database error: {e}")
        app.logger.error("Error configuraing databse; No Database.")

        if is_running_under_gunicorn():
            app.logger.critical(
                "Fatal error: running under gunicorn without a database."
            )
            raise e

    setup_mongo(app)

    @app.teardown_appcontext
    def close_db(exception):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @login_manager.user_loader
    def load_user(user_id):
        from cspawn.models import User

        return User.query.get(user_id)

    return app
