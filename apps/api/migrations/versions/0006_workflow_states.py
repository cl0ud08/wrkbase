"""workflow_states table (configurable board columns) + RLS;
replace tickets.status enum with workflow_state_id + position

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_STATES = [("Backlog", 0, True), ("In Progress", 1, False), ("Review", 2, False), ("Done", 3, False)]


def upgrade() -> None:
    bind = op.get_bind()

    # uq_projects_id_org_id already exists (migration 0004) — this table
    # needs the exact same tenant-consistency guarantee tickets got: a
    # workflow_state's project_id must belong to a project that's really
    # in its own org_id, not just any project in any org. Any table shaped
    # like "org-scoped AND project-scoped" needs this, not just tickets —
    # it's a property of the shape, not something ticket-specific.
    op.create_table(
        "workflow_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("order", sa.Integer, nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "org_id"],
            ["projects.id", "projects.org_id"],
            ondelete="CASCADE",
            name="fk_workflow_states_project_org",
        ),
    )
    # Supports tickets.workflow_state_id's composite FK below, same pattern
    # as uq_tickets_id_project_id supporting the self-referential parent_id.
    op.create_unique_constraint(
        "uq_workflow_states_id_project_id", "workflow_states", ["id", "project_id"]
    )
    op.create_index("ix_workflow_states_org_id", "workflow_states", ["org_id"])
    op.create_index("ix_workflow_states_project_id", "workflow_states", ["project_id"])

    bind.execute(text("ALTER TABLE workflow_states ENABLE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE workflow_states FORCE ROW LEVEL SECURITY"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON workflow_states
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )

    # Backfill: every project that existed before this slice gets the same
    # default states the app now seeds on project creation, so its
    # existing tickets have somewhere valid to point below. (New projects
    # get this from app/api/projects.py at creation time, not from here —
    # see that file for why seeding lives in the endpoint, not a trigger.)
    op.execute(
        f"""
        INSERT INTO workflow_states (id, org_id, project_id, name, "order", is_default, created_at)
        SELECT gen_random_uuid(), p.org_id, p.id, s.name, s.ord, s.is_default, now()
        FROM projects p
        CROSS JOIN (VALUES {", ".join(f"('{n}', {o}, {str(d).lower()})" for n, o, d in _DEFAULT_STATES)}) AS s(name, ord, is_default)
        """
    )

    op.add_column("tickets", sa.Column("workflow_state_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("tickets", sa.Column("position", sa.Float, nullable=True))

    # Map each existing ticket's old status to the matching new state in
    # its OWN project (by name, not by some global state id — every
    # project got its own copy of the four states above).
    op.execute(
        """
        UPDATE tickets t
        SET workflow_state_id = ws.id
        FROM workflow_states ws
        WHERE ws.project_id = t.project_id
          AND ws.name = CASE t.status
                WHEN 'todo' THEN 'Backlog'
                WHEN 'in_progress' THEN 'In Progress'
                WHEN 'done' THEN 'Done'
              END
        """
    )
    # Positions didn't exist before; assign each existing ticket a slot
    # within its (now-known) column, ordered by when it was created.
    op.execute(
        """
        UPDATE tickets t
        SET position = sub.rn * 1024.0
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY workflow_state_id ORDER BY created_at) AS rn
            FROM tickets
        ) sub
        WHERE t.id = sub.id
        """
    )

    op.alter_column("tickets", "workflow_state_id", nullable=False)
    op.alter_column("tickets", "position", nullable=False)
    op.drop_column("tickets", "status")
    op.execute("DROP TYPE ticket_status")

    op.create_foreign_key(
        "fk_tickets_workflow_state_project",
        "tickets",
        "workflow_states",
        ["workflow_state_id", "project_id"],
        ["id", "project_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_tickets_workflow_state_id", "tickets", ["workflow_state_id"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_tickets_workflow_state_id", table_name="tickets")
    op.drop_constraint("fk_tickets_workflow_state_project", "tickets", type_="foreignkey")

    op.execute("CREATE TYPE ticket_status AS ENUM ('todo', 'in_progress', 'done')")
    op.add_column(
        "tickets",
        sa.Column(
            "status",
            postgresql.ENUM("todo", "in_progress", "done", name="ticket_status", create_type=False),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE tickets t
        SET status = CASE ws.name
                WHEN 'Backlog' THEN 'todo'
                WHEN 'In Progress' THEN 'in_progress'
                WHEN 'Review' THEN 'in_progress'
                WHEN 'Done' THEN 'done'
                ELSE 'todo'
              END
        FROM workflow_states ws
        WHERE ws.id = t.workflow_state_id
        """
    )
    op.alter_column("tickets", "status", nullable=False)
    op.drop_column("tickets", "position")
    op.drop_column("tickets", "workflow_state_id")

    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON workflow_states"))
    bind.execute(text("ALTER TABLE workflow_states NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE workflow_states DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_workflow_states_project_id", table_name="workflow_states")
    op.drop_index("ix_workflow_states_org_id", table_name="workflow_states")
    op.drop_constraint("uq_workflow_states_id_project_id", "workflow_states", type_="unique")
    op.drop_table("workflow_states")
