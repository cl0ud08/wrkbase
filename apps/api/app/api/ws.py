"""Phase 2: authenticated, tenant-safe WebSocket rooms. Two independent
kinds now exist — a per-project board room (slice 1: the connection
itself; slice 2: ticket-move fan-out) and a per-user notification room
(slice 3) — sharing everything that isn't specific to what each room is
scoped by. The handshake protections (CSWSH origin check, ticket auth) and
the bounded-connection-lifetime room loop are identical between them,
extracted into _authenticate_handshake/_run_room below rather than
duplicated a second time; only the resource-access check and the channel
name differ per room.
"""

import asyncio
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.core.config import settings
from app.core.redis import redis_client
from app.db.models import Project
from app.db.session import AsyncSessionLocal, set_tenant_context
from app.services.notifications import notification_channel
from app.services.ticket_events import project_channel
from app.services.ws_tickets import redeem_ws_ticket

router = APIRouter()


async def _reject(websocket: WebSocket, code: int) -> None:
    # Calling close() *before* accept() is what makes this a rejected
    # upgrade, not an accepted-then-closed connection: the ASGI server
    # (uvicorn) responds to the handshake's HTTP request with a non-101
    # status instead of completing the 101 Switching Protocols upgrade at
    # all. A client seeing "connection refused during handshake" is a
    # materially different, and correct, signal from "connected, then
    # immediately disconnected" — the former never establishes a socket a
    # misbehaving or slow-to-notice client could briefly treat as live.
    await websocket.close(code=code)


async def _authenticate_handshake(websocket: WebSocket) -> dict | None:
    """CSWSH + ticket auth, common to every WS room in this app. Returns
    the redeemed ticket's claims (user_id/org_id/role) on success; on
    failure, the connection has already been rejected and the caller
    should simply return.

    --- CSWSH: verify Origin before anything else ---------------------------
    A WebSocket handshake is an HTTP Upgrade request, but the browser does
    NOT apply the same-origin-policy response-blocking that protects a
    normal fetch/XHR: a page on evil.com can open
    `new WebSocket("wss://this-api/ws/...")`, the browser will happily
    attach this origin's ambient cookies to that handshake exactly as it
    would to any other request to this host, and complete the connection —
    there is no browser-enforced barrier stopping evil.com's own JS from
    then reading and sending real messages over the resulting socket,
    unlike XHR where SOP blocks the response body from ever reaching the
    page. This is the literal CSWSH (Cross-Site WebSocket Hijacking)
    attack, and app.main's CORSMiddleware does nothing to stop it:
    Starlette's CORSMiddleware explicitly checks `scope["type"] == "http"`
    and passes any other scope type — including "websocket" — through
    untouched. Checked against the same cors_origins allowlist HTTP
    already uses (an env var, since local dev and a future deployed
    frontend are different origins) rather than a separate list, since
    "who is allowed to act as this app's frontend" is one answer, not two.

    --- Auth: a short-lived, single-use ticket, not a JWT in the URL -------
    The browser WebSocket API (`new WebSocket(url, protocols?)`) has no
    way to set an Authorization header on the handshake — a hard
    client-side API limitation, not a server-side choice, which rules out
    this app's existing Bearer-token path for any browser-originated
    socket. The httpOnly access_token cookie is scoped to the frontend's
    own origin via the same-origin Next.js rewrite proxy (see
    next.config.ts) specifically so it can stay httpOnly; this project's
    pinned Next.js version documents no support for that proxy forwarding
    a WebSocket Upgrade request, so every room here connects directly to
    the API's own origin instead of through it — meaning that cookie would
    not reliably arrive even if it weren't origin-scoped. See
    app/services/ws_tickets.py for why a minted, single-use ticket is used
    instead of putting the real access token in the URL.
    """
    origin = websocket.headers.get("origin")
    if origin not in settings.cors_origins_list:
        await _reject(websocket, status.WS_1008_POLICY_VIOLATION)
        return None

    ticket = websocket.query_params.get("ticket")
    claims = await redeem_ws_ticket(ticket) if ticket else None
    if claims is None:
        await _reject(websocket, status.WS_1008_POLICY_VIOLATION)
        return None
    return claims


async def _run_room(websocket: WebSocket, channel: str) -> None:
    """Accepts an already-authorized connection, subscribes it to `channel`,
    and runs it until disconnect, forced expiry, or error — identical for
    every room in this app once the room-specific access check (if any)
    has passed. Cleanly unsubscribed and closed on every exit path.

    --- Accept, then bound how long this connection is trusted -------------
    Tenant context in this app has only ever meant "set once per
    transaction, for the one request currently using it" — RLS's
    set_config(..., is_local=true) is explicitly discarded at the end of
    whatever transaction set it. A WebSocket connection has no natural
    transaction boundary the way an HTTP request does: it's not "one
    request," it's a channel that can stay open for hours. Any DB access a
    room needs re-derives its own context fresh and short-lived (see
    project_socket's access check below) rather than holding one session
    open for the connection's whole life.

    The harder question is staleness: is it safe to trust this connection
    for as long as the socket happens to stay open? No — that would
    silently reopen the exact class of bug fixed in Slice 12 (`POST
    /auth/refresh` trusting a stale cached role indefinitely), just with an
    unbounded window instead of a 15-minute one. Rather than invent a
    polling interval to re-check access on a timer, the connection is
    simply force-closed once the access token it was minted from would
    have expired anyway, requiring a genuine reconnect — a fresh ticket, a
    fresh handshake, a fresh access check — to continue. That reuses a
    bound this app already treats as the ceiling for every other
    credential's staleness (access_token_expire_minutes), so no room here
    is ever trusted longer than an ordinary HTTP session would be between
    refreshes.

    --- One dedicated Redis subscription per connection ---------------------
    Not one shared subscription fanning out to many local listeners: two
    people connected to the same channel still means two independent
    `.subscribe()` calls against Redis. That costs slightly more Redis-side
    connections than a shared-subscription-plus-in-process-demux design
    would, but it means there is no per-connection routing table in this
    process that a bug could get wrong — Redis itself is what fans one
    published message out to every subscriber of a channel. There's
    nothing here for a leak between independently-authenticated sessions
    to live in, because there's no shared, mutable dispatch state to leak
    from. Revisit if connection *count* ever becomes the actual
    bottleneck — it is not, at this app's real scale.
    """
    await websocket.accept()

    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)

    deadline = time.monotonic() + settings.access_token_expire_minutes * 60

    async def _client_loop() -> None:
        # Detects disconnect and (once a room has real inbound messages to
        # handle) would read them — no room in this app has message
        # handling yet, so inbound frames are simply discarded. Runs
        # concurrently with _redis_loop: reading and writing a WebSocket
        # are independent ASGI channels, so a slow or silent client
        # doesn't block message delivery, and vice versa.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    async def _redis_loop() -> None:
        # pubsub.listen() also yields the subscribe confirmation itself
        # (type "subscribe", not "message") — skipped, not forwarded.
        # decode_responses=True on redis_client means message["data"] is
        # already the exact JSON string the publisher produced; relayed
        # as-is, no re-encoding needed on this side.
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            await websocket.send_text(message["data"])

    try:
        client_task = asyncio.create_task(_client_loop())
        redis_task = asyncio.create_task(_redis_loop())
        expiry_task = asyncio.create_task(asyncio.sleep(max(deadline - time.monotonic(), 0)))

        done, pending = await asyncio.wait(
            {client_task, redis_task, expiry_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task is not expiry_task and task.exception() is not None:
                raise task.exception()  # surface a real bug instead of swallowing it

        if expiry_task in done:
            await websocket.close(
                code=status.WS_1000_NORMAL_CLOSURE, reason="Session expired — reconnect"
            )
    except WebSocketDisconnect:
        pass
    finally:
        # Cleanly unsubscribed and closed on every exit path — normal
        # disconnect, expiry, or an unhandled error above — not just the
        # happy path. A pubsub object that's merely dropped (relying on
        # GC) would leave Redis still tracking this connection as a live
        # subscriber indefinitely.
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@router.websocket("/ws/projects/{project_id}")
async def project_socket(websocket: WebSocket, project_id: uuid.UUID) -> None:
    claims = await _authenticate_handshake(websocket)
    if claims is None:
        return
    org_id = uuid.UUID(claims["org_id"])

    # Confirm the caller can actually reach this project — same check
    # every other resource in this app makes: RLS already scopes this
    # query to org_id, so a project from another org simply matches zero
    # rows, indistinguishable from a project that doesn't exist at all —
    # same "doesn't exist" vs. "exists in another org" ambiguity
    # _get_project_or_404 deliberately preserves for HTTP. A short-lived
    # session opened just for this one query, not held for the
    # connection's lifetime — see _run_room's docstring for why.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_id)
        result = await session.execute(
            select(Project.id).where(Project.id == project_id, Project.deleted_at.is_(None))
        )
        if result.scalar_one_or_none() is None:
            await _reject(websocket, status.WS_1008_POLICY_VIOLATION)
            return

    await _run_room(websocket, project_channel(project_id))


@router.websocket("/ws/notifications")
async def notifications_socket(websocket: WebSocket) -> None:
    # No resource-access check needed beyond the ticket itself: unlike a
    # project (which a user might not have access to), there's nothing
    # further to authorize here — the ticket's own user_id claim *is* the
    # room. See app/services/notifications.py's module docstring for why
    # this is a genuinely separate room from project_socket above, not the
    # existing project room reused: a notification has to reach a user
    # regardless of which project (if any) they're currently looking at,
    # so its channel can't be keyed on project_id at all, and this
    # connection is opened once at the shell-layout level (alive on every
    # authenticated page), not per-project-page the way project_socket is.
    claims = await _authenticate_handshake(websocket)
    if claims is None:
        return
    user_id = uuid.UUID(claims["user_id"])
    await _run_room(websocket, notification_channel(user_id))
