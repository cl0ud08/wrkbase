import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings
from app.core.security import decode_access_token


def _decode_user_id(request: Request) -> str | None:
    token: str | None = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[len("bearer ") :]
    if token is None:
        token = request.cookies.get("access_token")
    if token is None:
        return None
    try:
        return decode_access_token(token).get("sub")
    except jwt.PyJWTError:
        return None


def key_by_user_or_ip(request: Request) -> str:
    # Per-user budget when a valid access token identifies one, so several
    # people behind the same office NAT/VPN don't share (and exhaust) one
    # IP-wide allowance. Falls back to IP for anonymous requests — which is
    # also exactly the right key for signup/login, before any user exists.
    user_id = _decode_user_id(request)
    return f"user:{user_id}" if user_id else f"ip:{get_remote_address(request)}"


# Redis-backed, not in-memory: slowapi's default (in-memory) storage counts
# requests in a plain dict inside one process. The moment the API runs as
# more than one worker/instance (any real deployment, and even a local
# `--workers 2`), each process has its own separate counter — a client could
# get a full fresh allowance from every worker, so the actual effective
# limit is (configured limit) x (worker count) instead of the configured
# limit. Redis gives every process a single shared counter, which is the
# whole point of a *rate* limit.
limiter = Limiter(
    key_func=key_by_user_or_ip,
    default_limits=["100/minute"],
    storage_uri=settings.redis_url,
)
