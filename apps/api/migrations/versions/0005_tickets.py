"""tickets table: epic/story/task/subtask hierarchy + RLS

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE TYPE ticket_type AS ENUM ('epic', 'story', 'task', 'subtask')")
    op.execute("CREATE TYPE ticket_status AS ENUM ('todo', 'in_progress', 'done')")

    # Supports the composite FK below: a ticket's project_id must belong to
    # a project that's actually in the ticket's own org_id — not just any
    # project, and not just any org. id alone is already unique (it's the
    # PK); this just makes (id, org_id) referenceable as a pair. This is a
    # tenant-consistency invariant, the same class of guarantee as RLS
    # itself, so it's enforced structurally rather than trusted to app
    # code that builds the INSERT.
    op.create_unique_constraint("uq_projects_id_org_id", "projects", ["id", "org_id"])

    op.create_table(
        "tickets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "type",
            postgresql.ENUM("epic", "story", "task", "subtask", name="ticket_type", create_type=False),
            nullable=False,
        ),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("description", sa.String, nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("todo", "in_progress", "done", name="ticket_status", create_type=False),
            nullable=False,
            server_default="todo",
        ),
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
        sa.ForeignKeyConstraint(
            ["project_id", "org_id"],
            ["projects.id", "projects.org_id"],
            ondelete="CASCADE",
            name="fk_tickets_project_org",
        ),
    )

    # Supports the self-referential composite FK below the same way
    # uq_projects_id_org_id supports the one above: a ticket's parent_id
    # must be a ticket in the SAME project, not just any project in the
    # org. NULL parent_id (top-level tickets) trivially satisfies a
    # multi-column FK — Postgres's default MATCH SIMPLE only checks the
    # constraint when every referencing column is non-null.
    op.create_unique_constraint("uq_tickets_id_project_id", "tickets", ["id", "project_id"])
    op.create_foreign_key(
        "fk_tickets_parent_project",
        "tickets",
        "tickets",
        ["parent_id", "project_id"],
        ["id", "project_id"],
        ondelete="RESTRICT",
    )

    op.create_index("ix_tickets_org_id", "tickets", ["org_id"])
    op.create_index("ix_tickets_project_id", "tickets", ["project_id"])
    op.create_index("ix_tickets_parent_id", "tickets", ["parent_id"])

    # Reuses set_updated_at() from migration 0004 — it's a generic
    # "NEW.updated_at = now()" function, no new trigger function needed.
    op.execute(
        """
        CREATE TRIGGER tickets_set_updated_at
        BEFORE UPDATE ON tickets
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    bind.execute(text("ALTER TABLE tickets ENABLE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE tickets FORCE ROW LEVEL SECURITY"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON tickets
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )
    # Note what RLS does NOT cover here: it guarantees org_id matches the
    # caller's tenant, but says nothing about whether project_id is the
    # "right" project within that org — that's exactly what the composite
    # FK above enforces instead. The subtask-must-have-a-story/task-parent
    # rule is a third, different kind of check (a type-conditional business
    # rule, not a tenant/scope boundary) and lives in the API layer — see
    # app/api/tickets.py — since it needs clean error messages and may
    # evolve as workflow rules become configurable in a later slice.


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON tickets"))
    bind.execute(text("ALTER TABLE tickets NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE tickets DISABLE ROW LEVEL SECURITY"))
    op.execute("DROP TRIGGER IF EXISTS tickets_set_updated_at ON tickets")
    op.drop_index("ix_tickets_parent_id", table_name="tickets")
    op.drop_index("ix_tickets_project_id", table_name="tickets")
    op.drop_index("ix_tickets_org_id", table_name="tickets")
    op.drop_constraint("fk_tickets_parent_project", "tickets", type_="foreignkey")
    op.drop_constraint("uq_tickets_id_project_id", "tickets", type_="unique")
    op.drop_table("tickets")
    op.drop_constraint("uq_projects_id_org_id", "projects", type_="unique")
    op.execute("DROP TYPE ticket_status")
    op.execute("DROP TYPE ticket_type")
