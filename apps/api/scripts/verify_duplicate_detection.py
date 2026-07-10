"""Proves semantic duplicate detection end to end: the async embed job
actually populates a ticket's embedding after creation and again after
an edit that changes title/description (point 6's stale-embedding
handling), the similarity query is scoped correctly (a near-duplicate
never surfaces across a project or org boundary, even when the
underlying vectors are byte-for-byte identical), and the threshold
behaves as tuned (a genuinely similar draft matches, a genuinely
different one doesn't).

Runs against the same local LLM stub as verify_triage.py and
verify_ticket_parse.py, for the same reasons (see verify_triage.py's
module docstring for the full real-vs-stub reasoning) -- with one
addition specific to embeddings: since there's no real semantic
meaning to approximate cheaply, the stub returns one of three
*geometrically constructed* deterministic vectors with exactly known
cosine similarities to each other (BASE-NEAR = 0.9, BASE-FAR = 0.0),
selected by a plainly-named sentinel in the request text
("stub_embed_near" / "stub_embed_far") -- see
scripts/_fake_llm_server.py's own docstring. This tests the plumbing
this app's own code owns (the pgvector query, its scoping, the
threshold comparison, re-embedding on edit), not whether Gemini's real
embeddings are semantically good enough for the tuned threshold to mean
anything against real ticket text -- that's
scripts/tune_duplicate_threshold.py's job, against the real API, run
manually.

Reaches into the database directly (AsyncSessionLocal + set_tenant_context)
to poll for a ticket's embedding landing -- the same "raw internals, not
just black-box HTTP" precedent verify_rls.py already established --
since TicketRead deliberately never serializes the raw embedding vector
over the API (nothing needs a 768-float array in a JSON response).

Needs the fake LLM server and at least one worker process running,
exactly like verify_triage.py:

    docker compose exec api python -m scripts._fake_llm_server &
    GROQ_BASE_URL=http://localhost:9100 GEMINI_BASE_URL=http://localhost:9100 \\
        docker compose exec api python -m worker.main &
    docker compose exec api python -m scripts.verify_duplicate_detection
"""

import asyncio
import uuid

import httpx
from sqlalchemy import select

from app.db.models import Ticket
from app.db.session import AsyncSessionLocal, set_tenant_context

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"

# See scripts/_fake_llm_server.py: any text with neither marker embeds
# to _VECTOR_BASE; "stub_embed_near" -> _VECTOR_NEAR (0.9 similarity to
# BASE); "stub_embed_far" -> _VECTOR_FAR (0.0 similarity to BASE, 0.436
# to NEAR) -- both well below SIMILARITY_THRESHOLD (0.83).
_NEAR_QUERY = "stub_embed_near probe text"
_FAR_QUERY = "stub_embed_far probe text"


async def signup(client: httpx.AsyncClient, org_name: str, email: str) -> dict:
    resp = await client.post(
        "/auth/signup", json={"org_name": org_name, "email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def get_org_id(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get("/auth/me", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()["org_id"]


async def create_project(client: httpx.AsyncClient, token: str, name: str) -> dict:
    resp = await client.post(
        "/projects", json={"name": name, "description": None}, headers=auth_headers(token)
    )
    resp.raise_for_status()
    return resp.json()


async def create_ticket(client: httpx.AsyncClient, token: str, project_id: str, title: str) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": "plain, no marker"},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def check_duplicates(client: httpx.AsyncClient, token: str, project_id: str, query: str) -> list[dict]:
    resp = await client.post(
        f"/projects/{project_id}/tickets/check-duplicates",
        json={"title": query, "description": None},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()["matches"]


async def wait_until_embedded(org_id: str, ticket_id: str, timeout: float = 15) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, uuid.UUID(org_id))
            result = await session.execute(
                select(Ticket.embedding).where(Ticket.id == uuid.UUID(ticket_id))
            )
            embedding = result.scalar_one_or_none()
        if embedding is not None:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"ticket {ticket_id} was not embedded within {timeout}s")


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        suffix = uuid.uuid4().hex[:8]

        # --- setup: org A with two projects, org B with one -------------
        org_a = await signup(client, f"Dup Org A {suffix}", f"admin-a-{suffix}@example.dev")
        token_a = org_a["access_token"]
        org_b = await signup(client, f"Dup Org B {suffix}", f"admin-b-{suffix}@example.dev")
        token_b = org_b["access_token"]
        org_a_id = await get_org_id(client, token_a)
        org_b_id = await get_org_id(client, token_b)

        project_a1 = await create_project(client, token_a, "Dup Project A1")
        project_a2 = await create_project(client, token_a, "Dup Project A2")
        project_b = await create_project(client, token_b, "Dup Project B")
        print("PASS: org A (two projects) and org B (one project) set up")

        # --- three tickets, identical (default) embeddings, three different
        # scopes: same project, a different project in the same org, and a
        # different org entirely.
        ticket_a1 = await create_ticket(client, token_a, project_a1["id"], "Target ticket in project A1")
        ticket_a2 = await create_ticket(client, token_a, project_a2["id"], "Same-org ticket in project A2")
        ticket_b = await create_ticket(client, token_b, project_b["id"], "Cross-org ticket in project B")

        await wait_until_embedded(org_a_id, ticket_a1["id"])
        await wait_until_embedded(org_a_id, ticket_a2["id"])
        await wait_until_embedded(org_b_id, ticket_b["id"])
        print("PASS: all three tickets embedded asynchronously after creation")

        # --- point 3/7: scoping. All three tickets share the exact same
        # embedding vector (the stub's default), so if project/org scoping
        # were broken, a query in project_a1 would surface all three.
        matches = await check_duplicates(client, token_a, project_a1["id"], _NEAR_QUERY)
        matched_ids = {m["ticket_id"] for m in matches}
        assert matched_ids == {ticket_a1["id"]}, (
            f"expected only project A1's own ticket, got {matched_ids}"
        )
        assert matches[0]["similarity"] == 0.9000000000000004 or round(matches[0]["similarity"], 4) == 0.9
        print(
            "PASS: a near-duplicate query in project A1 matches only project A1's own ticket -- "
            "never the same-org ticket in project A2, never the cross-org ticket in project B, "
            "despite all three sharing an identical stored embedding"
        )

        # --- point 4/7: threshold behavior -- a genuinely different query
        # (0.0 similarity to the stored embedding) must not match at all.
        far_matches = await check_duplicates(client, token_a, project_a1["id"], _FAR_QUERY)
        assert far_matches == [], f"expected no matches for a genuinely different query, got {far_matches}"
        print("PASS: a genuinely different query (similarity 0.0) matches nothing")

        # --- point 1/7: the parse-equivalent check -- confirm check-duplicates
        # itself creates nothing (already implicit above: ticket counts were
        # never touched by any check_duplicates call), stated explicitly here.
        before = await client.get(
            f"/projects/{project_a1['id']}/tickets", headers=auth_headers(token_a)
        )
        before.raise_for_status()
        assert len(before.json()) == 1, "check-duplicates calls must never create a ticket"
        print("PASS: repeated check-duplicates calls created no tickets")

        # --- point 6/7: stale embeddings on edit. Edit ticket A1's
        # title/description to something the stub maps to _VECTOR_FAR
        # instead of the default _VECTOR_BASE, and confirm the *near*
        # query (which matched it above) no longer does, while the *far*
        # query now does -- proof the embedding was actually regenerated
        # to the new value, not merely invalidated or left stale.
        patch = await client.patch(
            f"/projects/{project_a1['id']}/tickets/{ticket_a1['id']}",
            json={"title": "stub_embed_far now describes this ticket", "description": None},
            headers=auth_headers(token_a),
        )
        patch.raise_for_status()

        # wait_until_embedded already confirmed non-NULL once; poll again
        # for the *changed* value specifically so this doesn't race the
        # re-embed job the way an unconditional sleep would.
        deadline = asyncio.get_event_loop().time() + 15
        while True:
            near_after_edit = await check_duplicates(client, token_a, project_a1["id"], _NEAR_QUERY)
            if not any(m["ticket_id"] == ticket_a1["id"] for m in near_after_edit):
                break
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("ticket A1 still matched the near query 15s after being edited")
            await asyncio.sleep(0.5)

        far_after_edit = await check_duplicates(client, token_a, project_a1["id"], _FAR_QUERY)
        assert any(m["ticket_id"] == ticket_a1["id"] for m in far_after_edit), (
            "expected the edited ticket to now match the far query, proving its embedding "
            "actually changed to the new value, not just stopped matching the old one"
        )
        print(
            "PASS: editing a ticket's title/description regenerates its embedding -- it stops "
            "matching what it used to match and starts matching what its new text actually is"
        )

    print("\nAll duplicate-detection checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
