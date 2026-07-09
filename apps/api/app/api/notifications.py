import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import Notification
from app.db.session import get_db
from app.schemas.notification import NotificationPage, NotificationRead, UnreadCountResponse

router = APIRouter(prefix="/notifications", tags=["notifications"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")


@router.get("", response_model=NotificationPage)
async def list_notifications(
    limit: int = 20,
    offset: int = 0,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> NotificationPage:
    # No explicit user_id filter: select_own_notifications (migration
    # 0016) already restricts every SELECT here to auth.user_id
    # unconditionally — the same "don't redundantly re-filter what RLS
    # already guarantees" convention every other list endpoint in this
    # app follows for org_id.
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    base = select(Notification)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    result = await db.execute(
        base.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    )
    items = list(result.scalars().all())
    return NotificationPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    auth: AuthContext = Depends(get_current_auth), db: AsyncSession = Depends(get_db)
) -> UnreadCountResponse:
    result = await db.execute(
        select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))
    )
    return UnreadCountResponse(count=result.scalar_one())


@router.patch("/{notification_id}/read", response_model=NotificationRead)
async def mark_read(
    notification_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Notification:
    # Same "doesn't exist" vs "exists but isn't yours" ambiguity as every
    # other resource in this app: RLS already scopes this to auth.user_id,
    # so another user's real notification id simply matches zero rows.
    result = await db.execute(select(Notification).where(Notification.id == notification_id))
    notification = result.scalar_one_or_none()
    if notification is None:
        raise _NOT_FOUND
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
    return notification


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(
    auth: AuthContext = Depends(get_current_auth), db: AsyncSession = Depends(get_db)
) -> None:
    await db.execute(
        update(Notification)
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.commit()
