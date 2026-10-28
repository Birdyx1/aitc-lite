from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .deps import get_session
from .models import RerankSettings
from .reranking import RerankConfig, rerank_texts
from .schemas import GenerationSettingsOutput, RerankSettingsInput, RerankTestOutput

router = APIRouter(prefix='/api/settings/rerank', tags=['Rerank 配置'])
PROBE_QUERY = '验证码过期怎么办？'
PROBE_DOCUMENTS = ['退款原路退回支付账户。', '验证码过期后需要重新获取。']


def public_settings(settings):
    return GenerationSettingsOutput(base_url=settings.base_url if settings else '', model=settings.model if settings else '',
                                    has_api_key=bool(settings and settings.api_key), configured=settings is not None,
                                    mock_mode=settings.mock_mode if settings else False,
                                    timeout_seconds=settings.timeout_seconds if settings else 20)


def load_rerank_config(session):
    settings = session.get(RerankSettings, 1)
    return RerankConfig(settings.base_url, settings.model, settings.api_key, settings.mock_mode,
                        settings.timeout_seconds) if settings else None


@router.get('', response_model=GenerationSettingsOutput)
def get_settings(session: Session = Depends(get_session)):
    return public_settings(session.get(RerankSettings, 1))


@router.put('', response_model=GenerationSettingsOutput)
def save_settings(payload: RerankSettingsInput, session: Session = Depends(get_session)):
    settings = session.get(RerankSettings, 1)
    key = payload.api_key.get_secret_value() if payload.api_key else ''
    if settings and settings.api_key and settings.base_url != payload.base_url and not key and not payload.clear_api_key:
        raise HTTPException(400, '修改 Rerank 根地址时，请同时替换或清除原 Key。')
    if settings is None:
        settings = RerankSettings(id=1, api_key='')
        session.add(settings)
    for name in ('base_url', 'model', 'mock_mode', 'timeout_seconds'):
        setattr(settings, name, getattr(payload, name))
    if payload.clear_api_key:
        settings.api_key = ''
    elif key:
        settings.api_key = key
    session.commit()
    return public_settings(settings)


@router.post('/test', response_model=RerankTestOutput)
async def test_settings(request: Request, session: Session = Depends(get_session)):
    config = load_rerank_config(session)
    if config is None:
        raise HTTPException(400, '请先保存独立的 Rerank 配置。')
    session.rollback()
    start = perf_counter()
    results = await rerank_texts(config, PROBE_QUERY, PROBE_DOCUMENTS, request.app.state.rerank_transport)
    return RerankTestOutput(mode='mock' if config.mock_mode else 'real', document_count=2,
                            order=[index + 1 for index, _ in results], scores=[score for _, score in results],
                            elapsed_ms=round((perf_counter()-start)*1000),
                            message='Mock 字符重合排序通过，不代表语义精排质量。' if config.mock_mode else
                            'Rerank 连接与结果格式验证通过；请用项目知识验收精排质量。')
