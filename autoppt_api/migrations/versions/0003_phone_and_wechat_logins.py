"""phone and wechat logins

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18 19:39:21.447759
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('phone', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('wechat_unionid', sa.String(length=64), nullable=True))
        batch_op.alter_column('email',
               existing_type=sa.VARCHAR(length=254),
               nullable=True)
        batch_op.create_unique_constraint('uq_users_phone', ['phone'])
        batch_op.create_unique_constraint('uq_users_wechat_unionid', ['wechat_unionid'])


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('uq_users_wechat_unionid', type_='unique')
        batch_op.drop_constraint('uq_users_phone', type_='unique')
        batch_op.alter_column('email',
               existing_type=sa.VARCHAR(length=254),
               nullable=False)
        batch_op.drop_column('wechat_unionid')
        batch_op.drop_column('phone')

