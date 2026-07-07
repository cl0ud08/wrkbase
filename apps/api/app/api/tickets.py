import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import Project, Ticket, TicketType, User, UserRole, WorkflowState
from app.db.session import get_db
from app.schemas.ticket import TicketCreate, TicketRead, TicketTreeNode, TicketUpdate

router = APIRouter(prefix="/projects/{project_id}/tickets", tags=["tickets"])

_PROJECT_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
_TICKET_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

# Fields that make up "core board interaction" (drag a card to a column /
# reorder it / assign it to someone) rather than a content edit. See
# _require_owner_or_admin's call sites: moving a card is everyday board
# collaboration — restricting it to the ticket's creator or an admin would
# mean only Alice could ever drag her own cards to Done. Assigning a
# ticket is the same kind of action: picking up an unowned ticket, or
# reassigning one to a teammate, is normal team behavior that shouldn't
# require being the creator or an admin — the same reasoning Jira/Linear
# apply by not gating "assign" behind an ownership check either. Editing
# what a ticket actually says (title/description/type/parent) stays
# creator-or-admin, same as Projects. A request touching both a
# collaborative field and a content field is treated as a content edit —
# the stricter rule wins.
_COLLABORATIVE_FIELDS = {"workflow_state_id", "position", "assignee_id"}


async def _get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    # Same shape as projects.py's helper: RLS already scopes this to the
    # caller's org, so a project id from another org is indistinguishable
    # from one that doesn't exist.
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise _PROJECT_NOT_FOUND
    return project


async def _get_ticket_or_404(db: AsyncSession, project_id: uuid.UUID, ticket_id: uuid.UUID) -> Ticket:
    # Scoped by project_id, not just id: a ticket id that's real but
    # belongs to a *different* project in the same org must also 404, not
    # leak across projects within one tenant.
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.project_id == project_id)
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise _TICKET_NOT_FOUND
    return ticket


def _require_owner_or_admin(ticket: Ticket, auth: AuthContext) -> None:
    # Same rule as Projects, no divergence: tickets don't yet have a
    # distinct assignee/reporter concept that would justify different
    # permissions — that's future work, same as per-project sharing.
    if ticket.created_by != auth.user_id and auth.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the ticket's creator or an org admin can modify it",
        )


async def _validate_parent(
    db: AsyncSession, project_id: uuid.UUID, ticket_type: TicketType, parent_id: uuid.UUID | None
) -> None:
    if parent_id is None:
        return

    # The composite FK (migration 0005) already guarantees, at the DB
    # level, that IF this insert/update succeeds, parent_id really is a
    # ticket in this same project. This check runs first anyway so a bad
    # parent_id gets a clean 422 with a real message instead of surfacing
    # as a raw IntegrityError.
    result = await db.execute(
        select(Ticket).where(Ticket.id == parent_id, Ticket.project_id == project_id)
    )
    parent = result.scalar_one_or_none()
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="parent_id must reference an existing ticket in the same project",
        )

    # The one rule the brief actually asked for: a subtask's parent must
    # be a story or task, never an epic or another subtask. This is a
    # type-conditional business rule, not a tenant/scope boundary, so it
    # lives here rather than as a DB constraint — see migration 0005's
    # closing comment for why that split makes sense.
    if ticket_type == TicketType.SUBTASK and parent.type not in (TicketType.STORY, TicketType.TASK):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A subtask's parent must be a story or task, not an epic or another subtask",
        )


async def _validate_workflow_state(
    db: AsyncSession, project_id: uuid.UUID, workflow_state_id: uuid.UUID
) -> WorkflowState:
    # Same shape as _validate_parent: the composite FK (migration 0006)
    # already guarantees this at the DB level, but checking first turns a
    # bad id into a clean 422 instead of a raw IntegrityError.
    result = await db.execute(
        select(WorkflowState).where(
            WorkflowState.id == workflow_state_id, WorkflowState.project_id == project_id
        )
    )
    state = result.scalar_one_or_none()
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="workflow_state_id must reference an existing state in this project",
        )
    return state


async def _validate_assignee(db: AsyncSession, org_id: uuid.UUID, assignee_id: uuid.UUID) -> None:
    # Org-scoped, not project-scoped, unlike _validate_workflow_state: this
    # app has no per-project membership, so any member of the ticket's org
    # is eligible. Same shape as the other _validate_x helpers — the
    # composite FK (migration 0009) already guarantees this at the DB
    # level, but checking first turns a bad id into a clean 422 instead of
    # a raw IntegrityError.
    result = await db.execute(select(User).where(User.id == assignee_id, User.org_id == org_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="assignee_id must reference a user who is a member of this org",
        )


async def _get_default_workflow_state(db: AsyncSession, project_id: uuid.UUID) -> WorkflowState:
    result = await db.execute(
        select(WorkflowState).where(
            WorkflowState.project_id == project_id, WorkflowState.is_default.is_(True)
        )
    )
    state = result.scalar_one_or_none()
    if state is None:
        # Every project gets a default state at creation time (see
        # projects.py) — reaching this means that invariant broke, not
        # that the caller did anything wrong.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Project has no default workflow state configured",
        )
    return state


async def _next_position(db: AsyncSession, workflow_state_id: uuid.UUID) -> float:
    result = await db.execute(
        select(func.max(Ticket.position)).where(Ticket.workflow_state_id == workflow_state_id)
    )
    max_position = result.scalar_one_or_none()
    return (max_position or 0.0) + 1024.0


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    project_id: uuid.UUID,
    payload: TicketCreate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    await _get_project_or_404(db, project_id)
    await _validate_parent(db, project_id, payload.type, payload.parent_id)

    if payload.workflow_state_id is not None:
        await _validate_workflow_state(db, project_id, payload.workflow_state_id)
        workflow_state_id = payload.workflow_state_id
    else:
        workflow_state_id = (await _get_default_workflow_state(db, project_id)).id

    position = payload.position
    if position is None:
        position = await _next_position(db, workflow_state_id)

    if payload.assignee_id is not None:
        await _validate_assignee(db, auth.org_id, payload.assignee_id)

    ticket = Ticket(
        org_id=auth.org_id,
        project_id=project_id,
        parent_id=payload.parent_id,
        type=payload.type,
        title=payload.title,
        description=payload.description,
        workflow_state_id=workflow_state_id,
        position=position,
        assignee_id=payload.assignee_id,
        created_by=auth.user_id,
    )
    db.add(ticket)
    await db.commit()
    return ticket


@router.get("", response_model=list[TicketRead])
async def list_tickets(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[Ticket]:
    await _get_project_or_404(db, project_id)
    result = await db.execute(
        select(Ticket).where(Ticket.project_id == project_id).order_by(Ticket.created_at)
    )
    return list(result.scalars().all())


@router.get("/tree", response_model=list[TicketTreeNode])
async def get_ticket_tree(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[TicketTreeNode]:
    # Registered before "/{ticket_id}" deliberately — FastAPI matches
    # routes in declaration order, and a plain {ticket_id} path segment
    # matches the literal string "tree" too (type coercion to UUID happens
    # after routing, not during it), so the more specific path has to come
    # first or this would 422 as an invalid ticket id instead of running.
    await _get_project_or_404(db, project_id)
    result = await db.execute(
        select(Ticket).where(Ticket.project_id == project_id).order_by(Ticket.created_at)
    )
    tickets = list(result.scalars().all())

    # Built once, here, instead of shipping a flat list and making every
    # client (web today, anything else later) re-derive the same
    # hierarchy independently. It's an O(n) grouping over data already
    # fetched in one query — cheap, and there's exactly one place this
    # logic needs to be correct.
    nodes: dict[uuid.UUID, TicketTreeNode] = {
        t.id: TicketTreeNode(**TicketRead.model_validate(t).model_dump(), children=[]) for t in tickets
    }
    roots: list[TicketTreeNode] = []
    for t in tickets:
        node = nodes[t.id]
        if t.parent_id is not None and t.parent_id in nodes:
            nodes[t.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.get("/{ticket_id}", response_model=TicketRead)
async def get_ticket(
    project_id: uuid.UUID,
    ticket_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    await _get_project_or_404(db, project_id)
    return await _get_ticket_or_404(db, project_id, ticket_id)


@router.patch("/{ticket_id}", response_model=TicketRead)
async def update_ticket(
    project_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    await _get_project_or_404(db, project_id)
    ticket = await _get_ticket_or_404(db, project_id, ticket_id)

    update_data = payload.model_dump(exclude_unset=True)

    if not set(update_data.keys()) <= _COLLABORATIVE_FIELDS:
        _require_owner_or_admin(ticket, auth)

    if "type" in update_data or "parent_id" in update_data:
        new_type = update_data.get("type", ticket.type)
        new_parent_id = update_data.get("parent_id", ticket.parent_id)
        await _validate_parent(db, project_id, new_type, new_parent_id)

    if "workflow_state_id" in update_data:
        await _validate_workflow_state(db, project_id, update_data["workflow_state_id"])

    if update_data.get("assignee_id") is not None:
        await _validate_assignee(db, auth.org_id, update_data["assignee_id"])

    for field, value in update_data.items():
        setattr(ticket, field, value)

    await db.commit()
    return ticket


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ticket(
    project_id: uuid.UUID,
    ticket_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_project_or_404(db, project_id)
    ticket = await _get_ticket_or_404(db, project_id, ticket_id)
    _require_owner_or_admin(ticket, auth)
    try:
        await db.delete(ticket)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a ticket that has children — delete or reassign them first",
        )
