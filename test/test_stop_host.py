"""
Unit tests for the push-on-stop orchestrator:

    cspawn/cs_docker/csmanager.py::CodeServerManager.stop_host / remove_all
    cspawn/cs_github/repo.py::CodeHostRepo.push / _get_service_container

No live Docker, GitHub, or network access in any test here. Follows the
in-memory-SQLite + MagicMock pattern established in
test/test_autoscale.py::TestApplyReaperZones.

Run with::

    uv run pytest test/test_stop_host.py -v
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cspawn.cs_docker.csmanager import CodeServerManager, StopResult
from cspawn.cs_github.repo import CodeHostRepo


# ---------------------------------------------------------------------------
# Shared fixtures — in-memory SQLite Flask app + CodeHost/User rows
# ---------------------------------------------------------------------------

def _make_flask_app():
    """Create a minimal in-memory Flask app wired to cspawn models."""
    from flask import Flask
    from cspawn.models import db as _db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-stop-host-secret"
    app.config["TESTING"] = True
    app.app_config = {
        "GITHUB_TOKEN": "test-token",
        "NODE_HOSTNAME_TEMPLATE": "{nodename}.example.com",
        "CODEHOST_PUSH_TIMEOUT_S": 30,
    }

    _db.init_app(app)

    with app.app_context():
        _db.create_all()

    return app, _db


def _make_manager(app):
    """Build a CodeServerManager instance without touching Docker.

    CodeServerManager.__init__ connects to a real Docker daemon (unavailable
    in unit tests). stop_host()/remove_all() only depend on self.app and
    self.get() (which we stub per-test), so we bypass __init__ entirely.
    """
    csm = CodeServerManager.__new__(CodeServerManager)
    csm.app = app
    csm.config = app.app_config
    return csm


_host_counter = 0


def _make_user_and_host(app, db, *, is_mia=False):
    """Create one User + CodeHost row. Returns (user_id, host_id)."""
    global _host_counter
    from cspawn.models import CodeHost, User

    _host_counter += 1
    suffix = _host_counter

    with app.app_context():
        user = User(
            user_id=f"uid-stophost{suffix}",
            email=f"stophost{suffix}@example.com",
            username=f"stophost{suffix}",
            is_active=True,
        )
        db.session.add(user)
        db.session.flush()

        host = CodeHost(
            user_id=user.id,
            service_id=f"svc-stophost-{suffix}",
            service_name=f"cs-stophost{suffix}",
            app_state="mia" if is_mia else "ready",
            state="mia" if is_mia else "running",
        )
        db.session.add(host)
        db.session.commit()

        return user.id, host.id


# ---------------------------------------------------------------------------
# CodeServerManager.stop_host
# ---------------------------------------------------------------------------

class TestStopHost:
    def test_push_succeeds_stops_and_deletes(self):
        """Happy path: push, stop, and delete all succeed."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            mock_service = MagicMock()
            csm.get = MagicMock(return_value=mock_service)

            with patch.object(CodeHostRepo, "push", return_value=0) as mock_push:
                result = csm.stop_host(host)

            mock_push.assert_called_once()
            mock_service.stop.assert_called_once()

            assert isinstance(result, StopResult)
            assert result.pushed is True
            assert result.push_error is None
            assert result.stopped is True
            assert result.stop_error is None
            assert result.deleted is True
            assert result.skipped_push_mia is False

            assert CodeHost.query.get(host_id) is None

    def test_push_failure_does_not_block_stop_or_delete(self, caplog):
        """A mocked push failure still lets stop + delete proceed."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            mock_service = MagicMock()
            csm.get = MagicMock(return_value=mock_service)

            with patch.object(CodeHostRepo, "push", side_effect=RuntimeError("git push failed")):
                with caplog.at_level("ERROR", logger="cspawn.docker"):
                    result = csm.stop_host(host)

            assert result.pushed is False
            assert result.push_error == "git push failed"
            assert result.stopped is True
            assert result.deleted is True
            mock_service.stop.assert_called_once()

            # ERROR-level log line emitted on the cspawn.docker logger.
            assert any(
                rec.levelname == "ERROR" and rec.name == "cspawn.docker"
                for rec in caplog.records
            )

            assert CodeHost.query.get(host_id) is None

    def test_push_times_out_recorded_as_push_error(self):
        """A TimeoutExpired surfacing from CodeHostRepo.push (as RuntimeError)
        is treated the same as any other push failure."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            csm.get = MagicMock(return_value=MagicMock())

            timeout_err = RuntimeError("git push timed out after 30s for stophost1 on swarm2")
            with patch.object(CodeHostRepo, "push", side_effect=timeout_err):
                result = csm.stop_host(host)

            assert result.pushed is False
            assert "timed out" in result.push_error
            assert result.stopped is True
            assert result.deleted is True

    def test_mia_host_skips_push_entirely(self, caplog):
        """is_mia=True: CodeHostRepo.push is never invoked; INFO (not ERROR) log."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db, is_mia=True)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host.is_mia is True
            csm.get = MagicMock(return_value=MagicMock())

            with patch.object(CodeHostRepo, "push") as mock_push:
                with caplog.at_level("INFO", logger="cspawn.docker"):
                    result = csm.stop_host(host)

            mock_push.assert_not_called()
            assert result.skipped_push_mia is True
            assert result.pushed is False
            assert result.push_error is None

            info_records = [
                rec for rec in caplog.records
                if rec.name == "cspawn.docker" and rec.levelname == "INFO"
            ]
            error_records = [
                rec for rec in caplog.records
                if rec.name == "cspawn.docker" and rec.levelname == "ERROR"
            ]
            assert len(info_records) >= 1
            assert len(error_records) == 0

            assert result.stopped is True
            assert result.deleted is True

    def test_push_false_never_calls_push_even_when_mia(self):
        """push=False must never call CodeHostRepo.push, MIA or not."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db, is_mia=False)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            csm.get = MagicMock(return_value=MagicMock())

            with patch.object(CodeHostRepo, "push") as mock_push:
                result = csm.stop_host(host, push=False)

            mock_push.assert_not_called()
            assert result.pushed is False
            assert result.skipped_push_mia is False
            assert result.stopped is True
            assert result.deleted is True

    def test_service_already_gone_counts_as_successful_stop(self):
        """self.get() returning None (service missing) is a successful stop."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            csm.get = MagicMock(return_value=None)

            with patch.object(CodeHostRepo, "push", return_value=0):
                result = csm.stop_host(host)

            assert result.stopped is True
            assert result.stop_error is None
            assert result.deleted is True

    def test_swarm_stop_failure_does_not_block_delete(self):
        """service.stop() raising is caught; delete still proceeds."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            mock_service = MagicMock()
            mock_service.stop.side_effect = RuntimeError("swarm boom")
            csm.get = MagicMock(return_value=mock_service)

            with patch.object(CodeHostRepo, "push", return_value=0):
                result = csm.stop_host(host)

            assert result.stopped is False
            assert result.stop_error == "swarm boom"
            assert result.deleted is True

            assert CodeHost.query.get(host_id) is None

    def test_db_delete_failure_rolls_back(self):
        """db.session.delete raising is caught; deleted=False, row still present."""
        from cspawn.models import CodeHost, db as _db

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            csm.get = MagicMock(return_value=None)

            with patch.object(CodeHostRepo, "push", return_value=0):
                with patch.object(_db.session, "delete", side_effect=RuntimeError("db down")):
                    result = csm.stop_host(host)

            assert result.deleted is False
            # Row must still exist since delete failed and was rolled back.
            assert CodeHost.query.get(host_id) is not None

    def test_stop_host_never_raises_when_every_step_fails(self):
        """All three steps failing must still return a StopResult, not raise."""
        from cspawn.models import CodeHost, db as _db

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            mock_service = MagicMock()
            mock_service.stop.side_effect = RuntimeError("stop boom")
            csm.get = MagicMock(return_value=mock_service)

            with patch.object(CodeHostRepo, "push", side_effect=RuntimeError("push boom")):
                with patch.object(_db.session, "delete", side_effect=RuntimeError("delete boom")):
                    result = csm.stop_host(host)

            assert result.push_error == "push boom"
            assert result.stop_error == "stop boom"
            assert result.deleted is False


# ---------------------------------------------------------------------------
# CodeServerManager.remove_all
# ---------------------------------------------------------------------------

class TestRemoveAll:
    def test_remove_all_calls_stop_host_once_per_row(self):
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _make_user_and_host(app, db)
        _make_user_and_host(app, db)
        _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            csm.get = MagicMock(return_value=None)

            with patch.object(CodeHostRepo, "push", return_value=0):
                with patch.object(csm, "stop_host", wraps=csm.stop_host) as spy:
                    results = csm.remove_all()

            assert spy.call_count == 3
            assert len(results) == 3
            assert all(isinstance(r, StopResult) for r in results)
            assert CodeHost.query.count() == 0

    def test_remove_all_isolates_per_host_push_failures(self):
        """One host's push failure must not affect the others' stop/delete."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host1 = _make_user_and_host(app, db)
        _, host2 = _make_user_and_host(app, db)
        _, host3 = _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            csm.get = MagicMock(return_value=None)

            def push_side_effect(self_repo, branch="master"):
                if self_repo.codehost.id == host2:
                    raise RuntimeError("push boom for host2")
                return 0

            with patch.object(CodeHostRepo, "push", autospec=True, side_effect=push_side_effect):
                results = csm.remove_all()

            assert len(results) == 3
            failing = [r for r in results if r.push_error]
            ok = [r for r in results if not r.push_error]
            assert len(failing) == 1
            assert len(ok) == 2

            # Despite the push failure, every host was still stopped+deleted.
            assert all(r.stopped for r in results)
            assert all(r.deleted for r in results)
            assert CodeHost.query.count() == 0

    def test_remove_all_push_false_forwarded_to_every_host(self):
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _make_user_and_host(app, db)
        _make_user_and_host(app, db)
        csm = _make_manager(app)

        with app.app_context():
            csm.get = MagicMock(return_value=None)

            with patch.object(CodeHostRepo, "push") as mock_push:
                results = csm.remove_all(push=False)

            mock_push.assert_not_called()
            assert all(not r.pushed for r in results)
            assert CodeHost.query.count() == 0

    def test_remove_all_empty_db_returns_empty_list(self):
        app, db = _make_flask_app()
        csm = _make_manager(app)

        with app.app_context():
            results = csm.remove_all()

        assert results == []


# ---------------------------------------------------------------------------
# CodeHostRepo.push — timeout hardening
# ---------------------------------------------------------------------------

def _make_repo_with_mock_service(app):
    """Build a CodeHostRepo backed by a mocked app.csm.get() service/container."""
    from cspawn.models import CodeHost

    mock_container = MagicMock()
    mock_container.id = "container123"
    mock_container.node.attrs = {"Description": {"Hostname": "swarm2"}}

    mock_service = MagicMock()
    mock_service.containers = [mock_container]
    mock_service.env = {"JTL_REPO": "org/repo"}

    app.csm = MagicMock()
    app.csm.get.return_value = mock_service

    host = CodeHost(service_id="svc-x", service_name="cs-x")
    return CodeHostRepo(host, app), mock_service, mock_container


class TestCodeHostRepoPush:
    def test_push_ok_passes_timeout_to_subprocess_run(self):
        app, db = _make_flask_app()
        with app.app_context():
            repo, _, _ = _make_repo_with_mock_service(app)

            fake_proc = MagicMock(returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                rc = repo.push()

            assert rc == 0
            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 30  # from CODEHOST_PUSH_TIMEOUT_S in app_config

    def test_push_default_timeout_is_30_when_unconfigured(self):
        app, db = _make_flask_app()
        app.app_config = {
            "GITHUB_TOKEN": "tok",
            "NODE_HOSTNAME_TEMPLATE": "{nodename}.example.com",
            # No CODEHOST_PUSH_TIMEOUT_S key at all.
        }
        with app.app_context():
            repo, _, _ = _make_repo_with_mock_service(app)

            fake_proc = MagicMock(returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                repo.push()

            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 30

    def test_push_explicit_timeout_overrides_config(self):
        app, db = _make_flask_app()
        with app.app_context():
            repo, _, _ = _make_repo_with_mock_service(app)

            fake_proc = MagicMock(returncode=0, stdout="", stderr="")
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                repo.push(timeout=5)

            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 5

    def test_push_raises_runtime_error_on_nonzero_returncode(self):
        app, db = _make_flask_app()
        with app.app_context():
            repo, _, _ = _make_repo_with_mock_service(app)

            fake_proc = MagicMock(returncode=1, stdout="", stderr="git push rejected")
            with patch("subprocess.run", return_value=fake_proc):
                with pytest.raises(RuntimeError, match="git push failed"):
                    repo.push()

    def test_push_timeout_expired_reraised_as_runtime_error(self):
        """subprocess.TimeoutExpired is caught and re-raised as RuntimeError,
        naming the host and the timeout. No real sleeping/hanging occurs
        because subprocess.run itself is mocked to raise immediately."""
        app, db = _make_flask_app()
        with app.app_context():
            repo, _, _ = _make_repo_with_mock_service(app)

            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="docker exec ...", timeout=30),
            ):
                with pytest.raises(RuntimeError, match="timed out"):
                    repo.push()

    def test_push_timeout_expired_message_names_timeout_value(self):
        app, db = _make_flask_app()
        app.app_config["CODEHOST_PUSH_TIMEOUT_S"] = 7
        with app.app_context():
            repo, _, _ = _make_repo_with_mock_service(app)

            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="docker exec ...", timeout=7),
            ):
                with pytest.raises(RuntimeError, match="7s"):
                    repo.push()


# ---------------------------------------------------------------------------
# CodeHostRepo._get_service_container — ValueError on missing service
# ---------------------------------------------------------------------------

class TestGetServiceContainer:
    def test_raises_value_error_when_service_missing(self):
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        with app.app_context():
            app.csm = MagicMock()
            app.csm.get.return_value = None

            host = CodeHost(service_id="svc-missing", service_name="cs-missing")
            repo = CodeHostRepo(host, app)

            with pytest.raises(ValueError, match="No service found for cs-missing"):
                repo._get_service_container()

    def test_push_surfaces_value_error_when_service_missing(self):
        """push() itself surfaces the ValueError (not an AttributeError) when
        the target Swarm service is already gone."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        with app.app_context():
            app.csm = MagicMock()
            app.csm.get.return_value = None

            host = CodeHost(service_id="svc-missing", service_name="cs-missing")
            repo = CodeHostRepo(host, app)

            with pytest.raises(ValueError):
                repo.push()
