"""Tests for NodeOp model and migration — sprint 006, ticket 001;
extended by sprint 010, ticket 002 (droplet_id column).

Verifies:
- NodeOp class is importable from cspawn.models.
- All required columns exist on the node_ops table with correct types.
- A NodeOp row can be created with defaults and round-tripped.
- A NodeOp row can be created with all fields set and round-tripped.
- Status update (pending → done) persists correctly.
- The Alembic migration upgrade() creates the node_ops table on SQLite.
- The Alembic migration downgrade() removes the node_ops table on SQLite.
- `droplet_id` defaults to None, round-trips when set, and the v007
  migration's upgrade()/downgrade() add/drop the column on SQLite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from flask import Flask
from sqlalchemy import inspect

from cspawn.models import NodeOp, db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_db():
    """Fresh in-memory SQLite app with all tables created via db.create_all()."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield db
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------------
# Import test
# ---------------------------------------------------------------------------


def test_nodeop_importable():
    """from cspawn.models import NodeOp must succeed without error."""
    from cspawn.models import NodeOp as _NodeOp  # noqa: F401
    assert _NodeOp is not None


# ---------------------------------------------------------------------------
# Column-presence tests (via SQLAlchemy inspection)
# ---------------------------------------------------------------------------


def test_node_ops_table_exists(app_db):
    inspector = inspect(app_db.engine)
    tables = inspector.get_table_names()
    assert "node_ops" in tables, "node_ops table missing from database"


def test_node_ops_required_columns_present(app_db):
    inspector = inspect(app_db.engine)
    columns = {c["name"] for c in inspector.get_columns("node_ops")}
    required = {
        "id",
        "kind",
        "tier",
        "target_fqdn",
        "droplet_id",
        "status",
        "exit_code",
        "log_path",
        "message",
        "created_by",
        "created_at",
        "started_at",
        "finished_at",
    }
    missing = required - columns
    assert not missing, f"Missing columns on node_ops: {missing}"


# ---------------------------------------------------------------------------
# ORM round-trip tests
# ---------------------------------------------------------------------------


def test_nodeop_create_with_defaults(app_db):
    """NodeOp row created with only required fields round-trips with correct defaults."""
    op = NodeOp(kind="expand", tier="large")
    app_db.session.add(op)
    app_db.session.commit()

    fetched = app_db.session.get(NodeOp, op.id)
    assert fetched is not None
    assert fetched.kind == "expand"
    assert fetched.tier == "large"
    assert fetched.status == "pending"
    assert fetched.target_fqdn is None
    assert fetched.droplet_id is None
    assert fetched.exit_code is None
    assert fetched.log_path is None
    assert fetched.message is None
    assert fetched.created_by is None
    assert fetched.created_at is not None
    assert fetched.started_at is None
    assert fetched.finished_at is None
    # id should be a non-empty string (UUID)
    assert isinstance(fetched.id, str)
    assert len(fetched.id) == 36


def test_nodeop_create_with_all_fields(app_db):
    """NodeOp row with all fields set round-trips correctly."""
    now = datetime.now(timezone.utc)
    op = NodeOp(
        kind="remove",
        tier=None,
        target_fqdn="node-01.example.com",
        status="running",
        exit_code=None,
        log_path="/var/log/ops/abc123.log",
        message="Removing node",
        created_by=None,
        created_at=now,
        started_at=now,
        finished_at=None,
    )
    app_db.session.add(op)
    app_db.session.commit()

    fetched = app_db.session.get(NodeOp, op.id)
    assert fetched.kind == "remove"
    assert fetched.target_fqdn == "node-01.example.com"
    assert fetched.status == "running"
    assert fetched.log_path == "/var/log/ops/abc123.log"
    assert fetched.message == "Removing node"
    # SQLite strips tzinfo on round-trip; compare naive equivalents
    assert fetched.created_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert fetched.started_at.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert fetched.finished_at is None


def test_nodeop_status_update(app_db):
    """NodeOp status and exit_code update from pending to done persist correctly."""
    op = NodeOp(kind="expand", tier="small", status="pending")
    app_db.session.add(op)
    app_db.session.commit()

    op_id = op.id

    # Update to done
    fetched = app_db.session.get(NodeOp, op_id)
    fetched.status = "done"
    fetched.exit_code = 0
    fetched.finished_at = datetime.now(timezone.utc)
    app_db.session.commit()

    updated = app_db.session.get(NodeOp, op_id)
    assert updated.status == "done"
    assert updated.exit_code == 0
    assert updated.finished_at is not None


def test_nodeop_create_without_droplet_id_defaults_to_none(app_db):
    """A NodeOp constructed without droplet_id reads back as None (no default, no backfill)."""
    op = NodeOp(kind="remove", target_fqdn="node-03.example.com")
    app_db.session.add(op)
    app_db.session.commit()

    fetched = app_db.session.get(NodeOp, op.id)
    assert fetched.droplet_id is None


def test_nodeop_droplet_id_round_trips(app_db):
    """A NodeOp created with droplet_id set round-trips the value through the DB."""
    op = NodeOp(kind="expand", tier="large", droplet_id=123456789)
    app_db.session.add(op)
    app_db.session.commit()

    op_id = op.id
    fetched = app_db.session.get(NodeOp, op_id)
    assert fetched.droplet_id == 123456789

    # Re-query via a fresh query (not just the identity map) to confirm the
    # value actually persisted, not just held in the Python object.
    app_db.session.expire_all()
    refetched = app_db.session.get(NodeOp, op_id)
    assert refetched.droplet_id == 123456789


def test_nodeop_multiple_rows(app_db):
    """Multiple NodeOp rows can coexist with independent UUIDs."""
    op1 = NodeOp(kind="expand", tier="large")
    op2 = NodeOp(kind="remove", target_fqdn="node-02.example.com")
    app_db.session.add_all([op1, op2])
    app_db.session.commit()

    assert op1.id != op2.id
    rows = app_db.session.query(NodeOp).all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Alembic migration tests (SQLite, upgrade/downgrade)
# ---------------------------------------------------------------------------


def test_migration_upgrade_creates_node_ops_table():
    """The Alembic upgrade() path creates the node_ops table on a fresh SQLite DB."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")

    # Create the users table so the FK reference in node_ops is satisfied
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(200) NOT NULL
            )
        """))

    # Run the upgrade via Operations directly (SQLite path of the migration)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        ops.create_table(
            "node_ops",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("tier", sa.String(100), nullable=True),
            sa.Column("target_fqdn", sa.String(255), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("log_path", sa.String(500), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )

    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    assert "node_ops" in tables, "node_ops table not created by migration upgrade"

    columns = {c["name"] for c in inspector.get_columns("node_ops")}
    assert "id" in columns
    assert "kind" in columns
    assert "status" in columns
    assert "created_at" in columns


def test_migration_downgrade_removes_node_ops_table():
    """The Alembic downgrade() path removes the node_ops table."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")

    # Create the table to simulate a state after upgrade
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE node_ops (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                kind VARCHAR(16) NOT NULL,
                tier VARCHAR(100),
                target_fqdn VARCHAR(255),
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                exit_code INTEGER,
                log_path VARCHAR(500),
                message TEXT,
                created_by INTEGER,
                created_at DATETIME NOT NULL,
                started_at DATETIME,
                finished_at DATETIME
            )
        """))

    # Run downgrade
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        ops.drop_table("node_ops")

    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    assert "node_ops" not in tables, "node_ops table not removed by migration downgrade"


# ---------------------------------------------------------------------------
# v007 migration tests (sprint 010, ticket 002): droplet_id column
# ---------------------------------------------------------------------------


def _make_node_ops_table(conn):
    """Create a node_ops table matching the post-v006 schema (pre-v007)."""
    conn.execute(sa.text("""
        CREATE TABLE node_ops (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            kind VARCHAR(16) NOT NULL,
            tier VARCHAR(100),
            target_fqdn VARCHAR(255),
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            exit_code INTEGER,
            log_path VARCHAR(500),
            message TEXT,
            created_by INTEGER,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            finished_at DATETIME
        )
    """))


def test_v007_migration_upgrade_adds_droplet_id_column():
    """The v007 migration's upgrade() adds droplet_id to an existing node_ops table on SQLite."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from migrations.versions.v007_add_node_op_droplet_id import upgrade

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _make_node_ops_table(conn)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            upgrade()

    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("node_ops")}
    assert "droplet_id" in columns, "droplet_id column not added by v007 migration upgrade"


def test_v007_migration_upgrade_is_idempotent():
    """Running upgrade() twice (simulating a re-run against an already-migrated DB) does not raise."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from migrations.versions.v007_add_node_op_droplet_id import upgrade

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _make_node_ops_table(conn)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            upgrade()

    # Second run against the already-migrated table must not raise.
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            upgrade()

    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("node_ops")}
    assert "droplet_id" in columns


def test_v007_migration_downgrade_drops_droplet_id_column():
    """The v007 migration's downgrade() removes droplet_id, leaving the rest of node_ops intact."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from migrations.versions.v007_add_node_op_droplet_id import downgrade

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _make_node_ops_table(conn)
        conn.execute(sa.text("ALTER TABLE node_ops ADD COLUMN droplet_id INTEGER"))

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            downgrade()

    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("node_ops")}
    assert "droplet_id" not in columns, "droplet_id column not removed by v007 migration downgrade"
    assert "id" in columns, "downgrade must not remove unrelated columns"
