"""users.is_verified + email_verification_tokens table (same no-RLS shape
as password_reset_tokens — see migration 0013's docstring, identical
reasoning applies).

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_verified", sa.Boolean, nullable=True))
    # Grandfather every account that existed before this feature shipped —
    # they were working fine without email verification; suddenly nudging
    # them to verify something that wasn't a requirement when they signed
    # up would be a regression, not a security improvement. Only accounts
    # created from here forward go through the real flow (see signup() in
    # app/api/auth.py: is_verified=True for an invite redemption, False —
    # the column's own default below — for a brand-new self-serve org).
    op.execute("UPDATE users SET is_verified = true")
    op.alter_column("users", "is_verified", nullable=False, server_default=text("false"))

    op.create_table(
        "email_verification_tokens",
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
            name="fk_email_verification_tokens_user",
        ),
    )
    op.create_index("ix_email_verification_tokens_user_id", "email_verification_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_email_verification_tokens_user_id", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_column("users", "is_verified")
