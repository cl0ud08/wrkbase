"""sprints table (Backlog + Sprints + story points) + RLS; single-active-
sprint partial unique index; tickets.sprint_id + tickets.story_points

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE TYPE sprint_status AS ENUM ('planned', 'active', 'completed')")

    # Same shape as workflow_states: org-scoped AND project-scoped, so it
    # needs both the composite (project_id, org_id) FK below (a sprint's
    # project really belongs to its own org, not just any org) and RLS on
    # org_id (a sprint from another org is invisible, not just unreachable
    # by a well-behaved query).
    op.create_table(
        "sprints",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("goal", sa.String, nullable=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("planned", "active", "completed", name="sprint_status", create_type=False),
            nullable=False,
            server_default="planned",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "org_id"],
            ["projects.id", "projects.org_id"],
            ondelete="CASCADE",
            name="fk_sprints_project_org",
        ),
    )
    # Supports tickets.sprint_id's composite FK below, same role as
    # uq_workflow_states_id_project_id for workflow_state_id.
    op.create_unique_constraint("uq_sprints_id_project_id", "sprints", ["id", "project_id"])
    op.create_index("ix_sprints_org_id", "sprints", ["org_id"])
    op.create_index("ix_sprints_project_id", "sprints", ["project_id"])

    bind.execute(text("ALTER TABLE sprints ENABLE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE sprints FORCE ROW LEVEL SECURITY"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON sprints
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )

    # The single-active-sprint rule, enforced where it actually has to be:
    # a plain per-query "is there already an active sprint" check has a
    # real TOCTOU race under concurrent requests (two near-simultaneous
    # "start sprint" calls can both pass the check before either commits).
    # This isn't a trust boundary the way RLS is (nothing here is about
    # not trusting future code to remember a WHERE clause), and it isn't a
    # business rule with a legitimate exception the way soft-delete's
    # filter is (there's never a valid reason for two active sprints in
    # one project) — it's a plain uniqueness invariant with a genuine
    # concurrency hazard, and only a constraint Postgres enforces
    # atomically at write time closes that regardless of how two
    # transactions interleave. See app/api/sprints.py's start_sprint for
    # where the resulting IntegrityError gets turned into a 409.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_sprints_one_active_per_project
        ON sprints (project_id)
        WHERE status = 'active'
        """
    )

    op.add_column("tickets", sa.Column("sprint_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("tickets", sa.Column("story_points", sa.Integer, nullable=True))
    op.create_foreign_key(
        "fk_tickets_sprint_project",
        "tickets",
        "sprints",
        ["sprint_id", "project_id"],
        ["id", "project_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_tickets_sprint_id", "tickets", ["sprint_id"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_tickets_sprint_id", table_name="tickets")
    op.drop_constraint("fk_tickets_sprint_project", "tickets", type_="foreignkey")
    op.drop_column("tickets", "story_points")
    op.drop_column("tickets", "sprint_id")

    op.execute("DROP INDEX uq_sprints_one_active_per_project")

    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON sprints"))
    bind.execute(text("ALTER TABLE sprints NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE sprints DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_sprints_project_id", table_name="sprints")
    op.drop_index("ix_sprints_org_id", table_name="sprints")
    op.drop_constraint("uq_sprints_id_project_id", "sprints", type_="unique")
    op.drop_table("sprints")

    op.execute("DROP TYPE sprint_status")
