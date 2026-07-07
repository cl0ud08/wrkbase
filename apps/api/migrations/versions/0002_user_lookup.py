"""user_lookup table + trigger to keep it synced with users

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No RLS on this table, deliberately: its only job is answering "which
    # org does this email belong to" before any tenant context can exist —
    # a global, unrestricted table is what makes that first login lookup
    # possible at all. It carries no password hash or other sensitive data.
    op.create_table(
        "user_lookup",
        sa.Column("email", sa.String, primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
    )
    # user_lookup is created after migration 0001's ALTER DEFAULT PRIVILEGES,
    # so wrkbase_app already has SELECT/INSERT/UPDATE/DELETE on it with no
    # extra GRANT needed here.

    # Enforced at the DB level, not in application code: any INSERT into
    # `users`, from any code path (the signup endpoint, the seed script,
    # future admin tooling), keeps user_lookup in sync automatically. A
    # trigger fires inside the same transaction as the triggering statement,
    # so this can't drift the way "the app remembered to do two inserts"
    # could — the same reasoning that put RLS in the database instead of an
    # app-layer WHERE clause.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_user_lookup() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO user_lookup (email, org_id, user_id)
                VALUES (NEW.email, NEW.org_id, NEW.id);
            ELSIF TG_OP = 'UPDATE' THEN
                UPDATE user_lookup
                SET email = NEW.email, org_id = NEW.org_id
                WHERE user_id = NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER users_sync_lookup
        AFTER INSERT OR UPDATE OF email, org_id ON users
        FOR EACH ROW EXECUTE FUNCTION sync_user_lookup();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_sync_lookup ON users")
    op.execute("DROP FUNCTION IF EXISTS sync_user_lookup()")
    op.drop_table("user_lookup")
