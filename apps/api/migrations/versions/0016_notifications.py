"""notifications table (Phase 2, slice 3): per-recipient events, delivered
live over the new /ws/notifications room and durably stored for GET
/notifications. See app/services/notifications.py for why this table needs
a genuinely different RLS shape than every other table in this app.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # mention included now even though nothing creates it yet (comments/
    # mentions don't exist in this app) — adding an enum label later is a
    # real, separate migration Postgres won't let run inside the same
    # transaction as other DDL depending on it, whereas including the
    # label up front costs nothing today. See NotificationType's own
    # docstring for the deferred-not-forgotten distinction.
    op.execute("CREATE TYPE notification_type AS ENUM ('assignment', 'invite_accepted', 'mention')")

    op.create_table(
        "notifications",
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
        # No composite (project_id, org_id)-style FK the way tickets/sprints
        # need: a notification isn't scoped to a project at all (an invite-
        # accepted notification has no project involved), so org_id is this
        # row's only structural scope. The *recipient* is scoped by the
        # composite FK below instead.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(
                "assignment", "invite_accepted", "mention", name="notification_type", create_type=False
            ),
            nullable=False,
        ),
        # Deliberately minimal, type-specific, never the full referenced
        # resource — enough to render the notification and link to it
        # (ticket_id/project_id/ticket_title/assigned_by for an assignment;
        # invite_id/accepted_email/role for invite_accepted), not a second
        # copy of data GET /projects/{id}/tickets/{id} already serves.
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    # Composite FK, same reasoning as tickets.assignee_id (migration 0009):
    # without this, nothing stops a notification row claiming org_id=A but
    # user_id=<a real user who actually belongs to org B> — the FK makes
    # "this recipient really is a member of this org" a DB-enforced
    # invariant, not just something every future INSERT is trusted to get
    # right. Reuses uq_users_id_org_id, already created by migration 0009 —
    # no new unique constraint needed on users.
    op.create_foreign_key(
        "fk_notifications_user_org",
        "notifications",
        "users",
        ["user_id", "org_id"],
        ["id", "org_id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_notifications_org_id", "notifications", ["org_id"])
    # Composite, not a plain user_id index: every real query this table
    # serves is "this user's notifications, most recent first" — GET
    # /notifications, the unread count, mark-read — so the index should
    # match the actual access pattern, not just the FK column.
    op.create_index(
        "ix_notifications_user_id_created_at",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
    )

    bind.execute(text("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE notifications FORCE ROW LEVEL SECURITY"))

    # Every other RLS-protected table in this app has one policy shape:
    # the acting user and the row's "owner" (org, or org+project) are the
    # same axis, so USING and WITH CHECK both just compare org_id (and
    # sometimes project_id) to the current session's own context.
    # Notifications break that assumption structurally: a notification is
    # always created *for* someone other than whoever is currently
    # authenticated — an admin assigns a ticket to a teammate, and the
    # teammate is the recipient, not the admin doing the PATCH; a new user
    # redeems an invite, and the admin who sent it is the recipient, in a
    # request where the admin isn't authenticated at all. If this table's
    # policy required user_id = current_user_id unconditionally (the
    # naive, "just add the extra column" version of every other table's
    # policy), no INSERT here could ever succeed — the acting user is
    # essentially never the recipient. So, three per-command policies,
    # each intentionally different, the same reasoning shape as
    # organizations (migration 0015) even though the actual asymmetry is
    # different:
    #
    #   - INSERT is scoped to org only (WITH CHECK org_id = current org).
    #     Creating a notification for any real member of your own org
    #     isn't a privacy leak — nothing is being *read* — and the
    #     composite FK above already guarantees the recipient genuinely
    #     belongs to that org regardless of what this policy allows.
    #
    #   - SELECT is scoped to org AND recipient (org_id = current org AND
    #     user_id = current user). This is the actual privacy boundary:
    #     nothing in this app has a legitimate reason for one user to read
    #     another's notifications, unlike soft-delete's restore path,
    #     which is exactly why this lives in RLS rather than an app-layer
    #     WHERE clause — an unconditional trust boundary, not a business
    #     rule with a sanctioned exception.
    #
    #   - UPDATE is scoped the same way as SELECT: a user can only mark
    #     their own notifications read, never someone else's, and (see
    #     app/services/notifications.py) creation uses a raw INSERT with
    #     no RETURNING specifically to avoid needing this policy to also
    #     cover the acting user's write.
    #
    #   - DELETE is left unpoliced (default-deny), same as organizations:
    #     nothing in this app ever deletes a notification row.
    bind.execute(
        text(
            """
            CREATE POLICY insert_org_notification ON notifications
            FOR INSERT
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )
    bind.execute(
        text(
            """
            CREATE POLICY select_own_notifications ON notifications
            FOR SELECT
            USING (
                org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
            """
        )
    )
    bind.execute(
        text(
            """
            CREATE POLICY update_own_notifications ON notifications
            FOR UPDATE
            USING (
                org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
            WITH CHECK (
                org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()

    bind.execute(text("DROP POLICY IF EXISTS update_own_notifications ON notifications"))
    bind.execute(text("DROP POLICY IF EXISTS select_own_notifications ON notifications"))
    bind.execute(text("DROP POLICY IF EXISTS insert_org_notification ON notifications"))
    bind.execute(text("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE notifications DISABLE ROW LEVEL SECURITY"))

    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_index("ix_notifications_org_id", table_name="notifications")
    op.drop_constraint("fk_notifications_user_org", "notifications", type_="foreignkey")
    op.drop_table("notifications")
    op.execute("DROP TYPE notification_type")
