"""projects table + tenant isolation RLS

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "projects",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.String, nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    op.create_index("ix_projects_org_id", "projects", ["org_id"])

    # Same trigger-based invariant pattern as user_lookup (migration 0002):
    # push "updated_at always reflects the last write" into the DB so no
    # future endpoint can forget to bump it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER projects_set_updated_at
        BEFORE UPDATE ON projects
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    # RLS in the same migration as the table, not a follow-up — same rule as
    # organizations/users. NULLIF from the start this time: migration 0003
    # found that a bare current_setting(...) comparison breaks on the
    # empty-string reset case, not just the NULL/unset case.
    bind.execute(text("ALTER TABLE projects ENABLE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE projects FORCE ROW LEVEL SECURITY"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON projects
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )
    # wrkbase_app already has SELECT/INSERT/UPDATE/DELETE on this table via
    # migration 0001's ALTER DEFAULT PRIVILEGES — no extra GRANT needed here.


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON projects"))
    bind.execute(text("ALTER TABLE projects NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE projects DISABLE ROW LEVEL SECURITY"))
    op.execute("DROP TRIGGER IF EXISTS projects_set_updated_at ON projects")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.drop_index("ix_projects_org_id", table_name="projects")
    op.drop_table("projects")
