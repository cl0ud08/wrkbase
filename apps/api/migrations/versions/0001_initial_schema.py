"""initial schema: organizations, users, tenant-isolation RLS

Revision ID: 0001
Revises:
Create Date: 2026-07-06

"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE TYPE user_role AS ENUM ('admin', 'member', 'viewer')")

    op.create_table(
        "organizations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        # The column every RLS policy in this app keys off.
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String, nullable=False, unique=True),
        sa.Column("hashed_password", sa.String, nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "member", "viewer", name="user_role", create_type=False),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])

    # --- Least-privilege application role ------------------------------------
    # Postgres RLS is bypassed automatically for superusers, and for a table's
    # own owner unless that table is also FORCEd — and FORCE still never binds
    # superusers. This migration runs as the Postgres superuser (it needs
    # CREATE ROLE / CREATE POLICY / ALTER TABLE), but the running API must
    # connect as an ordinary, non-owning role or every policy below is a no-op.
    app_db_user = os.environ.get("APP_DB_USER", "wrkbase_app")
    app_db_password = os.environ["APP_DB_PASSWORD"]

    # CREATE ROLE is DDL: Postgres's extended query protocol only accepts
    # bind parameters for SELECT/INSERT/UPDATE/DELETE/VALUES, not for DO
    # blocks or CREATE ROLE, so it can't be parametrized directly. Instead,
    # check existence with a normal bound SELECT (that part *can* be
    # parametrized), and only fall back to a literal, manually-escaped
    # CREATE ROLE if it doesn't exist yet.
    role_exists = bind.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
        {"role_name": app_db_user},
    ).scalar_one_or_none()

    if role_exists is None:
        escaped_password = app_db_password.replace("'", "''")
        bind.execute(
            text(f'CREATE ROLE "{app_db_user}" WITH LOGIN PASSWORD \'{escaped_password}\'')
        )

    # GRANT/identifier names can't be bind params either; these come from our
    # own trusted env config and Postgres itself, not user input.
    current_db = bind.execute(text("SELECT current_database()")).scalar_one()
    bind.execute(text(f'GRANT CONNECT ON DATABASE "{current_db}" TO "{app_db_user}"'))
    bind.execute(text(f'GRANT USAGE ON SCHEMA public TO "{app_db_user}"'))
    bind.execute(
        text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON organizations, users TO "{app_db_user}"')
    )
    # So future migrations' tables (created by this same superuser role)
    # don't each need their own repeated GRANT statement.
    bind.execute(
        text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_db_user}"'
        )
    )

    # --- Row-level security: tenant isolation on `users` ---------------------
    bind.execute(text("ALTER TABLE users ENABLE ROW LEVEL SECURITY"))
    # Belt-and-suspenders: FORCE also binds the table owner (the superuser),
    # not just other roles. Not load-bearing today since the app never
    # connects as the owner, but it means an accidental future connection
    # as the owner role still gets stopped instead of silently bypassing RLS.
    bind.execute(text("ALTER TABLE users FORCE ROW LEVEL SECURITY"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON users
            USING (org_id = current_setting('app.current_org_id', true)::uuid)
            WITH CHECK (org_id = current_setting('app.current_org_id', true)::uuid)
            """
        )
    )
    # current_setting(name, true) — the `true` is missing_ok: it returns NULL
    # instead of raising when app.current_org_id was never set for this
    # session, rather than erroring. org_id = NULL evaluates to NULL (not
    # true) under SQL's three-valued logic, so the row is filtered out. That's
    # the whole default-deny mechanism: no context set means zero rows
    # visible, not an error and not every tenant's rows.


def downgrade() -> None:
    bind = op.get_bind()
    app_db_user = os.environ.get("APP_DB_USER", "wrkbase_app")

    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON users"))
    bind.execute(text("ALTER TABLE users NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE users DISABLE ROW LEVEL SECURITY"))

    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
    op.execute("DROP TYPE user_role")

    bind.execute(text(f'DROP OWNED BY "{app_db_user}"'))
    bind.execute(text(f'DROP ROLE IF EXISTS "{app_db_user}"'))
