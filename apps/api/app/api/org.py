import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import User, UserRole
from app.db.session import get_db
from app.schemas.member import MemberRead, MemberUpdate
from app.services.refresh_tokens import revoke_all_refresh_tokens_for_user

router = APIRouter(prefix="/org", tags=["org"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")


def _require_admin(auth: AuthContext) -> None:
    if auth.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only an org admin can manage members"
        )


async def _get_member_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    # RLS scopes this to the caller's org already, same as every other
    # _get_x_or_404 in this app — a user id from another org is
    # indistinguishable from one that doesn't exist.
    result = await db.execute(select(User).where(User.id == user_id))
    member = result.scalar_one_or_none()
    if member is None:
        raise _NOT_FOUND
    return member


async def _admin_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(User).where(User.role == UserRole.ADMIN))
    return result.scalar_one()


async def _ensure_not_last_admin(db: AsyncSession, target: User) -> None:
    # The lockout this guards against: an org that reaches zero admins has
    # no path back — every admin-gated action (inviting people, managing
    # members, reconfiguring the board) becomes permanently unreachable
    # through the API, with no "reset" short of a manual DB fix. It has to
    # be enforced at the point of the mutating action (here), not checked
    # periodically, since the bad state is irreversible the instant it's
    # created. A demotion away from admin has the exact same failure mode
    # as a removal, so both call sites below share this one guard rather
    # than only covering DELETE, which is all the brief literally named.
    if target.role != UserRole.ADMIN:
        return
    count = await _admin_count(db)
    if count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove or demote the last remaining admin of an organization",
        )


@router.get("/members", response_model=list[MemberRead])
async def list_members(
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    # Open to any org member, not admin-only — same reasoning as listing
    # workflow states: everyone needs to see who's on the team (e.g. to
    # know who a ticket's creator is), only *changing* membership is
    # privileged.
    result = await db.execute(select(User).order_by(User.created_at))
    return list(result.scalars().all())


@router.patch("/members/{user_id}", response_model=MemberRead)
async def update_member_role(
    user_id: uuid.UUID,
    payload: MemberUpdate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> User:
    _require_admin(auth)
    member = await _get_member_or_404(db, user_id)

    if payload.role != member.role:
        await _ensure_not_last_admin(db, member)

    member.role = payload.role
    await db.commit()
    return member


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_admin(auth)
    member = await _get_member_or_404(db, user_id)
    await _ensure_not_last_admin(db, member)

    # Projects/tickets this member created are kept, not deleted — see
    # migration 0008 and Project/Ticket.created_by in app/db/models.py.
    # user_lookup cascades automatically (ON DELETE CASCADE).
    await db.delete(member)
    await db.commit()

    # Closes the "removed but still has a working session indefinitely"
    # gap: every outstanding refresh token for this user is killed, so
    # they can't silently mint fresh access tokens after this point. Done
    # AFTER the commit, not before: if the commit had failed, revoking
    # their sessions first would've locked out someone who's actually
    # still a legitimate member. Their current access token (if any) is a
    # separate, smaller residual gap — a stateless JWT with no server-side
    # record to delete, so it simply expires naturally within its 15-
    # minute lifetime.
    await revoke_all_refresh_tokens_for_user(member.id)
