"""Phase 3, slice 2: a real LLM call replaces the hardcoded placeholder
priority. This is the moment migration 0017's own docstring predicted —
"there's nothing a third state would need to distinguish yet" — a genuine
third state now exists: an LLM call can fail (both providers exhausted),
and that has to be visible and distinct from still-pending, not silently
indistinguishable from it. triage_status replaces triaged_at IS NULL as
the authoritative state signal; triaged_at is kept (still nullable, still
set only on success) as a real "when did this complete" timestamp, not a
state flag anymore.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE TYPE triage_status AS ENUM ('pending', 'triaged', 'failed')")

    op.add_column(
        "tickets",
        sa.Column(
            "triage_status",
            postgresql.ENUM("pending", "triaged", "failed", name="triage_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
    )
    # A native Postgres array of short strings, not JSONB and not a join
    # table. JSONB would be strictly more flexible (richer label objects
    # later) but there's nothing today that needs more than "a few short
    # strings" — that flexibility would sit unused. A proper Label +
    # TicketLabel join table is the right shape *if* labels ever become an
    # org-wide managed taxonomy (rename everywhere, assign colors,
    # autocomplete across tickets) — a real, much bigger feature nobody
    # has asked for yet. These are LLM-suggested free-text labels with no
    # independent identity; a plain array is the honest match for that,
    # not a placeholder for a feature that may never get built.
    op.add_column("tickets", sa.Column("labels", postgresql.ARRAY(sa.String), nullable=True))
    # One sentence explaining the priority the LLM chose -- so a human can
    # see *why*, not just trust the result. No comment system exists in
    # this app to attach this to (checked, not assumed), so it's a plain
    # field on the ticket, same shape as labels/priority: nullable, set
    # once, only on success.
    op.add_column("tickets", sa.Column("triage_reasoning", sa.String, nullable=True))
    # Set only when triage_status = 'failed' -- both providers were tried
    # and exhausted their retry budgets (see worker/main.py). Real
    # visibility into *why* a ticket is stuck, not a silent dead end.
    op.add_column("tickets", sa.Column("triage_error", sa.String, nullable=True))

    # Backfill: rows already triaged under the previous slice's hardcoded-
    # placeholder worker have triaged_at set but would otherwise default to
    # triage_status='pending' under the new column -- that's a genuine
    # regression for existing data, not just a cosmetic gap, since the
    # board would show "AI triaging…" forever for tickets that already
    # finished. Anything with triaged_at set already succeeded.
    bind.execute(sa.text("UPDATE tickets SET triage_status = 'triaged' WHERE triaged_at IS NOT NULL"))


def downgrade() -> None:
    op.drop_column("tickets", "triage_error")
    op.drop_column("tickets", "triage_reasoning")
    op.drop_column("tickets", "labels")
    op.drop_column("tickets", "triage_status")
    op.execute("DROP TYPE triage_status")
