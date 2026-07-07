"""invites table + RLS; invite_lookup (token -> org_id/invite_id, no RLS,
same pre-auth-bootstrap role as user_lookup)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # No composite FK here, unlike tickets/workflow_states. Those needed
    # (project_id, org_id) pairs because they're scoped along TWO
    # dimensions at once (a project within an org) that could disagree —
    # a project_id from a different org in the same table. An invite is
    # only ever org-scoped, the same single dimension as `projects` or
    # `users` themselves, so a plain single-column org_id FK is the
    # correct, sufficient guarantee — there's no second scope for it to
    # drift out of sync with.
    op.create_table(
        "invites",
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
        sa.Column("email", sa.String, nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "member", "viewer", name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("token", sa.String, nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    op.create_index("ix_invites_org_id", "invites", ["org_id"])

    bind.execute(text("ALTER TABLE invites ENABLE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE invites FORCE ROW LEVEL SECURITY"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON invites
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )

    # No RLS on this one, deliberately — identical reasoning to user_lookup
    # (migration 0002): redeeming an invite at signup means looking it up
    # by token BEFORE any tenant context can exist to scope an
    # RLS-protected query with. Holds only a token -> (org_id, invite_id)
    # mapping, nothing sensitive (no email, no role) — the real
    # authorization is "do you possess this unguessable 32-byte token,"
    # which this table alone can't leak.
    op.create_table(
        "invite_lookup",
        sa.Column("token", sa.String, primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invites.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
    )

    # Same trigger-sync pattern as sync_user_lookup (migration 0002): any
    # INSERT into `invites`, from any code path, keeps invite_lookup
    # populated automatically. No UPDATE branch needed — unlike users
    # (whose email/org_id can change), nothing invite_lookup cares about
    # (token, org_id, invite_id) is ever updated after an invite is
    # created; accepted_at changes don't touch this table at all. No
    # DELETE branch needed either: invite_lookup.invite_id has ON DELETE
    # CASCADE, so revoking an invite (DELETE FROM invites) removes its
    # lookup row automatically without a second trigger.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_invite_lookup() RETURNS trigger AS $$
        BEGIN
            INSERT INTO invite_lookup (token, org_id, invite_id)
            VALUES (NEW.token, NEW.org_id, NEW.id);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER invites_sync_lookup
        AFTER INSERT ON invites
        FOR EACH ROW EXECUTE FUNCTION sync_invite_lookup();
        """
    )
    # wrkbase_app already has SELECT/INSERT/UPDATE/DELETE on both new
    # tables via migration 0001's ALTER DEFAULT PRIVILEGES.


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP TRIGGER IF EXISTS invites_sync_lookup ON invites")
    op.execute("DROP FUNCTION IF EXISTS sync_invite_lookup()")
    op.drop_table("invite_lookup")

    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON invites"))
    bind.execute(text("ALTER TABLE invites NO FORCE ROW LEVEL SECURITY"))
    bind.execute(text("ALTER TABLE invites DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_invites_org_id", table_name="invites")
    op.drop_table("invites")
