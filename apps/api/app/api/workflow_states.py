import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_current_auth
from app.db.models import Project, UserRole, WorkflowState
from app.db.session import get_db
from app.schemas.workflow_state import WorkflowStateCreate, WorkflowStateRead, WorkflowStateUpdate

router = APIRouter(prefix="/projects/{project_id}/workflow-states", tags=["workflow-states"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow state not found")


async def _get_project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _get_state_or_404(db: AsyncSession, project_id: uuid.UUID, state_id: uuid.UUID) -> WorkflowState:
    result = await db.execute(
        select(WorkflowState).where(WorkflowState.id == state_id, WorkflowState.project_id == project_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        raise _NOT_FOUND
    return state


def _require_admin(auth: AuthContext) -> None:
    # Stricter than Projects/Tickets on purpose: a workflow state isn't
    # "owned" by whoever happens to create it the way a project or ticket
    # is — it's shared board configuration that changes what every column
    # means for the whole team. Reconfiguring it is closer to an admin
    # action than a personal-resource edit, so there's no creator carve-out
    # here at all. Listing stays open to any org member — they need to see
    # the columns to render the board.
    if auth.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an org admin can modify workflow states",
        )


async def _clear_existing_default(db: AsyncSession, project_id: uuid.UUID) -> None:
    await db.execute(
        update(WorkflowState)
        .where(WorkflowState.project_id == project_id, WorkflowState.is_default.is_(True))
        .values(is_default=False)
    )


@router.post("", response_model=WorkflowStateRead, status_code=status.HTTP_201_CREATED)
async def create_workflow_state(
    project_id: uuid.UUID,
    payload: WorkflowStateCreate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> WorkflowState:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)

    if payload.is_default:
        await _clear_existing_default(db, project_id)

    state = WorkflowState(
        org_id=auth.org_id,
        project_id=project_id,
        name=payload.name,
        order=payload.order,
        is_default=payload.is_default,
    )
    db.add(state)
    await db.commit()
    return state


@router.get("", response_model=list[WorkflowStateRead])
async def list_workflow_states(
    project_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> list[WorkflowState]:
    await _get_project_or_404(db, project_id)
    result = await db.execute(
        select(WorkflowState).where(WorkflowState.project_id == project_id).order_by(WorkflowState.order)
    )
    return list(result.scalars().all())


@router.patch("/{state_id}", response_model=WorkflowStateRead)
async def update_workflow_state(
    project_id: uuid.UUID,
    state_id: uuid.UUID,
    payload: WorkflowStateUpdate,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> WorkflowState:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    state = await _get_state_or_404(db, project_id, state_id)

    update_data = payload.model_dump(exclude_unset=True)
    if update_data.get("is_default") is True:
        await _clear_existing_default(db, project_id)

    for field, value in update_data.items():
        setattr(state, field, value)

    await db.commit()
    return state


@router.delete("/{state_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow_state(
    project_id: uuid.UUID,
    state_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_project_or_404(db, project_id)
    _require_admin(auth)
    state = await _get_state_or_404(db, project_id, state_id)
    try:
        await db.delete(state)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a workflow state that still has tickets in it",
        )
