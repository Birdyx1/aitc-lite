"""知识快照在模型调用前落库，供检查点恢复与历史引用追溯使用。"""
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import update

from .hybrid_search import HybridInput, HybridOutput, RouteOutput, RerankOutput, retrieve_hybrid
from .knowledge.bm25 import current_signature, read_chunks
from .models import GenerationKnowledge, KnowledgeDocument, KnowledgeChunk, Requirement
from .requirements import require_requirement


def context_is_stale(session, project_id, context):
    return bool(context and context.snapshot and
                context.snapshot['corpus_signature'] != current_signature(session, project_id))


def assert_context(session, project_id, run_id):
    context = session.get(GenerationKnowledge, run_id)
    if not context or not context.snapshot or context_is_stale(session, project_id, context):
        raise HTTPException(409, '知识库已变化或检索快照不可用，请重新检索并生成；历史草稿不变。')


async def resolve_context(app, project_id, requirement_id, payload, fingerprint, requirement, feature):
    with app.state.session_factory() as session:
        session.execute(update(Requirement).where(Requirement.id == requirement_id, Requirement.project_id == project_id)
                        .values(updated_at=Requirement.updated_at))
        require_requirement(session, project_id, requirement_id)
        context = session.get(GenerationKnowledge, str(payload.request_id))
        if context:
            if context.requirement_id != requirement_id or context.request_fingerprint != fingerprint:
                raise HTTPException(409, '知识快照请求标识已被使用，请重新生成。')
            if context.attempt_pending and not context.snapshot:
                raise HTTPException(409, "上次知识检索没有留下可复用快照；请确认重复调用费用后恢复任务，或重新发起生成。")
            if context.snapshot:
                assert_context(session, project_id, context.id)
                return deepcopy(context.snapshot)
        else:
            context = GenerationKnowledge(id=str(payload.request_id), requirement_id=requirement_id,
                                          request_fingerprint=fingerprint, snapshot=None, case_citations={})
            session.add(context)
        context.attempt_pending = True
        session.commit()
    query = payload.rag_query or ' '.join([feature['feature'], feature['description'],
                                          feature.get('acceptance_criteria', ''), requirement['title']])[:200]
    with app.state.session_factory() as session:
        try:
            result = await retrieve_hybrid(project_id, HybridInput(query=query, top_k=payload.rag_top_k,
                                          candidate_k=20, use_rerank=payload.rag_rerank, rerank_k=20), app, session)
        except HTTPException as error:
            if not payload.rag_allow_empty or error.status_code != 503:
                raise
            # 双路不可用时只按用户明确选择降级；项目删除、版本冲突绝不降级。
            session.rollback()
            require_requirement(session, project_id, requirement_id)
            result = HybridOutput(query=query, top_k=payload.rag_top_k, candidate_k=20,
                corpus_signature=current_signature(session, project_id), corpus_chunk_count=len(read_chunks(session, project_id)),
                unique_candidate_count=0, overlap_count=0, query_tokens=[], degraded=True, mock_mode=False,
                mode='no_results', message='知识检索服务不可用；按本次明确选择，不注入知识继续生成。', elapsed_ms=0,
                routes={'bm25': RouteOutput(status='error', message='双路检索未成功完成，未使用候选。'),
                        'vector': RouteOutput(status='error', message='双路检索未成功完成，未使用候选。')},
                rerank=RerankOutput(status='empty', message='没有有效候选，未使用精排结果。'), hits=[])
        if not result.hits:
            if not payload.rag_allow_empty:
                raise HTTPException(400, '未检索到知识分块，未调用生成模型。请调整知识查询、补充文档，或明确允许无知识继续生成。')
            result.degraded = True
            if '服务不可用' not in result.message:
                result.message = '未检索到有效知识分块；按本次明确选择，不注入知识继续生成。'
        snapshot = result.model_dump(mode='json')
        sources = [{**hit, 'citation_id': f'K{rank}'} for rank, hit in enumerate(snapshot.pop('hits'), 1)]
        snapshot['sources'] = sources
        snapshot['context_version'] = 'rag-context-v1'
        # 与文档写入串行化：检索到发布快照之间再次核对。
        session.rollback()
        session.execute(update(Requirement).where(Requirement.id == requirement_id,
                        Requirement.project_id == project_id).values(updated_at=Requirement.updated_at))
        require_requirement(session, project_id, requirement_id)
        if current_signature(session, project_id) != snapshot['corpus_signature']:
            raise HTTPException(409, '知识库在保存检索快照时发生变化，请重新生成。')
        context = session.get(GenerationKnowledge, str(payload.request_id))
        context.snapshot, context.attempt_pending = snapshot, False
        session.commit()
        return deepcopy(snapshot)


def evidence(session, project_id, context):
    if not context or not context.snapshot:
        return None
    result = deepcopy(context.snapshot)
    result['case_citations'] = context.case_citations
    result['outdated'] = context_is_stale(session, project_id, context)
    for source in result['sources']:
        document = session.get(KnowledgeDocument, source['document_id'])
        chunk = session.get(KnowledgeChunk, source['chunk_id'])
        source['current_status'] = ('deleted' if not document else 'updated' if
                                    document.revision != source['document_revision'] or not chunk else 'current')
    return result
