from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project
from .deps import get_session
from .schemas import ProjectInput, ProjectOutput

router = APIRouter(prefix="/api/projects", tags=["项目"])


def require_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")
    return project


@router.get("", response_model=list[ProjectOutput])
def list_projects(session: Session = Depends(get_session)):
    return session.scalars(select(Project).order_by(Project.updated_at.desc(), Project.id.desc())).all()


@router.post("", response_model=ProjectOutput, status_code=201)
def create_project(payload: ProjectInput, session: Session = Depends(get_session)):
    project = Project(**payload.model_dump())
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectOutput)
def get_project(project_id: int, session: Session = Depends(get_session)):
    return require_project(session, project_id)


@router.put("/{project_id}", response_model=ProjectOutput)
def update_project(project_id: int, payload: ProjectInput, session: Session = Depends(get_session)):
    project = require_project(session, project_id)
    project.name = payload.name
    project.description = payload.description
    session.commit()
    session.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int, session: Session = Depends(get_session)):
    project = require_project(session, project_id)
    session.delete(project)
    session.commit()
    return Response(status_code=204)

