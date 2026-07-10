import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import SprintRetroStatus, SprintStatus


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
    # Computed, same as total_points — only meaningful once completed
    # (before that nothing's been returned yet, so it would just equal
    # total_points). See _points_planned in app/api/sprints.py.
    points_planned: int | None
    # Computed at read time, never stored — see app/services/at_risk.py's
    # module docstring for why this whole feature has no schema, queue,
    # or scheduler of its own. NULL for a planned/completed sprint (risk
    # is only meaningful for the one currently ACTIVE sprint); a real
    # count, possibly 0, for the active one. See _at_risk_count in
    # app/api/sprints.py.
    at_risk_count: int | None
    created_at: datetime
    # NULL until the sprint completes (see app/api/sprints.py's
    # complete_sprint) — a completed sprint is never observably NULL
    # here. See app/services/sprint_retro.py / worker/main.py.
    retro_status: SprintRetroStatus | None
    retro_narrative: str | None
    retro_completed_highlights: list[str] | None
    retro_incomplete_notes: list[str] | None
    retro_risks: list[str] | None
    retro_error: str | None
    retro_generated_at: datetime | None


class SprintAssignRequest(BaseModel):
    # Bulk backlog -> sprint assignment (see POST .../assign). Individual
    # moves (dragging one ticket, including back OUT of a sprint) go
    # through the existing ticket PATCH endpoint instead — sprint_id is a
    # collaborative field there, same class as workflow_state_id/position.
    ticket_ids: list[uuid.UUID] = Field(min_length=1)
