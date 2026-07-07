"""Proves the tenant_isolation RLS policy on `users` actually holds. Not part
of the app, and not pytest — deliberately raw asserts so a failure stops the
script immediately at the exact check that broke. Run after scripts.seed:

    docker compose exec api python -m scripts.verify_rls
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.models import Organization, User
from app.db.session import AsyncSessionLocal, set_tenant_context


async def get_org_id(session, name: str) -> uuid.UUID:
    result = await session.execute(select(Organization.id).where(Organization.name == name))
    org_id = result.scalar_one_or_none()
    if org_id is None:
        raise RuntimeError(f"org {name!r} not found — run `python -m scripts.seed` first")
    return org_id


async def main() -> None:
    async with AsyncSessionLocal() as session:
        org_a_id = await get_org_id(session, "Acme Corp")
        org_b_id = await get_org_id(session, "Globex Inc")

    # 1. No tenant context set at all. This is the case that matters most:
    #    a bug that forgets to set context should fail closed (see nothing),
    #    not fail open (see every tenant's rows).
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        rows = result.scalars().all()
        assert len(rows) == 0, f"expected 0 rows with no context set, got {len(rows)}"
        print("PASS: no context set -> 0 rows visible (default-deny)")

    # 2. Context set to org A -> sees only its own row.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_a_id)
        result = await session.execute(select(User))
        rows = result.scalars().all()
        assert len(rows) == 1 and rows[0].org_id == org_a_id, "org A context leaked or hid its own row"
        print(f"PASS: org A context -> sees only its own user ({rows[0].email})")

    # 3. Context set to org B -> sees only its own row, never org A's.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_b_id)
        result = await session.execute(select(User))
        rows = result.scalars().all()
        assert len(rows) == 1 and rows[0].org_id == org_b_id, "org B context leaked or hid its own row"
        print(f"PASS: org B context -> sees only its own user ({rows[0].email})")

    # 4. Context set to an org id that doesn't exist -> still 0 rows, not an
    #    error and not a full-table leak. Confirms the policy compares
    #    values, it doesn't just check "is something set".
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, uuid.uuid4())
        result = await session.execute(select(User))
        rows = result.scalars().all()
        assert len(rows) == 0, f"expected 0 rows for an unknown org id, got {len(rows)}"
        print("PASS: unknown org id -> 0 rows (no accidental full-table leak)")

    # 5. Cross-tenant write: context says org A, but the row being inserted
    #    claims org_id = org B. WITH CHECK must reject this, proving RLS
    #    guards writes too, not just SELECT.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_a_id)
        session.add(
            User(org_id=org_b_id, email="attacker@evil.test", hashed_password="x")
        )
        try:
            await session.commit()
        except DBAPIError:
            await session.rollback()
            print("PASS: cross-tenant insert (context=A, org_id=B) rejected by WITH CHECK")
        else:
            raise AssertionError("cross-tenant insert should have been rejected but succeeded")

    print("\nAll RLS checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
