import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import NotificationType


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # No org_id/user_id here, deliberately: the caller already knows these
    # are their own (that's what RLS just enforced), and neither tells a
    # connected client anything it needs to render or link to the
    # notification — same "minimal, not a mirror of every column" shape
    # as InvitePreview.
    type: NotificationType
    payload: dict
    read_at: datetime | None
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationRead]
    total: int
    limit: int
    offset: int


class UnreadCountResponse(BaseModel):
    count: int
