import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.db.models import Notification, NotificationType


def notification_channel(user_id: uuid.UUID) -> str:
    return f"notifications:{user_id}"


async def create_notification(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    type: NotificationType,
    payload: dict,
) -> Notification:
    """Inserts a notification row (uncommitted — the caller commits this
    together with whatever change triggered it, in one transaction, so a
    ticket reassignment can never succeed while its notification silently
    fails to save, or vice versa). Does NOT publish — call
    publish_notification once the caller's own commit has actually
    succeeded, so a rolled-back change can never push a live "notification
    created" event for a row that doesn't exist.

    Uses a raw INSERT with explicit, client-generated id/created_at rather
    than session.add(Notification(...)) for the same reason organizations'
    self-serve signup does (migration 0015): the ORM always issues an
    implicit RETURNING on INSERT to read server-generated columns back,
    and Postgres gates that RETURNING on the table's SELECT policy, not
    the INSERT policy that actually permitted the write. Here that's not
    a timing problem (like organizations' id-not-known-yet case) but a
    structural one — the acting user creating this notification is
    essentially never its recipient (an admin assigns a ticket to a
    teammate; a new user's signup notifies the admin who invited them),
    so notifications' SELECT policy (user_id = current_user_id) would
    reject the RETURNING every single time, for every trigger point this
    app has. Generating the values up front and skipping RETURNING
    entirely sidesteps the mismatch rather than fighting it.
    """
    notification = Notification(
        id=uuid.uuid4(),
        org_id=org_id,
        user_id=user_id,
        type=type,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    # CAST(...), not :type::notification_type -- SQLAlchemy's text() bind-
    # parameter parser gets confused by a `::` cast immediately following a
    # named parameter (it leaves ":type::notification_type" completely
    # unsubstituted, a real, confirmed-by-running-it gotcha, not a style
    # preference), and silently sends the literal ":type::notification_type"
    # text to asyncpg as if it were SQL, which fails with a syntax error
    # right next to the actual bug instead of on it.
    await db.execute(
        text(
            """
            INSERT INTO notifications (id, org_id, user_id, type, payload, created_at)
            VALUES (
                :id, :org_id, :user_id,
                CAST(:type AS notification_type), CAST(:payload AS jsonb),
                :created_at
            )
            """
        ),
        {
            "id": str(notification.id),
            "org_id": str(org_id),
            "user_id": str(user_id),
            "type": type.value,
            "payload": json.dumps(payload),
            "created_at": notification.created_at,
        },
    )
    return notification


async def publish_notification(notification: Notification) -> None:
    await redis_client.publish(
        notification_channel(notification.user_id),
        json.dumps(
            {
                "type": "notification.created",
                "notification": {
                    "id": str(notification.id),
                    "type": notification.type.value,
                    "payload": notification.payload,
                    "read_at": None,
                    "created_at": notification.created_at.isoformat(),
                },
            }
        ),
    )
