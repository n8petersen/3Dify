"""Add users, sessions tables and user_id column on jobs.

Revision ID: 001_add_users_sessions
Revises:
Create Date: 2026-02-22
"""
from alembic import op
import sqlalchemy as sa

revision = "001_add_users_sessions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.add_column("jobs", sa.Column("user_id", sa.String(), nullable=True))
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    op.create_foreign_key("fk_jobs_user_id", "jobs", "users", ["user_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_jobs_user_id", "jobs", type_="foreignkey")
    op.drop_index("ix_jobs_user_id", table_name="jobs")
    op.drop_column("jobs", "user_id")
    op.drop_table("sessions")
    op.drop_table("users")
