import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import UserRole


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: UserRole
    created_at: datetime


class MemberUpdate(BaseModel):
    role: UserRole
