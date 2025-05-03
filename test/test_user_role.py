import logging
import unittest
import warnings
from pathlib import Path

from faker import Faker

from cspawn.models import ClassProto
from cspawn.models import CodeHost, ClassProto, User

from cspawn.util.test_fixture import *


class TestUserRole(unittest.TestCase):
    def setUp(self):
        import cspawn
        from cspawn.init import init_app

        this_dir = Path(__file__).parent
        config_dir = Path(cspawn.__file__).parent.parent

        warnings.filterwarnings("ignore")
        self.app = init_app(config_dir=config_dir, log_level=logging.ERROR, sqlfile=this_dir / "test.db")

        self.fake = Faker()

    def test_user_basic(self):
        print(self.app.app_config["SECRET_KEY"])

    def test_user_role_basic(self):
        make_data(self.app)

        with self.app.app_context():
            code_host = CodeHost.query.first()
            self.assertEqual(code_host.user.username, User.query.first().username)
            self.assertEqual(code_host.host_image.name, ClassProto.query.first().name)


if __name__ == "__main__":
    unittest.main()
