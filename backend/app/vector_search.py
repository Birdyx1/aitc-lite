"""项目向量索引：持久任务、分批向量化、影子 collection 完成后才发布。"""
import asyncio
import hashlib
import json
from dataclasses import asdict
from time import perf_counter
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .deps import get_session
from .embedding_settings import load_embedding_config
from .embeddings import embed_texts
from .knowledge.bm25 import corpus_signature, current_signature, read_chunks
from .models import VectorBuild, VectorIndex, utc_now
from .projects import require_project
from .schemas import KnowledgeSearchHit, KnowledgeSearchInput

router = APIRouter(prefix="/api/projects/{project_id}/vector-index", tags=["知识向量检索"])
BATCH_SIZE = 16
ACTIVE = ("queued", "running")


def profile(config):
    # Key 与超时变化不改变向量空间；它们不进入索引身份，也不返回给前端。
    values = ["embedding-v1", config.base_url, config.model, config.mock_mode, config.expected_dimensions]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def collection_name(task_id):
    return f"aitc-v13-{task_id}"


class BuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class BuildOutput(BaseModel):
    id: str
    status: str
    completed: int
    total: int
    error: str


class IndexOutput(BaseModel):
    status: str
    message: str
    chunk_count: int
    indexed_chunk_count: int
    dimensions: int | None
    model: str
    mock_mode: bool
    updated_at: str | None
    task: BuildOutput | None


class SearchOutput(BaseModel):
    query: str
    model: str
    mock_mode: bool
    dimensions: int
    elapsed_ms: int
    message: str
    hits: list[KnowledgeSearchHit]


def build_output(task):
    return BuildOutput(id=task.id, status=task.status, completed=task.completed, total=task.total, error=task.error)


@router.get("", response_model=IndexOutput)
def index_status(project_id: int, session: Session = Depends(get_session)):
    # SQLite 驱动不会为 SELECT 自动 BEGIN；保持索引和任务读取同一快照，
    # 避免后台发布恰好发生在两次查询之间时返回旧维度配新成功状态。
    session.execute(text("BEGIN"))
    try:
        require_project(session, project_id)
        chunks = read_chunks(session, project_id)
        index = session.get(VectorIndex, project_id)
        try:
            config = load_embedding_config(session)
        except HTTPException:
            config = None
        task = session.scalars(select(VectorBuild).where(VectorBuild.project_id == project_id)
                               .order_by(VectorBuild.created_at.desc(), VectorBuild.id.desc())).first()
        status, message = "missing", "尚未构建向量索引。"
        if not config:
            status, message = "unconfigured", "请先保存 Embedding 配置。"
        elif not chunks:
            status, message = "empty", "请先添加知识文档。"
        elif index:
            if index.corpus_signature != corpus_signature(chunks) or index.profile != profile(config):
                status, message = "stale", "知识文档或向量空间配置已变化，请重建索引。"
            else:
                status, message = "ready", "索引可用；语义检索仅使用当前项目。"
        result = IndexOutput(status=status, message=message, chunk_count=len(chunks),
                           indexed_chunk_count=index.chunk_count if index else 0,
                           dimensions=index.dimensions if index else None, model=index.model if index else "",
                           mock_mode=index.mock_mode if index else bool(config and config.mock_mode),
                           updated_at=index.updated_at.isoformat() if index else None,
                           task=build_output(task) if task else None)
        return result
    finally:
        # 返回或抛出异常前释放快照，不等待异步响应的依赖清理。
        session.rollback()


@router.post("/builds", response_model=BuildOutput, status_code=202)
def submit_build(project_id: int, payload: BuildInput, session: Session = Depends(get_session)):
    session.execute(text("BEGIN IMMEDIATE"))
    require_project(session, project_id)
    existing = session.get(VectorBuild, str(payload.request_id))
    if existing:
        if existing.project_id != project_id:
            raise HTTPException(409, "请求标识已被使用，请重新发起。")
        return build_output(existing)
    active = session.scalar(select(VectorBuild.id).where(VectorBuild.project_id == project_id,
                                                        VectorBuild.status.in_(ACTIVE)))
    if active:
        raise HTTPException(409, "当前项目已有索引构建任务，请等待完成。")
    config = load_embedding_config(session)
    chunks = read_chunks(session, project_id)
    if not chunks:
        raise HTTPException(400, "项目没有有效分块，请先保存知识文档。")
    task = VectorBuild(id=str(payload.request_id), project_id=project_id, total=len(chunks),
                       corpus_signature=corpus_signature(chunks), profile=profile(config))
    session.add(task)
    session.commit()
    return build_output(task)


def settle_vector_builds(app):
    with app.state.session_factory() as session:
        session.execute(update(VectorBuild).where(VectorBuild.status.in_(ACTIVE)).values(
            status="interrupted", error="服务重启或停止，任务已中断；请手动重新构建，可能重复产生 Embedding 费用。"))
        session.commit()


def check_snapshot(session, task, expected_profile):
    require_project(session, task.project_id)
    if current_signature(session, task.project_id) != task.corpus_signature:
        raise HTTPException(409, "构建期间知识文档发生变化，未发布新索引；请重新构建。")
    if profile(load_embedding_config(session)) != expected_profile:
        raise HTTPException(409, "构建期间 Embedding 配置发生变化，未发布新索引；请重新构建。")


async def run_build(app, task_id):
    factory = app.state.session_factory
    with factory() as session:
        task = session.get(VectorBuild, task_id)
        if not task or task.status != "queued":
            return
        config = load_embedding_config(session)
        check_snapshot(session, task, task.profile)
        chunks = read_chunks(session, task.project_id)
        task.status = "running"
        session.commit()
        project_id, expected_profile = task.project_id, task.profile
    dimensions = None
    for start in range(0, len(chunks), BATCH_SIZE):
        with factory() as session:
            task = session.get(VectorBuild, task_id)
            if not task:
                return  # 项目删除，停止后续付费调用。
            check_snapshot(session, task, expected_profile)
        batch = chunks[start:start + BATCH_SIZE]
        vectors = await embed_texts(config, [chunk.content for chunk in batch], app.state.embedding_transport)
        if dimensions is not None and dimensions != len(vectors[0]):
            raise HTTPException(502, "Embedding 不同批次的维度不一致，未发布新索引。")
        dimensions = len(vectors[0])
        await asyncio.to_thread(app.state.vector_store.add, collection_name(task_id),
                                [chunk.chunk_id for chunk in batch], vectors, start == 0)
        with factory() as session:
            task = session.get(VectorBuild, task_id)
            if not task:
                return
            check_snapshot(session, task, expected_profile)
            task.completed = start + len(batch)
            session.commit()
    # SQLite 写锁内再次核对，保证文档变更与发布不会交叉。
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        task = session.get(VectorBuild, task_id)
        if not task:
            return
        check_snapshot(session, task, expected_profile)
        index = session.get(VectorIndex, project_id)
        if not index:
            index = VectorIndex(project_id=project_id)
            session.add(index)
        index.collection = collection_name(task_id)
        index.corpus_signature = task.corpus_signature
        index.profile = expected_profile
        index.dimensions = dimensions
        index.chunk_count = len(chunks)
        index.model = config.model
        index.mock_mode = config.mock_mode
        index.updated_at = utc_now()
        task.status = "succeeded"
        session.commit()


async def cleanup_collections(app):
    with app.state.session_factory() as session:
        keep = set(session.scalars(select(VectorIndex.collection)))
        keep.update(collection_name(id) for id in session.scalars(select(VectorBuild.id).where(VectorBuild.status.in_(ACTIVE))))
    await asyncio.to_thread(app.state.vector_store.cleanup, keep)


async def vector_worker(app):
    ticks = 0
    while True:
        with app.state.session_factory() as session:
            task_id = session.scalar(select(VectorBuild.id).where(VectorBuild.status == "queued")
                                     .order_by(VectorBuild.created_at, VectorBuild.id).limit(1))
        if task_id:
            try:
                await run_build(app, task_id)
            except Exception as exc:
                # 仅暴露本地已脱敏的 HTTP 错误，不序列化第三方异常或原始响应。
                message = exc.detail if isinstance(exc, HTTPException) else "向量索引构建失败，请检查本地存储和依赖后重新构建。"
                with app.state.session_factory() as session:
                    task = session.get(VectorBuild, task_id)
                    if task:
                        task.status, task.error = "failed", message
                        session.commit()
        if task_id or ticks % 40 == 0:
            try:
                await cleanup_collections(app)
            except Exception:
                pass  # 清理失败不破坏已发布索引；下次空闲或重启继续清理。
        ticks += 1
        await asyncio.sleep(0.25)


@router.post("/search", response_model=SearchOutput)
async def search_vectors(project_id: int, payload: KnowledgeSearchInput, request: Request,
                         session: Session = Depends(get_session)):
    start = perf_counter()
    require_project(session, project_id)
    config = load_embedding_config(session)
    index = session.get(VectorIndex, project_id)
    signature = current_signature(session, project_id)
    if not index:
        raise HTTPException(409, "请先构建当前项目的向量索引。")
    if index.profile != profile(config) or index.corpus_signature != signature:
        raise HTTPException(409, "知识文档或 Embedding 配置已变化，请先重建向量索引。")
    collection, dimensions, count = index.collection, index.dimensions, index.chunk_count
    chunks = {chunk.chunk_id: chunk for chunk in read_chunks(session, project_id)}
    session.rollback()
    vectors = await embed_texts(config, [payload.query], request.app.state.embedding_transport)
    if len(vectors[0]) != dimensions:
        raise HTTPException(409, "查询向量维度与索引不一致，请核对模型并重建索引。")
    try:
        matches = await asyncio.to_thread(request.app.state.vector_store.query, collection,
                                          vectors[0], min(payload.top_k, count))
    except Exception:
        raise HTTPException(503, "本地向量索引不可用，请重新构建索引后重试。") from None
    session.rollback()
    require_project(session, project_id)
    fresh = session.get(VectorIndex, project_id)
    if (not fresh or fresh.collection != collection or current_signature(session, project_id) != signature
            or profile(load_embedding_config(session)) != profile(config)):
        raise HTTPException(409, "检索期间知识库或索引配置发生变化，请重新检索。")
    if any(id not in chunks for id, _ in matches):
        raise HTTPException(503, "向量索引与知识分块不一致，请重建索引。")
    return SearchOutput(query=payload.query, model=config.model, mock_mode=config.mock_mode,
                        dimensions=dimensions, elapsed_ms=round((perf_counter()-start)*1000),
                        message=("Mock 哈希向量仅供流程演示，不具备语义检索能力。" if config.mock_mode else
                                 "按余弦相似度返回最近分块；相似度不是置信度，无匹配知识时也可能返回结果。"),
                        hits=[KnowledgeSearchHit(rank=rank, score=max(-1.0, min(1.0, 1-distance)),
                                                 matched_tokens=[], **asdict(chunks[id]))
                              for rank, (id, distance) in enumerate(matches, 1)])
