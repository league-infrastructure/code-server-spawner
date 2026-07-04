"""
Regression tests for ticket 008-002: propagate `Service.first_container()`
(added in ticket 001) to every remaining container-resolution call site, and
prove the stale-node -> MIA -> purge/reap repair path composes with sprint
007's `stop_host()`/`purge` machinery end to end.

Covers:
- `CodeHostRepo._get_service_container()` / `.pull()` / `StudentRepo.
  _get_service_and_container()` / `HostS3Sync.get_service_and_container()`
  each propagate `Service.first_container()`'s stale-node `ValueError`
  unchanged (never a raw `docker.errors.NotFound`).
- `CodeHostRepo.pull()` no longer raises `AttributeError` (pre-existing
  `self._get_container()` bug, fixed as part of this ticket) and reaches
  the same container-resolution path as `push()`.
- `CodeHostRepo.push()` on a stale-node service raises a clean `ValueError`,
  and `CodeServerManager.stop_host()` (sprint 007, unmodified) still stops
  and deletes the host with `push_error` populated from that specific
  exception — proving composition, not just tolerance of a generic mock.
- A `CodeHost` row shaped exactly as ticket 001's `to_model()` would produce
  for a stale-node host (`state=mia`, `app_state=mia`) is picked up by
  `host purge`'s existing `is_mia`-or-`is_quiescent` filter with zero
  filter changes, and `stop_host()` skips its push
  (`skipped_push_mia=True`) while still removing the service and deleting
  the row.
- `host cont`: guards a missing service, resolves a container via
  `first_container()`, and prints a clean message (not a traceback) for a
  stale-node service, distinct from the "service not found" message.

No live Docker, GitHub, or network access anywhere in this file —
everything is mocked.

Run with::

    uv run pytest test/test_stale_node_consumers.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from docker.errors import NotFound as DockerNotFound

from cspawn.cli.host import cont as host_cont_cmd
from cspawn.cli.host import purge as host_purge_cmd
from cspawn.cs_docker.csmanager import CodeServerManager, StopResult
from cspawn.cs_github.repo import CodeHostRepo, StudentRepo
from cspawn.util.host_s3_sync import HostS3Sync


STALE_NODE_MESSAGE = (
    "No container found for service {name}: its task's node no longer "
    "exists in the Swarm (likely destroyed by the autoscaler)"
)


def _stale_service(name="cs-x"):
    """A MagicMock service whose first_container() raises the same
    stale-node ValueError that cspawn.cs_docker.proc.Service.first_container()
    raises when node_missing is True (ticket 001)."""
    service = MagicMock()
    service.name = name
    service.first_container.side_effect = ValueError(STALE_NODE_MESSAGE.format(name=name))
    return service


# ---------------------------------------------------------------------------
# Flask/SQLite fixtures — same pattern as test_stop_host.py
# ---------------------------------------------------------------------------

def _make_flask_app():
    """Create a minimal in-memory Flask app wired to cspawn models."""
    from flask import Flask
    from cspawn.models import db as _db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-stale-node-consumers-secret"
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
    """Build a CodeServerManager instance without touching Docker (same
    trick as test_stop_host.py::_make_manager)."""
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
            user_id=f"uid-stalenode{suffix}",
            email=f"stalenode{suffix}@example.com",
            username=f"stalenode{suffix}",
            is_active=True,
        )
        db.session.add(user)
        db.session.flush()

        host = CodeHost(
            user_id=user.id,
            service_id=f"svc-stalenode-{suffix}",
            service_name=f"cs-stalenode{suffix}",
            app_state="mia" if is_mia else "ready",
            state="mia" if is_mia else "running",
        )
        db.session.add(host)
        db.session.commit()

        return user.id, host.id


# ---------------------------------------------------------------------------
# CodeHostRepo._get_service_container() / pull() / push() — delegate to
# Service.first_container()
# ---------------------------------------------------------------------------

class TestCodeHostRepoConsumers:
    def test_get_service_container_propagates_stale_node_value_error(self):
        app, db = _make_flask_app()
        with app.app_context():
            from cspawn.models import CodeHost

            app.csm = MagicMock()
            app.csm.get.return_value = _stale_service("cs-x")

            host = CodeHost(service_id="svc-x", service_name="cs-x")
            repo = CodeHostRepo(host, app)

            with pytest.raises(ValueError, match="node no longer exists"):
                repo._get_service_container()

    def test_pull_no_longer_raises_attribute_error(self):
        """Pre-existing bug: pull() called the nonexistent self._get_container().
        It must now resolve a container the same way push() does, via
        _get_service_container() -> service.first_container()."""
        app, db = _make_flask_app()
        with app.app_context():
            from cspawn.models import CodeHost

            mock_container = MagicMock()
            mock_container.id = "container123456"

            mock_service = MagicMock()
            mock_service.first_container.return_value = mock_container

            app.csm = MagicMock()
            app.csm.get.return_value = mock_service

            host = CodeHost(service_id="svc-pull", service_name="cs-pull")
            repo = CodeHostRepo(host, app)

            # dry_run=True short-circuits before touching container.o.exec_run,
            # proving container resolution itself succeeded (no AttributeError).
            rc = repo.pull(dry_run=True)

            assert rc == 0
            mock_service.first_container.assert_called_once()

    def test_pull_propagates_stale_node_value_error(self):
        app, db = _make_flask_app()
        with app.app_context():
            from cspawn.models import CodeHost

            app.csm = MagicMock()
            app.csm.get.return_value = _stale_service("cs-pull-stale")

            host = CodeHost(service_id="svc-pull-stale", service_name="cs-pull-stale")
            repo = CodeHostRepo(host, app)

            with pytest.raises(ValueError, match="node no longer exists"):
                repo.pull()

    def test_push_raises_value_error_not_raw_notfound_on_stale_node(self):
        """The ticket's central assertion: push() surfaces a clean ValueError,
        never docker.errors.NotFound, when the service's task's node is gone."""
        app, db = _make_flask_app()
        with app.app_context():
            from cspawn.models import CodeHost

            app.csm = MagicMock()
            app.csm.get.return_value = _stale_service("cs-push-stale")

            host = CodeHost(service_id="svc-push-stale", service_name="cs-push-stale")
            repo = CodeHostRepo(host, app)

            with pytest.raises(ValueError, match="node no longer exists") as excinfo:
                repo.push()

            assert not isinstance(excinfo.value, DockerNotFound)


class TestStudentRepoConsumer:
    def test_get_service_and_container_propagates_stale_node_value_error(self):
        app = MagicMock()
        app.csm = MagicMock()
        app.csm.get_by_username.return_value = _stale_service("student-svc")

        repo = StudentRepo(
            config=None,
            app=app,
            org="org",
            name="repo-student",
            upstream_name="upstream",
            upstream_url="https://github.com/org/upstream",
            username="student1",
        )

        with pytest.raises(ValueError, match="node no longer exists"):
            repo._get_service_and_container()


class TestHostS3SyncConsumer:
    def test_get_service_and_container_propagates_stale_node_value_error(self):
        app = MagicMock()
        app.app_config = {
            "STORAGE_ENDPOINT": "https://s3.example.com",
            "STORAGE_ACCESS_KEY": "key",
            "STORAGE_SECRET": "secret",
            "STORAGE_BUCKET": "bucket",
        }
        app.csm = MagicMock()
        app.csm.get_by_username.return_value = _stale_service("s3-svc")

        syncer = HostS3Sync(app)

        with pytest.raises(ValueError, match="node no longer exists"):
            syncer.get_service_and_container("student1")


# ---------------------------------------------------------------------------
# Composition with sprint 007: push failure -> stop_host() still stops+deletes
# ---------------------------------------------------------------------------

class TestStopHostComposesWithStaleNode:
    def test_stale_node_push_error_does_not_block_stop_or_delete(self):
        """Real (unmodified) stop_host() drives a real (unmodified) push()
        into a service whose first_container() raises the stale-node
        ValueError. Must surface as push_error, never crash the batch."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db, is_mia=False)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            stale = _stale_service(host.service_name)

            # app.csm.get(...) backs CodeHostRepo._get_service_container()
            # inside the real push(); csm.get(...) backs the separate
            # "stop the live service" step inside stop_host() itself.
            app.csm = MagicMock()
            app.csm.get.return_value = stale
            csm.get = MagicMock(return_value=stale)

            result = csm.stop_host(host)

            assert result.pushed is False
            assert result.push_error is not None
            assert "node no longer exists" in result.push_error
            assert result.stopped is True
            assert result.deleted is True
            stale.stop.assert_called_once()

            assert CodeHost.query.get(host_id) is None

    def test_mia_stale_node_row_skips_push_and_still_removes_everything(self):
        """A CodeHost row shaped exactly as ticket 001's to_model() produces
        for a stale-node host (state=mia, app_state=mia): stop_host() must
        skip the push entirely and still stop + delete."""
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        _, host_id = _make_user_and_host(app, db, is_mia=True)
        csm = _make_manager(app)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            # Sanity: this is exactly the shape host purge's existing
            # `is_mia or is_quiescent` filter selects, unchanged by this ticket.
            assert host.is_mia is True

            stale = _stale_service(host.service_name)
            app.csm = MagicMock()
            app.csm.get.return_value = stale
            csm.get = MagicMock(return_value=stale)

            with patch.object(CodeHostRepo, "push") as mock_push:
                result = csm.stop_host(host)

            mock_push.assert_not_called()
            assert result.skipped_push_mia is True
            assert result.pushed is False
            assert result.push_error is None
            assert result.stopped is True
            assert result.deleted is True

            assert CodeHost.query.get(host_id) is None


# ---------------------------------------------------------------------------
# host purge — unmodified is_mia/is_quiescent filter picks up the row
# ---------------------------------------------------------------------------

class TestHostPurgeSelectsStaleNodeMiaRow:
    def test_purge_selects_row_shaped_like_to_model_mia_output(self):
        """No changes to cli/host.py's purge command: its existing
        `ch.is_mia or ch.is_quiescent` filter must select a row shaped
        exactly as ticket 001's to_model() would leave it for a stale-node
        host, and forward it to stop_host() unchanged."""
        from flask import Flask
        from cspawn.models import CodeHost, User, db as _db

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        _db.init_app(app)

        with app.app_context():
            _db.create_all()
            app.csm = MagicMock()
            app.db = _db

            user = User(user_id="uid-purge-stale", username="purge-stale", is_active=True)
            _db.session.add(user)
            _db.session.flush()

            host = CodeHost(
                user_id=user.id,
                service_id="svc-purge-stale",
                service_name="cs-purge-stale",
                state="mia",
                app_state="mia",
            )
            _db.session.add(host)
            _db.session.commit()

            assert host.is_mia is True

            # Capture the call details synchronously, while the CodeHost row
            # is still attached to a live session — purge()'s own trailing
            # db.session.commit() expires the instance afterward (same
            # caveat documented in test_cli_stop_paths.py::TestHostPurge).
            captured = []

            def _stop_host(ch, *, push=True, branch="master"):
                captured.append(ch.service_name)
                return StopResult(
                    service_name=ch.service_name,
                    skipped_push_mia=True,
                    stopped=True,
                    deleted=True,
                )

            app.csm.stop_host.side_effect = _stop_host

            with patch("cspawn.cli.host.get_app", return_value=app):
                result = CliRunner().invoke(host_purge_cmd, [], catch_exceptions=False)

            assert result.exit_code == 0, result.output
            assert captured == ["cs-purge-stale"]
            # Push was never actually attempted for this MIA row.
            assert "(pushed)" not in result.output
            assert "Stopped and deleted:   cs-purge-stale" in result.output

            _db.session.remove()
            _db.drop_all()


# ---------------------------------------------------------------------------
# host cont — s is None guard, first_container() delegation, ValueError branch
# ---------------------------------------------------------------------------

class TestHostContCommand:
    def test_missing_service_prints_not_found_message(self):
        app = MagicMock()
        app.csm.get.return_value = None

        with patch("cspawn.cli.host.get_app", return_value=app):
            result = CliRunner().invoke(host_cont_cmd, ["missing-svc"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "Service missing-svc not found" in result.output

    def test_stale_node_service_prints_clean_message_not_traceback(self):
        app = MagicMock()
        stale = _stale_service("stale-svc")
        stale.containers_info.return_value = iter(
            [{"container_id": "c1", "node_id": "node-dead"}]
        )
        app.csm.get.return_value = stale

        with patch("cspawn.cli.host.get_app", return_value=app):
            result = CliRunner().invoke(host_cont_cmd, ["stale-svc"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "Cannot resolve container for stale-svc" in result.output
        # Distinct from the existing "service not found" message.
        assert "not found" not in result.output
