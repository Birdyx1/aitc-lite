"""共用文本模型调用；连接验证使用固定短消息，业务调用显式提供消息。"""

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .schemas import ConnectionTestOutput


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    mock_mode: bool
    timeout_seconds: int


async def test_connection(config: ModelConfig, transport=None) -> ConnectionTestOutput:
    if config.mock_mode:
        return ConnectionTestOutput(
            mode="mock", message="Mock 流程验证通过，本次未连接外部模型。",
            reply="AITC_OK（Mock）", elapsed_ms=0,
        )
    start = perf_counter()
    reply = await chat_completion(config, [
        {"role": "user", "content": "这是连接验证，请只回复 AITC_OK。"},
    ], max_tokens=64, transport=transport)
    return ConnectionTestOutput(
        mode="real", message="真实模型连接通过，已收到对话响应。",
        reply=reply[:200], elapsed_ms=round((perf_counter() - start) * 1000),
    )


async def chat_completion(config: ModelConfig, messages: list[dict], max_tokens: int, transport=None) -> str:
    if not config.base_url or not config.model:
        raise HTTPException(status_code=400, detail="请先保存生成模型配置，或显式开启 Mock 模式。")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    payload = {"model": config.model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    if urlsplit(config.base_url).hostname == "api.deepseek.com":
        # 连接验证和本阶段结构化拆解使用非思考模式。
        payload["thinking"] = {"type": "disabled"}
    try:
        # 同时限制整个调用和各网络阶段；禁止重定向，避免将凭据带到其他地址。
        async with asyncio.timeout(config.timeout_seconds):
            async with httpx.AsyncClient(
                transport=transport, timeout=config.timeout_seconds,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.post(
                    f"{config.base_url}/chat/completions", headers=headers,
                    json=payload,
                )
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="模型连接超时，请检查地址、网络或增加超时时间。") from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise HTTPException(status_code=502, detail="无法连接模型服务，请检查 API 根地址和本机网络。") from None

    # 不把服务端原始错误体或异常文本返回前端，避免回显凭据。
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=502, detail="模型服务拒绝访问，请检查 API Key 和模型访问权限。")
    if response.status_code == 429:
        raise HTTPException(status_code=502, detail="模型服务限流或额度不足，请检查服务账号后重试。")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"模型服务返回 HTTP {response.status_code}，请检查根地址、模型名称与服务状态。")
    try:
        body = response.json()
        choice = body["choices"][0]
        if not isinstance(choice, dict):
            raise ValueError("invalid choice")
        if choice.get("finish_reason") == "length":
            raise HTTPException(status_code=502, detail="模型输出达到长度上限，请缩短需求后重试，或拆分为多份需求。")
        reply = choice["message"]["content"]
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("empty reply")
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="模型返回格式不兼容：需要非空的 choices[0].message.content 文本。") from None

    if config.api_key:
        reply = reply.replace(config.api_key, "[已隐藏]")
    return reply
