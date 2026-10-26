import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .deps import get_session
from .features import content_hash
from .llm import chat_completion
from .models import CaseDraft, CaseGeneration, Feature, FeatureSheet, Project, Requirement, utc_now
from .requirements import require_requirement
from .schemas import CaseDraftOutput, CaseGenerateInput, CaseGenerationOutput, CaseGenerationSummary, FeatureItem
from .settings import load_model_config
from .workflows.case_generation import PROMPT_VERSION, build_case_graph

router = APIRouter(prefix="/api/projects/{project_id}/requirements/{requirement_id}/case-generations", tags=["用例草稿生成"])


def request_fingerprint(payload: CaseGenerateInput) -> str:
    data = payload.model_dump(mode="json", exclude={"request_id"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def summary(session: Session, requirement: Requirement, generation: CaseGeneration) -> CaseGenerationSummary:
    sheet = session.get(FeatureSheet, requirement.id)
    return CaseGenerationSummary(
        id=generation.id, feature_name=generation.feature_name, strategy=generation.strategy,
        source=generation.source, model=generation.model, case_count=generation.case_count,
        created_at=generation.created_at,
        outdated=(content_hash(requirement.title, requirement.content) != generation.input_hash
                  or not sheet or not sheet.confirmed or sheet.revision != generation.sheet_revision),
    )


def detail(session: Session, requirement: Requirement, generation: CaseGeneration) -> CaseGenerationOutput:
    cases = session.scalars(select(CaseDraft).where(CaseDraft.generation_id == generation.id).order_by(CaseDraft.sort_order)).all()
    return CaseGenerationOutput(
        **summary(session, requirement, generation).model_dump(),
        sheet_revision=generation.sheet_revision, input_hash=generation.input_hash,
        feature_snapshot=generation.feature_snapshot, requirement_snapshot=generation.requirement_snapshot,
        prompt_version=generation.prompt_version, graph_nodes=generation.graph_nodes,
        cases=[CaseDraftOutput.model_validate(case, from_attributes=True) for case in cases],
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
    requirement = require_requirement(session, project_id, requirement_id)
    generation = session.get(CaseGeneration, str(generation_id))
    if generation is None or generation.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail="该需求下的生成记录不存在。")
    return detail(session, requirement, generation)


@router.post("", response_model=CaseGenerationOutput, status_code=201)
async def generate_cases(project_id: int, requirement_id: int, payload: CaseGenerateInput,
                         request: Request, response: Response, session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    cached = replay(session, requirement, payload)
    if cached:
        response.status_code = 200
        return cached
    feature = require_confirmed_feature(session, requirement, payload)
    feature_snapshot = FeatureItem.model_validate(feature, from_attributes=True).model_dump(mode="json")
    requirement_snapshot = {"title": requirement.title, "content": requirement.content}
    config = load_model_config(session)
    session.rollback()

    async def call_model(messages, max_tokens):
        return await chat_completion(config, messages, max_tokens, transport=request.app.state.llm_transport)

    graph = build_case_graph(call_model)
    state = await graph.ainvoke({
        "requirement": requirement_snapshot, "feature": feature_snapshot, "strategy": payload.strategy,
        "source": "mock" if config.mock_mode else "real", "trace": [],
    })

    # 网络期间可能有人编辑/删除需求。重新取得写锁并核对确认版本，成功后整批入库。
    locked = session.execute(update(Requirement).where(
        Requirement.id == requirement_id, Requirement.project_id == project_id,
        Requirement.title == requirement_snapshot["title"], Requirement.content == requirement_snapshot["content"],
    ).values(updated_at=utc_now()).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        raise HTTPException(status_code=409, detail="生成期间需求已变化或删除，结果未入库，请重新核对后生成。")
    session.expire_all()
    requirement = require_requirement(session, project_id, requirement_id)
    cached = replay(session, requirement, payload)
    if cached:
        response.status_code = 200
        return cached
    require_confirmed_feature(session, requirement, payload)
    generation = CaseGeneration(
        id=str(payload.request_id), requirement_id=requirement_id, request_fingerprint=request_fingerprint(payload),
        input_hash=payload.input_hash, sheet_revision=payload.expected_revision,
        feature_name=feature_snapshot["feature"], feature_snapshot=feature_snapshot,
        requirement_snapshot=requirement_snapshot, strategy=payload.strategy, source=state["source"],
        model="" if config.mock_mode else config.model, prompt_version=PROMPT_VERSION,
        graph_nodes=state["trace"], case_count=len(state["cases"]),
    )
    session.add(generation)
    session.flush()
    for index, case in enumerate(state["cases"]):
        session.add(CaseDraft(id=str(uuid4()), generation_id=generation.id, sort_order=index, **case))
    session.get(Project, project_id).updated_at = utc_now()
    session.commit()
    return detail(session, requirement, generation)
