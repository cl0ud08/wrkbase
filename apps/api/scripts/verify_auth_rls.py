"""Proves auth + RLS are wired together end to end, not just each correct in
isolation. Hits the running API over real HTTP (not internal function
calls), inside the api container's own network namespace. Run:

    docker compose exec api python -m scripts.verify_auth_rls
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


async def me(client: httpx.AsyncClient, access_token: str) -> dict:
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    resp.raise_for_status()
    return resp.json()


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        email_a = f"user-a-{suffix}@example.dev"
        email_b = f"user-b-{suffix}@example.dev"

        tokens_a = await signup(client, f"Org A {suffix}", email_a)
        tokens_b = await signup(client, f"Org B {suffix}", email_b)

        me_a = await me(client, tokens_a["access_token"])
        me_b = await me(client, tokens_b["access_token"])

        assert me_a["org_id"] != me_b["org_id"], "two brand-new orgs somehow share an org_id"
        assert me_a["org_user_count"] == 1, f"org A should see only its own user, saw {me_a['org_user_count']}"
        assert me_b["org_user_count"] == 1, f"org B should see only its own user, saw {me_b['org_user_count']}"
        print("PASS: two orgs signed up; /auth/me for each sees only its own org's user count")

        # --- refresh rotation ---------------------------------------------
        refresh_resp = await client.post("/auth/refresh", json={"refresh_token": tokens_a["refresh_token"]})
        refresh_resp.raise_for_status()
        rotated_a = refresh_resp.json()
        assert rotated_a["refresh_token"] != tokens_a["refresh_token"], "refresh did not rotate the token"

        reuse_resp = await client.post("/auth/refresh", json={"refresh_token": tokens_a["refresh_token"]})
        assert reuse_resp.status_code == 401, "reusing a rotated-out refresh token should be rejected"
        print("PASS: refresh token rotates on use; the old one is rejected on reuse")

        # --- logout revokes server-side -----------------------------------
        logout_resp = await client.post("/auth/logout", json={"refresh_token": rotated_a["refresh_token"]})
        assert logout_resp.status_code == 204
        after_logout_resp = await client.post(
            "/auth/refresh", json={"refresh_token": rotated_a["refresh_token"]}
        )
        assert after_logout_resp.status_code == 401, "refresh token should be dead after logout"
        print("PASS: logout revokes the refresh token server-side")

        # --- login resolves the right org via user_lookup ------------------
        login_resp = await client.post("/auth/login", json={"email": email_a, "password": PASSWORD})
        login_resp.raise_for_status()
        login_tokens = login_resp.json()
        me_after_login = await me(client, login_tokens["access_token"])
        assert me_after_login["org_id"] == me_a["org_id"], "login resolved to the wrong org"
        assert me_after_login["org_user_count"] == 1
        print("PASS: login (via user_lookup -> users) resolves to the correct org and user")

        # --- wrong password is rejected without leaking which field was wrong
        bad_login_resp = await client.post("/auth/login", json={"email": email_a, "password": "wrong"})
        assert bad_login_resp.status_code == 401
        print("PASS: wrong password rejected")

    print("\nAll auth + RLS integration checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
