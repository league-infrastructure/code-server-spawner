"""Tests for the cluster pre-sizing section on the class detail page.

Sprint 005, Ticket 003.

Verifies:
- The cluster section is present when the viewing user is an instructor of the class.
- The cluster section is absent when the viewing user is a student.
- The cluster section is absent when the viewing user is not authenticated.
- Status text reflects each zone state (unarmed, provisioning, active, expired).
- "Create my cluster" button label is correct for unarmed/expired zones.
- Re-arm button label is correct for provisioning/active zones.
- target_nodes count is displayed in provisioning/active states.
- purge_after timestamp is shown in provisioning state.
- purge_by timestamp is shown in active state.

Notes:
- SQLite does not preserve timezone info in DateTime columns; the Class.can_start,
  Class.can_register, and Class.is_current hybrid properties compare tz-aware
  datetime.now(utc) against tz-naive values returned from SQLite, which raises
  TypeError.  We patch those properties to return safe defaults so the template
  can render without a live PostgreSQL database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, PropertyMock

import pytest
from flask import Blueprint, Flask
from flask_bootstrap import Bootstrap5
from flask_font_awesome import FontAwesome
from flask_login import LoginManager

from cspawn.models import db, Class, ClassProto, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proto(db_session) -> ClassProto:
    proto = ClassProto(
        name="Test Proto",
        image_uri="test-image:latest",
        hash="deadbeef",
    )
    db_session.session.add(proto)
    db_session.session.flush()
    return proto


def _make_user(
    db_session,
    username: str,
    is_instructor: bool = False,
    is_admin: bool = False,
) -> User:
    user = User(
        user_id=f"uid-{username}",
        username=username,
        is_instructor=is_instructor,
        is_admin=is_admin,
        is_active=True,
    )
    db_session.session.add(user)
    db_session.session.flush()
    return user


def _make_class(db_session, proto_id: int) -> Class:
    # Use a tz-naive start_date so comparisons work against SQLite's naive-returning driver.
    # SQLite doesn't preserve tzinfo even with DateTime(timezone=True).
    cls = Class(
        name="Test Class",
        proto_id=proto_id,
        start_date=datetime(2020, 1, 1),
    )
    db_session.session.add(cls)
    db_session.session.flush()
    return cls


def _make_stub_blueprints():
    """Return minimal stub blueprints for endpoints the base template url_for() references."""
    auth_stub = Blueprint("auth", __name__)

    @auth_stub.route("/auth/profile")
    def profile():
        return "profile"

    @auth_stub.route("/auth/logout")
    def logout():
        return "logout"

    @auth_stub.route("/auth/login")
    def login():
        return "login"

    admin_stub = Blueprint("admin", __name__)

    @admin_stub.route("/admin/")
    def index():
        return "admin"

    @admin_stub.route("/admin/stop_impersonating", methods=["POST"])
    def stop_impersonating():
        return "stop"

    return auth_stub, admin_stub


# ---------------------------------------------------------------------------
# Fixture — isolated Flask app
# ---------------------------------------------------------------------------


@pytest.fixture()
def flask_app():
    """Minimal Flask test app with in-memory SQLite, login support, and bootstrap."""
    app = Flask("cspawn")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"
    app.config["DEFAULT_CAPACITY"] = "6"
    app.config["LOGIN_DISABLED"] = False

    db.init_app(app)

    from cspawn.main import main_bp
    app.register_blueprint(main_bp, url_prefix="/")

    auth_stub, admin_stub = _make_stub_blueprints()
    app.register_blueprint(auth_stub, url_prefix="/auth")
    app.register_blueprint(admin_stub, url_prefix="/admin")

    # Bootstrap5 and FontAwesome are required by the base template
    Bootstrap5(app)
    FontAwesome(app)

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


def _login(client, flask_app, user_id: int):
    with flask_app.app_context():
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True


def _get_detail(client, class_id: int):
    """GET /class/<id>/details with SQLite-safe hybrid-property patches.

    SQLite drops timezone info from DateTime columns, causing tz-aware vs.
    tz-naive comparison failures in Class.can_start / can_register / is_current.
    We temporarily replace those hybrid_property descriptors with plain
    properties that return safe defaults.
    """
    from sqlalchemy.ext.hybrid import hybrid_property as _hp

    _orig_can_start = Class.__dict__["can_start"]
    _orig_can_register = Class.__dict__["can_register"]
    _orig_is_current = Class.__dict__["is_current"]

    # Replace with simple properties that return False
    Class.can_start = property(lambda self: False)
    Class.can_register = property(lambda self: False)
    Class.is_current = property(lambda self: False)

    try:
        return client.get(f"/class/{class_id}/details")
    finally:
        Class.can_start = _orig_can_start
        Class.can_register = _orig_can_register
        Class.is_current = _orig_is_current


# ---------------------------------------------------------------------------
# Shared data fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def setup_users_and_class(flask_app):
    """Return (instructor_id, student_id, other_instructor_id, class_id)."""
    with flask_app.app_context():
        proto = _make_proto(db)
        instr = _make_user(db, "instr1", is_instructor=True)
        student = _make_user(db, "stu1", is_instructor=False)
        other_instr = _make_user(db, "other_instr", is_instructor=True)
        cls = _make_class(db, proto.id)
        cls.instructors.append(instr)
        cls.students.append(student)
        db.session.commit()
        return instr.id, student.id, other_instr.id, cls.id


# ---------------------------------------------------------------------------
# Section visibility
# ---------------------------------------------------------------------------


class TestClusterSectionVisibility:
    def test_cluster_section_present_for_class_instructor(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        assert resp.status_code == 200
        assert b"Cluster Pre-sizing" in resp.data

    def test_cluster_section_absent_for_student(self, flask_app, client, setup_users_and_class):
        _, student_id, _, class_id = setup_users_and_class
        _login(client, flask_app, student_id)
        # detail_class is instructor_required — students get 403
        resp = _get_detail(client, class_id)
        assert resp.status_code == 403

    def test_cluster_section_absent_for_other_instructor(self, flask_app, client, setup_users_and_class):
        """An instructor who is NOT in class_.instructors must not see the cluster section."""
        _, _, other_instr_id, class_id = setup_users_and_class
        _login(client, flask_app, other_instr_id)
        resp = _get_detail(client, class_id)
        assert resp.status_code == 200
        # other_instr is not in class_.instructors — section must be absent
        assert b"Cluster Pre-sizing" not in resp.data

    def test_cluster_section_absent_for_unauthenticated(self, flask_app, client, setup_users_and_class):
        _, _, _, class_id = setup_users_and_class
        resp = _get_detail(client, class_id)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Zone display text
# ---------------------------------------------------------------------------


def _set_purge_window(flask_app, class_id, purge_after, purge_by, target_nodes=0):
    with flask_app.app_context():
        cls = Class.query.get(class_id)
        cls.purge_after = purge_after
        cls.purge_by = purge_by
        cls.target_nodes = target_nodes
        db.session.commit()


class TestClusterZoneDisplay:
    def test_unarmed_shows_create_my_cluster_button(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        # purge_after is None by default
        _set_purge_window(flask_app, class_id, None, None)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        assert resp.status_code == 200
        assert b"Create my cluster" in resp.data

    def test_provisioning_shows_cluster_presized_text(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        now = datetime.now(timezone.utc)
        pa = now + timedelta(hours=1)
        pb = pa + timedelta(hours=1)
        _set_purge_window(flask_app, class_id, pa, pb, target_nodes=3)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Cluster pre-sized" in html
        assert "3 nodes" in html
        assert "Re-arm cluster" in html

    def test_provisioning_shows_purge_after_timestamp(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        now = datetime.now(timezone.utc)
        pa = now + timedelta(hours=1)
        pb = pa + timedelta(hours=1)
        _set_purge_window(flask_app, class_id, pa, pb, target_nodes=2)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        html = resp.data.decode()
        # The timestamp must appear in the form we render (%Y-%m-%d %H:%M UTC)
        expected_ts = pa.strftime("%Y-%m-%d %H:%M UTC")
        assert expected_ts in html

    def test_active_shows_cluster_active_text(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        now = datetime.now(timezone.utc)
        pa = now - timedelta(minutes=30)
        pb = now + timedelta(minutes=30)
        _set_purge_window(flask_app, class_id, pa, pb, target_nodes=2)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        html = resp.data.decode()
        assert "Cluster active" in html
        assert "2 nodes" in html
        assert "Re-arm cluster" in html

    def test_active_shows_purge_by_timestamp(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        now = datetime.now(timezone.utc)
        pa = now - timedelta(minutes=30)
        pb = now + timedelta(minutes=30)
        _set_purge_window(flask_app, class_id, pa, pb, target_nodes=1)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        html = resp.data.decode()
        expected_ts = pb.strftime("%Y-%m-%d %H:%M UTC")
        assert expected_ts in html

    def test_expired_shows_window_expired_text(self, flask_app, client, setup_users_and_class):
        instr_id, _, _, class_id = setup_users_and_class
        now = datetime.now(timezone.utc)
        pa = now - timedelta(hours=3)
        pb = now - timedelta(hours=2)
        _set_purge_window(flask_app, class_id, pa, pb, target_nodes=1)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        html = resp.data.decode()
        assert "Cluster window expired" in html
        assert "Create new cluster" in html

    def test_singular_node_label_when_target_nodes_is_1(self, flask_app, client, setup_users_and_class):
        """1 node should say '1 node' not '1 nodes'."""
        instr_id, _, _, class_id = setup_users_and_class
        now = datetime.now(timezone.utc)
        pa = now + timedelta(hours=1)
        pb = pa + timedelta(hours=1)
        _set_purge_window(flask_app, class_id, pa, pb, target_nodes=1)
        _login(client, flask_app, instr_id)
        resp = _get_detail(client, class_id)
        html = resp.data.decode()
        assert "1 node " in html or "1 node<" in html or "1 node." in html
        assert "1 nodes" not in html
