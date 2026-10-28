from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .deps import get_session
from .embeddings import EmbeddingConfig, embed_texts
from .models import EmbeddingSettings
from .schemas import EmbeddingSettingsInput, EmbeddingSettingsOutput, EmbeddingTestOutput

router = APIRouter(prefix="/api/settings/embedding", tags=["Embedding 配置"])
PROBE_TEXTS = ["验证码过期后需重新获取。", "登录验证码失效，请重新申请。"]


def public_settings(settings):
    return EmbeddingSettingsOutput(
        base_url=settings.base_url if settings else "", model=settings.model if settings else "",
        has_api_key=bool(settings and settings.api_key), configured=settings is not None,
        mock_mode=settings.mock_mode if settings else False, timeout_seconds=settings.timeout_seconds if settings else 20,
        expected_dimensions=settings.expected_dimensions if settings else None,
    )


@router.get("", response_model=EmbeddingSettingsOutput)
def get_settings(session: Session = Depends(get_session)):
    return public_settings(session.get(EmbeddingSettings, 1))


@router.put("", response_model=EmbeddingSettingsOutput)
def save_settings(payload: EmbeddingSettingsInput, session: Session = Depends(get_session)):
    settings = session.get(EmbeddingSettings, 1)
    key = payload.api_key.get_secret_value() if payload.api_key else ""
    if settings and settings.api_key and settings.base_url != payload.base_url and not key and not payload.clear_api_key:
        raise HTTPException(status_code=400, detail="修改 Embedding 根地址时，请同时替换或清除原 Key。")
    if settings is None:
        settings = EmbeddingSettings(id=1, api_key="")
        session.add(settings)
    for name in ("base_url", "model", "mock_mode", "timeout_seconds", "expected_dimensions"):
        setattr(settings, name, getattr(payload, name))
    if payload.clear_api_key:
        settings.api_key = ""
    elif key:
        settings.api_key = key
    session.commit()
    return public_settings(settings)


def load_embedding_config(session):
    settings = session.get(EmbeddingSettings, 1)
    if not settings:
        raise HTTPException(status_code=400, detail="请先保存独立的 Embedding 配置。")
    return EmbeddingConfig(settings.base_url, settings.model, settings.api_key, settings.mock_mode,
                           settings.timeout_seconds, settings.expected_dimensions)


@router.post("/test", response_model=EmbeddingTestOutput)
async def test_settings(request: Request, session: Session = Depends(get_session)):
    config = load_embedding_config(session)
    session.rollback()
    start = perf_counter()
    vectors = await embed_texts(config, PROBE_TEXTS, request.app.state.embedding_transport)
    return EmbeddingTestOutput(
        mode="mock" if config.mock_mode else "real", dimensions=len(vectors[0]), vector_count=len(vectors),
        elapsed_ms=round((perf_counter() - start) * 1000),
        message="Mock 向量流程通过，未联网；哈希示例不具备语义检索能力。" if config.mock_mode else "Embedding 连接与向量格式验证通过；不代表语义检索质量已验证。",
    )
