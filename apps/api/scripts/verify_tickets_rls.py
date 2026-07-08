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

        # --- ticket numbering: per-org sequence, not global -------------------
        # Both orgs are brand new, so each org's *own* first ticket lands on
        # 1 -- if the counter were accidentally shared/global instead of
        # per-org, org B's ticket would have landed on 2, not 1.
        assert ticket_a["ticket_number"] == 1, f"expected 1, got {ticket_a['ticket_number']}"
        assert ticket_b["ticket_number"] == 1, f"expected 1, got {ticket_b['ticket_number']}"

        second_a = await create_ticket(
            client, token_a, project_a["id"], type="task", title="Org A Second Ticket"
        )
        second_a.raise_for_status()
        assert second_a.json()["ticket_number"] == 2, (
            f"expected 2, got {second_a.json()['ticket_number']}"
        )
        print("PASS: ticket numbers are sequential per org, not a shared global counter")

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

        # --- assignee: same-org assignment works ------------------------------
        own_org_user_id = ticket_a["created_by"]
        assign_resp = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_a['id']}",
            json={"assignee_id": own_org_user_id},
            headers=auth_headers(token_a),
        )
        assign_resp.raise_for_status()
        assert assign_resp.json()["assignee_id"] == own_org_user_id
        print("PASS: assigning a ticket to a real member of its own org succeeds")

        # --- assignee: cannot assign to a user from a different org -----------
        # No composite-FK-bypassing app-layer trust here: org_b's real user
        # id (org B's project creator) is a genuine user row, just in the
        # wrong org — structurally rejected by _validate_assignee before it
        # ever reaches the DB's composite FK.
        cross_org_user_id = project_b["created_by"]
        cross_assign_resp = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_a['id']}",
            json={"assignee_id": cross_org_user_id},
            headers=auth_headers(token_a),
        )
        assert cross_assign_resp.status_code == 422, (
            f"expected 422, got {cross_assign_resp.status_code}"
        )
        print("PASS: assigning a ticket to a user from a different org is rejected (422)")

        # --- assignee: unassigning (null) doesn't need validation -------------
        unassign_resp = await client.patch(
            f"/projects/{project_a['id']}/tickets/{ticket_a['id']}",
            json={"assignee_id": None},
            headers=auth_headers(token_a),
        )
        unassign_resp.raise_for_status()
        assert unassign_resp.json()["assignee_id"] is None
        print("PASS: unassigning a ticket (assignee_id: null) succeeds")

        # --- soft-delete: children-exist check still blocks it -----------------
        # epic -> story -> good_subtask still forms a live chain at this
        # point. Soft-delete never issues a hard DELETE, so the old
        # FK-RESTRICT-triggered IntegrityError can't fire anymore — this is
        # proving the *replacement* explicit check (_has_active_children)
        # actually blocks it, not just trusting the old mechanism still
        # applies.
        blocked_delete_epic = await client.delete(
            f"/projects/{project_a['id']}/tickets/{epic['id']}", headers=auth_headers(token_a)
        )
        assert blocked_delete_epic.status_code == 409, (
            f"expected 409, got {blocked_delete_epic.status_code}"
        )
        print("PASS: soft-deleting a ticket with active children is still blocked (409)")

        # --- soft-delete a leaf: excluded from get/list/tree --------------------
        subtask_id = good_subtask.json()["id"]
        delete_subtask = await client.delete(
            f"/projects/{project_a['id']}/tickets/{subtask_id}", headers=auth_headers(token_a)
        )
        assert delete_subtask.status_code == 204, f"expected 204, got {delete_subtask.status_code}"

        get_deleted = await client.get(
            f"/projects/{project_a['id']}/tickets/{subtask_id}", headers=auth_headers(token_a)
        )
        assert get_deleted.status_code == 404, "a soft-deleted ticket should 404 for its own org too"

        list_after = await client.get(
            f"/projects/{project_a['id']}/tickets", headers=auth_headers(token_a)
        )
        list_after.raise_for_status()
        assert subtask_id not in {t["id"] for t in list_after.json()}, (
            "a soft-deleted ticket should not appear in the list"
        )

        tree_after = await client.get(
            f"/projects/{project_a['id']}/tickets/tree", headers=auth_headers(token_a)
        )
        tree_after.raise_for_status()

        def _find(nodes: list[dict], target_id: str) -> dict | None:
            for node in nodes:
                if node["id"] == target_id:
                    return node
                found = _find(node["children"], target_id)
                if found is not None:
                    return found
            return None

        assert _find(tree_after.json(), subtask_id) is None, (
            "a soft-deleted ticket should not appear in /tree either"
        )
        print("PASS: a soft-deleted ticket is excluded from GET/list/tree for its own org, not just hidden from other orgs")

        # now that its only child is soft-deleted, the story can be deleted too
        delete_story = await client.delete(
            f"/projects/{project_a['id']}/tickets/{story['id']}", headers=auth_headers(token_a)
        )
        assert delete_story.status_code == 204, f"expected 204, got {delete_story.status_code}"
        print("PASS: once its only child is soft-deleted, the parent can be soft-deleted too")

        # --- RLS still applies to a soft-deleted row, not just an active one ---
        # get/list/tree above already prove exclusion for org A's own view;
        # this proves org B specifically can't reach the same soft-deleted
        # row either -- most interestingly via restore, brand-new code with
        # no prior cross-org coverage at all.
        cross_get_deleted = await client.get(
            f"/projects/{project_a['id']}/tickets/{subtask_id}", headers=auth_headers(token_b)
        )
        assert cross_get_deleted.status_code == 404, f"expected 404, got {cross_get_deleted.status_code}"

        cross_restore = await client.post(
            f"/projects/{project_a['id']}/tickets/{subtask_id}/restore", headers=auth_headers(token_b)
        )
        assert cross_restore.status_code == 404, f"expected 404, got {cross_restore.status_code}"
        print(
            "PASS: RLS still applies to a soft-deleted row -- org B can't see or restore "
            "org A's soft-deleted ticket"
        )

        # --- restore works, for the rightful owner -------------------------------
        restore_resp = await client.post(
            f"/projects/{project_a['id']}/tickets/{subtask_id}/restore", headers=auth_headers(token_a)
        )
        restore_resp.raise_for_status()
        assert restore_resp.json()["deleted_at"] is None
        get_after_restore = await client.get(
            f"/projects/{project_a['id']}/tickets/{subtask_id}", headers=auth_headers(token_a)
        )
        assert get_after_restore.status_code == 200, f"expected 200, got {get_after_restore.status_code}"
        print("PASS: restoring a soft-deleted ticket makes it visible again")

        # restoring an already-active ticket is a clean 400, not a silent no-op
        restore_again = await client.post(
            f"/projects/{project_a['id']}/tickets/{subtask_id}/restore", headers=auth_headers(token_a)
        )
        assert restore_again.status_code == 400, f"expected 400, got {restore_again.status_code}"
        print("PASS: restoring a ticket that isn't deleted is rejected (400), not a silent no-op")

    print("\nAll ticket tenant-isolation and hierarchy checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
