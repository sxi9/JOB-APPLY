"""notified events: the target kind instead of the number / group jid, no duplicates, and a unique index
(application_id, detail) for kind='notified' so a new-job alert batch is claimed before the send

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # older rows carried the raw target ("whatsapp:+91..." / "whatsapp:...@g.us"): keep the kind only
    op.execute("UPDATE application_events SET detail = 'whatsapp:group' "
               "WHERE kind = 'notified' AND detail LIKE 'whatsapp:%@g.us'")
    op.execute("UPDATE application_events SET detail = 'whatsapp:number' "
               "WHERE kind = 'notified' AND detail LIKE 'whatsapp:%' "
               "AND detail NOT IN ('whatsapp:number', 'whatsapp:group')")
    # duplicates from overlapping runs: keep the first row per application and target kind
    op.execute("DELETE FROM application_events WHERE kind = 'notified' AND id NOT IN "
               "(SELECT MIN(id) FROM application_events WHERE kind = 'notified' GROUP BY application_id, detail)")
    op.create_index("ux_application_events_notified", "application_events", ["application_id", "detail"],
                    unique=True, sqlite_where=sa.text("kind = 'notified'"), postgresql_where=sa.text("kind = 'notified'"))


def downgrade() -> None:
    op.drop_index("ux_application_events_notified", table_name="application_events")
