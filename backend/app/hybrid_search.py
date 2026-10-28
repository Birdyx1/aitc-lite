"""同一项目分块快照的 BM25 + 向量双路召回与可追溯 RRF 融合。"""
import asyncio
import math
from dataclasses import asdict
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from .deps import get_session
from .embedding_settings import load_embedding_config
from .embeddings import embed_texts
from .knowledge.bm25 import corpus_signature, current_signature, read_chunks, rank_chunks, tokenize
from .knowledge.rrf import ALGORITHM, RRF_K, fuse_rankings
from .models import VectorIndex
from .projects import require_project
from .schemas import KnowledgeSearchInput, KnowledgeSearchHit
from .vector_search import profile
from .rerank_settings import load_rerank_config
from .reranking import identity as rerank_identity, rerank_texts

router = APIRouter(prefix="/api/projects/{project_id}/hybrid-search", tags=["知识混合检索"])


class HybridInput(KnowledgeSearchInput):
    candidate_k: int = Field(default=20, ge=1, le=50, strict=True)

    use_rerank: bool = Field(default=False, strict=True)
    rerank_k: int = Field(default=20, ge=1, le=50, strict=True)

    @model_validator(mode="after")
    def enough_candidates(self):
        if self.candidate_k < self.top_k:
            raise ValueError("召回条数不能小于最终返回条数")
        if self.use_rerank and self.rerank_k < self.top_k:
            raise ValueError("精排候选数不能小于最终返回条数")
        return self


class RouteOutput(BaseModel):
    status: Literal['ok', 'empty', 'no_terms', 'error', 'unconfigured', 'missing', 'stale']
    message: str
    candidate_count: int = 0


class RerankOutput(BaseModel):
    status: Literal["disabled", "unconfigured", "empty", "applied", "fallback"]
    message: str
    model: str = ""
    mock_mode: bool = False
    candidate_count: int = 0
    elapsed_ms: int = 0


class HybridHit(KnowledgeSearchHit):
    rrf_rank: int
    rerank_score: float | None = None
    bm25_rank: int | None
    bm25_score: float | None
    bm25_contribution: float
    vector_rank: int | None
    vector_score: float | None
    vector_contribution: float


class HybridOutput(BaseModel):
    query: str
    top_k: int
    candidate_k: int
    rrf_k: int = RRF_K
    algorithm: str = ALGORITHM
    embedding_model: str = ""
    corpus_signature: str
    corpus_chunk_count: int
    unique_candidate_count: int
    overlap_count: int
    query_tokens: list[str]
    degraded: bool
    mock_mode: bool
    mode: Literal['fused', 'bm25_only', 'vector_only', 'no_results', 'empty']
    message: str
    elapsed_ms: int
    routes: dict[str, RouteOutput]
    rerank: RerankOutput
    hits: list[HybridHit]


def config_or_none(session):
    try:
        return load_embedding_config(session)
    except HTTPException as error:
        if error.status_code != 400:
            raise
        return None


def index_identity(index):
    return index.collection if index else None


def bm25_recall(chunks, query, count):
    try:
        terms = list(dict.fromkeys(tokenize(query)))
        if len(terms) > 64:
            return RouteOutput(status='error', message='BM25 查询词超过 64 个，请缩短查询。'), [], [], {}
        if not terms:
            return RouteOutput(status='no_terms', message='未提取到 BM25 有效词项。'), [], terms, {}
        matches, _ = rank_chunks(chunks, terms)
        matches = matches[:count]
        return (RouteOutput(status='ok' if matches else 'empty', message='BM25 召回完成。' if matches else 'BM25 无匹配词项。', candidate_count=len(matches)),
                [chunk.chunk_id for chunk, _, _ in matches], terms,
                {chunk.chunk_id: (score, matched) for chunk, score, matched in matches})
    except Exception:
        return RouteOutput(status='error', message='BM25 分词或评分失败，请稍后重试。'), [], [], {}


async def vector_recall(app, config, index_data, chunks, query, count, unavailable):
    if unavailable:
        return unavailable, [], {}
    collection, dimensions = index_data
    try:
        vectors = await embed_texts(config, [query], app.state.embedding_transport)
        if len(vectors[0]) != dimensions:
            return RouteOutput(status='error', message='查询向量维度与索引不一致，请核对配置并重建。'), [], {}
        matches = await asyncio.to_thread(app.state.vector_store.query, collection, vectors[0], min(count, len(chunks)))
        seen = set()
        for id, distance in matches:
            if id not in chunks or id in seen or not math.isfinite(distance):
                raise ValueError('invalid vector candidates')
            seen.add(id)
        return (RouteOutput(status='ok' if matches else 'empty', message='向量召回完成。' if matches else '向量召回无结果。', candidate_count=len(matches)),
                [id for id, _ in matches], {id: max(-1.0, min(1.0, 1-distance)) for id, distance in matches})
    except Exception:
        # 第三方异常、HTTP 内容与凭据不进入响应；单路故障由成功路继续提供结果。
        return RouteOutput(status='error', message='向量召回失败，请检查 Embedding 服务、额度或本地索引；未自动重试。'), [], {}


@router.post('', response_model=HybridOutput)
async def hybrid_search(project_id: int, payload: HybridInput, request: Request,
                        session: Session = Depends(get_session)):
    return await retrieve_hybrid(project_id, payload, request.app, session)


async def retrieve_hybrid(project_id: int, payload: HybridInput, app, session):
    started = perf_counter()
    require_project(session, project_id)
    chunks = read_chunks(session, project_id)
    signature = corpus_signature(chunks)
    config = config_or_none(session)
    expected_profile = profile(config) if config else None
    index = session.get(VectorIndex, project_id)
    expected_index = index_identity(index)
    rerank_config = load_rerank_config(session) if payload.use_rerank else None
    unavailable = None
    if not config:
        unavailable = RouteOutput(status='unconfigured', message='未配置 Embedding，本次仅使用 BM25。')
    elif not index:
        unavailable = RouteOutput(status='missing', message='尚未构建向量索引，本次仅使用 BM25。')
    elif index.profile != expected_profile or index.corpus_signature != signature:
        unavailable = RouteOutput(status='stale', message='向量索引已过期，请重建；本次仅使用当前分块的 BM25。')
    index_data = (index.collection, index.dimensions) if index else None
    session.rollback()  # 网络调用期间不持有读事务。
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    if chunks:
        bm25, vector = await asyncio.gather(
            asyncio.to_thread(bm25_recall, chunks, payload.query, payload.candidate_k),
            vector_recall(app, config, index_data, by_id, payload.query, payload.candidate_k, unavailable),
        )
        bm_route, bm_ids, terms, bm_details = bm25
        vec_route, vec_ids, vec_details = vector
    else:
        bm_route = RouteOutput(status='empty', message='当前项目没有有效知识分块。')
        vec_route = RouteOutput(status='empty', message='当前项目没有有效知识分块，未调用 Embedding。')
        bm_ids, vec_ids, terms, bm_details, vec_details = [], [], [], {}, {}
    def validate_snapshot():
        session.rollback()
        require_project(session, project_id)
        fresh_config = config_or_none(session)
        fresh_index = session.get(VectorIndex, project_id)
        if (current_signature(session, project_id) != signature
                or (profile(fresh_config) if fresh_config else None) != expected_profile
                or index_identity(fresh_index) != expected_index
                or (payload.use_rerank and rerank_identity(load_rerank_config(session)) != rerank_identity(rerank_config))):
            raise HTTPException(409, '检索期间知识文档、配置或向量索引发生变化，请重新检索。')
    validate_snapshot()
    success = {'ok', 'empty', 'no_terms'}
    degraded = bm_route.status not in success or vec_route.status not in success
    if bm_route.status not in success and vec_route.status not in success:
        raise HTTPException(503, 'BM25 与向量召回均不可用，请检查分词依赖、Embedding 配置和索引后重试。')
    fused = fuse_rankings({'bm25': bm_ids, 'vector': vec_ids})
    mode = ('empty' if not chunks else 'fused' if bm_ids and vec_ids else 'bm25_only' if bm_ids
            else 'vector_only' if vec_ids else 'no_results')
    message = ('已降级检索，请查看两路状态与原因。' if degraded else
               '当前项目没有有效知识分块。' if not chunks else
               '已按 RRF 融合并按分块 ID 去重；融合分数不是置信度。')
    rerank_info = RerankOutput(status='disabled', message='未启用 Rerank，使用 RRF 排名。')
    selected = fused[:payload.top_k]
    rerank_scores = {}
    rrf_ranks = {id: rank for rank, (id, _, _) in enumerate(fused, 1)}
    if payload.use_rerank:
        if not fused:
            rerank_info = RerankOutput(status='empty', message='没有召回候选，未调用 Rerank。')
        elif rerank_config is None:
            rerank_info = RerankOutput(status='unconfigured', message='未配置 Rerank，已保留 RRF 排名。')
        else:
            candidates = fused[:payload.rerank_k]
            rerank_start = perf_counter()
            session.rollback()
            try:
                results = await rerank_texts(rerank_config, payload.query,
                                             [by_id[id].content for id, _, _ in candidates], app.state.rerank_transport)
                selected = [candidates[index] for index, _ in results[:payload.top_k]]
                rerank_scores = {candidates[index][0]: score for index, score in results}
                rerank_info = RerankOutput(status='applied', message='已按 Rerank 分数排序；RRF 原始排名与分数保留。')
            except Exception as error:
                selected = fused[:payload.top_k]
                rerank_scores = {}
                rerank_info = RerankOutput(status='fallback', message=(error.detail if isinstance(error, HTTPException)
                                                                     else 'Rerank 精排失败。') + ' 已保留 RRF 排名，未自动重试。')
            rerank_info.model = rerank_config.model
            rerank_info.mock_mode = rerank_config.mock_mode
            rerank_info.candidate_count = len(candidates)
            rerank_info.elapsed_ms = round((perf_counter()-rerank_start)*1000)
        validate_snapshot()
    if rerank_info.status in {'unconfigured', 'fallback'}:
        degraded = True
        message = '精排已降级，请查看召回与精排状态。'
    hits = []
    for rank, (id, score, ranks) in enumerate(selected, 1):
        bm_rank, vec_rank = ranks.get('bm25'), ranks.get('vector')
        raw_bm, matched = bm_details.get(id, (None, []))
        hits.append(HybridHit(rank=rank, score=score, rrf_rank=rrf_ranks[id], rerank_score=rerank_scores.get(id), matched_tokens=matched, **asdict(by_id[id]),
                              bm25_rank=bm_rank, bm25_score=raw_bm,
                              bm25_contribution=1/(RRF_K+bm_rank) if bm_rank else 0,
                              vector_rank=vec_rank, vector_score=vec_details.get(id),
                              vector_contribution=1/(RRF_K+vec_rank) if vec_rank else 0))
    return HybridOutput(embedding_model=config.model if config else "", query=payload.query, top_k=payload.top_k, candidate_k=payload.candidate_k,
                        corpus_signature=signature, corpus_chunk_count=len(chunks),
                        unique_candidate_count=len(fused), overlap_count=len(set(bm_ids)&set(vec_ids)),
                        query_tokens=terms, degraded=degraded, mock_mode=bool(config and config.mock_mode),
                        mode=mode, message=message, elapsed_ms=round((perf_counter()-started)*1000),
                        routes={'bm25': bm_route, 'vector': vec_route}, rerank=rerank_info, hits=hits)
