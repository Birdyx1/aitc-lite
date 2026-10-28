"""统一只读工具适配层。项目来自运行时上下文，不接受模型传项目或执行命令。"""
import asyncio
from dataclasses import asdict
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from ..features import content_hash
from ..execution_tasks import project_execution_summary
from ..hybrid_search import HybridInput, bm25_recall, retrieve_hybrid
from ..judge_runs import current_input_hash
from ..knowledge.bm25 import read_chunks
from ..models import (CaseDraft, CaseGeneration, CaseReview, Feature, FeatureSheet, JudgeRun,
                      KnowledgeChunk, KnowledgeDocument, Requirement)
from ..projects import require_project
from ..requirements import require_requirement
from ..reviews import review_output


class Args(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NoArgs(Args):
    pass


class ListRequirements(Args):
    query: str = Field(default='', max_length=100)
    limit: int = Field(default=5, ge=1, le=10, strict=True)

    @field_validator('query')
    @classmethod
    def valid_query(cls, value):
        if any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('invalid text')
        return value.strip()


class GetRequirement(Args):
    requirement_id: int = Field(ge=1, strict=True)


class SearchKnowledge(Args):
    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=3, ge=1, le=5, strict=True)
    use_rerank: bool = Field(default=False, strict=True)

    @field_validator('query')
    @classmethod
    def valid_query(cls, value):
        value = ListRequirements.valid_query(value)
        if not value:
            raise ValueError('empty query')
        return value


class ListCases(ListRequirements):
    requirement_id: int | None = Field(default=None, ge=1, strict=True)
    status: Literal['all', 'pending', 'accepted', 'rejected'] = 'all'


class ListJudge(Args):
    requirement_id: int | None = Field(default=None, ge=1, strict=True)
    limit: int = Field(default=3, ge=1, le=5, strict=True)


def overview(session, project_id, args):
    project = require_project(session, project_id)
    req_ids = select(Requirement.id).where(Requirement.project_id == project_id)
    gen_ids = select(CaseGeneration.id).where(CaseGeneration.requirement_id.in_(req_ids))
    def count(model, *conditions):
        return session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
    status = func.coalesce(CaseReview.status, 'pending')
    review_counts = dict(session.execute(select(status, func.count(CaseDraft.id)).outerjoin(CaseReview, CaseReview.draft_id == CaseDraft.id)
                            .where(CaseDraft.generation_id.in_(gen_ids)).group_by(status)).all())
    current_generated = session.scalar(select(func.count(func.distinct(Feature.id))).select_from(Feature)
        .join(FeatureSheet, FeatureSheet.requirement_id == Feature.requirement_id)
        .join(Requirement, Requirement.id == Feature.requirement_id)
        .join(CaseGeneration, (CaseGeneration.requirement_id == Requirement.id) &
              (CaseGeneration.feature_snapshot['id'].as_string() == Feature.id))
        .where(Requirement.project_id == project_id, Feature.included.is_(True), FeatureSheet.confirmed.is_(True),
               CaseGeneration.sheet_revision == FeatureSheet.revision,
               CaseGeneration.requirement_snapshot['title'].as_string() == Requirement.title,
               CaseGeneration.requirement_snapshot['content'].as_string() == Requirement.content)) or 0
    judges = dict(session.execute(select(JudgeRun.status, func.count()).where(JudgeRun.generation_id.in_(gen_ids))
                                 .group_by(JudgeRun.status)).all())
    chunks = session.scalar(select(func.count(KnowledgeChunk.id)).join(KnowledgeDocument)
        .where(KnowledgeDocument.project_id == project_id, KnowledgeChunk.document_revision == KnowledgeDocument.revision)) or 0
    return {'project_id': project_id, 'name': project.name, 'description': project.description,
        'requirements': count(Requirement, Requirement.project_id == project_id),
        'included_features': count(Feature, Feature.requirement_id.in_(req_ids), Feature.included.is_(True)),
        'current_features_with_generation': current_generated,
        'coverage_note': '仅统计当前纳入功能点中有对应版本生成记录的数量，不代表语义覆盖率或测试通过率。',
        'generation_batches': count(CaseGeneration, CaseGeneration.requirement_id.in_(req_ids)),
        'drafts': count(CaseDraft, CaseDraft.generation_id.in_(gen_ids)), 'review_counts': review_counts,
        'knowledge_documents': count(KnowledgeDocument, KnowledgeDocument.project_id == project_id),
        'knowledge_chunks': chunks, 'judge_counts': judges,
        'judge_note': '评估数量包含历史记录；请查询具体报告核对 Mock、输入版本和评分，不等于实测结果。',
        'execution': project_execution_summary(session, project_id)}


def list_requirements(session, project_id, args):
    query = select(Requirement).where(Requirement.project_id == project_id)
    if args.query:
        query = query.where(Requirement.title.contains(args.query, autoescape=True))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(Requirement.updated_at.desc(), Requirement.id.desc()).limit(args.limit)).all()
    return {'total_matches': total, 'limit': args.limit, 'items': [{'requirement_id': row.id, 'title': row.title,
        'content_excerpt': row.content[:240], 'excerpt_truncated': len(row.content) > 240} for row in rows]}


def get_requirement(session, project_id, args):
    req = require_requirement(session, project_id, args.requirement_id)
    sheet = session.get(FeatureSheet, req.id)
    features = session.scalars(select(Feature).where(Feature.requirement_id == req.id).order_by(Feature.sort_order).limit(10)).all()
    return {'requirement_id': req.id, 'title': req.title, 'content': req.content[:6000],
        'content_truncated': len(req.content) > 6000, 'sheet_revision': sheet.revision if sheet else None,
        'scope_confirmed': bool(sheet and sheet.confirmed and sheet.source_hash == content_hash(req.title, req.content)),
        'features': [{'id': item.id, 'name': item.feature, 'included': item.included,
                      'description': item.description[:600]} for item in features],
        'features_limit': 10, 'message': '最多返回前 10 个功能点，正文最多 6000 字符；需要全文请打开需求页。'}


def list_cases(session, project_id, args):
    if args.requirement_id is not None:
        require_requirement(session, project_id, args.requirement_id)
    query = select(CaseDraft, CaseReview, Requirement.id).join(CaseGeneration, CaseGeneration.id == CaseDraft.generation_id)
    query = query.join(Requirement, Requirement.id == CaseGeneration.requirement_id).outerjoin(CaseReview, CaseReview.draft_id == CaseDraft.id)
    query = query.where(Requirement.project_id == project_id)
    if args.requirement_id is not None:
        query = query.where(Requirement.id == args.requirement_id)
    if args.status != 'all':
        query = query.where(func.coalesce(CaseReview.status, 'pending') == args.status)
    if args.query:
        title = func.coalesce(CaseReview.edited_case['title'].as_string(), CaseDraft.title)
        query = query.where(title.contains(args.query, autoescape=True))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(query.order_by(CaseGeneration.created_at.desc(), CaseDraft.sort_order).limit(args.limit)).all()
    items = []
    for draft, review, rid in rows:
        current = review_output(draft, review)
        fields = current.current.model_dump()
        truncated = (len(fields['steps']) > 6 or any(len(step) > 300 for step in fields['steps'])
                     or len(fields['expected_result']) > 1200 or len(fields['precondition']) > 600)
        fields['steps'] = [step[:300] for step in fields['steps'][:6]]
        fields['expected_result'] = fields['expected_result'][:1200]
        fields['precondition'] = fields['precondition'][:600]
        items.append({'case_id': draft.id, 'requirement_id': rid, 'generation_id': draft.generation_id,
                      'status': current.status, 'revision': current.revision, 'was_edited': current.was_edited,
                      'fields': fields, 'content_truncated': truncated})
    return {'total_matches': total, 'limit': args.limit, 'items': items}


def list_judges(session, project_id, args):
    if args.requirement_id is not None:
        require_requirement(session, project_id, args.requirement_id)
    query = select(JudgeRun, Requirement.id).join(CaseGeneration, CaseGeneration.id == JudgeRun.generation_id)
    query = query.join(Requirement, Requirement.id == CaseGeneration.requirement_id).where(Requirement.project_id == project_id)
    if args.requirement_id is not None:
        query = query.where(Requirement.id == args.requirement_id)
    rows = session.execute(query.order_by(JudgeRun.created_at.desc(), JudgeRun.id.desc()).limit(args.limit)).all()
    current = {}
    results = []
    for run, rid in rows:
        if run.generation_id not in current:
            current[run.generation_id] = current_input_hash(session, project_id, rid, run.generation_id)
        report = run.report
        results.append({'judge_id': run.id, 'requirement_id': rid, 'generation_id': run.generation_id,
            'status': run.status, 'model': run.model, 'model_source': run.model_source, 'mock_mode': run.mock_mode,
            'outdated': current[run.generation_id] != run.input_hash, 'error': run.error_message,
            'report': {'checked_count': report['checked_count'], 'usable_count': report['usable_count'],
                       'suspect_count': report['suspect_count'], 'average_score': report['average_score'],
                       'items': [{'case_id': item['case_id'], 'usable': item['usable'], 'hallucinations': item['hallucinations'][:3]}
                                 for item in report['items']]} if report else None})
    return {'items': results, 'limit': args.limit, 'message': '只读历史评估；Mock、过期或失败记录不能视为当前实测结论。'}


TOOLS = {
    'builtin_project_overview': (NoArgs, '查询当前项目需求、功能点生成、评审、知识、Judge 数量与人工执行任务统计。普通计数不等于覆盖率；人工通过率只按人工执行条目计算。', overview),
    'builtin_list_requirements': (ListRequirements, '按标题关键词查询当前项目需求列表及正文摘要。', list_requirements),
    'builtin_get_requirement': (GetRequirement, '根据当前项目需求 ID 读取正文和功能点；不能查询其他项目。', get_requirement),
    'builtin_list_cases': (ListCases, '查询当前项目已保存的人工用例版本，可按需求、标题和评审状态筛选。', list_cases),
    'builtin_list_judge_reports': (ListJudge, '查询当前项目最近的只读 Judge 报告摘要，包含 Mock 与过期提示。', list_judges),
    'builtin_search_knowledge': (SearchKnowledge, '检索当前项目知识；真实模式复用混合检索，可选精排，可能调用 Embedding/Rerank；Mock 仅本地 BM25。', None),
}


def tool_schemas():
    return [{'type': 'function', 'function': {'name': name, 'description': description,
              'parameters': model.model_json_schema()}} for name, (model, description, _) in TOOLS.items()]


async def execute_tool(app, project_id, name, args, mock_mode=False):
    with app.state.session_factory() as session:
        require_project(session, project_id)
        if name == 'builtin_search_knowledge':
            if mock_mode:
                chunks = read_chunks(session, project_id)
                session.rollback()
                route, ids, terms, details = await asyncio.to_thread(bm25_recall, chunks, args.query, args.top_k)
                if route.status == 'error':
                    raise HTTPException(503, route.message)
                by_id = {chunk.chunk_id: chunk for chunk in chunks}
                return {'mode': 'mock_keyword_only', 'message': 'Mock Agent 使用本地 BM25，不调用 Embedding/Rerank。',
                        'route_status': route.status, 'route_message': route.message, 'query_tokens': terms,
                        'hits': [{'rank': index, 'score': details[id][0], **asdict(by_id[id])} for index, id in enumerate(ids, 1)]}
            result = await retrieve_hybrid(project_id, HybridInput(query=args.query, top_k=args.top_k,
                                           candidate_k=20, use_rerank=args.use_rerank), app, session)
            return result.model_dump(mode='json')
        return TOOLS[name][2](session, project_id, args)
