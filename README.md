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
```

`verify_rls.py` proves default-deny (no tenant context → zero rows),
strict cross-org isolation, and that a cross-tenant write is rejected —
directly against Postgres. `verify_auth_rls.py` proves the same thing end
to end over real HTTP: two orgs sign up, log in, and each only ever sees
its own data. Both run in CI on every push (`.github/workflows/ci.yml`),
each as its own clearly labeled step — a regression in either is the one
failure in this repo that matters most.

## Current status

**Phase 0 (foundation) is complete:** Docker Compose skeleton, DB schema
with Row-Level Security enforced from the first migration, a full custom
JWT auth flow (signup/login/refresh/logout) on both backend and frontend
with httpOnly cookies and silent token refresh, Redis-backed rate
limiting, and a CI pipeline that guards the RLS/auth proofs on every push.

**Not yet built (Phase 1):** Projects, tickets, sprints, and comments —
the actual project-management functionality. The `/dashboard` page is
currently a placeholder. There's also no async job queue yet (RabbitMQ is
in the stack but unused — nothing needs it until an AI job exists to
queue) and no pytest suite (verification so far is the proof scripts
above, run manually and in CI).

See [CHANGELOG.md](CHANGELOG.md) for the full slice-by-slice build log and
the reasoning behind the architectural decisions along the way.
