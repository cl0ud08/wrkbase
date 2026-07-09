import json
import uuid

from app.core.redis import redis_client


# One function builds the channel name for both sides (tickets.py publishes
# to it, app/api/ws.py subscribes to it) so there's exactly one place a typo
# could ever cause a mismatch, not two copies that have to be kept in sync
# by hand.
def project_channel(project_id: uuid.UUID) -> str:
    return f"project:{project_id}"


def _json_safe(value: object) -> object:
    return str(value) if isinstance(value, uuid.UUID) else value


async def publish_ticket_update(
    *, project_id: uuid.UUID, ticket_id: uuid.UUID, changes: dict[str, object], updated_by: uuid.UUID
) -> None:
    """Broadcast a board-relevant ticket change to everyone connected to this
    project's room.

    `changes` is deliberately a minimal diff, not the full ticket: enough
    for a connected client to splice the update into its local state (the
    same shape update_ticket's own caller already sends, and the same shape
    the frontend's optimistic drag-end handler already knows how to apply)
    without a round trip back to the database. It only ever contains
    fields from tickets.py's _COLLABORATIVE_FIELDS — this event type
    exists for board interactions (move, assign, plan into a sprint), not
    content edits (title/description/type/parent_id), which never
    broadcast at all. Redis pub/sub messages have no delivery guarantee to
    a client that's briefly disconnected (see app/api/ws.py's connection
    lifetime notes) -- that's an accepted gap for this slice, closed by
    the client's normal fetch-on-load covering anything missed, not by
    replaying history here.
    """
    payload = {
        "type": "ticket.updated",
        "project_id": str(project_id),
        "ticket_id": str(ticket_id),
        "changes": {k: _json_safe(v) for k, v in changes.items()},
        "updated_by": str(updated_by),
    }
    await redis_client.publish(project_channel(project_id), json.dumps(payload))
