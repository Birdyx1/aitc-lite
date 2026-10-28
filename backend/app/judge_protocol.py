import json
import re
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PROMPT_VERSION = 'case-judge-v1'
RUBRIC_VERSION = 'judge-rubric-v1-threshold3'
RULES_VERSION = 'case-rules-v1'
PROMPT = (Path(__file__).parent / 'prompts' / 'judge.md').read_text(encoding='utf-8')
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Dimension(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    score: int = Field(ge=0, le=5, strict=True)
    reason: str = Field(min_length=1, max_length=600)


class JudgeItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: UUID
    relevance: Dimension
    executability: Dimension
    verifiability: Dimension
    hallucinations: list[Text] = Field(max_length=5)
    suggestions: list[Text] = Field(max_length=5)


class ParsedJudge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[JudgeItem] = Field(min_length=1, max_length=8)


def parse_report(raw, case_ids):
    try:
        if not isinstance(raw, str) or len(raw) > 100000:
            raise ValueError('reply size')
        fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', raw.strip(), re.DOTALL | re.IGNORECASE)
        parsed = ParsedJudge.model_validate_json(fenced.group(1) if fenced else raw)
        by_id = {str(item.case_id): item.model_dump(mode='json') for item in parsed.items}
        if len(by_id) != len(parsed.items) or set(by_id) != set(case_ids):
            raise ValueError('case identity mismatch')
        items = [by_id[id] for id in case_ids]
        text = json.dumps(items, ensure_ascii=False)
        text.encode('utf-8')
        def invalid_text(value):
            if isinstance(value, str):
                return '\x00' in value
            if isinstance(value, list):
                return any(invalid_text(item) for item in value)
            if isinstance(value, dict):
                return any(invalid_text(item) for item in value.values())
            return False
        if invalid_text(items) or any(not item[key]['reason'].strip() for item in items
                                  for key in ('relevance', 'executability', 'verifiability')):
            raise ValueError('invalid text')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HTTPException(502, 'Judge 输出结构校验失败：需完整唯一的用例 ID、0–5 整数评分、理由和有效文本。') from None
    for item in items:
        scores = [item[key]['score'] for key in ('relevance', 'executability', 'verifiability')]
        item['average_score'] = round(sum(scores) / 3, 2)
        item['usable'] = min(scores) >= 3 and not item['hallucinations']
    return {'items': items, 'checked_count': len(items), 'usable_count': sum(item['usable'] for item in items),
            'suspect_count': sum(bool(item['hallucinations']) for item in items),
            'average_score': round(sum(item[key]['score'] for item in items for key in ('relevance', 'executability', 'verifiability'))/(3*len(items)), 2),
            'threshold': 3, 'rubric_version': RUBRIC_VERSION}


def mock_report(snapshot):
    return json.dumps({'items': [{'case_id': case['case_id'],
        **{key: {'score': 3, 'reason': 'Mock 固定示例分数，仅验证结构与展示，不代表质量判断。'}
           for key in ('relevance', 'executability', 'verifiability')},
        'hallucinations': [], 'suggestions': ['Mock 示例：请人工核对需求、步骤、预期结果及引用依据。']}
        for case in snapshot['cases']]}, ensure_ascii=False)
