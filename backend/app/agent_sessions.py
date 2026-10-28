import asyncio
import hashlib
import json
from datetime import timezone
from time import perf_counter
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from .agents.protocol import (MAX_MODEL_CALLS, MAX_TOOL_CALLS, MAX_RESULT_CHARS, TOOL_TIMEOUT, TURN_TIMEOUT,
                               agent_completion, mock_completion, parse_message)
from .agents.tools import TOOLS, execute_tool, tool_schemas
from .agents.workflow import ENGINE_VERSION, PROMPT, PROMPT_VERSION, build_agent_graph
from .deps import get_session
from .generation_tasks import config_signature
from .models import (AgentSession, AgentTurn, AgentToolCall, GenerationSettings, EmbeddingSettings,
                     RerankSettings, JudgeSettings, AgentMcpContext, McpInvocation, utc_now)
from .projects import require_project
from .settings import load_model_config

router = APIRouter(prefix='/api/projects/{project_id}/agent-sessions', tags=['项目 Agent'])
ACTIVE = ('queued', 'running')
from .mcp.registry import credential_keys, project_tools, validate_arguments, require_grant
from .mcp.runtime import execute_external, invocation_output


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approval_token: UUID
    argument_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    approve: bool



class SessionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: UUID


class TurnInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    message: str = Field(min_length=1, max_length=2000)

    @field_validator('message')
    @classmethod
    def valid_message(cls, value):
        value = value.strip()
        if not value or any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('invalid question')
        return value


def secrets(session):
    return [row.api_key for model in (GenerationSettings, EmbeddingSettings, RerankSettings, JudgeSettings)
            if (row := session.get(model, 1)) and row.api_key] + credential_keys(session)


def sanitize(value, keys):
    if isinstance(value, str):
        value = value.encode('utf-8', errors='replace').decode()
        for key in sorted(set(keys), key=len, reverse=True):
            value = value.replace(key, '[已隐藏]')
        return value
    if isinstance(value, dict):
        return {sanitize(key, keys): sanitize(item, keys) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, keys) for item in value]
    return value


def timestamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat()


def require_session(session, project_id, session_id):
    require_project(session, project_id)
    row = session.get(AgentSession, str(session_id))
    if not row or row.project_id != project_id:
        raise HTTPException(404, '当前项目下的 Agent 对话不存在。')
    return row


def require_turn(session, project_id, session_id, turn_id):
    require_session(session, project_id, session_id)
    turn = session.get(AgentTurn, str(turn_id))
    if not turn or turn.session_id != str(session_id):
        raise HTTPException(404, '当前对话下的 Agent 轮次不存在。')
    return turn


def session_output(row):
    return {'id': row.id, 'project_id': row.project_id, 'title': row.title,
            'created_at': timestamp(row.created_at), 'updated_at': timestamp(row.updated_at)}


def turn_output(session, turn):
    events = session.scalars(select(AgentToolCall).where(AgentToolCall.turn_id == turn.id).order_by(AgentToolCall.ordinal)).all()
    return {'id': turn.id, 'session_id': turn.session_id, 'question': turn.question, 'answer': turn.answer,
        'status': turn.status, 'error_message': turn.error_message, 'model': turn.model, 'mock_mode': turn.mock_mode,
        'model_calls': turn.model_calls, 'tool_calls': turn.tool_calls, 'model_call_pending': turn.model_call_pending,
        'prompt_version': turn.prompt_version, 'created_at': timestamp(turn.created_at),
        'tools': [{'id': event.id, 'source_id': event.source_id, 'tool_name': event.tool_name,
                   'arguments': event.arguments, 'status': event.status, 'result': event.result,
                   'error_message': event.error_message, 'elapsed_ms': event.elapsed_ms,
                   'mcp': invocation_output(session.get(McpInvocation, event.id)),
                   'started_at': timestamp(event.started_at), 'updated_at': timestamp(event.updated_at)} for event in events]}


@router.get('')
def list_sessions(project_id: int, session: Session = Depends(get_session)):
    require_project(session, project_id)
    return [session_output(row) for row in session.scalars(select(AgentSession).where(AgentSession.project_id == project_id)
                .order_by(AgentSession.updated_at.desc(), AgentSession.id.desc())).all()]


@router.post('', status_code=201)
def create_session(project_id: int, payload: SessionInput, response: Response, session: Session = Depends(get_session)):
    session.execute(text('BEGIN IMMEDIATE'))
    require_project(session, project_id)
    row = session.get(AgentSession, str(payload.session_id))
    if row:
        if row.project_id != project_id:
            raise HTTPException(409, '对话标识已被使用，请重新创建。')
        response.status_code = 200
        return session_output(row)
    row = AgentSession(id=str(payload.session_id), project_id=project_id, title='新对话')
    session.add(row);session.commit()
    return session_output(row)


@router.get('/{session_id}')
def get_session_detail(project_id: int, session_id: UUID, session: Session = Depends(get_session)):
    row = require_session(session, project_id, session_id)
    turns = session.scalars(select(AgentTurn).where(AgentTurn.session_id == row.id).order_by(AgentTurn.created_at, AgentTurn.id)).all()
    return {**session_output(row), 'turns': [turn_output(session, turn) for turn in turns]}


@router.post('/{session_id}/turns', status_code=202)
def submit_turn(project_id: int, session_id: UUID, payload: TurnInput, response: Response, session: Session = Depends(get_session)):
    session.execute(text('BEGIN IMMEDIATE'))
    conversation = require_session(session, project_id, session_id)
    fingerprint = hashlib.sha256((str(session_id)+':'+payload.message).encode()).hexdigest()
    existing = session.get(AgentTurn, str(payload.request_id))
    if existing:
        if existing.session_id != str(session_id) or existing.request_fingerprint != fingerprint:
            raise HTTPException(409, '提问请求标识已被使用，请重新发起。')
        response.status_code = 200
        return turn_output(session, existing)
    if session.scalar(select(AgentTurn.id).where(AgentTurn.session_id == str(session_id), AgentTurn.status.in_(ACTIVE))):
        raise HTTPException(409, '当前对话已有运行中的提问，请等待完成。')
    if session.scalar(select(func.count()).select_from(AgentTurn).where(AgentTurn.session_id == str(session_id))) >= 50:
        raise HTTPException(400, '当前对话已达到 50 轮，请新建对话。')
    config = load_model_config(session)
    question = sanitize(payload.message, secrets(session))
    turn = AgentTurn(id=str(payload.request_id), session_id=str(session_id), request_fingerprint=fingerprint,
        question=question, model=config.model, mock_mode=config.mock_mode, settings_signature=config_signature(config),
        engine_version=ENGINE_VERSION, prompt_version=PROMPT_VERSION, prompt_snapshot=PROMPT,
        tools_snapshot=tool_schemas())
    external_schemas, external_mapping = project_tools(session, project_id) if not config.mock_mode else ([], {})
    turn.tools_snapshot = sanitize([*turn.tools_snapshot, *external_schemas], secrets(session))
    session.add(turn);session.flush()
    session.add(AgentMcpContext(turn_id=turn.id, tools=sanitize(external_mapping, secrets(session))))
    if conversation.title == '新对话':
        conversation.title = question[:60]
    conversation.updated_at = utc_now()
    session.commit()
    return turn_output(session, turn)


@router.get('/{session_id}/turns/{turn_id}')
def get_turn(project_id: int, session_id: UUID, turn_id: UUID, session: Session = Depends(get_session)):
    return turn_output(session, require_turn(session, project_id, session_id, turn_id))


@router.get('/{session_id}/turns/{turn_id}/events')
async def stream_turn(project_id: int, session_id: UUID, turn_id: UUID, request: Request,
                      session: Session = Depends(get_session)):
    require_turn(session, project_id, session_id, turn_id)
    session.rollback()
    async def events():
        previous = None
        seen_start, seen_end = set(), set()
        ticks = 0
        while not await request.is_disconnected():
            with request.app.state.session_factory() as reader:
                try:
                    turn = require_turn(reader, project_id, session_id, turn_id)
                except HTTPException:
                    yield 'data: '+json.dumps({'type':'error','message':'项目、对话或轮次已删除。'},ensure_ascii=False)+'\n\n'
                    return
                marker = (turn.status, turn.updated_at)
                if marker != previous:
                    payload = turn_output(reader, turn)
                    previous = marker
                    for tool in payload['tools']:
                        if tool['id'] not in seen_start:
                            seen_start.add(tool['id'])
                            yield 'data: '+json.dumps({'type':'tool_started','turn_id':payload['id'], 'tool':{**tool,'status':'running','result':None,'error_message':''}},ensure_ascii=False)+'\n\n'
                        if tool['status'] not in ('running','awaiting_approval') and tool['id'] not in seen_end:
                            seen_end.add(tool['id'])
                            yield 'data: '+json.dumps({'type':'tool_finished','turn_id':payload['id'],'tool':tool},ensure_ascii=False)+'\n\n'
                    yield 'data: '+json.dumps({'type':'snapshot','turn':payload},ensure_ascii=False)+'\n\n'
                if turn.status not in ACTIVE:
                    yield 'data: {"type":"done"}\n\n'
                    return
            ticks += 1
            if ticks % 40 == 0:
                yield ': ping\n\n'
            await asyncio.sleep(.25)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@router.post('/{session_id}/turns/{turn_id}/tools/{tool_id}/decision')
def decide_tool(project_id: int, session_id: UUID, turn_id: UUID, tool_id: UUID, payload: ApprovalInput,
                session: Session = Depends(get_session)):
    session.execute(text('BEGIN IMMEDIATE'))
    turn = require_turn(session, project_id, session_id, turn_id)
    event = session.get(AgentToolCall, str(tool_id))
    invocation = session.get(McpInvocation, str(tool_id))
    if not event or event.turn_id != turn.id or not invocation:
        raise HTTPException(404, '本轮 MCP 调用不存在。')
    if str(payload.approval_token) != invocation.approval_token or payload.argument_hash != invocation.argument_hash:
        raise HTTPException(409, '工具或参数与待确认内容不一致，请刷新。')
    decision = 'approved' if payload.approve else 'rejected'
    if invocation.decision == decision:
        return turn_output(session, turn)
    if turn.status != 'running' or event.status != 'awaiting_approval' or invocation.decision != 'pending':
        raise HTTPException(409, '该调用已结束或确认失效，未重复执行。')
    require_grant(session, project_id, invocation.context_snapshot)
    if turn.settings_signature != config_signature(load_model_config(session)):
        raise HTTPException(409, '生成模型配置已变化，未接受确认；请重新提问。')
    invocation.decision = decision;invocation.decided_at = utc_now()
    event.updated_at = turn.updated_at = utc_now();session.commit()
    return turn_output(session, turn)


def settle_agent(app):
    with app.state.session_factory() as session:
        ids = select(AgentTurn.id).where(AgentTurn.status.in_(ACTIVE))
        session.execute(update(AgentToolCall).where(AgentToolCall.turn_id.in_(ids), AgentToolCall.status.in_(('running', 'awaiting_approval')))
                        .values(status='interrupted', error_message='服务停止，调用结果未完成；未自动重试。', updated_at=utc_now()))
        session.execute(update(AgentTurn).where(AgentTurn.status.in_(ACTIVE)).values(status='interrupted',
                        error_message='服务停止或重启，本轮已中断。重新提问可能重复计费；本轮未自动重试。', updated_at=utc_now()))
        pending = session.scalars(select(McpInvocation).join(AgentToolCall, AgentToolCall.id == McpInvocation.tool_call_id).where(AgentToolCall.status == 'interrupted', McpInvocation.decision == 'pending')).all()
        for item in pending:
            item.decision = 'expired';item.decided_at = utc_now()
        session.commit()


def safe_result(data, source_id, keys):
    data = sanitize(data, keys)
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    if len(encoded) > MAX_RESULT_CHARS:
        return {'source_id': source_id, 'ok': True, 'truncated': True, 'data': {'preview': encoded[:9000]},
                'message': '结果已截断，请缩小查询或打开来源页面获取完整内容。'}
    return {'source_id': source_id, 'ok': True, 'truncated': False, 'data': data}


async def execute_agent(app, turn_id):
    with app.state.session_factory() as session:
        turn = session.get(AgentTurn, turn_id)
        if not turn or turn.status != 'running':
            return
        conversation = session.get(AgentSession, turn.session_id)
        project_id, session_id = conversation.project_id, conversation.id
        keys = secrets(session)
        history = list(session.scalars(select(AgentTurn).where(AgentTurn.session_id == session_id,
                           AgentTurn.status == 'succeeded', AgentTurn.id != turn.id)
                           .order_by(AgentTurn.created_at.desc(), AgentTurn.id.desc()).limit(4)).all())
        messages = [{'role':'system', 'content': PROMPT+f'\n当前项目 ID：{project_id}。项目范围不可更改。'}]
        for old in reversed(history):
            messages.extend([{'role':'user','content':old.question},{'role':'assistant','content':old.answer}])
        messages.append({'role':'user','content':turn.question})
        schemas = turn.tools_snapshot
        context = session.get(AgentMcpContext, turn.id)
        external_tools = context.tools if context else {}
    def guard(session):
        turn = require_turn(session, project_id, session_id, turn_id)
        if turn.status != 'running':
            raise HTTPException(409, 'Agent 轮次已停止。')
        config = load_model_config(session)
        if turn.settings_signature != config_signature(config):
            raise HTTPException(409, '生成模型配置已变化，本轮不再调用模型；请重新提问。')
        if turn.engine_version != ENGINE_VERSION:
            raise HTTPException(409, 'Agent 流程或工具版本已变化，请重新提问。')
        keys.extend(secrets(session))
        return turn, config
    async def model_step(state):
        if state['model_calls'] >= MAX_MODEL_CALLS:
            raise HTTPException(400, '本轮已达到模型调用次数上限，请缩小问题后重新提问。')
        with app.state.session_factory() as session:
            turn, config = guard(session)
            turn.model_calls = state['model_calls']+1
            turn.model_call_pending = not config.mock_mode
            turn.updated_at = utc_now();session.commit()
        choice = 'required' if state['tool_calls'] == 0 else 'none' if state['tool_calls'] >= MAX_TOOL_CALLS or state['model_calls'] == MAX_MODEL_CALLS-1 else 'auto'
        reply = mock_completion(state['messages']) if config.mock_mode else await agent_completion(config,
            state['messages'], schemas, choice, transport=app.state.agent_transport if app.state.agent_transport is not None else app.state.llm_transport)
        # 只保存和发送所需字段；reasoning_content 等其他响应字段不会进入对话或审计。
        reply = parse_message(sanitize(reply, keys))
        for call in reply.get('tool_calls', []):
            try:
                value = json.loads(call['function']['arguments'])
                if not isinstance(value, dict):
                    raise ValueError('not object')
                call['function']['arguments'] = json.dumps(sanitize(value, keys), ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                call['function']['arguments'] = 'INVALID_JSON'
        calls = reply.get('tool_calls', [])
        if calls and (len(calls)+state['tool_calls'] > MAX_TOOL_CALLS or state['model_calls']+1 >= MAX_MODEL_CALLS):
            raise HTTPException(400, '本轮工具或模型调用预算已用尽，未执行新工具，请缩小问题。')
        if any(call['id'] in state['native_ids'] for call in calls):
            raise HTTPException(502, '模型重复使用了工具调用标识，未重复执行。')
        if not calls and not state['successful_tools']:
            raise HTTPException(502, '本轮没有成功的项目查询，未生成已核实答复；请检查工具状态或模型工具调用支持。')
        with app.state.session_factory() as session:
            turn, _ = guard(session)
            turn.model_call_pending = False;turn.updated_at = utc_now();session.commit()
        return {'messages':[*state['messages'],reply], 'model_calls':state['model_calls']+1,
                'native_ids':[*state['native_ids'],*[call['id'] for call in calls]],
                'answer': reply.get('content') or '' if not calls else ''}
    async def tools_step(state):
        messages = list(state['messages']);count = state['tool_calls'];successful = state['successful_tools']
        for call in messages[-1]['tool_calls']:
            name = call['function']['name'];count += 1;source_id = f'T{count}'
            args, error = None, ''
            external = external_tools.get(name)
            try:
                if external:
                    args = json.loads(call['function']['arguments']);validate_arguments(external, args)
                else:
                    if name not in TOOLS:
                        raise ValueError('not allowed')
                    args = TOOLS[name][0].model_validate(json.loads(call['function']['arguments']))
            except Exception:
                args = None
                error = '工具未允许或参数无效；请核对已提供的工具和输入 schema。'
            started = perf_counter();event_id = str(uuid4())
            with app.state.session_factory() as session:
                turn, config = guard(session)
                event = AgentToolCall(id=event_id, turn_id=turn_id, ordinal=count, source_id=source_id,
                    native_id=call['id'], tool_name=name, arguments=sanitize(args if external else args.model_dump(mode='json'), keys) if args is not None else {'invalid':True})
                session.add(event);turn.tool_calls = count;turn.updated_at = utc_now();session.commit()
            result = None
            try:
                if error:
                    raise HTTPException(400, error)
                raw = await execute_external(app, project_id, event_id, external, args) if external else await asyncio.wait_for(execute_tool(app, project_id, name, args, config.mock_mode), TOOL_TIMEOUT)
                if external and raw.get('is_error'):
                    raise HTTPException(502, 'MCP 工具报告执行错误；结果需核实，未自动重试。')
                with app.state.session_factory() as session:
                    keys.extend(secrets(session))
                result = safe_result(raw, source_id, keys)
                successful += 1
            except (TimeoutError, asyncio.TimeoutError):
                error = '工具调用超时，结果未知；未自动重试。'
            except Exception as exc:
                error = sanitize(str(exc.detail), keys) if isinstance(exc, HTTPException) else '工具查询失败，请检查数据与本地服务；未自动重试。'
            with app.state.session_factory() as session:
                turn = require_turn(session, project_id, session_id, turn_id)
                event = session.get(AgentToolCall, event_id)
                invocation = session.get(McpInvocation, event_id)
                if invocation and invocation.decision == 'pending':
                    invocation.decision = 'expired';invocation.decided_at = utc_now()
                event.status = ('unknown' if invocation and invocation.dispatched and error else 'failed') if error else 'succeeded'
                event.result = result;event.error_message = error
                event.elapsed_ms = round((perf_counter()-started)*1000)
                event.updated_at = turn.updated_at = utc_now();session.commit()
            wire = result or {'source_id': source_id, 'ok':False, 'error':error}
            messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(wire,ensure_ascii=False)})
        return {'messages':messages,'tool_calls':count,'successful_tools':successful}
    graph = build_agent_graph(model_step, tools_step)
    async with asyncio.timeout(TURN_TIMEOUT):
        state = await graph.ainvoke({'messages':sanitize(messages,keys),'model_calls':0,'tool_calls':0,
                                    'successful_tools':0,'native_ids':[],'answer':''}, {'recursion_limit':20})
    with app.state.session_factory() as session:
        turn, _ = guard(session)
        turn.answer = sanitize(state['answer'], keys);turn.status = 'succeeded';turn.updated_at = utc_now()
        session.get(AgentSession, session_id).updated_at = utc_now();session.commit()


async def agent_worker(app):
    while True:
        try:
            with app.state.session_factory() as session:
                id = session.scalar(select(AgentTurn.id).where(AgentTurn.status == 'queued').order_by(AgentTurn.created_at, AgentTurn.id).limit(1))
                if id:
                    changed = session.execute(update(AgentTurn).where(AgentTurn.id == id, AgentTurn.status == 'queued')
                                              .values(status='running', updated_at=utc_now()))
                    session.commit()
                    if not changed.rowcount:
                        id = None
            if id:
                try:
                    await execute_agent(app, id)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    with app.state.session_factory() as session:
                        turn = session.get(AgentTurn, id)
                        if turn:
                            turn.status = 'failed'
                            turn.error_message = sanitize(str(error.detail), secrets(session)) if isinstance(error, HTTPException) else ('本轮总耗时达到上限，未自动重试。' if isinstance(error, TimeoutError) else 'Agent 执行失败，请核对配置后重新提问；未自动重试。')
                            turn.updated_at = utc_now()
                            session.execute(update(AgentToolCall).where(AgentToolCall.turn_id == id, AgentToolCall.status.in_(('running', 'awaiting_approval')))
                                .values(status='interrupted', error_message='本轮已停止，工具结果未完成。', updated_at=utc_now()))
                            for item in session.scalars(select(McpInvocation).join(AgentToolCall, AgentToolCall.id == McpInvocation.tool_call_id).where(AgentToolCall.turn_id == id, McpInvocation.decision == 'pending')):
                                item.decision = 'expired';item.decided_at = utc_now()
                            session.commit()
            else:
                await asyncio.sleep(.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)
