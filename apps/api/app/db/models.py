import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, FetchedValue, ForeignKey, String, text
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


class Project(Base):
    __tablename__ = "projects"
    # Async gotcha found wiring the update endpoint up: without
    # eager_defaults, the ORM defers fetching server_onupdate-marked
    # columns until the next attribute access, expecting a transparent
    # lazy-load. Under async SQLAlchemy there's no such thing — that access
    # has to be awaited, and FastAPI's response serialization can't await
    # mid-attribute-read. It surfaced as a 500 (MissingGreenlet) on the very
    # first real UPDATE. eager_defaults forces the UPDATE statement itself
    # to RETURNING the trigger-modified updated_at, so the in-memory object
    # is already correct by the time serialization touches it.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    # RESTRICT, not CASCADE: deleting a user shouldn't silently wipe out
    # every project they ever created. No user-deletion flow exists yet, so
    # this is a forward-looking default, not a tested constraint.
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    # server_onupdate (not server_default): this table has a BEFORE UPDATE
    # trigger (migration 0004) that sets updated_at = now() on every UPDATE.
    # Without this marker SQLAlchemy has no way to know the trigger changed
    # the row, and the in-memory object would show a stale timestamp after
    # commit until something explicitly re-fetched it.
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), server_onupdate=FetchedValue()
    )
