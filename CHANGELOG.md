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

---

## Slice 11 — Catching up CI, CHANGELOG, and README for Slices 8–10

Tickets, the Kanban board, and Invites/Team management were each built
and manually verified in their own turn, but — per this project's
established discipline of pushing (and updating CI/CHANGELOG) as an
explicit, separate step — none of it had been wired into CI or
documented yet. This slice closes that gap for all three at once.

**A rate-limit collision, found by actually running the full sequence,
not assumed safe.** `/auth/signup` is rate-limited to 10/minute per IP.
Every proof script signs up multiple orgs over real HTTP from the same
runner IP; individually that's fine, but stacked back-to-back in one CI
job the signup counts compound on one shared budget. Auth + Projects
alone already used 4; adding Tickets (2) + Workflow (2) + Invites (7,
the most signup-heavy script here, since it exercises tampering, replay,
and email-mismatch paths that each cost a signup attempt) would push the
running total to 15 in the space of a few seconds — over the limit
deterministically, not flakily, since CI runs these steps far faster
than the 60-second window they'd need to spread across. This doesn't
test the rate limiter itself (no proof script asserts anything about
it), so resetting Redis (`FLUSHALL`) between the three new steps doesn't
weaken any real security proof — it just stops unrelated proof scripts
from cross-talking on a budget that only exists because of how CI
happens to run them in sequence. Verified by actually running all six
scripts back-to-back locally with the same resets CI now uses, with no
artificial waits, before trusting the CI YAML to work.

---

## Slice 12 — Hardening the invites/auth slice: two real bugs, one of
them a genuine authorization vulnerability

Team invites + member management (Slice 10) was manually verified end to
end — including the session-revocation behavior below — before this
slice closes it out. This slice is a distinct, later pass: going back
over the auth path Slice 10 touched and asking "is this actually safe,"
not part of the original build.

**Closing Slice 10's own acknowledged gap: a removed member's refresh
tokens are now revoked.** Slice 10 shipped with this explicitly flagged
as out of scope — a removed member's outstanding refresh token was left
to expire naturally over its full 7-day lifetime, meaning they could
keep minting fresh 15-minute access tokens for up to a week after being
removed. Closed with a reverse index in Redis
(`refresh_tokens_by_user:{user_id}`, a set of that user's live tokens,
maintained alongside the existing per-token keys and TTL-refreshed on
every new token issued) — `DELETE /org/members/{id}` now walks that set
and kills every outstanding token for the removed user in one shot. The
set can hold a few already-expired token strings between refreshes
(Redis key expiry doesn't fire a callback to clean up a set that
references it), which is harmless: deleting an already-gone key is a
no-op, not an error.

**Argon2 was blocking the event loop — found in an earlier code review,
fixed now.** `hash_password`/`verify_password` were synchronous calls
made directly inside async route handlers. Argon2 is deliberately slow
and memory-hard (the entire point of it as a password hash), so calling
it un-awaited on the event loop blocks every other concurrent request
being served by that worker for the duration — this was observed
firsthand as `/health` hanging for 10+ seconds under load during earlier
testing. Fixed by making both functions `async` and wrapping the
argon2-cffi calls in `starlette.concurrency.run_in_threadpool` —
**inside `security.py` itself, not at the call sites**, so the fix can't
be silently undone by some future caller forgetting to wrap it. Also
widened `verify_password`'s exception handling from just
`VerifyMismatchError` (wrong password — the expected case) to also
catch `VerificationError`/`InvalidHashError` (a malformed or corrupted
stored hash), so that case fails closed as "not verified" instead of
propagating as an unhandled 500 — the same default-deny instinct behind
RLS: an ambiguous auth state should never resolve to "let them in," and
it shouldn't crash the request either.

**The important one: `POST /auth/refresh` was trusting a stale, cached
role instead of re-checking the database — a real authorization bug,
not a style nit.** This was found by accident, while wiring up the
token-revocation fix above: tracing through what data `refresh()`
actually used to reissue an access token revealed it never touched the
database at all. A refresh token's Redis payload stores the user's role
*at the moment it was issued* (`{"user_id":..., "org_id":..., "role":
...}`), and `refresh()` was reading `role` straight out of that cached
payload — then, because every rotation calls `store_refresh_token` with
that same `role` value, the stale role got faithfully copied forward
into the *next* token's payload too, on every single refresh, forever.

**Why this matters more than it might look at first.** Access tokens
already carry an accepted, bounded staleness window — a JWT is valid for
15 minutes no matter what changes server-side in the meantime, and
that's a deliberate, documented tradeoff of stateless tokens. This bug
was different in kind, not degree: refreshing is the mechanism that's
*supposed* to bring a session back in line with reality every 15
minutes, and it wasn't. Concretely — an org admin uses `PATCH
/org/members/{id}` to demote a compromised or departing admin down to
`member`. Their current access token still has 15 minutes of admin
rights left, which is expected and fine. But the moment that token
expires, their browser silently calls `/auth/refresh` — and the old code
would hand back a **brand new access token with `role: "admin"` again**,
read straight from the stale refresh-token payload, with no database
check in between. As long as they kept using the app (refreshing every
~15 minutes, same as any normal active session), they would have kept
their admin privileges **indefinitely** — the demotion would never
actually take effect until they explicitly logged out and back in, which
nothing in the product prompts anyone to do. That's a real privilege-
persistence vulnerability: an authorization decision made by an admin
through the UI silently failing to apply to an already-issued session.

**The fix.** `refresh()` now does exactly what `login()` already does a
few lines above it in the same file: `set_tenant_context` using the
`org_id` from the refresh token payload (that part's still trusted — it
doesn't change on a role edit), then a real `SELECT` against `users` for
the *current* role, through the same RLS-protected path every other
query in this app goes through. If the user no longer exists (e.g. the
revocation above somehow didn't catch them — belt and suspenders), the
refresh is rejected outright. The staleness window for a role change is
now bounded by the same 15 minutes as everything else in this system,
not "however long the user keeps using the app."

**Verified, not just fixed.** `verify_invites_rls.py` gained two new
checks specifically for this pass: refreshing with a token issued
*before* a promotion now correctly comes back with the *new* role, and a
removed member's original refresh token is rejected with `401` rather
than silently honored. Both fail against the old code and pass against
the new code — confirmed by running them, not inferred from reading the
diff. All six proof scripts were re-run together afterward with no
regressions.

**One more thing checked, not assumed: does a `NULL` `created_by` (a
removed member's old tickets, from Slice 10's `SET NULL` migration)
render sensibly on the board?** Checked by grepping the actual frontend
rather than guessing — `createdBy` is referenced only in `lib/types.ts`
(the type definitions and API-response mappers); it is never rendered
anywhere in the UI. The Kanban card shows only a ticket's type and
title, and there's no ticket detail view yet. So there's nothing to
break — not because `NULL` is handled gracefully, but because the field
isn't surfaced in the UI at all yet, a pre-existing gap rather than a
new one this migration introduced.

---

## Slice 13 — Ticket assignee

**Built:** `tickets.assignee_id` (migration 0009), folded into the
existing `PATCH /tickets/{id}`, an assignee picker + initials badge on
the Kanban card, and three new checks in `verify_tickets_rls.py`.

**The composite-FK question, answered the same way a fourth time —
worth stating as the general rule, not just the specific case.** RLS
scopes rows in the table it's defined on; it says nothing about what a
foreign key on that row happens to point at in some *other* table. A
plain `assignee_id: ForeignKey("users.id")` would let a ticket in org A
be assigned to a real `user_id` from org B — a legitimate row, just in
the wrong tenant — and RLS on `tickets` would never notice, because it
only ever checks `tickets.org_id`, never the org of whatever the row's
other columns reference. This is exactly the same gap
`fk_tickets_project_org`, `fk_tickets_parent_project`, and
`fk_tickets_workflow_state_project` already closed, applied here a
fourth time: `(assignee_id, org_id) -> users(id, org_id)`, backed by a
new `uq_users_id_org_id` constraint the same way the earlier ones needed
their own supporting unique constraint on the referenced side. The
general rule this keeps confirming: **any column that references a row
in another table, where that table is itself tenant-scoped, needs a
composite FK pinning both rows to the same org — a single-column FK is
never enough on its own once RLS is the thing doing the tenant
isolation.** Org-scoped rather than project-scoped this time, unlike
`workflow_state_id`: this app has no per-project membership, so any
member of the ticket's org is a valid assignee for any ticket in any of
that org's projects — one dimension, not two.

**Folded into the existing `PATCH /tickets/{id}` rather than a new
`PATCH /tickets/{id}/assignee` route.** A dedicated endpoint would
duplicate `_get_ticket_or_404`, the 404-vs-403 handling, and response
serialization for no real benefit — `TicketUpdate`'s `exclude_unset`
PATCH semantics already express "change just the assignee" cleanly as
`{"assignee_id": "..."}`, the same way they already express "just move
this card" as `{"workflow_state_id": ..., "position": ...}`. One
flexible endpoint with field-based authorization branching is the
established idiom here, not a REST-purity split.

**Authorization: any org member, not creator/admin-only.** Assigning a
ticket — picking up an unowned one, or handing yours to a teammate — is
ordinary team behavior, not a privileged action; gating it behind
creator-or-admin would mean only the person who happened to file a
ticket could ever hand it to someone else, which breaks the basic point
of a shared board the same way restricting drag-and-drop to a ticket's
creator would have. `assignee_id` joins `workflow_state_id`/`position`
in what used to be called `_MOVE_ONLY_FIELDS` — renamed
`_COLLABORATIVE_FIELDS`, since "move-only" stopped being an accurate
name for the set the moment a third, non-move field joined it. Editing
what a ticket actually *says* (title/description/type/parent) still
requires creator-or-admin; assigning it doesn't.

**Member removal: `SET NULL`, consistent with `created_by`, no reason
to diverge.** Same reasoning as migration 0008 — a removed member's
assigned tickets are kept, just unassigned, not destroyed or blocked
from being removed. The frontend renders this as "Unassigned" (the
picker's empty option, and a plain `?` in the initials badge) rather
than leaving a blank or broken-looking card.

**A new dnd-kit gotcha, worth flagging as a general pattern, not a
one-off.** The Kanban card's outer `<div>` carries dnd-kit's drag
listeners (`{...listeners}`, spread there since the whole card is
draggable), and the assignee `<select>` is a child of that div. Without
`onPointerDown={(e) => e.stopPropagation()}` on the select, opening the
dropdown registers as a pointer-down on the card underneath it too —
dnd-kit reads that as the start of a drag gesture. This isn't specific
to `<select>`: **any future interactive control added directly onto a
draggable card — a button, a checkbox, a text input — will need the
same `stopPropagation` on pointer-down**, or it'll either fight the drag
gesture or get swallowed by it. Worth remembering as a checklist item
the next time something interactive lands on a card, not rediscovering
it fresh.

**Verified, not just built.** Three new checks in
`verify_tickets_rls.py`: assigning within the same org succeeds,
assigning to a real user from a *different* org is rejected (422,
caught by `_validate_assignee` before it ever reaches the DB's composite
FK), and unassigning (`assignee_id: null`) succeeds without triggering
assignee validation at all. Full six-script regression re-run
afterward, plus `ruff check .` run locally this time before pushing —
the ruff failure from the previous slice's first CI run was exactly the
kind of thing that check would have caught before the push, not after.

---

## Slice 14 — Automated security scanning in CI

**Built:** a new `secret-scan` job (gitleaks, full git history) and a
`pip-audit`/`npm audit` step in the backend/frontend jobs respectively —
plus the actual first scan's real findings, dealt with rather than just
reported.

**pip-audit: fails on any finding, no carve-out.** Backend dependencies
here are a young, actively-maintained set with no deep vendored-tooling
layers — every one of the 21 findings the very first run turned up (see
below) had a real fix version available, none were the "no patch exists
yet" case this slice was explicitly asked to reason about. Strict-by-
default is the right call for as long as that keeps being true; the
day a genuinely unfixable one shows up, the answer is an explicit,
commented `--ignore-vuln <ID>` in the CI step — a reviewed exception,
not a quiet retreat to warn-only that nobody would notice going stale
six months later.

**The first real scan found 21 vulnerabilities across 3 packages, and
they were fixed, not just logged.** `pyjwt==2.10.1` (multiple PYSEC
advisories, the library that signs and verifies every access token in
this app), `starlette==0.41.3` (FastAPI's transitive ASGI layer,
several CVEs), and `pytest==8.3.4` (dev-only). Fixed by bumping
`pyjwt` to `2.13.0`, `pytest`/`pytest-asyncio` to `9.1.1`/`1.4.0` (the
old `pytest-asyncio==0.25.0` pin doesn't support pytest 9, a real
version-compatibility wrinkle discovered mid-upgrade, not anticipated),
and `fastapi` to `0.139.0` — starlette isn't a direct pin in
`requirements.txt` at all, it's pulled in transitively by FastAPI, so
clearing its CVEs meant bumping FastAPI far enough that pip's resolver
picks a patched starlette (`1.3.1`) on its own. That's a large jump
(FastAPI `0.115.6` to `0.139.0`, over twenty minor releases, starlette
`0.41.3` to `1.3.1`, a major-line jump) with real breaking-change risk
on paper — verified safe empirically, not assumed: the full six-script
proof-script regression, `ruff check .`, and a bare `pytest -v`
collection run all passed clean against the upgraded stack before this
was trusted.

**npm audit: `--audit-level=high`, not the default (fails on anything,
including low/moderate) — and there's a real, current finding
demonstrating exactly why, not a hypothetical.** Next.js `16.2.10` (the
latest *stable* release as of this scan) bundles `postcss@8.4.31`
internally, which has a moderate XSS advisory
(GHSA-qx2v-qp2m-jg93). `npm audit fix --force`'s suggested fix is to
downgrade to `next@9.3.3` — a multi-year regression, not a fix; no
stable Next.js release has bumped its internal postcss yet, only
canary/preview builds have, which aren't safe to run in production.
The actual exploitability is low regardless of severity label: this
postcss instance only ever compiles this project's own authored
Tailwind source at build time, never untrusted CSS supplied at
runtime, which is the scenario the advisory actually describes.
**Decision: accepted and tracked, not silently ignored** — noted
directly in the CI step's own comment so it's visible at the exact
point it's being allowed through, with a note to revisit once Next.js
ships a stable release with a patched postcss. A HIGH or CRITICAL
finding anywhere in the tree still fails this step; only this one
specific moderate finding is why the threshold isn't the npm default.

**gitleaks, run across full git history, not just the current
tree — and it came back genuinely clean.** `fetch-depth: 0` on checkout
is required for this to mean anything: the default shallow, single-
commit clone would let gitleaks scan only the latest commit, silently
never touching the other seventeen. Run locally first exactly as CI
will run it before trusting the wiring: **18 commits, ~536KB scanned,
zero leaks found.** No `.env` contents, no real JWT secret, no database
credentials anywhere in this public repo's history — the git-ignore
discipline around `.env` held, and the intentionally-fake CI/dev-only
values (`wrkbase_ci_password`, `ci-test-secret-do-not-use-outside-ci`,
`correct horse battery staple` as a proof-script test password) don't
match gitleaks' structured-secret detection patterns, which is correct,
expected behavior for values that were never real credentials in the
first place — not a false negative to worry about.

**Fails on any finding, no carve-out, and this one needed no argument
either way — it just doesn't have the CVE ambiguity to be lenient
about.** A dependency CVE is theoretical until proven exploitable
against how the vulnerable code is actually used, and a fix may
genuinely not exist. A leaked secret has none of that ambiguity: the
moment it's in git history — especially a *public* repo's history —
it has to be treated as already compromised, independent of whether
the specific finding "looks" exploitable. A known false positive (a
placeholder value in a docs example) gets an explicit, reviewed
`.gitleaks.toml` allowlist entry if one is ever actually needed, not a
softened default threshold that would let a real leak blend into
routine noise.

**The upstream `zricethezav/gitleaks` Docker image, invoked directly
via a plain `docker run` step — not the `gitleaks/gitleaks-action`
marketplace action.** Its current licensing terms weren't something
that could be verified with confidence from here, and a raw `docker://`
container-action step's `args`-nesting behavior wasn't worth risking
getting subtly wrong in YAML with no local way to test it before
pushing. A plain `run:` step invoking the exact command already run
and verified locally has no such uncertainty — same image, same flags,
nothing trusted blind.

---

## Slice 15 — Soft-delete for projects and tickets

**Built:** `deleted_at` (migration 0010) on both tables, every read path
updated to exclude soft-deleted rows by default, `DELETE` endpoints
that set `deleted_at` instead of removing the row, a `POST
.../restore` endpoint for each resource with the same authorization as
delete, and proof-script coverage for all of it. Groundwork for the
audit-log work coming next: "this was deleted" becomes a fact that can
be logged and undone, not an event that erases its own evidence.

**The composite-FK question — a foreign key can't see `deleted_at` at
all, so the answer has to live somewhere else.** A Postgres FK
constraint only ever asserts "a row with this key exists" — it
references a unique/PK constraint, not an arbitrary predicate, so
there's no way to write `REFERENCES projects(id, org_id) WHERE
deleted_at IS NULL`. `fk_tickets_project_org` and
`fk_tickets_parent_project` stay exactly as they were in migrations
0005/0006, unmodified — structurally, nothing stops a ticket's
`project_id` or `parent_id` from pointing at a row that's since been
soft-deleted. The composite FK's job was never "block references to
deleted rows"; it was always "block references to rows in the *wrong
org/project*," and that job is unaffected. Whether a soft-deleted
project/parent can be validly *referenced going forward* is answered at
the app layer instead — and it turns out to need no new logic at all:
`_get_project_or_404` and `_validate_parent`'s existence check both
already filter `deleted_at IS NULL` (see below), so a soft-deleted
project or parent simply fails to turn up in those same queries,
producing the exact same 404/422 a genuinely nonexistent id already
would. The exclusion filter and the "can't reference a deleted row"
rule are the same mechanism, not two things to keep in sync.

**Where "exclude soft-deleted by default" lives — the genuinely new
decision, and it comes out differently than RLS.** Explicit
`.where(Model.deleted_at.is_(None))` in each read-path query — mostly
concentrated in the small number of chokepoint helpers
(`_get_project_or_404`, `_get_ticket_or_404`, `_validate_parent`) every
endpoint already funnels through, plus the two/three queries
(`list_projects`, `list_tickets`, `get_ticket_tree`) that don't. Not a
database-level mechanism the way RLS is. The RLS argument was: don't
trust every query, present and future, to remember a `WHERE org_id =
...` clause, because forgetting it even once is a cross-tenant data
leak — a trust/compliance failure, and RLS's rule has to be
*unconditional*, applying to literally every query against that table
with zero legitimate exceptions. Soft-delete doesn't share either
property. First, the stakes are lower: forgetting the filter means a
user briefly sees their *own* org's own data in a stale state
("why is my deleted ticket still showing") — a correctness bug, not a
trust boundary crossed. Second, and more structurally decisive: **the
filter needs a legitimate, deliberate exception**, and RLS-style
policies don't have a clean way to express one. The restore endpoint's
entire job is finding a row *specifically because* it's soft-deleted —
if this filter lived as an unconditional DB policy the way RLS does,
restore would need its own bypass mechanism (a second policy, a
role check, a session flag mirroring `app.current_org_id`) just to
counteract the first policy, real added complexity to solve a
lower-stakes problem. An explicit per-query `.where()` clause instead
just doesn't get added on the one call site that deliberately doesn't
want it (`_get_project_or_404(..., include_deleted=True)`), no
counter-mechanism required. This is genuinely a different answer than
RLS got, not RLS's reasoning quietly reapplied — and it's not a novel
pattern for this codebase either: project_id scoping within an org was
already handled the same explicit, app-layer way (backed by composite
FKs for structural correctness), for the same reason — RLS's
"unconditional, no exceptions" bar doesn't fit every scoping problem
this app has, only the tenant-isolation one.

**The children-exist check's *mechanism* changed even though its
*outcome* didn't.** Under hard-delete, `fk_tickets_parent_project` is
`ON DELETE RESTRICT` — attempting to delete a ticket with children
raised a real `IntegrityError`, caught and turned into a 409. That
entire mechanism stops working under soft-delete: soft-deleting a
ticket never issues a `DELETE` statement, so the FK's `RESTRICT` clause
never has anything to fire on. The check has to become an explicit,
proactive query instead (`_has_active_children`, checking for
non-deleted children specifically), run *before* setting `deleted_at`,
not reacted to after the fact. Still blocks the exact same case for the
exact same reason: soft-deleting a ticket with live children would
leave `/tree` rendering children whose parent has vanished from the
default view. A ticket whose only children are *already* soft-deleted
is fine to soft-delete — proven directly in the proof script (delete
the leaf subtask, then the previously-blocked epic delete succeeds).

**Project archiving: decided to be a genuinely separate concept from
soft-delete, not built in this slice.** Mechanically they look
identical — hide by default, need an explicit action to undo — which
makes reusing `deleted_at` tempting. The reason not to: `deleted_at` is
this feature's *cleanup-eligibility* signal — the audit-log/retention
work this slice explicitly sets up for will eventually want to answer
"what's been soft-deleted long enough to purge for good," and an
archived project is never supposed to be eligible for that, no matter
how old. Reusing the same column would mean "archive" and "mark for
eventual deletion" become indistinguishable to any future retention
job, a foot-gun that wouldn't show up until that job actually ships and
starts purging things nobody meant to lose. `deleted_at` says *this
might go away*; archiving needs to say *this is done, keep it
forever*, and those are different enough claims to deserve different
columns whenever archiving actually gets built. Not building it now:
the ask here was to decide whether it overlaps with soft-delete, not to
ship the feature, and adding an `archived_at` column with no endpoint
using it yet would just be dead schema sitting around unused — the
same "don't add reflexively" instinct that kept `eager_defaults` off
`WorkflowState` in an earlier slice.

**Verified, not just built.** Both proof scripts gained soft-delete
coverage: excluded from get/list/tree for the *same* org (not just
hidden from other orgs, which was already true and proves nothing new
about soft-delete specifically), the children-exist check still
blocking with the new mechanism, restore actually working, restoring
an already-active resource rejected with a clean 400 instead of a
silent no-op, and — the part with real regression risk, since it's
brand-new code with no prior cross-org coverage at all — a cross-org
`restore` attempt still 404ing exactly like get/patch/delete already
did. Soft-deleting a project was also proven to make its tickets
unreachable as one unit (`GET .../tickets` 404s once the project itself
is soft-deleted) without touching the tickets' own `deleted_at` at
all — restoring the project alone brings them straight back, which is
the archiving-shaped behavior this slice deliberately isn't building a
separate feature for yet. Full six-script regression, `ruff`, and
`pip-audit` all re-run clean afterward.

---

## Slice 16 — UI/UX redesign: a real design system, a global theme
toggle, and per-org ticket numbering

**Built:** Every screen (homepage, login, signup, dashboard, Kanban
board, workflow settings, team) restyled against one shared token
system instead of default Tailwind, plus a persistent app shell, a
real marketing landing page, and a project-wide dark/light theme
toggle. This slice is mostly styling, but three things in it are new
mechanisms, not new colors — the route restructuring, the ticket-key
schema addition, and the theme system — and those are what's actually
worth documenting; the per-page visual changes speak for themselves.

**The token system, exact values.** CSS custom properties on `:root`
(light, the fallback), re-declared under `:root[data-theme="dark"]`
and `:root[data-theme="light"]` (see the theme-toggle section below
for why those exist alongside a plain media query), mapped into
Tailwind's utility namespace once via `@theme inline` so components
use plain classes (`bg-surface`, `text-ink-secondary`) that are
already theme-reactive — no `dark:` variants anywhere in the
component tree.

- Backgrounds: `--bg-base` `#f2f3f5` / `#14171c`, `--bg-surface`
  `#ffffff` / `#191d24`, `--bg-surface-2` `#f8f9fb` / `#1f2530`,
  `--bg-hover` `#ececf1` / `#242a35`.
- Borders: `--line-subtle` `#e4e6ea` / `#262b34`, `--line` `#d4d7dd` /
  `#323944`, `--line-strong` `#b7bcc5` / `#454e5c`.
- Text: `--ink` `#1b1f26` / `#e7eaef`, `--ink-secondary` `#5c6472` /
  `#99a2b0`, `--ink-tertiary` `#848d9b` / `#616b79`.
- One accent (desaturated amber, not a saturated brand blue): `--accent`
  `#b8823c` / `#d9a25b`, plus `-hover`/`-active`/`-on`/`-subtle`/
  `-subtle-text` variants for buttons and highlighted states.
- Muted semantic colors, deliberately a *different* hue family from
  the accent so an AI-touchpoint highlight (accent) is never confused
  with a status color: `--success`, `--info`, `--warning`, `--danger`,
  `--neutral`, each with a paired `-bg` for chip backgrounds.
- Four muted ticket-type colors (`--type-epic/story/task/subtask` +
  `-bg`), used only by the board's `TypeBadge`.
- Radii: `--radius-sm` 4px, `--radius-md` 6px, `--radius-lg` 10px —
  Linear-density rounding, not the full-pill buttons default
  shadcn/Tailwind demos tend toward.
- Type: IBM Plex Sans for UI copy, IBM Plex Mono for anything that's a
  *value* — ticket ids, timestamps, status codes — both self-hosted via
  `next/font/google`, picked deliberately over Inter/Space Grotesk
  (flagged in the design pass as the current "safe default" look most
  AI-assisted design output converges on).

**Route restructuring: a persistent shell where there was none.**
`dashboard/`, `projects/[projectId]/`, and `team/` moved under a new
`app/(shell)/` route group with its own `layout.tsx` — a route group
changes nothing about the URLs, only which layout wraps which pages.
Before this slice there was no persistent chrome at all: every page
was its own island, no nav, no way to tell which org's data you were
looking at short of reading it off the page content. The shell adds a
sticky top bar with a nav, and — the one deliberately "always visible"
element — an org-identity chip (initials + org name) that's meant to
double as a quiet reminder of the tenant-isolation pitch: you're
always looking at *one* org's board, and the UI never lets that fact
scroll out of view.

**Ticket numbering (`organizations.ticket_prefix`/`next_ticket_number`,
`tickets.ticket_number` — migration 0011): a real schema addition, not
a styling decision, that this slice needed and built.** The board
redesign wanted to show a ticket's key next to its title
(`WRK-142`-style), and the honest options were: derive something
fake from the UUID (a truncated id that looks like a key but means
nothing and isn't stable under re-sorting), or add a real per-org
sequence. Asked directly, chose the real one. `ticket_prefix` is
*stored* on the organization at signup, not derived live from
`org.name` on every read — there's no org-rename endpoint today so
the two are equivalent right now, but a ticket's displayed key is
meant to be permanent once assigned, and deriving it live would
silently change every existing ticket's key the moment renaming ships.
`next_ticket_number` is incremented with a single atomic statement —
`UPDATE organizations SET next_ticket_number = next_ticket_number + 1
... RETURNING next_ticket_number - 1` — relying on Postgres's normal
row-level locking to serialize concurrent ticket creation in the same
org; no separate application-level lock needed, and no read-then-write
race the way `SELECT next_ticket_number` followed by a separate
`UPDATE` would have. Proof-script coverage added specifically to catch
the failure mode a *shared* counter would produce: two brand-new orgs'
first tickets both landing on `1`, not `1` and `2`.

**A real theme system, not just a media query.** The app already had
`@media (prefers-color-scheme: dark)` before this slice, but that's
read-only — a visitor's OS setting, with no way to override it short
of changing their OS. This slice adds `ThemeProvider`
(`lib/theme-context.tsx`), a small hand-rolled context (not a
dependency — the whole job is "read one `localStorage` key, default to
dark, write it back on toggle," not enough surface to justify pulling
in a library for) that stamps `data-theme="dark"` or `data-theme="light"`
onto `<html>`. `globals.css` gained explicit `:root[data-theme="dark"]`
/ `:root[data-theme="light"]` blocks alongside the existing media
query — an attribute selector on `:root` is more specific than a bare
`:root` inside `@media`, so the manual override always wins regardless
of OS preference, in both directions. An inline script in
`app/layout.tsx`'s `<head>` — plain JS, no library — reads the stored
preference (or defaults to `dark`) and sets the attribute *before*
hydration, so there's no light-then-dark flash on load the way a
purely React-driven toggle would produce. Dark is now the deliberate
product default everywhere (previously only true if the visitor's OS
happened to be set to dark) — light is one click away via the toggle
now living in both the app shell's top bar and the landing page hero.

**The blueprint-grid texture, applied project-wide, deliberately
faint.** A tiled 44px grid-line pattern (`--grid-line`, ~5–6% opacity)
layered alongside each screen's base background color via a `bg-grid`
utility class. Applied everywhere — the shell, login, signup, the
landing page — not just the marketing hero, because the ask was one
consistent visual identity across the whole product, not a
marketing-only flourish. Kept intentionally faint specifically because
it has to sit behind dense card grids on the Kanban board without
competing with the tickets on top of it — this is the same restraint
argument as the rest of the design direction (Jira's heaviness is the
thing being avoided, not replaced with a different kind of visual
noise).

**The landing page didn't exist before.** The root route was
previously a bare "sign up / log in" placeholder. It's now a full
marketing page — hero with a live-style board mockup built from the
same real components and tokens as the actual board (not a
screenshot), a feature grid grounded in what's actually shipped (RLS,
soft-delete, real ticket ids, CI scanning — no invented features),
and a security section documenting the actual enforcement mechanisms
by name. Logged-in visitors hitting `/` are redirected straight to
`/dashboard`, same pattern as Linear/Notion — the landing page is
strictly for logged-out visitors.

**Verified.** ESLint, `tsc --noEmit`, and a full `next build` all
clean. Two real bugs caught along the way, not glossed over: an
import-path depth miscount on the workflow-settings page after the
route move (`../../../../lib/api` needed to become
`../../../../../lib/api` — caught by `tsc`, not by inspection), and a
bad `Edit` that duplicated a JSX block while escaping quotes on the
landing page (caught by re-reading the file before it ever reached
lint). Because the board's `Card`/`Column` components were
restructured, not just recolored — `Card` gained a `ticketKey` prop
and a `TypeBadge`, hover/drag states were rewritten against new
tokens — this slice's regression pass re-confirms drag-and-drop itself
still works end to end, not just that the new styles compile.

---

## Slice 17 — Phase 1c: Backlog + Sprints + story points

**Built:** A `sprints` table (migration 0012) with the same org-scoped-
AND-project-scoped shape as `workflow_states` — RLS plus a composite
`(project_id, org_id)` FK — and `tickets.sprint_id`/`story_points`.
Sprint CRUD, `start`/`complete` status-transition actions, a bulk
backlog-to-sprint assignment endpoint, a paginated backlog view, and a
server-computed `total_points` per sprint. Frontend: a sprints index,
a backlog page with checkbox multi-select, and a two-pane drag-and-
drop planning view. Built contract-first, same discipline as every
slice since Tickets: Pydantic schemas and the matching TypeScript
interfaces were written and reviewed before any endpoint logic
existed to implement them.

**Single-active-sprint: a genuinely third category, not RLS's
reasoning or soft-delete's reasoning reapplied.** The rule — at most
one sprint per project can be `active` — is enforced by a partial
unique index (`CREATE UNIQUE INDEX ... ON sprints (project_id) WHERE
status = 'active'`), not an app-layer check-then-write. Working
through why against the two precedents this app already has:

- RLS's shape is *an unconditional trust boundary with zero
  legitimate exceptions*, and the risk it defends against is a future
  code path forgetting to check at all — which is why it has to live
  in the database as a policy nothing can accidentally bypass.
- Soft-delete's shape is *a business rule with exactly one legitimate,
  deliberate exception* (restore), which is why an app-layer
  `.where()` fit — it can express "except here" cleanly, and a
  blanket DB policy can't.
- Single-active-sprint fits neither. There's no legitimate exception
  the way restore is one — two active sprints in a project is never
  correct, full stop. But it also isn't primarily a "don't trust every
  future query" problem the way RLS is. It's a **plain uniqueness
  invariant with a real concurrency hazard**: two nearly-simultaneous
  "start sprint" requests for two different sprints in the same
  project. An app-layer check (`SELECT` for an existing active sprint,
  then `UPDATE`) is a textbook time-of-check-to-time-of-use race —
  both requests can pass the check before either commits, and both
  end up active. Only a constraint Postgres enforces atomically at
  write time closes that, regardless of how the two transactions
  interleave.

Proven as a race, not asserted sequentially:
`scripts/verify_sprints_rls.py` fires two `start` calls at two
different sprints in the same project via `asyncio.gather`, then
asserts the status codes are exactly one `200` and one `409`, and
that a follow-up `GET` shows exactly one sprint actually active. A
sequential "call start twice and check the second one fails" test
would have passed even if the constraint were missing entirely — it
wouldn't have exercised the interleaving that's the actual risk here.
`start_sprint` catches the resulting `IntegrityError` from the losing
request and turns it into a clean `409`, same shape as the `IntegrityError`-to-409
translation this app has used before soft-delete replaced it for
tickets.

**Completing a sprint returns unfinished work to the backlog — with a
named, deliberate limitation.** Backlog is defined simply as
`sprint_id IS NULL`. `complete_sprint` sets `sprint_id = NULL` on
every ticket in the sprint that is *not* in the project's terminal
workflow column, so unfinished work is immediately eligible for the
next sprint's planning without anyone having to notice and move it by
hand. A ticket that *did* reach the terminal column keeps its
`sprint_id` pointing at the now-completed sprint — that's the only
record of what the sprint actually finished, useful for velocity
reporting later, and it's why the backlog definition doesn't also
match "sprint_id points at a completed sprint": that would resurface
finished work too, not just unfinished work.

The limitation, stated plainly rather than hidden in behavior: this
app has no explicit `is_done`/terminal flag on `WorkflowState`, so
"the column with the highest `order` value" stands in for "done."
That's true for the default seeded workflow and true for any project
that hasn't added columns after its real done state, but it is a
heuristic, not a guarantee — a project with, say, a trailing "Won't
Fix" or "Blocked (Reopened)" column after its actual done state would
misclassify tickets sitting in that column as "unfinished" on
completion. **Flagged here as a concrete candidate for the next
workflow-states revision**: a real terminal/`is_done` flag on
`WorkflowState`, replacing this order-based proxy once workflow
states support configuring which column actually means done. Not
built now — this slice is about sprints, not redesigning workflow
state configuration, and shipping a flag with no UI to set it would
just be dead schema, the same "don't add reflexively" call this app
made about `eager_defaults` on `WorkflowState` and about project
archiving after soft-delete.

**`total_points` is computed server-side, never left for the frontend
to sum.** `SprintRead` carries a `total_points` field that isn't a
stored column — it's `SUM(story_points)` over the sprint's current,
non-deleted tickets, attached to the ORM object before serialization.
Named `total_points` rather than `capacity` on purpose: in Scrum,
"capacity" usually means the team's *available* effort, a number this
app doesn't model at all, and reusing that word for "sum of what's
committed" would be a quiet accuracy bug in the vocabulary, not just
the code. `list_sprints` computes every sprint's total in one grouped
query (`GROUP BY sprint_id`), not one query per sprint — the same
N+1 avoidance every other list endpoint in this app already follows.

**Backlog pagination — the gap flagged a while back, finally
addressed, and addressed here first rather than retrofitted.** Plain
offset/limit (`?limit=&offset=`, capped at 200), returning
`{items, total, limit, offset}`. Chosen over keyset/cursor pagination
because a project's backlog is hundreds of tickets, not millions, and
the acknowledged tradeoff — a concurrent insert or delete during
paging can shift which tickets land on which page — is a UI-polish
nit for a backlog view, not a correctness or security issue the way
it would be for, say, an audit log. Keyset's added complexity isn't
worth paying for in this first pass.

**Authorization split, extended, not reinvented.** Sprint
create/update/delete/start/complete are admin-only, same class of
"shared structure, not a personal resource" as `WorkflowState` — a
sprint has no `created_by` (deliberately absent from the contract:
planning artifacts aren't owned the way a ticket or project is).
Moving tickets into or out of a sprint — a plain ticket `PATCH` with
`sprint_id`, or the bulk `.../assign` endpoint — stays open to any
org member: `sprint_id` and `story_points` both joined
`_COLLABORATIVE_FIELDS` alongside `workflow_state_id`/`position`/
`assignee_id`, the same split that already separates "who can define
a board column" from "who can drag a card into one."

**Verified.** `scripts/verify_sprints_rls.py` (new, wired into CI
right after the workflow-state proof script): cross-org sprint access
rejected at the project-lookup stage (RLS-driven, not a special case),
cross-project `sprint_id` assignment rejected structurally (same
class of check as a cross-org assignee), the concurrent-start race
described above, paginated backlog honoring `limit`/`offset`/`total`,
bulk-assign rejecting anything not currently in the backlog, `total_points`
matching a hand-computed sum, and the full start → complete →
auto-return → reappears-in-backlog lifecycle. Full seven-script
backend regression (the six existing scripts plus this one), `ruff`,
and `pip-audit` all re-run clean afterward; frontend `ESLint`,
`tsc --noEmit`, and a full `next build` all clean with every new route
registered.

---

## Slice 18 — Phase 1d: Password reset

**Built:** `password_reset_tokens` (migration 0013, no `org_id`, no
RLS — this table has no authenticated management surface the way
`Invite` does, so it never faces RLS's need for a tenant context
before it can be queried; it's structurally closer to `UserLookup`/
`InviteLookup` than to `Invite` itself). `POST /auth/password-reset/
request` and `POST /auth/password-reset/confirm`, both public and
unauthenticated by necessity — that's the whole point of a recovery
path — with a 5/minute-per-IP rate limit on `request`, stricter than
login/signup's 10/minute since this endpoint is a strictly easier
abuse target (spamming it costs an attacker nothing but a valid-
looking email address). Frontend: a "Forgot password?" link on
login, a request form, and a token-from-URL confirm form.

**Email enumeration — genuinely no observable difference, not just
the same message.** The naive fix — return the same `message` string
whether or not the account exists — still leaks the answer through a
second channel this app specifically has to solve around: `reset_link`
is only how this endpoint hands back a token at all, since real email
delivery is out of scope (same limitation as invites). Populating that
field only when the account is real, and omitting or nulling it
otherwise, would make the field's *presence* the oracle instead of the
message text — a smaller leak, but still a leak, and still enough to
enumerate every registered email in the system one HTTP call at a time.

The actual fix: `request_password_reset` generates a token on *every*
call, unconditionally, before it even knows whether the account
exists. That token is only ever persisted to `password_reset_tokens` —
and therefore only ever valid — when the email resolves to a real user
via `UserLookup`. The response always contains a well-formed
`reset_link`, real account or not. An unpersisted token, handed to
`confirm_password_reset`, fails with exactly the same 400 ("Invalid,
expired, or already-used reset link") a genuinely expired or
already-used real token gets — there is no code path, status code, or
response shape that distinguishes "this email was never registered"
from "this link already expired." Proven directly in
`verify_password_reset.py`, not just asserted: a request for a real
email and a request for a fabricated one are compared key-for-key and
found identical, and the fabricated one's token is then handed to
`confirm` and shown to actually fail, not just cosmetically resemble a
working link.

**Named, not hidden: the one thing this doesn't equalize.** The
found-branch does one extra write (`INSERT INTO password_reset_tokens`)
that the not-found branch skips entirely — a real, if small, timing
difference between the two cases. This is a deliberate scope
boundary, not an oversight: response-content parity closes the loop
an attacker can actually exploit cheaply (read the JSON, compare
fields); constant-time equalization against network-level timing
analysis is a substantially deeper problem — the DB write's few
milliseconds are usually swamped by ordinary network jitter over a
real connection — and solving it isn't free (a dummy write, or an
artificial delay, adds real latency to the common case for a
marginal, hard-to-actually-exploit benefit). Flagged here as an
explicit, acknowledged residual gap, the same honesty this project
has applied to every other named-but-deferred limitation (the
workflow-order "done" heuristic, project archiving, etc.), not
something quietly left unsaid.

**Why `confirm_password_reset` revokes every outstanding refresh
token, walked through as an actual attack, not asserted as obviously
correct.** Say an attacker has a stolen refresh token — a synced
browser profile on a device that went missing, a session cookie
leaked some other way. They never needed the password at all: refresh
rotation (see `refresh()` in `app/api/auth.py`) only requires *them*
to keep refreshing before the legitimate user does, which they can do
indefinitely, no password ever re-checked at any point in that cycle.
Now the legitimate user notices something's off and resets their
password through this flow. If `confirm_password_reset` only updated
`hashed_password` and stopped there, that reset would have changed
*nothing the attacker actually needed* — their already-issued refresh
token is a completely separate credential from the password, sitting
in Redis with no relationship to `users.hashed_password` at all,
still valid, still rotating on schedule. The user would walk away
believing they'd secured the account while the attacker's session
kept working, unaffected, through the very mechanism (password reset)
they used specifically because they suspected compromise. That's the
actual failure mode `revoke_all_refresh_tokens_for_user(user.id)`
closes — every session dies, including the attacker's, forcing
anyone who wants back in to actually know the new password. This is
not a new mechanism: it's the identical fix `remove_member` already
applied in `app/api/org.py` for the same underlying gap ("removed but
still has a working session indefinitely"), just triggered by a
different trust-changing event. Called after the commit, not before —
same ordering, same reasoning as `remove_member`: if the password
update had failed to commit, revoking every session first would lock
out someone whose password never actually changed.

**One deliberate exception to the real-HTTP-only proof-script
convention, named as such.** Every proof script this project has
written so far — `verify_rls.py` through `verify_sprints_rls.py` —
proves its case entirely over real HTTP, on principle: it's what
actually runs in production, so it's what should be exercised.
Proving a *token expires* breaks that principle structurally, not
because of laziness: `password_reset_expire_minutes` defaults to 45,
and no HTTP call can fast-forward a wall clock. The alternatives were
running CI 45 minutes slower for one assertion, making the expiry
window configurable per-environment (real behavior diverging between
dev/test and production, exactly the kind of split this project has
avoided elsewhere), or reaching directly into Postgres to backdate one
token's `expires_at` into the past. `verify_password_reset.py` does
the last of these — `password_reset_tokens` has no RLS (see above),
so this doesn't even need a tenant context, just a direct `UPDATE` via
the same `AsyncSessionLocal` `scripts/seed.py` already uses — and the
module docstring says so explicitly, so this doesn't read as an
unexplained inconsistency with every other script's discipline.

**Verified.** `verify_password_reset.py` (new; wired into CI
immediately after the auth+RLS integration proof, before projects):
the enumeration-identical-response check described above, reuse
rejected (a token that already succeeded can't succeed again),
requesting a reset twice and confirming with the newer token
invalidates the older one, the backdated-expiry check, and — the
check with real security weight — a refresh token issued *before* the
reset is proven dead *after* it via the same "actually call
`/auth/refresh` and check it fails" structural test `verify_invites_rls.py`
already established for member removal, not an assumption that the
revocation call worked. Full eight-script backend regression, `ruff`,
and `pip-audit` all clean; frontend `ESLint`, `tsc --noEmit`, and a
full `next build` clean with both new routes registered.

---

## Slice 19 — Phase 1d: Email verification

**Built:** `EmailVerificationToken` (migration 0014, same no-org_id,
no-RLS shape as `PasswordResetToken` and for the identical reason —
no authenticated management surface, no tenant-context bootstrap
problem to solve) and `users.is_verified` (backfilled `true` for
every account that existed before this slice — they were working
fine without it, and suddenly nudging them for something that wasn't
a requirement when they signed up would be a regression, not an
improvement). Signup generates a token and returns the verification
link directly in the response (same dev-mode stand-in for real email
delivery as invites and password reset), `POST /auth/verify-email`,
and a rate-limited `POST /auth/resend-verification`.

**Soft-nudge, not a gate — and the reasoning is specific to this
app's threat model, not a general "verification is optional"
stance.** The instinct with email verification is usually to block
until it's done, because that's the default for public consumer
apps where an unverified account is a stranger with no established
relationship to anyone else on the platform — verification is doing
real work there, screening out throwaway/bot signups before they can
touch anything. Wrkbase's actual security boundary is somewhere
else entirely: Row-Level Security enforces tenant isolation at the
database, and that enforcement doesn't know or care whether
`is_verified` is true — an unverified user is exactly as isolated
from every other org's data as a verified one, because the isolation
is architectural, not identity-gated. Blocking an unverified user
from creating a project or a ticket wouldn't be closing a real gap in
this app; it would be adding friction against a cross-tenant risk
that doesn't exist here, modeled on a threat this app's actual
security mechanism doesn't share. What verification *does* still
protect against — losing account recovery because the email on file
is a typo or was never real — is real, but it's a self-inflicted
UX cost the account owner bears alone, not a risk to anyone else,
which is exactly the class of problem a persistent nudge fits and a
hard lockout over-solves. Proven directly in
`verify_email_verification.py`, not just asserted: a freshly
signed-up, still-unverified user creates a project and a ticket over
real HTTP with no special-casing anywhere in the request path.

**Invite-redemption is auto-verified — and the reason is a stronger
claim than "it avoids redundant friction."** A self-click
verification link proves exactly one thing: whoever clicked it
controls that inbox right now. That's a real but narrow fact —
it says nothing about whether the person is who they claim to be
relative to anyone else, because nobody else was involved in the
decision to let them in. An invite redemption proves something
categorically different: an already-authenticated admin, already
inside the org, made a deliberate decision to grant *this specific
person* access, and `_redeem_invite`'s email-binding check (see
Slice 6) means the invite can only be redeemed by whoever controls
the *exact* address that admin chose — not "some inbox," but the one
the admin picked on purpose. That's a human vouching for another
human, which is a fundamentally different and stronger kind of trust
than a bot-or-not inbox check — self-click verification is a floor
under an otherwise-anonymous signup; an invite is a ceiling an admin
already applied. Requiring both wouldn't add security, it would
just fail to recognize that the stronger check already happened.
This is also the thing that makes the soft-nudge decision above hold
together end to end: because invited users — very likely the
majority of real accounts on a team tool, after the first admin
signs up — are auto-verified, "unverified" ends up describing almost
exclusively the first user of a brand-new org, someone with no one
else yet depending on them either way.

**Resend invalidates the prior token immediately; password reset
lets several coexist. Different by design, and the reasoning is
about what each token is actually *for*, not an arbitrary choice.**
Password reset's coexistence tolerance exists because a reset is
requested under real pressure — locked out, unsure which device
still has the email, possibly trying more than once because the
first attempt seemed to not arrive. Every one of those requests
represents the *same* legitimate need, and whichever token the user
actually manages to use should work — invalidating an earlier one
the moment a second is requested would mean a user who re-requests
"just in case" can accidentally kill the link sitting unread in a
tab on their other device, adding friction to the exact moment
friction is most costly, for zero security gain (redemption already
requires the token itself, so an old token being *also* valid isn't
a weaker guarantee, just a more forgiving one). Verification doesn't
share that pressure: it's a one-time, low-stakes state flip with no
adversarial pressure and no "which device" ambiguity worth
preserving, and clicking "resend" is a near-explicit signal that the
previous link is considered dead by the person who requested the new
one. Keeping the old one alive anyway would only mean more valid,
unused tokens sitting in old emails and browser history for a
feature where the cost of being wrong about that (having to click
resend again) is under a second. The two policies aren't in tension —
each is the right shape for what that specific token is protecting,
and it's the same underlying distinction as the soft-nudge decision
above: password reset guards something that actually matters if
gotten wrong; verification doesn't.

**Two Turbopack failures this slice's regression pass hit that
looked identical but weren't.** Both surfaced as the same symptom —
`tsc --noEmit` failing on a syntactically broken
`.next/dev/types/validator.ts` — and both were "fixed" by deleting
that generated directory, which is exactly why it would have been
easy to file them as the same bug. They weren't. The first was
ordinary cache staleness: the dev server's generated route-type file
hadn't caught up with routes that had moved or been added, the same
category of issue this project has hit repeatedly since the design-
system slice, normally cleared by a full `--force-recreate -V` and
a fresh compile. The second, hit later in the same session, produced
a *corrupted* file, not a stale one — inspecting it directly showed
genuinely duplicated, interleaved fragments mid-file (part of one
route's validation block spliced into the middle of another's), the
signature of a torn write: several `curl` warm-up requests fired in
quick succession triggered concurrent regeneration of the same
generated file, and two writers landed on top of each other. The fix
for the second case wasn't "recreate the container" (staleness's
fix) — the container was already fresh — it was deleting the
corrupted `.next/dev/types` directory and warming the routes
*sequentially*, one request at a time, so regeneration never raced
itself again. Same visible failure, same file, genuinely different
root cause, genuinely different fix.

**Verified.** `verify_email_verification.py` (new; wired into CI
right after `verify_password_reset.py`): the soft-nudge behavior
proven by actually creating a project and a ticket as an unverified
user, invite-redemption's auto-verify with no token generated at
all, reuse and backdated-expiry rejection (the same deliberate,
named exception to the real-HTTP-only proof-script convention as
password reset — no HTTP call can fast-forward `email_verification_
expire_hours`), and resend's immediate invalidation proven by
requesting twice and confirming the *older* token specifically stops
working, not just that the newer one succeeds. Full nine-script
backend regression, `ruff`, and `pip-audit` all clean; frontend
`ESLint`, `tsc --noEmit`, and a full `next build` clean with
`/verify-email` registered.

---

## Slice 20 — SaaS marketing landing page, plus two real bugs the
review pass caught

**Built:** `/` rebuilt as a genuine SaaS marketing page — hero with a
live interactive demo (type a plain-English bug report, watch it
parse into a structured, triaged ticket), six benefit-led feature
sections, a static before/after comparison with an animated time
stat, a scroll-paced ticket-lifecycle sequence, three real pricing
tiers ($0 / $5 / $12 per user/month), and a "who it's for" trust
section with no fabricated testimonials. Deliberately feature-only:
no build-log narrative, no competitor named anywhere, no GitHub link
as a CTA (moved to the footer only). `motion` (Framer Motion's
current package) was added as a real dependency here, not a
gratuitous one — `whileInView`, `useReducedMotion`, and layout-aware
`animate()` genuinely replace what would otherwise have been hand-
rolled `IntersectionObserver` plumbing and manual reduced-motion
branching repeated at every call site.

**Routing: `/` is now permanently the marketing page, authenticated
or not** — the existing `(shell)` routes (`/dashboard`,
`/projects/*`, `/team`) deliberately did not move to an `/app` prefix;
they were already login-gated by `proxy.ts`, so moving them would
have been a large refactor (updating every internal `href` across the
app) in exchange for a URL-naming preference, not a functional gap.
`LandingPage` reads auth state itself and swaps every CTA ("Start
free trial" → "Go to dashboard") rather than the page redirecting a
signed-in visitor away from their own homepage, the same pattern real
SaaS marketing sites use.

**Bug 1 — signup and login silently stopped landing users in the
app.** Both `router.push("/")` on success, dating from when `/`
auto-redirected a logged-in visitor straight to `/dashboard`. That
auto-redirect was the thing this slice deliberately removed (see
above) — and nothing else was checking for the gap it left behind.
The result wasn't an error or a broken build: `/` is a perfectly
valid, 200-rendering route either way, so `tsc`, `ESLint`, and
`next build` all stayed clean through it. A brand-new user finishing
signup, or an existing one logging in, landed back on marketing copy
trying to sell them the product they'd just joined — one avoidable
extra click, not a crash, which is exactly the kind of regression
that survives every automated check this project runs and only
surfaces from someone actually clicking through the flow. Fixed by
pointing both redirects at `/dashboard` directly. Worth naming
plainly: this class of bug — a client-side navigation target
becoming stale after an unrelated page's behavior changed underneath
it — has no automated guardrail in this stack today; the six-plus
backend proof scripts and the frontend's lint/type-check/build
pipeline all verify *shape* (does the code compile, does the route
exist, does the API contract hold), not *flow* (does clicking through
signup actually land you where a signed-up user should land).

**Bug 2 — the hero demo's jump cut, and why the first diagnosis was
wrong.** The original suspicion (mine, going into this slice's
review) was that the input box not visually shrinking before the
ticket card appeared below it read as abrupt — the "hand-off
compression" beat that got simplified away during the initial build.
Looking at the actually-built sequence beat by beat instead of
guessing: the input staying a fixed size while new content appears
below it is an ordinary, well-understood pattern (inline search
results, validation messages) — not the defect. The real one was
smaller and easy to miss: the cursor blink and the accent processing-
dot are both `motion.span` elements inside an `AnimatePresence`, and
neither had an `exit` prop set. Without one, `AnimatePresence` doesn't
transition an element out when its render condition goes false — it
just deletes it, instantly, on the same frame the ticket card begins
its own (properly transitioned) fade-in. Every *other* moving part of
the handoff — the input's border color, the card's entrance — already
had a real transition; the one that didn't was the least visually
prominent element in the whole sequence, which is exactly why it was
misdiagnosed as a sizing problem on the big, obvious box instead.
Fixed with a two-line addition (`exit={{ opacity: 0 }}` on both spans),
not a new animation. The debugging lesson worth keeping: when an
`AnimatePresence` sequence feels like it's cutting somewhere, check
every child's `exit` prop before touching the layout of the child
that's easiest to look at — the missing transition is as likely to be
on the small auxiliary element nobody's looking at as on the thing
that visually dominates the frame.

**Verified.** `ESLint`, `tsc --noEmit`, and a full `next build` clean
after both fixes. Routing re-checked over real HTTP (not just read
from the code): signup and login via the actual `/api/auth/*` proxy
paths both establish a valid session and `/dashboard` resolves `200`
under it; `/` still resolves `200` and serves the marketing page for
a request carrying no session cookie at all. The literal client-side
`router.push` navigation itself — the browser's URL bar actually
changing after a button click — isn't something curl can execute;
that part was confirmed by hand, in a real browser, not simulated.

---

## Slice 21 — Closing the organizations RLS gap deferred since Slice 2

**Built:** migration `0015`, giving `organizations` its own
`FORCE ROW LEVEL SECURITY` policies. This table has had none since
Slice 2 — it's the tenant root, not a tenant-scoped resource, and
adding a policy for it collided with the login-bootstrap problem
`user_lookup` was built to solve, so it was deliberately deferred.
Worth closing now that invites make multi-org membership real: a
compromised or misused `wrkbase_app` connection could otherwise
enumerate every organization's name, `ticket_prefix`, and
`next_ticket_number` with no tenant filtering at all.

**Why the policy predicate is `id`, not `org_id`.** Every other
RLS-protected table in this app is scoped *by* an `org_id` column
pointing at `organizations` — the row belongs to a tenant. This table
has no such column, because a row here *is* a tenant: there's nothing
for it to point at except itself. So the predicate has to be
`id = current_org_id` instead of the usual `org_id = current_org_id`.
It's a one-token difference, but it's the reason this table couldn't
just reuse the `tenant_isolation` policy shape from `users`/`sprints`/
everywhere else — the column that "is this row's tenant" is a
different column here.

**Why one command needed a different shape from the other three.**
That same `id`-keyed predicate is also why this table needed four
separate per-command decisions instead of one `USING`+`WITH CHECK`
pair:

- **SELECT** — scoped (`id = current_org_id`). Org A must never read
  org B's row by id, the ordinary default-deny case.
- **UPDATE** — scoped the same way. Grepping every real
  `organizations` touch point before writing the migration surfaced
  the one that mattered: `tickets.py`'s `_next_ticket_number()` runs
  `UPDATE organizations ... RETURNING` on *every single ticket
  creation*, to atomically bump the per-org ticket counter. Without an
  UPDATE policy, `FORCE ROW LEVEL SECURITY` defaults to deny for any
  command with no applicable policy — every ticket creation would have
  silently broken the moment this migration shipped. Scoping it also
  means org A can never bump org B's counter, which leaving it
  permissive would otherwise have allowed.
- **INSERT** — deliberately left permissive (`WITH CHECK true`).
  Self-serve signup creates the `Organization` row before any tenant
  context can exist: the org doesn't have an identity to scope by
  until it exists. A restrictive check here would make it impossible
  to ever create the first org. This isn't a workaround — an INSERT
  can't leak *existing* rows the way SELECT/UPDATE can, and
  unrestricted self-serve org creation is already this app's
  intentional behavior; the policy just states that plainly.
- **DELETE** — left with no policy at all (default-deny under FORCE
  RLS). No code path in this app ever deletes an organization, so
  there's nothing to make permissive — and leaving it unpoliced means
  that stays true even if someone adds a delete path later without
  thinking about this table specifically.

**The subtle part: Postgres checks `RETURNING` against the SELECT
policy, even on an INSERT.** This is the real find of this slice, in
the same spirit as the `NULLIF` GUC bug from Slice 2 — a piece of RLS
behavior that isn't obvious from the policy's own `USING`/`WITH CHECK`
clauses and will bite anyone who assumes "permissive INSERT policy"
means "INSERT always succeeds." SQLAlchemy's Postgres dialect appends
an implicit `RETURNING` to essentially every INSERT — ORM `session.add()`
and even a plain Core `insert()` — specifically to read back
server-generated columns like `id` and `created_at` into the Python
object. Postgres, in turn, only returns a row from `RETURNING` if that
row passes the table's applicable **SELECT** policy — not the INSERT
policy that just permitted the write. So the very first attempt at
self-serve signup after this migration failed outright:
`InsufficientPrivilegeError: new row violates row-level security
policy for table "organizations"`, thrown from the INSERT statement,
despite `insert_new_org`'s `WITH CHECK (true)` being trivially
satisfied. The actual failure was the implicit RETURNING trying to
read the just-inserted row back under `select_own_org`'s
`id = current_org_id` — and no tenant context existed yet to satisfy
it, because the org's id wasn't known until the INSERT itself
completed. Two commands, two different policies, and the one that
silently mattered wasn't the one anybody would have looked at first.

Fixed by generating the org's UUID client-side
(`Organization(id=uuid.uuid4(), ...)`) instead of leaving it to the
column's `gen_random_uuid()` server default, in both `signup()` and
`scripts/seed.py`, and setting tenant context to that known id
*before* the insert rather than after. This isn't a hack layered on
top of the RLS design — it brings `organizations` in line with how
the `users` insert immediately below it already worked all along
(context set before the row exists, because `users.org_id` is always
known up front). Proving the INSERT policy alone, with no tenant
context at all, needed a genuinely raw SQL `text()` insert in
`verify_rls.py` — even a bare Core `insert()` against the table
object still triggers Postgres's dialect-level implicit `RETURNING`
and hits the exact same wall.

**Traced all three auth entry points, not just login:**
- **Login** never queries `organizations` directly — it resolves via
  `user_lookup` (no RLS), unchanged.
- **Self-serve signup** is the path the RETURNING interaction above
  actually broke, and the one the client-side-id fix targets.
- **Signup via invite** resolves the target org through
  `invite_lookup` (no RLS) before touching `organizations` at all —
  already correctly ordered, confirmed by direct read and by
  `verify_invites_rls.py` passing unchanged.

**Extended `verify_rls.py`** (now 9 checks, up from 5) rather than
adding a separate `verify_organizations_rls.py`: this is core RLS
mechanics for the foundational table Slice 2 deferred, the same
category as the existing `users` checks, not a resource-specific
concern the way `verify_projects_rls.py` etc. are. New checks: org A's
context can read its own organization row; org A's context reading
org B's organization row by real id returns zero rows, not an error;
creating a new organization with no tenant context at all still
succeeds (the permissive INSERT, proven via raw SQL for the reason
above); org A's context updating org B's `next_ticket_number` affects
zero rows, not an error. `get_org_id()`, the script's own setup
helper, also had to change — it used to query `organizations` by name
directly with no context set at all, which the new SELECT policy
would now correctly reject. Resolved via `user_lookup` instead
(`email -> org_id`, no RLS), the same bootstrap mechanism login itself
relies on, keyed off each seeded org's known admin email rather than
the org's name.

**Verified:** migration applied cleanly; all 9 `verify_rls.py` checks
pass; all 8 other proof scripts (`verify_auth_rls`,
`verify_email_verification`, `verify_invites_rls`,
`verify_password_reset`, `verify_projects_rls`, `verify_sprints_rls` —
including its concurrent race check, which exercises the same
`_next_ticket_number` UPDATE path this slice's new UPDATE policy
guards — `verify_tickets_rls`, `verify_workflow_rls`) pass with no
regressions; `ruff check .` clean; `pip-audit` shows only pre-existing
`pip`-tool CVEs unrelated to any application dependency.

---

## Slice 22 — Phase 2, slice 1: an authenticated WebSocket connection

**Built:** `WS /ws/projects/{project_id}` — one room per project, no
pub/sub or message handling yet. This slice is entirely about the four
ways into the connection: authenticate the handshake itself, verify
the caller can reach the requested project, refuse Cross-Site
WebSocket Hijacking, and bound how long the connection stays trusted
once open. `POST /auth/ws-ticket` mints the short-lived credential the
handshake authenticates with (see below). On the frontend,
`lib/ws.ts` connects on the project board page and logs connection
state to the console — nothing rendered yet, matching the backend's
scope.

**Token delivery: the full reasoning chain, not just "query param."**
Every link here was checked, not assumed:

1. The browser `WebSocket` constructor (`new WebSocket(url,
   protocols?)`) has no headers argument at all — confirmed directly,
   not assumed. This app's existing `Authorization: Bearer` path
   (used by API clients and this app's own proof scripts) is simply
   unavailable to a browser-originated socket; there is no header to
   put it in.
2. The httpOnly `access_token` cookie is scoped to the frontend's own
   origin, by design — it only reaches the API today via the
   same-origin Next.js rewrite proxy (`next.config.ts`), which is what
   lets it stay httpOnly at all. Checked this project's own pinned
   Next.js version's bundled docs (`node_modules/next/dist/docs`)
   directly for whether `rewrites()` forwards a WebSocket Upgrade
   request: no mention of WebSocket support anywhere in the
   rewrites/proxy docs for this version. Rather than depend on
   undocumented behavior, the socket connects straight to the API's
   own origin — which also means the cookie wouldn't reliably arrive
   here even if it weren't origin-scoped, since this is now a genuine
   cross-origin browser connection.
3. That leaves a query parameter as the only channel the browser
   actually offers. The question that mattered was *what* travels
   through it. Putting the real access token there was rejected
   deliberately: a WS handshake URL persists in server access logs and
   any intermediate proxy's logs for as long as the token in it stays
   valid — roughly 15 minutes and freely reusable within that window,
   a materially bigger exposure than this app accepts for a credential
   anywhere else. Instead, `POST /auth/ws-ticket` — authenticated
   completely normally, behind `get_current_auth`, over the existing
   cookie-through-the-proxy path — mints a random, single-use, 30-
   second ticket (Redis-backed, fetch-and-delete via `GETDEL`, the
   same primitive `pop_refresh_token` already uses for exactly this
   race). This is the same shape already established for invite,
   password-reset, and email-verification tokens: an opaque credential
   good for exactly one redemption, bridging a session into a context
   that can't safely carry the real one. It satisfies "the existing
   session's claims, delivered via query param" without the 15-minute,
   reusable exposure a raw token would carry.

**Tenant context for a connection with no natural transaction
boundary.** Every prior use of `set_tenant_context` in this app has
been "once, per transaction, for the one request currently using it"
— `is_local=true` ties the setting to the transaction it was set in,
discarded when that transaction ends (migration 0001's docstring).
That model assumes a request is short-lived. A WebSocket connection
isn't a request; it's a channel that can stay open for hours, with no
transaction boundary to anchor context to. Holding one DB
session/transaction open for the connection's entire life just to
keep `app.current_org_id` set would mean an idle transaction sitting
on a Postgres connection for hours — its own operational cost — for
no benefit until a real query is actually needed (none yet, in this
slice). So `org_id` is kept as a plain Python value on the connection
instead, and would be re-applied via a fresh, short-lived
`set_tenant_context` call each time a query is actually needed — the
access-check below already does exactly this once, at connect time.

**The staleness question, and why it's the same bug class as the
`/auth/refresh` stale-role fix (Slice 12), just generalized.** Slice
12 found `POST /auth/refresh` trusting a cached role from a refresh
token's stored payload instead of re-checking the database — an admin
demoting a user had no effect until that user's *next* refresh, up to
15 minutes later, because refresh was the mechanism supposed to bring
a session back in line with reality and wasn't. The question here is
structurally identical: if this connection just trusts `org_id`/role
for as long as the socket happens to stay open, a demotion, removal,
or project-access change made two minutes into a six-hour-old
connection would silently fail to apply until the client happened to
reconnect on its own — an unbounded staleness window instead of a
15-minute one, which is a regression from what this app already
guarantees everywhere else, not a neutral gap. Two ways to close it:
poll the database on some invented interval, or simply stop trusting
the connection once the access token it descends from would have
expired anyway, and require a genuine reconnect — fresh ticket, fresh
handshake, fresh access-check — to continue. Chose the second:
`settings.access_token_expire_minutes` (15 minutes) from connect time
is reused directly as the connection's own lifetime ceiling, so a
role or access change takes effect within the exact same bound every
other credential in this app already has, with no new re-check
interval invented and no separate polling mechanism to maintain.

**CSWSH: verified against Starlette's actual source, not general
WebSocket folklore.** A WebSocket handshake is an HTTP Upgrade
request, but it does not inherit the same-origin-policy protection a
normal `fetch`/XHR gets for free: SOP blocks a malicious page's JS
from *reading* a cross-origin response, but a completed WebSocket
handshake hands that page's JS a live, bidirectional connection with
no equivalent read-blocking — and the browser attaches this origin's
ambient cookies to that handshake exactly as it would to any other
request to this host. That's Cross-Site WebSocket Hijacking. The
question that mattered for this specific codebase: does
`app.main`'s existing `CORSMiddleware` already stop this, the way it
stops a normal cross-origin `fetch`? Checked directly by reading
Starlette's installed `CORSMiddleware.__call__` source rather than
assuming either way:

```python
async def __call__(self, scope, receive, send):
    if scope["type"] != "http":  # pragma: no cover
        await self.app(scope, receive, send)
        return
    ...
```

It passes any non-`"http"` scope straight through untouched —
`"websocket"` included — so `CORSMiddleware` does nothing for this
endpoint at all. A genuinely reusable fact about this specific stack,
not general trivia: nothing in this app's middleware stack protects a
WebSocket route from CSWSH unless the route checks for it itself. Fixed
by verifying the handshake's `Origin` header against
`settings.cors_origins_list` before doing anything else — the same
env-driven allowlist HTTP already uses (local dev vs. a future
deployed origin), not a second list to keep in sync.

**Project access, same shape as every other resource.** RLS already
scopes the access-check query to `org_id`, so a `project_id` from
another org simply matches zero rows — indistinguishable from a
project that doesn't exist, the same ambiguity `_get_project_or_404`
deliberately preserves for HTTP. Checked with a short-lived session
opened just for this one query, not the connection-lifetime session
the tenant-context section above explains why to avoid.

**Reject the upgrade, don't accept-then-close.** `websocket.close()`
called *before* `websocket.accept()` makes uvicorn respond to the
handshake's HTTP request with a non-101 status instead of completing
the upgrade at all — confirmed via `verify_ws.py` catching
`websockets.exceptions.InvalidStatus` specifically, which only raises
when the 101 Switching Protocols response never happened. A client
that briefly treats a connection as live before it's actually
authorized is a materially worse failure mode than one that's refused
outright during connect(); this app's four rejection paths (no
ticket, invalid ticket, mismatched Origin, no project access) all
close before accept for that reason.

**Extended the proof-script pattern with `scripts/verify_ws.py`,**
covering exactly the four rejection paths above (each individually
confirmed as a handshake-level rejection, not accept-then-close), a
reused ticket rejected on its second redemption (`GETDEL`'s
single-use guarantee, proven, not just implemented), and the real
happy path — connects, survives a 2-second idle period plus a live
ping, and closes cleanly from the client side with no exception. The
15-minute connection-expiry timer itself isn't exercised by CI (that
would make every run 15 minutes slower for a mechanism review already
covers); it's verified by code review and by the same
`settings.access_token_expire_minutes` value already being proven
correct as the HTTP staleness bound elsewhere in this app.

**Verified beyond the proof script:** no browser automation tool was
available in this environment, so the actual browser-facing path —
cookie-based signup through the Next.js proxy, a cookie-authenticated
`ws-ticket` mint through that same proxy, then the direct-to-API
socket exactly as `lib/ws.ts` performs it — was verified with a
scripted equivalent of that exact sequence instead of a literal
DevTools check, and it connects successfully end to end. All 9
existing proof scripts re-run with no regressions; `ruff check .`,
frontend `ESLint`, `tsc --noEmit`, and a full `next build` all clean.

---

## Slice 23 — Phase 2, slice 2: Redis pub/sub for live board updates

**Built:** `update_ticket` publishes to `project:{project_id}` whenever
a PATCH touches a `_COLLABORATIVE_FIELDS` key; every WebSocket
connection to that project subscribes to the same channel and forwards
matching messages straight through to its client. The board page
applies an incoming change directly to local state instead of
refetching. No new endpoints — this slice wires the connection built
in Slice 22 up to something real.

**Payload: a minimal diff, not the full ticket, and why `updated_by`
replaces server-side echo suppression.**
`{type: "ticket.updated", project_id, ticket_id, changes, updated_by}`.
`changes` only ever contains the subset of `_COLLABORATIVE_FIELDS`
actually present in the triggering PATCH — never the full `TicketRead`
shape, which would mean building and shipping a complete resource over
the wire for every move, defeating the entire "diff, not refetch"
point — and never a content field (title/description/type/parent_id),
which doesn't broadcast at all, since this event type exists for board
interaction, not content edits. `updated_by` is the acting user's id,
included specifically so a *receiving* client can recognize its own
change echoing back and skip re-applying it. The alternative — the
server tracking which connections belong to which user and
deliberately not forwarding a publish back to its own author — would
mean either per-connection filtering logic in `app/api/ws.py` (a
second thing that could leak or misfire, on top of the channel-scoping
this slice already leans on for tenant isolation) or a second Redis
message shape carrying connection identity instead of user identity.
Tagging the payload with who caused it and letting every subscriber
independently decide what to do with that is simpler and pushes the
decision to the side that actually has the context to make it: the
receiving tab already knows whether the id in `updated_by` is itself.

**Subscription design: one dedicated `pubsub` per connection, not a
shared one with in-process routing.** Every WebSocket connection to
`app/api/ws.py` opens its own `redis_client.pubsub()` and calls
`.subscribe(project_channel(project_id))` independently — two people
with the same project open means two separate Redis-level
subscriptions to the same channel name, not one shared subscription
locally fanned out to two in-memory listeners. This costs slightly
more Redis-side connections than a shared-subscription-plus-demux
design would (a real, deliberate tradeoff, revisited only if
connection *count* ever actually becomes this app's bottleneck — it
is nowhere close at this app's real scale). What it buys: there is no
per-connection dispatch table in this process that a bug could get
wrong. Redis's own pub/sub primitive is what fans one published
message out to every subscriber of a channel; this app never
re-implements that step itself, so there's nothing here for a leak
between independently-authenticated sessions to hide in. Verified
directly, not just argued: `PUBSUB NUMSUB` showed exactly 2 real
subscribers while two tabs were connected to the same project, and
exactly 1 immediately after one of them disconnected — see below.

**Proving multi-process fan-out required two literal separate
processes — and explains why a weaker test wouldn't have proven
anything.** The entire reason Redis pub/sub was chosen here over an
in-process event emitter (an `asyncio.Queue`, a dict of callbacks) is
that a single running API instance's memory isn't a reliable channel
between two different requests in production — there's more than one
worker, potentially more than one machine. But that's exactly the
property a naive test can fail to actually exercise: every check in
`verify_ws_pubsub.py` would pass identically against a broken,
purely-in-process implementation, *as long as the test only ever runs
one API process* — which is what this repo's CI already did for every
other proof script, and what running the new script against a single
`uvicorn` instance would have silently continued doing. A `--scale`-based
Docker Compose scale-out was considered and rejected for the same
reason: a load balancer would pick which of N processes handles which
request, non-deterministically, giving no way to *guarantee* the
mutating PATCH and the receiving WebSocket landed on different
processes on any given run — a proof that only sometimes proves
anything isn't a proof. Instead, CI starts a second, literal `uvicorn`
process on a different port (8001), sharing only Postgres and Redis
with the first (8000) — no shared Python interpreter, no shared
memory, nothing but the two real infrastructure dependencies this
slice actually depends on. `verify_ws_pubsub.py` deliberately routes
one WebSocket connection through port 8000 and the other through port
8001, then issues the mutating PATCH through port 8000 only. Port
8001's connection receiving that event is only possible if it
travelled through Redis — port 8001's process never executed a single
line of the PATCH handler that produced it.

**`PUBSUB NUMSUB` proves cleanup, not just accepts that the code looks
right.** `app/api/ws.py`'s `finally` block calls
`pubsub.unsubscribe(...)` and `pubsub.aclose()` on every exit path —
normal disconnect, the Slice 22 expiry timer firing, or an unhandled
error. Trusting that from reading the code would have been exactly
the kind of "should work" claim this project's proof-script discipline
exists to replace with a real check: `verify_ws_pubsub.py` opens two
connections, confirms `PUBSUB NUMSUB project:{id}` reports `2`,
closes one, and confirms it reports `1` — a lingering `2` would mean
a closed connection is still silently consuming a Redis subscription
slot indefinitely, exactly the kind of resource leak that's invisible
in a two-connection dev test and only shows up as Redis running out of
subscriber slots after days of real production traffic.

**Frontend: a stale-closure bug caught before it shipped, not after.**
The board page's WebSocket `onmessage` handler lives inside a
`useEffect` that only re-runs on `[projectId, user?.id]` — not on
every `tickets` change, which would mean tearing down and reopening
the socket on every single board update, fighting with the very
events it exists to receive. That means the handler can never safely
read `tickets` directly from render scope the way `handleDragEnd`
does a few lines below it (that one's fine — it's called fresh from a
DnD event on the current render, not from a long-lived effect closure)
— doing so here would permanently close over whatever `tickets` was
on the render the effect last ran, silently overwriting every future
local update with a stale snapshot the moment the first live event
arrived. Fixed by using `setTickets`'s functional-update form
(`setTickets((prev) => prev?.map(...))`), which reads the current
state at update time regardless of what the effect's closure
captured — the same category of bug as the RETURNING-gated-by-
SELECT-policy discovery in Slice 21: not caught by any type checker or
lint rule, only by actually tracing what a specific piece of framework
machinery does under the hood.

**Extended the proof-script pattern with `scripts/verify_ws_pubsub.py`:**
two authenticated connections to the same project on the two separate
processes above; a mutation on that project received by both
(byte-identical payloads); a mutation on a second, unrelated project
received by neither, within a timeout; the `PUBSUB NUMSUB` cleanup
check above; and the surviving connection still receiving events
normally after the other one disconnects.

**Verified:** all 9 prior proof scripts plus `verify_ws.py` re-run
with no regressions; the new `verify_ws_pubsub.py` passes against the
genuine two-process setup, in CI and locally; `ruff check .` clean;
`pip-audit` shows only the same pre-existing `pip`-tool CVEs, unrelated
to this slice; frontend `ESLint`, `tsc --noEmit`, and a full
`next build` all clean.

---

## Slice 24 — Invite link retrieval, copy-to-clipboard, and a real
duplicate-invite bug found along the way

**Built:** `POST /invites/{invite_id}/regenerate` — revoke-and-reissue
for a pending invite whose link wasn't copied in time; a shared
`CopyableLink` component (Clipboard API with a "Copied!" confirmation,
falling back to select-to-copy when the API is unavailable) used
everywhere a one-time link is shown — the Team page's invite reveal,
`forgot-password`'s reset link, and `VerificationBanner`'s resend
link — plus a way back to each of those forms once a link has been
shown, which didn't exist before (the form was simply gone once
`result`/`state` was set).

**Point 1 first: is the invite token safe to just re-display, or was
it designed like a password reset link?** Checked, not assumed:
`Invite.token` (`app/db/models.py`) is stored as plain text, unhashed
— technically retrievable. But `InviteRead`'s own docstring already
answers the design question: it's deliberately excluded from every
`GET /invites` response, "if an admin needs to reshare, revoke and
recreate" — the same treatment as a password-reset link, by design,
not an oversight. That sentence is what `regenerate_invite` is.

**Why regenerate is delete-then-recreate, not an in-place token
update — checked migration 0007, not assumed.** `invite_lookup` is
kept in sync by an `AFTER INSERT` trigger only; there's no `AFTER
UPDATE` counterpart. Updating the existing row's `token` column
directly would leave `invite_lookup` still pointing the *old* token at
this invite (now holding different contents) and create no entry at
all for the new one — both `preview_invite` and `_redeem_invite`
resolve a token exclusively through `invite_lookup`, so the old link
would keep resolving and the new one would never work. Deleting
cascades the stale `invite_lookup` row away (`ondelete=CASCADE`);
inserting a fresh `Invite` row fires the trigger again for the new
token. A new endpoint rather than reusing `POST /invites`: it acts on
a specific already-listed invite id without resupplying email/role,
and matches this app's existing sub-action convention (`/start`,
`/complete`, `/restore` elsewhere) instead of overloading create
semantics.

**The real bug, found by actually using the feature: `create_invite`
never checked for an existing pending invite, only an existing
*user*.** The `UserLookup` check already in place stopped inviting
someone already registered; nothing stopped inviting the same,
not-yet-registered email twice, three times, any number of times —
each click created its own independently-valid `Invite` row with its
own token, all simultaneously redeemable, with nothing in the Team
page's list distinguishing them beyond eyeballing which rows share an
email. Worse than just clutter: redeeming any *one* of the duplicates
does nothing to the others (`_redeem_invite` only touches the row it's
given — checked directly, not assumed), so they'd sit there showing
"pending" forever even after the person had already joined via a
different link. Fixed with a check next to the existing one: a
pending (`accepted_at IS NULL`, not yet expired) invite for that email
in the same org now returns `409` pointing at Revoke/Regenerate
instead of silently piling up. An *expired*, never-accepted invite
doesn't block a new one — that row is already inert, and creating a
fresh invite when the only match is a dead one is exactly what
`regenerate_invite` already does deliberately for an explicit row.

**Reconfirmed, not just carried forward: does this change anything
about password-reset's multiple-valid-tokens design?** Checked
directly against the two things that actually made invite duplication
a real bug, not against a vague "duplicates are bad" instinct:

1. *Is there a list-management surface where duplicates become
   visually confusing?* Invites have one — the Team page's pending-
   invite list, which an admin actively reads to track who still needs
   onboarding. Password-reset tokens have no equivalent: no endpoint
   lists a user's outstanding reset tokens anywhere, to anyone. There
   is nothing for a duplicate to visibly clutter.
2. *Does redeeming one duplicate clean up the others, or do they linger
   forever?* Invites: lingered forever, confirmed above — the actual
   mechanism that turned "harmless coexistence" into a real bug.
   Password reset: the opposite, and already built — `confirm_password
   _reset` (Slice 18) explicitly invalidates every other outstanding
   token for that user the moment any one of them is used successfully.
   Losing track of an old reset link, then requesting a new one, then
   using the new one, silently cleans up the old one as a side effect;
   losing track of an old invite link never did.

Both conditions that made the invite bug real are specifically absent
for password reset. The original reasoning stands — this is a
different situation, not a smaller version of the same one — and nothing
here changes it.

**Extended `verify_invites_rls.py`** with 6 new checks: regenerating
returns a fresh id + token (never the same row edited in place); the
*original* link stops resolving the instant regeneration succeeds; the
new link resolves and is genuinely redeemable end to end, not merely
previewable; cross-org regenerate attempts 404; regenerating an
already-accepted invite is rejected (400); and — the bug fix itself —
inviting an email with an already-pending invite is rejected (409)
instead of creating a second, indistinguishable, independently-valid
row.

**Verified:** all 21 checks in `verify_invites_rls.py` pass (first run,
no fixups needed); `verify_auth_rls` and `verify_projects_rls` re-run
as an adjacency check with no regressions; `ruff check .` clean;
frontend `ESLint` and `tsc --noEmit` clean; a full `next build` clean.

---

## Slice 25 — Phase 2, slice 3: notifications, and a real test of
whether the WebSocket infrastructure was ever general-purpose

**Built:** a `notifications` table, two real trigger points (ticket
assignment, invite acceptance — comment/mention support checked for
and confirmed absent, so that trigger point is deferred, not built
speculatively), `GET /notifications` / `/unread-count` / `PATCH
/{id}/read` / `POST /read-all`, a new `/ws/notifications` room
delivering them live, and a bell in the shell header. The largest
slice in Phase 2, and the one that actually tested the premise Slice
22–23 were built on: is this WebSocket/pub-sub layer genuinely
general-purpose, or was it quietly shaped around ticket-move events
specifically? Building a second, structurally different room is what
answered that — see the architecture section below.

**The RLS redesign, and why the obvious first design was wrong.**
Every other RLS-protected table in this app has one shape: the acting
user and the row's owner are the same axis, so `USING`/`WITH CHECK`
both just compare `org_id` (or `org_id`+`project_id`) to the current
session's context. The instinctive version of a "private to one user"
policy is `org_id = current_org_id AND user_id = current_user_id`,
checked everywhere, no per-command split — the same shape as `users`
or `sprints`. That's wrong for this table, and tracing through the two
real trigger points is what caught it before it shipped, not after: a
notification is always created *for* someone other than whoever is
currently authenticated. An admin PATCHes a ticket's `assignee_id` to
a teammate — the teammate is the recipient, the admin is the actor. A
new user redeems an invite — the admin who sent it is the recipient,
in a request where that admin isn't authenticated at all. If
`user_id = current_user_id` were required unconditionally, neither
INSERT could ever succeed, because the acting user is essentially
never the recipient for either real code path this table has. So,
three per-command policies instead:

- **INSERT** is scoped to org only (`org_id = current_org_id`).
  Creating a notification for any real member of your own org isn't a
  privacy leak — nothing is being *read* — and the composite FK to
  `(users.id, org_id)` already guarantees the recipient genuinely
  belongs to that org regardless of what this policy permits.
- **SELECT/UPDATE** are scoped to org *and* recipient
  (`org_id = current_org_id AND user_id = current_user_id`). This is
  the actual privacy boundary: nothing in this app has a legitimate
  reason for one user to read or mark-read another's notifications —
  unlike soft-delete's restore path, there's no sanctioned exception,
  which is exactly why this lives in RLS rather than an app-layer
  `WHERE` clause someone could forget to add to a future endpoint.
- **DELETE** is left unpoliced (default-deny), same as `organizations`
  — nothing in this app ever deletes a notification row.

`app.current_user_id` is a new session GUC (`set_actor_context`,
`app/db/session.py`), set alongside the existing `app.current_org_id`
in `get_current_auth` — the one real choke point for every
authenticated HTTP request. Deliberately not folded into
`set_tenant_context` itself: several existing call sites (self-serve
signup, `scripts.seed`, invite redemption) run before any specific
"acting user" exists at all, and notification *creation* deliberately
runs as a different user than the recipient — folding user scoping
into the org-scoping function would force a `user_id` onto call sites
that have no coherent one to give it.

**The second RETURNING-gated-by-SELECT-policy occurrence — same root
cause as organizations (Slice 21), a structural mismatch this time,
not a timing one.** The ORM's implicit `INSERT ... RETURNING`, used to
read server-generated columns back after any `session.add()`, is
gated by a table's SELECT policy in Postgres, not the INSERT policy
that actually permitted the write. For `organizations` (Slice 21) the
mismatch was timing: the org's `id` wasn't known until the INSERT
itself completed, so `id = current_org_id` couldn't be satisfied yet.
For `notifications` it's structural and permanent: the acting user
creating a notification is essentially never its recipient, so
`select_own_notifications`'s `user_id = current_user_id` would reject
the RETURNING on *every single call*, for both trigger points this
app has — there's no ordering fix that makes it eventually true.
Fixed the same way conceptually (skip RETURNING entirely) but
differently in practice: `create_notification`
(`app/services/notifications.py`) generates `id` and `created_at`
client-side and issues a raw `INSERT` with no `RETURNING` clause at
all, rather than `session.add()`.

**A real SQLAlchemy gotcha, found by running it, not by reading
docs.** The raw INSERT's first draft used
`VALUES (..., :type::notification_type, :payload::jsonb, ...)` —
Postgres's own `::` cast syntax. SQLAlchemy's `text()` bind-parameter
parser doesn't handle a `::` cast immediately following a named
parameter: it left `:type::notification_type` completely
unsubstituted and sent that literal string to asyncpg, which failed
with a syntax error pointing at the `:` — a confusing error one step
removed from the actual bug. Fixed with `CAST(:type AS
notification_type)` / `CAST(:payload AS jsonb)` instead of `::`,
which SQLAlchemy's parser has no trouble with. Confirmed via a direct
smoke test against a real session before it was ever wired into an
endpoint — the same "verify empirically, don't assume" discipline
that caught the RETURNING issue above in the first place.

**Centralized in one service, not inline in each endpoint.**
`app/services/notifications.py` (`create_notification` +
`publish_notification`) is called from two genuinely unrelated files —
`tickets.py`'s `update_ticket` and `auth.py`'s `signup()` — that share
no other code path and would otherwise each need to independently get
the client-generated-id-plus-raw-INSERT shape right, and independently
remember to publish only after a successful commit (a rolled-back
change must never push a live "notification created" event for a row
that doesn't exist — both callers construct the notification inside
the same transaction as whatever triggered it, commit once, and only
publish afterward). One place for that logic to be correct, the same
reasoning `ticket_events.publish_ticket_update` already established
for board events, generalized here to a service two unrelated parts of
the codebase both depend on.

**Point 3: why `/ws/projects/{id}` was the wrong shape, stated
plainly, not forced into fitting.** The existing room is scoped by
`project_id` and opened only when a user is looking at that specific
project's board — `connectProjectSocket` is called from the board
page's own `useEffect`, nowhere else. A notification has to reach a
user regardless of what they're currently looking at: assigned a
ticket in Project B while viewing Project A's board, or while on the
dashboard or team page with no project open at all, where today there
is no live connection whatsoever. Subscribing a user to *every*
project their org has, just in case, would defeat the entire reason
project-scoped channels exist (bounded, relevant subscriptions, not
"everything that might ever matter"). There is no way to reshape the
existing room to cover this without breaking what makes it correct for
its own purpose — this needed a second, genuinely different room, not
a bigger version of the first one:

- **`/ws/notifications`** — no resource in the path at all, channel
  `notifications:{user_id}`. No further access check needed beyond the
  ticket itself: unlike a project a user might not have rights to,
  there's nothing left to authorize once the ticket's own `user_id`
  claim is established — the ticket *is* the room.
- **Lifetime**: opened once at the shell-layout level
  (`app/(shell)/layout.tsx`), alive across every authenticated page —
  structurally different from the board room, which lives and dies
  with one specific page. A browser tab on the board page now holds up
  to two concurrent connections (its project room plus the always-on
  notification room); everywhere else in the app, just the one.
- **A multiplexed alternative was considered and rejected as
  over-engineering for this slice**: one persistent connection per
  browser tab that dynamically subscribes/unsubscribes to
  `project:{id}` as the user navigates, instead of two separate
  endpoints. That would need real bidirectional message handling (a
  client-sent "subscribe to project X" command — this app's WebSocket
  connections have never had any inbound message handling at all) and
  a way to re-run the project-access check for an *already-accepted*
  connection asking to add a new subscription mid-flight, not just at
  handshake time. A real, legitimate future optimization if connection
  *count* ever actually matters at this app's scale — it doesn't yet —
  but meaningfully more complex than what two independently-simple
  rooms already solve correctly today.

**Building the second room is what proved the first one was actually
general-purpose — not by assertion, by refactor.** `_authenticate_
handshake` (CSWSH origin check + ticket redemption) and `_run_room`
(accept, subscribe, the bounded-lifetime dual-task loop, cleanup) are
now extracted, shared functions in `app/api/ws.py`, used by both
`project_socket` and the new `notifications_socket`. Writing the
second room is what revealed the exact boundary between "generic to
any WS room in this app" and "specific to the board" — the origin
check, the ticket auth, the 15-minute staleness bound, and the
per-connection-subscription cleanup guarantee needed zero changes to
serve a completely different channel shape; only the resource-access
check (a real DB query for a project, nothing at all for a user's own
notifications) and the channel name actually varied. If the first
room's code had needed real surgery to support a second, differently-
shaped use, that would have been the honest signal that it was
accidentally coupled to ticket events after all — it didn't, which is
the actual, demonstrated answer to the question this slice opened
with.

**Extended the proof-script pattern with `scripts/verify_notifications.py`:**
both trigger points fire with correct, type-specific payloads;
assigning to yourself generates no notification; the acting admin
never receives their own action as a notification; a fully unrelated
org has zero visibility into another org's activity; mark-read and
unread-count behave correctly, including a cross-user mark-read
attempt returning 404 (RLS blocking it, not an app-layer check); and —
the point 3 payoff — a same-org teammate connected simultaneously to
the identical project board room never receives the other user's
notification, while both of them *do* receive the ordinary
`ticket.updated` broadcast (including the actor's own echo, which is
by design — self-filtering is a frontend concern, not a server one),
proving the two channel types coexist on the same infrastructure
without ever leaking into each other.

**Verified:** all 10 prior proof scripts (including `verify_ws_pubsub`
against a genuine second process) re-run with no regressions; the new
`verify_notifications.py` (12 checks) passes; `ruff check .` clean;
`pip-audit` shows only the same pre-existing `pip`-tool CVEs;
frontend `ESLint`, `tsc --noEmit`, and a full `next build` clean.

---

## Slice 26 — Phase 3, slice 1: async ticket triage, and RabbitMQ's
actual first real use

**Correction, stated plainly rather than quietly worked around:**
RabbitMQ was believed to already be present in `docker-compose.yml`,
provisioned since Phase 0 and simply unused. Checked before writing
anything, per this project's own standing rule — it wasn't there. No
service, no client library, no reference anywhere in the codebase.
Built from scratch this slice, not "wired up": the `rabbitmq` service
(with a real app user, not the `guest`/`guest` default, which
RabbitMQ itself refuses to accept from anywhere but localhost — this
app's containers are never that to each other), `aio-pika` as the
client, and a `TriageJob` message contract.

**Built:** `POST /projects/{id}/tickets` now publishes a `TriageJob`
instead of doing anything synchronously, returning immediately with
the ticket `pending_triage`; a new `worker` Compose service consumes
the queue, sets `priority`/`triaged_at`, and pushes the result live
over the board's existing WebSocket room; a small "AI triaging…" badge
clears itself the instant that arrives. No real LLM call yet —
`priority` is a hardcoded placeholder, deliberately not derived from
the ticket's content, so nothing about this slice's output could be
mistaken for actual triage quality. That's next.

**A separate service, not a separate deployable — and specifically not
one that runs its own migrations.** `worker` builds from the exact
same `./apps/api` image and codebase as `api` (the Dockerfile's
`COPY . .` already includes `worker/`); only the container `command`
differs. It deliberately does **not** run `entrypoint.sh` — only one
service should ever execute `alembic upgrade head` at container start,
and `api` already owns that. Two containers racing to migrate on a
simultaneous cold start is exactly the kind of concurrency bug worth
not creating in the first place, not one worth handling gracefully
after the fact.

**`pending_triage` reuses the nullable-timestamp-as-state idiom, not a
new enum.** `priority` and `triaged_at` are both nullable on `tickets`;
`triaged_at IS NULL` *is* `pending_triage`, the same shape as
`accepted_at`/`read_at`/`used_at` elsewhere in this app rather than a
fourth status concept invented for one table. Both are set together,
once, only by the worker — never via `TicketCreate`/`TicketUpdate`.

**Ack/nack: the library's actual default, checked, then deliberately
kept.** aio-pika requires an explicit ack/nack/reject per message
unless a consumer opts into `no_ack=True` — confirmed by reading the
library's real behavior before relying on it, not assumed. That
default (nothing auto-acknowledged) is what `worker/main.py` uses,
on purpose: a message is only acked after the DB commit *and* the
Redis publish both succeed. `prefetch_count=1` is set explicitly too
— the undocumented-but-real default with no QoS configured is for
RabbitMQ to hand a connected consumer every ready message at once,
which would let whichever worker connects first drain the entire
queue and starve a second one, defeating the whole point of running
more than one.

**Both reliability properties requirement 5 asked about were proven
empirically, not reasoned about and left there:**

- **A worker crashing mid-job doesn't lose the job.** Tested in
  complete isolation, on its own throwaway queue rather than the real
  one (so it can't race the actual running worker(s)): a message is
  received but deliberately never acked, then the connection is
  closed — simulating a crash. A fresh consumer on the same queue
  receives the identical message again, and — the stronger check, not
  just "a message arrived" — with RabbitMQ's own `redelivered` flag
  set to `True`, confirming the broker itself recognizes this as a
  redelivery, not a coincidence.
- **Two workers competing for the same queue never double-process a
  job.** A real second worker process (not a mocked one) was started
  and left running; five tickets created concurrently produced exactly
  five triage-completion events over the board's WebSocket room — not
  four, not six. More than five would have meant a job got processed
  twice; this is what actually rules that out, not just RabbitMQ's
  documented competing-consumers guarantee taken on faith.

**Tenant context in a process with no natural request or connection
boundary — the genuinely new variant of a question this project has
now answered three ways.** HTTP has one request; a WebSocket
connection has one handshake, bounded by the same 15-minute staleness
ceiling as everything else. A worker has neither: it's a loop
processing an unbounded sequence of jobs for however many different
orgs happen to publish one, over a lifetime that could span days.
There is no ambient "current org" for the process itself — only
whichever job is being handled *right now*. So context is established
fresh, on a brand-new short-lived session, from that one job's own
payload, every single time — never held across jobs, never assumed
from whatever the previous job happened to be.

That still leaves the question every other defense-in-depth decision
in this app has already asked once: is the job's claimed `org_id`
trusted blindly? No. The very first thing the worker does with it is
an RLS-scoped lookup of the job's `ticket_id` *under that claimed
org's context* — not a raw by-id fetch. Proved this isn't just
theoretical: `verify_triage.py` publishes a job claiming org A's real
ticket under org B's `org_id`, directly, bypassing the API entirely.
The RLS-scoped lookup finds nothing (org B's context can never see org
A's row), the worker rejects it as a poison message, and org A's
ticket comes back completely unchanged afterward — not silently
corrupted, not retried forever.

**A real bug found by actually cold-starting the stack, not assumed
away.** The worker's very first boot crashed outright: RabbitMQ's own
healthcheck (`rabbitmq-diagnostics ping`) can report healthy slightly
before the AMQP listener on 5672 is actually accepting connections,
and aio-pika's `connect_robust` only reconnects *after* an initial
connection succeeds — its first-attempt retry budget isn't unlimited,
and exhausting it raises. With no restart policy on the `worker`
service, that's a permanently dead container; Compose does not retry
a crashed one-shot process on its own. Fixed with `restart:
on-failure` and confirmed working, not just plausible: a forced cold
restart of both `rabbitmq` and `worker` together showed
`RestartCount: 1` on the worker — it crashed once, exactly as
predicted, and healed itself with no manual intervention.

**Extended the proof-script pattern with `scripts/verify_triage.py`,**
reaching directly into `app.services.queue` to construct deliberately
adversarial messages the real API would never publish — the same
"raw internals, not just black-box HTTP" precedent `verify_rls.py`
already established. Covers: `pending_triage` immediately on creation,
before any worker involvement; the live WebSocket update on
completion; tenant isolation for the async path (a second org's board
room never receives another org's triage events); the cross-org
poisoned-job rejection above; both reliability proofs above; and the
competing-consumers proof above. A genuine test-ordering race was
caught and fixed while building this: the live-update check originally
created its ticket *before* opening the WebSocket connection, and with
two idle workers competing, the job routinely finished before the
subscription existed to receive it — Redis pub/sub has no replay for a
not-yet-connected subscriber (already documented in
`ticket_events.py`), so this wasn't a system bug, but it was a real
bug in what the test was actually proving. Fixed by connecting first,
the same order a real client always uses.

**Verified:** all 11 prior proof scripts re-run with no regressions,
including `verify_tickets_rls` (exercises `create_ticket` directly,
now publishing a real job on every run) and `verify_ws_pubsub` against
a genuine second API process; the new `verify_triage.py` (8 checks)
passes with two real competing worker processes running; `ruff check .`
clean; `pip-audit` shows only the same pre-existing `pip`-tool CVEs,
nothing new from `aio-pika`; frontend `ESLint`, `tsc --noEmit`, and a
full `next build` clean.

---

## Slice 27 — Phase 3, slice 2: a real LLM in the triage worker, and a
cross-slice regression it exposed in an unrelated proof script

**Built:** `app/services/llm_triage.py`, replacing Slice 26's
hardcoded placeholder with a real call to Groq (primary) and Gemini
(fallback), plus the schema and worker changes to write and display
its output for real.

**Structured output: `json_object` mode + Pydantic, not Groq's own
`json_schema` mode — checked empirically, not assumed.** Groq's
strict, schema-enforced `response_format={"type": "json_schema", ...}`
was tried first, since it's the stronger guarantee. It returned a real
400: `"This model does not support response format json_schema"` for
`llama-3.3-70b-versatile` — that mode is real, but only on a subset of
Groq's models, and the fast, cost-effective one this slice actually
wants isn't one of them. `{"type": "json_object"}` (loose JSON
mode — guarantees syntactically valid JSON, not schema conformance)
works fine on this model, confirmed the same way. So the exact shape
(`priority`, `labels`, `reasoning`) is spelled out in the system
prompt instead, and every response — from either provider — is parsed
through a `TriageResult` Pydantic model regardless of which mode
produced it. Gemini's `response_schema` (real, SDK-enforced structured
output) was also checked, not assumed, and does work as documented —
worth noting `gemini-2.0-flash` returned a 429 with this key's free-tier
quota for that specific model reported as a literal `0`, a real,
current constraint discovered by trying it, not a hypothetical;
`gemini-2.5-flash` works. Gemini's stronger guarantee doesn't earn it a
pass through validation either — "the SDK enforced it" and "this app
independently confirmed it" are different claims, and `TriageResult`
is what makes the second claim true for both providers alike. Labels
get stripped, lowercased, and capped (5 labels, 30 characters each) in
`TriageResult`'s own validator; a response that parses as JSON but
fails these checks is treated exactly like a timeout or a rate limit —
a failed attempt, not a half-trusted write.

**`priority` moved from a free string to a real `TicketPriority` enum,
and `pending_triage` moved from Slice 26's nullable-timestamp idiom to
a real `triage_status` enum (`pending`/`triaged`/`failed`).** Slice 26
picked the nullable-timestamp idiom deliberately, but flagged its own
limit at the time: it only has two states. This slice adds a genuine
third one — an LLM call can now fail in a way that isn't "hasn't run
yet" — so the idiom stops fitting and a real enum column
(migration 0018) replaces it. `labels` is a native Postgres
`ARRAY(String)`, not `jsonb` or a join table: it's a handful of
free-text, no-independent-identity strings per ticket, which is the
exact shape a Postgres array is for. `jsonb` would be unused
flexibility for data that's already flat; a join table (`Label` +
`TicketLabel`) is the right shape only if labels become an org-wide
managed taxonomy with their own identity, filtering, and rename
semantics — not what's being built here.

**Groq-then-Gemini fallback, with a hard budget, not an unbounded
retry loop.** `triage_ticket()` tries Groq up to
`_MAX_ATTEMPTS_PER_PROVIDER` (2) times, then Gemini up to the same
budget, logging which provider actually served the result — genuinely
useful for debugging, and proven to actually fire, not just wired:
`verify_triage_llm.py` deliberately breaks `_call_groq` for one real
call and confirms the real Gemini API serves a valid result in its
place. Each individual provider call is wrapped in
`asyncio.wait_for(..., timeout=10.0)` — an unbounded hung LLM call
would otherwise hold both the worker and the RabbitMQ message it's
processing indefinitely, the same class of problem Slice 26's
prefetch/ack reasoning already exists to prevent, just from a new
source. If both providers exhaust their budget, `triage_ticket()`
raises `TriageFailed` — a real, terminal outcome, not a silent drop.

**A caught `TriageFailed` is ack'd, not nack'd-and-requeued — the
worker's ack/nack design extended, not reused unchanged.** Slice 26's
ack/nack policy was built for *infrastructure* failures (a DB write or
Redis publish failing mid-job), where `nack(requeue=True)` is correct:
retry the whole job, infrastructure recovers. An LLM failure is a
different kind of failure — a handled business outcome, not a
transient one. `worker/main.py` catches `TriageFailed` specifically,
writes `triage_status="failed"` plus `triage_error` to the real ticket
row, publishes that over the board's existing WebSocket room so a
`pending_triage` ticket never sits invisible forever, commits, and
*returns normally* — letting the outer loop ack the message. Letting a
`TriageFailed` fall through to the generic
`except Exception: nack(requeue=True)` handler instead would restart
the full retry budget from zero on every redelivery, silently burning
API spend on a job that's never going to succeed. This is the concrete
answer to "what happens if both providers fail": a real, visible,
terminal ticket state, not an infinite loop or a swallowed error.

**Frontend:** the board's `TriageIndicator` now shows the real
`priority` (color-coded badge) and `labels` once triage completes, a
`triage_reasoning` tooltip so a human can see *why* the AI picked a
given priority, and a distinct red "Triage failed" state (with the
real error as its tooltip) instead of the old binary
triaged/not-triaged badge — over the same live-update WebSocket
mechanism Slice 26 already built, unchanged.

**CI's proof script hits a local stub, not the real APIs — a
deliberate, explicit departure from this project's real-infrastructure
testing precedent, not a default applied without thought.** Every
other proof script in this repo — RLS, auth, Redis pub/sub, and
Slice 26's own RabbitMQ/worker proof — deliberately runs against real
Postgres, real Redis, real RabbitMQ in CI, on the standing principle
that mocking the thing you're trying to prove works defeats the
purpose. A real LLM API call doesn't fit that principle the same way,
for reasons specific to it and not to the others: Postgres, Redis, and
RabbitMQ in CI are free, fully self-controlled, and deterministic —
spinning one up costs nothing and produces the same result every run.
A real Groq or Gemini call costs real money on every single push to a
public repo, is genuinely non-deterministic even at low temperature
(the same ticket can legitimately classify differently run to run),
and depends on a third party's uptime and rate limits, none of which
this project controls. Mocking `triage_ticket()` itself at the Python
level was rejected too — that would stop exercising the real Groq/
Gemini SDKs' own request construction and response parsing, which is
exactly the code most likely to break silently (a header format
change, a response shape change). The middle path:
`scripts/_fake_llm_server.py`, a minimal local FastAPI server speaking
just enough of each provider's real wire shape (`choices[0].message
.content` for Groq, `candidates[0].content.parts[0].text` for Gemini)
to satisfy the real, unmodified SDK clients' own response parsing —
verified by pointing the actual `AsyncGroq`/`genai.Client` code at it
via `base_url` override, a plain, SDK-native config knob both clients
already support, not a special mock-mode branch grafted onto
production code. CI's `verify_triage.py` run now exercises 100% of
this app's own code (queue consume, prompt build, real HTTP call
through the real SDKs, response parsing, `TriageResult` validation, DB
write, Redis publish) with only the literal network hop swapped for a
deterministic, free, instant one. The real integration — actual model
behavior, actual latency, an actual fallback firing against Gemini's
live API — is what `scripts/verify_triage_llm.py` exists for
separately: not wired into CI, run manually/locally, and explicit in
its own docstring about why (real API credits, real non-determinism,
exactly the two properties CI's own run needed to avoid).

**A real bug caught locally before it could break CI: an empty API
key produces an illegal HTTP header, not a clean auth failure.**
Building the CI wiring for the stub server surfaced this: pointing
`AsyncGroq`/`genai.Client` at *any* base URL with an empty
`api_key=""` — which is `Settings`'s own default — produces
`httpx.LocalProtocolError: Illegal header value b'Bearer '`, raised by
httpx itself before the request ever leaves the process, because a
trailing-whitespace-only header value is invalid per the HTTP spec.
Against the stub server this would have looked identical to "the stub
server is broken," not "the key is unset" — a confusing failure mode
to debug from CI logs alone. Fixed by giving CI job-level
`GROQ_API_KEY`/`GEMINI_API_KEY` deliberately fake but *non-empty*
values (`ci-stub-groq-key` / `ci-stub-gemini-key`) — the stub server
never checks them, so any non-empty string works, but non-empty is
what keeps the header well-formed.

**The cross-slice regression: `verify_ws_pubsub.py` (Phase 2, predates
this slice) started failing, with nothing in its own code touched.**
Running the full regression suite after wiring the stub into CI
surfaced a real failure in `verify_ws_pubsub.py`'s "a mutation on a
different project is never received" check — an `AssertionError` on a
message tab1 wasn't expecting at all. The actual mechanism: that
script creates two fixture tickets as normal test setup (one per
project) the same way it always has, but as of this slice, *every*
ticket creation now also enqueues an async triage job that eventually
publishes its own `ticket.updated` event to that ticket's board-room
channel — a side effect that didn't exist when `verify_ws_pubsub.py`
was written and has nothing to do with the mutation the script is
actually trying to test. tab1 only ever subscribes to its own
project's channel (confirmed via the very `PUBSUB NUMSUB` check
earlier in the same script), so the stray message it received could
only have been that project's own ticket's belated triage-completion
event, landing squarely inside the 1.5-second "expect nothing" window
by pure timing. This is the exact same race class Slice 26's own
`verify_triage.py` had already hit once (a ticket's completion racing
ahead of a not-yet-connected WebSocket subscriber) — just newly
surfaced in a second, older script that had no reason to think about
async triage when it was written, because at the time it was written,
tickets didn't have any async side effect at all. **Fixed by reusing
`verify_triage.py`'s own technique**: added a `wait_until_triaged`
helper and drained both fixture tickets' triage jobs immediately after
creating them, before any WebSocket connection opens — removing the
race by construction instead of widening the timeout and hoping.
Re-run clean afterward, including the cross-process fan-out check
this script exists for in the first place.

**That fix passed locally and then failed in CI — for a reason that
mattered.** The local dry run of the exact CI wiring (stub server, two
stub-pointed workers) passed cleanly, so the fix was pushed. CI's real
run then hit a hard, deterministic timeout: `wait_until_triaged`
waited the full 15 seconds and raised, because in CI at that point in
the job, no worker exists yet at all — `ci.yml` only starts the stub
server and the two worker processes right before the triage-specific
proof, near the very end. Locally this never showed up because dev's
`worker` Compose service is a normal, always-on service that had
simply been running the whole time in the background, quietly
draining the fixture tickets regardless of where in the script order
they were created. The fix that worked locally relied on an assumption
that happened to be true in dev and false in CI — worth stating
plainly rather than glossing over, since "passed in my dry run" turned
out not to be the same claim as "will pass in CI" here. The real fix:
moved the stub-server and worker-startup steps in `ci.yml` from right
before the triage-specific proof to right after the migrations step,
so a real consumer is active for the entire rest of the job, not just
the last few steps of it. This isn't just a workaround for the
timeout — it's the more architecturally honest ordering, since
`worker` genuinely is an always-on service everywhere else this app
runs (local dev, and eventually production); CI had been the one
environment quietly special-casing it to start late, and that's what
made the earlier fix's assumption invisible until CI actually
exercised it. Re-verified locally by reproducing the corrected order
end to end — stopping the persistent dev worker, starting the stub and
two stub-pointed workers immediately (before any proof script runs, as
CI now does), then running `verify_tickets_rls`, `verify_sprints_rls`,
`verify_ws_pubsub`, `verify_notifications`, and `verify_triage` in
sequence — all clean, including `verify_ws_pubsub`'s drain resolving
in well under a second against the stub instead of timing out.

**Audited the other two ticket-creating proof scripts for the same
exposure, rather than assuming the fix belonged in exactly one
place.** `verify_notifications.py` creates a ticket as a fixture too
and has its own "expect no message" check — but on a *structurally
different* channel (`notifications:{user_id}`, not `project:{id}`)
that a triage completion never publishes to, so that specific check
was never at risk. Its later board-room checks (asserting a
`ticket.updated` event arrives and isn't a `notification.created`)
could theoretically still race with a stray triage completion, but
even under that race the assertions would keep passing — a triage
completion *is* a `ticket.updated` event on the same channel, so the
check's literal conditions hold regardless of which specific
`ticket.updated` event arrives first. Left as-is: fixing a race that
can't produce a false failure would be effort spent proving a point
the test doesn't need proven. `verify_ws.py` was the easy case — it
never creates a ticket at all (it only exercises the WebSocket
handshake itself), so it was never exposed to begin with. Checking all
three rather than patching the one that happened to fail is the same
standard this project already holds itself to for RLS gaps and ack/nack
races: a bug's *class*, not just its one observed instance, is what
needs to be ruled out.

**Verified:** all 12 backend proof scripts pass, including the fixed
`verify_ws_pubsub.py` and the CI-representative local dry run of the
new stub-server wiring (stub server + two stub-pointed worker
processes, matching CI's exact env-var setup); `verify_triage_llm.py`
passes against the real Groq and Gemini APIs (outage ticket →
high/critical, cosmetic ticket → low/medium, a real Gemini fallback
firing with Groq deliberately broken, both-broken raising
`TriageFailed`); `ruff check .` clean; `pip-audit` clean, including the
two new dependencies (`groq`, `google-genai`); frontend `npm audit`
(only the same pre-existing, already-tracked moderate `postcss`
finding), `ESLint`, `tsc --noEmit`, and a full `next build` all clean.

---

## Slice 28 — Phase 3, slice 3: natural-language ticket creation, reusing
the triage slice's LLM plumbing instead of a second parallel path

**Built:** `POST /projects/{id}/tickets/parse` — takes raw text ("create
a bug for login failing on Safari, high priority"), asks Groq (Gemini on
fallback) to extract a candidate title/description/type/priority/labels,
and returns it for review. Creates nothing by itself. A user reviews
and edits the candidate, then confirms through the exact same
`POST /projects/{id}/tickets` every other ticket already goes through —
no second creation endpoint, no "already parsed" flag.

**Synchronous, not published to RabbitMQ — the deciding question is
"is there a caller actively waiting on this result," not "does it call
an LLM."** Triage (Slice 27) is async because it runs *after* ticket
creation has already succeeded and returned to the caller — nobody is
blocked on its result, so paying LLM latency inline on every creation
would be pure cost with no user-facing benefit, which is exactly why
the "AI triaging…" pending-state UI exists at all. Parsing inverts
that: the entire reason to call this endpoint is to get the result back
and show it to the user before anything is created. There's no
"successful action" to acknowledge early and finish in the background —
the HTTP response *is* the useful output. A background-job version
would need its own polling or WebSocket-delivery mechanism for a single
one-off request the caller is already sitting on a connection for: real
added complexity bought for zero benefit, given parse latency (one
bounded LLM call, ~1-3s) is well within normal HTTP tolerance. Both
calls hit the same providers for the same kind of reason (structured
extraction from text) — what differs is only ever *who's waiting and
for what*, not the LLM plumbing underneath, which is why extracting
that plumbing into a shared module (next) made sense while the
sync/async split around it didn't need to.

**`app/services/llm_client.py` (new): the Groq-primary/Gemini-fallback
mechanics extracted out of `llm_triage.py`, once ticket parsing became
a second real call site needing the identical behavior** — timeout per
call, a bounded retry budget per provider, provider fallback order,
and validating the raw response through a caller-supplied Pydantic
model before trusting it. Two real call sites is what justified this
now; a single one, or a hypothetical future one, wouldn't have — this
project's own standing rule against building abstractions ahead of
actual need, applied to its own LLM code instead of just app features.
`llm_triage.py`'s public API (`TriageFailed`, `TriageResult`,
`triage_ticket`) is byte-for-byte unchanged — `worker/main.py` needed
zero edits, since `triage_ticket()` now just catches the shared
module's generic `LLMCallFailed` and re-raises it as the same
`TriageFailed` callers have always caught. `ticket_parse.py` gets its
own `TicketParseUnavailable` for the same reason: each call site keeps
a domain-specific failure name for its own callers to catch, even
though both are raised from the one shared `LLMCallFailed` underneath.
Label-list validation (strip/lowercase/cap count/length) moved to
`app/services/llm_labels.py` too — `TriageResult` and the new
`ParsedTicketCandidate` both ask a model for "a few short, lowercase,
free-text labels" and both need to distrust it identically, so that
rule lives once.

**`ParsedTicketCandidate.confident` is a first-class field the model
sets, and a second, independent gate this app enforces on top of it —
never trusting the model's own claim about itself any more than its
factual output.** The system prompt instructs the model to set
`confident: false` and explain why in `clarification` rather than
fabricate a title or type it isn't sure about — the model is asked to
be honest. But asking isn't the same as trusting: a `model_validator`
on `ParsedTicketCandidate` independently re-checks the shape that
`confident` value actually implies. `confident=true` requires both
`title` and `type` to genuinely be present, and rejects `type=subtask`
outright (a subtask needs a specific parent ticket that free text alone
can never supply — the prompt asks the model to avoid it too, but this
is the actual enforcement, not the request). `confident=false` requires
`clarification` to actually be present, not empty. A response that
claims `confident=true` but is missing `title` fails this validator and
counts as a failed attempt in `call_llm`'s retry loop — exactly like a
timeout, a malformed JSON body, or a rate limit — never a half-trusted
result written through because the model's own flag said it was fine.
Confirmed empirically against both real providers, not assumed: a clear
input ("create a bug for login failing on Safari, high priority")
returns `confident: true` with a real title/type from both Groq and a
forced Gemini fallback; a deliberately ambiguous one ("asdf lol
whatever") returns `confident: false` with a real, specific
`clarification` from both — including Gemini's `response_schema`
correctly handling every field as nullable, not just the previously-
proven all-required shape from triage.

**Two failure classes, two different HTTP responses, on purpose.**
Both providers exhausting their attempt budgets (`TicketParseUnavailable`)
is a real infrastructure/provider failure — nothing about the input is
the problem, Groq and Gemini were just unreachable or kept erroring.
That's a 503: *"couldn't reach the AI parsing service right now, try
again, or fill in manually."* `confident=false` is the opposite: the
service worked perfectly and gave an honest, correct answer — "I looked
at this and can't confidently tell." That's a 200, because it isn't an
error at all; collapsing it into a 5xx would tell the frontend (and a
future reader of this code) that something broke when nothing did.
Conflating these two into one generic "parse failed" response would
have thrown away the one distinction that actually matters to a user
deciding what to do next: retry the exact same request (infra hiccup)
versus rephrase or just fill in the form (their input genuinely wasn't
clear enough).

**Frontend: review step lets you edit title/description/type — what
actually gets submitted — but shows `priority`/`labels` as a read-only
"AI preview," not editable form fields, even though the candidate
carries both.** Every created ticket still gets async-triaged exactly
as before (Slice 27), regardless of whether it came from this flow or
the plain form — confirmed directly rather than assumed (see the last
proof-script check below), and that async result is the actual source
of truth for a ticket's real priority/labels, not this preview.
Editing the preview values would therefore be inert: whatever a user
typed into those fields would never be sent anywhere (`TicketCreate`
still only accepts `type`/`title`/`description`, unchanged since
Slice 1) and would be silently overwritten the moment triage completes
regardless. Offering editable fields that quietly do nothing is worse
than not offering them — the read-only preview, captioned "finalized
after creation," tells the truth about what's actually about to happen
instead of implying a control that isn't real. A low-confidence
response never pre-fills the review form at all — its own fields aren't
promised reliable even when partially present (see the model's own
docstring) — and offers "try rephrasing" or "fill in manually" (seeded
with the user's own raw text as a starting title, not a fabricated
guess) instead.

**Extended the proof-script pattern with `scripts/verify_ticket_parse.py`**
against the same local LLM stub as `verify_triage.py`, for the same
reasoning (free, deterministic, exercises every real line of this app's
own code). `scripts/_fake_llm_server.py` became content-aware to serve
this: it reads the same prompt text a real model would read and
distinguishes a triage request from a parse request by each one's own
system-prompt wording, and a confident parse from a low-confidence one
via a plainly-named sentinel (`stub_trigger_low_confidence`) the proof
script puts in its own deliberately-ambiguous test input — visible in
both places, not a hidden test-only hook. Covers: a confident parse
creates nothing by itself (ticket count unchanged before/after); a
low-confidence parse is honestly reported, never fabricates a title or
type, and also creates nothing; tenant isolation on the new endpoint
specifically (org B parsing under org A's project id → 404), not
assumed just because it reuses `create_ticket`'s own project-lookup
helper; and — the composition Slice 3's own brief asked to have
confirmed, not assumed — confirming a parsed-and-edited candidate
through the normal creation endpoint still starts the ticket
`pending_triage` and still reaches `triaged` via the real worker
exactly like any other ticket.

**Verified:** all 12 backend proof scripts pass, including the new
`verify_ticket_parse.py`, re-run against the CI-representative local
stub setup (stub server + two stub-pointed workers + a stub-pointed API
process, matching CI's job-level env exactly); the parse prompt
confirmed empirically against the real Groq and Gemini APIs for both
the confident and low-confidence cases, and a real Gemini fallback with
Groq deliberately broken; `ruff check .` clean; `pip-audit` clean;
frontend `ESLint`, `tsc --noEmit`, and a full `next build` all clean.
No browser-level UI verification this slice — no browser automation
tool was available in this environment, so the frontend flow was
verified by exercising the exact same HTTP endpoints the UI calls
(`verify_ticket_parse.py`) plus clean lint/typecheck/build, stated
here plainly rather than implied as equivalent to having actually
clicked through it.

---

## Slice 29 — Phase 3, slice 4: semantic duplicate detection, and what
13 real ticket pairs revealed about the actual limits of the approach

**Built:** `tickets.embedding` (pgvector `vector(768)`, migration 0019
— the first real use of the extension since it was locked into the
Postgres image in Phase 0), populated asynchronously by a new
`EmbedJob`/`ticket_embedding` queue after ticket creation and again
after any edit that touches title/description, and a synchronous
`POST /projects/{id}/tickets/check-duplicates` endpoint that embeds a
draft ticket's text and queries for existing tickets above a cosine
-similarity threshold — surfaced as a non-blocking warning in both the
manual creation form and the NL-parse review step, never a hard gate.

**Groq has no embedding model at all — confirmed with a real 404, not
assumed.** `AsyncGroq`'s client exposes an `embeddings` attribute, but
that's inherited from the OpenAI-compatible SDK base it's built on, not
a real Groq capability: its live model list has no embedding model on
it, and calling `client.embeddings.create(...)` returns
`"model_not_found"`. Unlike triage and ticket-parsing — both genuine
Groq-primary/Gemini-fallback cases — there is no second provider here.
Gemini's `gemini-embedding-001` is the only real option, called with
`output_dimensionality=768` rather than its own 3072-dim default: not
an arbitrary shrink, but a real constraint driving the choice —
pgvector's ANN index types (HNSW, IVFFlat) have a hard 2000-dimension
ceiling, so a 3072-dim column could never be indexed at all, only ever
sequentially scanned. 768 is one of Gemini's own documented Matryoshka
-trained reduced sizes (alongside 1536), not a naive truncation, and
stays comfortably under that ceiling if this project's ticket volume
ever grows enough to justify an index later — no second migration to
shrink the column first if that day comes. No index is created yet at
today's scale (a portfolio app's handful of demo projects, not
millions of tickets) — pgvector's own guidance is to build IVFFlat/HNSW
*after* a table has representative data, not speculatively on an empty
one, and a plain sequential scan with `<=>` is fast enough here.

**Two queues, two jobs, one process — not triage plus one more step.**
Both triage and embedding are async for the identical reason: nobody
is waiting on either result at ticket-creation time, so paying either
one's latency inline would be pure cost with no user-facing benefit.
That answers *whether* to background embedding generation; it doesn't
answer whether it should be the *same* background job as triage — a
separate question, decided by thinking through what combining them
would actually cost, not by default. Folding embedding into
`_process_job` would couple two outcomes that have nothing to do with
each other: different providers entirely (Groq-then-Gemini for triage;
Gemini-only for embedding), independent failure modes, no shared
cause. A single combined job forces one ack/nack decision to cover
both — nack-and-requeue on a transient embedding hiccup would silently
re-run an already-succeeded triage call, re-paying for a real LLM
classification nothing asked to redo; acking regardless of the
embedding outcome would need its own internal partial-failure
bookkeeping just to know a retry is still owed. Two independent
messages (`TriageJob` on `ticket_triage`, `EmbedJob` on
`ticket_embedding`) sidestep this entirely — each gets its own
ack/nack lifecycle, its own retry budget, its own terminal-failure
handling, and neither's outcome has any bearing on the other's. That
argues for two *jobs*, not two *services*: both still run in the one
`worker` process, on two concurrent consumer loops sharing one AMQP
connection but separate channels, each with its own
`prefetch_count=1` QoS so one queue's traffic can never starve the
other's fair share. The independence that matters is at the message
level, not the deployment topology — duplicating the container for a
distinction that doesn't need it would be the same premature-topology
mistake this app avoids everywhere else.

**`EmbedJob` carries only `ticket_id`/`org_id` — deliberately not
title/description, unlike `TriageJob`, and that difference is the
entire fix for stale embeddings on edit.** `TriageJob` carries a copy
of the title/description it was published with, read directly by
`triage_ticket()` — correct for triage, where the job is a one-shot
classification of whatever the ticket said *at creation time*.
Embedding has a second trigger `TriageJob` never had: an edit, any
time later, to the same fields the embedding was built from. If
`EmbedJob` copied `TriageJob`'s shape, an edit would need to reason
about *which* queued job is newest before trusting its payload — a
real ordering hazard if two edits land close together. Instead
`_process_embed_job` always re-reads title/description fresh from the
ticket row at process time, and `EmbedJob` carries nothing to make
that unnecessary. That makes an `EmbedJob` a trigger — "something
about this ticket may have changed, re-embed it" — not a snapshot of
what to embed, and firing it on every title/description-touching PATCH
(exactly like create_ticket already does) becomes trivially safe with
no ordering assumption: whichever `EmbedJob` for a given ticket happens
to run *last* always embeds whatever the ticket's text actually is
right now, regardless of how many are queued or in what order they
arrive. Verified for real, not just reasoned about: created a ticket
about CSV export, confirmed a Safari-login draft didn't match it,
edited its title/description to be about Safari login, and confirmed
it then did — the stored embedding demonstrably changed to the new
text's value, not merely stopped matching the old one.

**The threshold-tuning finding — worth real space, not a line item.**
`scripts/tune_duplicate_threshold.py` ran 13 real ticket-title/
description pairs through the real Gemini embedding API, across three
categories. SIMILAR pairs (the same underlying bug or feature request,
reworded — e.g. "Login fails on Safari" / "Safari login broken";
"Password reset email never arrives" / "Reset password email not
being delivered") scored 0.8351-0.9185. DIFFERENT pairs (genuinely
unrelated — e.g. "Login fails on Safari" / "Add CSV export to reports
page") scored 0.4632-0.5207 — a clean, unambiguous >0.3 gap between
the two clusters, confirming semantic embeddings are doing real work
at the topic level. The finding is the third category: ADJACENT pairs
— same category, a genuinely *different* specific issue (e.g.
"Password reset email never arrives" / "Verification email never
arrives"; "Export tickets to CSV" / "Export tickets to PDF"; "Add dark
mode toggle" / "Add light mode toggle"; "Login fails on Safari" /
"Login fails on Firefox") — scored 0.7921-0.9300, a spread that
genuinely overlaps *both* other clusters. "Password reset" and "email
verification" are topically almost the same thing (an email that
doesn't arrive) even though they're different bugs with different root
causes; cosine similarity on a topic-level embedding has no way to
represent that distinction, because the distinction isn't really about
topic at all. This is not a tuning failure fixable with a better
threshold — no single number can sit above every SIMILAR score and
below every ADJACENT score, because the two ranges (0.8351-0.9185 and
0.7921-0.9300) actually overlap. It's a real, structural precision
ceiling on what topic-level semantic similarity can distinguish, and
it's the direct, concrete justification for point 5 of this slice's
own brief: a non-blocking warning, not a hard gate. A hard gate would
periodically block a legitimately new ticket ("add light mode") just
because it resembles an existing one ("add dark mode") closely enough
in vector space, with no way to tell the difference from inside the
embedding alone — a false positive with a real cost (a blocked
action), not just a mildly annoying flag. `SIMILARITY_THRESHOLD = 0.83`
sits just below the observed SIMILAR floor (catches every tested
genuine duplicate) and enormously above the observed DIFFERENT ceiling
(excludes every tested unrelated pair, large margin), landing inside
the ADJACENT cluster's own real spread — exactly where a threshold
belongs, given that cluster's scores genuinely straddle both of the
others. An occasional flag on a merely-related (not actually
duplicate) ticket costs a human one glance; a wrongly blocked creation
costs real friction for no correctness gained.

**RLS and the composite-FK-equivalent discipline apply cleanly to a
similarity query too, confirmed rather than assumed** — the first
query shape in this app that orders by a distance instead of an
equality filter. RLS already restricts every row a session can see to
the caller's own org; `project_id = :project_id` is the same explicit,
defense-in-depth filter `_get_ticket_or_404` and friends already apply
on top of RLS, not instead of it — a project id from a *different*
project in the *same* org must still never leak in, which org-only RLS
scoping wouldn't catch alone. None of that is specific to vector
search; `ORDER BY embedding <=> :query_vector` just decides order and
cutoff *within* that already-correctly-scoped row set, no differently
in principle than `ORDER BY created_at` elsewhere. Proven directly:
`verify_duplicate_detection.py` creates three tickets carrying the
*exact same* stored embedding across three different scopes (same
project, a different project in the same org, a different org
entirely) and confirms a matching query in the first project surfaces
only that project's own ticket — never the other two, despite the
underlying vectors being byte-for-byte identical, which is precisely
what would leak through first if either scoping layer were broken.

**Extended the proof-script pattern with two scripts, deliberately
split the same way triage's real-vs-stub proofs already are.**
`scripts/_fake_llm_server.py` gained a content-aware `embed_content`
handler for `verify_duplicate_detection.py` (CI-wired): since there's
no cheap way to approximate real semantic meaning, it returns one of
three *geometrically constructed* vectors (two orthonormal basis
vectors in 768-dim space, mixed by a chosen angle) with exactly known
cosine similarities — 0.9 for a "near" pair, 0.0 for a "far" pair —
selected by a plainly-named sentinel in the request text, the same
"visible in both places, not hidden" convention `stub_trigger_low
_confidence` already established last slice. This tests the plumbing
this app's own code actually owns (the pgvector query, its project/org
scoping, the threshold comparison, re-embedding on edit)
deterministically and for free, every push. Whether Gemini's real
embeddings are semantically good enough for the tuned threshold to
mean anything against real ticket text is a different question,
answered separately and manually by
`scripts/tune_duplicate_threshold.py` against the real API — not
wired into CI, for the same cost-and-non-determinism reasoning already
established for `verify_triage_llm.py`.

**Verified:** all 13 backend proof scripts pass, including the new
`verify_duplicate_detection.py`, re-run against the CI-representative
local stub setup (stub server + two stub-pointed workers + a stub
-pointed API process, matching CI's job-level env exactly);
`tune_duplicate_threshold.py` passes against the real Gemini API with
the exact numbers documented above; `ruff check .` clean; `pip-audit`
clean, including the new `pgvector` dependency; frontend `ESLint`,
`tsc --noEmit`, and a full `next build` all clean. Manually verified
end to end in a real browser this slice (screenshots reviewed
directly): a near-duplicate correctly warns with a similarity score
and a link, a genuinely different ticket creates with no warning, a
second project never sees a match despite identical underlying text,
and editing a ticket's title/description measurably changes what it
matches against afterward.

---

## Slice 30 — Phase 3, slice 5: the AI Security Champion, this project's
headline AppSec differentiator

**Built:** a small, explicit, human-curated trigger library
(`app/services/appsec_triggers.py`) covering five security-relevant
categories — file upload, auth/permission changes, payment/PII
handling, external API calls, admin permission changes — each with a
keyword list and a concrete checklist. A ticket whose title/description
matches any category is flagged synchronously, at creation or edit
time, with zero LLM involvement in that decision. A matched ticket then
gets a real LLM call (Groq-primary/Gemini-fallback, via the same
`llm_client.py` triage, parsing, and duplicate detection all build on)
that writes one tailored comment applying the matched categories'
checklists to what the ticket actually says — not a templated dump of
the static list. Surfaced on the board as an accent-colored "AppSec
review required" badge.

**Two honest gaps, checked directly rather than assumed away, with a
concrete fallback for each.** This project has no ticket-comment
system — verified by grepping every model, migration, and API route in
`apps/api`, not inferred from its absence being unsurprising; the
codebase already carried explicit "no comment system exists" notes
from Slice 27's `triage_reasoning`, which hit this exact gap first.
`appsec_comment` is a plain field on the ticket, following that same
precedent rather than building on top of infrastructure that was never
actually there. Separately, this app has no standalone ticket-detail
page at all — every ticket is managed inline on the board, backlog, or
sprint views, confirmed by checking every route under
`app/(shell)/projects/[projectId]/`. Requirement 5's "badge on the card
and in the ticket detail view" narrows to just the card, the only place
a ticket is ever actually shown; there's no second surface to add a
badge to yet, and inventing a whole new page to satisfy the letter of
a requirement whose premise doesn't hold here would be scope invented
for its own sake, not a real gap this slice needs to close.

**Why the keyword-gate-then-LLM design is more strongly justified here
than the same shape was for triage or embedding.** Triage and embedding
call an LLM unconditionally, for every ticket, because every ticket
genuinely needs a priority and a duplicate-detection vector — there's
no meaningful gate to put in front of a question every ticket has a
real answer to. AppSec review is a different shape of question
entirely: "does this specific ticket carry security implications,"
where the honest answer for the large majority of tickets (a copy
change, a color tweak, a backlog reorder) is no. Paying for an LLM call
on every single ticket to arrive at "nothing to flag" would be cost
spent for zero signal, on a scale the other two features don't share.
That's the *cost* argument, and it would be sufficient on its own — but
there's a second, independent argument specific to this being a
*security* control, worth stating in full rather than folding into the
cost point: a security process benefits from being predictable and
auditable in a way a priority label doesn't. A rule-based gate is
something a security-minded reader can open and read — "these five
categories, these keywords, this is what gets checked" — and extend
deliberately, by adding a keyword or a whole new category. An LLM
silently deciding "nothing to see here" on a security-relevant ticket,
with no fixed vocabulary to point to afterward and ask *why not*, is a
worse failure mode for this specific kind of control than for a
priority label that's wrong: a missed triage priority gets corrected
next time someone looks at the ticket; a missed security review can
mean a real vulnerability shipped without anyone ever having looked.
Determinism and auditability aren't a consolation prize for skipping an
LLM call here — for a security-relevant gate specifically, they're a
property worth having independent of cost.

**The accepted false-negative tradeoff, stated as a real limitation, not
glossed over.** A plain substring match against title/description has
real, honest recall limits: a ticket that says "let users attach
supporting documents" without ever using the word "upload" will not
match `file_upload`, even though a human reviewer would recognize it
instantly. This is an accepted, deliberate tradeoff, not an oversight —
the keyword lists in `appsec_triggers.py` lean wide rather than
precise specifically to manage it (`file_upload` alone matches on
"upload," "attachment," "attach a file," "import file," "import csv,"
"csv import," "image upload," "profile picture," "avatar," and "file
input"), accepting more false positives (an extra, cheap LLM call
spent on a ticket that turns out not to need one) in exchange for fewer
false negatives (a real security-relevant ticket going completely
unflagged). That asymmetry is deliberate: a false positive here costs
a human a few seconds skimming a note they can dismiss; a false
negative costs nothing visibly, which is exactly what makes it
dangerous. The keyword lists are living data, meant to be extended as
real gaps get discovered — an explicit maintenance surface, not a
one-time list.

**A third, fully independent job and queue — the same coupling argument
from triage vs. embedding, reapplied rather than assumed to transfer
automatically.** AppSec review's own LLM call has nothing to do with
whether triage or embedding succeed: different prompt, independent
failure mode, no shared cause. Folding it into either existing job
would recreate the exact problem Slice 29 already worked out for
triage vs. embedding — nack-and-requeue on an AppSec-specific hiccup
would redundantly re-run an already-succeeded triage or embedding
call, silently re-paying for LLM work nothing asked to redo. `AppSecJob`
gets its own queue (`ticket_appsec_review`), its own ack/nack lifecycle,
consumed by a third concurrent loop in the same `worker` process (three
channels now, each with independent `prefetch_count=1` QoS) — no new
deployment topology, since the independence that matters is at the
message level, not the process level.

This case does introduce one genuinely new property triage and
embedding don't share, addressed directly rather than left as an
unstated wrinkle: `AppSecJob` is the first of the three that's
*conditionally* published. `TriageJob` and `EmbedJob` go out for every
ticket, unconditionally; `AppSecJob` only goes out when the keyword
gate already matched, synchronously, before anything is published —
most tickets never publish one at all. Does that change the separate-
queue conclusion? No — and it's worth being precise about why not: the
coupling argument above is about what happens *once a job exists and
is being processed* (does its ack/nack outcome affect another job's
retry budget), not about how often a job gets created in the first
place. Conditional publishing changes this queue's traffic volume
relative to the other two; it has no bearing on whether that traffic,
once it exists, should share a message with a different job type. The
two questions are independent, and only one of them is what the
separate-queue decision actually turns on.

**The flag is additive-only, and never silently cleared by an edit — a
deliberate divergence from `EmbedJob`'s own trigger-not-snapshot
precedent, not a reuse of it.** `EmbedJob` carries no title/description
and the worker always re-reads the ticket's current text, specifically
so a stale embedding can never survive an edit — whichever `EmbedJob`
runs last always reflects the ticket's current reality, and that's
unambiguously correct for embeddings: a vector describing text a
ticket no longer has is simply wrong, with no legitimate reason to keep
it around. `AppSecJob` reuses that same "always re-read current data"
mechanic for *deciding what's newly relevant* — but `_apply_appsec_
trigger` was deliberately written so an edit can only ever add
categories to `appsec_categories`, never remove one, and
`appsec_review_status` is never reset back to NULL once a ticket has
ever been flagged. That's not an oversight carried over from copying
`EmbedJob`'s shape; it's the opposite choice, made because a security
flag is a fundamentally different kind of data than a similarity
vector. An embedding has no meaning independent of the text it was
computed from, so letting it track that text exactly, always, is
strictly correct. A security flag means something different: it's a
record that *at some point, real security-relevant wording appeared on
this ticket*, and a human may or may not have acted on it yet. If a
later edit that merely rewords the security-relevant part (or removes
it without actually addressing the underlying concern) silently
cleared the flag, the system would be actively erasing evidence that a
review was ever warranted — the security equivalent of a vulnerability
report closing itself the moment someone edits its title. The right
analogy isn't `EmbedJob`; it's how a real security review process
works: a flag is raised by evidence and cleared by a human decision,
never by the evidence quietly changing shape. No "dismiss" action
exists yet in this slice — a real gap, left explicitly for future work
rather than papered over with an auto-clear that would defeat the
entire point of the flag persisting in the first place.

**Extended the proof-script pattern with two scripts, the same real-
vs-stub split already established for every other LLM-backed feature
in this app.** `scripts/verify_appsec_triggers.py` (CI-wired) starts
with pure, synchronous, no-HTTP assertions — `match_triggers` has no
I/O at all — covering all five categories with a realistic matching
ticket *and* a realistic unrelated one each (a real false-positive/
false-negative check, not just "the mechanism runs"), then exercises
the full async pipeline against the local stub (synchronous flagging
before the worker runs, the worker completing a review and storing a
real comment, an edit introducing a new category triggering a fresh
review, a second edit's new category merging in rather than replacing
the first, and the same cross-org poison-job rejection check already
proven for `TriageJob`). `scripts/verify_appsec_review_llm.py` (not
CI-wired, real Groq/Gemini APIs, run manually) is where "genuinely
tailored" actually gets checked, since the stub's deterministic
response has no real language understanding to judge that against:
two different tickets in the same category must produce genuinely
different comments, each comment must reference a concrete detail
specific to its own ticket's wording, and no raw checklist sentence
may appear verbatim — plus the same real-Gemini-fallback and both-
providers-fail checks already proven for triage and parsing.

**Verified:** all 15 backend proof scripts pass, including the new
`verify_appsec_triggers.py`, re-run against the CI-representative
local stub setup (stub server + two stub-pointed workers + a stub
-pointed API process, matching CI's job-level env exactly);
`verify_appsec_review_llm.py` passes against the real Groq and Gemini
APIs, including the tailored-comment checks and a real Gemini fallback
with Groq deliberately broken; `ruff check .` clean; `pip-audit`
clean; frontend `ESLint`, `tsc --noEmit`, and a full `next build` all
clean. Manually verified end to end against the real APIs (not just
proof-script assertions): a file-upload ticket flags synchronously
with the right category, the worker's completed comment references
the ticket's own specifics (the JPG/PNG avatar, the Stripe invoice
logo), an unrelated ticket never flags at all, and editing a trivial
ticket into security-relevance triggers a real, correctly-merged
review.

---

## Slice 31 — Phase 4, slice 1: the AI sprint summary agent, and a real
data-loss bug it surfaced along the way

**Built:** when a sprint completes, a structured retro is generated
async — a short narrative, what got done, what didn't, and risk
flags/notable blockers, sectioned rather than a wall of text
(`app/services/sprint_retro.py`, via the same Groq-primary/Gemini-
fallback `llm_client.py` triage, parsing, duplicate detection, and
AppSec review all build on). Stored on the sprint row, never
regenerated on view, with a manual regenerate action available. Surfaced
on the sprint page as an accent-colored panel matching the existing
AI-feature vocabulary, polling while generation is in flight.

**A real data-loss bug, found while building this slice and fixed on
its own, ahead of the feature.** `complete_sprint` (migration 0012) has
always auto-returned unfinished tickets to the backlog with a bulk
`UPDATE ... SET sprint_id = NULL`. That was, and still is, correct for
what the sprints feature originally needed — unfinished work goes back
to the backlog, immediately plannable again. Nobody noticed at the time
that the UPDATE is also destructive in a way nothing downstream could
ever recover from: once `sprint_id` is NULL, there is no remaining
query that can ever again say "this ticket used to be on sprint X and
didn't finish." A ticket that stayed in the sprint's terminal column
keeps `sprint_id` pointing at that sprint forever, deliberately, for
velocity history — a returned ticket got no equivalent. This was never
a functional bug against anything the sprints feature itself needed;
the backlog return worked, and still works, exactly as designed. It
became a real bug the moment this slice needed to know what had been
returned, and found that history had already been silently erased for
every sprint ever completed before the fix — a correct-in-isolation
change quietly breaking a later assumption it had no way to know about,
the ordinary shape a latent data-loss bug takes. Fixed by capturing a
snapshot (ticket number, title, story points) of exactly which tickets
are about to be returned, before the bulk `UPDATE` runs, in the same
transaction `complete_sprint` already uses (migration 0021,
`retro_returned_snapshot`). Committed on its own, ahead of and
independent of the retro feature: it stops the data loss regardless of
whether anything ever reads the column, which is exactly why it's a fix
and not a feature.

**The data audit: what this app actually has to build a retro from,
checked directly rather than assumed from the original roadmap.** The
roadmap for this slice implicitly assumed a retro could draw on ticket
state-change history — when a ticket moved into the sprint, how long it
sat in progress. That data does not exist anywhere in this codebase.
There is no audit/activity/event-log table at all, checked directly
against every model in `app/db/models.py`, not inferred from its
absence being unsurprising. `Ticket.updated_at` is a single blanket
"last touched" timestamp covering any field change whatsoever — title,
description, `workflow_state_id`, `sprint_id`, assignee, all the
same column — not a per-transition record; there is no way to answer
"when did this ticket enter this sprint" from anything this app
persists. The two timestamp-shaped fields `Ticket` does have
(`triaged_at`, `appsec_reviewed_at`) are narrow AI-pipeline fields with
nothing to do with board or sprint movement. There is also still no
ticket-comment or activity-log system — the same gap Slice 30's AppSec
work already found and worked around — so there's no qualitative "what
actually happened" input beyond each ticket's own title and story
points, and where it ended up. Net result: the context bundle this
slice builds (`app/services/sprint_retro_context.py`) is honestly
thinner than the roadmap assumed — a structural summary of what
finished and what didn't, by title and points, no timeline, no
qualitative history — designed around what's real rather than padded
out with fabricated structure to match an assumption that didn't
survive contact with the actual schema.

**Sync vs. async, reasoned fresh rather than assumed from precedent.**
A user clicking "Complete sprint" could superficially resemble either
of this app's two existing patterns — a single deliberate click,
waiting for *something* to come back, the same shape as NL ticket
parsing. But what actually decided sync-vs-async both previous times
was never "is the user waiting" in the abstract; it was whether the
*next thing the user needs to do* is gated on the result. NL parse is
sync because the user cannot proceed — review, edit, submit — without
the parsed fields; there is no version of that flow where returning
immediately and filling the ticket in later makes sense. Ending a
sprint has no such gate: the action the user actually wants (the sprint
ends, unfinished work returns to the backlog, the board reflects it) is
fully complete the instant `complete_sprint`'s own transaction commits,
with nothing about a retro's content required to make that true or
usable. Nobody is blocked on retro text to start the next sprint or
re-plan the backlog; the retro is something read once, not a value the
UI needs synchronously. That's async — the same conclusion
triage/embedding/AppSec reached, but reasoned fresh on its own terms,
plus one reason specific to this endpoint: `complete_sprint` is the one
place in this app that mutates real planning state for a whole team in
a single transaction, and coupling that to an LLM call's latency (or a
provider outage) would mean a flaky external API could make ending a
sprint itself slow or fail — a worse failure mode than a slow triage,
because it blocks real team workflow rather than just delaying an
enrichment.

**A fourth, fully independent queue — the first job scoped to a
Sprint, not a Ticket.** Decided the same way triage vs. embedding vs.
AppSec was, not inherited by default: `SprintRetroJob` shares neither
the other three's failure characteristics nor their trigger entity. An
LLM outage failing a sprint retro has nothing to do with whether some
ticket's triage, embedding, or AppSec review succeeded, so folding it
into any existing queue would recreate the exact ack/nack coupling
problem Slice 29 and Slice 30 already worked out — and unlike those
three, this is also a different *kind* of job entirely: a different
consumer (`Sprint`, not `Ticket`), a different payload shape, a
different row to write the outcome onto. The job payload still stays
"trigger, not snapshot" (`sprint_id`/`org_id` only), but for a narrower
reason than `EmbedJob`/`AppSecJob`: the worker does *not* re-derive
which tickets were returned from live ticket state, because by the
time the job is consumed that information no longer exists there at
all — it only exists because `complete_sprint` already persisted the
returned-ticket snapshot onto the sprint row itself, in the same
transaction that completed the sprint, before the job was even
published. The worker re-reads the `Sprint` row fresh rather than
trusting a payload-carried snapshot, same defense-in-depth instinct as
everywhere else in this app — that row already has everything it needs.

**No reopen path exists for a completed sprint — confirmed, not
assumed, and it's what justifies store-once regeneration semantics.**
`SprintUpdate` deliberately excludes `status` (see its own docstring in
`app/schemas/sprint.py`, written back in the original sprints slice);
`start_sprint` only accepts a `PLANNED` sprint, `complete_sprint` only
accepts an `ACTIVE` one. There is no code path anywhere in this app
that can move a sprint's status backward once it's `COMPLETED` —
checked directly against every sprint-status transition in
`app/api/sprints.py`, not assumed from the absence of an obvious
"reopen" button. That means the set of tickets a completed sprint's
retro is built from — `retro_returned_snapshot` plus whatever still
points at `sprint_id` — is permanently frozen the moment
`complete_sprint` commits; the underlying data can never invalidate a
stored retro after the fact. What *can* still change post-completion is
the sprint's own name/goal (`SprintUpdate` allows both regardless of
status), which could leave a previously-generated retro's prose
referencing a goal that's since been reworded — a real but cosmetic
staleness, not a data problem, and not disproportionate enough to
justify auto-regenerating (and re-paying for an LLM call) on every such
edit. A manual regenerate endpoint covers it instead, guarded only
against overlapping in-flight generation (rejecting a second regenerate
call while `retro_status` is still `PENDING` with a 409) — not against
staleness the underlying data can't actually have.

**Extended the proof-script pattern.** `scripts/verify_sprint_retro.py`
(CI-wired) covers tenant isolation (a job claiming the wrong `org_id`
rejected, the real row untouched — the same cross-org poison-job check
already proven for `TriageJob`/`EmbedJob`/`AppSecJob`), every edge case
this slice's own spec named explicitly — a sprint with zero completed
tickets, a sprint with exactly one ticket, a sprint where everything
got auto-returned, and (added on top, belt-and-suspenders) a sprint
with literally zero tickets ever assigned — asserting well-formed,
non-crashing, non-garbage output for each rather than just "the
mechanism runs," and the stored-not-regenerated behavior: two
consecutive `GET`s return byte-identical retro content, a regenerate
call is rejected for a non-completed sprint (400) and for a sprint
already regenerating (409), and a successful regenerate produces a
provably new `retro_generated_at`. The local LLM stub
(`scripts/_fake_llm_server.py`) gained a fourth branch, distinguished
the same content-aware way as the other three (a phrase unique to
`sprint_retro.py`'s own system prompt) — but unlike AppSec's category-
key echo, this stub reads the real completed/returned ticket *counts*
straight out of the real prompt text `sprint_retro.py`'s own prompt
builder writes, which is what makes the edge-case assertions above
mean something rather than just exercising plumbing with an opaque
fixed reply.

**Verified:** the migration was split into two — `0021` (the
`retro_returned_snapshot` bugfix alone) and `0022` (everything else) —
and both were downgraded and re-applied from a clean state to confirm
each stands on its own; all 17 CI-wired backend proof scripts pass,
including the new `verify_sprint_retro.py` and a re-run of the
pre-existing `verify_sprints_rls.py` as a regression check against the
`complete_sprint` change; `ruff check .` clean; frontend `tsc --noEmit`
and `ESLint` clean on the touched files. Run against the real dev stack
(stub LLM server + a stub-pointed worker process substituted for the
real one, then restored), not just described: every proof-script
assertion above was actually executed and passed, including the
points-planned-vs-done math for a mixed sprint (3 done / 7 planned)
and the regenerate flow's new-timestamp check.

---

## Slice 32 — Phase 4, slice 2: at-risk ticket detection, and a
deliberate departure from the roadmap's original LLM-narrated plan

**Built:** a small, explicit, rule-based scoring pass
(`app/services/at_risk.py`) over every ticket in a project's currently
ACTIVE sprint — unowned-and-not-started, no activity in a while, and
low sprint runway for unfinished work, three signals requiring at least
two to co-occur before a ticket is actually flagged. Computed fresh on
every read, surfaced as a warning-colored badge on ticket cards (board
and sprint views) and an "N at risk" rollup on the sprint header.

**The updated_at investigation, and why the finding didn't kill the
signal.** The roadmap for this slice assumed a "stale, no updates"
check built on `Ticket.updated_at`. Reading the actual trigger SQL
(migrations 0004/0005, `tickets_set_updated_at`) rather than trusting
the column's name: it's an unconditional `BEFORE UPDATE`, no `WHEN`
clause, firing on any write to a tickets row — including the triage
worker setting `priority`/`labels`, the embed worker setting
`embedding`, the appsec worker setting `appsec_review_status`. On its
face that disqualifies `updated_at` as a "human touched this" signal.
But tracing through *when* those writes actually happen changes the
conclusion: `TriageJob`, `EmbedJob`, and `AppSecJob` are all published
synchronously, only from `create_ticket`/`update_ticket`, and only ever
as an immediate reaction to a human action — never independently, never
on a schedule, never a second time later on their own. The gap between
"a human edited this ticket" and "the resulting worker write lands" is
bounded by ordinary job latency — seconds, occasionally longer under
load — never days. At the day-granularity this feature actually
operates at (is a ticket stale by *days*, not by minutes), that gap is
noise. `updated_at` is used here as a real, if slightly imprecise,
"time since any activity, human or its immediate automated follow-on"
signal — not naively trusted as if the trigger were scoped to human
edits, and not discarded either, since discarding it would throw away
real signal over a corruption window that turns out not to matter at
the timescale that matters. Reworking the trigger itself (a `WHEN`
clause excluding AI-pipeline-owned columns) was considered and
rejected: it would change `updated_at`'s semantics for the whole app —
a strictly bigger, riskier change than this feature needs, for a
precision improvement that wouldn't change a single day-granularity
result.

**Why 2-of-3, not any single signal.** Each of the three checks is
individually weak and noisy on its own: being unowned and unstarted is
completely normal on day one of a sprint; low runway is true for
nearly every unfinished ticket in a sprint's final days regardless of
whether anything is actually wrong with any one of them; staleness
alone can just mean a ticket is genuinely fine and hasn't needed
touching. Requiring two independent signals to co-occur is what turns
three individually weak, gameable heuristics into a real signal worth
surfacing on a board — the same reasoning already behind AppSec's flag
being additive across matched categories rather than firing loudly on
the weakest possible match. `verify_at_risk.py` proves this directly,
not just asserts it: a ticket with exactly one fired signal (low
runway alone) is checked to stay under the threshold, both in the pure
unit tests and over real HTTP.

**Zero LLM calls anywhere in this feature's pipeline — the sharpest
reasoning in this slice, and a real departure from what the roadmap
assumed.** The original plan for this slice was an LLM-narrated
version of the risk flag, the same keyword-gate-then-LLM shape AppSec
used. Working through what that LLM step would actually be asked to do
is what killed it. AppSec's LLM call does real work: it reads genuinely
unstructured free text — the ticket's own title and description — and
produces guidance that requires actual language understanding to
synthesize, because the input has no fixed shape and the checklist
items that apply depend on what the ticket specifically says. That's
synthesis a template cannot perform. At-risk detection has no
equivalent free-text step anywhere in its pipeline. Every input to a
risk assessment is already a clean, structured fact this module
computed itself before an LLM would ever be called: `assignee_id`
is/isn't `NULL`, a `workflow_state_id` equality check, an integer day
count. The only thing a "narrate this in plain language" LLM step
could possibly add is turning `unowned_and_not_started=True,
days_since_update=5, days_remaining=2` into a sentence — which a plain
f-string already does, exactly, deterministically, instantly, with
zero risk of a model paraphrasing "2 days" as "about a week" or
dropping a fact under latency pressure. AppSec's cost argument (most
tickets don't need review, an LLM call on all of them would be wasted
spend) applies here too, but it's not the deciding one — the deciding
one is that the entire value proposition an LLM call would need to
justify itself (turning unstructured text into tailored judgment)
simply isn't present anywhere in this feature's inputs. That's a
stronger, structural conclusion than "not worth it for every ticket" —
the reasoning doesn't support an LLM step for *any* flagged ticket, not
just most of them. This is the one place in Phase 3/4 so far where the
original roadmap's plan was reasoned through and explicitly not built,
rather than built and then justified after the fact.

**On-demand, not scheduled — and why a cache would be strictly worse,
not just unneeded.** Every prior AI feature in this app (triage,
embedding, AppSec review, sprint retro) needed async machinery because
each did something genuinely expensive worth caching: a real LLM call,
with real latency and real failure modes, whose result then had to be
stored so it wasn't repeated on every read. Risk scoring has no
expensive step to amortize — a handful of field comparisons plus one
or two already-established-pattern queries (the project's workflow-
state order bounds, the project's one active sprint), on data already
being fetched for the same request regardless. Computing it fresh on
every read (the same pattern `Sprint.total_points` and
`Sprint.points_planned` already established) isn't just simpler than
caching it — a cached/scheduled version would be actively worse. A
periodic rescore (the one option this app has zero existing
infrastructure for — no cron container, no APScheduler, no scheduled
GitHub Actions workflow anywhere in this repo, confirmed by searching,
not assumed absent) introduces a real staleness window of its own: a
cached score going stale between runs, worst exactly when a sprint is
ending fast and ticket state is changing quickly — precisely the
moment an at-risk signal needs to be most current, not most delayed.
On-demand computation doesn't have that failure mode at all, because
there is nothing to go stale. Building scheduling infrastructure here
to solve a staleness problem on-demand computation doesn't have would
have been exactly the kind of "because the pattern exists" over-
engineering this app has avoided everywhere else.

**Extended the proof-script pattern, with no LLM stub or worker needed
at all.** `scripts/verify_at_risk.py` is the first CI-wired proof
script in Phase 3/4 that needs neither the fake LLM stub server nor an
extra worker process — a direct, visible consequence of the zero-LLM
conclusion above. Pure, synchronous checks cover a deliberately
at-risk ticket (all 3 signals, flagged), a healthy one (0 signals, not
flagged), a ticket with exactly 1 fired signal (correctly under
threshold), a ticket in the terminal column (never at risk regardless
of every other factor), and an overdue-but-still-active sprint (a
negative days-remaining still counts, with its own reason string).
Then the same over real HTTP for two independent orgs each running
their own active sprint at the same moment — specifically to exercise
this slice's two genuinely new queries (`find_active_sprint`,
`workflow_state_bounds`) under concurrent multi-tenant load, not just
a single org where scoping bugs would have nowhere to show up.

**Verified:** all 18 CI-wired backend proof scripts pass, including the
new `verify_at_risk.py`, plus a re-run of `verify_tickets_rls.py` and
`verify_sprints_rls.py` as regression checks against the `list_tickets`/
`get_ticket`/sprint-serialization changes; `ruff check .` clean;
frontend `tsc --noEmit`, `ESLint`, and a full `next build` all clean.
No migration in this slice at all — the first AI-adjacent slice with no
schema change of any kind, a direct consequence of computing everything
at read time and persisting nothing.
