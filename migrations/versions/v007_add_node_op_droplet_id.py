"""Add droplet_id column to node_ops.

Sprint 010: node op reliability — recover orphaned NodeOps on restart.

Revision ID: v007_add_node_op_droplet_id
Revises: v006_add_node_op_table
Create Date: 2026-07-06

Migration path rationale
------------------------
This revision adds one nullable ``droplet_id`` column to the existing
``node_ops`` table so a later ticket can record which DigitalOcean droplet
an ``expand`` ``NodeOp`` created, allowing an interrupted op's message to
name that specific droplet.

The migration is additive and idempotent:
- PostgreSQL: ``ALTER TABLE node_ops ADD COLUMN IF NOT EXISTS droplet_id
  INTEGER`` via ``op.execute``.
- SQLite/other (tests): ``op.add_column(...)`` inside a ``try/except`` that
  silences the "duplicate column" OperationalError.

``downgrade()`` drops the column: PostgreSQL uses ``DROP COLUMN IF EXISTS``;
SQLite uses ``op.drop_column``.

No backfill: existing rows (and any row created without setting
``droplet_id``) read back as ``None``.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

# ---------------------------------------------------------------------------
# Alembic revision identifiers
# ---------------------------------------------------------------------------
revision = "v007_add_node_op_droplet_id"
down_revision = "v006_add_node_op_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.execute(sa.text("""
            ALTER TABLE node_ops ADD COLUMN IF NOT EXISTS droplet_id INTEGER
        """))
    else:
        # SQLite / other: use Alembic add_column; silently skip if the column
        # already exists.
        try:
            op.add_column("node_ops", sa.Column("droplet_id", sa.Integer(), nullable=True))
        except OperationalError:
            # Column already exists — migration is idempotent.
            pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.execute(sa.text("ALTER TABLE node_ops DROP COLUMN IF EXISTS droplet_id"))
    else:
        op.drop_column("node_ops", "droplet_id")
