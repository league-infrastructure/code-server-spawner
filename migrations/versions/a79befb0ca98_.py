""".

Revision ID: a79befb0ca98
Revises: 9b2a8f7283f7
Create Date: 2025-03-16 20:43:19.612558

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a79befb0ca98'
down_revision = '9b2a8f7283f7'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('classes', schema=None) as batch_op:
        batch_op.create_unique_constraint(None, ['class_code'])

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('classes', schema=None) as batch_op:
        batch_op.drop_constraint(None, type_='unique')

    # ### end Alembic commands ###
