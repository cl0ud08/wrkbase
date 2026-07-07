import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.core.security import create_access_token, generate_refresh_token, hash_password, verify_password
from app.db.models import Organization, User, UserLookup, UserRole
from app.db.session import get_db, set_tenant_context
from app.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest, SignupRequest, TokenPair
from app.services.refresh_tokens import pop_refresh_token, revoke_refresh_token, store_refresh_token

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
)


async def _issue_token_pair(*, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> TokenPair:
    access_token = create_access_token(user_id=user_id, org_id=org_id, role=role)
    refresh_token = generate_refresh_token()
    await store_refresh_token(refresh_token, user_id=user_id, org_id=org_id, role=role)
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post("/signup", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    # First user of a brand-new org: self-serve signup creates the workspace,
    # same pattern as Slack/Linear/Notion onboarding. Joining an *existing*
    # org (invite links) is a separate, not-yet-built flow.
    org = Organization(name=payload.org_name)
    db.add(org)
    await db.flush()  # organizations has no RLS, so no tenant context needed yet

    await set_tenant_context(db, org.id)

    user = User(
        org_id=org.id,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=UserRole.ADMIN,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")

    return await _issue_token_pair(user_id=user.id, org_id=org.id, role=user.role.value)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    # Step 1: user_lookup has no RLS, so this is the one query in the app
    # allowed to run with no tenant context set — answering "which org" is a
    # prerequisite FOR having a tenant context, not something RLS can gate.
    lookup_result = await db.execute(select(UserLookup).where(UserLookup.email == payload.email))
    lookup = lookup_result.scalar_one_or_none()
    if lookup is None:
        raise _INVALID_CREDENTIALS

    # Step 2: now that the org is known, set tenant context and do the actual
    # credential check against `users` — the same RLS-protected path every
    # other org-scoped query in the app goes through. user_lookup can't do
    # this check itself: it holds no password hash (by design, it's routing
    # info only, not sensitive data), and giving it one would mean two copies
    # of credential state to keep in sync, plus a second "read a user" path
    # that quietly bypasses RLS — exactly the kind of forked, inconsistent
    # code path that tends to turn into a real vulnerability later.
    await set_tenant_context(db, lookup.org_id)
    user_result = await db.execute(select(User).where(User.id == lookup.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise _INVALID_CREDENTIALS

    return await _issue_token_pair(user_id=user.id, org_id=user.org_id, role=user.role.value)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest) -> TokenPair:
    data = await pop_refresh_token(payload.refresh_token)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    # Rotation: the old token was already deleted by pop_refresh_token above
    # (single-use). A stolen-but-unused refresh token is only ever valid
    # until the legitimate user's next normal refresh — which happens every
    # ~15 minutes as access tokens expire — not for its full multi-day
    # lifetime the way an unrotated long-lived token would be.
    return await _issue_token_pair(
        user_id=uuid.UUID(data["user_id"]),
        org_id=uuid.UUID(data["org_id"]),
        role=data["role"],
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest) -> None:
    # Actually invalidates the token server-side (Redis key deletion), not
    # just something the client is expected to forget on its own.
    await revoke_refresh_token(payload.refresh_token)


@router.get("/me")
async def me(
    auth: AuthContext = Depends(get_current_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    user_result = await db.execute(select(User).where(User.id == auth.user_id))
    user = user_result.scalar_one()

    # Same tenant-scoped session as the row above: this count can only ever
    # include rows from auth.org_id, proving auth + RLS are wired together,
    # not just each independently correct.
    count_result = await db.execute(select(func.count()).select_from(User))
    org_user_count = count_result.scalar_one()

    return {
        "id": str(user.id),
        "email": user.email,
        "org_id": str(user.org_id),
        "role": user.role.value,
        "org_user_count": org_user_count,
    }
