import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import (
    AppSecReviewStatus,
    NotificationType,
    Organization,
    Project,
    Sprint,
    Ticket,
    TicketType,
    User,
    UserRole,
    WorkflowState,
)
from app.db.session import get_db
from app.schemas.ticket import TicketCreate, TicketPage, TicketRead, TicketTreeNode, TicketUpdate
from app.schemas.ticket_duplicate import (
    DuplicateCandidate,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
)
from app.schemas.ticket_parse import TicketParseRequest
from app.services.appsec_triggers import match_triggers
from app.services.notifications import create_notification, publish_notification
from app.services.queue import (
    AppSecJob,
    EmbedJob,
    TriageJob,
    publish_appsec_job,
    publish_embed_job,
    publish_triage_job,
)
from app.services.ticket_duplicates import find_possible_duplicates
from app.services.ticket_embedding import EmbeddingFailed
from app.services.ticket_events import publish_ticket_update
from app.services.ticket_parse import (
    ParsedTicketCandidate,
    TicketParseUnavailable,
    parse_ticket_text,
)

logger = logging.getLogger("api.tickets")

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
# the stricter rule wins. sprint_id/story_points join this set for the
# same reason: dragging a card into a sprint or jotting down an estimate
# is planning collaboration, not a content edit, the same class as
# dragging a card between board columns.
_COLLABORATIVE_FIELDS = {"workflow_state_id", "position", "assignee_id", "sprint_id", "story_points"}


async def _get_project_or_404(
    db: AsyncSession, project_id: uuid.UUID, *, include_deleted: bool = False
) -> Project:
    # Same shape as projects.py's helper: RLS already scopes this to the
    # caller's org, so a project id from another org is indistinguishable
    # from one that doesn't exist. deleted_at IS NULL filtered by default
    # for the same reason as projects.py's copy of this helper — every
    # ticket endpoint in this file scopes through this function first, so
    # a soft-deleted project makes its entire ticket subtree unreachable
    # through the normal /projects/{id}/tickets/... paths in one place,
    # with no per-endpoint filtering to remember. include_deleted is used
    # nowhere in this file today (a ticket's own restore only needs the
    # TICKET looked up with include_deleted, not its project — see
    # restore_ticket) but matches projects.py's helper shape for
    # consistency and in case a future endpoint needs it.
    query = select(Project).where(Project.id == project_id)
    if not include_deleted:
        query = query.where(Project.deleted_at.is_(None))
    result = await db.execute(query)
    project = result.scalar_one_or_none()
    if project is None:
        raise _PROJECT_NOT_FOUND
    return project


async def _get_ticket_or_404(
    db: AsyncSession, project_id: uuid.UUID, ticket_id: uuid.UUID, *, include_deleted: bool = False
) -> Ticket:
    # Scoped by project_id, not just id: a ticket id that's real but
    # belongs to a *different* project in the same org must also 404, not
    # leak across projects within one tenant. deleted_at IS NULL filtered
    # by default — this is the one place every ticket-by-id lookup in this
    # file goes through, so this single filter covers get/update/delete
    # all at once. include_deleted=True is used only by restore_ticket,
    # which specifically needs to find a currently-soft-deleted row.
    query = select(Ticket).where(Ticket.id == ticket_id, Ticket.project_id == project_id)
    if not include_deleted:
        query = query.where(Ticket.deleted_at.is_(None))
    result = await db.execute(query)
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


def _apply_appsec_trigger(ticket: Ticket) -> bool:
    """Runs the keyword gate (app/services/appsec_triggers.match_triggers)
    against the ticket's current title/description and, if it matches,
    flags the ticket synchronously — additive-only, mirroring EmbedJob's
    own "never silently un-flag" discipline: a category already recorded
    here is never removed just because a later edit's wording happens to
    drop the matching keyword. Shared by both create_ticket and
    update_ticket below, since the logic is identical either way: create
    is just the special case where there's no existing state yet.

    Returns True if a new AppSecJob is worth publishing — the first
    match ever, or a genuinely new category beyond what's already
    recorded — False if there's nothing new for the LLM to review (no
    match at all, or the matched categories are already a subset of
    what's already flagged).
    """
    matched = match_triggers(ticket.title, ticket.description)
    if not matched:
        return False

    matched_keys = {c.key for c in matched}
    existing_keys = set(ticket.appsec_categories or [])
    is_first_match = ticket.appsec_review_status is None
    has_new_category = not matched_keys.issubset(existing_keys)
    if not is_first_match and not has_new_category:
        return False

    ticket.appsec_categories = sorted(existing_keys | matched_keys)
    ticket.appsec_review_status = AppSecReviewStatus.PENDING
    return True


async def _validate_parent(
    db: AsyncSession, project_id: uuid.UUID, ticket_type: TicketType, parent_id: uuid.UUID | None
) -> None:
    if parent_id is None:
        return

    # The composite FK (migration 0005) already guarantees, at the DB
    # level, that IF this insert/update succeeds, parent_id really is a
    # ticket in this same project. This check runs first anyway so a bad
    # parent_id gets a clean 422 with a real message instead of surfacing
    # as a raw IntegrityError. deleted_at IS NULL is part of this query
    # too: the FK itself can't express "not soft-deleted" (see migration
    # 0010), so a soft-deleted ticket has to be rejected as a parent here,
    # at the app layer — it simply doesn't turn up in this query, so it
    # hits the exact same 422 a genuinely nonexistent parent_id would.
    result = await db.execute(
        select(Ticket).where(
            Ticket.id == parent_id, Ticket.project_id == project_id, Ticket.deleted_at.is_(None)
        )
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


async def _validate_sprint(db: AsyncSession, project_id: uuid.UUID, sprint_id: uuid.UUID) -> None:
    # Same shape as _validate_workflow_state: the composite FK (migration
    # 0012) already guarantees this at the DB level, but checking first
    # turns a bad id into a clean 422 instead of a raw IntegrityError. Does
    # NOT check sprint status here — a completed sprint is a valid, if
    # unusual, direct assignment target via plain PATCH (the dedicated
    # bulk-assign endpoint in app/api/sprints.py is the one that blocks
    # completed sprints, since that's the actual "planning" entry point;
    # this one is closer to an admin/cleanup escape hatch).
    result = await db.execute(
        select(Sprint).where(Sprint.id == sprint_id, Sprint.project_id == project_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sprint_id must reference an existing sprint in this project",
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


async def _next_ticket_number(db: AsyncSession, org_id: uuid.UUID) -> int:
    # Atomic increment-and-fetch in one statement: Postgres serializes
    # concurrent UPDATEs to the same row automatically, so two tickets
    # being created in the same org at the same instant still each get a
    # distinct, correctly-incremented number — no separate locking needed,
    # and no race the way a read-then-write (SELECT next_ticket_number,
    # then UPDATE) would have.
    result = await db.execute(
        update(Organization)
        .where(Organization.id == org_id)
        .values(next_ticket_number=Organization.next_ticket_number + 1)
        .returning(Organization.next_ticket_number - 1)
    )
    return result.scalar_one()


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

    if payload.sprint_id is not None:
        await _validate_sprint(db, project_id, payload.sprint_id)

    ticket_number = await _next_ticket_number(db, auth.org_id)

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
        sprint_id=payload.sprint_id,
        story_points=payload.story_points,
        ticket_number=ticket_number,
        created_by=auth.user_id,
        # priority/triaged_at start NULL (pending_triage) simply by being
        # omitted here — nothing synchronous happens with them at creation
        # time at all; see the publish below.
    )
    # Cheap, synchronous, deterministic — no I/O, no LLM call. Sets
    # appsec_review_status/appsec_categories on the object in memory,
    # before it's ever inserted, so a flagged ticket's very first row
    # already carries the flag; see _apply_appsec_trigger's own
    # docstring for why this is the one field set synchronously while
    # the tailored comment itself is async.
    needs_appsec_review = _apply_appsec_trigger(ticket)

    db.add(ticket)
    await db.commit()

    # Fire-and-forget: the response below returns immediately with the
    # ticket still pending_triage, not blocked on anything — see
    # worker/main.py for the consumer that actually sets priority/
    # triaged_at, asynchronously, and publishes the live update itself.
    await publish_triage_job(
        TriageJob(
            ticket_id=ticket.id, org_id=auth.org_id, title=ticket.title, description=ticket.description
        )
    )
    # Same fire-and-forget reasoning as the triage job above, on its own
    # separate queue -- see app/services/queue.py's EmbedJob docstring
    # and worker/main.py for why embedding generation is a genuinely
    # separate job from triage, not one more step folded into it.
    await publish_embed_job(EmbedJob(ticket_id=ticket.id, org_id=auth.org_id))
    # Conditional, unlike the two publishes above: most tickets match no
    # trigger category at all and never publish an AppSecJob — see
    # AppSecJob's own docstring in queue.py for why that's the point,
    # not an oversight.
    if needs_appsec_review:
        await publish_appsec_job(AppSecJob(ticket_id=ticket.id, org_id=auth.org_id))

    return ticket


# Synchronous, same "is there a waiting caller" reasoning as parse_ticket
# below -- a user reviewing a draft ticket (either the plain form or the
# NL-parse review step) is deciding right now whether to create it, and
# a duplicate warning only helps if it's part of that decision, not
# discovered after the ticket already exists. Generates one throwaway
# embedding for the draft text and compares it against other tickets'
# already-stored embeddings (populated by the async EmbedJob above,
# whenever those tickets were themselves created or last edited) --
# nothing here is written to the database. See
# app/services/ticket_duplicates.py for the query itself, the
# project-scoping reasoning, and the empirically-tuned threshold.
@router.post("/check-duplicates", response_model=DuplicateCheckResponse)
async def check_duplicates(
    project_id: uuid.UUID,
    payload: DuplicateCheckRequest,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> DuplicateCheckResponse:
    await _get_project_or_404(db, project_id)

    try:
        matches = await find_possible_duplicates(
            db, project_id=project_id, title=payload.title, description=payload.description
        )
    except EmbeddingFailed as exc:
        # Real provider failure, not "nothing matched" -- same 503-vs-200
        # distinction already established for ticket parsing. Nothing
        # was created either way; this just tells the frontend it's
        # worth retrying, not that the draft itself is the problem.
        logger.error("duplicate check unavailable for project=%s: %s", project_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Couldn't check for duplicates right now. Try again, or just create the ticket.",
        ) from exc

    return DuplicateCheckResponse(
        matches=[
            DuplicateCandidate(
                ticket_id=m.ticket.id,
                ticket_number=m.ticket.ticket_number,
                title=m.ticket.title,
                similarity=m.similarity,
            )
            for m in matches
        ]
    )


# Synchronous, not published to RabbitMQ like the triage job above: a user
# is actively looking at this request, waiting to review whatever comes
# back, before deciding to create anything. Triage is async because it
# runs *after* ticket creation has already succeeded and returned to the
# caller -- there's no one waiting on its result to decide their next
# action, so paying its latency inline on every ticket creation would be
# pure cost with no user-facing benefit (that's exactly why the "AI
# triaging…" pending-state UI pattern exists at all). Parsing is the
# opposite: the entire point of calling this endpoint is to get the
# result back and show it, so the HTTP response *is* the useful output,
# not a mere acknowledgment a background job will later supersede. A
# background-job version of this endpoint would need its own polling or
# WebSocket-delivery mechanism for a single one-off request the caller is
# already sitting on a connection for -- real added complexity bought for
# zero benefit, given parse latency (a single bounded LLM call, ~1-3s) is
# well within normal HTTP request tolerance.
#
# Deliberately does NOT create a ticket. Returns a candidate for the
# caller to review/edit; actual creation goes through the exact same
# POST above (the caller submits the reviewed/edited title/description/
# type through the normal TicketCreate body) -- see this router's own
# create_ticket for why that composition doesn't need a separate
# "already parsed" code path: TicketCreate already accepts exactly the
# fields a confirmed candidate has (type/title/description), and
# create_ticket already doesn't care whether a human typed those directly
# into a form or edited them from an AI suggestion first. priority/labels
# are intentionally never threaded through to creation, even though the
# candidate includes them: every created ticket still gets async-triaged
# exactly as today (see create_ticket above), which is the actual source
# of truth for priority/labels on every ticket, parsed-origin or not --
# passing the parse's preview values through would just be silently
# overwritten moments later, so there's nothing to gain and a stale-looking
# UI moment to lose by pretending they're authoritative here.
@router.post("/parse", response_model=ParsedTicketCandidate)
async def parse_ticket(
    project_id: uuid.UUID,
    payload: TicketParseRequest,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> ParsedTicketCandidate:
    await _get_project_or_404(db, project_id)

    try:
        candidate, provider = await parse_ticket_text(payload.text)
    except TicketParseUnavailable as exc:
        # A real provider/infrastructure failure -- both Groq and Gemini
        # were tried and exhausted their budgets -- not "the AI looked at
        # this and wasn't confident" (that's confident=False below, a
        # normal 200). Nothing was created either way; a 503 tells the
        # frontend this is worth retrying or falling back to the plain
        # manual form, not treating the input itself as the problem.
        logger.error("ticket parse unavailable for project=%s: %s", project_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Couldn't reach the AI parsing service right now. Try again, or fill in the ticket form manually.",
        ) from exc

    logger.info(
        "parsed ticket text for project=%s via=%s confident=%s", project_id, provider, candidate.confident
    )
    return candidate


@router.get("", response_model=list[TicketRead])
async def list_tickets(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[Ticket]:
    await _get_project_or_404(db, project_id)
    result = await db.execute(
        select(Ticket)
        .where(Ticket.project_id == project_id, Ticket.deleted_at.is_(None))
        .order_by(Ticket.created_at)
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
        select(Ticket)
        .where(Ticket.project_id == project_id, Ticket.deleted_at.is_(None))
        .order_by(Ticket.created_at)
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


@router.get("/backlog", response_model=TicketPage)
async def get_backlog(
    project_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> TicketPage:
    # Registered before "/{ticket_id}", same routing-order reasoning as
    # /tree above — a literal path segment has to come before the
    # catch-all UUID param or FastAPI never reaches this route.
    await _get_project_or_404(db, project_id)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    # Backlog = sprint_id IS NULL, full stop — not "or its sprint is
    # completed." complete_sprint (app/api/sprints.py) already sets
    # sprint_id back to NULL for anything unfinished when a sprint wraps
    # up, so a ticket that's still attached to a completed sprint is
    # exactly the tickets that *did* finish — real history, not backlog.
    base = select(Ticket).where(
        Ticket.project_id == project_id, Ticket.sprint_id.is_(None), Ticket.deleted_at.is_(None)
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    result = await db.execute(base.order_by(Ticket.created_at).limit(limit).offset(offset))
    items = list(result.scalars().all())
    return TicketPage(items=items, total=total, limit=limit, offset=offset)


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

    if update_data.get("sprint_id") is not None:
        await _validate_sprint(db, project_id, update_data["sprint_id"])

    # Captured before the mutation loop below overwrites it: this is what
    # tells a reassignment (notify) apart from "PATCHed to the assignee it
    # already had" (don't) — the field being present in update_data alone
    # doesn't mean it's actually changing.
    previous_assignee_id = ticket.assignee_id

    for field, value in update_data.items():
        setattr(ticket, field, value)

    # Only worth re-running the gate when title/description actually
    # changed — nothing else in a PATCH could change what match_triggers
    # sees. Same additive-only, first-match-or-new-category logic as
    # creation (see _apply_appsec_trigger) — an edit that touches these
    # fields without introducing new security-relevant wording is a
    # no-op here, same as it is for EmbedJob just below.
    needs_appsec_review = False
    if "title" in update_data or "description" in update_data:
        needs_appsec_review = _apply_appsec_trigger(ticket)

    new_assignee_id = update_data.get("assignee_id")
    notification = None
    if (
        new_assignee_id is not None
        and new_assignee_id != previous_assignee_id
        and new_assignee_id != auth.user_id  # assigning to yourself needs no notification
    ):
        notification = await create_notification(
            db,
            org_id=auth.org_id,
            user_id=new_assignee_id,
            type=NotificationType.ASSIGNMENT,
            payload={
                "ticket_id": str(ticket.id),
                "project_id": str(project_id),
                "ticket_title": ticket.title,
                "assigned_by": str(auth.user_id),
            },
        )

    await db.commit()

    if notification is not None:
        await publish_notification(notification)

    # A stale embedding is a silent-gap class of bug -- a ticket whose
    # title/description changed but whose stored vector still describes
    # the old text would keep matching (or failing to match) based on
    # words that aren't even on the ticket anymore, and nothing about
    # that is visible anywhere. Re-publishing EmbedJob here is the exact
    # same trigger creation already fires, on the exact same queue,
    # picked up by the exact same worker code -- regenerating an
    # embedding isn't a distinct operation from generating one the first
    # time, just the same one happening again. See EmbedJob's own
    # docstring for why it's safe to fire this on every title/
    # description-touching PATCH with no ordering concerns.
    if "title" in update_data or "description" in update_data:
        await publish_embed_job(EmbedJob(ticket_id=ticket.id, org_id=auth.org_id))
    if needs_appsec_review:
        await publish_appsec_job(AppSecJob(ticket_id=ticket.id, org_id=auth.org_id))

    # Live-board fan-out: only the collaborative subset, even for a
    # request that also touched a content field in the same PATCH (the
    # stricter-rule-wins check above already gated the whole request on
    # ownership for that case; this only decides what's worth pushing to
    # everyone else's board in real time). Nothing to publish if this
    # PATCH didn't touch any of them.
    board_changes = {k: v for k, v in update_data.items() if k in _COLLABORATIVE_FIELDS}
    if board_changes:
        await publish_ticket_update(
            project_id=project_id, ticket_id=ticket.id, changes=board_changes, updated_by=auth.user_id
        )

    return ticket


async def _has_active_children(db: AsyncSession, ticket_id: uuid.UUID) -> bool:
    # Under hard-delete this check was implicit: fk_tickets_parent_project
    # is ON DELETE RESTRICT, so attempting to delete a ticket with children
    # raised IntegrityError, caught and turned into a 409. Soft-delete
    # never issues a DELETE statement at all — the row stays put, so that
    # FK never fires, and the check has to become an explicit query
    # instead. Deliberately checks for *non-deleted* children only: a
    # ticket whose only children are themselves already soft-deleted is
    # fine to soft-delete, same as a ticket that never had children.
    result = await db.execute(
        select(exists().where(Ticket.parent_id == ticket_id, Ticket.deleted_at.is_(None)))
    )
    return bool(result.scalar())


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

    # Same rule as before soft-delete, still enforced, just by a different
    # mechanism (see _has_active_children): soft-deleting a ticket that
    # still has live children would leave the /tree view with children
    # whose parent has vanished from the default view — an orphaned-
    # looking subtree, not a genuinely broken reference (the row and its
    # composite FK are both still intact), but confusing and avoidable.
    if await _has_active_children(db, ticket_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a ticket that has children — delete or reassign them first",
        )

    ticket.deleted_at = datetime.now(timezone.utc)
    await db.commit()


@router.post("/{ticket_id}/restore", response_model=TicketRead)
async def restore_ticket(
    project_id: uuid.UUID,
    ticket_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Ticket:
    # The project itself is looked up WITHOUT include_deleted: restoring a
    # ticket under a still-soft-deleted project is deliberately blocked by
    # this 404, not specially handled — the project has to be restored
    # first. Consistent with a soft-deleted project hiding its entire
    # ticket subtree as one unit; restoring one ticket into a project
    # nobody can currently see wouldn't accomplish much anyway.
    await _get_project_or_404(db, project_id)
    ticket = await _get_ticket_or_404(db, project_id, ticket_id, include_deleted=True)
    _require_owner_or_admin(ticket, auth)
    if ticket.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ticket is not deleted")

    # No re-validation of parent_id here on purpose: if the parent is
    # itself still soft-deleted (or was deleted since), get_ticket_tree
    # already degrades gracefully — a restored ticket whose parent isn't
    # in the visible set simply renders as a root instead of nesting,
    # self-healing once the parent is restored too on a later fetch,
    # rather than needing new blocking logic here.
    ticket.deleted_at = None
    await db.commit()
    return ticket
