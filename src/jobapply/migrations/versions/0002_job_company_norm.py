"""jobs.company_norm for dedupe candidate lookup

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("company_norm", sa.String(), nullable=False, server_default=""))
        batch.create_index("ix_jobs_company_norm", ["company_norm"])


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_company_norm")
        batch.drop_column("company_norm")
