"""独立持久评估任务：评估当前已保存人工版本，结果不写入草稿或评审表。"""
import asyncio
import hashlib
import json
import unicodedata
from copy import deepcopy
from datetime import timezone
from time import perf_counter
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .case_generations import require_generation, summary
from .deps import get_session
from .features import content_hash
from .generation_tasks import config_signature
from .judge_protocol import PROMPT, PROMPT_VERSION, RUBRIC_VERSION, RULES_VERSION, mock_report, parse_report
from .judge_settings import resolve_judge_config, selected_transport
from .knowledge.bm25 import current_signature
from .llm import chat_completion
from .models import (CaseDraft, CaseGeneration, CaseReview, FeatureSheet, GenerationKnowledge,
                     GenerationSkills, JudgeRun, Requirement, utc_now)
from .rag import evidence
from .reviews import review_output
from .skill_snapshots import skill_evidence

router = APIRouter(prefix='/api/projects/{project_id}/requirements/{requirement_id}/case-generations/{generation_id}/judge-runs', tags=['AI Judge'])
ACTIVE = ('queued', 'running')


class JudgeSubmitInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    retry_of: UUID | None = None


class JudgeRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    generation_id: str
    retry_of: str | None
    status: Literal['queued', 'running', 'succeeded', 'failed', 'interrupted']
    input_hash: str
    model_source: str
    model: str
    mock_mode: bool
    prompt_version: str
    rubric_version: str
    error_message: str
    model_call_pending: bool
    elapsed_ms: int | None
    created_at: str
    outdated: bool
    checked_count: int | None
    usable_count: int | None


class JudgeRunOutput(JudgeRunSummary):
    input_snapshot: dict
    config_snapshot: dict
    prompt_snapshot: str
    report: dict | None


def normalized(value):
    return ''.join(unicodedata.normalize('NFKC', value).casefold().split())


def local_checks(cases, outdated):
    findings = []
    titles, content = {}, {}
    for case in cases:
        fields, id = case['fields'], case['case_id']
        title = normalized(fields['title'])
        body = json.dumps([normalized(fields['precondition']), [normalized(step) for step in fields['steps']],
                           normalized(fields['expected_result'])], ensure_ascii=False)
        for code, key, seen, message in [('duplicate_title', title, titles, '用例标题与另一条相同（忽略空白、大小写及全半角）。'),
                                         ('duplicate_content', body, content, '步骤、前置条件与预期结果与另一条相同，需人工确认是否重复。')]:
            if key in seen:
                findings.append({'code': code, 'case_id': id, 'related_case_id': seen[key], 'message': message})
            else:
                seen[key] = id
        if '待澄清' in json.dumps(fields, ensure_ascii=False):
            findings.append({'code': 'needs_clarification', 'case_id': id, 'related_case_id': None,
                             'message': '用例包含待澄清条件，请核实业务依据。'})
    if outdated:
        findings.append({'code': 'historical_evidence', 'case_id': None, 'related_case_id': None,
                         'message': '生成依据已变化，本次只评估所保存的历史依据与当前草稿快照。'})
    return findings


def build_input(session, project_id, requirement_id, generation_id):
    requirement, generation = require_generation(session, project_id, requirement_id, generation_id)
    drafts = session.scalars(select(CaseDraft).where(CaseDraft.generation_id == generation_id)
                             .order_by(CaseDraft.sort_order)).all()
    if not drafts or len(drafts) > 8:
        raise HTTPException(400, 'Judge 需要 1–8 条已保存的用例草稿。')
    cases = []
    try:
        for draft in drafts:
            review = review_output(draft, session.get(CaseReview, draft.id))
            cases.append({'case_id': draft.id, 'review_revision': review.revision, 'review_status': review.status,
                          'was_edited': review.was_edited, 'fields': review.current.model_dump()})
    except ValueError:
        raise HTTPException(400, '已保存草稿的字段或步骤结构无效，请核对后再评估。') from None
    sheet = session.get(FeatureSheet, requirement_id)
    knowledge = session.get(GenerationKnowledge, generation_id)
    generation_outdated = summary(session, requirement, generation).outdated
    snapshot = {'generation_id': generation_id, 'requirement': generation.requirement_snapshot,
        'feature': generation.feature_snapshot, 'strategy': generation.strategy,
        'generation_source': generation.source, 'generation_model': generation.model,
        'generation_prompt_version': generation.prompt_version, 'generation_outdated': generation_outdated,
        'current_source': {'requirement_hash': content_hash(requirement.title, requirement.content),
            'sheet_revision': sheet.revision if sheet else None, 'sheet_confirmed': bool(sheet and sheet.confirmed),
            'knowledge_signature': current_signature(session, project_id) if knowledge and knowledge.snapshot else None},
        'knowledge': evidence(session, project_id, knowledge),
        'skills': skill_evidence(session.get(GenerationSkills, generation_id)), 'cases': cases,
        'rules_version': RULES_VERSION, 'rule_findings': local_checks(cases, generation_outdated)}
    return snapshot


def input_hash(snapshot):
    try:
        raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (UnicodeError, ValueError):
        raise HTTPException(400, '评估输入含无效文本，请检查已保存内容。') from None
    if len(raw) > 512 * 1024:
        raise HTTPException(400, '评估输入超过 512 KiB，请缩短需求或草稿后再评估；未调用模型。')
    return hashlib.sha256(raw).hexdigest()


def judge_signature(config, source):
    return hashlib.sha256((source + ':' + config_signature(config)).encode()).hexdigest()


def run_summary(run, current_hash):
    return JudgeRunSummary(id=run.id, generation_id=run.generation_id, retry_of=run.retry_of, status=run.status,
        input_hash=run.input_hash, model_source=run.model_source, model=run.model, mock_mode=run.mock_mode,
        prompt_version=run.prompt_version, rubric_version=run.rubric_version, error_message=run.error_message,
        model_call_pending=run.model_call_pending, elapsed_ms=run.elapsed_ms, created_at=run.created_at.replace(tzinfo=timezone.utc).isoformat(),
        outdated=current_hash != run.input_hash,
        checked_count=run.report['checked_count'] if run.report else None,
        usable_count=run.report['usable_count'] if run.report else None)


def current_input_hash(session, project_id, requirement_id, generation_id):
    try:
        return input_hash(build_input(session, project_id, requirement_id, generation_id))
    except HTTPException:
        return None


def run_output(run, current_hash):
    return JudgeRunOutput(**run_summary(run, current_hash).model_dump(), input_snapshot=deepcopy(run.input_snapshot),
                           config_snapshot=deepcopy(run.config_snapshot), prompt_snapshot=run.prompt_snapshot,
                           report=deepcopy(run.report))


@router.get('', response_model=list[JudgeRunSummary])
def list_runs(project_id: int, requirement_id: int, generation_id: UUID, session: Session = Depends(get_session)):
    gid = str(generation_id)
    require_generation(session, project_id, requirement_id, gid)
    current = current_input_hash(session, project_id, requirement_id, gid)
    rows = session.scalars(select(JudgeRun).where(JudgeRun.generation_id == gid)
                           .order_by(JudgeRun.created_at.desc(), JudgeRun.id.desc())).all()
    return [run_summary(run, current) for run in rows]


@router.get('/{run_id}', response_model=JudgeRunOutput)
def get_run(project_id: int, requirement_id: int, generation_id: UUID, run_id: UUID,
            session: Session = Depends(get_session)):
    gid = str(generation_id)
    require_generation(session, project_id, requirement_id, gid)
    run = session.get(JudgeRun, str(run_id))
    if not run or run.generation_id != gid:
        raise HTTPException(404, '该生成记录下的 Judge 评估不存在。')
    return run_output(run, current_input_hash(session, project_id, requirement_id, gid))


@router.post('', response_model=JudgeRunOutput, status_code=202)
def submit_run(project_id: int, requirement_id: int, generation_id: UUID, payload: JudgeSubmitInput,
               response: Response, session: Session = Depends(get_session)):
    session.execute(text('BEGIN IMMEDIATE'))
    gid = str(generation_id)
    require_generation(session, project_id, requirement_id, gid)
    existing = session.get(JudgeRun, str(payload.request_id))
    if existing:
        if existing.generation_id != gid or existing.retry_of != (str(payload.retry_of) if payload.retry_of else None):
            raise HTTPException(409, 'Judge 请求标识已被使用，请重新发起。')
        response.status_code = 200
        return run_output(existing, current_input_hash(session, project_id, requirement_id, gid))
    if session.scalar(select(JudgeRun.id).where(JudgeRun.generation_id == gid, JudgeRun.status.in_(ACTIVE))):
        raise HTTPException(409, '这批草稿已有活动 Judge 任务，请等待完成。')
    if payload.retry_of:
        parent = session.get(JudgeRun, str(payload.retry_of))
        if not parent or parent.generation_id != gid or parent.status not in ('failed', 'interrupted'):
            raise HTTPException(409, '只能重试这批草稿已失败或中断的 Judge 任务。')
    snapshot = build_input(session, project_id, requirement_id, gid)
    digest = input_hash(snapshot)
    config, source = resolve_judge_config(session)
    run = JudgeRun(id=str(payload.request_id), generation_id=gid,
        retry_of=str(payload.retry_of) if payload.retry_of else None, input_hash=digest, input_snapshot=snapshot,
        settings_signature=judge_signature(config, source),
        config_snapshot={'base_url': config.base_url, 'model': config.model, 'model_source': source,
                         'mock_mode': config.mock_mode, 'timeout_seconds': config.timeout_seconds},
        model_source=source, model=config.model, mock_mode=config.mock_mode, prompt_version=PROMPT_VERSION,
        prompt_snapshot=PROMPT, rubric_version=RUBRIC_VERSION, report=None)
    session.add(run)
    session.commit()
    return run_output(run, digest)


def settle_judge_runs(app):
    with app.state.session_factory() as session:
        session.execute(update(JudgeRun).where(JudgeRun.status.in_(ACTIVE)).values(status='interrupted',
            error_message='服务停止或重启，评估已中断；未自动重试。手动重试可能重复计费。', updated_at=utc_now()))
        session.commit()


async def execute_judge(app, run_id):
    started = perf_counter()
    with app.state.session_factory() as session:
        run = session.get(JudgeRun, run_id)
        if not run or run.status != 'running':
            return
        generation = session.get(CaseGeneration, run.generation_id)
        requirement = session.get(Requirement, generation.requirement_id)
        config, source = resolve_judge_config(session)
        if judge_signature(config, source) != run.settings_signature:
            raise HTTPException(409, '排队期间 Judge 配置已变化，未调用模型，请重新评估。')
        if run.prompt_version != PROMPT_VERSION or run.prompt_snapshot != PROMPT or run.rubric_version != RUBRIC_VERSION:
            raise HTTPException(409, 'Judge 规则或提示词版本已变化，请重新评估。')
        if input_hash(build_input(session, requirement.project_id, requirement.id, generation.id)) != run.input_hash:
            raise HTTPException(409, '排队期间草稿或依据已变化，未调用模型，请重新评估当前版本。')
        snapshot = deepcopy(run.input_snapshot)
        run.model_call_pending = not config.mock_mode
        session.commit()
    raw = mock_report(snapshot) if config.mock_mode else await chat_completion(config,
        [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': json.dumps(snapshot, ensure_ascii=False)}],
        6144, transport=selected_transport(app, source), max_response_bytes=1024*1024)
    report = parse_report(raw, [case['case_id'] for case in snapshot['cases']])
    with app.state.session_factory() as session:
        run = session.get(JudgeRun, run_id)
        if run:
            # 即使人工版本在调用期间改变，也保存针对原快照的评估；读取时明确标过期。
            run.report, run.status, run.error_message = report, 'succeeded', ''
            run.model_call_pending = False
            run.elapsed_ms, run.updated_at = round((perf_counter()-started)*1000), utc_now()
            session.commit()


async def judge_worker(app):
    while True:
        try:
            with app.state.session_factory() as session:
                run_id = session.scalar(select(JudgeRun.id).where(JudgeRun.status == 'queued')
                                        .order_by(JudgeRun.created_at, JudgeRun.id).limit(1))
                if run_id:
                    changed = session.execute(update(JudgeRun).where(JudgeRun.id == run_id, JudgeRun.status == 'queued')
                                              .values(status='running', updated_at=utc_now()))
                    session.commit()
                    if not changed.rowcount:
                        run_id = None
            if run_id:
                attempt_start = perf_counter()
                try:
                    await execute_judge(app, run_id)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    with app.state.session_factory() as session:
                        run = session.get(JudgeRun, run_id)
                        if run:
                            run.status = 'failed'
                            run.elapsed_ms = round((perf_counter()-attempt_start)*1000)
                            run.error_message = str(error.detail) if isinstance(error, HTTPException) else 'Judge 执行失败，请核对配置和本地服务后手动重试。'
                            run.updated_at = utc_now()
                            session.commit()
            else:
                await asyncio.sleep(.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)
