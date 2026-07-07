import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkflowStateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    order: int
    is_default: bool = False


class WorkflowStateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    order: int | None = None
    is_default: bool | None = None


class WorkflowStateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    order: int
    is_default: bool
    created_at: datetime
