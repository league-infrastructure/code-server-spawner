"""Tests for ticket 006-002: _ensure_priv_key fallback and node op-run CLI worker.

Covers:
- _ensure_priv_key: primary path returned when config/secrets/id_rsa exists.
- _ensure_priv_key: fallback to ~/.ssh/id_rsa when primary absent.
- _ensure_priv_key: raises ClickException naming both paths when neither exists.
- op-run --help: command is registered and accepts op_id argument.
- op-run expand lifecycle: pending → running → done; log_path created; timestamps set.
- op-run remove lifecycle: same lifecycle with kind='remove'.
- op-run failure path: ctx.invoke raises → status='failed', message populated.
- op-run flock serialization: second invocation fails immediately when lock held.

Also covers sprint-010 ticket-004 (record created droplet id/fqdn on its
triggering NodeOp):
- op-run threading: kind='expand' passes node_op_id=op_id into
  ctx.invoke(expand, ...), wrapped in an app context.
- _create_droplet: node_op_id set + matching NodeOp row -> droplet_id/
  target_fqdn recorded and committed.
- _create_droplet: node_op_id=None (every existing caller) -> complete no-op.
- _create_droplet: missing NodeOp row / DB write failure -> swallowed,
  creation still returns its normal tuple.

No live Docker, DigitalOcean, or real database I/O in any test here.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click import ClickException
from click.testing import CliRunner
from flask import Flask

from cspawn.cli.node import _create_droplet, _ensure_priv_key, op_run
from cspawn.models import NodeOp, db


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_db():
    """Fresh in-memory SQLite Flask app with all tables created."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app, db
        db.session.remove()
        db.drop_all()


def _make_op(app_db_pair, kind: str = "expand", tier: str | None = "large",
             target_fqdn: str | None = None) -> str:
    """Create a pending NodeOp in the DB and return its id."""
    app, _db = app_db_pair
    with app.app_context():
        op = NodeOp(kind=kind, tier=tier, target_fqdn=target_fqdn, status="pending")
        _db.session.add(op)
        _db.session.commit()
        return op.id


def _invoke_create_droplet(app, *, node_op_id: str | None = None, droplet_id: int = 4242,
                            patch_commit_raises: bool = False):
    """Call `_create_droplet` with the DigitalOcean/SSH/network surface mocked
    out (follows `test_node_cloud_init.py`'s conventions), inside `app`'s app
    context so the optional node_op_id DB write-back (ticket 004) can run.

    Returns ((droplet, ip, fqdn, shortname), mock_droplet_instance).
    """
    mgr = MagicMock()
    mgr.get_all_droplets.return_value = []
    manager_client = MagicMock()

    mock_instance = MagicMock()
    mock_instance.id = droplet_id
    droplet_cls = MagicMock(return_value=mock_instance)

    with contextlib.ExitStack() as stack:
        stack.enter_context(app.app_context())
        stack.enter_context(patch("cspawn.cli.node.get_config", return_value={}))
        stack.enter_context(patch("cspawn.cli.node.get_logger", return_value=MagicMock()))
        stack.enter_context(patch(
            "cspawn.cli.node._ensure_priv_key",
            return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")),
        ))
        stack.enter_context(patch("cspawn.cli.node._collect_do_ssh_keys", return_value=[]))
        stack.enter_context(patch("cspawn.cli.node.digitalocean.Droplet", droplet_cls))
        stack.enter_context(patch("cspawn.cli.node._wait_for_droplet_active", return_value="10.0.0.5"))
        stack.enter_context(patch("cspawn.cli.node._find_manager_droplet", return_value=None))
        if patch_commit_raises:
            stack.enter_context(patch.object(db.session, "commit", side_effect=RuntimeError("simulated DB failure")))

        result = _create_droplet(
            ctx=MagicMock(),
            mgr=mgr,
            manager_client=manager_client,
            name_template="swarm{serial}.example.com",
            do_token="fake-token",
            do_region="nyc3",
            do_size="s-1vcpu-2gb",
            do_image="docker-20-04",
            project_selector=None,
            desired_serial=5,
            docker_uri="ssh://root@manager.example.com",
            do_tag=None,
            tier=None,
            node_op_id=node_op_id,
        )

    return result, mock_instance


# ---------------------------------------------------------------------------
# _ensure_priv_key tests
# ---------------------------------------------------------------------------


class TestEnsurePrivKey:
    def test_primary_path_returned_when_present(self, tmp_path, monkeypatch):
        """When config/secrets/id_rsa exists, it is returned as the primary path."""
        # Create primary key files
        secrets_dir = tmp_path / "config" / "secrets"
        secrets_dir.mkdir(parents=True)
        priv_key = secrets_dir / "id_rsa"
        priv_key.write_text("FAKE_PRIVATE_KEY")
        pub_key = secrets_dir / "id_rsa.pub"
        pub_key.write_text("FAKE_PUBLIC_KEY")

        monkeypatch.setattr(
            "cspawn.cli.node.find_parent_dir",
            lambda: str(tmp_path),
        )

        result_priv, result_pub = _ensure_priv_key()
        assert result_priv == priv_key
        assert result_pub == pub_key

    def test_fallback_ssh_returned_when_primary_absent(self, tmp_path, monkeypatch):
        """When config/secrets/id_rsa is absent, ~/.ssh/id_rsa is returned."""
        # Primary secrets dir does NOT contain id_rsa
        secrets_dir = tmp_path / "config" / "secrets"
        secrets_dir.mkdir(parents=True)
        # primary key NOT created

        # Create fallback SSH key files in a fake home dir
        fake_home = tmp_path / "home"
        ssh_dir = fake_home / ".ssh"
        ssh_dir.mkdir(parents=True)
        fallback_priv = ssh_dir / "id_rsa"
        fallback_priv.write_text("FAKE_FALLBACK_PRIVATE_KEY")
        fallback_pub = ssh_dir / "id_rsa.pub"
        fallback_pub.write_text("FAKE_FALLBACK_PUBLIC_KEY")

        monkeypatch.setattr(
            "cspawn.cli.node.find_parent_dir",
            lambda: str(tmp_path),
        )
        monkeypatch.setattr(
            "cspawn.cli.node.Path.home",
            staticmethod(lambda: fake_home),
        )

        result_priv, result_pub = _ensure_priv_key()
        assert result_priv == fallback_priv
        assert result_pub == fallback_priv.with_suffix(".pub")

    def test_raises_when_both_absent(self, tmp_path, monkeypatch):
        """Raises ClickException naming both paths when neither key exists."""
        # Neither primary nor fallback key exists
        secrets_dir = tmp_path / "config" / "secrets"
        secrets_dir.mkdir(parents=True)
        fake_home = tmp_path / "empty_home"
        fake_home.mkdir()

        monkeypatch.setattr(
            "cspawn.cli.node.find_parent_dir",
            lambda: str(tmp_path),
        )
        monkeypatch.setattr(
            "cspawn.cli.node.Path.home",
            staticmethod(lambda: fake_home),
        )

        with pytest.raises(ClickException) as exc_info:
            _ensure_priv_key()

        msg = exc_info.value.format_message()
        # Should name both paths
        assert "config" in msg and "id_rsa" in msg
        assert ".ssh" in msg or "id_rsa" in msg

    def test_pub_key_path_derived_from_priv_key_fallback(self, tmp_path, monkeypatch):
        """Fallback: pub key path is derived as <priv>.pub even if .pub is absent."""
        secrets_dir = tmp_path / "config" / "secrets"
        secrets_dir.mkdir(parents=True)

        fake_home = tmp_path / "home2"
        ssh_dir = fake_home / ".ssh"
        ssh_dir.mkdir(parents=True)
        fallback_priv = ssh_dir / "id_rsa"
        fallback_priv.write_text("FAKE")
        # .pub deliberately NOT created

        monkeypatch.setattr(
            "cspawn.cli.node.find_parent_dir",
            lambda: str(tmp_path),
        )
        monkeypatch.setattr(
            "cspawn.cli.node.Path.home",
            staticmethod(lambda: fake_home),
        )

        result_priv, result_pub = _ensure_priv_key()
        assert result_priv == fallback_priv
        assert result_pub == fallback_priv.with_suffix(".pub")
        # Pub key doesn't exist — caller must check
        assert not result_pub.exists()


# ---------------------------------------------------------------------------
# op-run help test
# ---------------------------------------------------------------------------


class TestOpRunHelp:
    def test_help_shows_command_and_argument(self):
        """--help exits 0 and mentions op_id argument."""
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(op_run, ["--help"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        # Must show the argument name
        assert "OP_ID" in result.output.upper() or "op_id" in result.output.lower()


# ---------------------------------------------------------------------------
# Helpers for op-run lifecycle tests
# ---------------------------------------------------------------------------

def _make_noop_command(name: str) -> click.Command:
    """Return a Click Command that does nothing (for mocking expand/stop_node)."""
    @click.command(name=name)
    @click.pass_context
    def _cmd(ctx, **kwargs):
        pass
    return _cmd


def _run_op(op_id: str, app, tmp_path, expand_cmd=None, stop_node_cmd=None, rebalance_cmd=None):
    """Invoke op_run with infrastructure mocked.

    Patches:
    - cspawn.cli.util.get_app → returns the provided Flask app
    - cspawn.cli.node.get_config → returns {DATA_DIR: str(tmp_path)}
    - cspawn.cli.node.expand → expand_cmd (default: noop command)
    - cspawn.cli.node.stop_node → stop_node_cmd (default: noop command)
    - cspawn.cli.node.rebalance → rebalance_cmd (default: noop command)
    """
    if expand_cmd is None:
        expand_cmd = _make_noop_command("expand")
    if stop_node_cmd is None:
        stop_node_cmd = _make_noop_command("stop")
    if rebalance_cmd is None:
        rebalance_cmd = _make_noop_command("rebalance")

    with patch("cspawn.cli.util.get_app", return_value=app), \
         patch("cspawn.cli.node.get_config", return_value={"DATA_DIR": str(tmp_path)}), \
         patch("cspawn.cli.node.expand", expand_cmd), \
         patch("cspawn.cli.node.stop_node", stop_node_cmd), \
         patch("cspawn.cli.node.rebalance", rebalance_cmd):
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(
            op_run,
            [op_id],
            obj={"v": 0, "deploy": "devel"},
            catch_exceptions=False,
        )
    return result


# ---------------------------------------------------------------------------
# op-run lifecycle tests
# ---------------------------------------------------------------------------


class TestOpRunExpand:
    def test_expand_lifecycle_pending_to_done(self, app_db, tmp_path):
        """kind='expand' op transitions pending→running→done; timestamps and log set."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        result = _run_op(op_id, app, tmp_path)
        assert result.exit_code == 0, result.output

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.status == "done", f"Expected done, got {op.status!r}"
            assert op.exit_code == 0
            assert op.started_at is not None
            assert op.finished_at is not None
            assert op.log_path is not None
            # Log file must exist
            assert Path(op.log_path).exists(), f"Log file missing at {op.log_path}"

    def test_expand_invokes_expand_with_tier_and_node_op_id(self, app_db, tmp_path):
        """kind='expand' calls ctx.invoke(expand, tier_name=op.tier, node_op_id=op.id).

        node_op_id threading is ticket 004: op_run must pass the triggering
        NodeOp's id through so _create_droplet can record the created
        droplet's id/fqdn back onto this exact row.
        """
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        captured_kwargs: list[dict] = []

        @click.command(name="expand")
        @click.option("--tier", "tier_name", default=None)
        @click.pass_context
        def mock_expand(ctx, tier_name, node_op_id=None):
            captured_kwargs.append({"tier_name": tier_name, "node_op_id": node_op_id})

        result = _run_op(op_id, app, tmp_path, expand_cmd=mock_expand)
        assert result.exit_code == 0, result.output

        assert len(captured_kwargs) == 1, f"Expected one expand call, got {captured_kwargs}"
        assert captured_kwargs[0]["tier_name"] == "large"
        assert captured_kwargs[0]["node_op_id"] == op_id

    def test_expand_invocation_runs_inside_app_context(self, app_db, tmp_path):
        """kind='expand' invokes expand() inside an active app context.

        _create_droplet's node_op_id write-back needs `db.session` to be
        usable. If op_run's kind=='expand' branch weren't wrapped in `with
        app.app_context():`, a DB operation performed by the invoked command
        would raise "working outside of application context" — this test
        performs exactly such an operation inside the mocked expand and
        asserts it succeeds and is durably committed.
        """
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        @click.command(name="expand")
        @click.option("--tier", "tier_name", default=None)
        @click.pass_context
        def mock_expand(ctx, tier_name, node_op_id=None):
            op = db.session.get(NodeOp, node_op_id)
            op.message = "app-context-probe-ok"
            db.session.commit()

        result = _run_op(op_id, app, tmp_path, expand_cmd=mock_expand)
        assert result.exit_code == 0, result.output

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.message == "app-context-probe-ok"


class TestOpRunRemove:
    def test_remove_lifecycle_pending_to_done(self, app_db, tmp_path):
        """kind='remove' op transitions pending→running→done; timestamps and log set."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="remove", target_fqdn="node-01.example.com")

        result = _run_op(op_id, app, tmp_path)
        assert result.exit_code == 0, result.output

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.status == "done"
            assert op.exit_code == 0
            assert op.started_at is not None
            assert op.finished_at is not None

    def test_remove_invokes_stop_node_with_fqdn(self, app_db, tmp_path):
        """kind='remove' calls ctx.invoke(stop_node, node_spec=op.target_fqdn, ...)."""
        app, _db = app_db
        fqdn = "node-02.example.com"
        op_id = _make_op(app_db, kind="remove", target_fqdn=fqdn)

        captured_kwargs: list[dict] = []

        @click.command(name="stop")
        @click.argument("node_spec")
        @click.option("--force/--no-force", default=False)
        @click.option("--dry-run", "dry_run", is_flag=True, default=False)
        @click.pass_context
        def mock_stop(ctx, node_spec, force, dry_run):
            captured_kwargs.append({"node_spec": node_spec, "force": force, "dry_run": dry_run})

        result = _run_op(op_id, app, tmp_path, stop_node_cmd=mock_stop)
        assert result.exit_code == 0, result.output

        assert len(captured_kwargs) == 1, f"Expected one stop_node call, got {captured_kwargs}"
        assert captured_kwargs[0]["node_spec"] == fqdn
        assert captured_kwargs[0]["force"] is False
        assert captured_kwargs[0]["dry_run"] is False


class TestOpRunRebalance:
    def test_rebalance_lifecycle_pending_to_done(self, app_db, tmp_path):
        """kind='rebalance' op transitions pending→running→done."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="rebalance", tier=None)

        result = _run_op(op_id, app, tmp_path)
        assert result.exit_code == 0, result.output

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.status == "done"
            assert op.exit_code == 0
            assert op.started_at is not None
            assert op.finished_at is not None

    def test_rebalance_invokes_rebalance_command(self, app_db, tmp_path):
        """kind='rebalance' calls ctx.invoke(rebalance, dry_run=False, no_push=False, ...)."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="rebalance", tier=None)

        captured_kwargs: list[dict] = []

        @click.command(name="rebalance")
        @click.option("-N", "--dry-run", "dry_run", is_flag=True, default=False)
        @click.option("--no-push", "no_push", is_flag=True, default=False)
        @click.option("--max-moves", "max_moves", type=int, default=None)
        @click.pass_context
        def mock_rebalance(ctx, dry_run, no_push, max_moves):
            captured_kwargs.append({"dry_run": dry_run, "no_push": no_push, "max_moves": max_moves})

        result = _run_op(op_id, app, tmp_path, rebalance_cmd=mock_rebalance)
        assert result.exit_code == 0, result.output

        assert len(captured_kwargs) == 1, f"Expected one rebalance call, got {captured_kwargs}"
        assert captured_kwargs[0]["dry_run"] is False
        assert captured_kwargs[0]["no_push"] is False
        assert captured_kwargs[0]["max_moves"] is None


class TestOpRunFailurePath:
    def test_ctx_invoke_raises_sets_failed_status(self, app_db, tmp_path):
        """If ctx.invoke raises, status='failed', exit_code=1, message populated."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        @click.command(name="expand")
        @click.option("--tier", "tier_name", default=None)
        @click.pass_context
        def failing_expand(ctx, tier_name, node_op_id=None):
            raise RuntimeError("simulated expansion failure")

        result = _run_op(op_id, app, tmp_path, expand_cmd=failing_expand)
        assert result.exit_code == 0, result.output  # command itself exits cleanly

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.status == "failed"
            assert op.exit_code == 1
            assert "simulated expansion failure" in (op.message or "")
            assert op.finished_at is not None

    def test_log_file_written_on_failure(self, app_db, tmp_path):
        """Log file is created AND non-empty (contains the failure) when the op fails.

        Regression: previously a failed op left a 0-byte log because op_run only
        redirected sys.stdout/stderr, while expand narrates via the logging
        module (whose handler was bound to the original stderr → /dev/null).
        """
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        @click.command(name="expand")
        @click.option("--tier", "tier_name", default=None)
        @click.pass_context
        def failing_expand(ctx, tier_name, node_op_id=None):
            raise RuntimeError("disk full")

        _run_op(op_id, app, tmp_path, expand_cmd=failing_expand)

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.log_path is not None
            assert Path(op.log_path).exists()
            contents = Path(op.log_path).read_text()
            # The failure headline must be in the log itself, not only op.message.
            assert "disk full" in contents, f"log was empty/missing failure: {contents!r}"

    def test_log_file_captures_logging_output(self, app_db, tmp_path):
        """log.info()/log.warning() emitted by the invoked command land in the log file.

        This is the core of the fix: the FileHandler attached in op_run captures
        the logging module output, not just click.echo/print.
        """
        import logging as _logging

        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        @click.command(name="expand")
        @click.option("--tier", "tier_name", default=None)
        @click.pass_context
        def chatty_expand(ctx, tier_name, node_op_id=None):
            _logging.getLogger("cspawn.cli").info("EXPAND-LOG-MARKER step one")
            _logging.getLogger("cspawn.docker").warning("DOCKER-LOG-MARKER step two")
            click.echo("ECHO-MARKER done")

        _run_op(op_id, app, tmp_path, expand_cmd=chatty_expand)

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            contents = Path(op.log_path).read_text()

        assert "EXPAND-LOG-MARKER step one" in contents
        assert "DOCKER-LOG-MARKER step two" in contents
        assert "ECHO-MARKER done" in contents


# ---------------------------------------------------------------------------
# op-run flock serialization test
# ---------------------------------------------------------------------------


class TestOpRunFlockSerialization:
    def test_second_invocation_fails_when_lock_held(self, app_db, tmp_path):
        """If the node-ops.lock is held, op-run marks the op failed immediately."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        lock_path = tmp_path / ".node-ops.lock"

        # Pre-acquire the lock in this process (simulates a concurrent op)
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            @click.command(name="expand")
            @click.option("--tier", "tier_name", default=None)
            @click.pass_context
            def should_not_be_called(ctx, tier_name):
                raise AssertionError("expand should not be called when lock is held")

            result = _run_op(op_id, app, tmp_path, expand_cmd=should_not_be_called)
            assert result.exit_code == 0, result.output

            with app.app_context():
                op = _db.session.get(NodeOp, op_id)
                assert op.status == "failed"
                assert op.exit_code == 1
                assert "another node operation is in progress" in (op.message or "")
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()


# ---------------------------------------------------------------------------
# op-run log path test
# ---------------------------------------------------------------------------


class TestOpRunLogPath:
    def test_log_file_path_is_data_dir_node_ops_id(self, app_db, tmp_path):
        """Log file is at {DATA_DIR}/node-ops/<op_id>.log."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        result = _run_op(op_id, app, tmp_path)
        assert result.exit_code == 0, result.output

        expected_log = tmp_path / "node-ops" / f"{op_id}.log"
        assert expected_log.exists(), f"Expected log at {expected_log}"

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.log_path == str(expected_log)


# ---------------------------------------------------------------------------
# _create_droplet: node_op_id write-back (sprint 010 ticket 004)
# ---------------------------------------------------------------------------


class TestCreateDropletRecordsNodeOp:
    """_create_droplet's optional node_op_id write-back.

    When an admin-triggered `expand` op creates a droplet, its id/fqdn must
    be recorded onto the triggering NodeOp row as soon as creation succeeds
    — so a later container restart (which ticket 003's sweep detects) can
    name the specific droplet that might have been orphaned. See
    `nodeop-orphaned-on-container-restart.md`.
    """

    def test_records_droplet_id_and_fqdn_when_node_op_id_set(self, app_db):
        """node_op_id matching a pending NodeOp row: droplet_id/target_fqdn
        are updated and committed after creation succeeds."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        result, mock_instance = _invoke_create_droplet(app, node_op_id=op_id, droplet_id=99887)
        droplet, ip, fqdn, shortname = result

        assert droplet is mock_instance
        assert ip == "10.0.0.5"
        assert fqdn == "swarm5.example.com"

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.droplet_id == 99887
            assert op.target_fqdn == "swarm5.example.com"

    def test_node_op_id_none_is_complete_noop(self, app_db):
        """node_op_id=None (every existing caller: bare CLI `expand`, the
        autoscaler's `apply_plan`): no NodeOp row is affected, and creation
        succeeds identically to before this ticket."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        result, mock_instance = _invoke_create_droplet(app, node_op_id=None)
        droplet, ip, fqdn, shortname = result

        assert droplet is mock_instance
        assert ip == "10.0.0.5"
        assert fqdn == "swarm5.example.com"

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.droplet_id is None
            assert op.target_fqdn is None

    def test_missing_node_op_row_does_not_raise(self, app_db):
        """node_op_id set but no matching NodeOp row exists: creation still
        returns its normal tuple, no exception propagates."""
        app, _db = app_db

        result, mock_instance = _invoke_create_droplet(app, node_op_id="does-not-exist", droplet_id=1234)
        droplet, ip, fqdn, shortname = result

        assert droplet is mock_instance
        assert ip == "10.0.0.5"
        assert fqdn == "swarm5.example.com"

    def test_db_write_failure_is_swallowed(self, app_db):
        """A DB error during the best-effort write (commit raises) is caught
        and logged as a warning; creation still returns its normal tuple —
        node creation must never fail because of this write-back."""
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large")

        result, mock_instance = _invoke_create_droplet(
            app, node_op_id=op_id, droplet_id=5555, patch_commit_raises=True,
        )
        droplet, ip, fqdn, shortname = result

        assert droplet is mock_instance
        assert ip == "10.0.0.5"
        assert fqdn == "swarm5.example.com"
