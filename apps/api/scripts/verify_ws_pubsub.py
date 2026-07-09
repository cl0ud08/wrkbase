"""Proves ticket-update events actually fan out over Redis pub/sub to every
connection subscribed to a project's room -- across two separate backend
*processes*, not just within one process's memory. That distinction is the
entire point: an in-process event emitter (a plain asyncio.Queue or a dict
of callbacks) would make every check in this file pass too, as long as
there's only ever one running API process — exactly the false confidence
this script exists to rule out. One of the two WebSocket connections below
is deliberately served by a second uvicorn process that never executes the
mutating PATCH's handler code at all; if it still receives the event, that
event could only have traveled through Redis.

Needs a second API process running alongside the one every other proof
script in this repo already assumes on port 8000:

    docker compose exec api uvicorn app.main:app --host 0.0.0.0 --port 8001 &
    docker compose exec api python -m scripts.verify_ws_pubsub
"""

import asyncio
import os
import uuid

import httpx
import redis.asyncio as redis
import websockets

PORT_A = "http://localhost:8000"
WS_PORT_A = "ws://localhost:8000"
WS_PORT_B = "ws://localhost:8001"
VALID_ORIGIN = "http://localhost:3000"
PASSWORD = "correct horse battery staple"


async def signup(client: httpx.AsyncClient, org_name: str, email: str) -> dict:
    resp = await client.post(
        "/auth/signup", json={"org_name": org_name, "email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()


def auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


async def create_project(client: httpx.AsyncClient, token: str, name: str) -> dict:
    resp = await client.post(
        "/projects", json={"name": name, "description": None}, headers=auth_headers(token)
    )
    resp.raise_for_status()
    return resp.json()


async def get_states(client: httpx.AsyncClient, token: str, project_id: str) -> list[dict]:
    resp = await client.get(f"/projects/{project_id}/workflow-states", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def create_ticket(client: httpx.AsyncClient, token: str, project_id: str, title: str) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": None},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def get_ws_ticket(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post("/auth/ws-ticket", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()["ticket"]


async def get_ticket(client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str) -> dict:
    resp = await client.get(f"/projects/{project_id}/tickets/{ticket_id}", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def wait_until_triaged(
    client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str, timeout: float = 15
) -> None:
    # Every ticket created below is also, as of Phase 3 slice 2, a trigger
    # for an async worker job that eventually publishes its own
    # ticket.updated event to this exact ticket's project channel -- the
    # same channel this script's "expect no message" checks listen on.
    # Undrained, that job can complete at any point after creation and
    # land in the middle of a later timing-sensitive assertion here,
    # exactly like the race verify_triage.py's own proof hit and fixed.
    # Draining both fixture tickets up front, before any WS connection
    # opens, removes the race instead of chasing it via a longer timeout.
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = await get_ticket(client, token, project_id, ticket_id)
        if t["triage_status"] != "pending":
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"ticket {ticket_id} was not triaged within {timeout}s")


async def move_ticket(
    client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str, workflow_state_id: str
) -> None:
    resp = await client.patch(
        f"/projects/{project_id}/tickets/{ticket_id}",
        json={"workflow_state_id": workflow_state_id},
        headers=auth_headers(token),
    )
    resp.raise_for_status()


async def expect_no_message(ws: websockets.ClientConnection, *, label: str) -> None:
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
    except asyncio.TimeoutError:
        print(f"PASS: {label} -> nothing received")
    else:
        raise AssertionError(f"{label}: expected no message, got {msg!r}")


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]

    async with httpx.AsyncClient(base_url=PORT_A) as client:
        tokens = await signup(client, f"PubSub Org {suffix}", f"pubsub-{suffix}@example.dev")
        token = tokens["access_token"]

        project = await create_project(client, token, "PubSub Project")
        other_project = await create_project(client, token, "PubSub Project (unrelated)")

        states = await get_states(client, token, project["id"])
        other_states = await get_states(client, token, other_project["id"])

        ticket = await create_ticket(client, token, project["id"], "Move me")
        other_ticket = await create_ticket(client, token, other_project["id"], "Should never be seen")

        # Drain both tickets' async triage jobs now, before any WS
        # connection exists to receive their completion events -- see
        # wait_until_triaged's docstring for why this has to happen here.
        await wait_until_triaged(client, token, project["id"], ticket["id"])
        await wait_until_triaged(client, token, other_project["id"], other_ticket["id"])

        ws_ticket_1 = await get_ws_ticket(client, token)
        ws_ticket_2 = await get_ws_ticket(client, token)

    channel = f"project:{project['id']}"
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

    # --- two tabs, deliberately on two different backend processes ---------
    tab1 = await websockets.connect(
        f"{WS_PORT_A}/ws/projects/{project['id']}?ticket={ws_ticket_1}", origin=VALID_ORIGIN
    )
    tab2 = await websockets.connect(
        f"{WS_PORT_B}/ws/projects/{project['id']}?ticket={ws_ticket_2}", origin=VALID_ORIGIN
    )
    print("PASS: two authenticated connections to the same project, on two separate backend processes")

    try:
        [(_, numsub)] = await r.pubsub_numsub(channel)
        assert numsub == 2, f"expected 2 subscribers on {channel}, got {numsub}"
        print("PASS: PUBSUB NUMSUB confirms both connections are real, independent Redis subscribers")

        # --- a mutation on the shared project reaches both tabs -------------
        async with httpx.AsyncClient(base_url=PORT_A) as client:
            await move_ticket(client, token, project["id"], ticket["id"], states[1]["id"])

        msg1 = await asyncio.wait_for(tab1.recv(), timeout=5)
        msg2 = await asyncio.wait_for(tab2.recv(), timeout=5)
        assert msg1 == msg2, "both tabs should receive the identical event payload"
        assert ticket["id"] in msg1, "event should reference the moved ticket"
        print(
            "PASS: a mutation applied via process A (8000) is received by a connection "
            "served entirely by process B (8001) -- proves real cross-process fan-out, "
            "not an in-process coincidence"
        )

        # --- a mutation on a DIFFERENT project is never received ------------
        async with httpx.AsyncClient(base_url=PORT_A) as client:
            await move_ticket(
                client, token, other_project["id"], other_ticket["id"], other_states[1]["id"]
            )
        await expect_no_message(tab1, label="unrelated project's mutation, tab1")
        await expect_no_message(tab2, label="unrelated project's mutation, tab2")

        # --- disconnecting actually cleans up the Redis subscription --------
        await tab1.close()
        await asyncio.sleep(0.5)  # let the server's finally: block run
        [(_, numsub_after)] = await r.pubsub_numsub(channel)
        assert numsub_after == 1, (
            f"expected 1 subscriber left on {channel} after tab1 disconnected, got {numsub_after} "
            "-- a lingering count here means the server isn't unsubscribing on disconnect"
        )
        print("PASS: closing one connection drops the Redis subscriber count -- no leaked subscription")

        # --- the still-open tab keeps working after the other one left ------
        async with httpx.AsyncClient(base_url=PORT_A) as client:
            await move_ticket(client, token, project["id"], ticket["id"], states[0]["id"])
        msg3 = await asyncio.wait_for(tab2.recv(), timeout=5)
        assert ticket["id"] in msg3
        print("PASS: the remaining connection keeps receiving events after the other one disconnected")
    finally:
        await tab2.close()
        await r.aclose()

    print("\nAll WebSocket pub/sub fan-out checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
