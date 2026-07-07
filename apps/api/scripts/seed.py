"""Throwaway dev data. Not part of the app — run manually:

    docker compose exec api python -m scripts.seed
"""

import asyncio

from app.db.models import Organization, User, UserRole
from app.db.session import AsyncSessionLocal, set_tenant_context

ORGS = [
    {"name": "Acme Corp", "user_email": "alice@acme.test"},
    {"name": "Globex Inc", "user_email": "bob@globex.test"},
]


async def main() -> None:
    for org_spec in ORGS:
        async with AsyncSessionLocal() as session:
            org = Organization(name=org_spec["name"])
            session.add(org)
            await session.flush()  # runs the INSERT so org.id is populated

            # organizations has no RLS, but users does — the WITH CHECK
            # policy requires tenant context to match the row being inserted.
            await set_tenant_context(session, org.id)

            user = User(
                org_id=org.id,
                email=org_spec["user_email"],
                hashed_password="not-a-real-hash-yet",  # auth slice replaces this
                role=UserRole.ADMIN,
            )
            session.add(user)
            await session.commit()
            print(f"seeded org={org.name!r} id={org.id} user={user.email!r}")


if __name__ == "__main__":
    asyncio.run(main())
