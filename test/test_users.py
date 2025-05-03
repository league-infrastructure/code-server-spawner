import logging
import unittest
import warnings
from pathlib import Path

import pytest
from faker import Faker
import json

from cspawn.models import ClassProto
from cspawn.init import db
from cspawn.models import *
from cspawn.models import User

from cspawn.models import CodeHost
from cspawn.util.test_fixture import *

from cspawn.cli.util import logger as cli_logger

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.INFO)
cli_logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", module="passlib.handlers.bcrypt")


class TestHosts(CSUnitTest):
    def setUp(self):
        super().setUp()
        print("\n" + ("#" * 80))

    def test_secret_key(self):
        """
        Test to ensure the secret key is set correctly in the app configuration.

        Args:
            app: Flask app instance.
        """
        for e in self.app.app_config["__CONFIG_PATH"]:
            logger.info("    config dir: %s", str(e))

        assert self.app.app_config["SECRET_KEY"] == "gQU97yUgJ6rq@4!p7-ni"

    def test_user_db(self):
        """
        Test to ensure user database operations work correctly.

        Args:
            app: Flask app instance.
        """

        app = self.app

        with app.app_context():
            db.create_all()

            # Delete all users with email addresses in the 'example.com' domain
            users_to_delete = User.query.filter(User.email.like("%@example.com")).all()
            for user in users_to_delete:
                db.session.delete(user)
            db.session.commit()

            bob = User(user_id=self.fake.uuid4(), username="bob", email="bob@example.com", password="password")
            larry = User(user_id=self.fake.uuid4(), username="larry", email="larry@example.com", password="password")
            sally = User(user_id=self.fake.uuid4(), username="sally", email="sally@example.com", password="password")

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

    def test_create_demo_users(self):
        self.create_demo_users()
        self.create_demo_images()

    def test_delete_all_users(self):
        """
        Test to delete all users from the database.

        Args:
            app: Flask app instance.
        """
        app = self.app

        with app.app_context():
            db.create_all()

            # Delete all users
            users_to_delete = User.query.all()
            for user in users_to_delete:
                db.session.delete(user)


if __name__ == "__main__":
    pytest.main(["-k", "test_user_db"])
