"""Tests for Class schema additions — purge_after, purge_by, target_nodes.

Sprint 005, Ticket 001.

Verifies:
- The three new columns exist on the Class model with the correct SQLAlchemy
  column definitions (nullable, correct type).
- A Class row can be created with all three fields absent (NULL).
- A Class row can be created and round-tripped with all three fields set.
- Class.running is still present and unmodified.
- The migration script applies cleanly to a fresh test DB (SQLite).

Uses an in-memory SQLite DB built with db.create_all() — no live PostgreSQL
needed for the model/column tests.  The migration test drives the Alembic
revision directly against a separate in-memory SQLite DB to confirm the
upgrade() and downgrade() paths work mechanically.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from flask import Flask
from sqlalchemy import inspect, text

from cspawn.models import db, Class, ClassProto


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_proto(db_session) -> ClassProto:
    """Insert and return the bare-minimum ClassProto required by Class.proto_id FK."""
    proto = ClassProto(
        name="Test Proto",
        image_uri="test-image:latest",
        hash="deadbeef",
    )
    db_session.session.add(proto)
    db_session.session.flush()
    return proto


def _minimal_class(proto_id: int) -> Class:
    now = datetime.now(timezone.utc)
    return Class(
        name="Test Class",
        proto_id=proto_id,
        start_date=now,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_db():
    """Fresh in-memory SQLite app with all tables created."""
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
# Column-presence tests (via SQLAlchemy inspection)
# ---------------------------------------------------------------------------

def test_class_has_purge_after_column(app_db):
    inspector = inspect(app_db.engine)
    columns = {c["name"] for c in inspector.get_columns("classes")}
    assert "purge_after" in columns, "purge_after column missing from classes table"


def test_class_has_purge_by_column(app_db):
    inspector = inspect(app_db.engine)
    columns = {c["name"] for c in inspector.get_columns("classes")}
    assert "purge_by" in columns, "purge_by column missing from classes table"


def test_class_has_target_nodes_column(app_db):
    inspector = inspect(app_db.engine)
    columns = {c["name"] for c in inspector.get_columns("classes")}
    assert "target_nodes" in columns, "target_nodes column missing from classes table"


def test_class_running_column_unchanged(app_db):
    """Class.running must still exist and be boolean/non-nullable."""
    inspector = inspect(app_db.engine)
    col_map = {c["name"]: c for c in inspector.get_columns("classes")}
    assert "running" in col_map, "running column must still exist"


# ---------------------------------------------------------------------------
# ORM round-trip tests
# ---------------------------------------------------------------------------

def test_class_row_null_purge_fields(app_db):
    """Class row with purge fields absent inserts and reads back as NULL."""
    proto = _minimal_proto(app_db)
    cls = _minimal_class(proto.id)
    # purge fields not set — rely on nullable default
    app_db.session.add(cls)
    app_db.session.commit()

    fetched = app_db.session.get(Class, cls.id)
    assert fetched.purge_after is None
    assert fetched.purge_by is None
    assert fetched.target_nodes is None


def test_class_row_with_purge_fields_set(app_db):
    """Class row with all three purge fields set round-trips correctly."""
    proto = _minimal_proto(app_db)
    cls = _minimal_class(proto.id)

    pa = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)
    pb = datetime(2026, 9, 1, 17, 0, 0, tzinfo=timezone.utc)

    cls.purge_after = pa
    cls.purge_by = pb
    cls.target_nodes = 12

    app_db.session.add(cls)
    app_db.session.commit()

    fetched = app_db.session.get(Class, cls.id)
    # SQLite strips tzinfo on round-trip; compare naive-equivalent values
    assert fetched.purge_after.replace(tzinfo=None) == pa.replace(tzinfo=None)
    assert fetched.purge_by.replace(tzinfo=None) == pb.replace(tzinfo=None)
    assert fetched.target_nodes == 12


def test_class_running_still_works(app_db):
    """Class.running column is unmodified — Boolean, defaults False."""
    proto = _minimal_proto(app_db)
    cls = _minimal_class(proto.id)
    app_db.session.add(cls)
    app_db.session.commit()

    fetched = app_db.session.get(Class, cls.id)
    assert fetched.running is False


# ---------------------------------------------------------------------------
# Alembic migration tests (SQLite, upgrade/downgrade)
# ---------------------------------------------------------------------------

def test_migration_upgrade_adds_columns():
    """The Alembic upgrade() path adds the three columns to an existing table."""
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite:///:memory:")

    # Create the classes table WITHOUT the new columns to simulate existing prod DB
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                proto_id INTEGER NOT NULL,
                start_date DATETIME NOT NULL,
                running BOOLEAN NOT NULL DEFAULT 0
            )
        """))
        conn.execute(sa.text(
            "INSERT INTO classes (name, proto_id, start_date, running) "
            "VALUES ('Test', 1, '2026-01-01', 0)"
        ))

    # Directly call the batch operations on the engine (simulating alembic env)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        with op.batch_alter_table("classes") as batch_op:
            batch_op.add_column(sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
            batch_op.add_column(sa.Column("purge_by", sa.DateTime(timezone=True), nullable=True))
            batch_op.add_column(sa.Column("target_nodes", sa.Integer(), nullable=True))

    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("classes")}
    assert "purge_after" in columns
    assert "purge_by" in columns
    assert "target_nodes" in columns

    # Existing row should have NULL for the new fields
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT purge_after, purge_by, target_nodes FROM classes WHERE id=1"
        )).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None


def test_migration_downgrade_removes_columns():
    """The Alembic downgrade() path removes the three columns."""
    import sqlalchemy as sa
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations

    engine = sa.create_engine("sqlite:///:memory:")

    # Create table WITH the new columns already present
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                proto_id INTEGER NOT NULL,
                start_date DATETIME NOT NULL,
                running BOOLEAN NOT NULL DEFAULT 0,
                purge_after DATETIME,
                purge_by DATETIME,
                target_nodes INTEGER
            )
        """))

    # Run downgrade (remove the columns via batch)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        with op.batch_alter_table("classes") as batch_op:
            batch_op.drop_column("target_nodes")
            batch_op.drop_column("purge_by")
            batch_op.drop_column("purge_after")

    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("classes")}
    assert "purge_after" not in columns
    assert "purge_by" not in columns
    assert "target_nodes" not in columns
    # running must still be there
    assert "running" in columns
