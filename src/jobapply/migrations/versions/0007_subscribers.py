"""subscribers (WhatsApp bot subscriptions) and subscriber_posts (which job each subscriber already got)

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscribers",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("phone_jid", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("fields", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("location", sa.String(), nullable=False, server_default=""),
        sa.Column("remote_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="whatsapp"),
    )
    op.create_index("ix_subscribers_phone_jid", "subscribers", ["phone_jid"], unique=True)
    op.create_table(
        "subscriber_posts",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("subscriber_id", sa.Integer(), sa.ForeignKey("subscribers.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("subscriber_id", "job_id", name="uq_subscriber_posts_subscriber_id_job_id"),
    )
    op.create_index("ix_subscriber_posts_subscriber_id", "subscriber_posts", ["subscriber_id"])
    op.create_index("ix_subscriber_posts_job_id", "subscriber_posts", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_subscriber_posts_job_id", table_name="subscriber_posts")
    op.drop_index("ix_subscriber_posts_subscriber_id", table_name="subscriber_posts")
    op.drop_table("subscriber_posts")
    op.drop_index("ix_subscribers_phone_jid", table_name="subscribers")
    op.drop_table("subscribers")
