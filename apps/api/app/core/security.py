import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

# Argon2id over bcrypt: it's the current OWASP-recommended default and won
# the Password Hashing Competition specifically for being memory-hard, which
# makes GPU/ASIC-accelerated cracking meaningfully more expensive than it is
# against bcrypt. bcrypt is still fine and more universally supported, but
# has no memory-hardness knob and silently truncates input past 72 bytes.
_password_hasher = PasswordHasher()


# Both async, wrapping the sync argon2-cffi call in run_in_threadpool — not
# just at the call site, but here, so the fix can't be forgotten by some
# future caller. Argon2 is deliberately slow and memory-hard (that's the
# whole point of it as a password hash); called directly inside an async
# route handler, that CPU-bound work runs ON the event loop and blocks every
# other concurrent request being served by that worker for the duration —
# observed firsthand as /health hanging for 10+ seconds under load during
# earlier testing. run_in_threadpool moves the call to a worker thread so
# the event loop stays free to keep serving other requests while it runs.
async def hash_password(password: str) -> str:
    return await run_in_threadpool(_password_hasher.hash, password)


async def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return await run_in_threadpool(_password_hasher.verify, hashed_password, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        # VerifyMismatchError: right format, wrong password — the expected
        # everyday case. VerificationError/InvalidHashError: the stored hash
        # itself is malformed or undecodable (e.g. corrupted data, a
        # future re-encoding bug) — treating that as "verification failed"
        # rather than letting it propagate as an unhandled 500 is the same
        # default-deny instinct as RLS: an ambiguous auth state should
        # never resolve to "let them in."
        return False


def create_access_token(*, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def generate_refresh_token() -> str:
    # Opaque random string, not a JWT: it carries no claims of its own,
    # it's just a lookup key into the server-side store (see
    # app/services/refresh_tokens.py), which is what makes it revocable.
    return secrets.token_urlsafe(32)
