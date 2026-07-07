# Wrkbase

Wrkbase is an AI-native, security-aware project management tool — a Jira
alternative built to demonstrate what a modern PM tool looks like when
multi-tenant data isolation is enforced at the database level (Postgres
Row-Level Security) rather than trusted to application code, and when
auth is built from first principles (custom JWT with rotating refresh
tokens) instead of bolted on via a third-party provider. It's a solo
portfolio project, built in vertical slices — schema, RLS policy, API
endpoint, and frontend together for one feature at a time — with the
reasoning behind each decision kept intact in [CHANGELOG.md](CHANGELOG.md).

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (App Router) + TypeScript |
| Backend | FastAPI (Python 3.12), SQLAlchemy 2.0 (async) + Alembic |
| Database | PostgreSQL 16 + pgvector, Row-Level Security |
| Cache / ephemeral state | Redis (refresh tokens, rate limiting) |
| Async job queue | RabbitMQ (planned — not introduced yet, no job needs it) |
| Auth | Custom JWT (15 min access + rotating 7-day refresh tokens), Argon2id password hashing |
| Rate limiting | slowapi, Redis-backed |
| CI | GitHub Actions (parallel backend/frontend jobs) |
| Local dev | Docker Compose |

## Running it locally

**1. Clone and configure environment**

```
git clone <repo-url>
cd wrkbase
cp .env.example .env
```

`.env` is git-ignored and holds dev-only secrets. If port `8000` or `6379`
is already taken by something else on your machine, change the
corresponding `*_PORT` value in `.env` (and `CORS_ORIGINS` if you move the
API's port) — nothing else needs to change, since every service reads its
address from `.env` at startup.

**2. Bring the stack up**

```
docker compose up --build
```

This starts Postgres, Redis, the FastAPI API, and the Next.js frontend.
Database migrations run automatically on API container start (see
`apps/api/entrypoint.sh`) — you don't need to run them by hand, but you
can:

```
docker compose exec api alembic upgrade head
```

- Web: http://localhost:3000
- API: http://localhost:8000/health (or your remapped `API_PORT`)
- API docs (Swagger UI): http://localhost:8000/docs

**3. Seed dev data and verify tenant isolation**

These aren't part of the app — they're throwaway scripts that prove the
most important property in this codebase: that Row-Level Security
actually isolates tenants, and that auth + RLS are wired together
correctly, not just each independently correct.

```
docker compose exec api python -m scripts.seed
docker compose exec api python -m scripts.verify_rls
docker compose exec api python -m scripts.verify_auth_rls
docker compose exec api python -m scripts.verify_projects_rls
docker compose exec api python -m scripts.verify_tickets_rls
docker compose exec api python -m scripts.verify_workflow_rls
docker compose exec api python -m scripts.verify_invites_rls
```

`verify_rls.py` proves default-deny (no tenant context → zero rows),
strict cross-org isolation, and that a cross-tenant write is rejected —
directly against Postgres. The rest each prove the same property end to
end over real HTTP for one resource: two orgs exercise it in parallel,
and each only ever sees/reaches its own data — `verify_auth_rls.py` for
signup/login/refresh, `verify_projects_rls.py` for projects,
`verify_tickets_rls.py` for tickets (plus the epic/story/task/subtask
hierarchy rule), `verify_workflow_rls.py` for Kanban board columns and
ticket moves, and `verify_invites_rls.py` for team invites and member
management (including the last-admin-removal guard). All six run in CI
on every push (`.github/workflows/ci.yml`), each as its own clearly
labeled step — a regression in any of them is the failure class that
matters most in this repo.

## Current status

**Phase 0 (foundation) is complete:** Docker Compose skeleton, DB schema
with Row-Level Security enforced from the first migration, a full custom
JWT auth flow (signup/login/refresh/logout) on both backend and frontend
with httpOnly cookies and silent token refresh, Redis-backed rate
limiting, and a CI pipeline that guards the RLS/auth proofs on every push.

**Phase 1 (in progress):** Projects CRUD, ticket CRUD with an
epic/story/task/subtask hierarchy, a Kanban board with configurable
per-project workflow states (drag-and-drop via dnd-kit), and team
invites + member management (an admin can invite someone into an
existing org by email/role, manage roles, and remove members, with a
guard against ever leaving an org with zero admins) are all built and
proof-scripted. Not yet built: sprints, comments, and anything against
pgvector (no embeddings/search exist yet — the extension is running but
unused). There's also no async job queue yet (RabbitMQ is in the stack
but unused — nothing needs it until an AI job exists to queue), no
invite email delivery (invite links are generated but shared manually,
by design for now), and no pytest suite (verification so far is the
proof scripts above, run manually and in CI).

See [CHANGELOG.md](CHANGELOG.md) for the full slice-by-slice build log and
the reasoning behind the architectural decisions along the way.
