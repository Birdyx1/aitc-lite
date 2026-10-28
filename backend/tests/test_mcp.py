import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import test_agent as fixtures
from app.main import create_app
from app.models import McpInvocation, McpServer, McpBinding
from app.mcp.registry import clean_catalog, project_tools

TOOLS=[{'name':name,'description':'本地测试 '+name,'inputSchema':{'type':'object','properties':{'text':{'type':'string','maxLength':100}},'required':['text'],'additionalProperties':False},'annotations':{'readOnlyHint':True}} for name in ['echo','write_note']]


class McpTests(unittest.TestCase):
    tearDown=fixtures.AgentTests.tearDown
    configure=fixtures.AgentTests.configure
    payload=fixtures.AgentTests.payload
    submit=fixtures.AgentTests.submit
    wait=fixtures.AgentTests.wait

    def setUp(self):
        fixtures.AgentTests.setUp(self)
        self.remote_calls=[];self.discoveries=[]
        async def discover(config):
            self.discoveries.append(config);return {'tools':TOOLS,'protocol_version':'2025-06-18'}
        async def call(config,name,args):
            self.remote_calls.append((config,name,args));return {'content':[{'type':'text','text':args['text']}],'is_error':False}
        self.app.state.mcp_discover=discover;self.app.state.mcp_call=call
        self.server=self.create_server()
        self.grant('echo','readonly')

    def create_server(self,name='demo'):
        result=self.client.post('/api/settings/mcp',json={'name':name,'transport':'http','endpoint':'http://127.0.0.1:18021/mcp','credentials':{'Authorization':'Bearer secret-mcp-key'}})
        self.assertEqual(result.status_code,201,result.text)
        result=self.client.post('/api/settings/mcp/'+result.json()['id']+'/discover')
        self.assertEqual(result.status_code,200,result.text);return result.json()

    def grant(self,name,mode,server=None,enabled=True):
        server=server or self.server
        bindings=self.client.get(f'/api/projects/{self.project}/mcp-bindings').json()
        binding=next(row for row in bindings if row['server']['id']==server['id'])
        tools=binding['tools'];tools[name]={'enabled':enabled,'mode':mode}
        response=self.client.put(f"/api/projects/{self.project}/mcp-bindings/{server['id']}",json={'server_revision':server['revision'],'revision':binding['revision'],'tools':tools})
        self.assertEqual(response.status_code,200,response.text)
        with self.app.state.session_factory() as session:
            return next((key for key,item in project_tools(session,self.project)[1].items() if item['remote_tool']==name and item['server_id']==server['id']),None)

    def waiting(self,id):
        until=time.monotonic()+5
        while time.monotonic()<until:
            row=self.client.get(self.url+'/turns/'+id).json()
            if row['tools'] and row['tools'][-1]['status']=='awaiting_approval':return row
            time.sleep(.03)
        self.fail('No approval requested: '+str(row))

    def decision(self,turn,approve=True,**extra):
        tool=turn['tools'][-1];mcp=tool['mcp']
        body={'approval_token':mcp['approval_token'],'argument_hash':mcp['argument_hash'],'approve':approve};body.update(extra)
        return self.client.post(self.url+'/turns/'+turn['id']+'/tools/'+tool['id']+'/decision',json=body)

    def test_config_secrets_catalog_and_safe_validation(self):
        text=self.client.get('/api/settings/mcp').text
        self.assertNotIn('secret-mcp-key',text);self.assertIn('credential_names',text)
        self.assertEqual(self.server['protocol_version'],'2025-06-18')
        for extra in ({'endpoint':'https://u:secret@example.com/mcp'},{'endpoint':'https://example.com/mcp?key=secret'}, {'transport':'stdio','command':'python','endpoint':''}, {'credentials':{'Host':'evil'}}, {'timeout_seconds':99}):
            result=self.client.post('/api/settings/mcp',json={'name':'x','transport':'http','endpoint':'http://localhost/mcp',**extra})
            self.assertEqual(result.status_code,422,result.text);self.assertNotIn('secret',result.text)

    def test_directory_untrusted_and_refs_rejected(self):
        for schema in ({'type':'object','$ref':'https://bad.invalid/schema'}, {'type':'string'}, {'type':'object','properties':{'x':{'$ref':'#/x'}}}):
            with self.assertRaises(Exception):clean_catalog([{'name':'echo','inputSchema':schema}])
        with self.assertRaises(Exception):clean_catalog([TOOLS[0],TOOLS[0]])

    def test_no_automatic_tool_grants_and_namespaces(self):
        other=self.create_server('same-tools')
        with self.app.state.session_factory() as session:
            self.assertEqual(len(project_tools(session,self.project)[1]),1)
            self.assertEqual(project_tools(session,999)[1],{})
        alias=self.grant('echo','readonly',other)
        original=self.grant('echo','readonly')
        self.assertNotEqual(alias,original);self.assertLessEqual(len(alias),64)

    def test_readonly_native_roundtrip_and_audit(self):
        alias=self.grant('echo','readonly');self.plan=[(alias,{'text':'hello'})]
        turn=self.wait(self.submit('外部查询')['id'])
        self.assertEqual(len(self.remote_calls),1);self.assertEqual(self.remote_calls[0][1],'echo')
        audit=turn['tools'][0]['mcp'];self.assertEqual(audit['decision'],'not_required');self.assertTrue(audit['dispatched'])
        self.assertNotIn('secret-mcp-key',json.dumps(turn));self.assertIn(alias,json.dumps(turn['tools']))
        self.client.get(self.url+'/turns/'+turn['id']+'/events');self.assertEqual(len(self.remote_calls),1)

    def test_write_requires_confirmation_and_duplicate_decision_is_idempotent(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit('记录便签')['id']);self.assertEqual(self.remote_calls,[])
        self.assertEqual(self.decision(turn,argument_hash='a'*64).status_code,409)
        self.assertEqual(self.decision(turn).status_code,200)
        done=self.wait(turn['id']);self.assertEqual(len(self.remote_calls),1)
        self.assertEqual(self.decision(turn).status_code,200);self.assertEqual(len(self.remote_calls),1)
        self.assertIsNotNone(done['tools'][0]['mcp']['decided_at'])

    def test_unknown_hint_does_not_bypass_confirmation_and_rejection(self):
        alias=self.grant('echo','unknown');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id']);self.assertEqual(self.decision(turn,False).status_code,200)
        done=self.wait(turn['id'],'failed');self.assertEqual(self.remote_calls,[])
        self.assertEqual(done['tools'][0]['mcp']['decision'],'rejected')

    def test_revoked_permission_prevents_pending_call(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id']);self.grant('write_note','write',enabled=False)
        self.assertEqual(self.decision(turn).status_code,409)
        self.wait(turn['id'],'failed');self.assertEqual(self.remote_calls,[])

    def test_schema_validation_and_unbound_tools_never_dispatch(self):
        alias=self.grant('echo','readonly')
        for name,args in [(alias,{'text':3}),(alias,{'text':'ok','command':'rm'}),('mcp_unbound',{'text':'ok'})]:
            self.plan=[(name,args)];done=self.wait(self.submit(str(uuid4()))['id'],'failed')
            self.assertEqual(done['tools'][0]['status'],'failed')
        self.assertEqual(self.remote_calls,[])

    def test_remote_failure_unknown_no_replay(self):
        async def fail(*args):self.remote_calls.append('sent');raise RuntimeError('secret-mcp-key')
        self.app.state.mcp_call=fail
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id']);self.decision(turn)
        done=self.wait(turn['id'],'failed');self.assertEqual(done['tools'][0]['status'],'unknown')
        self.assertNotIn('secret-mcp-key',json.dumps(done));self.assertEqual(len(self.remote_calls),1)
        self.decision(turn);self.assertEqual(len(self.remote_calls),1)

    def test_duplicate_write_in_same_turn_is_blocked(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'}),(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id']);self.decision(turn)
        done=self.wait(turn['id']);self.assertEqual(len(self.remote_calls),1)
        self.assertEqual(done['tools'][1]['status'],'failed')

    def test_configuration_changes_require_discovery_and_rebinding(self):
        payload={key:self.server[key] for key in ('name','transport','endpoint','command','args','enabled','timeout_seconds','max_result_chars','revision')}
        self.assertEqual(self.client.put('/api/settings/mcp/'+self.server['id'],json={**payload,'endpoint':'http://localhost:18022/mcp'}).status_code,400)
        updated=self.client.put('/api/settings/mcp/'+self.server['id'],json={**payload,'enabled':False})
        self.assertEqual(updated.status_code,200,updated.text);self.assertEqual(updated.json()['tools'],[])
        with self.app.state.session_factory() as session:self.assertEqual(project_tools(session,self.project)[1],{})
        self.assertEqual(self.client.put('/api/settings/mcp/'+self.server['id'],json=payload).status_code,409)

    def test_mock_does_not_offer_or_call_external_tools(self):
        self.configure(mock=True)
        done=self.wait(self.submit()['id']);self.assertEqual(self.remote_calls,[])
        self.assertTrue(done['mock_mode']);self.assertTrue(all(not row['tool_name'].startswith('mcp_') for row in done['tools']))

    def test_restart_expires_approval_no_auto_replay(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id']);self.client.__exit__(None,None,None)
        self.app=create_app(self.path,agent_transport=self.transport)
        self.client=TestClient(self.app).__enter__()
        done=self.client.get(self.url+'/turns/'+turn['id']).json()
        self.assertEqual(done['status'],'interrupted');self.assertEqual(done['tools'][0]['mcp']['decision'],'expired')
        self.assertEqual(self.decision(turn).status_code,409);self.assertEqual(self.remote_calls,[])

    def test_project_delete_cascades_context_and_audit(self):
        alias=self.grant('echo','readonly');self.plan=[(alias,{'text':'ok'})];self.wait(self.submit()['id'])
        self.client.delete('/api/projects/'+str(self.project))
        with self.app.state.session_factory() as session:
            self.assertEqual(list(session.scalars(select(McpBinding))),[])
            self.assertEqual(list(session.scalars(select(McpInvocation))),[])
            self.assertIsNotNone(session.get(McpServer,self.server['id']))


    def test_model_configuration_changed_during_approval_blocks_dispatch(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id']);self.configure(mock=True)
        self.assertEqual(self.decision(turn).status_code,409)
        self.wait(turn['id'],'failed');self.assertEqual(self.remote_calls,[])

    def test_approval_project_scope_and_expiry(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        with patch('app.mcp.runtime.APPROVAL_TIMEOUT',.4):
            turn=self.waiting(self.submit()['id'])
            other=self.client.post('/api/projects',json={'name':'other'}).json()['id']
            tool=turn['tools'][0]
            result=self.client.post(f"/api/projects/{other}/agent-sessions/{self.sid}/turns/{turn['id']}/tools/{tool['id']}/decision",json={'approval_token':tool['mcp']['approval_token'],'argument_hash':tool['mcp']['argument_hash'],'approve':True})
            self.assertEqual(result.status_code,404)
            done=self.wait(turn['id'],'failed')
        self.assertEqual(done['tools'][0]['mcp']['decision'],'expired');self.assertEqual(self.remote_calls,[])

    def test_disconnected_external_tool_does_not_break_builtin_query(self):
        async def fail(*args):raise RuntimeError('secret-mcp-key')
        self.app.state.mcp_call=fail
        alias=self.grant('echo','readonly');self.plan=[(alias,{'text':'hello'}),('builtin_project_overview',{})]
        turn=self.wait(self.submit()['id'])
        self.assertEqual(turn['tools'][0]['status'],'unknown');self.assertEqual(turn['tools'][1]['status'],'succeeded')
        self.assertNotIn('secret-mcp-key',json.dumps(turn))

    def test_deleted_service_invalidates_pending_approval_but_keeps_audit(self):
        alias=self.grant('write_note','write');self.plan=[(alias,{'text':'note'})]
        turn=self.waiting(self.submit()['id'])
        self.assertEqual(self.client.delete('/api/settings/mcp/'+self.server['id']).status_code,204)
        self.wait(turn['id'],'failed');self.assertEqual(self.remote_calls,[])
        with self.app.state.session_factory() as session:self.assertEqual(len(list(session.scalars(select(McpInvocation)))),1)


class McpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_sdk_stdio_discovery_call_and_process_cleanup(self):
        import sys
        from app.mcp.client import discover,call
        config={'transport':'stdio','command':sys.executable,'args':[str(Path(__file__).resolve().parents[1]/'examples/mcp_demo.py')],
                'credentials':{},'timeout_seconds':10,'max_result_chars':1000}
        result=await discover(config)
        self.assertEqual({item['name'] for item in result['tools']},{'lookup_rule','record_demo_note'})
        self.assertIn(result['protocol_version'],['2025-06-18','2025-03-26','2024-11-05'])
        clean_catalog(result['tools'])
        response=await call(config,'lookup_rule',{'topic':'取消订单'})
        self.assertFalse(response['is_error']);self.assertIn('取消',json.dumps(response,ensure_ascii=False))

    async def test_official_streamable_http_transport_with_asgi_server(self):
        import httpx
        from mcp.server.fastmcp import FastMCP
        from app.mcp.client import discover,call
        mcp=FastMCP('transport-test',json_response=True,stateless_http=True)
        @mcp.tool()
        def echo(text:str)->str:return text
        asgi=mcp.streamable_http_app()
        def factory(headers=None,timeout=None,auth=None):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi),headers=headers,timeout=timeout,auth=auth)
        config={'transport':'http','endpoint':'http://localhost/mcp','credentials':{'Authorization':'Bearer fake'},'timeout_seconds':5,'max_result_chars':1000}
        async with mcp.session_manager.run():
            with patch('app.mcp.client.http_client',factory):
                discovered=await discover(config)
                self.assertEqual(discovered['tools'][0]['name'],'echo')
                response=await call(config,'echo',{'text':'hello'})
                self.assertFalse(response['is_error']);self.assertIn('hello',json.dumps(response))

    async def test_result_truncation_and_remote_error_not_retried(self):
        import httpx
        from mcp.server.fastmcp import FastMCP
        from app.mcp.client import call
        mcp=FastMCP('result-test',json_response=True,stateless_http=True);calls=[]
        @mcp.tool()
        def huge()->str:calls.append(1);return '中'*5000
        @mcp.tool()
        def fail()->str:calls.append(1);raise ValueError('test-error')
        asgi=mcp.streamable_http_app()
        def factory(headers=None,timeout=None,auth=None):return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi),headers=headers,timeout=timeout,auth=auth)
        config={'transport':'http','endpoint':'http://localhost/mcp','credentials':{},'timeout_seconds':5,'max_result_chars':1000}
        async with mcp.session_manager.run():
            with patch('app.mcp.client.http_client',factory):
                result=await call(config,'huge',{});self.assertTrue(result['truncated']);self.assertEqual(len(result['preview']),1000)
                error=await call(config,'fail',{});self.assertTrue(error['is_error']);self.assertEqual(len(calls),2)

    async def test_transport_timeout_has_no_automatic_retry(self):
        import httpx
        from mcp.server.fastmcp import FastMCP
        from app.mcp.client import call
        mcp=FastMCP('timeout-test',json_response=True,stateless_http=True);calls=[]
        @mcp.tool()
        async def slow()->str:
            calls.append(1);await asyncio.sleep(2);return 'done'
        asgi=mcp.streamable_http_app()
        def factory(headers=None,timeout=None,auth=None):return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi),headers=headers,timeout=timeout,auth=auth)
        config={'transport':'http','endpoint':'http://localhost/mcp','credentials':{},'timeout_seconds':.1,'max_result_chars':1000}
        async with mcp.session_manager.run():
            with patch('app.mcp.client.http_client',factory):
                with self.assertRaises(Exception) as caught:await call(config,'slow',{})
        self.assertEqual(caught.exception.status_code,502)
        self.assertLessEqual(len(calls),1)
