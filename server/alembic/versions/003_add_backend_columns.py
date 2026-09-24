"""Add backend columns: backend, backend_job_id, billed_seconds, backend_cost_usd.

Revision ID: 003_add_backend_columns
Revises: 002_add_text_to_3d
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "003_add_backend_columns"
down_revision = "002_add_text_to_3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("backend", sa.String(), nullable=True))
    op.create_index("ix_jobs_backend", "jobs", ["backend"])
    op.add_column("jobs", sa.Column("backend_job_id", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("billed_seconds", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("backend_cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "backend_cost_usd")
    op.drop_column("jobs", "billed_seconds")
    op.drop_column("jobs", "backend_job_id")
    op.drop_index("ix_jobs_backend", table_name="jobs")
    op.drop_column("jobs", "backend")
