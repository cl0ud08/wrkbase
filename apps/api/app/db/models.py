import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # The column every RLS policy in this app keys off. Not nullable: a user
    # that can't be scoped to a tenant is a bug, not a valid state.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    # create_type=False: the migration creates the `user_role` Postgres enum
    # explicitly, so SQLAlchemy shouldn't try to (re)create it on metadata.create_all.
    # values_callable: without it, SQLAlchemy sends the Python member's NAME
    # ("ADMIN") on the wire, but the Postgres enum's labels are the lowercase
    # VALUEs ("admin") — mismatch fails with "invalid input value for enum".
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            create_type=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=UserRole.MEMBER.value,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class UserLookup(Base):
    """email -> (org_id, user_id) routing only. No RLS, no password hash.

    Kept in sync by a Postgres trigger on `users` (migration 0002), not by
    application code, so the invariant holds no matter which code path
    inserts a user. The app only ever reads this table.
    """

    __tablename__ = "user_lookup"

    email: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
