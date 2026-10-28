import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client, DEFAULT_INHERITED_ENV_VARS
from mcp.client.streamable_http import streamablehttp_client

# SDK 可能记录原始远端消息、URL 或 stderr；应用只展示固定错误。
logging.getLogger('mcp').setLevel(logging.CRITICAL + 1)
logging.getLogger('httpx').setLevel(logging.WARNING)


def http_client(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=False, trust_env=False)


@asynccontextmanager
async def connect(config):
    """每次操作独立会话，退出时清理 HTTP/子进程；无自动重试。"""
    async with asyncio.timeout(config['timeout_seconds']):
        if config['transport'] == 'stdio':
            # 显式覆盖 SDK 默认继承项，不继承父进程的凭据/代理。
            env = {key: '' for key in DEFAULT_INHERITED_ENV_VARS}
            env['PATH'] = '/usr/bin:/bin'
            env.update(config['credentials'])
            params = StdioServerParameters(command=config['command'], args=config['args'], env=env)
            with open(os.devnull, 'w') as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config['timeout_seconds'])) as session:
                        info = await session.initialize()
                        yield session, info
        else:
            async with streamablehttp_client(config['endpoint'], headers=config['credentials'],
                    timeout=config['timeout_seconds'], sse_read_timeout=config['timeout_seconds'],
                    httpx_client_factory=http_client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config['timeout_seconds'])) as session:
                    info = await session.initialize()
                    yield session, info


async def discover(config):
    try:
        async with connect(config) as (session, info):
            tools, cursor, cursors = [], None, set()
            for _ in range(10):
                page = await session.list_tools(cursor=cursor)
                tools.extend(tool.model_dump(mode='json', exclude_none=True) for tool in page.tools)
                if len(tools)>50:
                    raise HTTPException(400, '服务超过 50 个工具上限，请缩小服务暴露范围。')
                cursor = page.nextCursor
                if not cursor:
                    return {'protocol_version': info.protocolVersion, 'tools': tools}
                if cursor in cursors:
                    break
                cursors.add(cursor)
            raise HTTPException(400, '工具列表分页过多或游标重复，未发布目录。')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'MCP 连接或工具发现失败，请核对传输、命令/端点、凭据与超时；HTTP 401/403 或需要 OAuth 的服务需另行配置，本版不支持 OAuth 登录。') from None


async def call(config, name, arguments):
    try:
        async with connect(config) as (session, _):
            result = await session.call_tool(name, arguments)
            # 不支持图片、音频、资源自动加载；它们仅返回类型提示。
            content = [{'type':'text','text':item.text} if item.type=='text' else {'type':item.type,'message':'本版未展开此类内容。'} for item in result.content]
            data = {'is_error':bool(result.isError), 'content':content, 'structured_content':result.structuredContent}
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(raw)>config['max_result_chars']:
                return {'is_error':bool(result.isError), 'truncated':True, 'preview':raw[:config['max_result_chars']], 'message':'MCP 结果超过服务配置长度，已截断。'}
            return data
    except Exception:
        raise HTTPException(502, 'MCP 调用失败或超时，远端是否执行需核实；本轮未自动重试。') from None
