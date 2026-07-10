"""Sprint planning.

complete_sprint's returned-ticket snapshot (see migration 0021 and its
own docstring for the full story) fixes a real data-loss bug: the bulk
UPDATE below that auto-returns unfinished tickets to the backlog was
always silently destroying the only record of which tickets those were,
for every sprint ever completed. Captured here, ahead of and independent
of whatever eventually reads it.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import Project, Sprint, SprintStatus, Ticket, UserRole, WorkflowState
from app.db.session import get_db
from app.schemas.sprint import SprintAssignRequest, SprintCreate, SprintRead, SprintUpdate
from app.schemas.ticket import TicketRead

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


async def _with_total_points(db: AsyncSession, sprint: Sprint) -> Sprint:
    sprint.total_points = await _total_points(db, sprint.id)  # type: ignore[attr-defined]
    return sprint


async def _with_total_points_many(db: AsyncSession, sprints: list[Sprint]) -> list[Sprint]:
    if not sprints:
        return sprints
    # One grouped query for every sprint on the page, not one query per
    # sprint — the same N+1 concern _next_position/_validate_* helpers
    # elsewhere in this app are written to avoid.
    result = await db.execute(
        select(Ticket.sprint_id, func.coalesce(func.sum(Ticket.story_points), 0))
        .where(Ticket.sprint_id.in_([s.id for s in sprints]), Ticket.deleted_at.is_(None))
        .group_by(Ticket.sprint_id)
    )
    totals = dict(result.all())
    for sprint in sprints:
        sprint.total_points = int(totals.get(sprint.id, 0))  # type: ignore[attr-defined]
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
    await db.commit()
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
