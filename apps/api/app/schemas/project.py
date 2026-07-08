import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectUpdate(BaseModel):
    # Both optional: PATCH semantics — only fields present in the request
    # body change (see exclude_unset in the endpoint), not just non-null ones.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    description: str | None
    # Nullable: a removed member's projects are kept, not deleted, with
    # created_by set to NULL (migration 0008) — see app/api/org.py.
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # Not settable via ProjectUpdate — only DELETE/{id}/restore touch this,
    # so it can't be bypassed via a plain PATCH.
    deleted_at: datetime | None
