"""Phase 4, slice 1: the AI sprint summary agent. A sprint's retro state,
nullable enum from the start (pending/completed/failed) -- the same
appsec_review_status lesson (migration 0020) applied up front rather than
relearned: NULL means "this sprint hasn't completed yet, there's nothing
to summarize," a real and permanent state for every planned/active
sprint, not a transient "hasn't started" the old nullable-timestamp idiom
would have conflated with "in progress."

retro_narrative/retro_completed_highlights/retro_incomplete_notes/
retro_risks are the LLM's structured output, stored as separate columns
(narrative as plain text, the three lists as native Postgres arrays of
short strings) rather than one JSONB blob -- same shape as AppSec's
appsec_comment + appsec_categories split (migration 0020), for the same
reason: these are independently useful, independently nullable-until-
generated fields, not an opaque document there's no reason to query into.

retro_error/retro_generated_at mirror appsec_review_error/
appsec_reviewed_at exactly: visibility into why AI-generated content is
missing, and a genuine completion timestamp separate from the status
enum itself.

Builds on retro_returned_snapshot (migration 0021) rather than adding it
here -- that column fixes a real, standalone data-loss bug in
complete_sprint's existing bulk UPDATE, worth its own migration and its
own commit ahead of this feature; see 0021's own docstring for the full
story. This migration only adds what's specific to the AI retro itself:
the generated content and its status, nothing this feature doesn't
strictly need.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE sprint_retro_status AS ENUM ('pending', 'completed', 'failed')")

    op.add_column(
        "sprints",
        sa.Column(
            "retro_status",
            postgresql.ENUM("pending", "completed", "failed", name="sprint_retro_status", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("sprints", sa.Column("retro_narrative", sa.String, nullable=True))
    op.add_column("sprints", sa.Column("retro_completed_highlights", postgresql.ARRAY(sa.String), nullable=True))
    op.add_column("sprints", sa.Column("retro_incomplete_notes", postgresql.ARRAY(sa.String), nullable=True))
    op.add_column("sprints", sa.Column("retro_risks", postgresql.ARRAY(sa.String), nullable=True))
    op.add_column("sprints", sa.Column("retro_error", sa.String, nullable=True))
    op.add_column("sprints", sa.Column("retro_generated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sprints", "retro_generated_at")
    op.drop_column("sprints", "retro_error")
    op.drop_column("sprints", "retro_risks")
    op.drop_column("sprints", "retro_incomplete_notes")
    op.drop_column("sprints", "retro_completed_highlights")
    op.drop_column("sprints", "retro_narrative")
    op.drop_column("sprints", "retro_status")
    op.execute("DROP TYPE sprint_retro_status")
