import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .deps import get_session
from .checkpoints import graph_config
from .features import content_hash
from .llm import chat_completion
from .models import GenerationSkills, GenerationKnowledge, CaseDraft, CaseGeneration, GenerationTask, Feature, FeatureSheet, Project, Requirement, utc_now
from .requirements import require_requirement
from .schemas import CaseDraftOutput, CaseGenerateInput, CaseGenerationOutput, CaseGenerationSummary, FeatureItem
from .settings import load_model_config
from .workflows.case_generation import PROMPT_VERSION, RAG_PROMPT_VERSION, SKILL_PROMPT_VERSION, RAG_SKILL_PROMPT_VERSION, build_case_graph
from .skill_snapshots import bind_skills, skill_evidence
from .specialists.registry import assert_supported
from .rag import resolve_context, assert_context, context_is_stale, evidence

router = APIRouter(prefix="/api/projects/{project_id}/requirements/{requirement_id}/case-generations", tags=["用例草稿生成"])


def request_fingerprint(payload: CaseGenerateInput) -> str:
    data = payload.model_dump(mode="json", exclude={"request_id"})
    if not payload.skill_ids:
        data.pop("skill_ids", None)
    if not payload.use_rag:
        for name in ("use_rag", "rag_query", "rag_top_k", "rag_rerank", "rag_allow_empty"):
            data.pop(name, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def summary(session: Session, requirement: Requirement, generation: CaseGeneration) -> CaseGenerationSummary:
    sheet = session.get(FeatureSheet, requirement.id)
    skills = session.get(GenerationSkills, generation.id)
    return CaseGenerationSummary(
        id=generation.id, feature_name=generation.feature_name, strategy=generation.strategy,
        source=generation.source, model=generation.model, case_count=generation.case_count,
        created_at=generation.created_at,
        skill_names=[item["name"] for item in skills.snapshot] if skills else [],
        outdated=(content_hash(requirement.title, requirement.content) != generation.input_hash
                  or not sheet or not sheet.confirmed or sheet.revision != generation.sheet_revision
                  or context_is_stale(session, requirement.project_id, session.get(GenerationKnowledge, generation.id))),
    )


def detail(session: Session, requirement: Requirement, generation: CaseGeneration) -> CaseGenerationOutput:
    cases = session.scalars(select(CaseDraft).where(CaseDraft.generation_id == generation.id).order_by(CaseDraft.sort_order)).all()
    return CaseGenerationOutput(
        **summary(session, requirement, generation).model_dump(),
        sheet_revision=generation.sheet_revision, input_hash=generation.input_hash,
        feature_snapshot=generation.feature_snapshot, requirement_snapshot=generation.requirement_snapshot,
        prompt_version=generation.prompt_version, graph_nodes=generation.graph_nodes,
        cases=[CaseDraftOutput.model_validate(case, from_attributes=True) for case in cases],
        knowledge=evidence(session, requirement.project_id, session.get(GenerationKnowledge, generation.id)),
        skills=skill_evidence(session.get(GenerationSkills, generation.id)),
    )


def replay(session: Session, requirement: Requirement, payload: CaseGenerateInput):
    existing = session.get(CaseGeneration, str(payload.request_id))
    if existing is None:
        return None
    if existing.requirement_id != requirement.id or existing.request_fingerprint != request_fingerprint(payload):
        raise HTTPException(status_code=409, detail="生成请求标识已被使用，请重新发起。")
    return detail(session, requirement, existing)


def require_confirmed_feature(session: Session, requirement: Requirement, payload: CaseGenerateInput):
    sheet = session.get(FeatureSheet, requirement.id)
    current_hash = content_hash(requirement.title, requirement.content)
    if (not sheet or not sheet.confirmed or sheet.source_hash != current_hash
            or sheet.revision != payload.expected_revision or current_hash != payload.input_hash):
        raise HTTPException(status_code=409, detail="需求或功能点已变化，或测试范围尚未确认，请重新核对并确认后生成。")
    feature = session.get(Feature, str(payload.feature_id))
    if not feature or feature.requirement_id != requirement.id or not feature.included:
        raise HTTPException(status_code=409, detail="请选择当前需求中已确认且纳入范围的功能点。")
    return feature


@router.get("", response_model=list[CaseGenerationSummary])
def list_generations(project_id: int, requirement_id: int, session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    generations = session.scalars(select(CaseGeneration).where(CaseGeneration.requirement_id == requirement_id)
                                  .order_by(CaseGeneration.created_at.desc(), CaseGeneration.id.desc())).all()
    return [summary(session, requirement, item) for item in generations]


@router.get("/{generation_id}", response_model=CaseGenerationOutput)
def get_generation(project_id: int, requirement_id: int, generation_id: UUID, session: Session = Depends(get_session)):
    requirement, generation = require_generation(session, project_id, requirement_id, str(generation_id))
    return detail(session, requirement, generation)


def require_generation(session: Session, project_id: int, requirement_id: int, generation_id: str):
    requirement = require_requirement(session, project_id, requirement_id)
    generation = session.get(CaseGeneration, str(generation_id))
    if generation is None or generation.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail="该需求下的生成记录不存在。")
    return requirement, generation


@router.post("", response_model=CaseGenerationOutput, status_code=201)
async def generate_cases(project_id: int, requirement_id: int, payload: CaseGenerateInput,
                         request: Request, response: Response, session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    cached = replay(session, requirement, payload)
    if cached:
        response.status_code = 200
        return cached
    if session.scalar(select(GenerationTask.id).where(
            GenerationTask.requirement_id == requirement_id, GenerationTask.status.in_(("queued", "running"))).limit(1)):
        raise HTTPException(status_code=409, detail="该需求已有生成任务，请等待完成。")
    return await run_generation(project_id, requirement_id, payload, request.app, session)


async def run_generation(project_id, requirement_id, payload, app, session, progress=None, runtime_config=None, checkpointed=False, model_started=None, guard_save=None):
    requirement = require_requirement(session, project_id, requirement_id)
    cached = replay(session, requirement, payload)
    if cached:
        return cached
    previous_context = session.get(GenerationKnowledge, str(payload.request_id))
    if previous_context and (previous_context.requirement_id != requirement_id or previous_context.request_fingerprint != request_fingerprint(payload)):
        raise HTTPException(409, "生成请求标识已关联其他知识快照，请使用新的请求标识。")
    feature = require_confirmed_feature(session, requirement, payload)
    feature_snapshot = FeatureItem.model_validate(feature, from_attributes=True).model_dump(mode="json")
    requirement_snapshot = {"title": requirement.title, "content": requirement.content}
    config = runtime_config or load_model_config(session)
    session.rollback()
    with app.state.session_factory() as skill_session:
        if payload.skill_ids:
            skill_session.execute(update(Requirement).where(Requirement.id == requirement_id,
                                 Requirement.project_id == project_id).values(updated_at=Requirement.updated_at))
            require_requirement(skill_session, project_id, requirement_id)
        skill_items = bind_skills(skill_session, payload, requirement_id, request_fingerprint(payload))
        skill_session.commit()

    async def call_model(messages, max_tokens):
        if payload.use_rag or payload.skill_ids:
            assert_supported(skill_items)
            with app.state.session_factory() as guard_session:
                current = require_requirement(guard_session, project_id, requirement_id)
                require_confirmed_feature(guard_session, current, payload)
                if payload.use_rag:
                    assert_context(guard_session, project_id, str(payload.request_id))
        if model_started:
            model_started()
        return await chat_completion(config, messages, max_tokens, transport=app.state.llm_transport)

    async def knowledge_provider():
        return await resolve_context(app, project_id, requirement_id, payload, request_fingerprint(payload),
                                     requirement_snapshot, feature_snapshot)

    graph = build_case_graph(call_model, app.state.checkpointer if checkpointed else None,
                             knowledge_provider if payload.use_rag else None, skill_snapshot=skill_items)
    initial = {
        "requirement": requirement_snapshot, "feature": feature_snapshot, "strategy": payload.strategy,
        "source": "mock" if config.mock_mode else "real", "trace": [],
    }
    state = initial
    if checkpointed:
        graph_options = graph_config(str(payload.request_id))
        snapshot = await graph.aget_state(graph_options)
        graph_input = None if snapshot.values else initial
        if snapshot.values:
            state = snapshot.values
        steps = 0
        # pending writes 已保存但尚未合并时，snapshot.next 可能为空；仍需让图推进合并。
        while graph_input is not None or snapshot.next or "validate" not in state.get("trace", []):
            steps += 1
            if steps > 8:
                raise HTTPException(status_code=409, detail="检查点无法继续推进，请重新生成。")
            if progress:
                await progress(state.get("trace", []))
            state = await graph.ainvoke(graph_input, graph_options,
                                       interrupt_after=["prepare", "generate", "validate"], durability="sync")
            graph_input = None
            snapshot = await graph.aget_state(graph_options)
        if progress:
            await progress(state.get("trace", []))
    else:
        async for state in graph.astream(initial, stream_mode="values"):
            if progress:
                await progress(state.get("trace", []))

    # 网络期间可能有人编辑/删除需求。重新取得写锁并核对确认版本，成功后整批入库。
    locked = session.execute(update(Requirement).where(
        Requirement.id == requirement_id, Requirement.project_id == project_id,
        Requirement.title == requirement_snapshot["title"], Requirement.content == requirement_snapshot["content"],
    ).values(updated_at=utc_now()).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        raise HTTPException(status_code=409, detail="生成期间需求已变化或删除，结果未入库，请重新核对后生成。")
    session.expire_all()
    if guard_save:
        guard_save(session)
    requirement = require_requirement(session, project_id, requirement_id)
    cached = replay(session, requirement, payload)
    if cached:
        return cached
    require_confirmed_feature(session, requirement, payload)
    if payload.use_rag:
        assert_context(session, project_id, str(payload.request_id))
    generation = CaseGeneration(
        id=str(payload.request_id), requirement_id=requirement_id, request_fingerprint=request_fingerprint(payload),
        input_hash=payload.input_hash, sheet_revision=payload.expected_revision,
        feature_name=feature_snapshot["feature"], feature_snapshot=feature_snapshot,
        requirement_snapshot=requirement_snapshot, strategy=payload.strategy, source=state["source"],
        model="" if config.mock_mode else config.model, prompt_version=((RAG_SKILL_PROMPT_VERSION if payload.use_rag else SKILL_PROMPT_VERSION) if skill_items else
                        (RAG_PROMPT_VERSION if payload.use_rag else PROMPT_VERSION)),
        graph_nodes=state["trace"], case_count=len(state["cases"]),
    )
    session.add(generation)
    session.flush()
    citations, skill_tags = {}, {}
    for index, case in enumerate(state["cases"]):
        draft_id = str(uuid4())
        session.add(CaseDraft(id=draft_id, generation_id=generation.id, sort_order=index, **case))
        skill_tags[draft_id] = state.get("skill_tags", [[] for _ in state["cases"]])[index]
        citations[draft_id] = state.get("citations", [[] for _ in state["cases"]])[index]
    if payload.use_rag:
        session.get(GenerationKnowledge, generation.id).case_citations = citations
    if skill_items:
        session.get(GenerationSkills, generation.id).case_tags = skill_tags
    session.get(Project, project_id).updated_at = utc_now()
    session.commit()
    return detail(session, requirement, generation)
