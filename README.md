# Wrkbase

AI-native, security-aware project management (Jira alternative).

## Local dev

```
cp .env.example .env
docker compose up --build
```

- Web: http://localhost:3000
- API: http://localhost:8000/health
- API docs: http://localhost:8000/docs

If a port is already taken by something else on your machine, change the
corresponding `*_PORT` (and `NEXT_PUBLIC_API_URL`/`CORS_ORIGINS` if you move
the API port) in `.env` — nothing else needs to change.

## Structure

- `apps/web` — Next.js (App Router) frontend
- `apps/api` — FastAPI backend
