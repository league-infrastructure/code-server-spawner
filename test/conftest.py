import pytest
from cspawn.init import init_app
from cspawn.models import db as _db


@pytest.fixture(scope="session")
def app():
    app = init_app(deployment="devel", log_level="DEBUG")
    app.config["TESTING"] = True
    # app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        yield app


@pytest.fixture(scope="session")
def temp_db(app):
    orig_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    _db.app = app
    with app.app_context():
        _db.create_all()
        yield _db
        _db.drop_all()
        app.config["SQLALCHEMY_DATABASE_URI"] = orig_uri


@pytest.fixture(scope="session")
def db(app):
    with app.app_context():
        yield _db
