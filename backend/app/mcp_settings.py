import json
import re
from typing import Literal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .deps import get_session
from .models import McpServer, McpBinding, utc_now
from .projects import require_project
from .mcp.client import discover
from .mcp.registry import configuration, clean_catalog, credential_keys

router=APIRouter(tags=['MCP 客户端'])


class ServerInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    transport:Literal['stdio','http']
    endpoint:str=Field(default='',max_length=1000)
    command:str=Field(default='',max_length=1000)
    args:list[str]=Field(default_factory=list,max_length=30)
    credentials:dict[str,SecretStr] | None=None
    clear_credentials:bool=False
    enabled:bool=True
    timeout_seconds:int=Field(default=20,ge=5,le=40,strict=True)
    max_result_chars:int=Field(default=12000,ge=1000,le=20000,strict=True)
    revision:int | None=None

    @model_validator(mode='after')
    def valid(self):
        self.name=self.name.strip()
        if not self.name or any('\x00' in v or any(0xD800<=ord(c)<=0xDFFF for c in v) for v in [self.name,self.command,self.endpoint,*self.args]):raise ValueError('invalid text')
        if sum(len(a) for a in self.args)>8000:raise ValueError('args too long')
        if self.transport=='stdio':
            if not Path(self.command).is_absolute() or self.endpoint:raise ValueError('absolute command required')
        else:
            url=urlsplit(self.endpoint)
            if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment or self.command or self.args:raise ValueError('invalid endpoint')
        if self.credentials is not None:
            if len(self.credentials)>20:raise ValueError('too many credentials')
            for key,value in self.credentials.items():
                pattern=r'[A-Za-z_][A-Za-z0-9_]{0,79}' if self.transport=='stdio' else r'[A-Za-z0-9-]{1,80}'
                if not re.fullmatch(pattern,key) or any(0xD800<=ord(c)<=0xDFFF for c in value.get_secret_value()) or len(value.get_secret_value())>4000 or any(c in value.get_secret_value() for c in ('\x00','\r','\n')):raise ValueError('invalid credentials')
                if self.transport=='http' and key.lower() not in ('authorization','x-api-key','api-key'):raise ValueError('unsupported header')
        return self


class Grant(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool=False
    mode:Literal['readonly','write','unknown']='unknown'


class BindingInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    server_revision:int
    revision:int=0
    tools:dict[str,Grant]


def require_server(session,id):
    server=session.get(McpServer,str(id))
    if not server:raise HTTPException(404,'MCP 服务不存在。')
    return server


def public_server(session,server):
    # 脱敏同时涵盖工具描述中远端意外回显的凭据。
    from .agent_sessions import sanitize, secrets
    return sanitize({key:getattr(server,key) for key in ('id','name','transport','endpoint','command','args','enabled','timeout_seconds','max_result_chars','revision','tools','protocol_version')} | {'credential_names':list(server.credentials)}, secrets(session))


@router.get('/api/settings/mcp')
def list_servers(session:Session=Depends(get_session)):
    return [public_server(session,row) for row in session.scalars(select(McpServer).order_by(McpServer.name,McpServer.id))]


def save_server(session,payload,server=None):
    if server and payload.revision!=server.revision:raise HTTPException(409,'服务配置已变化，请刷新后编辑。')
    secret_values={k:v.get_secret_value() for k,v in (payload.credentials or {}).items()}
    if server and server.credentials and (payload.transport!=server.transport or payload.endpoint!=server.endpoint or payload.command!=server.command or payload.args!=server.args) and payload.credentials is None and not payload.clear_credentials:
        raise HTTPException(400,'改变服务目标或命令时，请同时替换或清除凭据。')
    if server is None:
        server=McpServer(id=str(uuid4()),revision=0,credentials={});session.add(server)
    for key in ('name','transport','endpoint','command','args','enabled','timeout_seconds','max_result_chars'):setattr(server,key,getattr(payload,key))
    if payload.clear_credentials:server.credentials={}
    if payload.credentials is not None:server.credentials=secret_values
    server.revision+=1;server.tools=[];server.protocol_version='';server.updated_at=utc_now()
    session.commit();return public_server(session,server)


@router.post('/api/settings/mcp',status_code=201)
def add_server(payload:ServerInput,session:Session=Depends(get_session)):
    if session.scalar(select(McpServer.id).limit(31).offset(29)):raise HTTPException(400,'服务数量最多 30 个。')
    return save_server(session,payload)


@router.put('/api/settings/mcp/{server_id}')
def edit_server(server_id:UUID,payload:ServerInput,session:Session=Depends(get_session)):
    session.execute(text('BEGIN IMMEDIATE'))
    return save_server(session,payload,require_server(session,server_id))


@router.delete('/api/settings/mcp/{server_id}',status_code=204)
def delete_server(server_id:UUID,session:Session=Depends(get_session)):
    session.delete(require_server(session,server_id));session.commit();return Response(status_code=204)


@router.post('/api/settings/mcp/{server_id}/discover')
async def discover_server(server_id:UUID,request:Request,session:Session=Depends(get_session)):
    server=require_server(session,server_id)
    if not server.enabled:raise HTTPException(400,'请先启用该服务。')
    revision=server.revision;config=configuration(server);session.rollback()
    result=await (request.app.state.mcp_discover or discover)(config)
    try:catalog=clean_catalog(result['tools'])
    except Exception:raise HTTPException(502,'MCP 工具目录无效或过大；仅支持 object 输入 schema，无 $ref 引用，最多 50 个工具。') from None
    session.execute(text('BEGIN IMMEDIATE'));session.expire_all();server=require_server(session,server_id)
    if server.revision!=revision:raise HTTPException(409,'发现期间配置已变化，未保存目录。')
    from .agent_sessions import sanitize, secrets
    server.tools=sanitize(catalog,secrets(session));server.protocol_version=result['protocol_version']
    server.revision+=1;server.updated_at=utc_now();session.commit()
    return public_server(session,server)


@router.get('/api/projects/{project_id}/mcp-bindings')
def get_bindings(project_id:int,session:Session=Depends(get_session)):
    require_project(session,project_id)
    bindings={row.server_id:row for row in session.scalars(select(McpBinding).where(McpBinding.project_id==project_id))}
    return [{'server':public_server(session,server), 'revision':bindings[server.id].revision if server.id in bindings else 0,
        'valid':server.id in bindings and bindings[server.id].server_revision==server.revision,
        'tools':bindings[server.id].tools if server.id in bindings else {}} for server in session.scalars(select(McpServer).order_by(McpServer.name))]


@router.put('/api/projects/{project_id}/mcp-bindings/{server_id}')
def put_binding(project_id:int,server_id:UUID,payload:BindingInput,session:Session=Depends(get_session)):
    session.execute(text('BEGIN IMMEDIATE'));require_project(session,project_id);server=require_server(session,server_id)
    binding=session.get(McpBinding,(project_id,str(server_id)))
    if payload.server_revision!=server.revision or payload.revision!=(binding.revision if binding else 0):raise HTTPException(409,'服务或项目授权已变化，请刷新。')
    if not set(payload.tools).issubset({tool['name'] for tool in server.tools}):raise HTTPException(400,'包含未发现的工具，请先发现目录。')
    if len(payload.tools)>50:raise HTTPException(400,'工具过多。')
    if not binding:
        binding=McpBinding(project_id=project_id,server_id=str(server_id),revision=0);session.add(binding)
    binding.server_revision=server.revision;binding.tools={name:grant.model_dump() for name,grant in payload.tools.items()};binding.revision+=1
    session.commit();return {'revision':binding.revision,'server_revision':server.revision,'tools':binding.tools}
