import json
import uuid

from app.core.config import settings
from app.core.redis import redis_client

_KEY_PREFIX = "refresh_token:"
_USER_TOKENS_PREFIX = "refresh_tokens_by_user:"


def _key(token: str) -> str:
    return f"{_KEY_PREFIX}{token}"


def _user_tokens_key(user_id: uuid.UUID) -> str:
    return f"{_USER_TOKENS_PREFIX}{user_id}"


async def store_refresh_token(token: str, *, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> None:
    ttl_seconds = settings.refresh_token_expire_days * 24 * 60 * 60
    payload = json.dumps({"user_id": str(user_id), "org_id": str(org_id), "role": role})
    await redis_client.set(_key(token), payload, ex=ttl_seconds)

    # Reverse index (user_id -> its live tokens), maintained alongside the
    # forward key so a member removal (see revoke_all_refresh_tokens_for_user
    # below) can find and kill every outstanding session for that user
    # without scanning the whole keyspace. The set's own TTL is reset on
    # every add so it roughly tracks the freshest token's lifetime — it can
    # end up holding a few already-expired token strings between refreshes
    # (Redis key expiry doesn't fire a callback to SREM them out), but that's
    # harmless: deleting an already-gone key is a no-op, not an error.
    user_key = _user_tokens_key(user_id)
    await redis_client.sadd(user_key, token)
    await redis_client.expire(user_key, ttl_seconds)


async def pop_refresh_token(token: str) -> dict | None:
    """Fetch-and-delete in one round trip: a refresh token is single-use.

    Using GETDEL instead of GET-then-DEL closes the race where two near-
    simultaneous refresh calls with the same token could both read it as
    valid before either deletes it — only the first caller can ever get a
    hit; a concurrent second call sees it already gone.
    """
    payload = await redis_client.getdel(_key(token))
    if payload is None:
        return None
    data = json.loads(payload)
    await redis_client.srem(_user_tokens_key(uuid.UUID(data["user_id"])), token)
    return data


async def revoke_refresh_token(token: str) -> None:
    payload = await redis_client.get(_key(token))
    await redis_client.delete(_key(token))
    if payload is not None:
        data = json.loads(payload)
        await redis_client.srem(_user_tokens_key(uuid.UUID(data["user_id"])), token)


async def revoke_all_refresh_tokens_for_user(user_id: uuid.UUID) -> None:
    """Kill every outstanding refresh token for a user in one shot — used
    when a member is removed from their org (see app/api/org.py), so a
    departed member can't silently mint fresh access tokens via refresh
    after their access is revoked. Their current (already-issued) access
    token is a separate, smaller gap: it's a stateless JWT with no server
    -side record to delete, so it simply expires naturally within its
    15-minute lifetime — this closes the much larger "indefinitely, via
    refresh" gap, not that last 15 minutes.
    """
    user_key = _user_tokens_key(user_id)
    tokens = await redis_client.smembers(user_key)
    if tokens:
        await redis_client.delete(*(_key(t) for t in tokens))
    await redis_client.delete(user_key)
