"""Add node_ops table for manual node operation tracking.

Sprint 006: admin Nodes tab — manual swarm node management.

Revision ID: v006_add_node_op_table
Revises: v001_add_class_purge_window_fields
Create Date: 2026-06-27

Migration path rationale
------------------------
This revision creates the ``node_ops`` table used by the admin Nodes tab to
track manual expand/remove node operations submitted through the UI.

The migration is idempotent:
- PostgreSQL: ``CREATE TABLE IF NOT EXISTS node_ops (...)`` via ``op.execute``.
- SQLite/other (tests): ``op.create_table(...)`` inside a ``try/except`` that
  silences the "table already exists" OperationalError.

``downgrade()`` drops the table: PostgreSQL uses ``DROP TABLE IF EXISTS``; SQLite
uses ``op.drop_table`` (raises if the table is absent, which is acceptable for
test teardown only).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

# ---------------------------------------------------------------------------
# Alembic revision identifiers
# ---------------------------------------------------------------------------
revision = "v006_add_node_op_table"
down_revision = "v001_add_class_purge_window_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS node_ops (
                id VARCHAR(36) NOT NULL,
                kind VARCHAR(16) NOT NULL,
                tier VARCHAR(100),
                target_fqdn VARCHAR(255),
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                exit_code INTEGER,
                log_path VARCHAR(500),
                message TEXT,
                created_by INTEGER REFERENCES users(id),
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                started_at TIMESTAMP WITH TIME ZONE,
                finished_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (id)
            )
        """))
    else:
        # SQLite / other: use Alembic create_table; silently skip if already exists.
        try:
            op.create_table(
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
        except OperationalError:
            # Table already exists — migration is idempotent.
            pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.execute(sa.text("DROP TABLE IF EXISTS node_ops"))
    else:
        op.drop_table("node_ops")
