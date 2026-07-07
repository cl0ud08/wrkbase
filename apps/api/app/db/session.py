import uuid
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def set_tenant_context(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Scope every RLS-governed query on this session's current transaction to one org.

    Uses set_config(..., is_local=true) instead of a literal `SET LOCAL`
    statement for two reasons: (1) it accepts a bound parameter, so the org_id
    never gets string-interpolated into SQL, and (2) `is_local=true` mirrors
    `SET LOCAL` — the setting is discarded when the transaction ends, so a
    pooled connection can never carry one request's tenant context into the
    next request that happens to reuse it.
    """
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id)},
    )
