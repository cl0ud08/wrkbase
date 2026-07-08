"""Throwaway dev data. Not part of the app — run manually:

    docker compose exec api python -m scripts.seed
"""

import asyncio

from app.core.security import hash_password
from app.core.ticket_prefix import derive_ticket_prefix
from app.db.models import Organization, User, UserRole
from app.db.session import AsyncSessionLocal, set_tenant_context

SEED_PASSWORD = "wrkbase-dev-password"

ORGS = [
    {"name": "Acme Corp", "user_email": "alice@acme.dev"},
    {"name": "Globex Inc", "user_email": "bob@globex.dev"},
]


async def main() -> None:
    for org_spec in ORGS:
        async with AsyncSessionLocal() as session:
            org = Organization(
                name=org_spec["name"], ticket_prefix=derive_ticket_prefix(org_spec["name"])
            )
            session.add(org)
            await session.flush()  # runs the INSERT so org.id is populated

            # organizations has no RLS, but users does — the WITH CHECK
            # policy requires tenant context to match the row being inserted.
            await set_tenant_context(session, org.id)

            user = User(
                org_id=org.id,
                email=org_spec["user_email"],
                hashed_password=await hash_password(SEED_PASSWORD),
                role=UserRole.ADMIN,
            )
            session.add(user)
            await session.commit()
            print(f"seeded org={org.name!r} id={org.id} user={user.email!r}")


if __name__ == "__main__":
    asyncio.run(main())
