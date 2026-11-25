"""Bind employee API keys to a session version and one stable project.

Revision ID: c7d5a20e3b94
Revises: b6a4f10d2c83
"""

import sqlalchemy as sa

from alembic import op

revision = "c7d5a20e3b94"
down_revision = "b6a4f10d2c83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("employee_session_version", sa.Integer(), nullable=True))
    op.add_column("api_keys", sa.Column("project_id", sa.String(), nullable=True))
    op.add_column("api_keys", sa.Column("project_permission", sa.String(), nullable=True))


def downgrade() -> None:
    # A scoped key must never become an unrestricted legacy key on downgrade.
    op.execute(sa.text("DELETE FROM api_keys WHERE employee_session_version IS NOT NULL "
                       "OR project_id IS NOT NULL OR project_permission IS NOT NULL"))
    op.drop_column("api_keys", "project_permission")
    op.drop_column("api_keys", "project_id")
    op.drop_column("api_keys", "employee_session_version")
