"""Phase 2: an authenticated WebSocket connection, proven secure (slice 1),
now forwarding live board events from Redis pub/sub to every connection
subscribed to a project's room (slice 2). The handshake's own protections
(CSWSH origin check, ticket auth, project-access check, bounded connection
lifetime) are unchanged from slice 1 — see their inline comments below.
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


@router.websocket("/ws/projects/{project_id}")
async def project_socket(websocket: WebSocket, project_id: uuid.UUID) -> None:
    # --- 1. CSWSH: verify Origin before anything else -----------------------
    # A WebSocket handshake is an HTTP Upgrade request, but the browser does
    # NOT apply the same-origin-policy response-blocking that protects a
    # normal fetch/XHR: a page on evil.com can open
    # `new WebSocket("wss://this-api/ws/projects/<guessed-id>")`, the
    # browser will happily attach this origin's ambient cookies to that
    # handshake exactly as it would to any other request to this host, and
    # complete the connection — there is no browser-enforced barrier
    # stopping evil.com's own JS from then reading and sending real
    # messages over the resulting socket, unlike XHR where SOP blocks the
    # response body from ever reaching the page. This is the literal CSWSH
    # (Cross-Site WebSocket Hijacking) attack, and app.main's CORSMiddleware
    # does nothing to stop it: Starlette's CORSMiddleware explicitly checks
    # `scope["type"] == "http"` and passes any other scope type — including
    # "websocket" — through untouched. CORS protection for HTTP endpoints
    # and CSWSH protection for this endpoint are two separate mechanisms;
    # only the second is written here, because nothing provides it for
    # free. Checked against the same cors_origins allowlist HTTP already
    # uses (an env var, since local dev and a future deployed frontend are
    # different origins) rather than a separate list, since "who is allowed
    # to act as this app's frontend" is one answer, not two.
    origin = websocket.headers.get("origin")
    if origin not in settings.cors_origins_list:
        await _reject(websocket, status.WS_1008_POLICY_VIOLATION)
        return

    # --- 2. Auth: a short-lived, single-use ticket, not a JWT in the URL ---
    # The browser WebSocket API (`new WebSocket(url, protocols?)`) has no
    # way to set an Authorization header on the handshake — that's a hard
    # client-side API limitation, not a server-side choice, which rules out
    # this app's existing Bearer-token path for any browser-originated
    # socket. The httpOnly access_token cookie is scoped to the frontend's
    # own origin via the same-origin Next.js rewrite proxy (see
    # next.config.ts) specifically so it can stay httpOnly; this project's
    # pinned Next.js version documents no support for that proxy forwarding
    # a WebSocket Upgrade request, so this socket connects directly to the
    # API's own origin instead of through it — meaning that cookie would
    # not reliably arrive here even if it weren't origin-scoped. See
    # app/services/ws_tickets.py for why a minted, single-use ticket is
    # used here instead of putting the real access token in the URL.
    ticket = websocket.query_params.get("ticket")
    claims = await redeem_ws_ticket(ticket) if ticket else None
    if claims is None:
        await _reject(websocket, status.WS_1008_POLICY_VIOLATION)
        return

    # user_id and role also ride along in the ticket's claims (see
    # ws_tickets.issue_ws_ticket) for the next slice's use — nothing in
    # this one needs anything but org_id yet.
    org_id = uuid.UUID(claims["org_id"])

    # --- 3. Confirm the caller can actually reach this project --------------
    # Same check every other resource in this app makes: RLS already scopes
    # this query to org_id, so a project from another org simply matches
    # zero rows, indistinguishable from a project that doesn't exist at
    # all — same "doesn't exist" vs. "exists in another org" ambiguity
    # _get_project_or_404 deliberately preserves for HTTP. A short-lived
    # session opened just for this one query, not held for the connection's
    # lifetime — see the module-level note below on why.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_id)
        result = await session.execute(
            select(Project.id).where(Project.id == project_id, Project.deleted_at.is_(None))
        )
        if result.scalar_one_or_none() is None:
            await _reject(websocket, status.WS_1008_POLICY_VIOLATION)
            return

    # --- 4. Accept, then bound how long this connection is trusted ---------
    # Tenant context in this app has only ever meant "set once per
    # transaction, for the one request currently using it" — RLS's
    # set_config(..., is_local=true) is explicitly discarded at the end of
    # whatever transaction set it (see set_tenant_context's docstring). A
    # WebSocket connection has no natural transaction boundary the way an
    # HTTP request does: it's not "one request," it's a channel that can
    # stay open for hours. Holding one DB session/transaction open for that
    # whole span just to keep app.current_org_id set would mean either an
    # idle transaction sitting on a Postgres connection for hours (its own
    # operational cost) or re-deriving org_id from the token again anyway
    # the moment a real query is needed. So org_id/user_id are kept as
    # plain Python values on this connection instead, and re-applied via a
    # fresh, short-lived set_tenant_context call each time (none yet — no
    # pub/sub or message handling in this slice) a query actually needs to
    # run, the same pattern the access-check above already uses.
    #
    # The harder question is staleness: is it safe to trust org_id/role for
    # as long as the socket happens to stay open? No — that would silently
    # reopen the exact class of bug fixed in Slice 12 (`POST /auth/refresh`
    # trusting a stale cached role indefinitely), just with an unbounded
    # window instead of a 15-minute one. If an admin demotes or removes
    # this user two minutes into a six-hour-old connection, that has to
    # take effect within the same bound every other credential in this app
    # already guarantees, not "whenever the socket happens to close." Two
    # options: poll the database on a timer to re-check the user still has
    # access, or simply stop trusting the connection once the access token
    # it was minted from would have expired anyway, and require a genuine
    # reconnect (a fresh ticket, a fresh handshake, a fresh access-check)
    # to continue. The second is simpler, needs no new re-check interval to
    # invent, and reuses a bound this app already treats as the ceiling for
    # every other credential's staleness — access_token_expire_minutes — so
    # a connection is never trusted for longer than an HTTP session would
    # be between refreshes.
    await websocket.accept()

    # --- 5. Subscribe to this project's room ---------------------------------
    # One Redis pub/sub connection per WebSocket connection, not one shared
    # subscription fanning out to many local listeners: two people with the
    # same project open still means two independent `.subscribe()` calls
    # against Redis. That's slightly more Redis-side connections than a
    # single shared-subscription-plus-in-process-demux design would use, but
    # it means there is no per-connection routing logic in this process that
    # a bug could get wrong — Redis itself is what fans one published
    # message out to every subscriber of a channel, which is exactly the
    # "leak across independently-authenticated sessions" risk requirement 2
    # asks about: there's no shared, mutable dispatch table here for a leak
    # to live in. Revisit if connection *count* ever becomes the actual
    # bottleneck — it is not, at this app's real scale.
    #
    # The channel name is keyed on project_id alone — the same project_id
    # already confirmed, above, to belong to this connection's own org via a
    # real RLS-scoped query. A connection to project A's channel structurally
    # cannot receive project B's events: nothing publishes to project A's
    # channel except a mutation that already passed project A's own
    # tenant-scoped lookup in tickets.py. There's no additional filtering
    # step here that could be gotten wrong, because there's nothing to
    # filter — subscribing to the right channel *is* the isolation.
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(project_channel(project_id))

    deadline = time.monotonic() + settings.access_token_expire_minutes * 60

    async def _client_loop() -> None:
        # Detects disconnect and (once this app has real inbound messages to
        # handle) would read them — no message handling yet, so inbound
        # frames are simply discarded. Runs concurrently with _redis_loop
        # below: reading and writing a WebSocket are independent ASGI
        # channels, so a slow or silent client doesn't block message
        # delivery, and vice versa.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    async def _redis_loop() -> None:
        # pubsub.listen() also yields the subscribe confirmation itself
        # (type "subscribe", not "message") — skipped, not forwarded, since
        # a client has no use for its own subscription's bookkeeping.
        # decode_responses=True on redis_client means message["data"] is
        # already the exact JSON string ticket_events.publish_ticket_update
        # produced; relayed as-is, no re-encoding needed on this side.
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
        # happy path. A pubsub object that's merely dropped (relying on GC)
        # would leave Redis still tracking this connection as a live
        # subscriber indefinitely; requirement 6's proof script checks this
        # via PUBSUB NUMSUB, not just trusts it.
        await pubsub.unsubscribe(project_channel(project_id))
        await pubsub.aclose()
