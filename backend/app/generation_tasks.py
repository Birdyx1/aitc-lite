"""单进程本地任务队列：先提交数据库，再由应用生命周期内的工作协程执行。"""

import asyncio
import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .case_generations import request_fingerprint, require_confirmed_feature, run_generation
from .deps import get_session
from .checkpoints import TaskPaused, checkpoint_trace
from .models import GenerationSkills, GenerationKnowledge, CaseGeneration, GenerationTask, GenerationTaskControl, Requirement, utc_now
from .requirements import require_requirement
from .schemas import CaseGenerateInput, GenerationTaskOutput, TaskResumeInput
from .settings import load_model_config
from .workflows.case_generation import WORKFLOW_VERSION
from .rag import context_is_stale
from .skill_snapshots import bind_skills
from .specialists.registry import assert_supported

router = APIRouter(prefix="/api/projects/{project_id}/requirements/{requirement_id}/generation-tasks", tags=["生成任务"])
ACTIVE = ("queued", "running")


def config_signature(config):
    # 凭据只参与不可逆比较摘要，不将 Key、消息或模型响应放入任务记录。
    data = [config.base_url, config.model, config.api_key, config.mock_mode, config.timeout_seconds]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def require_task(session, project_id, requirement_id, task_id):
    require_requirement(session, project_id, requirement_id)
    task = session.get(GenerationTask, str(task_id))
    if not task or task.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail="该需求下的生成任务不存在。")
    return task


def resume_reason(session, task, control):
    if not control:
        return "此历史任务没有节点检查点支持，请重新生成。"
    if control.workflow_version != WORKFLOW_VERSION:
        return "生成流程版本已变化，请重新生成。"
    skills = session.get(GenerationSkills, task.id)
    if skills:
        try:
            assert_supported(skills.snapshot)
        except HTTPException as error:
            return str(error.detail)
    requirement = session.get(Requirement, task.requirement_id)
    if context_is_stale(session, requirement.project_id, session.get(GenerationKnowledge, task.id)):
        return "知识库已变化，请重新检索并生成。"
    try:
        require_confirmed_feature(session, requirement, CaseGenerateInput.model_validate(task.payload))
        if config_signature(load_model_config(session)) != task.settings_signature:
            return "模型配置已变化，请恢复原配置或重新生成。"
    except HTTPException as error:
        return str(error.detail)
    if session.scalar(select(GenerationTask.id).where(
            GenerationTask.requirement_id == task.requirement_id, GenerationTask.id != task.id,
            GenerationTask.status.in_(ACTIVE)).limit(1)):
        return "该需求还有其他活动任务，请等待完成。"
    return ""


def task_output(session, task):
    result = GenerationTaskOutput.model_validate(task)
    result.skill_ids = task.payload.get("skill_ids", [])
    control = session.get(GenerationTaskControl, task.id)
    context = session.get(GenerationKnowledge, task.id)
    result.requires_knowledge_retry = bool(context and context.attempt_pending and not context.snapshot)
    result.pause_requested = bool(control and control.pause_requested)
    result.checkpoint_nodes = checkpoint_trace(session, task.id)
    result.requires_model_retry = bool(control and control.model_call_pending and "generate" not in result.checkpoint_nodes)
    if task.status in ("paused", "interrupted", "failed"):
        result.resume_block_reason = resume_reason(session, task, control)
        result.can_resume = not result.resume_block_reason
    return result


@router.post("/{task_id}/pause", response_model=GenerationTaskOutput)
def pause_task(project_id: int, requirement_id: int, task_id: UUID, session: Session = Depends(get_session)):
    require_task(session, project_id, requirement_id, task_id)
    session.execute(update(Requirement).where(Requirement.id == requirement_id).values(updated_at=Requirement.updated_at))
    session.expire_all()
    task = require_task(session, project_id, requirement_id, task_id)
    control = session.get(GenerationTaskControl, task.id)
    if not control or task.status not in (*ACTIVE, "paused"):
        raise HTTPException(status_code=409, detail="当前任务不能暂停，请刷新状态。")
    control.pause_requested = True
    if task.status == "queued":
        task.status = "paused"
    task.updated_at = utc_now()
    session.commit()
    return task_output(session, task)


@router.post("/{task_id}/resume", response_model=GenerationTaskOutput)
def resume_task(project_id: int, requirement_id: int, task_id: UUID, payload: TaskResumeInput,
                session: Session = Depends(get_session)):
    require_task(session, project_id, requirement_id, task_id)
    session.execute(update(Requirement).where(Requirement.id == requirement_id).values(updated_at=Requirement.updated_at))
    session.expire_all()
    task = require_task(session, project_id, requirement_id, task_id)
    if task.status in ACTIVE or task.status == "succeeded":
        return task_output(session, task)
    control = session.get(GenerationTaskControl, task.id)
    reason = resume_reason(session, task, control)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    context = session.get(GenerationKnowledge, task.id)
    if context and context.attempt_pending and not context.snapshot and not payload.retry_uncertain_knowledge_call:
        raise HTTPException(409, "上次知识检索没有留下可复用快照，可能已计费；请确认允许重复检索后恢复。")
    if context:
        context.attempt_pending = False
    trace = checkpoint_trace(session, task.id)
    if control.model_call_pending and "generate" not in trace and not payload.retry_uncertain_model_call:
        raise HTTPException(status_code=409, detail="上次模型请求没有可复用的检查点，可能已经计费。请确认允许重新调用后恢复。")
    control.pause_requested = False
    control.model_call_pending = False
    task.status, task.error_message, task.updated_at = "queued", "", utc_now()
    session.commit()
    return task_output(session, task)


@router.get("", response_model=list[GenerationTaskOutput])
def list_tasks(project_id: int, requirement_id: int, session: Session = Depends(get_session)):
    require_requirement(session, project_id, requirement_id)
    tasks = session.scalars(select(GenerationTask).where(GenerationTask.requirement_id == requirement_id)
                            .order_by(GenerationTask.created_at.desc(), GenerationTask.id.desc())).all()
    return [task_output(session, task) for task in tasks]


@router.get("/{task_id}", response_model=GenerationTaskOutput)
def get_task(project_id: int, requirement_id: int, task_id: UUID, session: Session = Depends(get_session)):
    return task_output(session, require_task(session, project_id, requirement_id, task_id))


@router.post("", response_model=GenerationTaskOutput, status_code=202)
def submit_task(project_id: int, requirement_id: int, payload: CaseGenerateInput, response: Response,
                session: Session = Depends(get_session)):
    require_requirement(session, project_id, requirement_id)
    # SQLite 写锁将检查与插入串行化，多个页面同时提交也只能创建一个活动任务。
    session.execute(update(Requirement).where(Requirement.id == requirement_id).values(
        updated_at=Requirement.updated_at).execution_options(synchronize_session=False))
    existing = session.get(GenerationTask, str(payload.request_id))
    fingerprint = request_fingerprint(payload)
    context = session.get(GenerationKnowledge, str(payload.request_id))
    if context and (context.requirement_id != requirement_id or context.request_fingerprint != fingerprint):
        raise HTTPException(409, "任务请求标识已关联其他知识快照，请重新发起。")
    if existing:
        if existing.requirement_id != requirement_id or existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="任务请求标识已被使用，请重新发起。")
        response.status_code = 200
        return task_output(session, existing)
    previous = session.get(CaseGeneration, str(payload.request_id))
    if previous and (previous.requirement_id != requirement_id or previous.request_fingerprint != fingerprint):
        raise HTTPException(status_code=409, detail="生成请求标识已被使用，请重新发起。")
    if session.scalar(select(GenerationTask.id).where(
            GenerationTask.requirement_id == requirement_id, GenerationTask.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(status_code=409, detail="该需求已有排队或运行中的任务，请等待完成后再生成。")
    requirement = require_requirement(session, project_id, requirement_id)
    feature = require_confirmed_feature(session, requirement, payload)
    if not previous:
        bind_skills(session, payload, requirement_id, fingerprint)
    config = load_model_config(session)
    if not config.mock_mode and (not config.base_url or not config.model):
        raise HTTPException(status_code=400, detail="请先保存生成模型配置，或显式开启 Mock 模式。")
    task = GenerationTask(
        id=str(payload.request_id), requirement_id=requirement_id, request_fingerprint=fingerprint,
        payload=payload.model_dump(mode="json"), settings_signature=config_signature(config),
        feature_name=feature.feature, strategy=payload.strategy,
        source=previous.source if previous else ("mock" if config.mock_mode else "real"),
        model=previous.model if previous else ("" if config.mock_mode else config.model),
        status="succeeded" if previous else "queued", stage="completed" if previous else "queued",
        progress=100 if previous else 0, completed_nodes=previous.graph_nodes if previous else [],
    )
    session.add(task)
    session.flush()
    session.add(GenerationTaskControl(task_id=task.id, workflow_version=WORKFLOW_VERSION))
    session.commit()
    session.refresh(task)
    return task_output(session, task)


def settle_unfinished(app):
    """启动/退出时核对结果，绝不自动重发可能已经计费的模型请求。仅支持一个应用进程。"""
    with app.state.session_factory() as session:
        for task in session.scalars(select(GenerationTask).where(GenerationTask.status.in_(ACTIVE))).all():
            generation = session.get(CaseGeneration, task.id)
            if generation and generation.requirement_id == task.requirement_id and generation.request_fingerprint == task.request_fingerprint:
                task.status, task.stage, task.progress = "succeeded", "completed", 100
                task.completed_nodes = generation.graph_nodes
                task.error_message = ""
            else:
                task.status = "interrupted"
                task.error_message = "服务停止或重启导致任务中断；可核对检查点后手动恢复，本次未自动重试。"
            trace = checkpoint_trace(session, task.id)
            if task.status != "succeeded" and trace:
                task.completed_nodes = trace
                task.stage, task.progress = {1: ("generate", 25), 2: ("validate", 70), 3: ("save", 90)}[len(trace)]
            task.updated_at = utc_now()
        session.commit()


def update_task(app, task_id, **values):
    with app.state.session_factory() as session:
        changed = session.execute(update(GenerationTask).where(
            GenerationTask.id == task_id, GenerationTask.status == "running",
        ).values(updated_at=utc_now(), **values))
        session.commit()
        return changed.rowcount == 1


async def execute_task(app, task_id):
    def check_control(session):
        task = session.get(GenerationTask, task_id)
        control = session.get(GenerationTaskControl, task_id)
        if not task or task.status != "running":
            raise asyncio.CancelledError()
        if control and control.pause_requested:
            raise TaskPaused()
        return control

    def model_started():
        with app.state.session_factory() as session:
            session.execute(update(GenerationTask).where(GenerationTask.id == task_id).values(
                updated_at=GenerationTask.updated_at).execution_options(synchronize_session=False))
            control = check_control(session)
            control.model_call_pending = True
            session.commit()

    async def progress(trace):
        stage, percentage = {0: ("prepare", 5), 1: ("generate", 25),
                             2: ("validate", 70), 3: ("save", 90)}[len(trace)]
        with app.state.session_factory() as session:
            task = session.get(GenerationTask, task_id)
            if not task or task.status != "running":
                raise asyncio.CancelledError()
            task.stage, task.progress, task.completed_nodes, task.updated_at = stage, percentage, trace, utc_now()
            control = session.get(GenerationTaskControl, task_id)
            if "generate" in trace and control:
                control.model_call_pending = False
            paused = bool(control and control.pause_requested)
            if paused:
                task.status = "paused"
            session.commit()
        if paused:
            raise TaskPaused()

    try:
        with app.state.session_factory() as session:
            task = session.get(GenerationTask, task_id)
            if not task or task.status != "running":
                return
            requirement = session.get(Requirement, task.requirement_id)
            pid, rid = requirement.project_id, requirement.id
            payload = CaseGenerateInput.model_validate(task.payload)
            control = session.get(GenerationTaskControl, task_id)
            if not control or control.workflow_version != WORKFLOW_VERSION:
                raise HTTPException(status_code=409, detail="生成流程版本已变化，请重新生成。")
            config = load_model_config(session)
            if config_signature(config) != task.settings_signature:
                raise HTTPException(status_code=409, detail="排队期间模型配置已变化，本次未调用模型，请重新发起任务。")
            session.rollback()
            await run_generation(pid, rid, payload, app, session, progress, runtime_config=config,
                                 checkpointed=True, model_started=model_started, guard_save=check_control)
        update_task(app, task_id, status="succeeded", stage="completed", progress=100,
                    completed_nodes=["prepare", "generate", "validate"], error_message="")
    except TaskPaused:
        update_task(app, task_id, status="paused")
    except asyncio.CancelledError:
        update_task(app, task_id, status="interrupted", error_message="服务停止或来源已删除，任务中断；可核对检查点后恢复，本次未自动重试。")
        raise
    except HTTPException as error:
        update_task(app, task_id, status="failed", error_message=str(error.detail))
    except Exception:
        # 只保存固定信息，不将第三方响应、异常堆栈或凭据写入任务表。
        update_task(app, task_id, status="failed", error_message="生成任务执行失败，请检查本地服务后重新发起。")


async def task_worker(app):
    while True:
        try:
            with app.state.session_factory() as session:
                task_id = session.scalar(select(GenerationTask.id).where(GenerationTask.status == "queued")
                                         .order_by(GenerationTask.created_at, GenerationTask.id).limit(1))
                if task_id:
                    claimed = session.execute(update(GenerationTask).where(
                        GenerationTask.id == task_id, GenerationTask.status == "queued",
                    ).values(status="running", updated_at=utc_now()))
                    session.commit()
                    if claimed.rowcount != 1:
                        continue
            if task_id:
                try:
                    await execute_task(app, task_id)
                except asyncio.CancelledError:
                    # 来源删除引起的单任务中止不应停止整个队列。
                    if asyncio.current_task().cancelling():
                        raise
            else:
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 临时数据库故障不让工作协程永久退出，也不重试已经领取的模型调用。
            await asyncio.sleep(1)
