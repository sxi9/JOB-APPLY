"""feed_posts: which job each WhatsApp group feed already posted (dedupe, written after a successful send)

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feed_posts",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("feed_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("feed_id", "job_id", name="uq_feed_posts_feed_id_job_id"),
    )
    op.create_index("ix_feed_posts_feed_id", "feed_posts", ["feed_id"])
    op.create_index("ix_feed_posts_job_id", "feed_posts", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_feed_posts_job_id", table_name="feed_posts")
    op.drop_index("ix_feed_posts_feed_id", table_name="feed_posts")
    op.drop_table("feed_posts")

