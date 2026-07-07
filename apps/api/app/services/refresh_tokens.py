import json
import uuid

from app.core.config import settings
from app.core.redis import redis_client

_KEY_PREFIX = "refresh_token:"


def _key(token: str) -> str:
    return f"{_KEY_PREFIX}{token}"


async def store_refresh_token(token: str, *, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> None:
    ttl_seconds = settings.refresh_token_expire_days * 24 * 60 * 60
    payload = json.dumps({"user_id": str(user_id), "org_id": str(org_id), "role": role})
    await redis_client.set(_key(token), payload, ex=ttl_seconds)


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
    return json.loads(payload)


async def revoke_refresh_token(token: str) -> None:
    await redis_client.delete(_key(token))
