import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import Project, UserRole
from app.db.session import get_db
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


async def _get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    # RLS already scopes this to the caller's org, so a project ID from
    # another org simply matches zero rows — "doesn't exist" and "exists in
    # another org" are indistinguishable here, on purpose. This endpoint
    # never confirms or denies that some other org's project ID is real.
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise _NOT_FOUND
    return project


def _require_owner_or_admin(project: Project, auth: AuthContext) -> None:
    # Rough first pass, per the brief: creator or an org admin. Real
    # per-project roles/sharing is a future slice — this only stops a
    # random org member from editing someone else's project today.
    if project.created_by != auth.user_id and auth.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the project's creator or an org admin can modify it",
        )


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Project:
    project = Project(
        org_id=auth.org_id,
        name=payload.name,
        description=payload.description,
        created_by=auth.user_id,
    )
    db.add(project)
    await db.commit()
    return project


@router.get("", response_model=list[ProjectRead])
async def list_projects(
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[Project]:
    # No explicit org_id filter — RLS already scopes every row to
    # auth.org_id. This is the actual payoff of Phase 0: a brand-new
    # resource gets tenant isolation for free just by going through
    # get_current_auth, with zero tenancy-aware code in this query.
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Project:
    return await _get_project_or_404(db, project_id)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> Project:
    project = await _get_project_or_404(db, project_id)
    _require_owner_or_admin(project, auth)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)

    await db.commit()
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _get_project_or_404(db, project_id)
    _require_owner_or_admin(project, auth)
    await db.delete(project)
    await db.commit()
