"""index usage records by time

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19 07:34:55.409155
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:

    with op.batch_alter_table('usage_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_usage_records_created_at'), ['created_at'], unique=False)




def downgrade() -> None:

    with op.batch_alter_table('usage_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_usage_records_created_at'))


