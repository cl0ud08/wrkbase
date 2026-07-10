"""Sprint planning, plus complete_sprint's sprint-retro triggering (Phase
4, slice 1 — see app/services/sprint_retro.py and worker/main.py for the
rest).

complete_sprint's returned-ticket snapshot (migration 0021, own commit)
fixed a real, standalone data-loss bug ahead of this feature: the bulk
UPDATE below that auto-returns unfinished tickets to the backlog was
always silently destroying the only record of which tickets those were,
for every sprint ever completed. This feature is the first thing that
actually reads that snapshot.

--- Sync or async? ---------------------------------------------------

Reasoned fresh, not assumed from precedent: a user clicking "Complete
sprint" could plausibly resemble either of this app's two existing
patterns. It's superficially like NL ticket parsing (POST .../parse) —
a single deliberate click, waiting for *something* to come back. But
what actually determined sync-vs-async in this app both previous times
was never "is the user waiting" in the abstract — it was whether the
*next thing the user needs to do* is gated on the result. NL parse is
sync because the user cannot proceed (review, edit, submit) without the
parsed fields; there is no version of that flow where returning
immediately and filling the ticket in later makes sense.

Ending a sprint has no such gate. The action the user actually wants —
the sprint ends, unfinished work returns to the backlog, the board
reflects it — is fully complete the instant complete_sprint's own
transaction commits, with nothing about a retro's content required to
make that true or usable. Nobody is blocked on retro text to do their
next real task (start the next sprint, re-plan the backlog); the retro
is something they'll likely open and read once, not a value the UI
needs synchronously to render the page correctly. That makes this
async, the same conclusion triage/embedding/AppSec reached — but for a
sprint-specific reason, not by assuming the answer transfers.

Two more reasons specific to this endpoint, not just "matches triage":

1. complete_sprint is the one place in this app that mutates real
   planning state for a whole team (every unfinished ticket's
   sprint_id) inside a single transaction. Coupling that to an LLM
   call's latency (or a provider outage) would mean a flaky/slow
   external API can make ending a sprint itself slow or fail — a far
   worse failure mode than a slow triage or missing AppSec comment,
   because it blocks real team workflow (nobody can start the next
   sprint) rather than just delaying an enrichment.
2. The retro's own input (retro_returned_snapshot) is only fully known
   at the exact moment complete_sprint's bulk UPDATE runs — so even if
   this were made synchronous, it would still have to happen inside or
   immediately after that same transaction, gaining nothing over firing
   it as a background job the same instant.

--- Fourth queue, or does this share a failure mode with something that
    already exists? -------------------------------------------------

Decided the same way triage vs. embedding vs. AppSec was: SprintRetroJob
does NOT share a queue with any of the three existing job types, because
it shares neither their failure characteristics nor their trigger
entity. It's the first job scoped to a Sprint rather than a Ticket — a
different consumer, a different payload shape, and a different DB row
to write the outcome onto. It also doesn't share fate with any of the
other three: an LLM outage failing a sprint retro has nothing to do with
whether some ticket's triage, embedding, or AppSec review succeeded, so
folding it into any existing queue would recreate the exact ack/nack
coupling problem worker/main.py's own module docstring already explains
for why triage/embed/AppSec are three queues, not one — a fourth
independent queue is the same conclusion, reapplied on its own terms
rather than inherited by default. See worker/main.py and
app/services/queue.py's SprintRetroJob for the rest, including why the
job payload still stays "trigger, not snapshot" despite the data being
genuinely transient this time.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import (
    Project,
    Sprint,
    SprintRetroStatus,
    SprintStatus,
    Ticket,
    UserRole,
    WorkflowState,
)
from app.db.session import get_db
from app.schemas.sprint import SprintAssignRequest, SprintCreate, SprintRead, SprintUpdate
from app.schemas.ticket import TicketRead
from app.services.at_risk import assess_ticket_risk, workflow_state_bounds
from app.services.queue import SprintRetroJob, publish_sprint_retro_job

router = APIRouter(prefix="/projects/{project_id}/sprints", tags=["sprints"])

_PROJECT_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
_SPRINT_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprint not found")


async def _get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise _PROJECT_NOT_FOUND
    return project


async def _get_sprint_or_404(db: AsyncSession, project_id: uuid.UUID, sprint_id: uuid.UUID) -> Sprint:
    result = await db.execute(
        select(Sprint).where(Sprint.id == sprint_id, Sprint.project_id == project_id)
    )
    sprint = result.scalar_one_or_none()
    if sprint is None:
        raise _SPRINT_NOT_FOUND
    return sprint


def _require_admin(auth: AuthContext) -> None:
    # Same class of resource as WorkflowState, not Project/Ticket: a sprint
    # has no creator concept (see SprintCreate — deliberately no
    # created_by), and defining/retiring sprints reshapes shared planning
    # structure for the whole team, not a personal resource one member
    # owns. Moving tickets INTO a sprint (assign_tickets below, and
    # sprint_id on a plain ticket PATCH) stays open to any member — that's
    # everyday planning work, the same split workflow_state_id already
    # draws between "who can define a column" and "who can drag a card
    # into one."
    if auth.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only an org admin can manage sprints"
        )


async def _total_points(db: AsyncSession, sprint_id: uuid.UUID) -> int:
    # SUM ignores NULLs on its own (an unestimated ticket contributes 0,
    # not an error and not treated as a real zero-point estimate) —
    # COALESCE only guards the "no tickets at all" case, where SUM itself
    # returns NULL rather than 0.
    result = await db.execute(
        select(func.coalesce(func.sum(Ticket.story_points), 0)).where(
            Ticket.sprint_id == sprint_id, Ticket.deleted_at.is_(None)
        )
    )
    return int(result.scalar_one())


def _points_planned(sprint: Sprint) -> int | None:
    # Only meaningful once a sprint has actually completed: before that,
    # nothing has been returned yet, so "planned" and "current total"
    # are the same number and showing both would be redundant noise.
    # total_points must already be bolted onto sprint (see callers below)
    # -- this is current points-done (only terminal tickets still carry
    # sprint_id post-completion, see complete_sprint) plus whatever was
    # captured in retro_returned_snapshot at the moment of completion,
    # the only record of what didn't make it -- see
    # app/services/sprint_retro_context.py for the full reasoning on why
    # that snapshot, not a live query, is the source for the second half.
    if sprint.status != SprintStatus.COMPLETED:
        return None
    returned_points = sum(
        (item.get("story_points") or 0) for item in (sprint.retro_returned_snapshot or [])
    )
    return sprint.total_points + returned_points  # type: ignore[attr-defined]


async def _at_risk_count(db: AsyncSession, sprint: Sprint) -> int | None:
    # NULL for a planned/completed sprint -- see app/services/at_risk.py's
    # module docstring for why a risk assessment only means anything
    # relative to the ONE currently-active sprint's own deadline. Short-
    # circuits before any query for every non-active sprint, so calling
    # this for every sprint in a list (see _with_total_points_many) costs
    # nothing extra beyond the at-most-one active sprint actually present.
    if sprint.status != SprintStatus.ACTIVE:
        return None
    tickets_result = await db.execute(
        select(Ticket).where(Ticket.sprint_id == sprint.id, Ticket.deleted_at.is_(None))
    )
    tickets = list(tickets_result.scalars().all())
    if not tickets:
        return 0
    earliest_state_id, terminal_state_id = await workflow_state_bounds(db, sprint.project_id)
    now = datetime.now(timezone.utc)
    today = now.date()
    return sum(
        1
        for ticket in tickets
        if assess_ticket_risk(
            ticket,
            sprint_end_date=sprint.end_date,
            today=today,
            now=now,
            earliest_state_id=earliest_state_id,
            terminal_state_id=terminal_state_id,
        ).at_risk
    )


async def _with_total_points(db: AsyncSession, sprint: Sprint) -> Sprint:
    sprint.total_points = await _total_points(db, sprint.id)  # type: ignore[attr-defined]
    sprint.points_planned = _points_planned(sprint)  # type: ignore[attr-defined]
    sprint.at_risk_count = await _at_risk_count(db, sprint)  # type: ignore[attr-defined]
    return sprint


async def _with_total_points_many(db: AsyncSession, sprints: list[Sprint]) -> list[Sprint]:
    if not sprints:
        return sprints
    # One grouped query for every sprint on the page, not one query per
    # sprint — the same N+1 concern _next_position/_validate_* helpers
    # elsewhere in this app are written to avoid. (at_risk_count doesn't
    # get the same batching -- see _at_risk_count's own comment on why
    # that's fine: it's a no-op query for every sprint except the one
    # that's actually active.)
    result = await db.execute(
        select(Ticket.sprint_id, func.coalesce(func.sum(Ticket.story_points), 0))
        .where(Ticket.sprint_id.in_([s.id for s in sprints]), Ticket.deleted_at.is_(None))
        .group_by(Ticket.sprint_id)
    )
    totals = dict(result.all())
    for sprint in sprints:
        sprint.total_points = int(totals.get(sprint.id, 0))  # type: ignore[attr-defined]
        sprint.points_planned = _points_planned(sprint)  # type: ignore[attr-defined]
        sprint.at_risk_count = await _at_risk_count(db, sprint)  # type: ignore[attr-defined]
    return sprints


async def _has_assigned_tickets(db: AsyncSession, sprint_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(exists().where(Ticket.sprint_id == sprint_id, Ticket.deleted_at.is_(None)))
    )
    return bool(result.scalar())


async def _terminal_workflow_state_id(db: AsyncSession, project_id: uuid.UUID) -> uuid.UUID | None:
    # Heuristic, not a guarantee: this app has no explicit is_done/terminal
    # flag on WorkflowState, so "the column with the highest `order`" is
    # used as a stand-in for "done" — true for the default seeded workflow
    # and any project that hasn't added columns after its real done state,
    # but not a real invariant. The correct fix is a dedicated terminal
    # flag on WorkflowState; deferred as a known gap for the same reason
    # project archiving was deferred after soft-delete — this slice is
    # about sprints, not redesigning workflow states.
    result = await db.execute(
        select(WorkflowState.id)
        .where(WorkflowState.project_id == project_id)
        .order_by(WorkflowState.order.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.post("", response_model=SprintRead, status_code=status.HTTP_201_CREATED)
async def create_sprint(
    project_id: uuid.UUID,
    payload: SprintCreate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Sprint:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)

    sprint = Sprint(
        org_id=auth.org_id,
        project_id=project_id,
        name=payload.name,
        goal=payload.goal,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    db.add(sprint)
    await db.commit()
    return await _with_total_points(db, sprint)


@router.get("", response_model=list[SprintRead])
async def list_sprints(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[Sprint]:
    await _get_project_or_404(db, project_id)
    result = await db.execute(
        select(Sprint).where(Sprint.project_id == project_id).order_by(Sprint.start_date)
    )
    sprints = list(result.scalars().all())
    return await _with_total_points_many(db, sprints)


@router.get("/{sprint_id}", response_model=SprintRead)
async def get_sprint(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Sprint:
    await _get_project_or_404(db, project_id)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)
    return await _with_total_points(db, sprint)


@router.patch("/{sprint_id}", response_model=SprintRead)
async def update_sprint(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    payload: SprintUpdate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Sprint:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(sprint, field, value)

    await db.commit()
    return await _with_total_points(db, sprint)


@router.delete("/{sprint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sprint(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)

    # Only a sprint that was never started can be deleted outright — once
    # active or completed, it's real project history (what was planned,
    # what actually happened), not a draft to throw away. Blocked
    # separately if it still has tickets on it, same shape as the
    # workflow-state delete's children check: unassign first, then delete.
    if sprint.status != SprintStatus.PLANNED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a planned (not yet started) sprint can be deleted",
        )
    if await _has_assigned_tickets(db, sprint_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a sprint that still has tickets assigned to it",
        )

    await db.delete(sprint)
    await db.commit()


@router.post("/{sprint_id}/start", response_model=SprintRead)
async def start_sprint(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Sprint:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)

    if sprint.status != SprintStatus.PLANNED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only a planned sprint can be started"
        )

    sprint.status = SprintStatus.ACTIVE
    try:
        await db.commit()
    except IntegrityError:
        # uq_sprints_one_active_per_project (migration 0012) firing: the
        # DB-enforced single-active-sprint rule, not an app-layer check —
        # this is the only place that race can actually surface, and it's
        # a real race (see the migration's comment and
        # scripts/verify_sprints_rls.py's concurrent-start test).
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another sprint in this project is already active",
        )
    return await _with_total_points(db, sprint)


@router.post("/{sprint_id}/complete", response_model=SprintRead)
async def complete_sprint(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Sprint:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)

    if sprint.status != SprintStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only an active sprint can be completed"
        )

    # Auto-return unfinished work to the backlog (sprint_id = NULL) so it's
    # immediately eligible for the next sprint's planning, without needing
    # a person to notice and move it manually. Tickets already in the
    # project's terminal column keep sprint_id pointing at this sprint —
    # that's deliberate: it's the only record of what this sprint actually
    # finished, useful for velocity later, and GET .../tickets/backlog
    # excludes anything with a non-NULL sprint_id regardless of that
    # sprint's status, so a completed ticket never reappears in the
    # backlog just because its sprint is done.
    conditions = [Ticket.sprint_id == sprint_id, Ticket.deleted_at.is_(None)]
    terminal_state_id = await _terminal_workflow_state_id(db, project_id)
    if terminal_state_id is not None:
        conditions.append(Ticket.workflow_state_id != terminal_state_id)

    # Captured BEFORE the bulk UPDATE below, not after: once that UPDATE
    # runs, sprint_id is NULL on every one of these tickets and there is
    # no query that can ever again tell you they were on this sprint at
    # all. This used to just happen with nothing capturing it first — see
    # migration 0021's own docstring for the full data-loss-bug story
    # this fixes.
    returned_result = await db.execute(
        select(Ticket.ticket_number, Ticket.title, Ticket.story_points).where(*conditions)
    )
    sprint.retro_returned_snapshot = [
        {"ticket_number": number, "title": title, "story_points": points}
        for number, title, points in returned_result.all()
    ]

    await db.execute(update(Ticket).where(*conditions).values(sprint_id=None))

    sprint.status = SprintStatus.COMPLETED
    # Set in the same transaction as status=COMPLETED, so a completed
    # sprint is never observably retro_status=NULL — see
    # app/services/sprint_retro.py and worker/main.py for what happens
    # next. Retro generation runs async and never blocks or reverses
    # this response: nobody clicking "Complete sprint" is waiting on an
    # AI summary to see their sprint actually end, and an LLM outage
    # should never be able to make ending a sprint fail. See this
    # module's own docstring for the full sync-vs-async and queue
    # reasoning.
    sprint.retro_status = SprintRetroStatus.PENDING
    await db.commit()

    await publish_sprint_retro_job(SprintRetroJob(sprint_id=sprint.id, org_id=sprint.org_id))

    return await _with_total_points(db, sprint)


@router.post("/{sprint_id}/retro/regenerate", response_model=SprintRead, status_code=status.HTTP_202_ACCEPTED)
async def regenerate_retro(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Sprint:
    """Re-fires the retro LLM call for an already-completed sprint.

    Does this need guarding? The tickets a completed sprint's retro is
    built from can never actually change after the fact: there is no
    "reopen a completed sprint" path anywhere in this app (SprintUpdate
    deliberately excludes status — see app/schemas/sprint.py — and
    start_sprint/complete_sprint each only accept one specific prior
    status), so retro_returned_snapshot and the set of tickets still
    pointing at this sprint_id are permanently frozen the moment
    complete_sprint commits. What CAN still change post-completion is
    the sprint's own name/goal (SprintUpdate allows both regardless of
    status) — which could leave a previously-generated retro's prose
    referencing a goal that's since been reworded. That's a real but
    cosmetic staleness, not a data problem, and not disproportionate
    enough to justify auto-regenerating (and re-paying for an LLM call)
    on every such edit — a manual regenerate action is enough.
    The guard that IS worth having: reject a regenerate call while one
    is already in flight (retro_status still PENDING), so two concurrent
    SprintRetroJobs for the same sprint don't race to write conflicting
    results into the same row — a wasted duplicate LLM call for no
    benefit, not a correctness bug, but cheap to just prevent outright.
    """
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)

    if sprint.status != SprintStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only a completed sprint has a retro to regenerate",
        )
    if sprint.retro_status == SprintRetroStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A retro is already generating for this sprint",
        )

    sprint.retro_status = SprintRetroStatus.PENDING
    await db.commit()

    await publish_sprint_retro_job(SprintRetroJob(sprint_id=sprint.id, org_id=sprint.org_id))

    return await _with_total_points(db, sprint)


@router.post("/{sprint_id}/assign", response_model=list[TicketRead])
async def assign_tickets(
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    payload: SprintAssignRequest,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[Ticket]:
    await _get_project_or_404(db, project_id)
    sprint = await _get_sprint_or_404(db, project_id, sprint_id)
    if sprint.status == SprintStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot assign tickets into a completed sprint"
        )

    # Every id has to be a real, currently-backlog ticket in this same
    # project — same "validate up front, fail as one unit" shape as
    # _validate_parent, rather than silently skipping ids that don't
    # qualify and leaving the caller to guess which ones actually moved.
    ticket_ids = set(payload.ticket_ids)
    result = await db.execute(
        select(Ticket).where(
            Ticket.id.in_(ticket_ids),
            Ticket.project_id == project_id,
            Ticket.deleted_at.is_(None),
            Ticket.sprint_id.is_(None),
        )
    )
    tickets = list(result.scalars().all())
    if {t.id for t in tickets} != ticket_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every ticket_id must be an existing, currently-backlog ticket in this project",
        )

    for ticket in tickets:
        ticket.sprint_id = sprint_id
    await db.commit()
    return tickets
