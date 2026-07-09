"""Proves notifications are created correctly at both real trigger points
(assignment, invite acceptance), are strictly isolated by org AND by
recipient (not just org — the actual privacy boundary this table's RLS,
migration 0016, is built around), and are delivered live over
/ws/notifications to the right user only, even when a same-org teammate is
simultaneously connected to the exact same project's board room. Run:

    docker compose exec api python -m scripts.verify_notifications
"""

import asyncio
import uuid

import httpx
import websockets

BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"
VALID_ORIGIN = "http://localhost:3000"
PASSWORD = "correct horse battery staple"


async def signup(client: httpx.AsyncClient, org_name: str, email: str) -> dict:
    resp = await client.post(
        "/auth/signup", json={"org_name": org_name, "email": email, "password": PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def invite_and_join(
    client: httpx.AsyncClient, admin_token: str, email: str, role: str = "member"
) -> dict:
    inv = await client.post(
        "/invites", json={"email": email, "role": role}, headers=auth_headers(admin_token)
    )
    inv.raise_for_status()
    join = await client.post(
        "/auth/signup", json={"email": email, "password": PASSWORD, "invite_token": inv.json()["token"]}
    )
    join.raise_for_status()
    return join.json()


async def get_ws_ticket(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post("/auth/ws-ticket", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()["ticket"]


async def notifications(client: httpx.AsyncClient, token: str) -> list[dict]:
    resp = await client.get("/notifications", headers=auth_headers(token))
    resp.raise_for_status()
    return resp.json()["items"]


async def expect_no_message(ws, *, label: str) -> None:
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
    except asyncio.TimeoutError:
        print(f"PASS: {label} -> nothing received")
    else:
        raise AssertionError(f"{label}: expected no message, got {msg!r}")


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]

    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # --- setup: two independent orgs -------------------------------------
        org_a = await signup(client, f"Notif Org A {suffix}", f"admin-a-{suffix}@example.dev")
        admin_a_token = org_a["access_token"]
        org_b = await signup(client, f"Notif Org B {suffix}", f"admin-b-{suffix}@example.dev")
        admin_b_token = org_b["access_token"]
        print("PASS: two independent orgs set up")

        # --- trigger point 1: invite accepted -> notifies the inviting admin -
        member_a = await invite_and_join(client, admin_a_token, f"member-a-{suffix}@example.dev")
        member_a_token = member_a["access_token"]
        member_a_id = (await client.get("/auth/me", headers=auth_headers(member_a_token))).json()["id"]

        admin_a_notifs = await notifications(client, admin_a_token)
        invite_notifs = [n for n in admin_a_notifs if n["type"] == "invite_accepted"]
        assert len(invite_notifs) == 1, f"expected 1 invite_accepted notification, got {len(invite_notifs)}"
        assert invite_notifs[0]["payload"]["accepted_email"] == f"member-a-{suffix}@example.dev"
        assert invite_notifs[0]["payload"]["role"] == "member"
        assert invite_notifs[0]["read_at"] is None
        print("PASS: invite acceptance notifies the admin who sent it, with the right email/role")

        # --- trigger point 2: ticket assignment -> notifies the new assignee -
        project = await client.post(
            "/projects", json={"name": "Notif Project", "description": None},
            headers=auth_headers(admin_a_token),
        )
        project.raise_for_status()
        project_id = project.json()["id"]
        ticket = await client.post(
            f"/projects/{project_id}/tickets",
            json={"type": "task", "title": "Assign me", "description": None},
            headers=auth_headers(admin_a_token),
        )
        ticket.raise_for_status()
        ticket_id = ticket.json()["id"]

        assign = await client.patch(
            f"/projects/{project_id}/tickets/{ticket_id}",
            json={"assignee_id": member_a_id},
            headers=auth_headers(admin_a_token),
        )
        assign.raise_for_status()

        member_a_notifs = await notifications(client, member_a_token)
        assignment_notifs = [n for n in member_a_notifs if n["type"] == "assignment"]
        assert len(assignment_notifs) == 1, f"expected 1 assignment notification, got {len(assignment_notifs)}"
        assert assignment_notifs[0]["payload"]["ticket_title"] == "Assign me"
        assert assignment_notifs[0]["payload"]["project_id"] == project_id
        print("PASS: ticket assignment notifies the new assignee, with the right ticket/project")

        # --- assigning to yourself needs no notification ----------------------
        self_assign = await client.patch(
            f"/projects/{project_id}/tickets/{ticket_id}",
            json={"assignee_id": (await client.get('/auth/me', headers=auth_headers(admin_a_token))).json()["id"]},
            headers=auth_headers(admin_a_token),
        )
        self_assign.raise_for_status()
        admin_a_notifs_after = await notifications(client, admin_a_token)
        assert not any(n["type"] == "assignment" for n in admin_a_notifs_after), (
            "assigning a ticket to yourself should not generate a notification"
        )
        print("PASS: assigning a ticket to yourself generates no notification")

        # --- the acting admin never received the assignment notification -----
        # (a different check from the self-assign one above: this confirms
        # the *original* assignment to member_a, made by admin_a, never
        # notified admin_a either — notifications go to the recipient, not
        # the actor.)
        assert not any(
            n["type"] == "assignment" and n["payload"].get("ticket_title") == "Assign me"
            for n in admin_a_notifs_after
        )
        print("PASS: the acting admin never receives their own assignment action as a notification")

        # --- cross-org isolation: org B sees none of org A's activity ---------
        admin_b_notifs = await notifications(client, admin_b_token)
        assert admin_b_notifs == [], f"org B admin should have zero notifications, got {admin_b_notifs}"
        print("PASS: a completely unrelated org has zero notifications from org A's activity")

        # --- mark-read / unread-count -----------------------------------------
        unread_before = await client.get("/notifications/unread-count", headers=auth_headers(member_a_token))
        assert unread_before.json()["count"] == 1

        mark = await client.patch(
            f"/notifications/{assignment_notifs[0]['id']}/read", headers=auth_headers(member_a_token)
        )
        mark.raise_for_status()
        assert mark.json()["read_at"] is not None

        unread_after = await client.get("/notifications/unread-count", headers=auth_headers(member_a_token))
        assert unread_after.json()["count"] == 0
        print("PASS: marking a notification read updates its read_at and the unread count")

        # --- a user can't mark someone else's notification as read ------------
        cross_mark = await client.patch(
            f"/notifications/{invite_notifs[0]['id']}/read", headers=auth_headers(member_a_token)
        )
        assert cross_mark.status_code == 404, f"expected 404, got {cross_mark.status_code}"
        print("PASS: marking another user's notification as read -> 404, not silently allowed")

        # --- live delivery: two users, same project, only the right one hears -
        member_a_ws_ticket = await get_ws_ticket(client, member_a_token)
        admin_a_ws_ticket = await get_ws_ticket(client, admin_a_token)
        member_a_proj_ticket = await get_ws_ticket(client, member_a_token)
        admin_a_proj_ticket = await get_ws_ticket(client, admin_a_token)

    def notif_uri(ticket: str) -> str:
        return f"{WS_URL}/ws/notifications?ticket={ticket}"

    def proj_uri(ticket: str) -> str:
        return f"{WS_URL}/ws/projects/{project_id}?ticket={ticket}"

    async with (
        websockets.connect(notif_uri(member_a_ws_ticket), origin=VALID_ORIGIN) as member_a_notif_ws,
        websockets.connect(notif_uri(admin_a_ws_ticket), origin=VALID_ORIGIN) as admin_a_notif_ws,
        websockets.connect(proj_uri(member_a_proj_ticket), origin=VALID_ORIGIN) as member_a_proj_ws,
        websockets.connect(proj_uri(admin_a_proj_ticket), origin=VALID_ORIGIN) as admin_a_proj_ws,
    ):
        print(
            "PASS: member and admin both connected simultaneously -- their own "
            "notification room each, and both to the SAME project's board room"
        )

        # Ticket is currently assigned to admin_a (the self-assign check
        # above), so re-assigning to member_a here is a genuine change --
        # exactly the condition create_notification's caller in
        # update_ticket requires (new_assignee_id != previous_assignee_id).
        async with httpx.AsyncClient(base_url=BASE_URL) as client:
            reassign = await client.patch(
                f"/projects/{project_id}/tickets/{ticket_id}",
                json={"assignee_id": member_a_id},
                headers=auth_headers(admin_a_token),
            )
            reassign.raise_for_status()

        msg = await asyncio.wait_for(member_a_notif_ws.recv(), timeout=5)
        assert '"type": "notification.created"' in msg
        assert ticket_id in msg
        print("PASS: member_a's OWN notification room receives the new assignment notification")

        await expect_no_message(admin_a_notif_ws, label="admin_a's notification room (not the recipient)")

        # Board events and notification events are on structurally
        # different channels (project:{id} vs notifications:{user_id}).
        # Every subscriber of the project channel gets the SAME
        # ticket.updated broadcast, including the actor who made the
        # change themselves — self-echo filtering is a frontend concern
        # (see the board page's updatedBy check), not something the
        # server suppresses. What matters here: neither board connection
        # ever receives a notification event, even though a real
        # notification was just created by the exact same request.
        board_msg_member = await asyncio.wait_for(member_a_proj_ws.recv(), timeout=5)
        board_msg_admin = await asyncio.wait_for(admin_a_proj_ws.recv(), timeout=5)
        assert '"type": "ticket.updated"' in board_msg_member
        assert '"type": "ticket.updated"' in board_msg_admin
        assert "notification.created" not in board_msg_member
        assert "notification.created" not in board_msg_admin
        print(
            "PASS: the project board room broadcasts ticket.updated to every "
            "subscriber including the actor, but never a notification event"
        )

    print("\nAll notification checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
