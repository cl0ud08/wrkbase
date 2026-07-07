import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.db.models import UserRole


class InviteCreate(BaseModel):
    email: EmailStr
    role: UserRole = UserRole.MEMBER

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        # Same normalization as SignupRequest/LoginRequest — this value has
        # to byte-for-byte match what the invitee later types into the
        # signup form for the email-binding check in app/api/auth.py.
        return value.lower()


class InviteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: UserRole
    invited_by: uuid.UUID | None
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime
    # No `token` field here, deliberately: this is the shape returned by
    # POST (after InviteCreateResponse) on every later GET /invites list
    # call. Re-exposing a still-usable bearer token on every list load
    # widens where it can leak (logs, screen shares, browser history) for
    # no real benefit — if an admin needs to reshare, revoke and recreate.


class InviteCreateResponse(InviteRead):
    # Only returned once, at creation. `link` is `token` pre-formatted into
    # the shareable signup URL so the frontend doesn't need to know the
    # route shape itself.
    token: str
    link: str


class InvitePreview(BaseModel):
    """Public, pre-auth shape for the signup page to render "You've been
    invited to join {org_name}" — deliberately minimal, no invite id, no
    inviter, nothing beyond what's needed to render that one line and
    prefill/lock the signup form.
    """

    org_name: str
    email: str
    role: UserRole
