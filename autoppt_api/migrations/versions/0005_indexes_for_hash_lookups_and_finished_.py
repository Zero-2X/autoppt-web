"""indexes for hash lookups and finished jobs

Found by the 48-hour soak (2026-10-19): deleting a job checks every file hash against job_files, artifacts
and upload_sessions, and the health check reads jobs by finished_at; all four scanned whole tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19 05:14:43.900126
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_artifacts_sha256'), ['sha256'], unique=False)

    with op.batch_alter_table('job_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_job_files_sha256'), ['sha256'], unique=False)

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jobs_finished_at'), ['finished_at'], unique=False)

    with op.batch_alter_table('upload_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_upload_sessions_sha256'), ['sha256'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('upload_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_upload_sessions_sha256'))

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_jobs_finished_at'))

    with op.batch_alter_table('job_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_job_files_sha256'))

    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_artifacts_sha256'))

