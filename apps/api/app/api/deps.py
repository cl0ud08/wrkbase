import uuid

import jwt
from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db, set_tenant_context


class AuthContext(BaseModel):
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str


_UNAUTHENTICATED = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def get_current_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    # Two callers, two delivery mechanisms: the browser frontend relies on
    # the httpOnly access_token cookie (it never sees the raw token), while
    # Bearer-token API clients (scripts, future mobile/service clients) send
    # it in the Authorization header. Header takes priority when both are
    # present since a deliberately-set header signals an explicit API caller.
    token: str | None = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[len("bearer ") :]
    if token is None:
        token = request.cookies.get("access_token")
    if token is None:
        raise _UNAUTHENTICATED

    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token"
        )

    auth = AuthContext(user_id=payload["sub"], org_id=payload["org_id"], role=payload["role"])

    # FastAPI caches a dependency's result per request by the callable's
    # identity, so this `get_db` call and any route's own `Depends(get_db)`
    # resolve to the exact same AsyncSession/connection for the request —
    # the tenant context set here is visible to every query the route makes.
    await set_tenant_context(db, auth.org_id)
    return auth
