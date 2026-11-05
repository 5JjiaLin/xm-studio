"""Freeze episode and generation target on provider calls."""

import sqlalchemy as sa

from alembic import op

revision: str = "c6e2a14f093b"
down_revision: str = "b5d1e8f3a627"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_calls", sa.Column("episode_id", sa.Integer(), nullable=True))
    op.add_column("api_calls", sa.Column("generation_target", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_calls", "generation_target")
    op.drop_column("api_calls", "episode_id")
