"""Proves the natural-language ticket-parsing endpoint end to end:
POST .../tickets/parse never creates anything by itself, a genuinely
ambiguous input gets an honest "I can't tell" response instead of a
fabricated one, tenant isolation holds for the new endpoint (not assumed
just because it reuses the same project-lookup helper create_ticket
does), and the actual parse-then-confirm flow a real user follows --
review the parsed candidate, then create through the normal ticket-
creation endpoint -- still triggers async triage exactly like any other
ticket creation.

Runs against the same local LLM stub as verify_triage.py, for the same
reasons (see that file's own module docstring for the full real-vs-stub
reasoning): free, deterministic, exercises every line of this app's own
code, and doesn't pay for or depend on the real Groq/Gemini APIs on
every push. The stub's low-confidence response is triggered by a plainly
-named sentinel ("stub_trigger_low_confidence") in this script's own
deliberately-ambiguous test input -- see scripts/_fake_llm_server.py's
_fake_result_for for the other side of that contract.

Needs the fake LLM server and at least one worker process running,
exactly like verify_triage.py:

    docker compose exec api python -m scripts._fake_llm_server &
    GROQ_BASE_URL=http://localhost:9100 GEMINI_BASE_URL=http://localhost:9100 \\
        docker compose exec api python -m worker.main &
    docker compose exec api python -m scripts.verify_ticket_parse
"""

import asyncio
import uuid

import httpx

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"

_LOW_CONFIDENCE_INPUT = "stub_trigger_low_confidence asdf whatever"
_CONFIDENT_INPUT = "create a bug for login failing on Safari, high priority"


async def signup(client: httpx.AsyncClient, org_name: str, email: str) -> dict:
    resp = await client.post(
        "/auth/signup", json={"org_name": org_name, "email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def create_project(client: httpx.AsyncClient, token: str, name: str) -> dict:
    resp = await client.post(
        "/projects", json={"name": name, "description": None}, headers=auth_headers(token)
    )
    resp.raise_for_status()
    return resp.json()


async def parse_text(client: httpx.AsyncClient, token: str, project_id: str, text: str) -> httpx.Response:
    return await client.post(
        f"/projects/{project_id}/tickets/parse", json={"text": text}, headers=auth_headers(token)
    )


async def list_tickets(client: httpx.AsyncClient, token: str, project_id: str) -> list[dict]:
    resp = await client.get(f"/projects/{project_id}/tickets", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def create_ticket(
    client: httpx.AsyncClient, token: str, project_id: str, *, type: str, title: str, description: str | None
) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": type, "title": title, "description": description},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def get_ticket(client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str) -> dict:
    resp = await client.get(f"/projects/{project_id}/tickets/{ticket_id}", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def wait_until_triaged(
    client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str, timeout: float = 15
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = await get_ticket(client, token, project_id, ticket_id)
        if t["triage_status"] != "pending":
            return t
        await asyncio.sleep(0.5)
    raise AssertionError(f"ticket {ticket_id} was not triaged within {timeout}s")


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        suffix = uuid.uuid4().hex[:8]

        # --- setup: two independent orgs, one project for org A --------------
        org_a = await signup(client, f"Parse Org A {suffix}", f"admin-a-{suffix}@example.dev")
        token_a = org_a["access_token"]
        org_b = await signup(client, f"Parse Org B {suffix}", f"admin-b-{suffix}@example.dev")
        token_b = org_b["access_token"]

        project_a = await create_project(client, token_a, "Parse Project A")
        print("PASS: two independent orgs set up")

        # --- tenant isolation: org B can't parse against org A's project -----
        cross_org = await parse_text(client, token_b, project_a["id"], _CONFIDENT_INPUT)
        assert cross_org.status_code == 404, f"expected 404, got {cross_org.status_code}"
        print("PASS: org B directly POSTing .../tickets/parse under org A's project id -> 404")

        # --- the confident case: a clear title/type is actually extracted ----
        before_count = len(await list_tickets(client, token_a, project_a["id"]))
        confident_resp = await parse_text(client, token_a, project_a["id"], _CONFIDENT_INPUT)
        assert confident_resp.status_code == 200, confident_resp.text
        candidate = confident_resp.json()
        assert candidate["confident"] is True, candidate
        assert candidate["title"], "expected a non-empty title for a clear, confident input"
        assert candidate["type"] in ("epic", "story", "task"), candidate["type"]
        assert candidate["type"] != "subtask", "parse must never suggest subtask -- no parent is choosable"
        print(f"PASS: a clear input is parsed confidently: title={candidate['title']!r} type={candidate['type']!r}")

        # --- point 1: parse alone creates nothing, confident or not ----------
        after_count = len(await list_tickets(client, token_a, project_a["id"]))
        assert after_count == before_count, (
            f"expected no ticket created by parse alone, had {before_count} before, {after_count} after"
        )
        print("PASS: a confident parse with no follow-up confirm leaves no ticket behind")

        # --- point 4: an ambiguous input is handled honestly, not fabricated -
        before_count_2 = len(await list_tickets(client, token_a, project_a["id"]))
        low_conf_resp = await parse_text(client, token_a, project_a["id"], _LOW_CONFIDENCE_INPUT)
        assert low_conf_resp.status_code == 200, low_conf_resp.text
        low_conf = low_conf_resp.json()
        assert low_conf["confident"] is False, low_conf
        assert low_conf["title"] is None, "a low-confidence response must not fabricate a title"
        assert low_conf["type"] is None, "a low-confidence response must not fabricate a type"
        assert low_conf["clarification"], "confident=false must explain why, not fail silently"
        print(f"PASS: an ambiguous input is honestly reported as low-confidence: {low_conf['clarification']!r}")

        after_count_2 = len(await list_tickets(client, token_a, project_a["id"]))
        assert after_count_2 == before_count_2, "a low-confidence parse must not create a ticket either"
        print("PASS: a low-confidence parse with no follow-up confirm also leaves no ticket behind")

        # --- point 3: parse-then-confirm composes correctly with async triage
        # A real client reviews `candidate` and may edit it; simulate that by
        # submitting through the exact same create-ticket endpoint every other
        # ticket goes through -- no "already parsed" flag or second creation
        # path, see app/api/tickets.py's parse_ticket docstring for why.
        created = await create_ticket(
            client,
            token_a,
            project_a["id"],
            type=candidate["type"],
            title=candidate["title"],
            description=candidate["description"],
        )
        assert created["triage_status"] == "pending", (
            "a ticket created from a parsed candidate should still start pending_triage, "
            "exactly like any other ticket -- the parse step's own priority/labels are a "
            "preview only, never written directly to the created ticket"
        )
        triaged = await wait_until_triaged(client, token_a, project_a["id"], created["id"])
        assert triaged["triage_status"] == "triaged", triaged
        assert triaged["priority"] is not None
        print(
            "PASS: confirming a parsed candidate creates a real ticket through the normal "
            "creation path, which still triggers async triage afterward exactly as usual"
        )

    print("\nAll ticket-parse checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
