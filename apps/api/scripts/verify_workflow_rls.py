"""Proves workflow states and ticket moves are tenant-isolated end to end,
same rigor as verify_projects_rls.py / verify_tickets_rls.py: org A cannot
GET/PATCH/DELETE org B's workflow state, and cannot move org B's ticket,
via direct ID access. Run:

    docker compose exec api python -m scripts.verify_workflow_rls
"""

import asyncio
import uuid

import httpx

BASE_URL = "http://localhost:8000"
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


async def list_states(client: httpx.AsyncClient, access_token: str, project_id: str) -> list[dict]:
    resp = await client.get(
        f"/projects/{project_id}/workflow-states", headers=auth_headers(access_token)
    )
    resp.raise_for_status()
    return resp.json()


async def create_ticket(client: httpx.AsyncClient, access_token: str, project_id: str, title: str) -> dict:
    resp = await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": None},
        headers=auth_headers(access_token),
    )
    resp.raise_for_status()
    return resp.json()


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        tokens_a = await signup(client, f"Org A {suffix}", f"wf-a-{suffix}@example.dev")
        tokens_b = await signup(client, f"Org B {suffix}", f"wf-b-{suffix}@example.dev")
        token_a, token_b = tokens_a["access_token"], tokens_b["access_token"]

        project_a = await create_project(client, token_a, "Org A Project")
        project_b = await create_project(client, token_b, "Org B Project")

        # --- project creation seeds the 4 default states -----------------------
        states_a = await list_states(client, token_a, project_a["id"])
        states_b = await list_states(client, token_b, project_b["id"])
        assert {s["name"] for s in states_a} == {"Backlog", "In Progress", "Review", "Done"}
        assert sum(1 for s in states_a if s["is_default"]) == 1
        print("PASS: creating a project seeds exactly the 4 default workflow states")

        backlog_a = next(s for s in states_a if s["name"] == "Backlog")
        done_a = next(s for s in states_a if s["name"] == "Done")
        backlog_b = next(s for s in states_b if s["name"] == "Backlog")

        # --- ticket creation defaults to the project's default state -----------
        ticket_a = await create_ticket(client, token_a, project_a["id"], "Org A Ticket")
        assert ticket_a["workflow_state_id"] == backlog_a["id"], "new ticket should land in Backlog"
        print("PASS: a new ticket with no explicit state lands in the project's default state")

        ticket_b = await create_ticket(client, token_b, project_b["id"], "Org B Ticket")

        # --- direct PATCH/DELETE of org B's workflow state by id ---------------
        # (there's no "get one" route for workflow states — only list and
        # mutate — so PATCH/DELETE are the two direct-by-id paths that exist)
        cross_patch = await client.patch(
            f"/projects/{project_a['id']}/workflow-states/{backlog_b['id']}",
            json={"name": "hijacked"},
            headers=auth_headers(token_a),
        )
        assert cross_patch.status_code == 404, f"expected 404, got {cross_patch.status_code}"
        print("PASS: org A directly PATCHing org B's workflow state id (under org A's own project) -> 404")

        cross_delete = await client.delete(
            f"/projects/{project_a['id']}/workflow-states/{backlog_b['id']}",
            headers=auth_headers(token_a),
        )
        assert cross_delete.status_code == 404, f"expected 404, got {cross_delete.status_code}"
        print("PASS: org A directly DELETEing org B's workflow state id -> 404")

        # --- org A cannot move org B's ticket via direct id ---------------------
        cross_move = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_b['id']}",
            json={"workflow_state_id": done_a["id"], "position": 1.0},
            headers=auth_headers(token_a),
        )
        assert cross_move.status_code == 404, f"expected 404, got {cross_move.status_code}"
        print("PASS: org A directly moving org B's ticket id -> 404")

        still_b = await client.get(
            f"/projects/{project_b['id']}/tickets/{ticket_b['id']}", headers=auth_headers(token_b)
        )
        still_b.raise_for_status()
        assert still_b.json()["workflow_state_id"] == backlog_b["id"], "org B's ticket moved unexpectedly"
        print("PASS: org B's ticket is still in Backlog after org A's attempted cross-tenant move")

        # --- sanity: the legitimate move within org A actually works -----------
        own_move = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_a['id']}",
            json={"workflow_state_id": done_a["id"], "position": 2048.0},
            headers=auth_headers(token_a),
        )
        own_move.raise_for_status()
        moved = own_move.json()
        assert moved["workflow_state_id"] == done_a["id"]
        assert moved["position"] == 2048.0
        print("PASS: org A can move its own ticket to another column")

        # --- moving a ticket to a state from a different project is rejected ---
        # (backlog_b is a real workflow_states id, just not in project_a)
        bad_move = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_a['id']}",
            json={"workflow_state_id": backlog_b["id"], "position": 1.0},
            headers=auth_headers(token_a),
        )
        assert bad_move.status_code in (404, 422), f"expected 404/422, got {bad_move.status_code}"
        print("PASS: moving a ticket to a workflow_state_id from a different project is rejected")

    print("\nAll workflow-state tenant-isolation checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
