"""Proves the WebSocket handshake itself can't be hijacked, spoofed, or used
to reach a project the caller doesn't have access to -- no pub/sub or
message handling exists yet (Phase 2 slice 1), so this is entirely about
the four ways into the connection, not what happens after. Run after the
API is up:

    docker compose exec api python -m scripts.verify_ws
"""

import asyncio
import uuid

import httpx
import websockets
from websockets.exceptions import InvalidStatus

BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"
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


async def create_project(client: httpx.AsyncClient, access_token: str, name: str) -> dict:
    resp = await client.post(
        "/projects", json={"name": name, "description": None}, headers=auth_headers(access_token)
    )
    resp.raise_for_status()
    return resp.json()


async def get_ticket(client: httpx.AsyncClient, access_token: str) -> str:
    resp = await client.post("/auth/ws-ticket", headers=auth_headers(access_token))
    resp.raise_for_status()
    return resp.json()["ticket"]


def ws_url(project_id: str, ticket: str | None) -> str:
    suffix = f"?ticket={ticket}" if ticket else ""
    return f"{WS_URL}/ws/projects/{project_id}{suffix}"


async def assert_rejected_at_handshake(uri: str, *, origin: str = VALID_ORIGIN, label: str) -> None:
    """Confirms the upgrade is refused outright -- an HTTP-level rejection
    during connect() itself -- not accepted and then immediately closed.
    Those are different signals: InvalidStatus means the 101 Switching
    Protocols response never happened at all.
    """
    try:
        async with websockets.connect(uri, origin=origin, open_timeout=5):
            raise AssertionError(f"{label}: connection should have been rejected, but it succeeded")
    except InvalidStatus:
        print(f"PASS: {label} -> upgrade rejected at handshake, not accepted-then-closed")


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        tokens_a = await signup(client, f"WS Org A {suffix}", f"ws-a-{suffix}@example.dev")
        tokens_b = await signup(client, f"WS Org B {suffix}", f"ws-b-{suffix}@example.dev")
        project_a = await create_project(client, tokens_a["access_token"], "WS Project A")
        project_b = await create_project(client, tokens_b["access_token"], "WS Project B")
        print("PASS: two orgs set up, each with its own project")

        # --- 1. no ticket at all -------------------------------------------
        await assert_rejected_at_handshake(
            ws_url(project_a["id"], None), label="no ticket"
        )

        # --- 2. garbage/invalid ticket --------------------------------------
        await assert_rejected_at_handshake(
            ws_url(project_a["id"], "not-a-real-ticket"), label="invalid ticket"
        )

        # --- 3. mismatched Origin, otherwise a fully valid connection -------
        good_ticket_for_origin_check = await get_ticket(client, tokens_a["access_token"])
        await assert_rejected_at_handshake(
            ws_url(project_a["id"], good_ticket_for_origin_check),
            origin="http://evil.test",
            label="mismatched Origin",
        )

        # --- 4. valid ticket, but the caller's org doesn't own this project -
        ticket_wrong_project = await get_ticket(client, tokens_a["access_token"])
        await assert_rejected_at_handshake(
            ws_url(project_b["id"], ticket_wrong_project),
            label="valid ticket, no access to this project",
        )

        # --- 5. a ticket already redeemed once can't be reused (replay) ----
        replay_ticket = await get_ticket(client, tokens_a["access_token"])
        async with websockets.connect(ws_url(project_a["id"], replay_ticket), origin=VALID_ORIGIN):
            pass  # first use: legitimately succeeds and is closed immediately
        await assert_rejected_at_handshake(
            ws_url(project_a["id"], replay_ticket), label="reused (already-redeemed) ticket"
        )

        # --- 6. the real happy path: connects, survives idle, closes clean -
        happy_ticket = await get_ticket(client, tokens_a["access_token"])
        async with websockets.connect(
            ws_url(project_a["id"], happy_ticket), origin=VALID_ORIGIN
        ) as socket:
            print("PASS: valid ticket + correct Origin + real project access -> connection accepted")
            await asyncio.sleep(2)
            await socket.ping()
            print("PASS: connection survives an idle period (2s, plus a live ping)")
        print("PASS: client-initiated close completes cleanly, no exception")

    print("\nAll WebSocket handshake checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
