import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, FetchedValue, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class TicketType(str, enum.Enum):
    EPIC = "epic"
    STORY = "story"
    TASK = "task"
    SUBTASK = "subtask"


class SprintStatus(str, enum.Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Stored, not derived live from `name` (migration 0011): a ticket's
    # displayed key (PREFIX-NUMBER) is meant to be permanent once assigned.
    # There's no org-rename endpoint today, so the two are equivalent right
    # now — stored anyway so a future rename can't silently change every
    # existing ticket's displayed id.
    ticket_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    # Backing counter for per-org sequential ticket numbers — see
    # create_ticket in app/api/tickets.py for the atomic
    # UPDATE ... RETURNING pattern that increments this.
    next_ticket_number: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))
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
    # Nullable + SET NULL (migration 0008), not the RESTRICT this started
    # as: member removal (see app/api/org.py) needs to actually delete a
    # User row, and RESTRICT would block that deletion for anyone who ever
    # created a project. A removed member's projects are kept — deleting
    # someone's org access shouldn't silently destroy their work — just
    # with an orphaned (NULL) creator reference. _require_owner_or_admin
    # treats a NULL creator as "not mine," so an orphaned project falls
    # back to admin-only edits, which is the right degraded state.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
    # Soft-delete (migration 0010). NULL = active. Set directly by
    # application code (DELETE /projects/{id}), not a trigger — no
    # eager_defaults wrinkle here the way updated_at has, since the value
    # is already known in Python before commit, not computed server-side.
    # Every read path filters this to NULL by default (see
    # app/api/projects.py's _get_project_or_404/list_projects) — see that
    # file for why this is an explicit app-layer filter rather than an
    # RLS-style unconditional policy.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowState(Base):
    """A board column (Backlog/In Progress/... ) for one project.

    No updated_at, deliberately: the contract for this table (see the
    Pydantic schema) has no trigger-maintained column, so there's nothing
    for eager_defaults to fix here — added reflexively it would just be
    dead weight. Reorders go through `order` (a plain client-supplied
    int), not a server-side timestamp.
    """

    __tablename__ = "workflow_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # No single-column FK on org_id/project_id — same reasoning as Ticket:
    # the real constraint is the composite (project_id, org_id) FK in
    # migration 0006, so a plain single-column marker here would describe
    # a weaker guarantee than what's actually enforced.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    order: Mapped[int] = mapped_column(nullable=False)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Sprint(Base):
    """A time-boxed planning window for one project.

    No eager_defaults, no updated_at: nothing here is trigger-maintained
    (status changes are set directly by application code — see
    app/api/sprints.py — same reasoning as Invite above).
    """

    __tablename__ = "sprints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # No single-column FK on org_id/project_id — same composite-FK
    # reasoning as WorkflowState: the real constraint is (project_id,
    # org_id) -> projects(id, org_id) in migration 0012.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    goal: Mapped[str | None] = mapped_column(String, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # create_type=False / values_callable: same enum-wiring reasoning as
    # UserRole/TicketType above. Status only ever changes through
    # start_sprint/complete_sprint (app/api/sprints.py), never a plain
    # PATCH — see SprintUpdate's docstring for why.
    status: Mapped[SprintStatus] = mapped_column(
        Enum(
            SprintStatus,
            name="sprint_status",
            create_type=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=SprintStatus.PLANNED.value,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Ticket(Base):
    __tablename__ = "tickets"
    # Applied proactively this time — this is exactly the Projects-slice
    # eager_defaults/MissingGreenlet lesson (see Project above), not
    # rediscovered here. See migration 0005 for the matching trigger.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # No single-column ForeignKey() markers on org_id/project_id/parent_id:
    # the real constraints are the composite ones in migration 0005
    # (project_id+org_id must match a real project in that org; parent_id+
    # project_id must match a real ticket in the same project). A plain
    # single-column FK here would describe a weaker guarantee than what's
    # actually enforced.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[TicketType] = mapped_column(
        Enum(
            TicketType,
            name="ticket_type",
            create_type=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    # Replaces the fixed status enum from the tickets slice: the workflow
    # is now per-project configurable state rows (migration 0006), not a
    # hardcoded todo/in_progress/done. No single-column FK here either —
    # same composite-FK reasoning as parent_id (must be a state in this
    # same project, not just any state in the org).
    workflow_state_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Fractional/"gap" index for ordering within a column: new cards get
    # max(position in that column) + 1024, and dropping a card between two
    # others computes the midpoint of their positions. Both are O(1) and
    # never require renumbering every other card in the column, which a
    # plain sequential integer position would need on most inserts.
    position: Mapped[float] = mapped_column(nullable=False)
    # Same SET NULL reasoning as Project.created_by above (migration 0008).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # No single-column FK here either — same composite-FK reasoning as
    # workflow_state_id: the real constraint is (assignee_id, org_id) ->
    # users(id, org_id) in migration 0009, org-scoped rather than
    # project-scoped since there's no per-project membership in this app —
    # any org member is a valid assignee for any ticket in any of that
    # org's projects. Without the composite FK, RLS alone wouldn't stop
    # assigning a ticket to a real user_id from a *different* org: RLS on
    # `tickets` only checks the ticket's own org_id, never the org of the
    # user a foreign key happens to point at. SET NULL on delete for the
    # same reason as created_by: a removed member's tickets are kept, just
    # unassigned, not destroyed or blocked from being removed.
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Per-org sequential display id (migration 0011) — combined with the
    # org's ticket_prefix on the frontend to render "WRK-142". Set once at
    # creation from Organization.next_ticket_number, never changes after.
    ticket_number: Mapped[int] = mapped_column(nullable=False)
    # No single-column FK — same composite-FK reasoning as
    # workflow_state_id: the real constraint is (sprint_id, project_id) ->
    # sprints(id, project_id) in migration 0012. NULL = backlog. Also goes
    # back to NULL automatically for tickets not in the project's terminal
    # workflow state when their sprint completes — see complete_sprint in
    # app/api/sprints.py.
    sprint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Nullable: estimation is optional, and unestimated tickets shouldn't
    # silently count as zero-effort in a sprint's total_points — see
    # _total_points in app/api/sprints.py, which sums only non-NULL values
    # (Postgres SUM already ignores NULLs, so this falls out for free).
    story_points: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), server_onupdate=FetchedValue()
    )
    # Same soft-delete shape as Project.deleted_at above (migration 0010),
    # same reasoning. A ticket with live (non-deleted) children can't be
    # soft-deleted — see delete_ticket in app/api/tickets.py — so by the
    # time a ticket's deleted_at is actually set, it's guaranteed to have
    # no visible children left to orphan in the /tree view.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invite(Base):
    """A pending or accepted invitation for one email to join this org.

    No eager_defaults: no column here is trigger-maintained (accepted_at is
    set by application code — see app/api/auth.py — not a DB trigger), so
    there's nothing async SQLAlchemy would need to re-fetch after a write.
    """

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            create_type=False,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    # Nullable + SET NULL: an admin who sent invites shouldn't become
    # undeletable because of it — same reasoning as Project/Ticket.created_by.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Opaque random token (secrets.token_urlsafe, same as refresh tokens),
    # not a JWT — see app/api/invites.py for the full reasoning. Unique so
    # invite_lookup (below) and the redemption query can both trust a token
    # resolves to exactly one row.
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # DateTime(timezone=True) spelled out explicitly here, unlike
    # created_at/updated_at elsewhere in this file: those are always
    # DB-computed (server_default/trigger), so the ORM never has to bind a
    # Python datetime value into an INSERT for them, and Mapped[datetime]'s
    # inferred (naive) column type never gets exercised. expires_at and
    # accepted_at are the first columns actually set from Python-side
    # datetime.now(timezone.utc) — without this, SQLAlchemy infers a plain
    # naive TIMESTAMP for the bind parameter, mismatching the migration's
    # real TIMESTAMP WITH TIME ZONE column and making asyncpg reject the
    # tz-aware value outright ("can't subtract offset-naive and
    # offset-aware datetimes"). Found by actually running this, not
    # inspected for in advance.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class InviteLookup(Base):
    """token -> (org_id, invite_id) routing only, no RLS — same role as
    UserLookup for login: redeeming an invite at signup has to find the
    invite BEFORE any tenant context exists to scope an RLS-protected query
    with. Kept in sync by a Postgres trigger on `invites` (migration 0007),
    not application code, for the same reason UserLookup is. Deliberately
    minimal: no email/role here, so a compromise of this table alone
    reveals nothing but "this token belongs to this org," and possession of
    the token itself is already the credential that matters.
    """

    __tablename__ = "invite_lookup"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    invite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invites.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )


class PasswordResetToken(Base):
    """A single-use, short-lived credential for resetting one user's own
    password. Deliberately has no org_id and no RLS — unlike Invite, this
    table has no authenticated, org-scoped management surface (no "list my
    org's outstanding reset tokens" view exists or is needed), so it never
    faces the chicken-and-egg problem RLS-protected tables have of needing
    a tenant context before they can be queried. It's structurally closer
    to UserLookup/InviteLookup (bootstrap-only, globally queryable by an
    unguessable key) than to Invite itself. See app/api/auth.py's
    confirm_password_reset for how org_id is recovered — via UserLookup's
    unique user_id index — once it's actually needed, at the point the
    RLS-protected `users` row has to be updated.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # CASCADE, same as UserLookup: a token for a user who's since been
    # removed from their org is meaningless and should simply vanish, not
    # need special-case handling at confirm time.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Opaque random token (secrets.token_urlsafe), same reasoning as
    # invite/refresh tokens — see app/api/invites.py's create_invite for
    # the full "why not a JWT" writeup, which applies identically here.
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
