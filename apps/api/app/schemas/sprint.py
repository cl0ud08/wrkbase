import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import SprintStatus


class SprintCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str | None = None
    start_date: date
    end_date: date


class SprintUpdate(BaseModel):
    # PATCH semantics via exclude_unset, same as Project/Ticket. status is
    # deliberately absent here — it only ever changes through the
    # start/complete actions (see app/api/sprints.py), which run the
    # transition-specific logic (the single-active-sprint constraint on
    # start, the unfinished-ticket return-to-backlog on complete) that a
    # generic PATCH has no business doing implicitly.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class SprintRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    goal: str | None
    start_date: date
    end_date: date
    status: SprintStatus
    # Computed (sum of story_points for this sprint's current tickets),
    # not a stored column — see _with_total_points in app/api/sprints.py.
    # Named total_points rather than "capacity" deliberately: in Scrum,
    # capacity usually means the team's *available* effort, a different
    # number this app doesn't model — this is the sum of what's actually
    # committed, so it gets the less overloaded name.
    total_points: int
    created_at: datetime


class SprintAssignRequest(BaseModel):
    # Bulk backlog -> sprint assignment (see POST .../assign). Individual
    # moves (dragging one ticket, including back OUT of a sprint) go
    # through the existing ticket PATCH endpoint instead — sprint_id is a
    # collaborative field there, same class as workflow_state_id/position.
    ticket_ids: list[uuid.UUID] = Field(min_length=1)
