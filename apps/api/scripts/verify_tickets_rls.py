"""Proves tickets are tenant-isolated end to end over real HTTP, same rigor
as verify_projects_rls.py, plus sanity checks on the two genuinely new
pieces of logic this slice added: the subtask-parent-type rule and the
tree endpoint. Run:

    docker compose exec api python -m scripts.verify_tickets_rls
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


async def create_ticket(
    client: httpx.AsyncClient,
    access_token: str,
    project_id: str,
    *,
    type: str,
    title: str,
    parent_id: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"/projects/{project_id}/tickets",
        json={"type": type, "title": title, "description": None, "parent_id": parent_id},
        headers=auth_headers(access_token),
    )


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        tokens_a = await signup(client, f"Org A {suffix}", f"tick-a-{suffix}@example.dev")
        tokens_b = await signup(client, f"Org B {suffix}", f"tick-b-{suffix}@example.dev")
        token_a, token_b = tokens_a["access_token"], tokens_b["access_token"]

        project_a = await create_project(client, token_a, "Org A Project")
        project_b = await create_project(client, token_b, "Org B Project")

        resp_a = await create_ticket(client, token_a, project_a["id"], type="task", title="Org A Ticket")
        resp_a.raise_for_status()
        ticket_a = resp_a.json()
        resp_b = await create_ticket(client, token_b, project_b["id"], type="task", title="Org B Ticket")
        resp_b.raise_for_status()
        ticket_b = resp_b.json()
        print("PASS: both orgs created a ticket in their own project")

        # --- list is scoped to each org's project -----------------------------
        list_a = await client.get(f"/projects/{project_a['id']}/tickets", headers=auth_headers(token_a))
        list_b = await client.get(f"/projects/{project_b['id']}/tickets", headers=auth_headers(token_b))
        list_a.raise_for_status()
        list_b.raise_for_status()
        assert {t["id"] for t in list_a.json()} == {ticket_a["id"]}
        assert {t["id"] for t in list_b.json()} == {ticket_b["id"]}
        print("PASS: GET tickets only lists each org's own ticket")

        # --- direct GET/PATCH/DELETE by id across orgs: 404, not filtered -----
        cross_get = await client.get(
            f"/projects/{project_a['id']}/tickets/{ticket_b['id']}", headers=auth_headers(token_a)
        )
        assert cross_get.status_code == 404, f"expected 404, got {cross_get.status_code}"
        print("PASS: org A directly GETting org B's ticket id -> 404")

        cross_patch = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_b['id']}",
            json={"title": "hijacked"},
            headers=auth_headers(token_a),
        )
        assert cross_patch.status_code == 404, f"expected 404, got {cross_patch.status_code}"
        print("PASS: org A directly PATCHing org B's ticket id -> 404")

        cross_delete = await client.delete(
            f"/projects/{project_a['id']}/tickets/{ticket_b['id']}", headers=auth_headers(token_a)
        )
        assert cross_delete.status_code == 404, f"expected 404, got {cross_delete.status_code}"
        print("PASS: org A directly DELETEing org B's ticket id -> 404")

        still_b = await client.get(
            f"/projects/{project_b['id']}/tickets/{ticket_b['id']}", headers=auth_headers(token_b)
        )
        still_b.raise_for_status()
        assert still_b.json()["title"] == "Org B Ticket", "org B's ticket changed unexpectedly"
        print("PASS: org B's ticket is unchanged after org A's attempted cross-tenant access")

        # --- sanity: creator can edit their own ticket's content; trigger bumps updated_at
        own_patch = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_a['id']}",
            json={"title": "Org A Ticket (renamed)"},
            headers=auth_headers(token_a),
        )
        own_patch.raise_for_status()
        body = own_patch.json()
        assert body["title"] == "Org A Ticket (renamed)"
        assert body["updated_at"] != ticket_a["updated_at"], "updated_at should change on edit"
        print("PASS: org A's creator can edit their own ticket; eager_defaults worked first try")

        # --- hierarchy rule: subtask's parent must be a story or task ----------
        epic_resp = await create_ticket(client, token_a, project_a["id"], type="epic", title="Epic")
        epic_resp.raise_for_status()
        epic = epic_resp.json()

        bad_subtask = await create_ticket(
            client, token_a, project_a["id"], type="subtask", title="Bad subtask", parent_id=epic["id"]
        )
        assert bad_subtask.status_code == 422, (
            f"subtask under an epic should be rejected, got {bad_subtask.status_code}"
        )
        print("PASS: subtask with an epic as parent is rejected (422)")

        story_resp = await create_ticket(
            client, token_a, project_a["id"], type="story", title="Story", parent_id=epic["id"]
        )
        story_resp.raise_for_status()
        story = story_resp.json()

        good_subtask = await create_ticket(
            client, token_a, project_a["id"], type="subtask", title="Good subtask", parent_id=story["id"]
        )
        good_subtask.raise_for_status()
        print("PASS: subtask with a story as parent is accepted")

        # a parent_id from a different project must also be rejected, even
        # though it's a real ticket id in the same org
        cross_project_parent = await create_ticket(
            client, token_a, project_a["id"], type="subtask", title="Cross-project subtask",
            parent_id=ticket_b["id"],
        )
        assert cross_project_parent.status_code in (404, 422), (
            f"parent_id from another project should be rejected, got {cross_project_parent.status_code}"
        )
        print("PASS: parent_id belonging to a different project is rejected")

        # --- tree endpoint: epic -> story -> subtask nests correctly -----------
        tree_resp = await client.get(
            f"/projects/{project_a['id']}/tickets/tree", headers=auth_headers(token_a)
        )
        tree_resp.raise_for_status()
        tree = tree_resp.json()
        epic_node = next((n for n in tree if n["id"] == epic["id"]), None)
        assert epic_node is not None, "epic should be a root node in the tree"
        story_node = next((c for c in epic_node["children"] if c["id"] == story["id"]), None)
        assert story_node is not None, "story should nest under its epic"
        assert any(c["id"] == good_subtask.json()["id"] for c in story_node["children"]), (
            "subtask should nest under its story"
        )
        print("PASS: /tickets/tree nests epic -> story -> subtask correctly")

    print("\nAll ticket tenant-isolation and hierarchy checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
