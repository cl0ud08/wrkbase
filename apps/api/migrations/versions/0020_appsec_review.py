"""Phase 3, slice 5: the AI Security Champion. A ticket's security review
state, deliberately shaped as a nullable enum from the start rather than
the nullable-timestamp idiom Slice 26 originally used for triage_status
and later had to replace — that migration's own lesson, applied here up
front instead of relearned: appsec_review_status has three real states
(pending, completed, failed) the moment a trigger category matches, plus
the "never applicable" state of the column being NULL for the vast
majority of tickets that never match any category at all. A two-value
nullable-timestamp idiom couldn't express "flagged, but the LLM call
failed" as distinct from "flagged, still pending" any more cleanly here
than it could for triage.

appsec_categories mirrors labels' own shape (migration 0018's tradeoff:
a native Postgres array of short strings, not JSONB, not a join table —
these are a handful of fixed category keys with no independent identity
of their own, the same reasoning that already applied to LLM-suggested
labels applies again here). appsec_comment is the LLM-generated,
ticket-specific security note — see app/services/appsec_review.py for
why this exists as a plain field rather than a ticket comment: this
project has no comment system (checked directly against the models,
migrations, and API routes, not assumed absent — see this migration's
own reasoning file), the same gap triage_reasoning already worked around
in migration 0018.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE appsec_review_status AS ENUM ('pending', 'completed', 'failed')")

    # Nullable, unlike triage_status: NULL is a real, common, permanent
    # state here -- "no trigger category has ever matched this ticket" --
    # not a transient "hasn't started yet" the way pending_triage was.
    # Most tickets will stay NULL forever; that's the design working as
    # intended; see appsec_triggers.py for why this is a deliberately
    # narrow, keyword-gated set, not "review every ticket."
    op.add_column(
        "tickets",
        sa.Column(
            "appsec_review_status",
            postgresql.ENUM("pending", "completed", "failed", name="appsec_review_status", create_type=False),
            nullable=True,
        ),
    )
    # Which trigger categories matched -- set synchronously, deterministically,
    # the moment a category matches (see app/api/tickets.py), independent of
    # whether the async LLM call that writes appsec_comment below ever
    # succeeds. A ticket is flagged the instant a keyword matches; the LLM
    # only ever enriches that flag with tailored guidance, never decides
    # whether it applies in the first place.
    op.add_column("tickets", sa.Column("appsec_categories", postgresql.ARRAY(sa.String), nullable=True))
    # The LLM-generated, ticket-specific security note -- NULL until
    # appsec_review_status = 'completed'.
    op.add_column("tickets", sa.Column("appsec_comment", sa.String, nullable=True))
    # Set only when appsec_review_status = 'failed' -- both providers were
    # tried and exhausted their retry budgets (see app/services/llm_client.py
    # and worker/main.py). The ticket stays flagged either way -- this is
    # visibility into why the AI-generated guidance specifically is
    # missing, not a reason to un-flag a real security concern.
    op.add_column("tickets", sa.Column("appsec_review_error", sa.String, nullable=True))
    # A genuine completion timestamp, set once review_status leaves
    # 'pending' -- same role as triaged_at, not a second copy of the
    # status itself.
    op.add_column("tickets", sa.Column("appsec_reviewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "appsec_reviewed_at")
    op.drop_column("tickets", "appsec_review_error")
    op.drop_column("tickets", "appsec_comment")
    op.drop_column("tickets", "appsec_categories")
    op.drop_column("tickets", "appsec_review_status")
    op.execute("DROP TYPE appsec_review_status")
