"""password_reset_tokens table — no RLS, no org_id: unlike invites, this
table has no authenticated org-scoped management surface, so it never
faces RLS's chicken-and-egg problem of needing a tenant context before
it can be queried.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No trigger-synced lookup table the way user_lookup/invite_lookup are:
    # those exist to mirror a *derived* routing view of another RLS-
    # protected table (users, invites) so it can be queried pre-tenant-
    # context. password_reset_tokens isn't derived from anything — it's
    # written directly by the application, and reading it never needs a
    # tenant context in the first place (see app/db/models.py's
    # PasswordResetToken docstring), so there's nothing to sync.
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String, nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_password_reset_tokens_user",
        ),
    )
    # Supports the confirm-time reuse-invalidation query (mark every other
    # still-live token for this user used too, once one succeeds) without
    # a full table scan.
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
