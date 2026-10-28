import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .database import Base, create_database
from .projects import router
from .requirements import router as requirements_router
from .settings import router as settings_router
from .embedding_settings import router as embedding_settings_router
from .features import router as features_router
from .case_generations import router as case_generations_router
from .reviews import router as reviews_router, library_router
from .testcase_export import router as export_router
from .knowledge_documents import router as knowledge_router
from .knowledge_search import router as knowledge_search_router
from .checkpoints import TaskCheckpointSaver
from .generation_tasks import router as tasks_router, task_worker, settle_unfinished
from .vector_search import router as vector_router, vector_worker, settle_vector_builds
from .knowledge.vector_store import VectorStore
from .hybrid_search import router as hybrid_router
from .rerank_settings import router as rerank_settings_router
from .specialists.registry import router as skills_router
from .judge_settings import router as judge_settings_router
from .judge_runs import router as judge_runs_router, judge_worker, settle_judge_runs
from .agent_sessions import router as agent_router, agent_worker, settle_agent
from .mcp_settings import router as mcp_router
from .execution_tasks import router as execution_router
from .evaluation.api import router as evaluation_router
from .project_workbench import router as workbench_router
from .evaluation.runtime import worker as evaluation_worker, settle as settle_evaluations

DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "aitc-lite.sqlite3"


def create_app(database_path: Path = DEFAULT_DATABASE, *, llm_transport=None, embedding_transport=None, rerank_transport=None, judge_transport=None, agent_transport=None, mcp_discover=None, mcp_call=None) -> FastAPI:
    engine, session_factory = create_database(database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        settle_unfinished(app)
        settle_vector_builds(app)
        settle_judge_runs(app)
        settle_agent(app)
        settle_evaluations(app)
        evaluation_task = asyncio.create_task(evaluation_worker(app))
        agent_task = asyncio.create_task(agent_worker(app))
        judge_task = asyncio.create_task(judge_worker(app))
        worker = asyncio.create_task(task_worker(app))
        vector_task = asyncio.create_task(vector_worker(app))
        try:
            yield
        finally:
            evaluation_task.cancel()
            with suppress(asyncio.CancelledError):
                await evaluation_task
            settle_evaluations(app)
            agent_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_task
            settle_agent(app)
            judge_task.cancel()
            with suppress(asyncio.CancelledError):
                await judge_task
            settle_judge_runs(app)
            vector_task.cancel()
            with suppress(asyncio.CancelledError):
                await vector_task
            settle_vector_builds(app)
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            settle_unfinished(app)
            engine.dispose()

    app = FastAPI(title="AITC Lite", version="0.23.0", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.checkpointer = TaskCheckpointSaver(session_factory)
    # 测试可注入 httpx.MockTransport；真实应用默认使用正常网络传输。
    app.state.llm_transport = llm_transport
    app.state.embedding_transport = embedding_transport
    app.state.rerank_transport = rerank_transport
    app.state.judge_transport = judge_transport
    app.state.agent_transport = agent_transport
    app.state.mcp_discover = mcp_discover
    app.state.mcp_call = mcp_call
    app.state.vector_store = VectorStore(database_path.parent / f"{database_path.stem}-chroma")
    app.include_router(router)
    app.include_router(requirements_router)
    app.include_router(settings_router)
    app.include_router(embedding_settings_router)
    app.include_router(features_router)
    app.include_router(case_generations_router)
    app.include_router(reviews_router)
    app.include_router(library_router)
    app.include_router(export_router)
    app.include_router(tasks_router)
    app.include_router(knowledge_router)
    app.include_router(knowledge_search_router)
    app.include_router(vector_router)
    app.include_router(hybrid_router)
    app.include_router(rerank_settings_router)
    app.include_router(skills_router)
    app.include_router(judge_settings_router)
    app.include_router(judge_runs_router)
    app.include_router(agent_router)
    app.include_router(mcp_router)
    app.include_router(execution_router)
    app.include_router(evaluation_router)
    app.include_router(workbench_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if "/evaluations" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": "评测请求无效：检查样本正文、功能点、标准点（每项2–500字）、版本、1–20个不重复样本及策略/Mock/RAG参数。"})
        if "/execution-tasks" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": "执行请求无效：任务名称/环境必填，选择 1–200 条不重复的用例；已执行状态须填写实际结果，检查状态、长度、Unicode 与版本。"})
        if "/mcp" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": "MCP 请求无效，请检查传输、绝对路径命令/无凭据端点、参数数组、凭据和版本。"})
        if "/agent-sessions" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": "Agent 请求无效，请检查项目、对话/请求标识及非空问题（最多 2000 字符）。"})
        if request.url.path.rstrip("/") == "/api/settings/judge":
            return JSONResponse(status_code=422, content={"detail": "Judge 配置无效，请检查复用开关、HTTP/HTTPS 根地址、模型、Key 与超时（5–60 秒整数）。"})
        if "/judge-runs" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": "Judge 请求参数无效，请检查项目、生成记录及请求/重试标识。"})
        if request.url.path.rstrip("/") == "/api/settings/rerank":
            return JSONResponse(status_code=422, content={"detail": "Rerank 配置无效，请检查 HTTP/HTTPS 根地址、模型、Key 和超时（5–60 秒整数）。"})
        if request.url.path.rstrip("/") == "/api/settings/embedding":
            return JSONResponse(status_code=422, content={"detail": "Embedding 配置无效，请检查 HTTP/HTTPS 根地址、模型、Key、超时（5–60 秒）和预期维度（1–32768，可留空）。"})
        if request.url.path.rstrip("/") == "/api/settings/generation":
            # 默认 422 包含原始 input，配置请求可能含 API Key，故不回显。
            return JSONResponse(status_code=422, content={"detail": "模型配置无效，请检查根地址、模型名称、Key 格式及超时范围（5–60 秒）。"})
        if request.method == "POST" and (request.url.path.rstrip("/").endswith("/case-generations") or request.url.path.rstrip("/").endswith("/generation-tasks")):
            return JSONResponse(status_code=422, content={"detail": "生成参数无效，请检查功能点、版本、策略、专项与 RAG 参数：查询最多 200 字符、知识分块 1–5，开关需为布尔值。"})
        if "/hybrid-search" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": "混合检索参数无效：查询 1–200 字符、Top K 1–20、每路召回 1–50 且不小于 Top K；精排候选 1–50，启用时不小于 Top K；开关需为布尔值。"})
        if "/knowledge-search" in request.url.path or request.url.path.endswith("/vector-index/search"):
            return JSONResponse(status_code=422, content={"detail": "检索参数无效：请输入 1–200 个字符，Top K 为 1–20 的整数。"})
        if "/knowledge-documents" in request.url.path:
            # 非法代理字符无法再次编码为 UTF-8；不要在校验错误中回显原始文档。
            return JSONResponse(status_code=422, content={"detail": "知识文档输入无效，请检查标题（1–200 字符）、正文（1–100000 字符）、来源文件名和版本；仅支持有效 Unicode 文本。"})
        return await request_validation_exception_handler(request, exc)

    @app.get("/api/health", tags=["状态"])
    def health():
        return {"status": "ok", "module": "offline-evaluation", "version": "0.23.0"}

    return app


app = create_app()

