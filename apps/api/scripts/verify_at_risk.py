"""Proves at-risk ticket detection end to end: the pure rule-based
scoring function (app/services/at_risk.py's assess_ticket_risk) flags a
deliberately at-risk ticket and correctly clears a healthy one, the
2-of-3 threshold genuinely requires two signals rather than firing on
any one alone, a ticket in the terminal column is never at risk
regardless of every other factor, a ticket outside the active sprint
gets no assessment at all (None, not False), the sprint-level
at_risk_count rollup matches, and tenant isolation holds for two
independent orgs each running their own active sprint at the same time
(this feature's own new queries -- find_active_sprint,
workflow_state_bounds -- are scoped correctly, not just inheriting RLS
by accident).

No LLM, no queue, no worker involved anywhere in this feature (see
app/services/at_risk.py's own module docstring for why) -- so unlike
verify_appsec_triggers.py or verify_sprint_retro.py, this script needs
no fake LLM stub server and no extra worker process running. Just the
API server itself.

    docker compose exec api python -m scripts.verify_at_risk
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx

from app.db.models import Ticket
from app.services.at_risk import assess_ticket_risk

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"


def _ticket(*, assignee_id: uuid.UUID | None, workflow_state_id: uuid.UUID, updated_at: datetime) -> Ticket:
    # A bare, unpersisted ORM instance -- assess_ticket_risk is pure and
    # only reads these three attributes, no DB/session involved.
    t = Ticket()
    t.assignee_id = assignee_id
    t.workflow_state_id = workflow_state_id
    t.updated_at = updated_at
    return t


def verify_scoring_accuracy() -> None:
    """Pure, synchronous, no HTTP, no DB -- assess_ticket_risk has no I/O
    at all.
    """
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    today = now.date()
    earliest, middle, terminal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    low_runway_end = today + timedelta(days=1)
    plenty_of_runway_end = today + timedelta(days=10)

    # --- a deliberately at-risk ticket: all 3 signals fire -----------------
    at_risk = assess_ticket_risk(
        _ticket(assignee_id=None, workflow_state_id=earliest, updated_at=now - timedelta(days=5)),
        sprint_end_date=low_runway_end, today=today, now=now,
        earliest_state_id=earliest, terminal_state_id=terminal,
    )
    assert at_risk.at_risk is True
    assert len(at_risk.reasons) == 3, f"expected 3 reasons, got {at_risk.reasons}"
    print("PASS: a deliberately at-risk ticket (unowned, stale, low runway) is flagged with all 3 reasons")

    # --- a healthy ticket: assigned, in progress, fresh, plenty of runway --
    healthy = assess_ticket_risk(
        _ticket(assignee_id=uuid.uuid4(), workflow_state_id=middle, updated_at=now),
        sprint_end_date=plenty_of_runway_end, today=today, now=now,
        earliest_state_id=earliest, terminal_state_id=terminal,
    )
    assert healthy.at_risk is False
    assert healthy.reasons == []
    print("PASS: a healthy ticket (assigned, in progress, fresh, plenty of runway) is not flagged")

    # --- terminal column: never at risk, regardless of every other factor --
    done = assess_ticket_risk(
        _ticket(assignee_id=None, workflow_state_id=terminal, updated_at=now - timedelta(days=10)),
        sprint_end_date=low_runway_end, today=today, now=now,
        earliest_state_id=earliest, terminal_state_id=terminal,
    )
    assert done.at_risk is False
    assert done.reasons == []
    print("PASS: a ticket in the terminal column is never at risk, even if unowned, stale, and runway is low")

    # --- exactly one signal: below the 2-of-3 threshold ---------------------
    one_signal = assess_ticket_risk(
        _ticket(assignee_id=uuid.uuid4(), workflow_state_id=middle, updated_at=now),
        sprint_end_date=low_runway_end, today=today, now=now,
        earliest_state_id=earliest, terminal_state_id=terminal,
    )
    assert one_signal.at_risk is False, "a single fired signal (low runway alone) must not cross the threshold"
    assert one_signal.reasons == ["Only 1 day left in the sprint and not yet done"]
    print("PASS: exactly one fired signal (low runway alone) stays below the 2-of-3 threshold")

    # --- exactly two signals: crosses the threshold --------------------------
    two_signals = assess_ticket_risk(
        _ticket(assignee_id=None, workflow_state_id=earliest, updated_at=now),
        sprint_end_date=low_runway_end, today=today, now=now,
        earliest_state_id=earliest, terminal_state_id=terminal,
    )
    assert two_signals.at_risk is True
    assert len(two_signals.reasons) == 2
    print("PASS: exactly two fired signals (unowned+not-started, low runway) crosses the threshold")

    # --- an overdue sprint (still ACTIVE, past end_date) --------------------
    overdue = assess_ticket_risk(
        _ticket(assignee_id=None, workflow_state_id=earliest, updated_at=now),
        sprint_end_date=today - timedelta(days=2), today=today, now=now,
        earliest_state_id=earliest, terminal_state_id=terminal,
    )
    assert overdue.at_risk is True
    assert any("overdue" in reason for reason in overdue.reasons), overdue.reasons
    print("PASS: a sprint past its end_date while still active triggers low-runway with an overdue-specific reason")


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


async def workflow_bounds(client: httpx.AsyncClient, token: str, project_id: str) -> tuple[str, str]:
    resp = await client.get(f"/projects/{project_id}/workflow-states", headers=auth_headers(token))
    resp.raise_for_status()
    states = resp.json()
    ordered = sorted(states, key=lambda s: s["order"])
    return ordered[0]["id"], ordered[-1]["id"]


async def create_sprint(
    client: httpx.AsyncClient, token: str, project_id: str, name: str, *, end_date: date
) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/sprints",
        json={"name": name, "goal": None, "start_date": "2026-07-01", "end_date": end_date.isoformat()},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def create_ticket(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    title: str,
    *,
    workflow_state_id: str | None = None,
    assignee_id: str | None = None,
) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={
            "type": "task",
            "title": title,
            "description": None,
            "workflow_state_id": workflow_state_id,
            "assignee_id": assignee_id,
        },
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


async def assign_to_sprint(client: httpx.AsyncClient, token: str, project_id: str, sprint_id: str, ticket_ids: list[str]) -> None:
    resp = await client.post(
        f"/projects/{project_id}/sprints/{sprint_id}/assign",
        json={"ticket_ids": ticket_ids},
        headers=auth_headers(token),
    )
    resp.raise_for_status()


async def build_scenario(client: httpx.AsyncClient, token: str, org_suffix: str) -> dict:
    """One org's full at-risk scenario: an active sprint ending very soon
    (forces low_runway for anything not done), and four tickets covering
    every case this slice's own spec named -- an at-risk ticket, a
    healthy one, a terminal one, and a backlog one not in the sprint at
    all. Returns everything a caller needs to assert against.
    """
    project = await create_project(client, token, f"At-Risk Project {org_suffix}")
    project_id = project["id"]
    earliest_id, terminal_id = await workflow_bounds(client, token, project_id)

    sprint = await create_sprint(
        client, token, project_id, f"Crunch Sprint {org_suffix}", end_date=date.today() + timedelta(days=1)
    )
    start_resp = await client.post(
        f"/projects/{project_id}/sprints/{sprint['id']}/start", headers=auth_headers(token)
    )
    start_resp.raise_for_status()

    at_risk_ticket = await create_ticket(
        client, token, project_id, f"Unowned, stuck in {earliest_id}", workflow_state_id=earliest_id
    )
    own_user_id = at_risk_ticket["created_by"]
    healthy_ticket = await create_ticket(
        client, token, project_id, "Owned and moving", workflow_state_id=earliest_id, assignee_id=own_user_id
    )
    terminal_ticket = await create_ticket(
        client, token, project_id, "Already done", workflow_state_id=terminal_id
    )
    backlog_ticket = await create_ticket(client, token, project_id, "Not even in this sprint")

    await assign_to_sprint(
        client, token, project_id, sprint["id"],
        [at_risk_ticket["id"], healthy_ticket["id"], terminal_ticket["id"]],
    )

    return {
        "project_id": project_id,
        "sprint_id": sprint["id"],
        "at_risk_ticket_id": at_risk_ticket["id"],
        "healthy_ticket_id": healthy_ticket["id"],
        "terminal_ticket_id": terminal_ticket["id"],
        "backlog_ticket_id": backlog_ticket["id"],
    }


async def get_tickets_by_id(client: httpx.AsyncClient, token: str, project_id: str) -> dict:
    resp = await client.get(f"/projects/{project_id}/tickets", headers=auth_headers(token))
    resp.raise_for_status()
    return {t["id"]: t for t in resp.json()}


async def main() -> None:
    verify_scoring_accuracy()

    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        org_a = await signup(client, f"At-Risk Org A {suffix}", f"risk-a-{suffix}@example.dev")
        token_a = org_a["access_token"]
        org_b = await signup(client, f"At-Risk Org B {suffix}", f"risk-b-{suffix}@example.dev")
        token_b = org_b["access_token"]
        print("PASS: two independent orgs set up, each about to run its own active sprint concurrently")

        scenario_a = await build_scenario(client, token_a, "A")
        scenario_b = await build_scenario(client, token_b, "B")

        # --- org A's own scenario scores correctly -------------------------
        tickets_a = await get_tickets_by_id(client, token_a, scenario_a["project_id"])
        at_risk_a = tickets_a[scenario_a["at_risk_ticket_id"]]
        assert at_risk_a["at_risk"] is True, at_risk_a
        assert len(at_risk_a["at_risk_reasons"]) == 2, at_risk_a["at_risk_reasons"]
        print("PASS: the deliberately at-risk ticket is flagged over real HTTP, with exactly 2 reasons")

        healthy_a = tickets_a[scenario_a["healthy_ticket_id"]]
        assert healthy_a["at_risk"] is False, healthy_a
        # Still gets exactly one reason (low runway -- the sprint really
        # does end in a day and this ticket really isn't done) -- being
        # owned only clears the *other* signal, and one alone correctly
        # stays under the 2-of-3 threshold. Not zero reasons; not flagged.
        assert healthy_a["at_risk_reasons"] == ["Only 1 day left in the sprint and not yet done"], (
            healthy_a["at_risk_reasons"]
        )
        print(
            "PASS: the owned ticket in the same low-runway sprint has one real reason on record "
            "but stays under threshold, correctly NOT flagged"
        )

        terminal_a = tickets_a[scenario_a["terminal_ticket_id"]]
        assert terminal_a["at_risk"] is False
        assert terminal_a["at_risk_reasons"] == []
        print("PASS: the already-done ticket is never flagged, even unowned in a low-runway sprint")

        backlog_a = tickets_a[scenario_a["backlog_ticket_id"]]
        assert backlog_a["at_risk"] is None, backlog_a
        assert backlog_a["at_risk_reasons"] is None
        print("PASS: a ticket not in the active sprint gets no assessment at all (None, not False)")

        # --- sprint-level rollup matches -------------------------------------
        sprint_a = (
            await client.get(f"/projects/{scenario_a['project_id']}/sprints/{scenario_a['sprint_id']}", headers=auth_headers(token_a))
        ).json()
        assert sprint_a["at_risk_count"] == 1, sprint_a["at_risk_count"]
        print("PASS: the sprint-level at_risk_count rollup matches (exactly the one at-risk ticket)")

        # --- org B's own, independent scenario also scores correctly --------
        # This is the part that actually exercises this slice's own new
        # queries (find_active_sprint, workflow_state_bounds) under two
        # orgs with an active sprint at the same time -- if either query
        # were missing its project_id/org scoping, org B's active sprint
        # or workflow bounds could leak into org A's computation (or vice
        # versa), which a single-org test could never catch.
        tickets_b = await get_tickets_by_id(client, token_b, scenario_b["project_id"])
        at_risk_b = tickets_b[scenario_b["at_risk_ticket_id"]]
        assert at_risk_b["at_risk"] is True
        print("PASS: org B's own at-risk ticket scores correctly while org A's active sprint exists concurrently")

        # --- direct cross-org access is still rejected (defense in depth) ---
        cross_org = await client.get(
            f"/projects/{scenario_b['project_id']}/tickets", headers=auth_headers(token_a)
        )
        assert cross_org.status_code == 404, f"expected 404, got {cross_org.status_code}"
        print("PASS: org A directly GETting org B's project's tickets (guessed real id) -> 404")

    print("\nAll at-risk detection checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
