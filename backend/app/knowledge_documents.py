from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, load_only

from .deps import get_session
from .knowledge.chunking import CHUNK_OVERLAP, CHUNK_SIZE, VERSION, split_document, text_hash
from .models import KnowledgeChunk, KnowledgeDocument, Project, utc_now
from .projects import require_project
from .schemas import (KnowledgeChunkOutput, KnowledgeCreateInput, KnowledgeDocumentOutput,
                      KnowledgeDocumentSummary, KnowledgeUpdateInput)

router = APIRouter(prefix="/api/projects/{project_id}/knowledge-documents", tags=["项目知识库"])


def require_document(session, project_id, document_id):
    require_project(session, project_id)
    document = session.get(KnowledgeDocument, str(document_id))
    if not document or document.project_id != project_id:
        raise HTTPException(status_code=404, detail="该项目下的知识文档不存在或已删除。")
    return document


def document_output(session, document):
    chunks = session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                             .order_by(KnowledgeChunk.position)).all()
    return KnowledgeDocumentOutput(
        **KnowledgeDocumentSummary.model_validate(document).model_dump(), content=document.content,
        content_hash=document.content_hash,
        chunks=[KnowledgeChunkOutput.model_validate(chunk) for chunk in chunks],
    )


def lock_document(session, project_id, document_id, revision):
    require_document(session, project_id, document_id)
    session.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == str(document_id),
                    KnowledgeDocument.project_id == project_id).values(updated_at=KnowledgeDocument.updated_at)
                    .execution_options(synchronize_session=False))
    session.expire_all()
    document = require_document(session, project_id, document_id)
    if document.revision != revision:
        raise HTTPException(status_code=409, detail="文档已被其他页面修改，请重新打开最新文档后再操作；本次未覆盖或删除。")
    return document


@router.get("", response_model=list[KnowledgeDocumentSummary])
def list_documents(project_id: int, session: Session = Depends(get_session)):
    require_project(session, project_id)
    return session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.project_id == project_id)
                           .options(load_only(KnowledgeDocument.id, KnowledgeDocument.project_id, KnowledgeDocument.title,
                                KnowledgeDocument.source_filename, KnowledgeDocument.revision, KnowledgeDocument.char_count,
                                KnowledgeDocument.chunk_count, KnowledgeDocument.chunker_version, KnowledgeDocument.chunk_size,
                                KnowledgeDocument.chunk_overlap, KnowledgeDocument.created_at, KnowledgeDocument.updated_at))
                           .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc())).all()


@router.post("", response_model=KnowledgeDocumentOutput, status_code=201)
def create_document(project_id: int, payload: KnowledgeCreateInput, response: Response,
                    session: Session = Depends(get_session)):
    require_project(session, project_id)
    locked = session.execute(update(Project).where(Project.id == project_id).values(updated_at=Project.updated_at)
                             .execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除。")
    session.expire_all()
    existing = session.get(KnowledgeDocument, str(payload.document_id))
    fields = payload.model_dump(exclude={"document_id"})
    if existing:
        if existing.project_id != project_id or any(getattr(existing, key) != value for key, value in fields.items()):
            raise HTTPException(status_code=409, detail="文档请求标识已被使用，请核实列表后重新录入。")
        response.status_code = 200
        return document_output(session, existing)
    chunks = split_document(str(payload.document_id), 1, payload.content)
    document = KnowledgeDocument(id=str(payload.document_id), project_id=project_id, **fields,
                revision=1, content_hash=text_hash(payload.content), char_count=len(payload.content), chunk_count=len(chunks),
                chunker_version=VERSION, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    session.add(document)
    session.flush()
    session.add_all([KnowledgeChunk(**chunk) for chunk in chunks])
    require_project(session, project_id).updated_at = utc_now()
    session.commit()
    return document_output(session, document)


@router.get("/{document_id}", response_model=KnowledgeDocumentOutput)
def get_document(project_id: int, document_id: UUID, session: Session = Depends(get_session)):
    return document_output(session, require_document(session, project_id, document_id))


@router.put("/{document_id}", response_model=KnowledgeDocumentOutput)
def update_document(project_id: int, document_id: UUID, payload: KnowledgeUpdateInput,
                    session: Session = Depends(get_session)):
    document = lock_document(session, project_id, document_id, payload.expected_revision)
    fields = payload.model_dump(exclude={"expected_revision"})
    if all(getattr(document, key) == value for key, value in fields.items()) and document.chunker_version == VERSION:
        return document_output(session, document)
    revision = document.revision + 1
    chunks = split_document(document.id, revision, payload.content)
    for key, value in fields.items():
        setattr(document, key, value)
    document.revision, document.updated_at = revision, utc_now()
    document.content_hash, document.char_count, document.chunk_count = text_hash(payload.content), len(payload.content), len(chunks)
    document.chunker_version, document.chunk_size, document.chunk_overlap = VERSION, CHUNK_SIZE, CHUNK_OVERLAP
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    session.add_all([KnowledgeChunk(**chunk) for chunk in chunks])
    require_project(session, project_id).updated_at = utc_now()
    session.commit()
    return document_output(session, document)


@router.delete("/{document_id}", status_code=204)
def delete_document(project_id: int, document_id: UUID, expected_revision: int = Query(ge=1),
                    session: Session = Depends(get_session)):
    document = lock_document(session, project_id, document_id, expected_revision)
    session.delete(document)
    require_project(session, project_id).updated_at = utc_now()
    session.commit()
    return Response(status_code=204)
