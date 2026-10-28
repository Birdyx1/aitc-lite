"""独立 Embedding 适配器：验证批量 float 向量，不接触生成模型配置。"""

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException

MAX_DIMENSIONS = 32768
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    mock_mode: bool
    timeout_seconds: int
    expected_dimensions: int | None


def normalize_vector(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_DIMENSIONS:
        raise ValueError("invalid dimensions")
    vector = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite number")
        vector.append(number)
    scale = max(abs(number) for number in vector)
    if not scale:
        raise ValueError("zero vector")
    scaled = [number / scale for number in vector]
    norm = math.sqrt(math.fsum(number * number for number in scaled))
    return [number / norm for number in scaled]


def parse_vectors(body, count, expected_dimensions):
    try:
        entries = body["data"]
        if not isinstance(entries, list) or len(entries) != count:
            raise ValueError("invalid batch size")
        ordered = {}
        for entry in entries:
            index = entry["index"]
            if type(index) is not int or not 0 <= index < count or index in ordered:
                raise ValueError("invalid index")
            ordered[index] = normalize_vector(entry["embedding"])
        vectors = [ordered[index] for index in range(count)]
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise ValueError("inconsistent dimensions")
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        raise HTTPException(status_code=502, detail="Embedding 返回格式无效：需要数量与索引对应、维度一致、非零且数值有限的 float 向量。") from None
    if expected_dimensions is not None and dimensions != expected_dimensions:
        raise HTTPException(status_code=502, detail="Embedding 返回维度与预期维度不一致，请核对模型或修改预期维度。")
    return vectors


async def embed_texts(config: EmbeddingConfig, texts: list[str], transport=None) -> list[list[float]]:
    if not isinstance(texts, list) or not texts or len(texts) > 32 or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("需要 1–32 条非空文本")
    if config.mock_mode:
        dimensions = config.expected_dimensions or 8
        # 哈希示例只用于流程测试，不具备语义向量能力。
        return [normalize_vector([hashlib.sha256(text.encode()).digest()[i % 32] - 127.5
                                  for i in range(dimensions)]) for text in texts]
    if not config.base_url or not config.model:
        raise HTTPException(status_code=400, detail="请先保存 Embedding 配置。")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        async with asyncio.timeout(config.timeout_seconds):
            async with httpx.AsyncClient(transport=transport, timeout=config.timeout_seconds,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", f"{config.base_url}/embeddings", headers=headers,
                                         json={"model": config.model, "input": texts, "encoding_format": "float"}) as response:
                    if response.status_code in (401, 403):
                        raise HTTPException(status_code=502, detail="Embedding 服务拒绝访问，请检查独立的 API Key 和模型权限。")
                    if response.status_code == 429:
                        raise HTTPException(status_code=502, detail="Embedding 服务限流或额度不足，请检查服务账号后重试。")
                    if response.status_code != 200:
                        raise HTTPException(status_code=502, detail=f"Embedding 服务返回 HTTP {response.status_code}，请检查地址、模型和接口兼容性。")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise HTTPException(status_code=502, detail="Embedding 响应超过大小上限，请检查模型及向量维度。")
                        data.extend(chunk)
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Embedding 连接超时，请检查地址、网络或增加超时。") from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise HTTPException(status_code=502, detail="无法连接 Embedding 服务，请检查根地址和网络。") from None
    try:
        body = json.loads(data)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=502, detail="Embedding 返回的 JSON 无效，请检查接口兼容性。") from None
    return parse_vectors(body, len(texts), config.expected_dimensions)
