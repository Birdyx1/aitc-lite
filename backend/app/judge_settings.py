from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.orm import Session

from .deps import get_session
from .llm import ModelConfig, test_connection
from .models import JudgeSettings
from .schemas import EmbeddingSettingsInput, ConnectionTestOutput
from .settings import load_model_config

router = APIRouter(prefix='/api/settings/judge', tags=['Judge 配置'])


class JudgeSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    use_generation_model: bool = Field(default=True, strict=True)
    base_url: str = Field(default='', max_length=1000)
    model: str = Field(default='', max_length=200)
    api_key: SecretStr | None = None
    clear_api_key: bool = Field(default=False, strict=True)
    mock_mode: bool = Field(default=False, strict=True)
    timeout_seconds: int = Field(default=20, ge=5, le=60, strict=True)

    @field_validator('base_url')
    @classmethod
    def valid_root(cls, value):
        value = EmbeddingSettingsInput.valid_root(value)
        if value.rstrip('/').endswith('/rerank'):
            raise ValueError('填写对话根地址')
        return EmbeddingSettingsInput.valid_plain_text(value)

    @field_validator('model')
    @classmethod
    def valid_model(cls, value):
        return EmbeddingSettingsInput.valid_plain_text(value)

    @field_validator('api_key')
    @classmethod
    def valid_key(cls, value):
        return EmbeddingSettingsInput.valid_key(value)

    @model_validator(mode='after')
    def valid_config(self):
        if not self.use_generation_model and not self.mock_mode and (not self.base_url or not self.model):
            raise ValueError('独立真实 Judge 需要地址和模型')
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value():
            raise ValueError('不能同时替换和清除 Key')
        return self


class JudgeSettingsOutput(BaseModel):
    configured: bool
    use_generation_model: bool
    base_url: str
    model: str
    has_api_key: bool
    mock_mode: bool
    timeout_seconds: int
    effective_source: str
    effective_model: str
    effective_mock_mode: bool | None
    effective_timeout_seconds: int | None


def resolve_judge_config(session):
    settings = session.get(JudgeSettings, 1)
    if settings is None or settings.use_generation_model:
        return load_model_config(session), 'generation'
    return ModelConfig(settings.base_url, settings.model, settings.api_key,
                       settings.mock_mode, settings.timeout_seconds), 'independent'


def selected_transport(app, source):
    return app.state.llm_transport if source == 'generation' else app.state.judge_transport


def public_settings(session):
    row = session.get(JudgeSettings, 1)
    try:
        config, source = resolve_judge_config(session)
    except HTTPException:
        config, source = None, 'unconfigured'
    return JudgeSettingsOutput(configured=row is not None,
        use_generation_model=row.use_generation_model if row else True,
        base_url=row.base_url if row else '', model=row.model if row else '', has_api_key=bool(row and row.api_key),
        mock_mode=row.mock_mode if row else False, timeout_seconds=row.timeout_seconds if row else 20,
        effective_source=source, effective_model=config.model if config else '',
        effective_mock_mode=config.mock_mode if config else None,
        effective_timeout_seconds=config.timeout_seconds if config else None)


@router.get('', response_model=JudgeSettingsOutput)
def get_settings(session: Session = Depends(get_session)):
    return public_settings(session)


@router.put('', response_model=JudgeSettingsOutput)
def save_settings(payload: JudgeSettingsInput, session: Session = Depends(get_session)):
    row = session.get(JudgeSettings, 1)
    key = payload.api_key.get_secret_value() if payload.api_key else ''
    if row and row.api_key and row.base_url != payload.base_url and not key and not payload.clear_api_key:
        raise HTTPException(400, '修改独立 Judge 根地址时，请同时替换或清除原 Key。')
    if row is None:
        row = JudgeSettings(id=1, api_key='')
        session.add(row)
    for name in ('use_generation_model', 'base_url', 'model', 'mock_mode', 'timeout_seconds'):
        setattr(row, name, getattr(payload, name))
    if payload.clear_api_key:
        row.api_key = ''
    elif key:
        row.api_key = key
    session.commit()
    return public_settings(session)


@router.post('/test', response_model=ConnectionTestOutput)
async def test_settings(request: Request, session: Session = Depends(get_session)):
    config, source = resolve_judge_config(session)
    session.rollback()
    result = await test_connection(config, selected_transport(request.app, source))
    result.message = ('复用生成模型：' if source == 'generation' else '独立 Judge：') + result.message
    return result
