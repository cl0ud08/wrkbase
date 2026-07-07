# Changelog

Chronological build log for Wrkbase, covering Phase 0 (foundation) and
Phase 1 (product features) as they ship. Each entry covers what was built
and the reasoning behind the decisions that mattered — written for
interview prep, not just a feature list. Phase 0 built 2026-07-06 through
2026-07-07; Phase 1 begins 2026-07-07.

---

## Slice 1 — Phase 0 skeleton

**Built:** `docker-compose.yml` wiring Postgres, Redis, FastAPI, and
Next.js together; a bare `GET /health` endpoint; a homepage that fetches
and displays it.

**Why `pgvector/pgvector:pg16` instead of plain `postgres:16` from day
one.** pgvector is a locked-in stack requirement (ticket/comment embeddings
later). Starting with the extension already compiled in avoids a "why
doesn't `CREATE EXTENSION vector` work" surprise several slices from now,
when swapping the base image would mean migrating real data.

**Why healthchecks + `depends_on: condition: service_healthy` on
Postgres/Redis.** Without this, the API container can start and accept
traffic before Postgres has finished its own init and is actually ready
for connections. That's invisible in normal dev (it "happens to work")
and then shows up as flaky failures on first boot or in CI — exactly the
kind of bug that's cheap to prevent up front and annoying to debug later.

**Port remapping note.** This machine already had another project bound
to 8000 and 6379. Ports were remapped in `.env` (API → 8010, Redis → 6380)
rather than touching the other project; `.env.example` keeps the
conventional 8000/6379 defaults since the conflict is machine-specific,
not project-specific.

---

## Slice 2 — DB schema + Row-Level Security

**Built:** `organizations` and `users` tables (global email uniqueness
constraint on `users.email`), a `user_lookup` table with a Postgres
trigger keeping it synced to `users`, the `tenant_isolation` RLS policy,
a seed script, and `scripts/verify_rls.py` proving isolation holds.

**Why two Postgres roles, not one.** Migrations run as a superuser
(`wrkbase`) that needs `CREATE ROLE`/`CREATE POLICY`/`ALTER TABLE`
privileges. The running API connects as a separate, ordinary role
(`wrkbase_app`) with no special privileges. This is the decision the whole
slice hinges on: Postgres RLS is silently bypassed for superusers always,
and for a table's own owner unless `FORCE ROW LEVEL SECURITY` is set (and
even `FORCE` never binds superusers). If the app connected as the same
role that ran migrations, every RLS policy below would be a silent
no-op — and you'd never notice in dev, because your own queries would
keep "working."

**The tenant-context mechanism:**
`SELECT set_config('app.current_org_id', :org_id, true)`, not a literal
`SET LOCAL` string. Postgres's wire protocol only accepts bind parameters
for `SELECT`/`INSERT`/`UPDATE`/`DELETE`/`VALUES`, not for `SET` itself —
`set_config()` is a normal function call, so it can take a bound
parameter. The third argument (`is_local=true`) mirrors `SET LOCAL`: the
setting is discarded when the transaction ends, so a pooled connection can
never carry one request's tenant context into the next request that
happens to reuse it.

**The `NULLIF` fix (migration 0003) — a real bug found while building the
next slice.** The *first-ever* `SET LOCAL` of a custom GUC on a given
physical connection resets to an empty string `''` on rollback — not to
true `NULL` — if that transaction ends without an explicit
commit/rollback (exactly what a read-only request does).  `''::uuid`
raised a cast error instead of comparing as "no match," turning an
intended default-deny into a 500. Fixed with
`NULLIF(current_setting(...), '')`, which collapses both "never set" and
"reset to empty" to the same `NULL` before the cast. Shipped as a new
migration rather than editing migration 0001 in place, since 0001 had
already been applied — the general rule once a migration has run
anywhere: fix forward, don't rewrite history.

**Why `user_lookup` is synced by a Postgres trigger, not application
code.** Same reasoning that put RLS in the database instead of an
app-layer `WHERE` clause: an invariant that depends on every code path
remembering to do two inserts will eventually drift when some other path
(a seed script, an admin tool, a future bulk import) forgets. A trigger
fires inside the same transaction as the `users` insert automatically,
for any writer, with no way to skip it.

**RLS default-deny mechanics.**
`current_setting('app.current_org_id', true)` — the `true` is
`missing_ok`: it returns `NULL` instead of raising when the setting isn't
present. `org_id = NULL` evaluates to `NULL` (not `true`) under SQL's
three-valued logic, so the row is filtered out. No context set means zero
rows visible, not an error and not every tenant's rows — proven five ways
in `verify_rls.py`: no context, org A context, org B context, an org id
that doesn't exist at all, and a cross-tenant *write* rejected by
`WITH CHECK`.

---

## Slice 3 — JWT auth backend

**Built:** `/auth/signup`, `/auth/login`, `/auth/refresh`, `/auth/logout`,
`/auth/me`; Argon2id password hashing; JWT access tokens (15 min) plus
opaque, rotating refresh tokens stored in Redis (7 days);
`scripts/verify_auth_rls.py` proving auth and RLS are wired together over
real HTTP.

**Why Argon2id over bcrypt.** Current OWASP-recommended default, and
memory-hard — meaningfully more expensive to accelerate on GPU/ASIC than
bcrypt, which has no memory-hardness knob and silently truncates input
past 72 bytes.

**Why login can't just check the password against `user_lookup`
directly.** `user_lookup` is designed to hold zero sensitive data —
putting a password hash there would duplicate credential state across two
tables and double the attack surface for a leak. More importantly, routing
every login through the same RLS-protected `users` table that every other
query uses means there's only one code path for "read user data," not a
fast path that quietly bypasses tenant isolation at the single most
security-critical moment.

**Why refresh tokens live in Redis, not Postgres.** They're ephemeral,
high-churn, single-use-then-replaced — a natural fit for TTL-based expiry
(`EX` on the key) instead of a cron job sweeping expired rows out of a
Postgres table. Also the first real use of the Redis container that had
been running idle since Phase 0. Honest tradeoff: Redis's default
persistence is weaker than Postgres's — an unpersisted restart invalidates
every refresh token and forces a re-login for everyone.

**Why `GETDEL`, not `GET` then `DEL`.** Atomic fetch-and-delete in one
round trip. Without it, two near-simultaneous refresh calls with the same
token could both read it as valid before either deletes it. With it, only
the first caller can ever get a hit — a concurrent second call sees it
already gone.

**Why rotation matters.** Each refresh token is single-use; the moment
it's used, it's deleted and replaced. If a refresh token leaks, an
unrotated long-lived token stays valid for its whole lifetime (days).
A rotated one only stays valid until the legitimate user's *next* normal
refresh — which happens automatically roughly every 15 minutes, since
that's the access token's lifetime. Rotation shrinks the attacker's
window from "the whole refresh token lifetime" to "until the real user's
next request."

**The acknowledged gap: global email uniqueness.** Email is unique across
the whole system, not per-org, specifically to keep login a single lookup
(`user_lookup` → org → password check) instead of needing an org hint
before the database can even be queried. The real cost: nobody can belong
to two orgs under the same email today. Solving that (a join table, or
per-org uniqueness plus an org slug/subdomain hint at login) is deferred,
not solved here.

---

## Slice 4 — JWT auth frontend

**Built:** signup and login pages, an `AuthProvider` React context,
an `apiFetch` wrapper with silent token refresh, a Next.js rewrite proxy,
`proxy.ts` for protected-route redirects, and a minimal `/dashboard`
placeholder page.

**Why httpOnly cookies forced an architecture change, not just a storage
swap.** The frontend (`:3000`) and API (`:8010`) were different origins.
A cookie set directly by the API would be scoped to the API's own origin
and never sent to `:3000` — meaning Next.js middleware, which only sees
cookies sent *to* its own origin, could never read it. Fixed with a
same-origin rewrite proxy (`/api/*` → the API container): the browser
only ever talks to itself, so a `Set-Cookie` in the proxied response lands
on the frontend's own origin. This is what makes both the httpOnly cookie
flow and origin-aware middleware possible at all with two separate
backend/frontend processes.

**Why `apiFetch`'s silent refresh de-duplicates the in-flight refresh
call.** Refresh tokens are single-use and rotate on every call (Slice 3).
If several requests hit a 401 at the same moment and each independently
called `/api/auth/refresh`, only the first would succeed — the rest would
race for an already-rotated-out token, fail, and incorrectly appear
logged out. Sharing one in-flight promise means concurrent callers all
await the same single attempt.

**A requirement tension, resolved rather than silently picked.**
"Redirect any page but /login or /signup when logged out" and "the
homepage shows both logged-in and logged-out states" are contradictory if
taken literally — a strict redirect would mean a logged-out visitor never
reaches the homepage's logged-out branch. Resolved by treating `/` as a
third public path (alongside login/signup) and adding a minimal protected
`/dashboard` placeholder specifically so the redirect logic has something
real to protect and test against.

**Next.js 16 caught a training-data-lag issue in real time.** Testing
produced a deprecation warning: this Next.js version renamed
`middleware.ts` → `proxy.ts` (function renamed `middleware` → `proxy`,
same API). Confirmed against the bundled docs in `node_modules` (per this
project's own `AGENTS.md` instruction to check for breaking changes on a
version newer than training data) and migrated the file rather than ship
deprecated code — also learned the dev server needs a restart to pick up
a brand-new proxy/middleware file; it doesn't hot-reload like page
components do.

---

## Slice 5 — Rate limiting

**Built:** slowapi with Redis-backed storage, a `key_by_user_or_ip`
key function, a global default of 100 requests/minute, a strict
10/minute override (IP-keyed) on `/auth/signup` and `/auth/login`, and a
custom 429 handler with a clear error body.

**Why Redis, not in-memory.** In-memory rate limiting counts requests in
a plain dict inside one process. The moment the API runs as more than one
worker or instance — any real deployment, or even a local `--workers 2`
— each process has its own separate counter, so the effective limit
becomes (configured limit) × (worker count) instead of the configured
limit. Redis gives every process a single shared counter, which is the
entire point of a *rate* limit.

**Why key by user when authenticated, by IP otherwise.** A per-user
budget means several people behind the same office NAT or VPN don't share
(and exhaust) one IP-wide allowance. Falling back to IP for anonymous
requests is also exactly the right key for signup/login, where there's no
user yet to key by — and brute-force protection specifically wants that
budget tied to network origin, not an identity the caller doesn't have.

**The CORS ordering fix.** `CORSMiddleware` has to be the *outermost*
layer (added last), so a 429 raised deep inside by `SlowAPIMiddleware`
still passes back through it on the way out and gets CORS headers.
Otherwise a browser calling a rate-limited endpoint would see an opaque
CORS failure instead of the actual 429.

**Verified empirically, not just configured.** Hammered `/auth/login`
past its limit: exactly 10 requests succeeded, the 11th onward got a
clean 429. Confirmed the same client's separate, more generous limit on
`/auth/me` wasn't affected — then deliberately pushed that endpoint to
105 requests too, confirming exactly 100 succeeded before 429s started,
so the generous limit was proven real rather than assumed untested.

---

## Slice 6 — CI pipeline

**Built:** `.github/workflows/ci.yml` with two parallel jobs (`backend`,
`frontend`), Postgres and Redis as real GitHub Actions service containers,
`verify_rls.py` and `verify_auth_rls.py` as their own clearly labeled
steps, ruff for backend linting, and ESLint/`tsc --noEmit`/`next build`
for the frontend.

**Why `verify_rls.py` and `verify_auth_rls.py` matter more here than
typical test coverage would.** Most test suites verify business logic; a
failure means a feature broke. These two verify a *security boundary* —
that tenant isolation can't silently regress. A failure here would mean
cross-tenant data leakage became possible: the one bug class in this
codebase that's both catastrophic and easy to introduce by accident (one
migration touching the RLS policy, one dependency change skipping
`set_tenant_context`) without anything else noticing. That's why each got
its own labeled CI step instead of being folded into a generic "run
tests" step — a failure here should be unmistakable, never buried in
scrollback.

**Why ruff over flake8.** One Rust-speed tool replacing
flake8+isort+pyupgrade, actively developed, and simple enough to adopt
with almost no existing violations to clean up first.

**Two real issues the pipeline caught before it ever ran on GitHub.**
`eslint-plugin-react-hooks`'s newer `set-state-in-effect` rule flagged
`AuthProvider`'s mount-time fetch as unsafe; fixed by wrapping the call in
an inline async IIFE, verified empirically that this changes how the
linter's static analysis reads the code rather than just suppressing the
warning. Separately, ruff's import-sort rule flagged `auth.py` after the
rate-limiting slice added a new import — auto-fixed with `ruff check
--fix`.

**Validation without a GitHub remote.** No commits existed yet to push
and trigger the real workflow, so every command was instead run verbatim
against throwaway Postgres/Redis containers on an isolated Docker
network — the full migration chain from a genuinely fresh database, both
proof scripts, and the API health-check wait loop — then torn down.
Stronger evidence than "the YAML parses": proof the commands work outside
the existing dev containers, which is the exact failure mode CI exists to
catch.

---

## Slice 7 — Projects CRUD (Phase 1 begins)

**Built:** the `Project` model, migration 0004 (table + RLS in the same
migration), full CRUD (`POST`/`GET`/`GET one`/`PATCH`/`DELETE
/projects`), a rough creator-or-admin authorization check,
`scripts/verify_projects_rls.py`, and a real `/dashboard` project list +
create form replacing the Phase 0 placeholder.

**Contract first.** `Project` (SQLAlchemy) → `ProjectCreate` /
`ProjectUpdate` / `ProjectRead` (Pydantic) → the TypeScript `Project`
interface — all defined before any endpoint logic, so the shape of the
resource was settled before its behavior was.

**`NULLIF` from the start, not rediscovered.** Migration 0004's policy
uses `NULLIF(current_setting(...), '')` immediately, applying the lesson
migration 0003 had to learn the hard way in Slice 2, instead of
reintroducing the same empty-string bug in every new table's RLS policy.

**The `eager_defaults` / `MissingGreenlet` bug — genuinely new, and
reusable beyond this table.** The first real UPDATE through the API
returned a 500, not the updated row. Cause: `projects.updated_at` is
maintained by a Postgres `BEFORE UPDATE` trigger, marked on the
SQLAlchemy model with `server_onupdate=FetchedValue()` so the ORM knows
the column changes server-side. Under sync SQLAlchemy that's enough — the
ORM lazily re-fetches the value on next access. Under **async**
SQLAlchemy, that lazy re-fetch needs an `await`, and FastAPI's response
serialization reads the attribute without one, so the load crashed
instead of just being slow. Fixed with `__mapper_args__ =
{"eager_defaults": True}` on `Project`, which forces the UPDATE statement
itself to `RETURNING` the trigger-modified column, so the in-memory
object is already correct before serialization ever touches it. This
isn't specific to projects: **any future table with a DB-side trigger
touching a column the ORM tracks will hit the same crash** — tickets
(status timestamps, comment counts) are the next obvious candidate, and
this needs to go on them too, not get rediscovered the hard way again.

**404 vs. 403 — two different trust boundaries, two different codes.** A
project ID from another org returns `404`: RLS makes "doesn't exist" and
"exists in another org" indistinguishable at the query level, and the
endpoint deliberately never reveals which. A project that *is* visible
(same org, RLS already let it through) but isn't owned by the caller and
isn't editable by an admin returns `403` instead — existence within your
own tenant isn't sensitive, only cross-tenant existence is.
`scripts/verify_projects_rls.py` proves the first case directly: org A
directly GETs and PATCHes org B's real project ID and gets `404` both
times, then re-reads org B's project to confirm it's genuinely untouched
— not just absent from a list.

**Rough authorization, flagged as rough.** Any authenticated org member
can create a project; only the creator or an org admin can update or
delete it. No per-project roles or sharing yet — noted as future work in
the code, not silently assumed to be enough long-term.

**Proof that Phase 0's wiring holds for a brand-new resource, not just
the auth endpoints it was built alongside.** `GET /projects` has no
`WHERE org_id = ...` anywhere in its code — it's a plain `SELECT * FROM
projects`, and it's still correctly tenant-scoped, because
`get_current_auth` already called `set_tenant_context` before the query
ran. That's the actual payoff of Phase 0's RLS investment: a new resource
type inherits tenant isolation for free just by using the existing
dependency — the one real friction point was an async-ORM/trigger
interaction, not the tenancy model itself.

---

## Slice 8 — Ticket CRUD (epic → story/task → subtask hierarchy)

**Built:** the `Ticket` model, migration 0005 (table + RLS in the same
migration), full CRUD scoped to a project
(`POST`/`GET`/`GET one`/`PATCH`/`DELETE /projects/{id}/tickets`), a
nested `GET /tickets/tree` endpoint, the subtask-parent-type business
rule, `scripts/verify_tickets_rls.py`, and a flat ticket list on the
frontend (later replaced by the Kanban board in Slice 9).

**`eager_defaults` applied proactively this time, and confirmed not
needed the hard way.** Slice 7 hit the async-SQLAlchemy
`MissingGreenlet` crash on `Project`'s first real UPDATE and had to
diagnose it after the fact. `Ticket` has the identical shape — a
trigger-maintained `updated_at` — so `__mapper_args__ = {"eager_defaults":
True}` went on the model from the start this time, not after a 500.
`verify_tickets_rls.py`'s creator-edit check exists specifically to prove
that: it passes clean on the first run, with no crash to rediscover.

**Composite FKs, reapplied without re-explaining them from scratch.**
Tickets are scoped along two dimensions that can disagree — `project_id`
and `org_id` — so `(project_id, org_id) → projects(id, org_id)` is a
composite FK, not a plain one, same reasoning as the RLS-isn't-enough
gap it closes. Tickets also self-reference via `parent_id`, which needs
the same treatment one level deeper: `(parent_id, project_id) →
tickets(id, project_id)`, so a subtask's parent can't silently be a real
ticket from a *different* project in the same org. Both need their own
supporting `UNIQUE(id, org_id)` / `UNIQUE(id, project_id)` constraint on
the referenced side before Postgres will accept them.

**Where the subtask-parent-type rule lives, and why.** A subtask's
parent must be a story or task, never an epic or another subtask. This
is enforced in `app/api/tickets.py`, not as a DB constraint — it's a
type-conditional business rule, not a tenant/scope boundary (RLS and the
composite FKs already own that), and it needs a clean 422 with a real
message rather than a raw constraint-violation error. It's also the kind
of rule likely to evolve once workflow rules become more configurable,
which is much cheaper to change in Python than in a `CHECK` constraint
or trigger.

**A FastAPI route-ordering gotcha.** `/tickets/tree` has to be declared
*before* `/tickets/{ticket_id}` — FastAPI matches routes in declaration
order, and the literal string `"tree"` matches a plain `{ticket_id}`
path parameter just fine at the routing layer; the failed `UUID`
coercion only happens *after* a route is matched, not during matching.
Declared the other way around, `GET /tickets/tree` would 422 as an
invalid ticket id instead of ever reaching the tree handler.

**The tree built once, server-side, not left for every client to
re-derive.** `GET /tickets/tree` groups a project's flat ticket list
into `epic → story/task → subtask` nesting in one O(n) pass over data
already fetched in a single query, and ships the nested shape directly.
The alternative — shipping the flat list and letting each client
(today's web frontend, anything else later) walk `parent_id` chains
itself — means as many places to get the grouping logic right as there
are clients, for a computation that's trivial to centralize.

---

## Slice 9 — Kanban board with configurable workflow states

**Built:** the `WorkflowState` model, migration 0006 (new table + RLS +
a real data migration that backfills default states for every existing
project, remaps `tickets.status` to `workflow_state_id`, and drops the
old status enum entirely), workflow-state CRUD endpoints, default-state
seeding on project creation, a move-ticket path
(`workflow_state_id`/`position` via the existing `PATCH /tickets/{id}`),
a full drag-and-drop board (dnd-kit) on the frontend, a basic settings
page for reordering/renaming states, and
`scripts/verify_workflow_rls.py`.

**`eager_defaults` correctly *not* applied — a real "no," not a reflex
"yes."** Unlike `Project`/`Ticket`, `WorkflowState` has no
trigger-maintained column (no `updated_at` in this table's contract —
reorders go through a plain client-supplied `order` int, not a
server-side timestamp). Adding `eager_defaults` anyway, out of habit
from the last two slices, would've been dead weight with nothing to fix.
The right call here was recognizing the earlier lesson didn't apply, not
reapplying it unconditionally.

**Composite FK, reapplied a third time — plus a third layer.**
`workflow_states.project_id` gets the same `(project_id, org_id) →
projects(id, org_id)` treatment as tickets, for the same reason. Tickets
then gained a *third* composite FK on top of the two from Slice 8:
`(workflow_state_id, project_id) → workflow_states(id, project_id)`, so
a ticket can't be moved into a workflow state that's real but belongs to
a different project in the same org.

**Where default-state seeding lives — a genuine judgment call, not a
copy of the `user_lookup` trigger pattern.** New projects get four
default states (`Backlog`/`In Progress`/`Review`/`Done`) seeded in the
`create_project` endpoint itself, not via a DB trigger. This is the
opposite choice from `user_lookup`, deliberately: trigger-based sync
earns its keep when an invariant must hold no matter which of *several*
code paths writes the parent row (users get created by signup, seed
scripts, and future admin tooling alike). Projects have exactly one
creation path today, so that guarantee isn't buying much yet, while
seeding logic here is product/template configuration (names, ordering,
which column is default) that's realistically going to become
per-org/per-template configurable later — a Python branch to change,
versus rewriting trigger SQL.

**The move-vs-edit authorization split.** Dragging a card
(`workflow_state_id`/`position` only) is open to any org member; editing
what a ticket actually says stays creator-or-admin, same as Projects.
Restricting drag-and-drop to a ticket's creator would mean only the
person who filed a card could ever move it to Done, which breaks the
basic point of a shared board. A request that touches both a move field
and a content field is treated as a content edit — the stricter rule
wins.

**The one real dnd-kit gotcha.** `SortableContext` only creates drop
targets for existing sortable *items*, so a column with zero cards had
nothing to catch a dropped card until `useDroppable` was added to the
column container itself, independent of the cards inside it. Both are
needed together — `SortableContext` for reordering within/between
non-empty columns, `useDroppable` so an empty column is a valid drop
target at all.

**Built plain first, then made optimistic, on purpose.** The board
worked end-to-end over real HTTP (drag → `PATCH` → refetch, no
client-side move before the server confirms) before any optimistic
update was added, so a failure would be easy to isolate to either layer.
The optimistic version snapshots ticket state before applying a local
move, and only restores that snapshot if the `PATCH` comes back
non-`ok` — rollback-on-failure, not a full reload, since the point of
optimism is to avoid the round-trip latency in the first place.

**A Docker anonymous-volume gotcha, hit for the first time here.** A new
page route (the settings page) 404'd even after a plain container
restart — Turbopack's route-manifest cache persists in the `.next`
anonymous volume, which `docker compose restart` doesn't clear. Fixed
with `docker compose up -d --force-recreate -V web`. That flag also
renews the `node_modules` anonymous volume, which silently deleted the
just-installed `dnd-kit` packages in the process — this is now a known
two-step recovery (`--force-recreate -V`, then reinstall any packages
added since the image was last built), not something to be surprised by
again.

---

## Slice 10 — Team invites + member management

**The actual gap being closed.** Every prior signup created a brand-new
org — there was no way for a second person to join an *existing* one.
**Built:** the `Invite`/`InviteLookup` models, migration 0007 (invites
table + RLS + the lookup-table bootstrap pattern) and migration 0008 (a
direct consequence of adding real member deletion —
`projects.created_by`/`tickets.created_by` had to move from `RESTRICT`
to nullable `SET NULL`), a second signup path that joins an existing org
via an invite token, invite CRUD + a public preview endpoint, org-member
list/role-change/removal endpoints with a last-admin-removal guard, a
`/team` settings page, an invite-aware signup page, and
`scripts/verify_invites_rls.py`.

**Opaque token, not a JWT.** Every redemption already has to hit the
database to check `accepted_at`/`expires_at` — business state a signed
token can't safely carry, since there's no clean way to "revoke" a JWT
short of a blocklist. An opaque token's entire security model is "look
it up; found, unexpired, and unaccepted means valid," which is exactly
the rule this feature needs, and it's trivially revocable by deleting
the row. This isn't a new pattern — it's the same choice already made
for refresh tokens back in Slice 3, reapplied rather than reinvented.

**The bootstrap-lookup problem, solved the same way `user_lookup`
already solved it.** Redeeming a token happens before any tenant context
exists, but `invites` carries real RLS like every other org-scoped
table. `invite_lookup` — a minimal, RLS-free `token → org_id` table kept
in sync by an `AFTER INSERT` trigger — is structurally identical to how
`user_lookup` lets login find an org before a tenant context can be set.
Deliberately minimal: no email, no role, nothing worth stealing beyond
what possessing the token itself already grants.

**No composite FK on invites — checked, not assumed.** Tickets and
workflow states needed composite FKs because they're scoped along *two*
dimensions that could independently disagree (a real project_id from
the *wrong* org). An invite is only ever org-scoped — one dimension,
same shape as `users` or `projects` themselves — so a plain single-column
`org_id` FK is the correct, sufficient guarantee. There's no second
scope for it to drift out of sync with.

**The last-admin-removal guard, generalized past the literal ask.** An
org that reaches zero admins has no way back — every admin-gated action
becomes permanently unreachable through the API. The brief specifically
named blocking *removal* of the last admin, but demoting them away from
`admin` has the exact same lockout failure mode, so both `DELETE
/org/members/{id}` and `PATCH .../role` share one guard
(`_ensure_not_last_admin`), not just the one path that was asked for.

**What happens to a removed member's tickets and projects.** Kept, not
deleted, with `created_by` set to `NULL` (migration 0008). The
alternative — cascading the delete — would silently destroy a departed
teammate's entire work history over an org-membership change, which is
a far more surprising and destructive default than losing attribution.
`_require_owner_or_admin` already treats a `NULL` creator as "not mine,"
so an orphaned resource correctly falls back to admin-only edits with no
extra code.

**An acknowledged gap, flagged rather than silently shipped or silently
fixed.** A removed member's still-live access token isn't revoked — it
simply expires naturally within 15 minutes, since there's no reverse
index today from `user_id` to that user's outstanding Redis
refresh-token keys to walk and delete. Worth closing later; genuinely
out of scope for this slice.

**Two real bugs, found by actually running it, not caught by review.**
`Invite.expires_at`/`accepted_at` are the first columns in this codebase
where a Python-computed, timezone-aware `datetime` gets bound directly
into an `INSERT` — every other timestamp column is DB-computed via
`server_default`/a trigger, so this exact failure mode never had a
chance to surface before. `Mapped[datetime]` alone infers a naive
column type, which asyncpg flatly rejected against the migration's real
`TIMESTAMP WITH TIME ZONE` column ("can't subtract offset-naive and
offset-aware datetimes") — fixed by declaring `DateTime(timezone=True)`
explicitly on both columns. Separately, refactoring `signup()` to branch
on an invite token left a stale reference to the old `org` variable in
the final `_issue_token_pair` call, an `UnboundLocalError` on the
invite-redemption path specifically (the new-org path never touched that
line). Both were caught by actually exercising the endpoints in Docker
before writing the proof script, not by inspection.
