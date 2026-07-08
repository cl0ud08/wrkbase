"""projects.deleted_at / tickets.deleted_at: soft-delete support

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No changes to the composite FKs (fk_tickets_project_org,
    # fk_tickets_parent_project) — deliberately. A foreign key constraint
    # only asserts that the referenced row *exists*; it has no concept of
    # deleted_at and can't be made to check it (Postgres FKs reference a
    # unique/PK constraint, not an arbitrary predicate — there's no
    # "REFERENCES projects(id, org_id) WHERE deleted_at IS NULL" syntax).
    # So a ticket's project_id/parent_id FK stays satisfied regardless of
    # whether the referenced row is soft-deleted — structurally, nothing
    # stops it. "Can a ticket be created against a soft-deleted
    # project/parent" is answered at the app layer instead: every lookup
    # a validation path uses (_get_project_or_404, _validate_parent) now
    # filters deleted_at IS NULL by default, so a soft-deleted project or
    # parent simply "doesn't exist" from those checks' point of view —
    # the exact same 404/422 a genuinely nonexistent id already produces,
    # with no new validation branch needed.
    op.add_column("projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tickets", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_projects_deleted_at", "projects", ["deleted_at"])
    op.create_index("ix_tickets_deleted_at", "tickets", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_tickets_deleted_at", table_name="tickets")
    op.drop_index("ix_projects_deleted_at", table_name="projects")
    op.drop_column("tickets", "deleted_at")
    op.drop_column("projects", "deleted_at")
