import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.models import Invite, InviteLookup, Organization, UserLookup, UserRole
from app.db.session import get_db, set_tenant_context
from app.schemas.invite import InviteCreate, InviteCreateResponse, InvitePreview, InviteRead

router = APIRouter(prefix="/invites", tags=["invites"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
_INVALID_INVITE = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid, expired, or already-used invite link"
)


def _require_admin(auth: AuthContext) -> None:
    # Same rule and reasoning as workflow_states.py's _require_admin: who
    # gets to join the org, and under what role, is shared org
    # configuration, not a personal-resource edit — no creator carve-out.
    if auth.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only an org admin can manage invites"
        )


@router.post("", response_model=InviteCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_invite(
    payload: InviteCreate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> InviteCreateResponse:
    _require_admin(auth)

    # user_lookup is global (no RLS, unique on email) — a cheap, honest
    # up-front check that this invite could ever actually be redeemed.
    # Without it, an admin could invite an email that's already registered
    # (in this org or, given global email uniqueness, any org) and the
    # failure would only surface much later, opaquely, when the invitee
    # tries to sign up and hits the ordinary "Email already in use" 409.
    existing = await db.execute(select(UserLookup).where(UserLookup.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This email is already registered"
        )

    # Opaque random token (secrets.token_urlsafe), not a JWT — deliberately
    # the same choice already made for refresh tokens (app/core/security.py).
    # A JWT would let anyone holding it decode org_id/email/role without a
    # DB round trip, but that statelessness buys nothing here: every
    # redemption already has to hit the DB anyway to check accepted_at and
    # expires_at (business state a signed token can't carry safely — it
    # can't be "revoked" by editing a field on it). An opaque token, by
    # contrast, is trivially revocable (DELETE the row — see revoke_invite
    # below) and its whole security model is "look it up; if found,
    # unexpired, and unaccepted, it's valid," which is exactly the business
    # rule this feature actually needs.
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.invite_expire_days)

    invite = Invite(
        org_id=auth.org_id,
        email=payload.email,
        role=payload.role,
        invited_by=auth.user_id,
        token=token,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.commit()

    link = f"{settings.frontend_url}/signup?invite={token}"
    return InviteCreateResponse(**InviteRead.model_validate(invite).model_dump(), token=token, link=link)


@router.get("", response_model=list[InviteRead])
async def list_invites(
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[Invite]:
    _require_admin(auth)
    result = await db.execute(select(Invite).order_by(Invite.created_at.desc()))
    return list(result.scalars().all())


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_admin(auth)
    result = await db.execute(select(Invite).where(Invite.id == invite_id))
    invite = result.scalar_one_or_none()
    if invite is None:
        raise _NOT_FOUND
    # Deleting an already-accepted invite is allowed too — it's a harmless
    # history cleanup at that point (the membership it granted already
    # exists independently of this row), not a way to undo a join.
    await db.delete(invite)
    await db.commit()


@router.get("/preview", response_model=InvitePreview)
# Public (no get_current_auth) and IP rate-limited, same 10/minute budget
# as signup/login: this is the one endpoint on the app that lets an
# unauthenticated caller probe whether a given token string means anything,
# so it gets the same brute-force protection as a credential check.
@limiter.limit("10/minute", key_func=get_remote_address)
async def preview_invite(request: Request, token: str, db: AsyncSession = Depends(get_db)) -> InvitePreview:
    lookup_result = await db.execute(select(InviteLookup).where(InviteLookup.token == token))
    lookup = lookup_result.scalar_one_or_none()
    if lookup is None:
        raise _INVALID_INVITE

    await set_tenant_context(db, lookup.org_id)
    invite_result = await db.execute(select(Invite).where(Invite.id == lookup.invite_id))
    invite = invite_result.scalar_one_or_none()
    if invite is None:
        raise _INVALID_INVITE

    if invite.accepted_at is not None or invite.expires_at < datetime.now(timezone.utc):
        raise _INVALID_INVITE

    org_result = await db.execute(select(Organization).where(Organization.id == invite.org_id))
    org = org_result.scalar_one()

    return InvitePreview(org_name=org.name, email=invite.email, role=invite.role)
