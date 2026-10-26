from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from .deps import get_session
from .models import Requirement, utc_now
from .projects import require_project
from .schemas import RequirementInput, RequirementOutput, RequirementSummary

router = APIRouter(prefix="/api/projects/{project_id}/requirements", tags=["需求"])


def require_requirement(session: Session, project_id: int, requirement_id: int) -> Requirement:
    require_project(session, project_id)
    requirement = session.scalar(select(Requirement).where(
        Requirement.id == requirement_id, Requirement.project_id == project_id,
    ))
    if requirement is None:
        raise HTTPException(status_code=404, detail="该项目下的需求不存在或已被删除")
    return requirement


@router.get("", response_model=list[RequirementSummary])
def list_requirements(project_id: int, session: Session = Depends(get_session)):
    require_project(session, project_id)
    return session.scalars(
        select(Requirement).where(Requirement.project_id == project_id)
        .options(load_only(Requirement.id, Requirement.project_id, Requirement.title,
                           Requirement.created_at, Requirement.updated_at))
        .order_by(Requirement.updated_at.desc(), Requirement.id.desc())
    ).all()


@router.post("", response_model=RequirementOutput, status_code=201)
def create_requirement(project_id: int, payload: RequirementInput, session: Session = Depends(get_session)):
    project = require_project(session, project_id)
    requirement = Requirement(project_id=project_id, **payload.model_dump())
    session.add(requirement)
    project.updated_at = utc_now()
    session.commit()
    session.refresh(requirement)
    return requirement


@router.get("/{requirement_id}", response_model=RequirementOutput)
def get_requirement(project_id: int, requirement_id: int, session: Session = Depends(get_session)):
    return require_requirement(session, project_id, requirement_id)


@router.put("/{requirement_id}", response_model=RequirementOutput)
def update_requirement(project_id: int, requirement_id: int, payload: RequirementInput,
                       session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    requirement.title = payload.title
    requirement.content = payload.content
    require_project(session, project_id).updated_at = utc_now()
    session.commit()
    session.refresh(requirement)
    return requirement


@router.delete("/{requirement_id}", status_code=204)
def delete_requirement(project_id: int, requirement_id: int, session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    session.delete(requirement)
    require_project(session, project_id).updated_at = utc_now()
    session.commit()
    return Response(status_code=204)
