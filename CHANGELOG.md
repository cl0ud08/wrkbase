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
