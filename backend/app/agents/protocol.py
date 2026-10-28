"""原生 function tools 对话适配，不以 JSON 文本冒充工具调用。"""
import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

MAX_MODEL_CALLS = 4
MAX_TOOL_CALLS = 6
MAX_RESULT_CHARS = 12000
TOOL_TIMEOUT = 45
TURN_TIMEOUT = 180


def parse_message(message):
    try:
        if not isinstance(message, dict):
            raise ValueError('message')
        content = message.get('content')
        if content is not None and (not isinstance(content, str) or len(content) > 8000 or '\x00' in content):
            raise ValueError('content')
        if content:
            content.encode('utf-8')
        calls = message.get('tool_calls') or []
        if not isinstance(calls, list) or len(calls) > 3:
            raise ValueError('tool calls')
        clean, seen = [], set()
        for call in calls:
            id, fn = call['id'], call['function']
            if call.get('type') != 'function' or not isinstance(id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', id) or id in seen:
                raise ValueError('call id')
            name, args = fn['name'], fn['arguments']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
                raise ValueError('tool name')
            if not isinstance(args, str) or len(args) > 8000:
                raise ValueError('arguments')
            seen.add(id)
            clean.append({'id': id, 'type': 'function', 'function': {'name': name, 'arguments': args}})
        if not clean and (not content or not content.strip()):
            raise ValueError('empty answer')
        return {'role': 'assistant', 'content': content, **({'tool_calls': clean} if clean else {})}
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(502, 'Agent 工具调用响应无效：需有效的调用 ID、函数名、参数字符串或非空答复。') from None


async def agent_completion(config, messages, schemas, choice, transport=None):
    payload = {'model': config.model, 'messages': messages, 'tools': schemas, 'tool_choice': choice,
               'max_tokens': 2048, 'stream': False}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 512*1024:
        raise HTTPException(400, '本轮上下文过大，请新建对话或缩小查询。')
    if urlsplit(config.base_url).hostname == 'api.deepseek.com':
        payload['thinking'] = {'type': 'disabled'}
    headers = {'Authorization': f'Bearer {config.api_key}'} if config.api_key else {}
    try:
        async with asyncio.timeout(config.timeout_seconds):
            async with httpx.AsyncClient(transport=transport, timeout=config.timeout_seconds,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream('POST', f'{config.base_url}/chat/completions', headers=headers, json=payload) as response:
                    if response.status_code in (400, 422):
                        raise HTTPException(502, '模型服务拒绝 Agent 工具协议，请确认模型支持原生 Tool Calls 及当前请求格式。')
                    if response.status_code in (401, 403):
                        raise HTTPException(502, 'Agent 模型拒绝访问，请检查生成模型的 Key 与权限。')
                    if response.status_code == 429:
                        raise HTTPException(502, 'Agent 模型限流或额度不足，请检查账号；未自动重试。')
                    if response.status_code != 200:
                        raise HTTPException(502, f'Agent 模型返回 HTTP {response.status_code}，请检查生成模型配置。')
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        if len(raw)+len(part) > 1024*1024:
                            raise HTTPException(502, 'Agent 响应超过 1 MiB 上限。')
                        raw.extend(part)
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, 'Agent 模型调用超时；未自动重试，重新提问可能重复计费。') from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise HTTPException(502, '无法连接 Agent 模型，请检查生成模型地址与网络。') from None
    try:
        first = json.loads(raw)['choices'][0]
        if first.get('finish_reason') not in ('stop', 'tool_calls'):
            raise ValueError('unfinished')
        message = parse_message(first['message'])
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise HTTPException(502, 'Agent 返回格式无效或输出未完成，未执行本次工具请求。') from None
    if choice == 'required' and not message.get('tool_calls'):
        raise HTTPException(502, '模型没有返回原生工具调用，请确认支持 Tool Calls；本轮未执行工具。')
    return message


def mock_completion(messages):
    if messages[-1]['role'] == 'tool':
        content = json.loads(messages[-1]['content'])
        return {'role': 'assistant', 'content': f"Mock 流程演示：依据 {content['source_id']} 的本地只读查询，请展开工具结果核对。\n" + json.dumps(content, ensure_ascii=False)[:5000]}
    question = messages[-1]['content']
    name, args = 'builtin_project_overview', {}
    if '知识' in question:
        name, args = 'builtin_search_knowledge', {'query': question[:200], 'top_k': 3, 'use_rerank': False}
    elif '评估' in question or 'Judge' in question:
        name, args = 'builtin_list_judge_reports', {'limit': 3}
    elif '用例' in question:
        name, args = 'builtin_list_cases', {'limit': 5, 'status': 'accepted' if '采纳' in question else 'all'}
    elif '需求' in question:
        name, args = 'builtin_list_requirements', {'limit': 5}
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'mock_call_1', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
