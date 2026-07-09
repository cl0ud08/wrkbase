import json
import secrets
import uuid

from app.core.redis import redis_client

_KEY_PREFIX = "ws_ticket:"

# Deliberately short: a ticket only has to survive the gap between "the
# frontend receives it in a JSON response" and "the frontend opens the
# WebSocket connection with it in the URL" — a fraction of a second in
# practice. Keeping that window small matters because unlike the access
# token this bridges from, this ticket travels in a URL: it can end up in
# server access logs and proxy logs for as long as it's valid. 30 seconds
# bounds that exposure to a fraction of what a leaked access token
# (~15 minutes, reusable) or refresh token (7 days) would cost.
_TICKET_TTL_SECONDS = 30


def _key(ticket: str) -> str:
    return f"{_KEY_PREFIX}{ticket}"


async def issue_ws_ticket(*, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    """Mint a single-use credential for one WebSocket handshake.

    Exists because neither of this app's two normal auth channels reaches a
    browser-originated WebSocket handshake: the browser WebSocket API has no
    way to set an Authorization header, and the httpOnly access_token cookie
    is scoped to the frontend's own origin (via the same-origin Next.js
    rewrite proxy — see next.config.ts), not to the API's origin a socket
    connects to directly. Rather than put the actual access token in the
    connection URL — a real, long-lived credential that would then persist
    in logs for its full remaining lifetime — this mints a ticket that's
    good for exactly one handshake, the same "opaque, single-use, redeemed
    once" shape already used for invite/password-reset/email-verification
    tokens.
    """
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"user_id": str(user_id), "org_id": str(org_id), "role": role})
    await redis_client.set(_key(ticket), payload, ex=_TICKET_TTL_SECONDS)
    return ticket


async def redeem_ws_ticket(ticket: str) -> dict | None:
    """Fetch-and-delete in one round trip, same GETDEL reasoning as
    pop_refresh_token: two near-simultaneous handshakes presenting the same
    ticket (e.g. a client retry racing the original) must not both succeed.
    """
    payload = await redis_client.getdel(_key(ticket))
    if payload is None:
        return None
    return json.loads(payload)
