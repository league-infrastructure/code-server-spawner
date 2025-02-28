import logging
import unittest
import warnings
from pathlib import Path

import pytest
from faker import Faker
import json

from cspawn.docker.models import CodeHost, HostImage
from cspawn.init import db
from cspawn.main.models import *
from cspawn.main.models import User

from cspawn.test_fixture import *


class TestHosts(CSUnitTest):

    def setUp(self):

        super().setUp()
        print("\n" + ("#" * 80))

        self.images = json.loads((self.data_dir / "images.json").read_text())

    def test_hosts_basic(self):

        self.create_demo_users()
        self.create_demo_images()

        with self.app.app_context():

            user: User = User.query.first()
            image: HostImage = HostImage.query.first()

        csm = self.app.csm

        r = csm.new_cs(user, image.image_uri, image.repo_uri)

        print(r)


if __name__ == "__main__":
    unittest.main()
