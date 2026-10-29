"""referral_drafts: drafted (and gated-send) referral requests per user per contact

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "referral_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("contact_name", sa.String(), nullable=False, server_default=""),
        sa.Column("contact_headline", sa.String(), nullable=False, server_default=""),
        sa.Column("contact_profile_url", sa.String(), nullable=False, server_default=""),
        sa.Column("channel", sa.String(), nullable=False, server_default="connection_note"),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_referral_drafts_user_id", "referral_drafts", ["user_id"])
    op.create_index("ix_referral_drafts_job_id", "referral_drafts", ["job_id"])
    op.create_index("ix_referral_drafts_status", "referral_drafts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_referral_drafts_status", table_name="referral_drafts")
    op.drop_index("ix_referral_drafts_job_id", table_name="referral_drafts")
    op.drop_index("ix_referral_drafts_user_id", table_name="referral_drafts")
    op.drop_table("referral_drafts")
