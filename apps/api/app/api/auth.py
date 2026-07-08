import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from slowapi.util import get_remote_address
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    verify_password,
)
from app.core.ticket_prefix import derive_ticket_prefix
from app.db.models import (
    EmailVerificationToken,
    Invite,
    InviteLookup,
    Organization,
    PasswordResetToken,
    User,
    UserLookup,
    UserRole,
)
from app.db.session import get_db, set_tenant_context
from app.schemas.auth import (
    EmailVerifyRequest,
    EmailVerifyResponse,
    LoginRequest,
    LogoutRequest,
    PasswordResetConfirm,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    RefreshRequest,
    ResendVerificationRequest,
    ResendVerificationResponse,
    SignupRequest,
    SignupResponse,
    TokenPair,
)
from app.services.refresh_tokens import (
    pop_refresh_token,
    revoke_all_refresh_tokens_for_user,
    revoke_refresh_token,
    store_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
)
_INVALID_INVITE = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid, expired, or already-used invite link"
)
_INVALID_RESET_TOKEN = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid, expired, or already-used reset link"
)
_INVALID_VERIFICATION_TOKEN = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="Invalid, expired, or already-used verification link",
)


async def _redeem_invite(db: AsyncSession, token: str, email: str) -> tuple[uuid.UUID, UserRole]:
    """Validate an invite token and mark it accepted, returning the
    (org_id, role) the new user should be created with. Does NOT commit —
    the caller commits this together with the User insert, in one
    transaction, so a token can never be marked accepted without the user
    it was accepted for actually existing (and vice versa).
    """
    # Step 1: same two-step shape as login() below — find the org from an
    # unguessable token BEFORE any tenant context exists to scope an
    # RLS-protected read with. invite_lookup carries nothing sensitive
    # (see app/db/models.py), same as user_lookup for the login case.
    lookup_result = await db.execute(select(InviteLookup).where(InviteLookup.token == token))
    lookup = lookup_result.scalar_one_or_none()
    if lookup is None:
        raise _INVALID_INVITE

    # Step 2: now that the org is known, the real row can be read through
    # the normal RLS-protected path, same as login()'s second step.
    await set_tenant_context(db, lookup.org_id)
    invite_result = await db.execute(select(Invite).where(Invite.id == lookup.invite_id))
    invite = invite_result.scalar_one_or_none()
    if invite is None:
        raise _INVALID_INVITE

    if invite.accepted_at is not None or invite.expires_at < datetime.now(timezone.utc):
        raise _INVALID_INVITE

    # Binds the token to the specific email it was issued for. The token's
    # own unguessability already limits who *can* redeem it, but without
    # this check a leaked/forwarded link would let anyone sign up as any
    # email under that org and role — this is what stops that.
    if invite.email != email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invite was issued for a different email address",
        )

    # Marked accepted now, committed later alongside the User insert. Two
    # concurrent redemptions of the same token would both pass this check
    # before either commits, but both are necessarily creating the SAME
    # email (just checked above) — the pre-existing `users.email` unique
    # constraint is what actually stops the second one, surfacing as the
    # ordinary "Email already in use" 409 a few lines down, not a new
    # locking mechanism built for this.
    invite.accepted_at = datetime.now(timezone.utc)
    return invite.org_id, invite.role


def _set_auth_cookies(response: Response, *, access_token: str, refresh_token: str) -> None:
    # Both tokens are httpOnly: client-side JS can never read them (via
    # document.cookie or a fetch response), which is what actually defends
    # against theft-via-XSS — the whole point of not using localStorage.
    # path="/" on both, not narrowed to e.g. /auth for the refresh cookie,
    # because the browser reaches this API through a same-origin Next.js
    # rewrite proxy (see apps/web/next.config.ts) — the path the browser
    # sees is /api/auth/..., not the backend's own /auth/... route, so a
    # path scoped to the backend's route shape wouldn't match what the
    # browser actually requests.
    response.set_cookie(
        "access_token",
        access_token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    response.set_cookie(
        "refresh_token",
        refresh_token,
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


async def _issue_token_pair(
    response: Response, *, user_id: uuid.UUID, org_id: uuid.UUID, role: str
) -> TokenPair:
    access_token = create_access_token(user_id=user_id, org_id=org_id, role=role)
    refresh_token = generate_refresh_token()
    await store_refresh_token(refresh_token, user_id=user_id, org_id=org_id, role=role)
    _set_auth_cookies(response, access_token=access_token, refresh_token=refresh_token)
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


async def _issue_verification_token(db: AsyncSession, user_id: uuid.UUID) -> str:
    # Invalidates any prior still-live token for this user first — at most
    # one live verification link at a time, by design (see
    # EmailVerificationToken's docstring for why this deliberately differs
    # from password reset, which allows several to coexist). A no-op for a
    # brand-new signup (nothing to invalidate yet), the actual point for
    # resend_verification below.
    await db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user_id, EmailVerificationToken.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
    )
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.email_verification_expire_hours
    )
    db.add(EmailVerificationToken(user_id=user_id, token=token, expires_at=expires_at))
    await db.commit()
    return f"{settings.frontend_url}/verify-email?token={token}"


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
# Always keyed by IP regardless of any stray cookie — signup/login are the
# endpoints most likely to be brute-forced, and the whole allowance should
# be tied to the caller's network origin, not an identity they don't have yet.
@limiter.limit("10/minute", key_func=get_remote_address)
async def signup(
    request: Request, payload: SignupRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> SignupResponse:
    # Two paths sharing one endpoint. No invite_token: first user of a
    # brand-new org, self-serve, same pattern as Slack/Linear/Notion
    # onboarding — org_name is required (enforced in SignupRequest). A
    # valid invite_token: join that invite's existing org, with that
    # invite's role, instead — org_name is ignored.
    is_invited = bool(payload.invite_token)
    if is_invited:
        org_id, role = await _redeem_invite(db, payload.invite_token, payload.email)
    else:
        # id generated client-side, not left to the column's
        # gen_random_uuid() server default: Postgres requires a row to
        # pass a table's SELECT policy before an INSERT's implicit
        # RETURNING (which the ORM always issues, to read back id and the
        # other server-generated columns) can return it — even though the
        # INSERT policy itself is permissive. Knowing the id up front lets
        # tenant context be set to it *before* the flush, the same
        # ordering the `users` insert just below already relies on.
        org = Organization(
            id=uuid.uuid4(), name=payload.org_name, ticket_prefix=derive_ticket_prefix(payload.org_name)
        )
        await set_tenant_context(db, org.id)
        db.add(org)
        await db.flush()
        org_id, role = org.id, UserRole.ADMIN

    user = User(
        org_id=org_id,
        email=payload.email,
        hashed_password=await hash_password(payload.password),
        role=role,
        # True for an invite redemption: the invite already binds this
        # account to a specific, admin-vouched-for email (see
        # _redeem_invite's email-binding check above) — a stronger trust
        # signal than a self-click link, so a separate verification step
        # would be redundant friction. False (soft-nudge, not a gate —
        # see User.is_verified's docstring) for a brand-new self-serve org.
        is_verified=is_invited,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")

    verification_link = None if is_invited else await _issue_verification_token(db, user.id)

    tokens = await _issue_token_pair(response, user_id=user.id, org_id=org_id, role=user.role.value)
    return SignupResponse(**tokens.model_dump(), verification_link=verification_link)


@router.post("/login", response_model=TokenPair)
@limiter.limit("10/minute", key_func=get_remote_address)
async def login(
    request: Request, payload: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenPair:
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
    if user is None or not await verify_password(payload.password, user.hashed_password):
        raise _INVALID_CREDENTIALS

    return await _issue_token_pair(response, user_id=user.id, org_id=user.org_id, role=user.role.value)


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    # Browser clients never see the raw refresh token (httpOnly cookie); the
    # browser just sends the cookie automatically. Bearer-token clients pass
    # it explicitly in the body instead.
    refresh_token = (payload.refresh_token if payload else None) or request.cookies.get("refresh_token")
    if refresh_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token provided")

    data = await pop_refresh_token(refresh_token)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    org_id = uuid.UUID(data["org_id"])
    user_id = uuid.UUID(data["user_id"])

    # Re-check the user's CURRENT role from the database rather than
    # trusting the role cached in the refresh token's own payload. Found
    # while wiring up member-removal token revocation: role is baked into
    # a refresh token at issuance and carried forward unchanged on every
    # rotation (store_refresh_token below re-persists whatever role this
    # call returns), so trusting the cached value would mean a role change
    # via PATCH /org/members/{id} never actually takes effect for that
    # user's existing session — a demoted admin would keep refreshing
    # with admin privileges indefinitely, not just for the already-
    # accepted 15-minute access-token window. This also gives removal a
    # second layer of defense: if a refresh token somehow outlived
    # revoke_all_refresh_tokens_for_user, the user row is simply gone from
    # this query and the refresh is rejected here too.
    await set_tenant_context(db, org_id)
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    # Rotation: the old token was already deleted by pop_refresh_token above
    # (single-use). A stolen-but-unused refresh token is only ever valid
    # until the legitimate user's next normal refresh — which happens every
    # ~15 minutes as access tokens expire — not for its full multi-day
    # lifetime the way an unrotated long-lived token would be.
    return await _issue_token_pair(response, user_id=user_id, org_id=org_id, role=user.role.value)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, payload: LogoutRequest | None = None
) -> None:
    refresh_token = (payload.refresh_token if payload else None) or request.cookies.get("refresh_token")
    # Actually invalidates the token server-side (Redis key deletion), not
    # just something the client is expected to forget on its own.
    if refresh_token is not None:
        await revoke_refresh_token(refresh_token)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


@router.post("/password-reset/request", response_model=PasswordResetRequestResponse)
# Stricter than login/signup's 10/minute: this is a strictly worse target
# for abuse than a credential check — a caller doesn't even need to guess
# anything to spam it, just enumerate/guess emails, and a "successful"
# spam here means an inbox full of unwanted reset links, not just a
# rejected login attempt.
@limiter.limit("5/minute", key_func=get_remote_address)
async def request_password_reset(
    request: Request, payload: PasswordResetRequest, db: AsyncSession = Depends(get_db)
) -> PasswordResetRequestResponse:
    # Enumeration prevention: this endpoint must be observably identical
    # (status code, response shape, response content) whether or not
    # payload.email belongs to a real account — otherwise it becomes a
    # free oracle for "is this email registered," which is exactly the
    # kind of information a credential-stuffing or targeted-phishing
    # campaign wants. A naive fix — return reset_link only when the
    # account exists, omit/null it otherwise — would still leak the
    # answer through the *presence* of that field, which defeats the
    # point while looking secure. Instead: a token is generated
    # unconditionally, on every call, but it's only ever persisted (and
    # therefore only ever valid) when the email resolves to a real user.
    # The response always contains a plausible-looking reset_link either
    # way. An unpersisted token fails at confirm_password_reset with
    # exactly the same "invalid or expired" 400 a genuinely expired or
    # already-used token gets — there is no observable difference between
    # "this email doesn't exist" and "this link already expired," which is
    # the actual property that matters. (What's *not* fully solved here:
    # the found-branch does one extra DB write the not-found branch
    # doesn't, a small residual timing signal — not equalized, an
    # acknowledged gap rather than a solved one.)
    token = secrets.token_urlsafe(32)

    lookup_result = await db.execute(select(UserLookup).where(UserLookup.email == payload.email))
    lookup = lookup_result.scalar_one_or_none()
    if lookup is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.password_reset_expire_minutes
        )
        db.add(PasswordResetToken(user_id=lookup.user_id, token=token, expires_at=expires_at))
        await db.commit()

    return PasswordResetRequestResponse(
        # Dev-mode-only shortcut, same limitation as invites (see
        # create_invite): real email delivery is out of scope, so the link
        # is handed back directly instead of sent out-of-band. Unlike
        # invites, this endpoint is unauthenticated and public, which is
        # exactly why the token-existence trick above matters here and
        # didn't need to for invites (created by an already-authenticated
        # admin who supplies the target email on purpose).
        reset_link=f"{settings.frontend_url}/reset-password?token={token}"
    )


@router.post("/password-reset/confirm", response_model=PasswordResetConfirmResponse)
async def confirm_password_reset(
    payload: PasswordResetConfirm, db: AsyncSession = Depends(get_db)
) -> PasswordResetConfirmResponse:
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token == payload.token)
    )
    reset_token = result.scalar_one_or_none()
    if (
        reset_token is None
        or reset_token.used_at is not None
        or reset_token.expires_at < datetime.now(timezone.utc)
    ):
        raise _INVALID_RESET_TOKEN

    # Same two-step pre-auth bootstrap shape as login()/redeem_invite():
    # this endpoint is unauthenticated, so there's no tenant context yet,
    # and PasswordResetToken carries no org_id of its own (see its model
    # docstring) — UserLookup's unique index on user_id is what recovers
    # it, the same way UserLookup's primary key on email does for login.
    lookup_result = await db.execute(
        select(UserLookup).where(UserLookup.user_id == reset_token.user_id)
    )
    lookup = lookup_result.scalar_one_or_none()
    if lookup is None:
        # The user was removed after requesting a reset but before using
        # it. Treated identically to an expired/invalid token, not a
        # distinct error — a distinct message here would itself be a
        # (smaller, authenticated-token-gated) enumeration leak.
        raise _INVALID_RESET_TOKEN

    await set_tenant_context(db, lookup.org_id)
    user_result = await db.execute(select(User).where(User.id == reset_token.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise _INVALID_RESET_TOKEN

    user.hashed_password = await hash_password(payload.new_password)
    reset_token.used_at = datetime.now(timezone.utc)
    # Every other still-live token for this user is invalidated too, not
    # just this one: someone who requested a reset three times (maybe
    # they lost the first two links) shouldn't leave two still-valid
    # reset links lying around in old emails/browser history after
    # they've already reset via the third.
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == reset_token.user_id,
            PasswordResetToken.id != reset_token.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(timezone.utc))
    )
    await db.commit()

    # The actual point of this endpoint, not an afterthought: without this,
    # changing your password does nothing to a session an attacker already
    # has. If someone stole a refresh token — a synced browser profile on
    # a shared/stolen device, a session cookie leaked some other way —
    # they never needed the password at all, and rotation (see refresh()
    # above) only forces THEM to keep refreshing to stay logged in, which
    # they can do indefinitely without ever knowing the password. A user
    # who notices something's wrong and resets their password would, with
    # no revocation, walk away believing they'd secured the account while
    # the attacker's session keeps working completely unaffected — the
    # reset would have changed nothing an attacker actually needed. Done
    # AFTER the commit, not before, same ordering as remove_member in
    # app/api/org.py: if the commit had failed, revoking every session
    # first would lock out someone whose password never actually changed.
    await revoke_all_refresh_tokens_for_user(user.id)

    return PasswordResetConfirmResponse()


@router.post("/verify-email", response_model=EmailVerifyResponse)
async def verify_email(
    payload: EmailVerifyRequest, db: AsyncSession = Depends(get_db)
) -> EmailVerifyResponse:
    result = await db.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.token == payload.token)
    )
    verify_token = result.scalar_one_or_none()
    if (
        verify_token is None
        or verify_token.used_at is not None
        or verify_token.expires_at < datetime.now(timezone.utc)
    ):
        raise _INVALID_VERIFICATION_TOKEN

    # Same two-step pre-auth bootstrap shape as confirm_password_reset:
    # unauthenticated, no tenant context yet, and EmailVerificationToken
    # carries no org_id of its own — UserLookup's unique user_id index
    # recovers it.
    lookup_result = await db.execute(
        select(UserLookup).where(UserLookup.user_id == verify_token.user_id)
    )
    lookup = lookup_result.scalar_one_or_none()
    if lookup is None:
        raise _INVALID_VERIFICATION_TOKEN

    await set_tenant_context(db, lookup.org_id)
    user_result = await db.execute(select(User).where(User.id == verify_token.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise _INVALID_VERIFICATION_TOKEN

    user.is_verified = True
    verify_token.used_at = datetime.now(timezone.utc)
    await db.commit()
    # No session revocation here, unlike password reset — verifying an
    # email isn't a "the account might be compromised" event, so there's
    # nothing to protect existing sessions from. This is a deliberate
    # non-decision, not an oversight.
    return EmailVerifyResponse()


@router.post("/resend-verification", response_model=ResendVerificationResponse)
# Stricter than login/signup, same 5/minute as password-reset/request and
# for the same reason — plus a concrete second one once real email
# delivery exists: this becomes a way to flood a real inbox with unwanted
# verification emails for an address the caller doesn't own.
@limiter.limit("5/minute", key_func=get_remote_address)
async def resend_verification(
    request: Request, payload: ResendVerificationRequest, db: AsyncSession = Depends(get_db)
) -> ResendVerificationResponse:
    # Same enumeration-safety shape as request_password_reset: a token is
    # generated unconditionally, but only ever persisted (and only ever
    # valid) when the email resolves to a real, still-unverified account.
    # The response is identical either way — including for an email that's
    # real but *already* verified, which gets the same generic message
    # rather than a distinct "already verified" reply that would itself
    # leak account-existence information.
    token = secrets.token_urlsafe(32)

    lookup_result = await db.execute(select(UserLookup).where(UserLookup.email == payload.email))
    lookup = lookup_result.scalar_one_or_none()
    if lookup is not None:
        await set_tenant_context(db, lookup.org_id)
        user_result = await db.execute(select(User).where(User.id == lookup.user_id))
        user = user_result.scalar_one_or_none()
        if user is not None and not user.is_verified:
            link = await _issue_verification_token(db, user.id)
            return ResendVerificationResponse(verification_link=link)

    return ResendVerificationResponse(
        verification_link=f"{settings.frontend_url}/verify-email?token={token}"
    )


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

    # organizations has no RLS, so this is just a plain lookup by id.
    org_result = await db.execute(select(Organization).where(Organization.id == auth.org_id))
    org = org_result.scalar_one()

    return {
        "id": str(user.id),
        "email": user.email,
        "org_id": str(user.org_id),
        "org_name": org.name,
        # Exposed so the frontend can render a ticket's display id
        # (PREFIX-NUMBER) without a second fetch — see migration 0011.
        "ticket_prefix": org.ticket_prefix,
        "role": user.role.value,
        "org_user_count": org_user_count,
        # Soft-nudge, not a gate — see User.is_verified's docstring. The
        # frontend uses this to show a persistent-but-non-blocking banner,
        # not to lock anything.
        "is_verified": user.is_verified,
    }
