import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import TicketType


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


class TicketUpdate(BaseModel):
    # PATCH semantics via exclude_unset in the endpoint, same as Project.
    parent_id: uuid.UUID | None = None
    type: TicketType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    workflow_state_id: uuid.UUID | None = None
    position: float | None = None
    assignee_id: uuid.UUID | None = None


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
    created_at: datetime
    updated_at: datetime
    # Not settable via TicketUpdate — only DELETE/{id}/restore touch this.
    deleted_at: datetime | None


class TicketTreeNode(TicketRead):
    children: list["TicketTreeNode"] = Field(default_factory=list)


TicketTreeNode.model_rebuild()
