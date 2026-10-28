from dataclasses import asdict
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .deps import get_session
from .knowledge.bm25 import (ALGORITHM, B, K1, TOKENIZER, corpus_signature, current_signature,
                             rank_chunks, read_chunks, tokenize)
from .projects import require_project
from .schemas import KnowledgeSearchHit, KnowledgeSearchInput, KnowledgeSearchOutput

router = APIRouter(prefix="/api/projects/{project_id}/knowledge-search", tags=["知识关键词检索"])


@router.post("", response_model=KnowledgeSearchOutput)
def search_knowledge(project_id: int, payload: KnowledgeSearchInput, session: Session = Depends(get_session)):
    require_project(session, project_id)
    start = perf_counter()
    try:
        terms = list(dict.fromkeys(tokenize(payload.query)))
    except Exception:
        raise HTTPException(status_code=503, detail="关键词分词暂时不可用，请稍后重试。") from None
    if len(terms) > 64:
        raise HTTPException(status_code=400, detail="检索词过多，请缩短关键词（最多 64 个不同词项）。")
    chunks = read_chunks(session, project_id)
    signature = corpus_signature(chunks)
    indexed_count, matches = 0, []
    if not terms:
        status, message = "no_terms", "未提取到有效检索词，请输入中文、英文关键词或错误码。"
    elif not chunks:
        status, message = "empty_corpus", "当前项目尚无有效知识分块，请先保存知识文档。"
    else:
        try:
            matches, indexed_count = rank_chunks(chunks, terms)
        except Exception:
            raise HTTPException(status_code=503, detail="BM25 关键词检索暂时不可用，请稍后重试。") from None
        if not indexed_count:
            status, message = "no_indexable_chunks", "当前分块未包含可检索词项，请补充中文、英文或错误码文本。"
        elif not matches:
            status, message = "no_matches", "没有命中当前项目的知识分块，请尝试原文中的词语或错误码。"
        else:
            status, message = "matched", "已按 BM25 相关度排序；多词查询可命中部分词项，分数不是置信度。"
    # 评分期间不持有读事务；重新核对当前有效块集合，拒绝返回过期的排名或片段。
    session.rollback()
    require_project(session, project_id)
    if current_signature(session, project_id) != signature:
        raise HTTPException(status_code=409, detail="知识库在检索期间发生变化，请重新检索获取最新结果。")
    hits = [KnowledgeSearchHit(rank=index, score=score, matched_tokens=matched, **asdict(chunk))
            for index, (chunk, score, matched) in enumerate(matches[:payload.top_k], 1)]
    return KnowledgeSearchOutput(
        project_id=project_id, query=payload.query, top_k=payload.top_k, query_tokens=terms,
        algorithm=ALGORITHM, tokenizer=TOKENIZER, k1=K1, b=B, corpus_signature=signature,
        corpus_chunk_count=len(chunks), indexed_chunk_count=indexed_count, total_matches=len(matches),
        elapsed_ms=round((perf_counter() - start) * 1000), status=status, message=message, hits=hits,
    )
