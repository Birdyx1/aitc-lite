from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .deps import get_session
from .llm import ModelConfig, test_connection
from .models import GenerationSettings
from .schemas import ConnectionTestOutput, GenerationSettingsInput, GenerationSettingsOutput

router = APIRouter(prefix="/api/settings/generation", tags=["生成模型配置"])


def public_settings(settings: GenerationSettings | None) -> GenerationSettingsOutput:
    return GenerationSettingsOutput(
        base_url=settings.base_url if settings else "",
        model=settings.model if settings else "",
        has_api_key=bool(settings and settings.api_key),
        mock_mode=settings.mock_mode if settings else False,
        timeout_seconds=settings.timeout_seconds if settings else 20,
        configured=settings is not None,
    )


@router.get("", response_model=GenerationSettingsOutput)
def get_settings(session: Session = Depends(get_session)):
    return public_settings(session.get(GenerationSettings, 1))


@router.put("", response_model=GenerationSettingsOutput)
def save_settings(payload: GenerationSettingsInput, session: Session = Depends(get_session)):
    settings = session.get(GenerationSettings, 1)
    new_key = payload.api_key.get_secret_value() if payload.api_key else ""
    if (settings and settings.api_key and payload.base_url != settings.base_url
            and not new_key and not payload.clear_api_key):
        raise HTTPException(status_code=400, detail="修改 API 根地址时，请同时替换或勾选清除已保存的 API Key。")
    if settings is None:
        settings = GenerationSettings(id=1, api_key="")
        session.add(settings)
    settings.base_url = payload.base_url
    settings.model = payload.model
    settings.mock_mode = payload.mock_mode
    settings.timeout_seconds = payload.timeout_seconds
    if payload.clear_api_key:
        settings.api_key = ""
    elif new_key:
        settings.api_key = new_key
    session.commit()
    return public_settings(settings)


@router.post("/test", response_model=ConnectionTestOutput)
async def test_settings(request: Request, session: Session = Depends(get_session)):
    settings = session.get(GenerationSettings, 1)
    if settings is None:
        raise HTTPException(status_code=400, detail="请先保存生成模型配置。")
    config = ModelConfig(
        base_url=settings.base_url, model=settings.model, api_key=settings.api_key,
        mock_mode=settings.mock_mode, timeout_seconds=settings.timeout_seconds,
    )
    return await test_connection(config, transport=request.app.state.llm_transport)
