import asyncio
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from ..models import McpInvocation, AgentToolCall, AgentTurn, utc_now
from ..settings import load_model_config
from ..generation_tasks import config_signature
from .registry import digest, require_grant, configuration
from .client import call

APPROVAL_TIMEOUT=120


def invocation_output(row):
    if not row:return None
    return {'server_name':row.server_name,'remote_tool':row.remote_tool,'mode':row.mode,
            'approval_token':row.approval_token,'argument_hash':row.argument_hash,'decision':row.decision,
            'decided_at':row.decided_at.isoformat()+'Z' if row.decided_at else None,
            'dispatched':row.dispatched,'description':row.context_snapshot['description']}


async def execute_external(app,project_id,event_id,snapshot,arguments):
    with app.state.session_factory() as session:
        event=session.get(AgentToolCall,event_id)
        require_grant(session,project_id,snapshot)
        fingerprint=digest(arguments)
        if snapshot['mode']!='readonly':
            previous=session.scalar(select(McpInvocation.tool_call_id).join(AgentToolCall,AgentToolCall.id==McpInvocation.tool_call_id)
                .where(AgentToolCall.turn_id==event.turn_id,McpInvocation.dispatched.is_(True),
                       McpInvocation.server_id==snapshot['server_id'],McpInvocation.remote_tool==snapshot['remote_tool'],McpInvocation.argument_hash==fingerprint))
            if previous:raise HTTPException(409,'本轮已发送过相同外部写入/未知调用，未重复执行，请先核实结果。')
        invocation=McpInvocation(tool_call_id=event_id,server_id=snapshot['server_id'],server_name=snapshot['server_name'],
            remote_tool=snapshot['remote_tool'],mode=snapshot['mode'],context_snapshot=snapshot,
            argument_hash=fingerprint,approval_token=str(uuid4()),decision='not_required' if snapshot['mode']=='readonly' else 'pending')
        session.add(invocation)
        if invocation.decision=='pending':event.status='awaiting_approval'
        event.updated_at=utc_now();session.get(AgentTurn,event.turn_id).updated_at=utc_now();session.commit()
    deadline=time.monotonic()+APPROVAL_TIMEOUT
    while True:
        with app.state.session_factory() as session:
            invocation=session.get(McpInvocation,event_id);event=session.get(AgentToolCall,event_id)
            if not invocation or not event:raise HTTPException(404,'项目或调用记录已删除，未继续执行。')
            require_grant(session,project_id,snapshot)
            turn=session.get(AgentTurn,event.turn_id)
            if turn.status!='running' or turn.settings_signature!=config_signature(load_model_config(session)):
                raise HTTPException(409,'本轮已停止或生成模型配置变化，未继续执行外部工具。')
            if invocation.decision in ('approved','not_required'):break
            if invocation.decision!='pending':raise HTTPException(400,'用户拒绝或调用确认已失效，未执行外部工具。')
        if time.monotonic()>deadline:raise HTTPException(408,'等待确认超过 120 秒，未执行外部工具。')
        await asyncio.sleep(.25)
    with app.state.session_factory() as session:
        session.execute(text('BEGIN IMMEDIATE'))
        server=require_grant(session,project_id,snapshot);config=configuration(server)
        invocation=session.get(McpInvocation,event_id);event=session.get(AgentToolCall,event_id)
        turn=session.get(AgentTurn,event.turn_id)
        if invocation.dispatched or turn.status!='running' or digest(arguments)!=invocation.argument_hash or turn.settings_signature!=config_signature(load_model_config(session)):
            raise HTTPException(409,'调用已发送或参数变化，未重复执行。')
        invocation.dispatched=True;event.status='running';event.updated_at=turn.updated_at=utc_now();session.commit()
    return await (app.state.mcp_call or call)(config,snapshot['remote_tool'],arguments)
