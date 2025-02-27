import logging
import warnings
from pathlib import Path

import pytest

from cspawn.main.models import User

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")

warnings.filterwarnings("ignore", module="passlib.handlers.bcrypt")


@pytest.fixture
def app():
    """
    Fixture to initialize the Flask application for testing.

    Returns:
        Flask app instance.
    """
    from cspawn.init import init_app

    # Set the environment variable for the config directory
    config_dir = Path(__file__).parent.parent

    logger.debug("XXX config_dir: %s", config_dir)

    return init_app(config_dir=config_dir)


def test_secret_key(app):
    """
    Test to ensure the secret key is set correctly in the app configuration.

    Args:
        app: Flask app instance.
    """
    for e in app.app_config["__CONFIG_PATH"]:
        logger.info("    config dir: %s", str(e))

    assert app.app_config["SECRET_KEY"] == "gQU97yUgJ6rq@4!p7-ni"


def test_user_db(app):
    """
    Test to ensure user database operations work correctly.

    Args:
        app: Flask app instance.
    """
    from cspawn.main.models import db

    with app.app_context():
        db.create_all()

        # Delete all users with email addresses in the 'example.com' domain
        users_to_delete = User.query.filter(User.email.like("%@example.com")).all()
        for user in users_to_delete:
            db.session.delete(user)
        db.session.commit()

        bob = User(username="bob", email="bob@example.com", password="password")
        larry = User(username="larry", email="larry@example.com", password="password")
        sally = User(username="sally", email="sally@example.com", password="password")

        db.session.add(bob)
        db.session.add(larry)
        db.session.add(sally)
        db.session.commit()

        # Ensure there are three users with example.com emails
        example_users = User.query.filter(User.email.like("%@example.com")).all()
        assert len(example_users) == 3

        user = User.query.filter_by(username="admin").first()
        logger.debug("XXX user: %s", user)
        # assert user is not None


def test_delete_all_users(app):
    """
    Test to delete all users from the database.

    Args:
        app: Flask app instance.
    """
    from cspawn.main.models import db

    with app.app_context():
        db.create_all()

        # Delete all users
        users_to_delete = User.query.all()
        for user in users_to_delete:
            db.session.delete(user)


if __name__ == "__main__":
    pytest.main(["-k", "test_user_db"])
