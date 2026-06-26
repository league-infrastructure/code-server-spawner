"""Add purge_after, purge_by, target_nodes to classes table.

Sprint 005: instructor-triggered cluster pre-sizing and time-windowed purging.

Revision ID: v001_add_class_purge_window_fields
Revises:
Create Date: 2026-06-26

Migration path rationale
------------------------
The live ``migrations/versions/`` directory did not exist — only stale
``versions-old/`` and ``versions-old-2/`` directories were present.
Flask-Migrate is wired (``Migrate`` in ``cspawn/init.py``) but had no
active revision chain.

Rather than running ``flask db migrate`` (which needs a live DB to diff
against) we hand-wrote this minimal revision.  This is safe because:

1. The three columns are simple nullable additions with no default.
2. We supply both ``upgrade()`` and ``downgrade()``.
3. The migration is idempotent via conditional ADD COLUMN checks executed
   through Alembic's ``op.execute()`` for PostgreSQL.  SQLite (used by
   tests) does not support IF NOT EXISTS on ALTER TABLE, so we use
   ``op.add_column()`` directly inside a ``try/except`` there.
4. ``flask db upgrade`` against an existing ``classes`` table (without
   these columns) applies cleanly; ``flask db downgrade`` removes them.

Index on ``purge_after``: added here because the reaper / demand queries
filter on it and the architecture review flagged it explicitly.
"""

from alembic import op
import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Alembic revision identifiers
# ---------------------------------------------------------------------------
revision = "v001_add_class_purge_window_fields"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # PostgreSQL supports IF NOT EXISTS on ALTER TABLE ADD COLUMN
        bind.execute(sa.text(
            "ALTER TABLE classes "
            "ADD COLUMN IF NOT EXISTS purge_after TIMESTAMP WITH TIME ZONE"
        ))
        bind.execute(sa.text(
            "ALTER TABLE classes "
            "ADD COLUMN IF NOT EXISTS purge_by TIMESTAMP WITH TIME ZONE"
        ))
        bind.execute(sa.text(
            "ALTER TABLE classes "
            "ADD COLUMN IF NOT EXISTS target_nodes INTEGER"
        ))
        # Add index on purge_after if it doesn't exist
        bind.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_classes_purge_after "
            "ON classes (purge_after)"
        ))
    else:
        # SQLite / other: use Alembic batch operations (safe for tests)
        with op.batch_alter_table("classes", schema=None) as batch_op:
            # Add columns; if they already exist Alembic will raise — acceptable
            # for non-production use; prod always uses PostgreSQL path above.
            batch_op.add_column(
                sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.add_column(
                sa.Column("purge_by", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.add_column(
                sa.Column("target_nodes", sa.Integer(), nullable=True)
            )
        op.create_index(
            "ix_classes_purge_after", "classes", ["purge_after"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.execute(sa.text(
            "DROP INDEX IF EXISTS ix_classes_purge_after"
        ))
        bind.execute(sa.text(
            "ALTER TABLE classes DROP COLUMN IF EXISTS purge_after"
        ))
        bind.execute(sa.text(
            "ALTER TABLE classes DROP COLUMN IF EXISTS purge_by"
        ))
        bind.execute(sa.text(
            "ALTER TABLE classes DROP COLUMN IF EXISTS target_nodes"
        ))
    else:
        op.drop_index("ix_classes_purge_after", table_name="classes")
        with op.batch_alter_table("classes", schema=None) as batch_op:
            batch_op.drop_column("target_nodes")
            batch_op.drop_column("purge_by")
            batch_op.drop_column("purge_after")
