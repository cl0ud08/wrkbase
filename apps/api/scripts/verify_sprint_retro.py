"""Proves the AI sprint summary agent end to end: complete_sprint
captures a returned-ticket snapshot and fires a real retro job; the async
worker generates a structured retro (narrative, completed highlights,
incomplete notes, risks) and stores it; the edge cases from this slice's
own spec produce sensible, non-crashing output rather than errors or
empty garbage (a sprint with nothing completed, a sprint with exactly
one ticket, a sprint with literally zero tickets ever assigned); a
retro is read from storage, not regenerated on every view, and the
manual regenerate action is properly guarded (rejected while one is
already in flight, rejected for a non-completed sprint); and tenant
isolation holds for this job type the same way it does for triage,
embedding, and AppSec review (a job claiming the wrong org_id is
rejected, not silently accepted).

Runs against the same local LLM stub as verify_appsec_triggers.py, for
the same reasoning (see verify_triage.py's module docstring for the full
real-vs-stub reasoning). The stub's sprint-retro response is
deterministic but genuinely content-aware: it reads the real
completed/returned ticket *counts* straight out of the real prompt text
sprint_retro.py's own prompt builder writes (see
scripts/_fake_llm_server.py), which is enough to prove real plumbing end
to end — real counts flow from the DB into the prompt and back out again
correctly for every edge case — without needing real language
understanding to judge whether the prose itself reads well. That's a
job for a future verify_sprint_retro_llm.py against the real API, run
manually, the same split already established for triage, parsing,
duplicate detection, and AppSec review.

Needs the fake LLM server and at least one worker process running:

    docker compose exec api python -m scripts._fake_llm_server &
    GROQ_BASE_URL=http://localhost:9100 GEMINI_BASE_URL=http://localhost:9100 \\
        docker compose exec api python -m worker.main &
    docker compose exec api python -m scripts.verify_sprint_retro
"""

import asyncio
import os
import uuid

import aio_pika
import httpx

from app.services.queue import SPRINT_RETRO_QUEUE_NAME, SprintRetroJob

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"


async def signup(client: httpx.AsyncClient, org_name: str, email: str) -> dict:
    resp = await client.post(
        "/auth/signup", json={"org_name": org_name, "email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def create_project(client: httpx.AsyncClient, token: str, name: str) -> dict:
    resp = await client.post("/projects", json={"name": name, "description": None}, headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def create_sprint(client: httpx.AsyncClient, token: str, project_id: str, name: str) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/sprints",
        json={"name": name, "goal": None, "start_date": "2026-07-01", "end_date": "2026-07-14"},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def create_ticket(
    client: httpx.AsyncClient, token: str, project_id: str, title: str, story_points: int | None = None
) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": None, "story_points": story_points},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def get_sprint(client: httpx.AsyncClient, token: str, project_id: str, sprint_id: str) -> dict:
    resp = await client.get(f"/projects/{project_id}/sprints/{sprint_id}", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def terminal_workflow_state(client: httpx.AsyncClient, token: str, project_id: str) -> dict:
    resp = await client.get(f"/projects/{project_id}/workflow-states", headers=auth_headers(token))
    resp.raise_for_status()
    return max(resp.json(), key=lambda s: s["order"])


async def wait_until_retro_ready(
    client: httpx.AsyncClient, token: str, project_id: str, sprint_id: str, timeout: float = 15
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        sprint = await get_sprint(client, token, project_id, sprint_id)
        if sprint["retro_status"] not in (None, "pending"):
            return sprint
        await asyncio.sleep(0.5)
    raise AssertionError(f"sprint {sprint_id} retro was not ready within {timeout}s")


async def run_sprint_to_completion(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    *,
    name: str,
    completed_titles: list[str],
    returned_titles: list[str],
) -> dict:
    """Creates a sprint, assigns the given completed/returned tickets to
    it (moving the completed ones into the project's terminal workflow
    column first), starts it, completes it, and waits for the retro.
    Returns the completed sprint (with retro fields populated).
    """
    terminal = await terminal_workflow_state(client, token, project_id)
    sprint = await create_sprint(client, token, project_id, name)

    ticket_ids = []
    for title in completed_titles:
        t = await create_ticket(client, token, project_id, title, story_points=3)
        move = await client.patch(
            f"/projects/{project_id}/tickets/{t['id']}",
            json={"workflow_state_id": terminal["id"]},
            headers=auth_headers(token),
        )
        move.raise_for_status()
        ticket_ids.append(t["id"])
    for title in returned_titles:
        t = await create_ticket(client, token, project_id, title, story_points=2)
        ticket_ids.append(t["id"])

    if ticket_ids:
        assign = await client.post(
            f"/projects/{project_id}/sprints/{sprint['id']}/assign",
            json={"ticket_ids": ticket_ids},
            headers=auth_headers(token),
        )
        assign.raise_for_status()

    start = await client.post(f"/projects/{project_id}/sprints/{sprint['id']}/start", headers=auth_headers(token))
    start.raise_for_status()

    complete = await client.post(
        f"/projects/{project_id}/sprints/{sprint['id']}/complete", headers=auth_headers(token)
    )
    complete.raise_for_status()
    assert complete.json()["retro_status"] == "pending", "retro_status should flip to pending the instant the sprint completes"

    return await wait_until_retro_ready(client, token, project_id, sprint["id"])


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    rabbitmq_url = os.environ["RABBITMQ_URL"]

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        org_a = await signup(client, f"Retro Org A {suffix}", f"retro-a-{suffix}@example.dev")
        token_a = org_a["access_token"]
        org_b = await signup(client, f"Retro Org B {suffix}", f"retro-b-{suffix}@example.dev")
        token_b = org_b["access_token"]
        org_b_id = (await client.get("/auth/me", headers=auth_headers(token_b))).json()["org_id"]

        project_a = await create_project(client, token_a, "Retro Project A")
        print("PASS: two independent orgs set up, each with its own project")

        # --- mixed sprint: some finished, some returned -----------------------
        mixed = await run_sprint_to_completion(
            client, token_a, project_a["id"],
            name="Mixed Sprint",
            completed_titles=["Ship the login page"],
            returned_titles=["Ship the billing page", "Ship the export feature"],
        )
        assert mixed["retro_status"] == "completed"
        assert mixed["retro_narrative"], "expected a non-empty narrative"
        assert mixed["retro_completed_highlights"], "expected at least one completed highlight"
        assert mixed["retro_incomplete_notes"], "expected at least one incomplete note (2 tickets were returned)"
        assert mixed["retro_risks"], "expected a risk flag (this sprint had returned tickets)"
        assert mixed["retro_generated_at"] is not None
        assert mixed["retro_error"] is None
        assert mixed["total_points"] == 3, f"expected total_points 3 (the one finished ticket), got {mixed['total_points']}"
        assert mixed["points_planned"] == 7, f"expected points_planned 7 (3 done + 2+2 returned), got {mixed['points_planned']}"
        print("PASS: a mixed sprint (some finished, some returned) generates a real retro with correct points math")

        # --- edge case: zero completed / everything auto-returned -------------
        all_returned = await run_sprint_to_completion(
            client, token_a, project_a["id"],
            name="All-Returned Sprint",
            completed_titles=[],
            returned_titles=["Nothing finished here", "Nor here"],
        )
        assert all_returned["retro_status"] == "completed"
        assert all_returned["retro_narrative"], "expected a non-empty narrative even with nothing completed"
        assert all_returned["retro_completed_highlights"] == [], "expected an empty (not garbage) completed_highlights list"
        assert all_returned["retro_incomplete_notes"], "expected incomplete_notes to be non-empty"
        assert all_returned["retro_risks"], "expected a risk flag for a sprint where nothing finished"
        assert all_returned["total_points"] == 0
        print("PASS: a sprint where everything got auto-returned (zero completed) produces sensible output, not empty garbage")

        # --- edge case: exactly one ticket, and it finished --------------------
        single = await run_sprint_to_completion(
            client, token_a, project_a["id"],
            name="Single-Ticket Sprint",
            completed_titles=["The only ticket in this sprint"],
            returned_titles=[],
        )
        assert single["retro_status"] == "completed"
        assert single["retro_narrative"]
        assert single["retro_completed_highlights"] == [
            "Stub highlight covering 1 completed ticket(s)."
        ]
        assert single["retro_incomplete_notes"] == [], "nothing was returned, so this should be empty"
        assert single["retro_risks"] == [], "nothing returned and one ticket finished -- no risk to flag"
        print("PASS: a single-ticket sprint produces a real retro with no spurious risk flags")

        # --- edge case: a completed sprint with literally zero tickets ever ----
        empty = await run_sprint_to_completion(
            client, token_a, project_a["id"], name="Empty Sprint", completed_titles=[], returned_titles=[]
        )
        assert empty["retro_status"] == "completed", "an empty sprint must not crash retro generation"
        assert empty["retro_narrative"]
        assert empty["retro_completed_highlights"] == []
        assert empty["retro_incomplete_notes"] == []
        assert empty["retro_risks"] == []
        assert empty["total_points"] == 0 and empty["points_planned"] == 0
        print("PASS: a sprint with zero tickets ever assigned completes cleanly with well-formed empty output")

        # --- stored, not regenerated on every view ------------------------------
        reread_1 = await get_sprint(client, token_a, project_a["id"], mixed["id"])
        reread_2 = await get_sprint(client, token_a, project_a["id"], mixed["id"])
        assert reread_1["retro_generated_at"] == reread_2["retro_generated_at"] == mixed["retro_generated_at"]
        assert reread_1["retro_narrative"] == reread_2["retro_narrative"] == mixed["retro_narrative"]
        print("PASS: GETting a sprint repeatedly returns the same stored retro, not a fresh generation each time")

        # --- regenerate: guarded against a non-completed sprint -----------------
        regen_on_active = await create_sprint(client, token_a, project_a["id"], "Not Completed Yet")
        regen_reject = await client.post(
            f"/projects/{project_a['id']}/sprints/{regen_on_active['id']}/retro/regenerate",
            headers=auth_headers(token_a),
        )
        assert regen_reject.status_code == 400, f"expected 400, got {regen_reject.status_code}"
        print("PASS: regenerating a retro for a sprint that hasn't completed yet is rejected (400)")

        # --- regenerate: real, and guarded against overlapping in-flight calls --
        regen = await client.post(
            f"/projects/{project_a['id']}/sprints/{mixed['id']}/retro/regenerate", headers=auth_headers(token_a)
        )
        assert regen.status_code == 202, f"expected 202, got {regen.status_code}"
        assert regen.json()["retro_status"] == "pending"

        regen_again = await client.post(
            f"/projects/{project_a['id']}/sprints/{mixed['id']}/retro/regenerate", headers=auth_headers(token_a)
        )
        assert regen_again.status_code == 409, f"expected 409 while a retro is already generating, got {regen_again.status_code}"
        print("PASS: a second regenerate call while one is already in flight is rejected (409)")

        regenerated = await wait_until_retro_ready(client, token_a, project_a["id"], mixed["id"])
        assert regenerated["retro_status"] == "completed"
        assert regenerated["retro_generated_at"] != mixed["retro_generated_at"], (
            "expected a new retro_generated_at timestamp proving this actually re-ran, not just re-served the old row"
        )
        print("PASS: regenerate actually re-runs generation (a new retro_generated_at), and is usable again once it completes")

        # --- defense in depth: a job with a mismatched org_id is rejected -------
        before = await get_sprint(client, token_a, project_a["id"], single["id"])
        connection = await aio_pika.connect_robust(rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            poison = SprintRetroJob(sprint_id=uuid.UUID(single["id"]), org_id=uuid.UUID(org_b_id))
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=poison.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=SPRINT_RETRO_QUEUE_NAME,
            )
        await asyncio.sleep(3)  # let the real worker pick this up and reject it
        after = await get_sprint(client, token_a, project_a["id"], single["id"])
        assert after["retro_status"] == before["retro_status"]
        assert after["retro_narrative"] == before["retro_narrative"]
        assert after["retro_generated_at"] == before["retro_generated_at"]
        print(
            "PASS: a job claiming org A's sprint under org B's context is rejected "
            "(RLS-scoped lookup finds nothing) and leaves the real row untouched"
        )

    print("\nAll sprint retro checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
