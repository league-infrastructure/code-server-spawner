"""Tests for ticket 007-002: migrate web/admin/background stop paths onto
``CodeServerManager.stop_host()``.

Covers the four call sites that were not already exercised by
``test/test_stop_host.py`` (the choke point itself) or
``test/test_autoscale.py::TestApplyReaperZones`` (the autoscale reaper):

- GET  /host/<host_id>/stop           — cspawn/main/routes/hosts.py::stop_host
- POST /admin/host/<int:host_id>/stop — cspawn/admin/routes.py::stop_host
- admin/teardown.py::_stop_user_servers
- POST /classes/students/remove       — cspawn/main/routes/classes.py::remove_students

No live Docker, GitHub, or Postgres access in any test here — ``app.csm`` is
always a ``MagicMock`` and every test uses an in-memory SQLite DB.

Run with::

    uv run pytest test/test_stop_call_sites.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_login import LoginManager

from cspawn.cs_docker.csmanager import StopResult
from cspawn.models import Class, ClassProto, CodeHost, User, db


# ---------------------------------------------------------------------------
# Shared fixtures — Flask app with main_bp + admin_bp registered, app.csm mocked
# ---------------------------------------------------------------------------

@pytest.fixture()
def flask_app():
    """Minimal Flask test app with in-memory SQLite and the real main + admin
    blueprints registered, so the routes under test run through their actual
    URL rules and decorators. ``app.csm`` is a MagicMock — no Docker/GitHub
    calls are attempted.
    """
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-stop-call-sites-secret"

    db.init_app(app)

    from cspawn.main import main_bp
    app.register_blueprint(main_bp)

    from cspawn.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/admin")

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "main.index"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    with app.app_context():
        db.create_all()
        app.csm = MagicMock()
        app.app_config = {}
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


_counter = 0


def _make_user(flask_app, username: str, **kwargs) -> int:
    with flask_app.app_context():
        user = User(user_id=f"uid-{username}", username=username, is_active=True, **kwargs)
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_host(flask_app, user_id: int, suffix: str) -> int:
    with flask_app.app_context():
        host = CodeHost(
            user_id=user_id,
            service_id=f"svc-{suffix}",
            service_name=f"cs-{suffix}",
            app_state="ready",
            state="running",
        )
        db.session.add(host)
        db.session.commit()
        return host.id


def _ok_result(name: str = "x") -> StopResult:
    return StopResult(service_name=name, pushed=True, stopped=True, deleted=True)


# ---------------------------------------------------------------------------
# GET /host/<host_id>/stop — student stop route
# ---------------------------------------------------------------------------

class TestStudentStopRoute:
    """cspawn/main/routes/hosts.py::stop_host"""

    def test_owner_stop_calls_stop_host_with_the_code_host(self, flask_app, client):
        user_id = _make_user(flask_app, "student1")
        host_id = _make_host(flask_app, user_id, "s1")
        _login(client, flask_app, user_id)

        flask_app.csm.stop_host.return_value = _ok_result("cs-s1")

        resp = client.get(f"/host/{host_id}/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_called_once()
        (called_host,), _ = flask_app.csm.stop_host.call_args
        assert called_host.id == host_id

    def test_clean_push_flashes_success(self, flask_app, client):
        user_id = _make_user(flask_app, "student2")
        host_id = _make_host(flask_app, user_id, "s2")
        _login(client, flask_app, user_id)

        flask_app.csm.stop_host.return_value = _ok_result("cs-s2")

        with patch("cspawn.main.routes.hosts.flash") as mock_flash:
            client.get(f"/host/{host_id}/stop")

        mock_flash.assert_called_once()
        message, category = mock_flash.call_args[0]
        assert category == "success"

    def test_push_failure_flashes_warning_naming_the_error(self, flask_app, client):
        user_id = _make_user(flask_app, "student3")
        host_id = _make_host(flask_app, user_id, "s3")
        _login(client, flask_app, user_id)

        flask_app.csm.stop_host.return_value = StopResult(
            service_name="cs-s3", pushed=False, push_error="git push failed",
            stopped=True, deleted=True,
        )

        with patch("cspawn.main.routes.hosts.flash") as mock_flash:
            client.get(f"/host/{host_id}/stop")

        mock_flash.assert_called_once()
        message, category = mock_flash.call_args[0]
        assert category == "warning"
        assert "git push failed" in message

    def test_other_users_host_is_not_stopped(self, flask_app, client):
        owner_id = _make_user(flask_app, "owner4")
        other_id = _make_user(flask_app, "other4")
        host_id = _make_host(flask_app, owner_id, "s4")
        _login(client, flask_app, other_id)

        resp = client.get(f"/host/{host_id}/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_not_called()

    def test_mine_with_no_extant_host_does_not_call_stop_host(self, flask_app, client):
        user_id = _make_user(flask_app, "student5")
        _login(client, flask_app, user_id)

        resp = client.get("/host/mine/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_not_called()


# ---------------------------------------------------------------------------
# POST /admin/host/<int:host_id>/stop — admin stop route
# ---------------------------------------------------------------------------

class TestAdminStopRoute:
    """cspawn/admin/routes.py::stop_host"""

    def test_calls_stop_host_with_the_code_host(self, flask_app, client):
        admin_id = _make_user(flask_app, "admin1", is_admin=True)
        target_id = _make_user(flask_app, "target1")
        host_id = _make_host(flask_app, target_id, "a1")
        _login(client, flask_app, admin_id)

        flask_app.csm.stop_host.return_value = _ok_result("cs-a1")

        resp = client.post(f"/admin/host/{host_id}/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_called_once()
        (called_host,), _ = flask_app.csm.stop_host.call_args
        assert called_host.id == host_id

    def test_clean_push_flashes_success(self, flask_app, client):
        admin_id = _make_user(flask_app, "admin2", is_admin=True)
        target_id = _make_user(flask_app, "target2")
        host_id = _make_host(flask_app, target_id, "a2")
        _login(client, flask_app, admin_id)

        flask_app.csm.stop_host.return_value = _ok_result("cs-a2")

        with patch("cspawn.admin.routes.flash") as mock_flash:
            client.post(f"/admin/host/{host_id}/stop")

        message, category = mock_flash.call_args[0]
        assert category == "success"

    def test_push_failure_flashes_warning_naming_the_error(self, flask_app, client):
        admin_id = _make_user(flask_app, "admin3", is_admin=True)
        target_id = _make_user(flask_app, "target3")
        host_id = _make_host(flask_app, target_id, "a3")
        _login(client, flask_app, admin_id)

        flask_app.csm.stop_host.return_value = StopResult(
            service_name="cs-a3", pushed=False, push_error="push boom",
            stopped=True, deleted=True,
        )

        with patch("cspawn.admin.routes.flash") as mock_flash:
            client.post(f"/admin/host/{host_id}/stop")

        message, category = mock_flash.call_args[0]
        assert category == "warning"
        assert "push boom" in message

    def test_service_not_found_in_swarm_still_removes_db_row_via_stop_host(self, flask_app, client):
        """The old 'service not found in Swarm but present in DB' branch is
        gone; stop_host() itself treats a missing service as a successful
        stop and still deletes the row (via its own stop-failure tolerance).
        This test only asserts the route always delegates to stop_host(),
        which is the choke point that now owns that tolerance."""
        admin_id = _make_user(flask_app, "admin4", is_admin=True)
        target_id = _make_user(flask_app, "target4")
        host_id = _make_host(flask_app, target_id, "a4")
        _login(client, flask_app, admin_id)

        # Simulate stop_host()'s own handling of an already-gone service:
        # stopped=True (goal state already holds), deleted=True.
        flask_app.csm.stop_host.return_value = _ok_result("cs-a4")

        resp = client.post(f"/admin/host/{host_id}/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_called_once()

    def test_host_id_zero_does_not_call_stop_host(self, flask_app, client):
        """Preserves the pre-existing (falsy-int) guard: host_id=0 short-circuits."""
        admin_id = _make_user(flask_app, "admin5", is_admin=True)
        _login(client, flask_app, admin_id)

        resp = client.post("/admin/host/0/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_not_called()

    def test_unknown_host_returns_404_and_does_not_call_stop_host(self, flask_app, client):
        admin_id = _make_user(flask_app, "admin6", is_admin=True)
        _login(client, flask_app, admin_id)

        resp = client.post("/admin/host/999999/stop")

        assert resp.status_code == 404
        flask_app.csm.stop_host.assert_not_called()

    def test_non_admin_is_redirected_and_stop_host_not_called(self, flask_app, client):
        plain_id = _make_user(flask_app, "plainadmin1", is_admin=False)
        target_id = _make_user(flask_app, "target5")
        host_id = _make_host(flask_app, target_id, "a5")
        _login(client, flask_app, plain_id)

        resp = client.post(f"/admin/host/{host_id}/stop")

        assert resp.status_code == 302
        flask_app.csm.stop_host.assert_not_called()


# ---------------------------------------------------------------------------
# admin/teardown.py::_stop_user_servers
# ---------------------------------------------------------------------------

def _make_bare_app():
    """Minimal Flask app (no blueprints) for direct function-level testing."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
    return app


def _make_user_with_hosts(app, n: int, *, username: str = "teardownuser"):
    with app.app_context():
        user = User(user_id=f"uid-{username}", username=username, is_active=True)
        db.session.add(user)
        db.session.flush()

        host_ids = []
        for i in range(n):
            host = CodeHost(
                user_id=user.id,
                service_id=f"svc-td-{username}-{i}",
                service_name=f"cs-td-{username}-{i}",
                app_state="ready",
                state="running",
            )
            db.session.add(host)
            db.session.flush()
            host_ids.append(host.id)

        db.session.commit()
        return user.id, host_ids


class TestStopUserServers:
    """cspawn/admin/teardown.py::_stop_user_servers — delegates to stop_host()."""

    def test_all_hosts_stopped_via_stop_host(self):
        from cspawn.admin.teardown import TeardownReport, _stop_user_servers

        app = _make_bare_app()
        user_id, host_ids = _make_user_with_hosts(app, 3, username="tdok")

        with app.app_context():
            user = User.query.get(user_id)
            report = TeardownReport(username=user.username)
            app.csm = MagicMock()
            app.csm.stop_host.return_value = _ok_result("x")

            _stop_user_servers(app, user, report)

            assert app.csm.stop_host.call_count == 3
            assert len(report.servers_stopped) == 3
            assert report.failures == []

    def test_push_failure_on_one_host_is_recorded_but_does_not_abort_the_batch(self):
        """A mocked push failure for one host must not prevent the remaining
        hosts in the same user's batch from being processed."""
        from cspawn.admin.teardown import TeardownReport, _stop_user_servers

        app = _make_bare_app()
        user_id, host_ids = _make_user_with_hosts(app, 3, username="tdpush")

        with app.app_context():
            user = User.query.get(user_id)
            report = TeardownReport(username=user.username)
            app.csm = MagicMock()

            def side_effect(ch, *, push=True, branch="master"):
                if ch.id == host_ids[0]:
                    return StopResult(
                        service_name=ch.service_name, pushed=False,
                        push_error="push boom", stopped=True, deleted=True,
                    )
                return StopResult(service_name=ch.service_name, pushed=True, stopped=True, deleted=True)

            app.csm.stop_host.side_effect = side_effect

            _stop_user_servers(app, user, report)

            # All three hosts were still individually processed.
            assert app.csm.stop_host.call_count == 3
            # All three were deleted (push failure doesn't block delete),
            # so all three land in servers_stopped...
            assert len(report.servers_stopped) == 3
            # ...and the push failure is separately recorded as a failure.
            assert len(report.failures) == 1
            assert "push boom" in report.failures[0]

    def test_delete_failure_recorded_as_failure_not_as_stopped(self):
        from cspawn.admin.teardown import TeardownReport, _stop_user_servers

        app = _make_bare_app()
        user_id, host_ids = _make_user_with_hosts(app, 1, username="tddel")

        with app.app_context():
            user = User.query.get(user_id)
            report = TeardownReport(username=user.username)
            app.csm = MagicMock()
            app.csm.stop_host.return_value = StopResult(
                service_name="cs-td-tddel-0", pushed=True, stopped=True, deleted=False,
            )

            _stop_user_servers(app, user, report)

            assert report.servers_stopped == []
            assert len(report.failures) == 1

    def test_no_hosts_is_a_no_op(self):
        from cspawn.admin.teardown import TeardownReport, _stop_user_servers

        app = _make_bare_app()
        with app.app_context():
            user = User(user_id="uid-nohosts", username="nohosts", is_active=True)
            db.session.add(user)
            db.session.commit()
            report = TeardownReport(username=user.username)
            app.csm = MagicMock()

            _stop_user_servers(app, user, report)

            app.csm.stop_host.assert_not_called()
            assert report.servers_stopped == []
            assert report.failures == []


# ---------------------------------------------------------------------------
# POST /classes/students/remove — cspawn/main/routes/classes.py::remove_students
# ---------------------------------------------------------------------------

def _make_class_with_students(flask_app, n_students: int, *, tag: str = "c1"):
    """Create one ClassProto/Class/instructor and n_students, each with a
    CodeHost whose service_name matches the student's username (the join
    key remove_students uses)."""
    with flask_app.app_context():
        proto = ClassProto(name=f"Proto-{tag}", image_uri="img:latest", hash=f"deadbeef{tag}")
        db.session.add(proto)
        db.session.flush()

        instructor = User(
            user_id=f"uid-instr-{tag}", username=f"instr-{tag}",
            is_active=True, is_instructor=True,
        )
        db.session.add(instructor)
        db.session.flush()

        cls = Class(
            name=f"Class-{tag}",
            proto_id=proto.id,
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        cls.instructors.append(instructor)
        db.session.add(cls)
        db.session.flush()

        student_ids = []
        host_ids: dict[int, int] = {}
        for i in range(n_students):
            student = User(
                user_id=f"uid-stu-{tag}-{i}", username=f"stu-{tag}-{i}",
                is_active=True, is_student=True,
            )
            db.session.add(student)
            db.session.flush()
            cls.students.append(student)

            host = CodeHost(
                user_id=student.id,
                service_id=f"svc-stu-{tag}-{i}",
                service_name=f"stu-{tag}-{i}",  # matches student.username
                app_state="ready",
                state="running",
            )
            db.session.add(host)
            db.session.flush()

            student_ids.append(student.id)
            host_ids[student.id] = host.id

        db.session.commit()
        return instructor.id, cls.id, student_ids, host_ids


class TestRemoveStudents:
    """cspawn/main/routes/classes.py::remove_students"""

    def test_removes_student_and_calls_stop_host_with_their_host(self, flask_app, client):
        instr_id, cls_id, student_ids, host_ids = _make_class_with_students(flask_app, 2, tag="rs1")
        _login(client, flask_app, instr_id)

        flask_app.csm.stop_host.return_value = _ok_result("stu-rs1-0")

        resp = client.post(
            "/classes/students/remove",
            data=json.dumps({"student_ids": [student_ids[0]], "class_id": cls_id}),
            content_type="application/json",
        )

        assert resp.status_code == 200
        flask_app.csm.stop_host.assert_called_once()
        (called_host,), _ = flask_app.csm.stop_host.call_args
        assert called_host.id == host_ids[student_ids[0]]

        with flask_app.app_context():
            cls = Class.query.get(cls_id)
            remaining_ids = {s.id for s in cls.students}
            assert student_ids[0] not in remaining_ids
            assert student_ids[1] in remaining_ids

    def test_multi_host_push_failure_does_not_abort_remaining_removals(self, flask_app, client):
        """A mocked push failure for one student's host must not prevent the
        other student in the same request from being processed and removed."""
        instr_id, cls_id, student_ids, host_ids = _make_class_with_students(flask_app, 2, tag="rs2")
        _login(client, flask_app, instr_id)

        def side_effect(host, *, push=True, branch="master"):
            if host.id == host_ids[student_ids[0]]:
                return StopResult(
                    service_name=host.service_name, pushed=False,
                    push_error="boom", stopped=True, deleted=True,
                )
            return StopResult(service_name=host.service_name, pushed=True, stopped=True, deleted=True)

        flask_app.csm.stop_host.side_effect = side_effect

        resp = client.post(
            "/classes/students/remove",
            data=json.dumps({"student_ids": student_ids, "class_id": cls_id}),
            content_type="application/json",
        )

        assert resp.status_code == 200
        assert flask_app.csm.stop_host.call_count == 2
        with flask_app.app_context():
            cls = Class.query.get(cls_id)
            assert cls.students == []

    def test_non_instructor_forbidden_and_stop_host_not_called(self, flask_app, client):
        instr_id, cls_id, student_ids, host_ids = _make_class_with_students(flask_app, 1, tag="rs3")
        plain_id = _make_user(flask_app, "plainstudent1")
        _login(client, flask_app, plain_id)

        resp = client.post(
            "/classes/students/remove",
            data=json.dumps({"student_ids": student_ids, "class_id": cls_id}),
            content_type="application/json",
        )

        assert resp.status_code == 403
        flask_app.csm.stop_host.assert_not_called()

    def test_student_without_host_still_removed_from_class(self, flask_app, client):
        instr_id, cls_id, student_ids, host_ids = _make_class_with_students(flask_app, 1, tag="rs4")
        with flask_app.app_context():
            host = CodeHost.query.get(host_ids[student_ids[0]])
            db.session.delete(host)
            db.session.commit()

        _login(client, flask_app, instr_id)

        resp = client.post(
            "/classes/students/remove",
            data=json.dumps({"student_ids": student_ids, "class_id": cls_id}),
            content_type="application/json",
        )

        assert resp.status_code == 200
        flask_app.csm.stop_host.assert_not_called()
        with flask_app.app_context():
            cls = Class.query.get(cls_id)
            assert cls.students == []
