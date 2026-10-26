from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .case_generations import require_generation, summary
from .deps import get_session
from .models import CaseDraft, CaseGeneration, CaseReview, Requirement, utc_now
from .projects import require_project
from .schemas import AcceptedCaseOutput, CaseFields, CaseReviewInput, CaseReviewOutput, CaseReviewsOutput

router = APIRouter(prefix="/api/projects/{project_id}/requirements/{requirement_id}/case-generations/{generation_id}/reviews", tags=["草稿评审"])
library_router = APIRouter(prefix="/api/projects/{project_id}/testcases", tags=["已采纳用例"])


def review_output(draft: CaseDraft, review: CaseReview | None) -> CaseReviewOutput:
    original = CaseFields.model_validate(draft, from_attributes=True)
    return CaseReviewOutput(
        draft_id=draft.id, revision=review.revision if review else 0,
        status=review.status if review else "pending", original=original,
        current=review.edited_case if review and review.edited_case is not None else original,
        was_edited=review.was_edited if review else False,
        rejection_reason=review.rejection_reason if review else "",
    )


@router.get("", response_model=CaseReviewsOutput)
def list_reviews(project_id: int, requirement_id: int, generation_id: UUID, session: Session = Depends(get_session)):
    require_generation(session, project_id, requirement_id, str(generation_id))
    drafts = session.scalars(select(CaseDraft).where(CaseDraft.generation_id == str(generation_id)).order_by(CaseDraft.sort_order)).all()
    return CaseReviewsOutput(generation_id=generation_id, items=[
        review_output(draft, session.get(CaseReview, draft.id)) for draft in drafts
    ])


@router.put("/{draft_id}", response_model=CaseReviewOutput)
def review_case(project_id: int, requirement_id: int, generation_id: UUID, draft_id: UUID,
                payload: CaseReviewInput, session: Session = Depends(get_session)):
    require_generation(session, project_id, requirement_id, str(generation_id))
    locked = session.execute(update(CaseDraft).where(
        CaseDraft.id == str(draft_id), CaseDraft.generation_id == str(generation_id),
    ).values(sort_order=CaseDraft.sort_order).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        raise HTTPException(status_code=404, detail="该生成记录下的草稿不存在。")
    session.expire_all()
    requirement, generation = require_generation(session, project_id, requirement_id, str(generation_id))
    draft = session.get(CaseDraft, str(draft_id))
    review = session.get(CaseReview, str(draft_id))
    target = {"accept": "accepted", "reject": "rejected"}.get(payload.action)
    if review and review.status != "pending":
        if target == review.status and (target != "rejected" or review.rejection_reason == payload.rejection_reason):
            return review_output(draft, review)  # 已完成操作的安全重放，不创建第二份用例。
        raise HTTPException(status_code=409, detail="该草稿已完成评审，不能继续编辑或更改评审结论。")
    if (review.revision if review else 0) != payload.expected_revision:
        raise HTTPException(status_code=409, detail="用例已在其他页面修改，请重新加载后评审。当前输入仍保留。")
    if payload.action == "accept" and summary(session, requirement, generation).outdated:
        raise HTTPException(status_code=409, detail="草稿依据已变化，请核对并重新生成后再采纳。")
    if review is None:
        review = CaseReview(draft_id=draft.id, revision=0, status="pending", was_edited=False, rejection_reason="")
        session.add(review)
    review.revision += 1
    review.updated_at = utc_now()
    if payload.action == "edit":
        fields = payload.fields.model_dump()
        original = CaseFields.model_validate(draft, from_attributes=True).model_dump()
        review.edited_case = fields
        review.was_edited = review.was_edited or fields != original
    else:
        review.status = target
        review.rejection_reason = payload.rejection_reason
    session.commit()
    return review_output(draft, review)


@library_router.get("", response_model=list[AcceptedCaseOutput])
def list_accepted(project_id: int, session: Session = Depends(get_session)):
    require_project(session, project_id)
    rows = session.execute(select(CaseDraft, CaseReview, CaseGeneration, Requirement)
        .join(CaseReview, CaseReview.draft_id == CaseDraft.id)
        .join(CaseGeneration, CaseGeneration.id == CaseDraft.generation_id)
        .join(Requirement, Requirement.id == CaseGeneration.requirement_id)
        .where(Requirement.project_id == project_id, CaseReview.status == "accepted")
        .order_by(CaseReview.updated_at.desc(), CaseDraft.id.desc())).all()
    return [AcceptedCaseOutput(
        **review_output(draft, review).current.model_dump(), id=draft.id, generation_id=generation.id,
        requirement_id=requirement.id, requirement_title=generation.requirement_snapshot["title"],
        module=generation.feature_snapshot["module"], feature_name=generation.feature_name,
        source=generation.source, outdated=summary(session, requirement, generation).outdated,
        was_edited=review.was_edited,
    ) for draft, review, generation, requirement in rows]
