import logging
import unittest
import warnings
from pathlib import Path

import pytest
from faker import Faker

from cspawn.cli.util import create_demo_users, make_data
from cspawn.docker.models import CodeHost, HostImage
from cspawn.init import db
from cspawn.main.models import User


class CSUnitTest(unittest.TestCase):

    def setUp(self):

        import cspawn
        from cspawn.init import init_app

        this_dir = Path(__file__).parent
        config_dir = Path(cspawn.__file__).parent.parent

        warnings.filterwarnings("ignore")
        self.app = init_app(
            config_dir=config_dir, log_level=logging.ERROR, sqlfile=this_dir / "test.db"
        )

        self.fake = Faker()


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
