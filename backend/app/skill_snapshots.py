from copy import deepcopy

from fastapi import HTTPException

from .models import GenerationSkills
from .specialists.registry import snapshots, assert_supported


def bind_skills(session, payload, requirement_id, fingerprint):
    previous = session.get(GenerationSkills, str(payload.request_id))
    if previous:
        if previous.requirement_id != requirement_id or previous.request_fingerprint != fingerprint:
            raise HTTPException(409, '生成请求标识已关联其他专项快照，请重新发起。')
        assert_supported(previous.snapshot)
        return deepcopy(previous.snapshot)
    if not payload.skill_ids:
        return []
    items = snapshots(payload.skill_ids)
    session.add(GenerationSkills(id=str(payload.request_id), requirement_id=requirement_id,
                                request_fingerprint=fingerprint, snapshot=items, case_tags={}))
    session.flush()
    return deepcopy(items)


def skill_evidence(context):
    return {'items': deepcopy(context.snapshot), 'case_tags': deepcopy(context.case_tags)} if context else None
