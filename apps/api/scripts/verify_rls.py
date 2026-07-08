"""Proves the tenant_isolation RLS policy on `users`, and the id-keyed RLS
policies on `organizations` (migration 0015), actually hold. Not part of the
app, and not pytest — deliberately raw asserts so a failure stops the script
immediately at the exact check that broke. Run after scripts.seed:

    docker compose exec api python -m scripts.verify_rls
"""

import asyncio
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.models import Organization, User, UserLookup
from app.db.session import AsyncSessionLocal, set_tenant_context


async def get_org_id(session, email: str) -> uuid.UUID:
    # Resolved via user_lookup (no RLS), not a direct `organizations`
    # query by name: organizations now has its own RLS (migration 0015),
    # so an unscoped SELECT against it returns nothing without tenant
    # context already set — exactly the chicken-and-egg problem
    # user_lookup exists to solve for login. The seeded user's email is a
    # fixed, known anchor for exactly the same reason it is there.
    result = await session.execute(select(UserLookup.org_id).where(UserLookup.email == email))
    org_id = result.scalar_one_or_none()
    if org_id is None:
        raise RuntimeError(f"no org found for seeded user {email!r} — run `python -m scripts.seed` first")
    return org_id


async def main() -> None:
    async with AsyncSessionLocal() as session:
        org_a_id = await get_org_id(session, "alice@acme.dev")
        org_b_id = await get_org_id(session, "bob@globex.dev")

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

    # 6. organizations is the tenant root, not a tenant-scoped resource —
    #    it has no org_id column, so its policy shape is id = current
    #    tenant context, not org_id = current tenant context. Checked
    #    here rather than a separate verify_organizations_rls.py: this is
    #    core RLS mechanics for the foundational table Phase 0 deferred,
    #    the same category as the `users` checks above, not a
    #    resource-specific concern the way projects/tickets/sprints are.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_a_id)
        result = await session.execute(select(Organization).where(Organization.id == org_a_id))
        org = result.scalar_one_or_none()
        assert org is not None and org.id == org_a_id, "org A context should see its own organization row"
        print("PASS: org A context -> can read its own organization row")

    # 7. Cross-org read: org A's context, org B's real id -> 0 rows, not
    #    an error and not org B's data. Same shape as check 4 above, just
    #    for the table that IS the tenant instead of one scoped to it.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_a_id)
        result = await session.execute(select(Organization).where(Organization.id == org_b_id))
        org = result.scalar_one_or_none()
        assert org is None, "org A context should not be able to read org B's organization row"
        print("PASS: org A context -> reading org B's organization row by id returns 0 rows, not an error")

    # 8. INSERT stays permissive with no tenant context set at all — the
    #    exact case self-serve signup and scripts.seed both rely on: a
    #    brand-new org doesn't have a tenant context yet because it
    #    doesn't exist yet. A restrictive WITH CHECK here would break
    #    every self-serve signup.
    #
    #    Uses genuinely raw SQL, not the SQLAlchemy insert() construct at
    #    all — SQLAlchemy's postgres dialect appends an implicit
    #    RETURNING <primary key> to *any* Core insert by default, ORM or
    #    not, and Postgres gates RETURNING on the SELECT policy too, which
    #    would fail here with no context set even though the INSERT
    #    itself is permitted. That's a real interaction app/api/auth.py's
    #    signup() and scripts.seed both had to account for (see their
    #    comments); this check specifically wants to prove just the
    #    INSERT policy, without that RETURNING complication.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("INSERT INTO organizations (name, ticket_prefix) VALUES (:name, :prefix)"),
            {"name": f"Verify RLS Temp {uuid.uuid4().hex[:8]}", "prefix": "VRT"},
        )
        await session.commit()
        print("PASS: creating a new organization with no tenant context set still succeeds (INSERT stays permissive)")

    # 9. UPDATE is scoped like SELECT, not left permissive like INSERT —
    #    org A's context cannot bump org B's next_ticket_number counter,
    #    the real, frequent UPDATE path against this table (see
    #    _next_ticket_number in app/api/tickets.py). rowcount, not an
    #    exception, is how RLS expresses "no" for an UPDATE that matches
    #    zero visible rows.
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, org_a_id)
        result = await session.execute(
            update(Organization)
            .where(Organization.id == org_b_id)
            .values(next_ticket_number=Organization.next_ticket_number + 1)
        )
        await session.commit()
        assert result.rowcount == 0, f"expected 0 rows updated, got {result.rowcount}"
        print("PASS: org A context -> updating org B's organization row affects 0 rows, not an error")

    print("\nAll RLS checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
