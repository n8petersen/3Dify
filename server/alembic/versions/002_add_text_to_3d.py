"""Add text-to-3D fields: job_type, prompt, generated_image_path.

Revision ID: 002_add_text_to_3d
Revises: 001_add_users_sessions
Create Date: 2026-02-23
"""
from alembic import op
import sqlalchemy as sa

revision = "002_add_text_to_3d"
down_revision = "001_add_users_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("job_type", sa.String(), nullable=False, server_default="image"))
    op.add_column("jobs", sa.Column("prompt", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("generated_image_path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "generated_image_path")
    op.drop_column("jobs", "prompt")
    op.drop_column("jobs", "job_type")
