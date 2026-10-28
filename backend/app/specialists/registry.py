"""显式注册两项专项；内容摘要同时覆盖 Prompt、元信息与 handler 实现。"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .handlers import api_context, security_context

ROOT = Path(__file__).parent
HANDLERS = {'api': api_context, 'security': security_context}
_HANDLER_BYTES = (ROOT / 'handlers.py').read_bytes()


def _definition(id, name, description, focus):
    prompt = (ROOT / 'prompts' / f'{id}.md').read_text(encoding='utf-8')
    item = {'id': id, 'name': name, 'version': f'{id}-v1', 'handler_version': f'{id}-handler-v1',
            'description': description, 'focus': focus, 'prompt': prompt}
    content = json.dumps(item, sort_keys=True, ensure_ascii=False).encode() + _HANDLER_BYTES
    return {**item, 'content_hash': hashlib.sha256(content).hexdigest()}


_REGISTRY = {
    'api': _definition('api', '接口测试', '针对当前功能明确的接口契约；缺少约定时写待澄清。',
                       ['请求与响应契约', '参数边界', '错误语义', '条件适用的幂等/分页']),
    'security': _definition('security', '安全与权限', '针对当前功能明确的认证、授权和数据保护规则。',
                            ['认证状态', '角色与资源归属', '拒绝后的数据不变性', '敏感字段保护']),
}
REGISTRY_VERSION = hashlib.sha256(Path(__file__).read_bytes() + json.dumps(_REGISTRY, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


class SkillCatalogItem(BaseModel):
    id: str
    name: str
    version: str
    handler_version: str
    description: str
    focus: list[str]
    content_hash: str


router = APIRouter(prefix='/api/skills', tags=['专项 Skill'])


@router.get('', response_model=list[SkillCatalogItem])
def catalog():
    return [SkillCatalogItem(**definition) for definition in _REGISTRY.values()]


def snapshots(ids):
    if any(id not in _REGISTRY for id in ids):
        raise HTTPException(422, '不支持的专项 Skill，请使用内置专项。')
    return [deepcopy(_REGISTRY[id]) for id in sorted(ids)]


def assert_supported(items):
    for item in items:
        current = _REGISTRY.get(item['id'])
        if not current or current['content_hash'] != item['content_hash']:
            raise HTTPException(409, '专项 Skill 版本已变化，请重新发起生成。')


def build_contexts(items, feature, strategy):
    assert_supported(items)
    return [{'id': item['id'], 'name': item['name'], 'version': item['version'],
             **HANDLERS[item['id']](feature, strategy)} for item in items]
