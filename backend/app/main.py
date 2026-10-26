from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .database import Base, create_database
from .projects import router
from .requirements import router as requirements_router
from .settings import router as settings_router
from .features import router as features_router
from .case_generations import router as case_generations_router
from .reviews import router as reviews_router, library_router

DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "aitc-lite.sqlite3"


def create_app(database_path: Path = DEFAULT_DATABASE, *, llm_transport=None) -> FastAPI:
    engine, session_factory = create_database(database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        yield
        engine.dispose()

    app = FastAPI(title="AITC Lite", version="0.6.0", lifespan=lifespan)
    app.state.session_factory = session_factory
    # 测试可注入 httpx.MockTransport；真实应用默认使用正常网络传输。
    app.state.llm_transport = llm_transport
    app.include_router(router)
    app.include_router(requirements_router)
    app.include_router(settings_router)
    app.include_router(features_router)
    app.include_router(case_generations_router)
    app.include_router(reviews_router)
    app.include_router(library_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.rstrip("/") == "/api/settings/generation":
            # 默认 422 包含原始 input，配置请求可能含 API Key，故不回显。
            return JSONResponse(status_code=422, content={"detail": "模型配置无效，请检查根地址、模型名称、Key 格式及超时范围（5–60 秒）。"})
        return await request_validation_exception_handler(request, exc)

    @app.get("/api/health", tags=["状态"])
    def health():
        return {"status": "ok", "module": "case-review", "version": "0.6.0"}

    return app


app = create_app()

