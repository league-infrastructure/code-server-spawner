from pathlib import Path

import pytest

from .fixtures import *


def test_auth_basic(app):
    print(app.app_config["SECRET_KEY"])
