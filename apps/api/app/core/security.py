import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

# Argon2id over bcrypt: it's the current OWASP-recommended default and won
# the Password Hashing Competition specifically for being memory-hard, which
# makes GPU/ASIC-accelerated cracking meaningfully more expensive than it is
# against bcrypt. bcrypt is still fine and more universally supported, but
# has no memory-hardness knob and silently truncates input past 72 bytes.
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return _password_hasher.verify(hashed_password, password)
    except VerifyMismatchError:
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
