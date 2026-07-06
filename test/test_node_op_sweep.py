"""Tests for sprint 010, ticket 003: boot-time sweep marks stuck 'running'
NodeOps as 'interrupted'.

Covers:
- `sweep_interrupted_node_ops(app)`: running -> interrupted (fields set,
  count returned, single commit); pending/done/failed/interrupted rows are
  never touched (not even `finished_at`); message composition with and
  without an orphan-droplet hint.
- `init_app(..., sweep_node_ops=...)` boot-gating: the single most
  safety-critical regression surface in this sprint. `sweep_node_ops=True`
  (the one real process-boot call site, `cspawn/app.py`) sweeps a stuck
  `running` row to `interrupted`; the default `sweep_node_ops=False` (every
  `cspawnctl` CLI call site, including `op-run` itself) leaves it untouched.
- A static guard: no `init_app(...)` call site other than `cspawn/app.py`
  may ever pass `sweep_node_ops=True`.

The `init_app` tests exercise the real function (not a stand-in), but stub
out two collaborators that are unrelated to the sweep and are unsafe to run
for real inside a test: `setup_database` (its Postgres-admin-connection
helper doesn't understand sqlite URIs and isn't needed here — table
creation is done directly) and `CodeServerManager` (opens a real SSH
connection to whatever `DOCKER_URI` happens to be configured). The
database URI itself is monkeypatched to an isolated on-disk sqlite file so
this test never touches this repo's real (and, per its checked-out `.env`,
live-infrastructure-pointing) database configuration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from cspawn.models import NodeOp, db, sweep_interrupted_node_ops


# ---------------------------------------------------------------------------
# Fixtures
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


def _make_op(app_db_pair, **kwargs) -> str:
    """Create a NodeOp row with the given fields and return its id."""
    app, _db = app_db_pair
    with app.app_context():
        op = NodeOp(**kwargs)
        _db.session.add(op)
        _db.session.commit()
        return op.id


def _snapshot(app_db_pair, op_id: str) -> dict:
    """Read back every field the sweep could plausibly touch."""
    app, _db = app_db_pair
    with app.app_context():
        op = _db.session.get(NodeOp, op_id)
        return {
            "status": op.status,
            "exit_code": op.exit_code,
            "message": op.message,
            "finished_at": op.finished_at,
        }


# ---------------------------------------------------------------------------
# sweep_interrupted_node_ops: running -> interrupted
# ---------------------------------------------------------------------------


class TestSweepMarksRunningInterrupted:
    def test_running_row_becomes_interrupted(self, app_db):
        app, _db = app_db
        op_id = _make_op(app_db, kind="expand", tier="large", status="running")

        count = sweep_interrupted_node_ops(app)
        assert count == 1

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.status == "interrupted"
            assert op.exit_code == 1
            assert op.message == "spawner restarted while op was in flight"
            assert op.finished_at is not None

    def test_returns_count_of_updated_rows(self, app_db):
        app, _db = app_db
        _make_op(app_db, kind="expand", tier="large", status="running")
        _make_op(app_db, kind="remove", target_fqdn="node-01.example.com", status="running")
        _make_op(app_db, kind="rebalance", status="running")
        # A non-running row must not inflate the count.
        _make_op(app_db, kind="expand", tier="small", status="pending")

        count = sweep_interrupted_node_ops(app)
        assert count == 3

    def test_commits_exactly_once_regardless_of_row_count(self, app_db, monkeypatch):
        app, _db = app_db
        _make_op(app_db, kind="expand", tier="large", status="running")
        _make_op(app_db, kind="remove", target_fqdn="node-01.example.com", status="running")

        commit_calls = []
        real_commit = _db.session.commit

        def _counting_commit(*args, **kwargs):
            commit_calls.append(1)
            return real_commit(*args, **kwargs)

        with app.app_context():
            monkeypatch.setattr(_db.session, "commit", _counting_commit)
            count = sweep_interrupted_node_ops(app)

        assert count == 2
        assert len(commit_calls) == 1, f"Expected exactly one commit, got {len(commit_calls)}"


# ---------------------------------------------------------------------------
# sweep_interrupted_node_ops: message composition
# ---------------------------------------------------------------------------


class TestSweepMessageComposition:
    def test_message_names_droplet_when_target_fqdn_and_droplet_id_present(self, app_db):
        app, _db = app_db
        op_id = _make_op(
            app_db,
            kind="expand",
            tier="large",
            status="running",
            target_fqdn="swarm5.dojtl.net",
            droplet_id=123456789,
        )

        sweep_interrupted_node_ops(app)

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert "spawner restarted while op was in flight" in op.message
            assert "swarm5.dojtl.net" in op.message
            assert "123456789" in op.message
            assert "orphaned" in op.message

    def test_message_generic_when_droplet_fields_absent(self, app_db):
        app, _db = app_db
        op_id = _make_op(app_db, kind="rebalance", status="running")

        sweep_interrupted_node_ops(app)

        with app.app_context():
            op = _db.session.get(NodeOp, op_id)
            assert op.message == "spawner restarted while op was in flight"
            assert "orphaned" not in op.message


# ---------------------------------------------------------------------------
# sweep_interrupted_node_ops: terminal/pending rows are never touched
# ---------------------------------------------------------------------------


class TestSweepLeavesOtherStatusesUntouched:
    @pytest.mark.parametrize("status", ["pending", "done", "failed", "interrupted"])
    def test_non_running_row_completely_untouched(self, app_db, status):
        app, _db = app_db
        fixed_finished_at = (
            datetime(2026, 1, 1, tzinfo=timezone.utc) if status != "pending" else None
        )
        op_id = _make_op(
            app_db,
            kind="expand",
            tier="large",
            status=status,
            exit_code=0 if status == "done" else (1 if status in ("failed", "interrupted") else None),
            message="pre-existing message" if status != "pending" else None,
            finished_at=fixed_finished_at,
        )

        before = _snapshot(app_db, op_id)
        count = sweep_interrupted_node_ops(app)
        after = _snapshot(app_db, op_id)

        assert count == 0
        assert after == before, f"Row with status={status!r} was modified by the sweep: {before} -> {after}"


# ---------------------------------------------------------------------------
# init_app(..., sweep_node_ops=...) boot-gating regression test
#
# This is the single most safety-critical test in this ticket: it proves
# that the sweep only ever fires when sweep_node_ops=True is passed
# explicitly, which today happens ONLY at cspawn/app.py's call site.
# ---------------------------------------------------------------------------


def _install_safe_init_app_stubs(monkeypatch):
    """Stub init_app() collaborators that are irrelevant to the sweep-gating
    behavior under test but are unsafe/impossible to exercise for real in a
    unit test:

    - setup_database: its Postgres-admin-connection helper doesn't
      understand sqlite URIs and isn't needed here (table creation is done
      directly against the isolated sqlite file).
    - CodeServerManager: opens a real SSH connection to whatever DOCKER_URI
      is configured.
    - setup_sessions: registers flask_session's "sessions" table on the
      module-global SQLAlchemy metadata (`cspawn.models.db`), which is
      shared across every Flask app instance in this process. Calling it
      from more than one real init_app() invocation in the same test run
      raises "Table 'sessions' is already defined for this MetaData
      instance" -- an artifact of this process-wide sharing, unrelated to
      the sweep logic under test.
    """
    import cspawn.init as init_module

    def _fake_setup_database(app):
        with app.app_context():
            app.db.create_all()

    monkeypatch.setattr(init_module, "setup_database", _fake_setup_database)
    monkeypatch.setattr(init_module, "CodeServerManager", lambda app: None)
    monkeypatch.setattr(init_module, "setup_sessions", lambda app, devel=False: None)


def _point_config_at_isolated_sqlite(monkeypatch, tmp_path, db_filename) -> str:
    """Redirect init_app()'s config to an isolated on-disk sqlite database
    and scratch directories, entirely inside tmp_path. os.environ wins over
    the checked-out .env in get_config()'s merge order, so this fully
    overrides this repo's real (devel/local-prod) DATABASE_URI.
    """
    db_uri = f"sqlite:///{tmp_path / db_filename}"
    monkeypatch.setenv("DATABASE_URI", db_uri)
    monkeypatch.setenv("APP_DIR", str(tmp_path / "app_dir"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data_dir"))
    return db_uri


def _seed_running_op(db_uri: str) -> str:
    """Create the schema at db_uri (a fresh file) and seed one running NodeOp."""
    seed_app = Flask(f"seed-{id(db_uri)}")
    seed_app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    seed_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(seed_app)
    with seed_app.app_context():
        db.create_all()
        op = NodeOp(kind="expand", tier="large", status="running")
        db.session.add(op)
        db.session.commit()
        op_id = op.id
        db.session.remove()
    return op_id


class TestInitAppSweepNodeOpsGating:
    def test_sweep_node_ops_true_marks_running_row_interrupted(self, tmp_path, monkeypatch):
        """The one enabling call site's behavior: init_app(sweep_node_ops=True)
        against a database containing a running NodeOp row marks it interrupted.
        """
        db_uri = _point_config_at_isolated_sqlite(monkeypatch, tmp_path, "sweep_true.db")
        op_id = _seed_running_op(db_uri)
        _install_safe_init_app_stubs(monkeypatch)

        from cspawn.init import init_app

        app = init_app(deployment="devel", sweep_node_ops=True)

        with app.app_context():
            op = db.session.get(NodeOp, op_id)
            assert op.status == "interrupted"
            assert op.exit_code == 1
            assert op.finished_at is not None

    def test_sweep_node_ops_default_false_leaves_running_row_untouched(self, tmp_path, monkeypatch):
        """The regression guard: every existing init_app(...) call site
        (cli/util.py::get_app, cli/devel.py, every test fixture) omits
        sweep_node_ops and so keeps the default False. A genuinely
        in-flight-looking row must be left completely alone.
        """
        db_uri = _point_config_at_isolated_sqlite(monkeypatch, tmp_path, "sweep_false.db")
        op_id = _seed_running_op(db_uri)
        _install_safe_init_app_stubs(monkeypatch)

        from cspawn.init import init_app

        app = init_app(deployment="devel")  # sweep_node_ops omitted -> False

        with app.app_context():
            op = db.session.get(NodeOp, op_id)
            assert op.status == "running"
            assert op.exit_code is None
            assert op.finished_at is None


# ---------------------------------------------------------------------------
# Static guard: only cspawn/app.py may pass sweep_node_ops=True
# ---------------------------------------------------------------------------


def _find_init_app_sweep_true_calls(path: Path) -> list[str]:
    """Return `"path:lineno"` for every real `init_app(...)` call in `path`
    that passes `sweep_node_ops=True` as a keyword argument.

    Parses the AST (rather than grepping raw text) so that comments and
    docstrings mentioning the string "sweep_node_ops=True" -- e.g. this
    ticket's own explanatory docstrings in cspawn/models.py and
    cspawn/app.py -- are never mistaken for an actual call site.
    """
    import ast

    tree = ast.parse(path.read_text(), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "init_app":
            continue
        for kw in node.keywords:
            if kw.arg == "sweep_node_ops" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                hits.append(f"{path}:{node.lineno}")
    return hits


def test_only_app_py_passes_sweep_node_ops_true():
    """Per architecture-update.md Step 6: the sweep must run only at the one
    true process-boot call site. This is a permanent regression guard against
    a future call site (in particular any cspawnctl CLI path) opting in by
    mistake.
    """
    repo_root = Path(__file__).resolve().parent.parent
    cspawn_dir = repo_root / "cspawn"
    app_py = cspawn_dir / "app.py"

    offending = []
    for path in cspawn_dir.rglob("*.py"):
        if path == app_py:
            continue
        offending.extend(_find_init_app_sweep_true_calls(path))

    assert offending == [], f"init_app(sweep_node_ops=True) must appear only in cspawn/app.py, also found at: {offending}"

    assert _find_init_app_sweep_true_calls(app_py), "cspawn/app.py must call init_app(sweep_node_ops=True)"
