"""A real data-loss bug, found while building Phase 4 slice 1 (the AI
sprint summary agent) and fixed here on its own, ahead of that feature's
own migration (0022) -- this fix stands on its own merits independent of
whatever eventually reads the column.

complete_sprint (app/api/sprints.py, migration 0012) auto-returns
unfinished tickets to the backlog by bulk-UPDATEing their sprint_id to
NULL. That was, and still is, the correct behavior for what the sprints
feature originally needed: unfinished work goes back to the backlog,
immediately plannable again. What nobody noticed at the time is that the
UPDATE is also destructive in a way nothing downstream could ever
recover from -- once sprint_id is NULL, there is no remaining query, no
column, no join, that can ever again say "this ticket used to be on
sprint X and didn't finish." A ticket that stayed in the sprint's
terminal column keeps sprint_id pointing at that sprint forever
(deliberately, for velocity history -- see complete_sprint's own
comment); a returned ticket gets no equivalent. This was never a
functional bug against anything the sprints feature itself needed --
the backlog return worked, and still works, exactly as designed. It
became a real bug the moment a later feature (this one) needed to know
what had been returned, and found that history had already been
silently erased for every sprint ever completed before this fix. A
correct-in-isolation change quietly broke a later assumption it had no
way to know about -- the ordinary shape a latent data-loss bug takes.

The fix: capture a snapshot (ticket_number, title, story_points) of
exactly which tickets are about to be returned, BEFORE the bulk UPDATE
runs, in the same transaction complete_sprint already uses. This is the
only correct point to do it -- once the UPDATE commits, the information
needed to build this snapshot is already gone. Structural data, not an
LLM output: captured synchronously and deterministically every time a
sprint completes, regardless of whether anything downstream (migration
0022's retro feature) ever successfully reads it.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sprints", sa.Column("retro_returned_snapshot", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("sprints", "retro_returned_snapshot")
