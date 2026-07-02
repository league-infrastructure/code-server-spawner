"""Tests for ticket 007-003: migrate CLI stop paths (`host stop`, `host
purge`, `sys shutdown`, `test teardown`) onto `CodeServerManager.stop_host()`
/ `remove_all()`, and the new `--no-push` escape hatches.

Covers:
- `host stop <name>` / `host stop --all`: routes through `stop_host()` /
  resolves rows via `CSMService.rec`; `--no-push` forwards `push=False`;
  a target with no matching `CodeHost` row falls back to a direct
  `.stop()` with a warning; one host's failure doesn't abort `--all`.
- `host purge`: byte-for-byte stdout parity with the pre-refactor inline
  implementation for the pushed / push-failed / stop-failed / dry-run
  (with and without --no-push) cases, now driven by `stop_host()`.
- `sys shutdown`: routes through `remove_all()`; `--no-push` forwards
  `push=False`.
- `test teardown`: always calls `stop_host(ch, push=False)` when a
  `CodeHost` row exists for the test student; falls back to a direct
  `.stop()` for a DB-row-less live service; the `--dry-run` "would stop
  service <name>" text is unchanged.

No live Docker, GitHub, or Postgres access in any test here — `app.csm`
is always a `MagicMock` and every test uses an in-memory SQLite DB.

Run with::

    uv run pytest test/test_cli_stop_paths.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from docker.errors import NotFound
from flask import Flask

from cspawn.cli.host import purge as host_purge_cmd
from cspawn.cli.host import stop as host_stop_cmd
from cspawn.cli.sys import shutdown as sys_shutdown_cmd
from cspawn.cli.test import teardown as test_teardown_cmd
from cspawn.cs_docker.csmanager import StopResult
from cspawn.models import CodeHost, User, db


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_db():
    """Fresh in-memory SQLite Flask app with all tables created, app.csm mocked."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        app.csm = MagicMock()
        app.db = db
        yield app
        db.session.remove()
        db.drop_all()


def _make_user(app, username: str) -> int:
    with app.app_context():
        user = User(user_id=f"uid-{username}", username=username, is_active=True)
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_host(app, user_id: int, service_name: str, *, mia: bool = False) -> int:
    with app.app_context():
        host = CodeHost(
            user_id=user_id,
            service_id=f"svc-{service_name}",
            service_name=service_name,
            app_state="mia" if mia else "ready",
            state="mia" if mia else "running",
        )
        db.session.add(host)
        db.session.commit()
        return host.id


def _ok_result(name: str = "x", **overrides) -> StopResult:
    defaults = dict(pushed=True, stopped=True, deleted=True)
    defaults.update(overrides)
    return StopResult(service_name=name, **defaults)


# ---------------------------------------------------------------------------
# host stop <name>
# ---------------------------------------------------------------------------

class TestHostStopSingle:
    def test_default_pushes_via_stop_host(self, app_db):
        user_id = _make_user(app_db, "stu1")
        _make_host(app_db, user_id, "cs-stu1")
        app_db.csm.stop_host.return_value = _ok_result("cs-stu1")

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["cs-stu1"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_called_once()
        (called_host,), kwargs = app_db.csm.stop_host.call_args
        assert called_host.service_name == "cs-stu1"
        assert kwargs["push"] is True

    def test_no_push_flag_passes_push_false(self, app_db):
        user_id = _make_user(app_db, "stu2")
        _make_host(app_db, user_id, "cs-stu2")
        app_db.csm.stop_host.return_value = _ok_result("cs-stu2")

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["--no-push", "cs-stu2"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_called_once()
        _, kwargs = app_db.csm.stop_host.call_args
        assert kwargs["push"] is False

    def test_orphan_service_falls_back_to_direct_stop(self, app_db):
        """No CodeHost row for the name — stop_host() is never called; the
        live Swarm service is stopped directly instead, with a warning."""
        mock_service = MagicMock()
        app_db.csm.get.return_value = mock_service

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["orphan-svc"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_not_called()
        mock_service.stop.assert_called_once()

    def test_service_not_found_prints_message_and_skips_stop_host(self, app_db):
        app_db.csm.get.side_effect = NotFound("nope")

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["missing-svc"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "Service missing-svc not found" in result.output
        app_db.csm.stop_host.assert_not_called()


# ---------------------------------------------------------------------------
# host stop --all
# ---------------------------------------------------------------------------

class TestHostStopAll:
    def test_calls_stop_host_per_service_with_row(self, app_db):
        user_id = _make_user(app_db, "stu3")
        host_id = _make_host(app_db, user_id, "cs-stu3")
        with app_db.app_context():
            ch = CodeHost.query.get(host_id)

        svc = MagicMock()
        svc.name = "cs-stu3"
        svc.rec = ch
        app_db.csm.list.return_value = [svc]
        app_db.csm.stop_host.return_value = _ok_result("cs-stu3")

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["--all"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_called_once()
        (called_host,), kwargs = app_db.csm.stop_host.call_args
        assert called_host.service_name == "cs-stu3"
        assert kwargs["push"] is True

    def test_no_push_flag_forwarded_for_all(self, app_db):
        user_id = _make_user(app_db, "stu3b")
        host_id = _make_host(app_db, user_id, "cs-stu3b")
        with app_db.app_context():
            ch = CodeHost.query.get(host_id)

        svc = MagicMock()
        svc.name = "cs-stu3b"
        svc.rec = ch
        app_db.csm.list.return_value = [svc]
        app_db.csm.stop_host.return_value = _ok_result("cs-stu3b")

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["--all", "--no-push"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        _, kwargs = app_db.csm.stop_host.call_args
        assert kwargs["push"] is False

    def test_orphan_in_all_falls_back_and_does_not_abort_loop(self, app_db):
        user_id = _make_user(app_db, "stu4")
        host_id = _make_host(app_db, user_id, "cs-stu4")
        with app_db.app_context():
            ch = CodeHost.query.get(host_id)

        orphan_svc = MagicMock()
        orphan_svc.name = "orphan"
        orphan_svc.rec = None
        orphan_svc.stop.side_effect = RuntimeError("boom")

        row_svc = MagicMock()
        row_svc.name = "cs-stu4"
        row_svc.rec = ch

        app_db.csm.list.return_value = [orphan_svc, row_svc]
        app_db.csm.stop_host.return_value = _ok_result("cs-stu4")

        with patch("cspawn.cli.host.get_app", return_value=app_db), \
             patch("cspawn.cli.host.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(host_stop_cmd, ["--all"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        orphan_svc.stop.assert_called_once()
        # The orphan's failure did not abort the loop — the row-bearing host
        # after it was still processed via stop_host().
        app_db.csm.stop_host.assert_called_once()
        (called_host,), _ = app_db.csm.stop_host.call_args
        assert called_host.service_name == "cs-stu4"
        assert "All services stopped successfully" in result.output


# ---------------------------------------------------------------------------
# host purge — byte-for-byte stdout parity with the pre-refactor block
# ---------------------------------------------------------------------------

class TestHostPurge:
    def test_pushed_stdout_matches_expected_format(self, app_db):
        user_id = _make_user(app_db, "px1")
        _make_host(app_db, user_id, "cs-px1", mia=True)

        # Capture the call details synchronously, while the CodeHost row is
        # still attached to a live session — purge()'s own trailing
        # db.session.commit() expires the instance afterward (unlike the
        # other tests in this class, app.db here is the real db object).
        captured = []

        def _stop_host(ch, *, push=True, branch="master"):
            captured.append((ch.service_name, push))
            return StopResult(service_name=ch.service_name, pushed=True, stopped=True, deleted=True)

        app_db.csm.stop_host.side_effect = _stop_host

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, [], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert result.output == "cs-px1:  (pushed) Stopped and deleted:   cs-px1\n"
        assert captured == [("cs-px1", True)]

    def test_push_failed_stdout_matches_expected_format(self, app_db):
        user_id = _make_user(app_db, "px2")
        _make_host(app_db, user_id, "cs-px2", mia=True)
        app_db.csm.stop_host.return_value = StopResult(
            service_name="cs-px2", pushed=False, push_error="git push failed",
            stopped=True, deleted=True,
        )

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, [], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert result.output == (
            "cs-px2:  (push failed: git push failed) Stopped and deleted:   cs-px2\n"
        )

    def test_stop_failed_stdout_matches_expected_format(self, app_db):
        user_id = _make_user(app_db, "px3")
        _make_host(app_db, user_id, "cs-px3", mia=True)
        app_db.csm.stop_host.return_value = StopResult(
            service_name="cs-px3", pushed=True, stopped=False,
            stop_error="node unreachable", deleted=True,
        )

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, [], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert result.output == (
            "cs-px3:  (pushed) (stop failed: node unreachable) Stopped and deleted:   cs-px3\n"
        )

    def test_dry_run_with_push_prints_would_push_stop_and_delete(self, app_db):
        user_id = _make_user(app_db, "px4")
        _make_host(app_db, user_id, "cs-px4", mia=True)

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, ["--dry-run"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert result.output == "cs-px4:  Would push, stop and delete: cs-px4\n"
        app_db.csm.stop_host.assert_not_called()

    def test_dry_run_no_push_prints_would_stop_and_delete(self, app_db):
        user_id = _make_user(app_db, "px5")
        _make_host(app_db, user_id, "cs-px5", mia=True)

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, ["--dry-run", "--no-push"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert result.output == "cs-px5:  Would stop and delete: cs-px5\n"
        app_db.csm.stop_host.assert_not_called()

    def test_no_push_flag_passes_push_false_to_stop_host(self, app_db):
        user_id = _make_user(app_db, "px6")
        _make_host(app_db, user_id, "cs-px6", mia=True)
        app_db.csm.stop_host.return_value = StopResult(
            service_name="cs-px6", stopped=True, deleted=True,
        )

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, ["--no-push"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        _, kwargs = app_db.csm.stop_host.call_args
        assert kwargs["push"] is False

    def test_final_commit_called_once_after_loop_when_not_dry_run(self, app_db):
        user_id = _make_user(app_db, "px7")
        _make_host(app_db, user_id, "cs-px7", mia=True)
        app_db.csm.stop_host.return_value = _ok_result("cs-px7")
        app_db.db = MagicMock()

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, [], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.db.session.commit.assert_called_once()

    def test_dry_run_never_commits(self, app_db):
        user_id = _make_user(app_db, "px8")
        _make_host(app_db, user_id, "cs-px8", mia=True)
        app_db.db = MagicMock()

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, ["--dry-run"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.db.session.commit.assert_not_called()

    def test_non_purgeable_host_is_skipped(self, app_db):
        """A host that is neither MIA nor quiescent is left untouched."""
        user_id = _make_user(app_db, "px9")
        _make_host(app_db, user_id, "cs-px9", mia=False)

        with patch("cspawn.cli.host.get_app", return_value=app_db):
            result = CliRunner().invoke(host_purge_cmd, [], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert result.output == ""
        app_db.csm.stop_host.assert_not_called()


# ---------------------------------------------------------------------------
# sys shutdown
# ---------------------------------------------------------------------------

class TestSysShutdown:
    def test_default_calls_remove_all_with_push_true(self, app_db):
        app_db.csm.remove_all.return_value = []

        with patch("cspawn.cli.sys.get_app", return_value=app_db):
            result = CliRunner().invoke(sys_shutdown_cmd, [], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.csm.remove_all.assert_called_once_with(push=True)

    def test_no_push_flag_calls_remove_all_with_push_false(self, app_db):
        app_db.csm.remove_all.return_value = []

        with patch("cspawn.cli.sys.get_app", return_value=app_db):
            result = CliRunner().invoke(sys_shutdown_cmd, ["--no-push"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        app_db.csm.remove_all.assert_called_once_with(push=False)


# ---------------------------------------------------------------------------
# test teardown
# ---------------------------------------------------------------------------

class TestTestTeardown:
    def test_stop_host_called_with_push_false_when_row_and_service_exist(self, app_db, monkeypatch):
        monkeypatch.setattr("cspawn.cli.test.N_STUDENTS", 1)
        user_id = _make_user(app_db, "teststudent01")
        _make_host(app_db, user_id, "teststudent01")

        # Capture the call details synchronously, while the CodeHost row is
        # still attached to a live session — teardown's own trailing
        # db.session.commit() expires the instance afterward.
        captured = []

        def _stop_host(ch, *, push=True, branch="master"):
            captured.append((ch.service_name, push))
            return _ok_result(ch.service_name, pushed=False)

        live_service = MagicMock()
        app_db.csm.get_by_username.return_value = live_service
        app_db.csm.stop_host.side_effect = _stop_host

        with patch("cspawn.cli.test.get_app", return_value=app_db), \
             patch("cspawn.cli.test.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(
                test_teardown_cmd, ["--keep-students"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert captured == [("teststudent01", False)]
        live_service.stop.assert_not_called()

    def test_orphan_service_with_no_db_row_falls_back_to_direct_stop(self, app_db, monkeypatch):
        monkeypatch.setattr("cspawn.cli.test.N_STUDENTS", 1)
        # No CodeHost row created for teststudent01.
        live_service = MagicMock()
        app_db.csm.get_by_username.return_value = live_service

        with patch("cspawn.cli.test.get_app", return_value=app_db), \
             patch("cspawn.cli.test.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(
                test_teardown_cmd, ["--keep-students"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_not_called()
        live_service.stop.assert_called_once()

    def test_dry_run_output_would_stop_service_unchanged(self, app_db, monkeypatch):
        monkeypatch.setattr("cspawn.cli.test.N_STUDENTS", 1)
        user_id = _make_user(app_db, "teststudent01")
        _make_host(app_db, user_id, "teststudent01")
        app_db.csm.get_by_username.return_value = MagicMock()

        with patch("cspawn.cli.test.get_app", return_value=app_db), \
             patch("cspawn.cli.test.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(
                test_teardown_cmd, ["--dry-run", "--keep-students"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert "would stop service teststudent01" in result.output
        app_db.csm.stop_host.assert_not_called()

    def test_db_only_row_deleted_directly_without_stop_host(self, app_db, monkeypatch):
        monkeypatch.setattr("cspawn.cli.test.N_STUDENTS", 1)
        user_id = _make_user(app_db, "teststudent01")
        _make_host(app_db, user_id, "teststudent01")
        app_db.csm.get_by_username.return_value = None  # no live service

        with patch("cspawn.cli.test.get_app", return_value=app_db), \
             patch("cspawn.cli.test.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(
                test_teardown_cmd, ["--keep-students"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_not_called()
        with app_db.app_context():
            assert CodeHost.query.filter_by(service_name="teststudent01").first() is None

    def test_never_pushes_regardless_of_state(self, app_db, monkeypatch):
        """push=False is always passed to stop_host() — teardown has no
        --no-push flag of its own; test-student work is never pushed."""
        monkeypatch.setattr("cspawn.cli.test.N_STUDENTS", 1)
        user_id = _make_user(app_db, "teststudent01")
        _make_host(app_db, user_id, "teststudent01")
        app_db.csm.get_by_username.return_value = MagicMock()
        app_db.csm.stop_host.return_value = _ok_result("teststudent01", pushed=False)

        with patch("cspawn.cli.test.get_app", return_value=app_db), \
             patch("cspawn.cli.test.get_logger", return_value=MagicMock()):
            result = CliRunner().invoke(
                test_teardown_cmd, ["--keep-students"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        app_db.csm.stop_host.assert_called_once()
        _, kwargs = app_db.csm.stop_host.call_args
        assert kwargs["push"] is False
