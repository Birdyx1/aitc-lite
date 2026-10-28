"""兼容文本 /rerank 协议；只按输入索引映射，不信任响应正文。"""
import asyncio
import json
import math
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException

MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RerankConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    mock_mode: bool
    timeout_seconds: int


def identity(config):
    return (config.base_url, config.model, config.mock_mode) if config else None


def parse_results(body, count):
    try:
        rows = body['results']
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError('incomplete results')
        scores = {}
        for row in rows:
            index, raw = row['index'], row['relevance_score']
            if type(index) is not int or not 0 <= index < count or index in scores:
                raise ValueError('invalid index')
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                raise ValueError('invalid score')
            scores[index] = float(raw)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise HTTPException(502, 'Rerank 返回格式无效：需要全部候选的唯一有效索引和有限数值分数。') from None


async def rerank_texts(config, query, documents, transport=None):
    if (not isinstance(query, str) or not query.strip() or not isinstance(documents, list)
            or not 1 <= len(documents) <= 50 or any(not isinstance(doc, str) or not doc.strip() for doc in documents)):
        raise ValueError('需要查询与 1–50 条非空候选正文')
    if config.mock_mode:
        # 字符重合仅演示重新排序，不是语义模型。
        terms = set(query.casefold())
        return sorted([(i, len(terms & set(doc.casefold())) / max(1, len(terms)))
                       for i, doc in enumerate(documents)], key=lambda item: (-item[1], item[0]))
    headers = {'Authorization': f'Bearer {config.api_key}'} if config.api_key else {}
    try:
        async with asyncio.timeout(config.timeout_seconds):
            async with httpx.AsyncClient(transport=transport, timeout=config.timeout_seconds,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream('POST', f'{config.base_url}/rerank', headers=headers,
                                         json={'model': config.model, 'query': query, 'documents': documents,
                                               'top_n': len(documents), 'return_documents': False}) as response:
                    if response.status_code in (401, 403):
                        raise HTTPException(502, 'Rerank 服务拒绝访问，请检查独立 Key 和模型权限。')
                    if response.status_code == 429:
                        raise HTTPException(502, 'Rerank 服务限流或额度不足，请检查账号后重试。')
                    if response.status_code != 200:
                        raise HTTPException(502, f'Rerank 服务返回 HTTP {response.status_code}，请检查地址和模型。')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise HTTPException(502, 'Rerank 响应超过大小上限。')
                        data.extend(chunk)
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, 'Rerank 调用超时，请检查网络或增加超时。') from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise HTTPException(502, '无法连接 Rerank 服务，请检查根地址和网络。') from None
    try:
        body = json.loads(data)
    except (ValueError, RecursionError):
        raise HTTPException(502, 'Rerank 返回 JSON 无效，请检查接口兼容性。') from None
    return parse_results(body, len(documents))
