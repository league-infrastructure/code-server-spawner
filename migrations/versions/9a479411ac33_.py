""".

Revision ID: 9a479411ac33
Revises: a79befb0ca98
Create Date: 2025-03-16 21:02:32.251351

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "9a479411ac33"
down_revision = "a79befb0ca98"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("sessions")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("is_anonymous")

    op.create_table(
        "sessions",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.VARCHAR(length=255), autoincrement=False, nullable=True),
        sa.Column("data", postgresql.BYTEA(), autoincrement=False, nullable=True),
        sa.Column("expiry", postgresql.TIMESTAMP(), autoincrement=False, nullable=True),
        sa.PrimaryKeyConstraint("id", name="sessions_pkey"),
        sa.UniqueConstraint("session_id", name="sessions_session_id_key"),
    )
    # ### end Alembic commands ###
