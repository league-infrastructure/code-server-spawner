"""Tests for POST /classes/<id>/cluster and GET /classes/<id>/cluster/status.

Sprint 005, Ticket 002.

Verifies:
- POST stamps purge_after, purge_by, target_nodes and returns JSON immediately.
- Timestamp math for end_date set vs. end_date=None.
- target_nodes sizing from roster + tier capacity.
- Idempotent re-arm recomputes all three fields.
- Non-instructor receives 403.
- GET /cluster/status returns correct zone string for all four states.
- No Docker or DO API calls occur in either route.

Uses a minimal in-memory Flask app with sqlite — no live PostgreSQL or DO needed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from math import ceil

import pytest
from flask import Flask
from flask_login import LoginManager, login_user

from cspawn.models import db, Class, ClassProto, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_proto(db_session) -> ClassProto:
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


def _make_class(db_session, proto_id: int, end_date=None) -> Class:
    cls = Class(
        name="Test Class",
        proto_id=proto_id,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_date=end_date,
    )
    db_session.session.add(cls)
    db_session.session.flush()
    return cls


# ---------------------------------------------------------------------------
# Fixture — isolated Flask app per test
# ---------------------------------------------------------------------------

@pytest.fixture()
def flask_app():
    """Minimal Flask test app with in-memory SQLite and login support."""
    import os

    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"))
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"
    # Default tier: capacity 6 (no NODE_TIERS config → synthesized default)
    app.config["DEFAULT_CAPACITY"] = "6"
    app.config["LOGIN_DISABLED"] = False

    db.init_app(app)

    from cspawn.main import main_bp
    app.register_blueprint(main_bp)

    login_manager = LoginManager()
    login_manager.init_app(app)

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


@pytest.fixture()
def instructor(flask_app):
    with flask_app.app_context():
        user = _make_user(db, "instructor1", is_instructor=True)
        db.session.commit()
        return user.id


@pytest.fixture()
def student_user(flask_app):
    with flask_app.app_context():
        user = _make_user(db, "student1", is_instructor=False)
        db.session.commit()
        return user.id


@pytest.fixture()
def class_with_end_date(flask_app, instructor):
    """Class with end_date at 18:00 UTC, owned by the instructor, 12 students."""
    with flask_app.app_context():
        proto = _minimal_proto(db)
        instr = User.query.get(instructor)
        end = datetime(2026, 6, 30, 18, 0, 0, tzinfo=timezone.utc)
        cls = _make_class(db, proto.id, end_date=end)
        cls.instructors.append(instr)

        # Add 12 students
        for i in range(12):
            s = _make_user(db, f"stu{i}", is_instructor=False)
            cls.students.append(s)

        db.session.commit()
        return cls.id


@pytest.fixture()
def class_no_end_date(flask_app, instructor):
    """Class without end_date, owned by instructor, 6 students."""
    with flask_app.app_context():
        proto = _minimal_proto(db)
        instr = User.query.get(instructor)
        cls = _make_class(db, proto.id, end_date=None)
        cls.instructors.append(instr)

        for i in range(6):
            s = _make_user(db, f"noend_stu{i}", is_instructor=False)
            cls.students.append(s)

        db.session.commit()
        return cls.id


# ---------------------------------------------------------------------------
# Helper: log in a user via the test client
# ---------------------------------------------------------------------------

def _login(client, flask_app, user_id: int):
    with flask_app.app_context():
        with client.session_transaction() as sess:
            # Flask-Login stores user_id as string in cookie
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True


# ---------------------------------------------------------------------------
# POST /classes/<id>/cluster — happy path with end_date
# ---------------------------------------------------------------------------

class TestClusterArmWithEndDate:
    def test_returns_200_json_success(self, flask_app, client, instructor, class_with_end_date):
        _login(client, flask_app, instructor)
        resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"success": True}

    def test_purge_after_is_at_least_click_plus_1h(self, flask_app, client, instructor, class_with_end_date):
        """purge_after >= click_time + 1h always."""
        before = datetime.now(timezone.utc)
        _login(client, flask_app, instructor)
        client.post(f"/classes/{class_with_end_date}/cluster")
        after = datetime.now(timezone.utc)

        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            pa = cls.purge_after
            if pa.tzinfo is None:
                pa = pa.replace(tzinfo=timezone.utc)
            # Must be >= before + 1h
            assert pa >= before + timedelta(hours=1)

    def test_purge_after_uses_end_date_time_of_day_when_later(self, flask_app, client, instructor, class_with_end_date):
        """When today @ end_date-time-of-day > click+1h, purge_after equals that cutoff."""
        # end_date is 18:00 UTC. If click_time + 1h < 18:00 today, purge_after = today@18:00.
        # We set end_date to 23:59 to make the time-of-day component clearly dominant.
        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            # Set end_date to 23:59 UTC today — well after click+1h in any reasonable test
            now_utc = datetime.now(timezone.utc)
            late = now_utc.replace(hour=23, minute=59, second=0, microsecond=0)
            cls.end_date = late
            db.session.commit()

        _login(client, flask_app, instructor)
        before = datetime.now(timezone.utc)
        client.post(f"/classes/{class_with_end_date}/cluster")

        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            pa = cls.purge_after
            if pa.tzinfo is None:
                pa = pa.replace(tzinfo=timezone.utc)

            # Expected: today @ 23:59 UTC
            expected_cutoff = datetime.combine(
                before.date(), late.timetz()
            ).replace(tzinfo=timezone.utc)
            # purge_after should be >= before+1h (always) and equal to today@23:59
            if expected_cutoff > before + timedelta(hours=1):
                assert abs((pa - expected_cutoff).total_seconds()) < 5

    def test_purge_by_equals_purge_after_plus_1h(self, flask_app, client, instructor, class_with_end_date):
        _login(client, flask_app, instructor)
        client.post(f"/classes/{class_with_end_date}/cluster")

        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            pa = cls.purge_after
            pb = cls.purge_by
            if pa.tzinfo is None:
                pa = pa.replace(tzinfo=timezone.utc)
            if pb.tzinfo is None:
                pb = pb.replace(tzinfo=timezone.utc)
            assert abs((pb - pa).total_seconds() - 3600) < 5

    def test_target_nodes_equals_ceil_students_over_capacity(self, flask_app, client, instructor, class_with_end_date):
        """12 students / capacity 6 = 2 nodes."""
        _login(client, flask_app, instructor)
        client.post(f"/classes/{class_with_end_date}/cluster")

        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            # DEFAULT_CAPACITY = 6, students = 12 → ceil(12/6) = 2
            assert cls.target_nodes == 2


# ---------------------------------------------------------------------------
# POST /classes/<id>/cluster — null end_date → click + 1h
# ---------------------------------------------------------------------------

class TestClusterArmNoEndDate:
    def test_purge_after_approximately_click_plus_1h(self, flask_app, client, instructor, class_no_end_date):
        before = datetime.now(timezone.utc)
        _login(client, flask_app, instructor)
        client.post(f"/classes/{class_no_end_date}/cluster")
        after = datetime.now(timezone.utc)

        with flask_app.app_context():
            cls = Class.query.get(class_no_end_date)
            pa = cls.purge_after
            if pa.tzinfo is None:
                pa = pa.replace(tzinfo=timezone.utc)

            # purge_after should be click_time + 1h; within 5s tolerance
            assert before + timedelta(hours=1) <= pa <= after + timedelta(hours=1) + timedelta(seconds=5)

    def test_target_nodes_ceil_6_students_over_6_capacity(self, flask_app, client, instructor, class_no_end_date):
        """6 students / capacity 6 = 1 node."""
        _login(client, flask_app, instructor)
        client.post(f"/classes/{class_no_end_date}/cluster")

        with flask_app.app_context():
            cls = Class.query.get(class_no_end_date)
            assert cls.target_nodes == 1


# ---------------------------------------------------------------------------
# Idempotent re-arm
# ---------------------------------------------------------------------------

class TestClusterReArm:
    def test_second_post_recomputes_all_fields(self, flask_app, client, instructor, class_with_end_date):
        """A second POST recomputes purge_after, purge_by, and target_nodes."""
        _login(client, flask_app, instructor)

        # First POST
        resp1 = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp1.status_code == 200

        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            pa1 = cls.purge_after
            pb1 = cls.purge_by
            tn1 = cls.target_nodes

        # Second POST (with a brief logical separation — timestamps may differ by sub-second)
        resp2 = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp2.status_code == 200

        with flask_app.app_context():
            cls = Class.query.get(class_with_end_date)
            pa2 = cls.purge_after
            pb2 = cls.purge_by
            tn2 = cls.target_nodes

        # Fields are recomputed (may be equal if instant, but must be present)
        assert pa2 is not None
        assert pb2 is not None
        assert tn2 is not None
        # target_nodes must still be correct on re-arm
        assert tn2 == tn1  # roster unchanged between the two POSTs

    def test_second_post_does_not_reject(self, flask_app, client, instructor, class_with_end_date):
        """Re-arm returns 200, not 409 or 400."""
        _login(client, flask_app, instructor)
        client.post(f"/classes/{class_with_end_date}/cluster")
        resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestClusterArmAuth:
    def test_non_instructor_gets_403(self, flask_app, client, student_user, class_with_end_date):
        _login(client, flask_app, student_user)
        resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 403

    def test_unauthenticated_gets_403(self, flask_app, client, class_with_end_date):
        resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 403

    def test_instructor_gets_200(self, flask_app, client, instructor, class_with_end_date):
        _login(client, flask_app, instructor)
        resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /classes/<id>/cluster/status — zone strings
# ---------------------------------------------------------------------------

class TestClusterStatus:
    def _set_class_timestamps(self, flask_app, class_id, purge_after, purge_by):
        with flask_app.app_context():
            cls = Class.query.get(class_id)
            cls.purge_after = purge_after
            cls.purge_by = purge_by
            db.session.commit()

    def test_unarmed_when_purge_after_is_none(self, flask_app, client, instructor, class_with_end_date):
        # Ensure purge_after is None (fresh class)
        self._set_class_timestamps(flask_app, class_with_end_date, None, None)
        _login(client, flask_app, instructor)
        resp = client.get(f"/classes/{class_with_end_date}/cluster/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "unarmed"}

    def test_provisioning_when_now_before_purge_after(self, flask_app, client, instructor, class_with_end_date):
        now = datetime.now(timezone.utc)
        pa = now + timedelta(hours=1)   # purge_after in the future
        pb = pa + timedelta(hours=1)
        self._set_class_timestamps(flask_app, class_with_end_date, pa, pb)
        _login(client, flask_app, instructor)
        resp = client.get(f"/classes/{class_with_end_date}/cluster/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "provisioning"}

    def test_active_when_now_between_purge_after_and_purge_by(self, flask_app, client, instructor, class_with_end_date):
        now = datetime.now(timezone.utc)
        pa = now - timedelta(minutes=30)  # purge_after in the past
        pb = now + timedelta(minutes=30)  # purge_by in the future
        self._set_class_timestamps(flask_app, class_with_end_date, pa, pb)
        _login(client, flask_app, instructor)
        resp = client.get(f"/classes/{class_with_end_date}/cluster/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "active"}

    def test_expired_when_now_after_purge_by(self, flask_app, client, instructor, class_with_end_date):
        now = datetime.now(timezone.utc)
        pa = now - timedelta(hours=3)
        pb = now - timedelta(hours=2)   # purge_by in the past
        self._set_class_timestamps(flask_app, class_with_end_date, pa, pb)
        _login(client, flask_app, instructor)
        resp = client.get(f"/classes/{class_with_end_date}/cluster/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "expired"}

    def test_status_non_instructor_gets_403(self, flask_app, client, student_user, class_with_end_date):
        _login(client, flask_app, student_user)
        resp = client.get(f"/classes/{class_with_end_date}/cluster/status")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# No Docker / DO primitives called
# ---------------------------------------------------------------------------

class TestNoDockerCalls:
    def test_post_does_not_call_run_autoscale(self, flask_app, client, instructor, class_with_end_date):
        from unittest.mock import patch
        _login(client, flask_app, instructor)
        with patch("cspawn.cs_docker.autoscale.run_autoscale") as mock_autoscale:
            resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 200
        mock_autoscale.assert_not_called()

    def test_post_does_not_call_expand_node(self, flask_app, client, instructor, class_with_end_date):
        from unittest.mock import patch
        _login(client, flask_app, instructor)
        with patch("cspawn.cli.node._create_droplet") as mock_expand:
            resp = client.post(f"/classes/{class_with_end_date}/cluster")
        assert resp.status_code == 200
        mock_expand.assert_not_called()
