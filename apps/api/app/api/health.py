from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.session import get_db

router = APIRouter()


@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "wrkbase-api"}


@router.get("/health/db")
async def health_check_db(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    # No tenant context is set on this session. If RLS is wired correctly,
    # this returns 0 regardless of how many users actually exist across all
    # orgs — proving connectivity AND default-deny through the real ORM
    # stack, not just a raw psql session.
    result = await db.execute(select(func.count()).select_from(User))
    count = result.scalar_one()
    return {
        "status": "ok",
        "database": "connected",
        "users_visible_without_tenant_context": count,
    }
