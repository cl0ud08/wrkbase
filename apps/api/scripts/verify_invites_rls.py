"""Proves the invite + member-management flow end to end over real HTTP:
an invite token deterministically joins its own org (never a different one,
no matter what a client claims), a tampered token is rejected, an invite is
bound to a specific email, only admins can manage invites/members, and the
last-admin-removal guard actually blocks the lockout it exists to prevent.
Run:

    docker compose exec api python -m scripts.verify_invites_rls
"""

import asyncio
import uuid

import httpx

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"


async def signup(
    client: httpx.AsyncClient,
    email: str,
    *,
    org_name: str | None = None,
    invite_token: str | None = None,
) -> httpx.Response:
    body: dict[str, str] = {"email": email, "password": PASSWORD}
    if org_name is not None:
        body["org_name"] = org_name
    if invite_token is not None:
        body["invite_token"] = invite_token
    return await client.post("/auth/signup", json=body)


def auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


async def me(client: httpx.AsyncClient, access_token: str) -> dict:
    resp = await client.get("/auth/me", headers=auth_headers(access_token))
    resp.raise_for_status()
    return resp.json()


async def create_invite(
    client: httpx.AsyncClient, access_token: str, email: str, role: str = "member"
) -> httpx.Response:
    return await client.post(
        "/invites", json={"email": email, "role": role}, headers=auth_headers(access_token)
    )


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # --- setup: two independent orgs -----------------------------------
        resp_a = await signup(client, f"admin-a-{suffix}@example.dev", org_name=f"Org A {suffix}")
        resp_a.raise_for_status()
        token_a = resp_a.json()["access_token"]
        me_a = await me(client, token_a)
        org_a_id, org_a_name = me_a["org_id"], me_a["org_name"]

        resp_b = await signup(client, f"admin-b-{suffix}@example.dev", org_name=f"Org B {suffix}")
        resp_b.raise_for_status()
        token_b = resp_b.json()["access_token"]
        refresh_admin_b = resp_b.json()["refresh_token"]
        me_b = await me(client, token_b)
        org_b_id = me_b["org_id"]
        print("PASS: two independent orgs set up, each with one admin")

        # --- create + preview an invite -------------------------------------
        invitee_email = f"invitee-{suffix}@example.dev"
        invite_resp = await create_invite(client, token_a, invitee_email, role="member")
        invite_resp.raise_for_status()
        invite = invite_resp.json()
        assert invite["token"] and invite["link"].endswith(invite["token"])
        print("PASS: admin creates an invite and gets back a token + shareable link")

        preview_resp = await client.get("/invites/preview", params={"token": invite["token"]})
        preview_resp.raise_for_status()
        preview = preview_resp.json()
        assert preview["org_name"] == org_a_name
        assert preview["email"] == invitee_email
        assert preview["role"] == "member"
        print("PASS: GET /invites/preview (public, pre-auth) shows the right org/email/role")

        # --- tampered token is rejected, not silently resolved -------------
        tampered = invite["token"][:-1] + ("x" if invite["token"][-1] != "x" else "y")
        tampered_resp = await client.get("/invites/preview", params={"token": tampered})
        assert tampered_resp.status_code == 400, f"expected 400, got {tampered_resp.status_code}"
        tampered_signup = await signup(client, invitee_email, invite_token=tampered)
        assert tampered_signup.status_code == 400, f"expected 400, got {tampered_signup.status_code}"
        print("PASS: a tampered/mangled token is rejected outright, not treated as valid")

        # --- redeeming the real token joins org A, never any other org -----
        # There is no client-suppliable "which org" field on this path at
        # all — the token is the only thing that determines it. Asserting
        # the result really landed in org_a_id (not org_b_id, not a new
        # org) is the actual proof that org A's token can't be leveraged
        # to join org B under any circumstance.
        join_resp = await signup(client, invitee_email, invite_token=invite["token"])
        join_resp.raise_for_status()
        token_member = join_resp.json()["access_token"]
        me_member = await me(client, token_member)
        assert me_member["org_id"] == org_a_id, "invite token joined the wrong org"
        assert me_member["org_id"] != org_b_id
        assert me_member["role"] == "member"
        print("PASS: redeeming org A's invite token joins org A with the invited role, never org B")

        # --- single-use: the same token can't be redeemed again ------------
        replay_resp = await signup(client, invitee_email, invite_token=invite["token"])
        assert replay_resp.status_code == 400, f"expected 400, got {replay_resp.status_code}"
        print("PASS: an already-accepted invite token can't be redeemed a second time")

        # --- email binding: token only redeemable by the invited email -----
        invite2_email = f"invitee2-{suffix}@example.dev"
        invite2_resp = await create_invite(client, token_a, invite2_email, role="viewer")
        invite2_resp.raise_for_status()
        invite2 = invite2_resp.json()
        wrong_email_resp = await signup(
            client, f"someone-else-{suffix}@example.dev", invite_token=invite2["token"]
        )
        assert wrong_email_resp.status_code == 400, f"expected 400, got {wrong_email_resp.status_code}"
        print("PASS: a real invite token rejects signup under a different email than it was issued for")

        # --- inviting an already-registered email is rejected up front -----
        dup_resp = await create_invite(client, token_a, invitee_email, role="member")
        assert dup_resp.status_code == 409, f"expected 409, got {dup_resp.status_code}"
        print("PASS: inviting an email that's already a registered user is rejected (409)")

        # --- inviting the same email twice while a pending invite already ---
        # exists is rejected too, not silently allowed to pile up -- a real
        # bug caught in actual use: nothing stopped this before, so the
        # same email could accumulate any number of simultaneously-valid,
        # independently-redeemable invite tokens with no indication in the
        # list view beyond eyeballing which rows share an email.
        pending_dup_email = f"pending-dup-{suffix}@example.dev"
        first_pending = await create_invite(client, token_a, pending_dup_email, role="member")
        first_pending.raise_for_status()
        second_pending = await create_invite(client, token_a, pending_dup_email, role="admin")
        assert second_pending.status_code == 409, f"expected 409, got {second_pending.status_code}"
        print("PASS: inviting an email with an already-pending invite is rejected (409), not duplicated")

        # --- non-admin cannot create invites or change roles ----------------
        member_invite_email = f"member-b-{suffix}@example.dev"
        member_invite_resp = await create_invite(client, token_b, member_invite_email, role="member")
        member_invite_resp.raise_for_status()
        member_join_resp = await signup(
            client, member_invite_email, invite_token=member_invite_resp.json()["token"]
        )
        member_join_resp.raise_for_status()
        token_member_b = member_join_resp.json()["access_token"]
        refresh_member_b = member_join_resp.json()["refresh_token"]
        me_member_b = await me(client, token_member_b)
        assert me_member_b["role"] == "member"

        forbidden_invite = await create_invite(client, token_member_b, f"x-{suffix}@example.dev")
        assert forbidden_invite.status_code == 403, f"expected 403, got {forbidden_invite.status_code}"

        admin_b_id = me_b["id"]
        forbidden_role_change = await client.patch(
            f"/org/members/{admin_b_id}",
            json={"role": "member"},
            headers=auth_headers(token_member_b),
        )
        assert forbidden_role_change.status_code == 403, (
            f"expected 403, got {forbidden_role_change.status_code}"
        )
        print("PASS: a non-admin member gets 403 creating invites and 403 changing roles")

        # --- last-admin-removal guard ----------------------------------------
        # org B currently has admin_b (admin) + member_b (member) — admin_b
        # is still the ONLY admin, so both removing and demoting them must
        # be blocked, even though org B has more than one member overall.
        blocked_delete = await client.delete(
            f"/org/members/{admin_b_id}", headers=auth_headers(token_b)
        )
        assert blocked_delete.status_code == 409, f"expected 409, got {blocked_delete.status_code}"

        blocked_demote = await client.patch(
            f"/org/members/{admin_b_id}", json={"role": "member"}, headers=auth_headers(token_b)
        )
        assert blocked_demote.status_code == 409, f"expected 409, got {blocked_demote.status_code}"
        print("PASS: removing or demoting the last remaining admin is blocked (409) both ways")

        # promoting member_b to admin first, THEN removing admin_b (now not
        # the last admin) should succeed — proves the guard is specifically
        # about being the *last* admin, not admin removal in general.
        member_b_id = me_member_b["id"]
        promote_resp = await client.patch(
            f"/org/members/{member_b_id}", json={"role": "admin"}, headers=auth_headers(token_b)
        )
        promote_resp.raise_for_status()

        # --- a role change takes effect on the NEXT refresh, not just re-login
        # member_b's refresh token was issued at signup, before this
        # promotion, with role="member" baked into its Redis payload. If
        # /auth/refresh trusted that cached value, this would still come
        # back "member" — it has to re-check the DB to see "admin" here.
        refreshed_after_promotion = await client.post(
            "/auth/refresh", json={"refresh_token": refresh_member_b}
        )
        refreshed_after_promotion.raise_for_status()
        me_after_promotion = await me(client, refreshed_after_promotion.json()["access_token"])
        assert me_after_promotion["role"] == "admin", (
            "refresh should reflect the freshly-promoted role, not a stale cached one"
        )
        print("PASS: refreshing with a pre-promotion token reflects the new role, not the stale one")

        now_allowed_delete = await client.delete(
            f"/org/members/{admin_b_id}", headers=auth_headers(token_b)
        )
        assert now_allowed_delete.status_code == 204, f"expected 204, got {now_allowed_delete.status_code}"
        print("PASS: once a second admin exists, removing the original admin succeeds")

        # --- a removed member's outstanding refresh token stops working ------
        # admin_b's original refresh token (from signup, never used since)
        # should have been revoked as part of the DELETE above — not just
        # left to expire naturally over the next 7 days.
        revoked_refresh = await client.post("/auth/refresh", json={"refresh_token": refresh_admin_b})
        assert revoked_refresh.status_code == 401, f"expected 401, got {revoked_refresh.status_code}"
        print("PASS: a removed member's outstanding refresh token is revoked, not left valid")

        # --- cross-org isolation on invites ----------------------------------
        cross_delete = await client.delete(
            f"/invites/{invite2['id']}", headers=auth_headers(token_b)
        )
        assert cross_delete.status_code == 404, f"expected 404, got {cross_delete.status_code}"
        print("PASS: org B directly DELETEing org A's invite id -> 404")

        # --- revoke works, and the revoked token stops resolving -------------
        revoke_resp = await client.delete(f"/invites/{invite2['id']}", headers=auth_headers(token_a))
        assert revoke_resp.status_code == 204, f"expected 204, got {revoke_resp.status_code}"
        after_revoke = await client.get("/invites/preview", params={"token": invite2["token"]})
        assert after_revoke.status_code == 400, f"expected 400, got {after_revoke.status_code}"
        print("PASS: revoking a pending invite makes its token stop resolving")

        # --- GET /org/members lists only your own org -------------------------
        members_a = await client.get("/org/members", headers=auth_headers(token_a))
        members_a.raise_for_status()
        emails_a = {m["email"] for m in members_a.json()}
        assert emails_a == {f"admin-a-{suffix}@example.dev", invitee_email}
        print("PASS: GET /org/members only lists org A's own members")

        # --- regenerate: old token dies, new one works, same shape as ---------
        # password-reset's resend-invalidates-old-token behavior.
        regen_email = f"regen-{suffix}@example.dev"
        original = await create_invite(client, token_a, regen_email, role="member")
        original.raise_for_status()
        original = original.json()

        regen_resp = await client.post(
            f"/invites/{original['id']}/regenerate", headers=auth_headers(token_a)
        )
        regen_resp.raise_for_status()
        regenerated = regen_resp.json()
        assert regenerated["id"] != original["id"], "regenerate should replace the row, not edit it in place"
        assert regenerated["token"] != original["token"]
        assert regenerated["email"] == regen_email and regenerated["role"] == "member"
        print("PASS: regenerating an invite returns a fresh id + token, same email/role")

        old_token_preview = await client.get(
            "/invites/preview", params={"token": original["token"]}
        )
        assert old_token_preview.status_code == 400, (
            f"expected the pre-regenerate token to stop resolving, got {old_token_preview.status_code}"
        )
        print("PASS: the original link stops working the instant regeneration succeeds")

        new_token_preview = await client.get(
            "/invites/preview", params={"token": regenerated["token"]}
        )
        new_token_preview.raise_for_status()
        assert new_token_preview.json()["email"] == regen_email
        regen_join = await signup(client, regen_email, invite_token=regenerated["token"])
        regen_join.raise_for_status()
        assert (await me(client, regen_join.json()["access_token"]))["org_id"] == org_a_id
        print("PASS: the regenerated link resolves and is genuinely redeemable end to end")

        # --- cross-org: org B cannot regenerate org A's invite ----------------
        cross_org_invite = await create_invite(client, token_a, f"cross-regen-{suffix}@example.dev")
        cross_org_invite.raise_for_status()
        cross_org_regen = await client.post(
            f"/invites/{cross_org_invite.json()['id']}/regenerate", headers=auth_headers(token_b)
        )
        assert cross_org_regen.status_code == 404, f"expected 404, got {cross_org_regen.status_code}"
        print("PASS: org B directly regenerating org A's invite id -> 404")

        # --- regenerating an already-accepted invite is rejected --------------
        accepted_regen = await client.post(
            f"/invites/{original['id']}/regenerate", headers=auth_headers(token_a)
        )
        # original['id'] no longer exists at all (deleted by its own
        # regeneration above) -- 404, the same outcome as "doesn't exist,"
        # is correct here too. The distinct "already accepted" 400 is
        # covered by regenerating the invite regen_join just redeemed.
        assert accepted_regen.status_code == 404, f"expected 404, got {accepted_regen.status_code}"
        already_accepted_regen = await client.post(
            f"/invites/{regenerated['id']}/regenerate", headers=auth_headers(token_a)
        )
        assert already_accepted_regen.status_code == 400, (
            f"expected 400, got {already_accepted_regen.status_code}"
        )
        print("PASS: regenerating an already-accepted invite is rejected (400), not silently reissued")

    print("\nAll invite + member-management checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
