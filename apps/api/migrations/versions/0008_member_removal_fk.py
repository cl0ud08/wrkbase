"""projects.created_by / tickets.created_by: RESTRICT -> nullable + SET NULL

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-14

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, the auto-generated FK constraint name Alembic/Postgres gave the
# original inline sa.ForeignKey() in migrations 0004/0005)
_TABLES = [
    ("projects", "projects_created_by_fkey"),
    ("tickets", "tickets_created_by_fkey"),
]


def upgrade() -> None:
    # Direct consequence of adding real member removal (app/api/org.py):
    # both columns were declared RESTRICT under the comment "no
    # user-deletion flow exists yet" (migrations 0004/0005) — that flow
    # exists now, and RESTRICT would make DELETE /org/members/{user_id}
    # fail with a raw IntegrityError for any member who ever created a
    # project or ticket, which is most members. Switching to SET NULL
    # keeps the project/ticket itself intact and just orphans the creator
    # reference — the alternative (CASCADE) would silently delete a
    # departed teammate's entire work history, which is a far more
    # surprising and destructive default than losing attribution.
    for table, fk_name in _TABLES:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.alter_column(table, "created_by", nullable=True)
        op.create_foreign_key(
            f"fk_{table}_created_by",
            table,
            "users",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    # Reversing this is lossy if any row has actually been orphaned by a
    # member removal in the meantime (NULL has no original user to restore
    # to) — alter_column(nullable=False) will simply fail on such data,
    # which is the correct, honest failure mode rather than silently
    # inventing a fake creator.
    for table, fk_name in _TABLES:
        op.drop_constraint(f"fk_{table}_created_by", table, type_="foreignkey")
        op.alter_column(table, "created_by", nullable=False)
        op.create_foreign_key(fk_name, table, "users", ["created_by"], ["id"], ondelete="RESTRICT")
