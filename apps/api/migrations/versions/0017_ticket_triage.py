"""Phase 3, slice 1: async ticket triage plumbing. priority and triaged_at
on tickets — no new RLS needed, both are plain nullable columns on an
already-RLS-protected table. triaged_at IS NULL is "pending_triage",
the same nullable-timestamp-as-state idiom already used throughout this
app (accepted_at, read_at, used_at) rather than a new status enum: there's
nothing a third state would need to distinguish yet (see
app/services/queue.py and worker/main.py for why a genuine failure case
still doesn't need one in this slice).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE ticket_priority AS ENUM ('low', 'medium', 'high', 'critical')")

    op.add_column(
        "tickets",
        sa.Column(
            "priority",
            postgresql.ENUM(
                "low", "medium", "high", "critical", name="ticket_priority", create_type=False
            ),
            nullable=True,
        ),
    )
    # NULL = pending_triage (set on creation, before the worker ever
    # touches the row); non-NULL = triaged, set once by worker/main.py
    # alongside priority. Not settable via TicketCreate/TicketUpdate —
    # only the worker writes this column.
    op.add_column("tickets", sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "triaged_at")
    op.drop_column("tickets", "priority")
    op.execute("DROP TYPE ticket_priority")
