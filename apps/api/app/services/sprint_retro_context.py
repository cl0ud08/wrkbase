"""Assembles the read-only context bundle a sprint retro's LLM call is
built from — pure data gathering, no LLM involved (see
app/services/sprint_retro.py for that half), so this can be tested and
reasoned about completely independently of the model call itself.

--- What's honestly available here, checked against the real schema and
    complete_sprint's real behavior, not assumed from the original
    Phase 4 roadmap ------------------------------------------------------

The roadmap for this slice implicitly assumed a retro could draw on
ticket state-change history — when a ticket moved into the sprint, how
long it sat in progress, that kind of thing. That data does not exist
anywhere in this codebase. There is no audit/activity/event-log table at
all (checked directly against app/db/models.py, not assumed absent).
Ticket.updated_at is a single blanket "last touched" timestamp covering
any field change whatsoever, not a per-transition record — there is no
way to answer "when did this ticket enter this sprint" or "how long was
it in progress" from anything this app persists today. The two
timestamp-shaped fields Ticket does have (triaged_at, appsec_reviewed_at)
are narrow AI-pipeline fields unrelated to board/sprint movement.

There is also still no ticket-comment or activity-log system (the same
gap the AI Security Champion slice already found and worked around) — so
there's no qualitative "what actually happened during the sprint" input
beyond each ticket's own title and story points, and where it ended up.

What genuinely IS durable: a ticket that ends the sprint in the
project's terminal workflow column keeps sprint_id pointing at this
sprint forever (see complete_sprint in app/api/sprints.py) — that's
recoverable from a live query at any time, no snapshot needed. What is
NOT durable: complete_sprint's bulk UPDATE nulls sprint_id for every
ticket returned to the backlog, which destroys the only link between
that ticket and the sprint it was kicked out of. There is nothing left
to reconstruct that from later. So complete_sprint captures a snapshot
of exactly which tickets are about to be returned (ticket_number, title,
story_points) BEFORE running that update, and persists it onto the
sprint row as retro_returned_snapshot — this module reads that snapshot
rather than trying (and failing) to reconstruct it live.

Net result: this context bundle is honestly thinner than the roadmap
assumed — a structural summary of what finished and what didn't, by
title and points, with no timeline and no qualitative history — not the
richer "how the sprint unfolded" narrative a real activity log would
have supported. Designed around what's real, not padded out with
fabricated structure.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Sprint, Ticket


@dataclass(frozen=True)
class TicketSnapshot:
    ticket_number: int
    title: str
    story_points: int | None


@dataclass(frozen=True)
class SprintRetroContext:
    sprint_name: str
    sprint_goal: str | None
    start_date: str
    end_date: str
    completed: list[TicketSnapshot]
    returned: list[TicketSnapshot]
    points_done: int
    points_planned: int


async def assemble_retro_context(db: AsyncSession, sprint: Sprint) -> SprintRetroContext:
    """sprint must already be COMPLETED, with retro_returned_snapshot
    already populated by complete_sprint at the moment it completed (see
    module docstring for why that snapshot can't be reconstructed here
    instead).

    No terminal-workflow-state lookup is needed here, deliberately: by
    the time a sprint is COMPLETED, complete_sprint's bulk UPDATE has
    already nulled sprint_id for every non-terminal ticket, so EVERY
    ticket still pointing at this sprint_id is, by construction, one
    that finished — a plain `sprint_id = :id` query is enough, with no
    need to re-derive or trust the project's current terminal column
    (which could theoretically have been reconfigured since completion;
    an accepted, extremely unlikely edge case, not engineered around
    here since it would only affect a live query this function doesn't
    even run).
    """
    completed_result = await db.execute(
        select(Ticket.ticket_number, Ticket.title, Ticket.story_points).where(
            Ticket.sprint_id == sprint.id,
            Ticket.deleted_at.is_(None),
        )
    )
    completed = [TicketSnapshot(*row) for row in completed_result.all()]
    points_done = sum(t.story_points or 0 for t in completed)

    returned = [
        TicketSnapshot(
            ticket_number=item["ticket_number"],
            title=item["title"],
            story_points=item["story_points"],
        )
        for item in (sprint.retro_returned_snapshot or [])
    ]
    points_returned = sum(t.story_points or 0 for t in returned)

    return SprintRetroContext(
        sprint_name=sprint.name,
        sprint_goal=sprint.goal,
        start_date=sprint.start_date.isoformat(),
        end_date=sprint.end_date.isoformat(),
        completed=completed,
        returned=returned,
        points_done=points_done,
        points_planned=points_done + points_returned,
    )
