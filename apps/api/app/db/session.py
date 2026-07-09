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


async def set_actor_context(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Same shape and reasoning as set_tenant_context, for a second, narrower
    axis: which specific user is making this request, not just which org.

    Exists for notifications (migration 0016) — the one table in this app
    where a user's own row-level privacy, not just their org's, is a real
    trust boundary: nothing here has a legitimate reason for one user to
    read another's notifications, so that check belongs in RLS, not an
    app-layer WHERE clause someone could forget. Called from
    get_current_auth alongside set_tenant_context, not folded into it: the
    two are set together for every real authenticated HTTP request, but
    several call sites that need org scoping (self-serve signup, seed.py,
    invite redemption) run before any specific "acting user" exists at
    all, and notification *creation* deliberately runs as a different
    user than the recipient (see app/services/notifications.py) — folding
    this into set_tenant_context would force a user_id onto call sites
    that have no coherent one to give it.
    """
    await session.execute(
        text("SELECT set_config('app.current_user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
