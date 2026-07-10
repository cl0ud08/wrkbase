import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import AppSecReviewStatus, TicketPriority, TicketType, TriageStatus


class TicketCreate(BaseModel):
    parent_id: uuid.UUID | None = None
    type: TicketType
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    # None = the project's default workflow state / appended to the end of
    # that column — the endpoint fills both in when omitted.
    workflow_state_id: uuid.UUID | None = None
    position: float | None = None
    # None = unassigned. Allowed at creation for symmetry with PATCH rather
    # than forcing a create-then-immediately-assign round trip.
    assignee_id: uuid.UUID | None = None
    # None = backlog (the default for a newly created ticket — nothing is
    # planned into a sprint just by existing).
    sprint_id: uuid.UUID | None = None
    story_points: int | None = Field(default=None, ge=0)


class TicketUpdate(BaseModel):
    # PATCH semantics via exclude_unset in the endpoint, same as Project.
    parent_id: uuid.UUID | None = None
    type: TicketType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    workflow_state_id: uuid.UUID | None = None
    position: float | None = None
    assignee_id: uuid.UUID | None = None
    sprint_id: uuid.UUID | None = None
    story_points: int | None = Field(default=None, ge=0)


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    parent_id: uuid.UUID | None
    type: TicketType
    title: str
    description: str | None
    workflow_state_id: uuid.UUID
    position: float
    # Per-org sequential display id (migration 0011) — combine with the
    # org's ticket_prefix (see /auth/me) on the frontend to render
    # "WRK-142". Not settable via TicketCreate/TicketUpdate — assigned once
    # by create_ticket from Organization.next_ticket_number.
    ticket_number: int
    # Nullable: a removed member's tickets are kept, not deleted, with
    # created_by set to NULL (migration 0008) — see app/api/org.py.
    created_by: uuid.UUID | None
    # Nullable: unassigned, or a removed member's old assignment (migration
    # 0009 sets this NULL on member removal, same as created_by).
    assignee_id: uuid.UUID | None
    # Nullable: NULL means "in the backlog" (see GET .../tickets/backlog).
    # Also goes back to NULL automatically for a ticket that was in an
    # active sprint but not in the project's terminal workflow state when
    # that sprint was completed — see complete_sprint in app/api/sprints.py.
    sprint_id: uuid.UUID | None
    story_points: int | None
    # The authoritative triage state — see Ticket.triage_status's
    # docstring (migration 0018) for why this replaced triaged_at IS NULL.
    triage_status: TriageStatus
    # All four set together, once, only on success — not settable via
    # TicketCreate/TicketUpdate. See app/services/llm_triage.py.
    priority: TicketPriority | None
    labels: list[str] | None
    triage_reasoning: str | None
    # Set only when triage_status = 'failed'.
    triage_error: str | None
    triaged_at: datetime | None
    # NULL for the vast majority of tickets — no trigger category has
    # ever matched. Not settable via TicketCreate/TicketUpdate; set only
    # by the keyword gate (app/services/appsec_triggers.py) and the
    # async review worker. See app/services/appsec_review.py and
    # migration 0020.
    appsec_review_status: AppSecReviewStatus | None
    appsec_categories: list[str] | None
    appsec_comment: str | None
    # Set only when appsec_review_status = 'failed'.
    appsec_review_error: str | None
    appsec_reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Not settable via TicketUpdate — only DELETE/{id}/restore touch this.
    deleted_at: datetime | None
    # Computed at read time, never stored (see app/services/at_risk.py's
    # module docstring for why no column/queue/schedule exists for this
    # at all) — NULL for every ticket not currently in its project's
    # active sprint, since risk is only meaningful relative to an active
    # sprint's own deadline. Only actually computed by
    # app/api/tickets.py's list_tickets/get_ticket; every other
    # TicketRead response (create/update/backlog/tree/assign) leaves
    # these at their default of NULL rather than paying for the extra
    # queries on a response the frontend re-fetches from anyway.
    at_risk: bool | None = None
    at_risk_reasons: list[str] | None = None


class TicketTreeNode(TicketRead):
    children: list["TicketTreeNode"] = Field(default_factory=list)


TicketTreeNode.model_rebuild()


class TicketPage(BaseModel):
    # Offset pagination, not keyset/cursor: simpler to reason about and
    # plenty for a project's backlog (hundreds of tickets, not millions).
    # The acknowledged tradeoff — a concurrent insert/delete during
    # paging can shift which items land on which page — is a UI-polish
    # nit here, not a correctness or security issue the way it would be
    # for, say, an audit log, so it's not worth keyset's added complexity
    # in this first pass.
    items: list[TicketRead]
    total: int
    limit: int
    offset: int
