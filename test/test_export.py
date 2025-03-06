import logging
import unittest
import warnings
from pathlib import Path

import pytest
from faker import Faker
import json


from cspawn.main.models import *
from cspawn.util.test_fixture import *


class TestExport(CSUnitTest):

    def setUp(self):

        super().setUp()
        print("\n" + ("#" * 80))

    def test_export_basic(self):

        d = json.loads((self.data_dir / "export.json").read_text())

        self.drop_db()

        with self.app.app_context():
            import_dict(d)

            users = User.query.all()
            for user in users:
                print(user)


if __name__ == "__main__":
    unittest.main()
