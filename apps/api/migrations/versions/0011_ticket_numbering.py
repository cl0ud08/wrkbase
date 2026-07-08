"""Per-org human-readable ticket numbers: organizations.ticket_prefix +
next_ticket_number, tickets.ticket_number

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Stored, not derived live from organizations.name on every read: there's
    # no org-rename endpoint today, so the two are equivalent right now, but
    # a ticket's displayed key (PREFIX-NUMBER) is meant to be a stable,
    # permanent identifier once assigned — if a rename feature ever ships,
    # recomputing the prefix from the (now different) name would silently
    # change every existing ticket's displayed ID. Storing it fixes that
    # ahead of time rather than becoming a real bug the day renaming exists.
    op.add_column("organizations", sa.Column("ticket_prefix", sa.String(length=8), nullable=True))
    # The counter backing atomic per-org sequence generation (see
    # create_ticket in app/api/tickets.py): a single `UPDATE organizations
    # SET next_ticket_number = next_ticket_number + 1 ... RETURNING` per
    # ticket creation, relying on Postgres's normal row-level locking for
    # concurrent UPDATEs to the same row — no separate locking scheme needed.
    op.add_column(
        "organizations",
        sa.Column("next_ticket_number", sa.Integer, nullable=False, server_default="1"),
    )

    # Backfill: derive each existing org's prefix from its name — first 4
    # alphanumeric characters, uppercased. Deliberately simple (no
    # collision-avoidance across orgs): a ticket key only ever needs to be
    # unique *within* its own org's UI, which the per-org sequence already
    # guarantees; two different orgs sharing a prefix causes no real
    # confusion since nobody views both orgs' tickets side by side.
    op.execute(
        r"""
        UPDATE organizations
        SET ticket_prefix = upper(left(regexp_replace(name, '[^a-zA-Z0-9]', '', 'g'), 4))
        """
    )
    # Degenerate case: a name with zero alphanumeric characters (e.g. all
    # punctuation) would backfill to an empty string — fall back to a fixed
    # placeholder rather than shipping a blank prefix.
    op.execute("UPDATE organizations SET ticket_prefix = 'ORG' WHERE ticket_prefix = ''")
    op.alter_column("organizations", "ticket_prefix", nullable=False)

    op.add_column("tickets", sa.Column("ticket_number", sa.Integer, nullable=True))
    # Backfill existing tickets: sequential per org, ordered by creation
    # time — the same ROW_NUMBER-partition-by-org shape migration 0006 used
    # to backfill ticket positions per workflow state.
    op.execute(
        """
        UPDATE tickets t
        SET ticket_number = sub.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY created_at) AS rn
            FROM tickets
        ) sub
        WHERE t.id = sub.id
        """
    )
    op.alter_column("tickets", "ticket_number", nullable=False)

    # Point each org's counter past whatever the backfill just assigned, so
    # the next ticket created continues the sequence instead of colliding.
    op.execute(
        """
        UPDATE organizations o
        SET next_ticket_number = COALESCE(
            (SELECT MAX(t.ticket_number) + 1 FROM tickets t WHERE t.org_id = o.id), 1
        )
        """
    )

    # Structural guarantee that the per-org sequence is actually unique,
    # same class of belt-and-suspenders as the composite FKs elsewhere in
    # this app: the atomic UPDATE...RETURNING pattern should already make
    # duplicates impossible, but this is what actually enforces it at the
    # DB level rather than trusting the application code alone.
    op.create_unique_constraint("uq_tickets_org_id_ticket_number", "tickets", ["org_id", "ticket_number"])


def downgrade() -> None:
    op.drop_constraint("uq_tickets_org_id_ticket_number", "tickets", type_="unique")
    op.drop_column("tickets", "ticket_number")
    op.drop_column("organizations", "next_ticket_number")
    op.drop_column("organizations", "ticket_prefix")
