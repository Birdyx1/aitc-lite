import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .deps import get_session
from .llm import chat_completion
from .models import Feature, FeatureSheet, Project, Requirement, utc_now
from .requirements import require_requirement
from .schemas import FeatureFields, FeatureItem, FeaturePreviewOutput, FeatureSaveInput, FeatureSheetOutput
from .settings import load_model_config

router = APIRouter(prefix="/api/projects/{project_id}/requirements/{requirement_id}/features", tags=["功能点"])
PROMPT = (Path(__file__).parent / "prompts" / "feature_parser.md").read_text(encoding="utf-8")


class ParsedFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: list[FeatureFields] = Field(min_length=1, max_length=50)


def content_hash(title: str, content: str) -> str:
    raw = json.dumps([title, content], ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sheet_output(session: Session, requirement: Requirement) -> FeatureSheetOutput:
    current_hash = content_hash(requirement.title, requirement.content)
    sheet = session.get(FeatureSheet, requirement.id)
    items = session.scalars(select(Feature).where(Feature.requirement_id == requirement.id).order_by(Feature.sort_order)).all()
    stale = bool(sheet and sheet.source_hash != current_hash)
    return FeatureSheetOutput(
        revision=sheet.revision if sheet else 0, current_hash=current_hash,
        source_hash=sheet.source_hash if sheet else "", source=sheet.source if sheet else "manual",
        model=sheet.model if sheet else "", stale=stale,
        confirmed=bool(sheet and sheet.confirmed and not stale),
        items=[FeatureItem.model_validate(item, from_attributes=True) for item in items],
    )


@router.get("", response_model=FeatureSheetOutput)
def get_features(project_id: int, requirement_id: int, session: Session = Depends(get_session)):
    return sheet_output(session, require_requirement(session, project_id, requirement_id))


@router.post("/analyze", response_model=FeaturePreviewOutput)
async def analyze_features(project_id: int, requirement_id: int, request: Request,
                           session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    title, content = requirement.title, requirement.content
    sheet = session.get(FeatureSheet, requirement_id)
    revision = sheet.revision if sheet else 0
    config = load_model_config(session)
    # 模型调用只生成预览；释放数据库事务，不在网络等待期间持有写锁。
    session.rollback()
    if config.mock_mode:
        lines = [line.strip() for line in content.splitlines() if line.strip()][:5]
        fields = [FeatureFields(
            module="Mock 示例", feature=f"示例功能点 {index + 1}",
            description=f"Mock 仅展示流程，请人工核对：{line[:1000]}",
            acceptance_criteria="", constraints="待人工根据原文补充", priority="P1",
        ) for index, line in enumerate(lines)]
    else:
        reply = await chat_completion(config, [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps({"title": title, "content": content}, ensure_ascii=False)},
        ], max_tokens=4096, transport=request.app.state.llm_transport)
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", reply.strip(), re.DOTALL | re.IGNORECASE)
        raw = fenced.group(1) if fenced else reply.strip()
        try:
            if len(raw) > 200000:
                raise ValueError("response too large")
            fields = ParsedFeatures.model_validate_json(raw).features
        except (ValueError, ValidationError):
            raise HTTPException(status_code=502, detail="模型未返回有效的功能点 JSON，请重试或缩短需求。已有功能点未被修改。") from None
    return FeaturePreviewOutput(
        base_revision=revision, input_hash=content_hash(title, content),
        source="mock" if config.mock_mode else "real", model="" if config.mock_mode else config.model,
        items=[FeatureItem(**item.model_dump()) for item in fields],
    )


@router.put("", response_model=FeatureSheetOutput)
def save_features(project_id: int, requirement_id: int, payload: FeatureSaveInput,
                  session: Session = Depends(get_session)):
    requirement = require_requirement(session, project_id, requirement_id)
    if payload.input_hash != content_hash(requirement.title, requirement.content):
        raise HTTPException(status_code=409, detail="需求原文已变化，请重新加载并核对功能点后保存。")
    # 先通过有条件写入取得 SQLite 写锁，再检查清单版本，串行化同一需求的保存。
    changed = session.execute(update(Requirement).where(
        Requirement.id == requirement_id, Requirement.project_id == project_id,
        Requirement.title == requirement.title, Requirement.content == requirement.content,
    ).values(updated_at=utc_now()).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(status_code=409, detail="需求原文已变化，请重新加载并核对功能点后保存。")
    session.expire_all()
    sheet = session.get(FeatureSheet, requirement_id)
    if (sheet.revision if sheet else 0) != payload.expected_revision:
        raise HTTPException(status_code=409, detail="功能点已在其他页面更新，请重新加载后再保存。当前输入仍保留。")
    ids = [str(item.id) for item in payload.items]
    foreign_id = session.scalar(select(Feature.id).where(Feature.id.in_(ids), Feature.requirement_id != requirement_id))
    if foreign_id:
        raise HTTPException(status_code=409, detail="功能点 ID 已被其他需求使用，请重新添加该功能点。")
    if sheet is None:
        sheet = FeatureSheet(requirement_id=requirement_id, revision=0)
        session.add(sheet)
    sheet.revision += 1
    sheet.source_hash = payload.input_hash
    sheet.source = payload.source
    sheet.model = payload.model
    sheet.confirmed = payload.confirmed
    session.flush()
    existing = {item.id: item for item in session.scalars(select(Feature).where(Feature.requirement_id == requirement_id))}
    for index, item in enumerate(payload.items):
        feature = existing.get(str(item.id))
        if feature is None:
            feature = Feature(id=str(item.id), requirement_id=requirement_id)
            session.add(feature)
        for key, value in item.model_dump(exclude={"id"}).items():
            setattr(feature, key, value)
        feature.sort_order = index
    session.execute(delete(Feature).where(Feature.requirement_id == requirement_id, Feature.id.not_in(ids)))
    session.get(Project, project_id).updated_at = utc_now()
    session.commit()
    return sheet_output(session, session.get(Requirement, requirement_id))
