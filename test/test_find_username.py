"""Tests for find_username — particularly numeric (student-ID) emails.

League student Google accounts use numeric student-ID email addresses such as
``52@students.jointheleague.org``. Deriving the username from the email
local-part alone produced bare numbers (``52``) and forks named
``Python-Apprentice-52``. find_username should prefer the display name in that
case while keeping email-derived usernames for descriptive addresses.

This module builds its own lightweight in-memory SQLite app so it does not
depend on the full ``init_app`` fixture (which needs live infrastructure).
"""

import uuid

import pytest
from flask import Flask

from cspawn.models import db, User
from cspawn.util.auth import find_username


@pytest.fixture()
def app_db():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield db
        db.session.remove()
        db.drop_all()


def _make_user(email, display_name=None):
    return User(
        user_id="t_" + uuid.uuid4().hex,
        username="not_set",
        email=email,
        display_name=display_name,
    )


def test_descriptive_email_uses_email_localpart(app_db):
    user = _make_user("john.smith@students.jointheleague.org", "John Smith")
    assert find_username(user) == "john-smith"


def test_numeric_email_falls_back_to_display_name(app_db):
    user = _make_user("52@students.jointheleague.org", "Jane Doe")
    assert find_username(user) == "jane-doe"


def test_numeric_email_without_name_keeps_number(app_db):
    user = _make_user("53@students.jointheleague.org", None)
    assert find_username(user) == "53"


def test_collision_gets_numeric_suffix(app_db):
    existing = _make_user("55@students.jointheleague.org", "Sam Lee")
    existing.username = find_username(existing)
    assert existing.username == "sam-lee"
    app_db.session.add(existing)
    app_db.session.commit()

    other = _make_user("56@students.jointheleague.org", "Sam Lee")
    assert find_username(other) == "sam-lee_1"


def test_no_email_no_name_gets_random_handle(app_db):
    user = _make_user(None, None)
    name = find_username(user)
    assert name.startswith("user-")
