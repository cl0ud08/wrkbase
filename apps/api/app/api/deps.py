import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db, set_tenant_context

_bearer_scheme = HTTPBearer()


class AuthContext(BaseModel):
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str


async def get_current_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    try:
        payload = decode_access_token(credentials.credentials)
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
