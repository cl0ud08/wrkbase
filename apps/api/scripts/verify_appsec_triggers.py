"""Proves the AI Security Champion end to end: the keyword gate
(app/services/appsec_triggers.match_triggers) actually fires on
realistic matching text and does not fire on clearly unrelated text
-- across every trigger category, not just one, a real false-positive/
false-negative check rather than "the mechanism runs at all"; a ticket
that matches is flagged synchronously, before any worker involvement;
the async worker completes the review and stores a comment; an edit
that introduces a new category triggers a fresh review and merges
categories additively rather than replacing them; tenant isolation
holds for this job type the same way it does for triage and embedding
(a job claiming the wrong org_id is rejected, not silently accepted).

Runs against the same local LLM stub as verify_triage.py, for the same
reasoning (see that file's own module docstring for the full real-vs-
stub reasoning). The stub's AppSec review response is deterministic
("Stub deterministic security review covering: <categories>") --
enough to prove the plumbing (job publish, worker consume, response
validated and stored, categories_addressed round-tripped correctly),
but NOT enough to judge whether a comment is genuinely tailored to a
specific ticket's own wording, since the stub has no real language
understanding to approximate cheaply. That's
scripts/verify_appsec_review_llm.py's job, against the real API, run
manually -- the same split already established for triage (verify_
triage.py vs. verify_triage_llm.py) and ticket parsing.

Needs the fake LLM server and at least one worker process running:

    docker compose exec api python -m scripts._fake_llm_server &
    GROQ_BASE_URL=http://localhost:9100 GEMINI_BASE_URL=http://localhost:9100 \\
        docker compose exec api python -m worker.main &
    docker compose exec api python -m scripts.verify_appsec_triggers
"""

import asyncio
import os
import uuid

import aio_pika
import httpx

from app.services.appsec_triggers import TRIGGER_CATEGORIES, match_triggers
from app.services.queue import APPSEC_QUEUE_NAME, AppSecJob

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"


def verify_trigger_accuracy() -> None:
    """Pure, synchronous, no HTTP -- match_triggers has no I/O at all.
    Covers every category with a realistic matching ticket and a
    realistic non-matching one, plus one deliberately trivial ticket
    that must match nothing at all.
    """
    positive_cases = {
        "file_upload": ("Add profile picture upload", "Users upload a JPG avatar for their profile."),
        "auth_permission": ("Add OAuth login with Google", "Support signing in via Google OAuth."),
        "payment_pii": ("Integrate Stripe for subscription billing", "Charge users monthly via Stripe."),
        "external_api": ("Integrate with Slack webhook", "Post build notifications to a Slack webhook."),
        "admin_permission": ("Allow admins to grant admin role", "Add UI to promote a member to admin."),
    }
    negative_cases = {
        "file_upload": ("Update button color on dashboard", "Change the primary button from blue to green."),
        "auth_permission": ("Fix typo in footer copyright text", None),
        "payment_pii": ("Reorder columns in the backlog table", "Move story points next to the title."),
        "external_api": ("Adjust column widths on the board", None),
        "admin_permission": ("Add dark mode toggle to settings", "Users want a dark theme option."),
    }
    assert set(positive_cases) == {c.key for c in TRIGGER_CATEGORIES}
    assert set(negative_cases) == {c.key for c in TRIGGER_CATEGORIES}

    for key, (title, desc) in positive_cases.items():
        matched = {c.key for c in match_triggers(title, desc)}
        assert key in matched, f"expected {key!r} to match {title!r}, got {matched}"
    print("PASS: every trigger category fires on a realistic matching ticket")

    for key, (title, desc) in negative_cases.items():
        matched = {c.key for c in match_triggers(title, desc)}
        assert key not in matched, f"expected {key!r} NOT to match {title!r}, got {matched}"
    print("PASS: every trigger category stays silent on a realistic unrelated ticket")

    assert match_triggers("Fix typo in footer", None) == []
    print("PASS: a trivial ticket matches zero categories")


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


async def create_ticket(
    client: httpx.AsyncClient, token: str, project_id: str, title: str, description: str | None = None
) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": description},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def get_ticket(client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str) -> dict:
    resp = await client.get(f"/projects/{project_id}/tickets/{ticket_id}", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()


async def patch_ticket(
    client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str, **fields: object
) -> dict:
    resp = await client.patch(
        f"/projects/{project_id}/tickets/{ticket_id}", json=fields, headers=auth_headers(token)
    )
    resp.raise_for_status()
    return resp.json()


async def wait_until_reviewed(
    client: httpx.AsyncClient, token: str, project_id: str, ticket_id: str, timeout: float = 15
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = await get_ticket(client, token, project_id, ticket_id)
        if t["appsec_review_status"] not in (None, "pending"):
            return t
        await asyncio.sleep(0.5)
    raise AssertionError(f"ticket {ticket_id} was not appsec-reviewed within {timeout}s")


async def main() -> None:
    verify_trigger_accuracy()

    suffix = uuid.uuid4().hex[:8]
    rabbitmq_url = os.environ["RABBITMQ_URL"]

    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        org_a = await signup(client, f"AppSec Org A {suffix}", f"admin-a-{suffix}@example.dev")
        token_a = org_a["access_token"]
        org_b = await signup(client, f"AppSec Org B {suffix}", f"admin-b-{suffix}@example.dev")
        token_b = org_b["access_token"]
        org_b_id = (await client.get("/auth/me", headers=auth_headers(token_b))).json()["org_id"]

        project_a = await create_project(client, token_a, "AppSec Project A")
        print("PASS: two independent orgs set up, each with its own project")

        # --- synchronous flag, before any worker involvement -----------------
        flagged = await create_ticket(
            client, token_a, project_a["id"],
            "Add profile picture upload",
            "Users upload a JPG avatar shown on their profile page.",
        )
        assert flagged["appsec_review_status"] == "pending"
        assert flagged["appsec_categories"] == ["file_upload"]
        assert flagged["appsec_comment"] is None
        print("PASS: a ticket matching a trigger category is flagged synchronously, before the worker runs")

        # --- an unrelated ticket is never flagged at all ----------------------
        unrelated = await create_ticket(client, token_a, project_a["id"], "Fix typo in footer", None)
        assert unrelated["appsec_review_status"] is None
        assert unrelated["appsec_categories"] is None
        print("PASS: an unrelated ticket's appsec_review_status stays NULL -- no job ever published")

        # --- the async worker completes the review and stores a real comment -
        reviewed = await wait_until_reviewed(client, token_a, project_a["id"], flagged["id"])
        assert reviewed["appsec_review_status"] == "completed"
        assert reviewed["appsec_categories"] == ["file_upload"]
        assert reviewed["appsec_comment"], "expected a non-empty AI-generated comment"
        assert reviewed["appsec_review_error"] is None
        assert reviewed["appsec_reviewed_at"] is not None
        print("PASS: the worker completes the review and stores a real comment, categories, and timestamp")

        # --- editing a ticket into security-relevance triggers a fresh review
        edited = await patch_ticket(
            client, token_a, project_a["id"], unrelated["id"],
            title="Allow admins to grant admin role to other members",
            description="Add a UI control and endpoint for promoting a member to admin.",
        )
        assert edited["appsec_review_status"] == "pending"
        assert edited["appsec_categories"] == ["admin_permission"]
        edited_reviewed = await wait_until_reviewed(client, token_a, project_a["id"], unrelated["id"])
        assert edited_reviewed["appsec_review_status"] == "completed"
        assert edited_reviewed["appsec_categories"] == ["admin_permission"]
        print("PASS: editing a ticket's title/description into security-relevance triggers a fresh review")

        # --- a second, different category matching later is additive, not a
        # replacement -- both categories end up recorded, not just the new one.
        merged = await patch_ticket(
            client, token_a, project_a["id"], unrelated["id"],
            description="Add a UI control and endpoint for promoting a member to admin, "
            "with an uploaded justification document attached.",
        )
        assert merged["appsec_review_status"] == "pending"
        assert set(merged["appsec_categories"]) == {"admin_permission", "file_upload"}
        print("PASS: a second edit that matches a new category merges it in, keeping the first")

        # --- defense in depth: a job with a mismatched org_id is rejected ----
        # Same poison-message shape as verify_triage.py's own check: a job
        # claiming org A's real ticket_id but org B's org_id. The RLS-scoped
        # lookup (set_tenant_context to the job's claimed org_id, then query)
        # finds nothing under org B's context for org A's ticket, rejects it
        # as a poison message, and leaves the real row untouched.
        before = await get_ticket(client, token_a, project_a["id"], flagged["id"])
        connection = await aio_pika.connect_robust(rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            poison = AppSecJob(ticket_id=uuid.UUID(flagged["id"]), org_id=uuid.UUID(org_b_id))
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=poison.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=APPSEC_QUEUE_NAME,
            )
        await asyncio.sleep(3)  # let the real worker pick this up and reject it
        after = await get_ticket(client, token_a, project_a["id"], flagged["id"])
        assert after["appsec_review_status"] == before["appsec_review_status"]
        assert after["appsec_comment"] == before["appsec_comment"]
        print(
            "PASS: a job claiming org A's ticket under org B's context is rejected "
            "(RLS-scoped lookup finds nothing) and leaves the real row untouched"
        )

    print("\nAll AppSec trigger checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
