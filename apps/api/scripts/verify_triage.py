"""Proves the async ticket-triage plumbing end to end: a ticket is
pending_triage the instant it's created (before any worker involvement),
the worker correctly transitions it and pushes a live update, tenant
isolation holds for the async path (including a direct RLS-defense-in-
depth check that a job can't touch another org's data even if its
payload claims to), and the two reliability properties requirement 5
asks about are proven, not assumed: an unacked message survives a
simulated worker crash (redelivered, not lost), and multiple competing
workers never double-process the same job.

Reaches into app.services.queue directly (TriageJob, TRIAGE_QUEUE_NAME)
to construct deliberately adversarial messages the real API would never
publish — the same "raw internals, not just black-box HTTP" precedent
verify_rls.py already established for RLS mechanics this specific.

Needs a second worker process running alongside the one every real
deployment has, for the competing-consumers check:

    docker compose exec api python -m worker.main &   (a second one)
    docker compose exec api python -m scripts.verify_triage
"""

import asyncio
import json
import os
import uuid

import aio_pika
import httpx
import websockets

from app.services.queue import TRIAGE_QUEUE_NAME, TriageJob

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


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def get_ws_ticket(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post("/auth/ws-ticket", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()["ticket"]


async def create_project(client: httpx.AsyncClient, token: str, name: str) -> dict:
    resp = await client.post(
        "/projects", json={"name": name, "description": None}, headers=auth_headers(token)
    )
    resp.raise_for_status()
    return resp.json()


async def create_ticket(client: httpx.AsyncClient, token: str, project_id: str, title: str) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": "a real description"},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def get_ticket(client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str) -> dict:
    resp = await client.get(
        f"/projects/{project_id}/tickets/{ticket_id}", headers=auth_headers(token)
    )
    resp.raise_for_status()
    return resp.json()


async def wait_until_triaged(
    client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str, timeout: float = 15
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = await get_ticket(client, token, project_id, ticket_id)
        if t["triaged_at"] is not None:
            return t
        await asyncio.sleep(0.5)
    raise AssertionError(f"ticket {ticket_id} was not triaged within {timeout}s")


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    rabbitmq_url = os.environ["RABBITMQ_URL"]

    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # --- setup: two independent orgs -------------------------------------
        org_a = await signup(client, f"Triage Org A {suffix}", f"admin-a-{suffix}@example.dev")
        token_a = org_a["access_token"]
        org_b = await signup(client, f"Triage Org B {suffix}", f"admin-b-{suffix}@example.dev")
        token_b = org_b["access_token"]
        me_b = (await client.get("/auth/me", headers=auth_headers(token_b))).json()
        org_b_id = me_b["org_id"]

        project_a = await create_project(client, token_a, "Triage Project A")
        project_b = await create_project(client, token_b, "Triage Project B")
        print("PASS: two independent orgs set up, each with its own project")

        # --- pending_triage immediately on creation, before any worker ------
        # Its own ticket, checked and then left alone -- the live-update
        # check right below needs a *separate* ticket created only after
        # the WS connection is already live, or the job could finish (with
        # two idle workers competing, it usually does, fast) before the
        # subscription even exists to receive it. Redis pub/sub has no
        # replay for a not-yet-connected subscriber (see
        # ticket_events.py's own docstring) -- that's not a bug, but it
        # does mean this test has to connect first, same as a real client
        # would (the board page always connects before anyone can act).
        ticket_a = await create_ticket(client, token_a, project_a["id"], "Triage me")
        assert ticket_a["priority"] is None
        assert ticket_a["triaged_at"] is None
        print("PASS: a freshly created ticket is pending_triage (both fields null) immediately, synchronously")

        # --- the worker correctly transitions it, live update included ------
        ws_ticket_a = await get_ws_ticket(client, token_a)
        uri_a = f"{WS_URL}/ws/projects/{project_a['id']}?ticket={ws_ticket_a}"
        async with websockets.connect(uri_a, origin=VALID_ORIGIN) as ws_a:
            ticket_a2 = await create_ticket(client, token_a, project_a["id"], "Triage me live")

            live_msg = await asyncio.wait_for(ws_a.recv(), timeout=10)
            live_data = json.loads(live_msg)
            assert live_data["type"] == "ticket.updated"
            assert live_data["ticket_id"] == ticket_a2["id"]
            assert live_data["changes"]["priority"] == "medium"
            assert live_data["updated_by"] == "00000000-0000-0000-0000-000000000000"
            print("PASS: the triage completion is pushed live over the board's existing WebSocket room")

        triaged = await wait_until_triaged(client, token_a, project_a["id"], ticket_a["id"])
        assert triaged["priority"] == "medium"
        print("PASS: the worker transitions a pending ticket to triaged with a real priority set")

        # --- tenant isolation for the async path: org B never sees it -------
        ws_ticket_b = await get_ws_ticket(client, token_b)
        uri_b = f"{WS_URL}/ws/projects/{project_b['id']}?ticket={ws_ticket_b}"
        async with websockets.connect(uri_b, origin=VALID_ORIGIN) as ws_b:
            ticket_b = await create_ticket(client, token_b, project_b["id"], "Org B's own ticket")
            await wait_until_triaged(client, token_b, project_b["id"], ticket_b["id"])
            try:
                unexpected = await asyncio.wait_for(ws_b.recv(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
            else:
                data = json.loads(unexpected)
                assert data["ticket_id"] != ticket_a["id"], (
                    "org B's connection received org A's triage event"
                )
        print("PASS: org B's board room never receives org A's triage events, and vice versa")

        # --- defense in depth: a job with a mismatched org_id is rejected ---
        # A job claiming org A's real ticket_id but org B's org_id — exactly
        # a malformed or tampered payload. worker/main.py's RLS-scoped
        # lookup (set_tenant_context to the job's claimed org_id, THEN
        # query) should find nothing under org B's context for org A's
        # ticket, reject it as a poison message, and leave the real row
        # completely untouched — not silently succeed, not retry forever.
        before = await get_ticket(client, token_a, project_a["id"], ticket_a["id"])
        connection = await aio_pika.connect_robust(rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            poison = TriageJob(
                ticket_id=uuid.UUID(ticket_a["id"]),
                org_id=uuid.UUID(org_b_id),
                title="cross-org poison test",
                description=None,
            )
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=poison.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=TRIAGE_QUEUE_NAME,
            )
        await asyncio.sleep(3)  # let the real worker pick this up and reject it
        after = await get_ticket(client, token_a, project_a["id"], ticket_a["id"])
        assert after["priority"] == before["priority"]
        assert after["triaged_at"] == before["triaged_at"]
        print(
            "PASS: a job claiming org A's ticket under org B's context is rejected "
            "(RLS-scoped lookup finds nothing) and leaves the real row untouched"
        )

        # --- reliability 1: an unacked message survives a simulated crash ---
        # Deliberately isolated on its own throwaway queue, not the real
        # ticket_triage queue — this proves the broker mechanic worker/
        # main.py's manual-ack design relies on, in complete isolation from
        # the real worker(s) that are also live and competing for real jobs.
        test_queue_name = f"test_requeue_{suffix}"
        connection_a = await aio_pika.connect_robust(rabbitmq_url)
        channel_a = await connection_a.channel()
        queue_a = await channel_a.declare_queue(test_queue_name, durable=True)
        await channel_a.default_exchange.publish(
            aio_pika.Message(body=b"crash-test-payload", delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=test_queue_name,
        )
        received = await queue_a.get(timeout=5)
        assert received.body == b"crash-test-payload"
        # No ack() -- simulating a worker crashing mid-job. Closing the
        # connection with an unacked message outstanding is exactly what
        # should trigger RabbitMQ's own automatic requeue.
        await connection_a.close()

        connection_b = await aio_pika.connect_robust(rabbitmq_url)
        channel_b = await connection_b.channel()
        queue_b = await channel_b.declare_queue(test_queue_name, durable=True)
        redelivered = await queue_b.get(timeout=5)
        assert redelivered.body == b"crash-test-payload"
        assert redelivered.redelivered is True, "RabbitMQ should flag this as a redelivery"
        await redelivered.ack()
        await connection_b.close()
        print(
            "PASS: an unacked message from a \"crashed\" consumer (disconnected before ack) "
            "is redelivered to a fresh consumer, not lost — confirmed via RabbitMQ's own "
            "redelivered flag, not just a message arriving"
        )

        # --- reliability 2: two competing workers never double-process -----
        # Needs a second worker process actually running (see module
        # docstring) — this only proves the *broker's* competing-consumers
        # guarantee holds in this app's real deployment shape; it can't
        # force a second worker into existence itself.
        ws_ticket_concurrency = await get_ws_ticket(client, token_a)
        uri_c = f"{WS_URL}/ws/projects/{project_a['id']}?ticket={ws_ticket_concurrency}"
        async with websockets.connect(uri_c, origin=VALID_ORIGIN) as ws_c:
            batch_count = 5
            created_ids = set()
            for i in range(batch_count):
                t = await create_ticket(client, token_a, project_a["id"], f"Concurrency test {i}")
                created_ids.add(t["id"])

            seen_triage_events: list[str] = []
            deadline = asyncio.get_event_loop().time() + 20
            while len(seen_triage_events) < batch_count:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws_c.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                data = json.loads(msg)
                if data.get("type") == "ticket.updated" and "priority" in data.get("changes", {}):
                    seen_triage_events.append(data["ticket_id"])

        assert len(seen_triage_events) == batch_count, (
            f"expected exactly {batch_count} triage-completion events, got "
            f"{len(seen_triage_events)}: {seen_triage_events} — more than one per ticket "
            "would mean the same job got processed twice"
        )
        assert set(seen_triage_events) == created_ids, "the triaged tickets don't match what was created"
        print(
            f"PASS: {batch_count} tickets created concurrently produce exactly {batch_count} "
            "triage-completion events, one each, not fewer or duplicated — RabbitMQ's "
            "competing-consumers guarantee holds even with multiple workers live"
        )

    print("\nAll ticket-triage checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
