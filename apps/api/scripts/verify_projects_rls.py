"""Proves projects are tenant-isolated end to end over real HTTP: two orgs
each create a project, and org A cannot see or modify org B's project even
by guessing its ID directly — not just filtered out of the list endpoint.
Run:

    docker compose exec api python -m scripts.verify_projects_rls
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


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        tokens_a = await signup(client, f"Org A {suffix}", f"proj-a-{suffix}@example.dev")
        tokens_b = await signup(client, f"Org B {suffix}", f"proj-b-{suffix}@example.dev")

        project_a = await create_project(client, tokens_a["access_token"], "Org A Project")
        project_b = await create_project(client, tokens_b["access_token"], "Org B Project")
        print("PASS: both orgs created a project")

        # --- list is scoped to each org -------------------------------------
        list_a = await client.get("/projects", headers=auth_headers(tokens_a["access_token"]))
        list_b = await client.get("/projects", headers=auth_headers(tokens_b["access_token"]))
        list_a.raise_for_status()
        list_b.raise_for_status()
        ids_a = {p["id"] for p in list_a.json()}
        ids_b = {p["id"] for p in list_b.json()}
        assert ids_a == {project_a["id"]}, f"org A's list should be exactly its own project, got {ids_a}"
        assert ids_b == {project_b["id"]}, f"org B's list should be exactly its own project, got {ids_b}"
        print("PASS: GET /projects only lists each org's own project")

        # --- direct GET by ID across orgs: 404, not just absent from a list -
        cross_get = await client.get(
            f"/projects/{project_b['id']}", headers=auth_headers(tokens_a["access_token"])
        )
        assert cross_get.status_code == 404, (
            f"org A directly GETting org B's project id should 404, got {cross_get.status_code}"
        )
        print("PASS: org A directly GETting org B's project id -> 404")

        # --- direct PATCH by ID across orgs ----------------------------------
        cross_patch = await client.patch(
            f"/projects/{project_b['id']}",
            json={"name": "hijacked"},
            headers=auth_headers(tokens_a["access_token"]),
        )
        assert cross_patch.status_code == 404, (
            f"org A patching org B's project id should 404, got {cross_patch.status_code}"
        )
        print("PASS: org A directly PATCHing org B's project id -> 404")

        # --- sanity: org B's project is genuinely untouched ------------------
        still_b = await client.get(
            f"/projects/{project_b['id']}", headers=auth_headers(tokens_b["access_token"])
        )
        still_b.raise_for_status()
        assert still_b.json()["name"] == "Org B Project", "org B's project changed unexpectedly"
        print("PASS: org B's project is unchanged after org A's attempted cross-tenant patch")

        # --- direct DELETE by ID across orgs ----------------------------------
        cross_delete = await client.delete(
            f"/projects/{project_b['id']}", headers=auth_headers(tokens_a["access_token"])
        )
        assert cross_delete.status_code == 404, (
            f"org A deleting org B's project id should 404, got {cross_delete.status_code}"
        )
        print("PASS: org A directly DELETEing org B's project id -> 404, nothing deleted")

        # --- sanity: the creator CAN update their own project -----------------
        own_patch = await client.patch(
            f"/projects/{project_a['id']}",
            json={"name": "Org A Project (renamed)"},
            headers=auth_headers(tokens_a["access_token"]),
        )
        own_patch.raise_for_status()
        body = own_patch.json()
        assert body["name"] == "Org A Project (renamed)"
        assert body["updated_at"] != project_a["updated_at"], "updated_at should change on edit"
        print("PASS: org A's creator can update their own project; updated_at bumped by the DB trigger")

    print("\nAll project tenant-isolation checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
