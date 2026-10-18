"""model gateway tokens

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 20:08:21.239750
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('model_tokens',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.Column('worker_id', sa.String(length=32), nullable=False),
    sa.Column('phase', sa.String(length=16), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('budget_input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cached_input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('output_tokens', sa.BigInteger(), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['worker_id'], ['workers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('model_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_model_tokens_job_id'), ['job_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('model_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_model_tokens_job_id'))

    op.drop_table('model_tokens')
