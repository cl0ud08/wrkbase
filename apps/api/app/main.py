from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.invites import router as invites_router
from app.api.org import router as org_router
from app.api.projects import router as projects_router
from app.api.sprints import router as sprints_router
from app.api.tickets import router as tickets_router
from app.api.workflow_states import router as workflow_states_router
from app.api.ws import router as ws_router
from app.core.config import settings
from app.core.rate_limit import limiter

app = FastAPI(title=settings.project_name)

app.state.limiter = limiter


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    response = JSONResponse(
        status_code=429,
        content={"detail": f"Too many requests ({exc.detail}). Please slow down and try again shortly."},
    )
    return limiter._inject_headers(response, request.state.view_rate_limit)


app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Added last so it's the outermost layer: a 429 raised deep inside (by
# SlowAPIMiddleware) still passes back through this on its way out and gets
# CORS headers, instead of a browser seeing an opaque CORS failure in place
# of the actual 429.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(tickets_router)
app.include_router(sprints_router)
app.include_router(workflow_states_router)
app.include_router(invites_router)
app.include_router(org_router)
app.include_router(ws_router)
