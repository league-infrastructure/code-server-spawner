import logging
import unittest
import warnings
from pathlib import Path

import pytest
import random
from faker import Faker

from cspawn.cli.util import create_demo_users, create_demo_images, make_data
from cspawn.docker.models import CodeHost, HostImage
from cspawn.init import db
from cspawn.main.models import User
from cspawn.util.apptypes import App


class CSUnitTest(unittest.TestCase):

    def setUp(self):

        import cspawn
        from cspawn.init import init_app

        self.this_dir = Path(__file__).parent
        self.config_dir = Path(cspawn.__file__).parent.parent

        self.dev_root = self.this_dir.parent

        self.data_dir = self.dev_root / "data"

        warnings.filterwarnings("ignore")

        self.app = init_app(
            config_dir=self.config_dir,
            log_level=logging.ERROR,
            sqlfile=self.this_dir / "test.db",
        )

        self.fake = Faker()

    def create_demo_users(self):

        with self.app.app_context():
            db.create_all()
            create_demo_users(self.app)

    def create_demo_images(self):

        with self.app.app_context():
            db.create_all()
            create_demo_images(self.app)


@pytest.fixture
def app():

    import cspawn
    from cspawn.init import init_app

    this_dir = Path(__file__).parent
    config_dir = Path(cspawn.__file__).parent.parent

    app = init_app(config_dir=config_dir, sqlfile=this_dir / "test.db")

    return app


@pytest.fixture
def fake():
    return Faker()


def make_fake_user(fake: Faker) -> User:

    is_admin = random.random() < 0.02
    is_instructor = not is_admin and random.random() < 0.05
    is_student = not is_admin and not is_instructor

    return User(
        user_id=fake.uuid4(),
        username=fake.user_name(),
        email=fake.email(),
        password=fake.password(),
        is_active=True,
        is_admin=is_admin,
        is_instructor=is_instructor,
        is_student=is_student,
        display_name=fake.name(),
    )
