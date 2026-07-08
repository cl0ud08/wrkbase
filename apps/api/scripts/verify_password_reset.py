"""Proves password reset end to end over real HTTP: the enumeration-safe
response shape, reuse/expiry rejection, and — the part with real security
consequences if it's wrong — that a password reset actually kills every
outstanding session, not just the password. The one exception to "real
HTTP only" is the expiry check: no HTTP call can fast-forward a wall
clock, so that one assertion connects directly to Postgres (password_reset_
tokens has no RLS — see app/db/models.py — so no tenant context is needed
to do it) to backdate a token's expires_at. Run:

    docker compose exec api python -m scripts.verify_password_reset
"""

import asyncio
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update

from app.db.models import PasswordResetToken
from app.db.session import AsyncSessionLocal

BASE_URL = "http://localhost:8000"
ORIGINAL_PASSWORD = "correct horse battery staple"


async def signup(client: httpx.AsyncClient, org_name: str, email: str) -> dict:
    resp = await client.post(
        "/auth/signup", json={"org_name": org_name, "email": email, "password": ORIGINAL_PASSWORD}
    )
    resp.raise_for_status()
    return resp.json()


def extract_token(reset_link: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(reset_link).query)["token"][0]


async def request_reset(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post("/auth/password-reset/request", json={"email": email})


async def confirm_reset(client: httpx.AsyncClient, token: str, new_password: str) -> httpx.Response:
    return await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
    )


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    email = f"reset-{suffix}@example.dev"
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        signup_tokens = await signup(client, f"Reset Org {suffix}", email)
        stolen_refresh_token = signup_tokens["refresh_token"]
        print("PASS: account created")

        # --- enumeration: identical response for a real vs nonexistent email
        real_resp = await request_reset(client, email)
        real_resp.raise_for_status()
        real_body = real_resp.json()

        fake_resp = await request_reset(client, f"nobody-{suffix}@example.dev")
        fake_resp.raise_for_status()
        fake_body = fake_resp.json()

        assert real_resp.status_code == fake_resp.status_code == 200
        assert set(real_body.keys()) == set(fake_body.keys())
        assert real_body["message"] == fake_body["message"]
        assert real_body["reset_link"] and fake_body["reset_link"], "both should include a link"
        assert real_body["reset_link"] != fake_body["reset_link"], "tokens themselves still differ"
        print("PASS: requesting a reset for a real vs. nonexistent email is observably identical (status, shape, message)")

        fake_token = extract_token(fake_body["reset_link"])
        fake_confirm = await confirm_reset(client, fake_token, "whatever-new-password")
        assert fake_confirm.status_code == 400, f"expected 400, got {fake_confirm.status_code}"
        print("PASS: the link generated for a nonexistent email doesn't actually work (never persisted)")

        # --- requesting twice: using the newer token invalidates the older one
        second_resp = await request_reset(client, email)
        second_resp.raise_for_status()
        older_token = extract_token(real_body["reset_link"])
        newer_token = extract_token(second_resp.json()["reset_link"])
        assert older_token != newer_token

        confirm_with_newer = await confirm_reset(client, newer_token, "second-new-password-456")
        assert confirm_with_newer.status_code == 200, f"expected 200, got {confirm_with_newer.status_code}"

        confirm_with_older = await confirm_reset(client, older_token, "should-not-apply")
        assert confirm_with_older.status_code == 400, f"expected 400, got {confirm_with_older.status_code}"
        print("PASS: a successful reset invalidates the user's other still-outstanding reset tokens too")

        # --- reuse: the token that just succeeded can't be used a second time
        reuse_resp = await confirm_reset(client, newer_token, "third-password-789")
        assert reuse_resp.status_code == 400, f"expected 400, got {reuse_resp.status_code}"
        print("PASS: a consumed reset token is rejected on reuse (400), not silently reapplied")

        # --- the actual point: every outstanding session dies, not just the password
        dead_session = await client.post("/auth/refresh", json={"refresh_token": stolen_refresh_token})
        assert dead_session.status_code == 401, f"expected 401, got {dead_session.status_code}"
        print("PASS: a refresh token issued before the reset is dead afterward — a stolen session doesn't survive a password reset")

        login_new = await client.post(
            "/auth/login", json={"email": email, "password": "second-new-password-456"}
        )
        assert login_new.status_code == 200, f"expected 200, got {login_new.status_code}"
        login_old = await client.post("/auth/login", json={"email": email, "password": ORIGINAL_PASSWORD})
        assert login_old.status_code == 401, f"expected 401, got {login_old.status_code}"
        print("PASS: login works with the new password and rejects the old one")

        # --- expiry: no HTTP call can fast-forward the clock, so this one
        # check goes straight to Postgres to backdate a real token's
        # expires_at — see module docstring for why this is the one
        # deliberate exception to this app's real-HTTP-only proof-script
        # convention.
        expiring_email = f"expiring-{suffix}@example.dev"
        await signup(client, f"Expiring Org {suffix}", expiring_email)
        expiring_resp = await request_reset(client, expiring_email)
        expiring_resp.raise_for_status()
        expiring_token = extract_token(expiring_resp.json()["reset_link"])

        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PasswordResetToken)
                .where(PasswordResetToken.token == expiring_token)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await session.commit()

        expired_confirm = await confirm_reset(client, expiring_token, "irrelevant-password")
        assert expired_confirm.status_code == 400, f"expected 400, got {expired_confirm.status_code}"
        print("PASS: an expired reset token is rejected (400), not accepted past its window")

    print("\nAll password-reset checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
