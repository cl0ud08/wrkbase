"""tickets.assignee_id: nullable, composite FK to users scoped by org

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Supports the composite FK below, same role as uq_projects_id_org_id /
    # uq_tickets_id_project_id: a ticket's assignee_id must be a user who's
    # really a member of the ticket's own org_id, not just any user in any
    # org. Org-scoped, not project-scoped, since this app has no
    # per-project membership — any org member can be assigned any ticket
    # in any of that org's projects.
    op.create_unique_constraint("uq_users_id_org_id", "users", ["id", "org_id"])

    op.add_column("tickets", sa.Column("assignee_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_tickets_assignee_org",
        "tickets",
        "users",
        ["assignee_id", "org_id"],
        ["id", "org_id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tickets_assignee_id", "tickets", ["assignee_id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_assignee_id", table_name="tickets")
    op.drop_constraint("fk_tickets_assignee_org", "tickets", type_="foreignkey")
    op.drop_column("tickets", "assignee_id")
    op.drop_constraint("uq_users_id_org_id", "users", type_="unique")
