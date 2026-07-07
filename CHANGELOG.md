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
