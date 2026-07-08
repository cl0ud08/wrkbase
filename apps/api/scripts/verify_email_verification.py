"""Proves email verification end to end over real HTTP: the invite-skips-
verification decision, the soft-nudge (not a gate) decision — an
unverified user can do everything a verified one can — reuse/expiry
rejection, and that a resend genuinely invalidates the prior token rather
than letting two live ones coexist. Same one deliberate exception to the
real-HTTP-only convention as verify_password_reset.py: the expiry check
backdates a token directly in Postgres, since email_verification_tokens
has no RLS (see app/db/models.py) and no HTTP call can fast-forward a
wall clock. Run:

    docker compose exec api python -m scripts.verify_email_verification
"""

import asyncio
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update

from app.db.models import EmailVerificationToken
from app.db.session import AsyncSessionLocal

BASE_URL = "http://localhost:8000"
PASSWORD = "correct horse battery staple"


def auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def extract_token(link: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["token"][0]


async def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        # --- self-serve signup: unverified, gets a real verification link
        email = f"verify-{suffix}@example.dev"
        signup_resp = await client.post(
            "/auth/signup",
            json={"org_name": f"Verify Org {suffix}", "email": email, "password": PASSWORD},
        )
        signup_resp.raise_for_status()
        signup_body = signup_resp.json()
        access_token = signup_body["access_token"]
        assert signup_body["verification_link"], "self-serve signup should return a verification link"
        print("PASS: self-serve signup returns a real verification link")

        me_before = await client.get("/auth/me", headers=auth_headers(access_token))
        me_before.raise_for_status()
        assert me_before.json()["is_verified"] is False
        print("PASS: is_verified starts false for a self-serve signup")

        # --- soft-nudge, not a gate: an unverified user can do everything
        project_resp = await client.post(
            "/projects",
            json={"name": "Unverified user's project", "description": None},
            headers=auth_headers(access_token),
        )
        assert project_resp.status_code == 201, (
            f"expected an unverified user to create a project fine, got {project_resp.status_code}"
        )
        project_id = project_resp.json()["id"]
        ticket_resp = await client.post(
            f"/projects/{project_id}/tickets",
            json={"type": "task", "title": "Unverified user's ticket", "description": None},
            headers=auth_headers(access_token),
        )
        assert ticket_resp.status_code == 201, (
            f"expected an unverified user to create a ticket fine, got {ticket_resp.status_code}"
        )
        print("PASS: an unverified user can create projects and tickets — verification is a nudge, not a gate")

        # --- invite path: auto-verified, no token generated at all
        invite_resp = await client.post(
            "/invites",
            json={"email": f"invitee-{suffix}@example.dev", "role": "member"},
            headers=auth_headers(access_token),
        )
        invite_resp.raise_for_status()
        invite_token = invite_resp.json()["token"]

        invited_signup = await client.post(
            "/auth/signup",
            json={
                "invite_token": invite_token,
                "email": f"invitee-{suffix}@example.dev",
                "password": PASSWORD,
            },
        )
        invited_signup.raise_for_status()
        invited_body = invited_signup.json()
        assert invited_body["verification_link"] is None, (
            "an invited signup should not generate a verification token at all"
        )
        invited_me = await client.get("/auth/me", headers=auth_headers(invited_body["access_token"]))
        invited_me.raise_for_status()
        assert invited_me.json()["is_verified"] is True
        print("PASS: joining via a valid invite is auto-verified — the admin's invite is the trust signal")

        # --- verify with the real token
        real_token = extract_token(signup_body["verification_link"])
        verify_resp = await client.post("/auth/verify-email", json={"token": real_token})
        assert verify_resp.status_code == 200, f"expected 200, got {verify_resp.status_code}"

        me_after = await client.get("/auth/me", headers=auth_headers(access_token))
        me_after.raise_for_status()
        assert me_after.json()["is_verified"] is True
        print("PASS: verifying with a real token flips is_verified to true")

        reuse_resp = await client.post("/auth/verify-email", json={"token": real_token})
        assert reuse_resp.status_code == 400, f"expected 400, got {reuse_resp.status_code}"
        print("PASS: a consumed verification token is rejected on reuse (400)")

        # --- resend: enumeration-safe, and already-verified looks identical too
        real_resend = await client.post("/auth/resend-verification", json={"email": email})
        real_resend.raise_for_status()
        real_resend_body = real_resend.json()

        fake_resend = await client.post(
            "/auth/resend-verification", json={"email": f"nobody-{suffix}@example.dev"}
        )
        fake_resend.raise_for_status()
        fake_resend_body = fake_resend.json()

        assert set(real_resend_body.keys()) == set(fake_resend_body.keys())
        assert real_resend_body["message"] == fake_resend_body["message"]
        print("PASS: resend for an already-verified email looks identical to resend for a nonexistent one")

        fake_resend_token = extract_token(fake_resend_body["verification_link"])
        fake_verify = await client.post("/auth/verify-email", json={"token": fake_resend_token})
        assert fake_verify.status_code == 400, f"expected 400, got {fake_verify.status_code}"
        print("PASS: the link from a resend for a nonexistent email doesn't actually work")

        # --- resend invalidates the prior token, not just adds a new one
        email2 = f"resend-{suffix}@example.dev"
        signup2 = await client.post(
            "/auth/signup",
            json={"org_name": f"Resend Org {suffix}", "email": email2, "password": PASSWORD},
        )
        signup2.raise_for_status()
        first_link = signup2.json()["verification_link"]
        first_token = extract_token(first_link)

        resend2 = await client.post("/auth/resend-verification", json={"email": email2})
        resend2.raise_for_status()
        second_token = extract_token(resend2.json()["verification_link"])
        assert first_token != second_token

        stale_verify = await client.post("/auth/verify-email", json={"token": first_token})
        assert stale_verify.status_code == 400, f"expected 400, got {stale_verify.status_code}"
        fresh_verify = await client.post("/auth/verify-email", json={"token": second_token})
        assert fresh_verify.status_code == 200, f"expected 200, got {fresh_verify.status_code}"
        print("PASS: resend invalidates the prior token immediately — at most one live link at a time, not two coexisting")

        # --- expiry: direct Postgres backdating, same deliberate exception
        # to the real-HTTP-only convention as verify_password_reset.py.
        email3 = f"expiring-{suffix}@example.dev"
        signup3 = await client.post(
            "/auth/signup",
            json={"org_name": f"Expiring Org {suffix}", "email": email3, "password": PASSWORD},
        )
        signup3.raise_for_status()
        expiring_token = extract_token(signup3.json()["verification_link"])

        async with AsyncSessionLocal() as session:
            await session.execute(
                update(EmailVerificationToken)
                .where(EmailVerificationToken.token == expiring_token)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await session.commit()

        expired_verify = await client.post("/auth/verify-email", json={"token": expiring_token})
        assert expired_verify.status_code == 400, f"expected 400, got {expired_verify.status_code}"
        print("PASS: an expired verification token is rejected (400)")

    print("\nAll email-verification checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
