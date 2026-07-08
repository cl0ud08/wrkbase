"""Proves sprints are tenant- and project-scoped end to end over real HTTP,
plus the genuinely new business logic this slice added: the single-
active-sprint constraint under a real concurrent race, the backlog
definition, and complete_sprint's auto-return-to-backlog behavior. Run:

    docker compose exec api python -m scripts.verify_sprints_rls
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


async def create_sprint(
    client: httpx.AsyncClient, access_token: str, project_id: str, name: str
) -> httpx.Response:
    return await client.post(
        f"/projects/{project_id}/sprints",
        json={
            "name": name,
            "goal": None,
            "start_date": "2026-07-01",
            "end_date": "2026-07-14",
        },
        headers=auth_headers(access_token),
    )


async def create_ticket(
    client: httpx.AsyncClient,
    access_token: str,
    project_id: str,
    *,
    title: str,
    story_points: int | None = None,
) -> httpx.Response:
    return await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": "task", "title": title, "description": None, "story_points": story_points},
        headers=auth_headers(access_token),
    )


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        tokens_a = await signup(client, f"Sprint Org A {suffix}", f"sprint-a-{suffix}@example.dev")
        tokens_b = await signup(client, f"Sprint Org B {suffix}", f"sprint-b-{suffix}@example.dev")
        token_a, token_b = tokens_a["access_token"], tokens_b["access_token"]

        project_a1 = await create_project(client, token_a, "Org A Project 1")
        project_a2 = await create_project(client, token_a, "Org A Project 2")
        project_b = await create_project(client, token_b, "Org B Project")

        # --- cross-org: a sprint from another org is invisible, not just unreachable
        sprint_resp = await create_sprint(client, token_a, project_a1["id"], "Cross-org test sprint")
        sprint_resp.raise_for_status()
        sprint_a1_x = sprint_resp.json()
        assert sprint_a1_x["status"] == "planned"
        assert sprint_a1_x["total_points"] == 0
        print("PASS: sprint created in planned status with total_points 0")

        cross_get = await client.get(
            f"/projects/{project_a1['id']}/sprints/{sprint_a1_x['id']}", headers=auth_headers(token_b)
        )
        assert cross_get.status_code == 404, f"expected 404, got {cross_get.status_code}"
        print("PASS: org B directly GETting org A's sprint (via org A's project id) -> 404")

        cross_project_of_b = await client.get(
            f"/projects/{project_b['id']}/sprints/{sprint_a1_x['id']}", headers=auth_headers(token_b)
        )
        assert cross_project_of_b.status_code == 404, f"expected 404, got {cross_project_of_b.status_code}"
        print("PASS: org B GETting org A's sprint id under org B's own (unrelated) project -> 404")

        # --- cross-project (same org): structural, same class as assignee ---------
        sprint_a2 = (await create_sprint(client, token_a, project_a2["id"], "Project A2 Sprint")).json()
        ticket_in_a1 = (
            await create_ticket(client, token_a, project_a1["id"], title="Lives in project A1")
        ).json()
        cross_project_assign = await client.patch(
            f"/projects/{project_a1['id']}/tickets/{ticket_in_a1['id']}",
            json={"sprint_id": sprint_a2["id"]},
            headers=auth_headers(token_a),
        )
        assert cross_project_assign.status_code == 422, (
            f"expected 422, got {cross_project_assign.status_code}"
        )
        print("PASS: assigning a ticket to a sprint from a *different project* in the same org is rejected (422)")

        # --- single-active-sprint: enforced under an actual concurrent race -------
        race_1 = (await create_sprint(client, token_a, project_a1["id"], "Race Sprint 1")).json()
        race_2 = (await create_sprint(client, token_a, project_a1["id"], "Race Sprint 2")).json()

        results = await asyncio.gather(
            client.post(f"/projects/{project_a1['id']}/sprints/{race_1['id']}/start", headers=auth_headers(token_a)),
            client.post(f"/projects/{project_a1['id']}/sprints/{race_2['id']}/start", headers=auth_headers(token_a)),
        )
        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 409], f"expected exactly one 200 and one 409, got {statuses}"
        print("PASS: two concurrent start-sprint calls on different sprints in the same project -> exactly one wins (200/409)")

        active_list = await client.get(f"/projects/{project_a1['id']}/sprints", headers=auth_headers(token_a))
        active_list.raise_for_status()
        active_sprints = [s for s in active_list.json() if s["status"] == "active"]
        assert len(active_sprints) == 1, f"expected exactly 1 active sprint, found {len(active_sprints)}"
        winner_id = active_sprints[0]["id"]
        print("PASS: exactly one sprint in the project is actually active after the race")

        # starting an already-active sprint is a clean 400, not a second race outcome
        restart = await client.post(
            f"/projects/{project_a1['id']}/sprints/{winner_id}/start", headers=auth_headers(token_a)
        )
        assert restart.status_code == 400, f"expected 400, got {restart.status_code}"
        print("PASS: starting an already-active sprint is rejected (400)")

        # the loser can be deleted (still planned, no tickets); the winner cannot (active)
        loser_id = race_2["id"] if winner_id == race_1["id"] else race_1["id"]
        delete_loser = await client.delete(
            f"/projects/{project_a1['id']}/sprints/{loser_id}", headers=auth_headers(token_a)
        )
        assert delete_loser.status_code == 204, f"expected 204, got {delete_loser.status_code}"
        delete_winner = await client.delete(
            f"/projects/{project_a1['id']}/sprints/{winner_id}", headers=auth_headers(token_a)
        )
        assert delete_winner.status_code == 409, f"expected 409, got {delete_winner.status_code}"
        print("PASS: a planned sprint with no tickets can be deleted; an active sprint cannot")

        # winner (active) is used below as the sprint we actually plan work into.
        active_sprint_id = winner_id

        # --- backlog: paginated, and defined as sprint_id IS NULL ------------------
        t1 = (await create_ticket(client, token_a, project_a1["id"], title="Backlog A", story_points=3)).json()
        t2 = (await create_ticket(client, token_a, project_a1["id"], title="Backlog B", story_points=5)).json()
        t3 = (await create_ticket(client, token_a, project_a1["id"], title="Backlog C", story_points=None)).json()

        page1 = await client.get(
            f"/projects/{project_a1['id']}/tickets/backlog?limit=2&offset=0", headers=auth_headers(token_a)
        )
        page1.raise_for_status()
        page1_body = page1.json()
        assert page1_body["limit"] == 2 and page1_body["offset"] == 0
        assert len(page1_body["items"]) == 2
        assert page1_body["total"] >= 3
        page2 = await client.get(
            f"/projects/{project_a1['id']}/tickets/backlog?limit=2&offset=2", headers=auth_headers(token_a)
        )
        page2.raise_for_status()
        assert len(page2.json()["items"]) >= 1
        print("PASS: GET .../tickets/backlog is paginated (limit/offset/total honored)")

        # --- bulk-assign: backlog -> sprint, in one call ---------------------------
        assign_resp = await client.post(
            f"/projects/{project_a1['id']}/sprints/{active_sprint_id}/assign",
            json={"ticket_ids": [t1["id"], t2["id"]]},
            headers=auth_headers(token_a),
        )
        assign_resp.raise_for_status()
        assigned = assign_resp.json()
        assert {t["id"] for t in assigned} == {t1["id"], t2["id"]}
        assert all(t["sprint_id"] == active_sprint_id for t in assigned)
        print("PASS: bulk-assign moves exactly the requested tickets from backlog into the sprint")

        # re-assigning an already-in-a-sprint ticket (not currently backlog) is rejected
        reassign = await client.post(
            f"/projects/{project_a1['id']}/sprints/{active_sprint_id}/assign",
            json={"ticket_ids": [t1["id"]]},
            headers=auth_headers(token_a),
        )
        assert reassign.status_code == 422, f"expected 422, got {reassign.status_code}"
        print("PASS: bulk-assigning a ticket that isn't currently in the backlog is rejected (422)")

        sprint_after_assign = (
            await client.get(
                f"/projects/{project_a1['id']}/sprints/{active_sprint_id}", headers=auth_headers(token_a)
            )
        ).json()
        assert sprint_after_assign["total_points"] == 8, (
            f"expected total_points 8 (3+5), got {sprint_after_assign['total_points']}"
        )
        print("PASS: sprint total_points is computed server-side (sum of story_points), not left to the frontend")

        backlog_after_assign = (
            await client.get(f"/projects/{project_a1['id']}/tickets/backlog?limit=200", headers=auth_headers(token_a))
        ).json()
        backlog_ids = {t["id"] for t in backlog_after_assign["items"]}
        assert t1["id"] not in backlog_ids and t2["id"] not in backlog_ids
        assert t3["id"] in backlog_ids
        print("PASS: assigned tickets disappear from the backlog; the untouched one stays")

        # --- complete_sprint: unfinished tickets auto-return, finished ones don't --
        states = (
            await client.get(f"/projects/{project_a1['id']}/workflow-states", headers=auth_headers(token_a))
        ).json()
        terminal_state = max(states, key=lambda s: s["order"])

        move_t1_to_done = await client.patch(
            f"/projects/{project_a1['id']}/tickets/{t1['id']}",
            json={"workflow_state_id": terminal_state["id"]},
            headers=auth_headers(token_a),
        )
        move_t1_to_done.raise_for_status()
        # t2 deliberately left in its non-terminal (default/backlog) column.

        complete_resp = await client.post(
            f"/projects/{project_a1['id']}/sprints/{active_sprint_id}/complete", headers=auth_headers(token_a)
        )
        complete_resp.raise_for_status()
        assert complete_resp.json()["status"] == "completed"

        t1_after = (
            await client.get(f"/projects/{project_a1['id']}/tickets/{t1['id']}", headers=auth_headers(token_a))
        ).json()
        t2_after = (
            await client.get(f"/projects/{project_a1['id']}/tickets/{t2['id']}", headers=auth_headers(token_a))
        ).json()
        assert t1_after["sprint_id"] == active_sprint_id, "finished ticket should keep its sprint_id (history)"
        assert t2_after["sprint_id"] is None, "unfinished ticket should auto-return to the backlog"
        print("PASS: completing a sprint returns unfinished tickets to the backlog and preserves finished ones' history")

        backlog_after_complete = (
            await client.get(f"/projects/{project_a1['id']}/tickets/backlog?limit=200", headers=auth_headers(token_a))
        ).json()
        backlog_ids_after = {t["id"] for t in backlog_after_complete["items"]}
        assert t2["id"] in backlog_ids_after, "auto-returned ticket should reappear in the backlog"
        assert t1["id"] not in backlog_ids_after, "finished ticket (still sprint-attached) should not"
        print("PASS: backlog reflects the auto-return immediately")

        # assigning into a completed sprint is rejected
        assign_into_completed = await client.post(
            f"/projects/{project_a1['id']}/sprints/{active_sprint_id}/assign",
            json={"ticket_ids": [t3["id"]]},
            headers=auth_headers(token_a),
        )
        assert assign_into_completed.status_code == 400, f"expected 400, got {assign_into_completed.status_code}"
        print("PASS: bulk-assigning into a completed sprint is rejected (400)")

        # completing an already-completed sprint is rejected
        recomplete = await client.post(
            f"/projects/{project_a1['id']}/sprints/{active_sprint_id}/complete", headers=auth_headers(token_a)
        )
        assert recomplete.status_code == 400, f"expected 400, got {recomplete.status_code}"
        print("PASS: completing an already-completed sprint is rejected (400)")

        # --- delete blocked while tickets are still assigned -----------------------
        blocking_sprint = (await create_sprint(client, token_a, project_a1["id"], "Delete-block test")).json()
        (
            await client.post(
                f"/projects/{project_a1['id']}/sprints/{blocking_sprint['id']}/assign",
                json={"ticket_ids": [t3["id"]]},
                headers=auth_headers(token_a),
            )
        ).raise_for_status()
        delete_blocked = await client.delete(
            f"/projects/{project_a1['id']}/sprints/{blocking_sprint['id']}", headers=auth_headers(token_a)
        )
        assert delete_blocked.status_code == 409, f"expected 409, got {delete_blocked.status_code}"
        print("PASS: deleting a sprint that still has tickets assigned is rejected (409)")

    print("\nAll sprint tenant-isolation and business-rule checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
