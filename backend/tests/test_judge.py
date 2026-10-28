import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

import test_case_generations as fixtures
from app.judge_protocol import parse_report
from app.judge_runs import execute_judge
from app.main import create_app
from app.models import CaseDraft, CaseGeneration, JudgeRun

SETTINGS='/api/settings/judge'
KEY='private-judge-test-key'


def report_item(id,score=4):
    return {'case_id':id,**{name:{'score':score,'reason':'基于当前快照的评估理由'} for name in ('relevance','executability','verifiability')},'hallucinations':[],'suggestions':[]}


class JudgeProtocolTests(unittest.TestCase):
    def test_order_threshold_and_hallucination_rule(self):
        ids=[str(uuid4()),str(uuid4())]
        a,b=report_item(ids[0],3),report_item(ids[1],5)
        b['hallucinations']=['阈值缺乏依据，需人工确认']
        result=parse_report('```json\n'+json.dumps({'items':[b,a]})+'\n```',ids)
        self.assertEqual([item['case_id'] for item in result['items']],ids)
        self.assertEqual(result['usable_count'],1);self.assertEqual(result['suspect_count'],1)
        self.assertTrue(result['items'][0]['usable']);self.assertFalse(result['items'][1]['usable'])
        a['verifiability']['score']=2
        self.assertEqual(parse_report(json.dumps({'items':[a,b]}),ids)['usable_count'],0)

    def test_bad_identity_scores_text_and_extra_fields_rejected(self):
        id=str(uuid4());other=str(uuid4());good=report_item(id)
        bodies=[{}, {'items':[]},{'items':[report_item(other)]},{'items':[good,good]}, {'items':[good],'usable':True}]
        for score in [True,3.0,'3',-1,6,float('nan'),float('inf')]:
            row=copy.deepcopy(good);row['relevance']['score']=score;bodies.append({'items':[row]})
        for reason in [' ','x'*601,'\ud800','\x00']:
            row=copy.deepcopy(good);row['relevance']['reason']=reason;bodies.append({'items':[row]})
        row=copy.deepcopy(good);row['usable']=True;bodies.append({'items':[row]})
        row=copy.deepcopy(good);row['hallucinations']=['x']*6;bodies.append({'items':[row]})
        for body in bodies:
            with self.assertRaises(HTTPException):parse_report(json.dumps(body),[id])
        with self.assertRaises(HTTPException):parse_report('x'*100001,[id])


class JudgeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'judge.sqlite3'
        self.calls=[]
        def respond(request):
            self.calls.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':'AITC_OK'},'finish_reason':'stop'}]})
        self.transport=httpx.MockTransport(respond)
        self.client=TestClient(create_app(self.path,llm_transport=self.transport,judge_transport=self.transport)).__enter__()
        self.cfg={'use_generation_model':False,'base_url':'https://judge.invalid/v1','model':'judge-test','api_key':KEY,'mock_mode':False,'timeout_seconds':5}

    def tearDown(self):self.client.__exit__(None,None,None);self.temp.cleanup()

    def test_default_generation_fallback_without_copying_key(self):
        self.assertEqual(self.client.get(SETTINGS).json()['effective_source'],'unconfigured')
        self.assertEqual(self.client.post(SETTINGS+'/test').status_code,400)
        self.client.put('/api/settings/generation',json={'base_url':'https://gen.invalid/v1','model':'writer','api_key':'private-generation-key','mock_mode':False,'timeout_seconds':10})
        current=self.client.get(SETTINGS).json()
        self.assertTrue(current['use_generation_model']);self.assertFalse(current['configured'])
        self.assertEqual(current['effective_source'],'generation');self.assertEqual(current['effective_model'],'writer')
        self.assertFalse(current['has_api_key']);self.assertNotIn('private-generation-key',json.dumps(current))
        self.assertEqual(self.client.post(SETTINGS+'/test').status_code,200)
        self.assertEqual(self.calls[0].headers['Authorization'],'Bearer private-generation-key')
        self.assertEqual(json.loads(self.calls[0].content)['max_tokens'],64)

    def test_independent_key_lifecycle_does_not_modify_other_settings(self):
        before=self.client.get('/api/settings/generation').json()
        self.assertEqual(self.client.put(SETTINGS,json=self.cfg).status_code,200)
        self.assertEqual(self.client.get('/api/settings/generation').json(),before)
        self.client.put(SETTINGS,json={**self.cfg,'api_key':''})
        self.assertTrue(self.client.get(SETTINGS).json()['has_api_key'])
        response=self.client.put(SETTINGS,json={**self.cfg,'base_url':'https://other.invalid/v1','api_key':''})
        self.assertEqual(response.status_code,400)
        self.client.put(SETTINGS,json={**self.cfg,'api_key':'replacement-key'})
        self.client.post(SETTINGS+'/test');self.assertEqual(self.calls[-1].headers['Authorization'],'Bearer replacement-key')
        self.client.put(SETTINGS,json={**self.cfg,'api_key':'','clear_api_key':True})
        self.assertFalse(self.client.get(SETTINGS).json()['has_api_key'])
        self.assertNotIn(KEY,json.dumps(self.client.get(SETTINGS).json()))

    def test_validation_redaction_and_restart_mock(self):
        for override in [{'base_url':'https://a.invalid/v1/chat/completions'},{'base_url':'https://a.invalid/v1/rerank'},{'base_url':'https://a.invalid?'},
                         {'model':'\ud800'},{'api_key':'中 文'},{'use_generation_model':'true'},{'timeout_seconds':True},{'timeout_seconds':61},{'clear_api_key':True}]:
            response=self.client.put(SETTINGS,content=json.dumps({**self.cfg,**override}).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text);self.assertNotIn(KEY,response.text)
        self.client.put(SETTINGS,json={**self.cfg,'mock_mode':True})
        self.client.__exit__(None,None,None);self.client=TestClient(create_app(self.path)).__enter__()
        current=self.client.get(SETTINGS).json();self.assertEqual(current['effective_source'],'independent');self.assertTrue(current['effective_mock_mode'])
        self.assertEqual(self.client.post(SETTINGS+'/test').json()['mode'],'mock');self.assertEqual(self.calls,[])


class JudgeRunTests(unittest.TestCase):
    tearDown=fixtures.CaseGenerationTests.tearDown
    configure=fixtures.CaseGenerationTests.configure
    payload=fixtures.CaseGenerationTests.payload

    def setUp(self):
        fixtures.CaseGenerationTests.setUp(self);self.configure()
        response=self.client.post(self.base,json=self.payload());self.assertEqual(response.status_code,201,response.text)
        self.generation=response.json();self.gid=self.generation['id'];self.url=self.base+'/'+self.gid+'/judge-runs'
        self.calls=[];self.judge_behavior='success';self.gate=threading.Event();self.gate.set();self.entered=threading.Event()
        async def respond(request):
            self.calls.append(request);self.entered.set()
            while not self.gate.is_set():await asyncio.sleep(.01)
            if isinstance(self.judge_behavior,int):return httpx.Response(self.judge_behavior,text=KEY)
            if self.judge_behavior=='timeout':raise httpx.ReadTimeout(KEY,request=request)
            if self.judge_behavior=='oversize':return httpx.Response(200,content=b'x'*(1024*1024+1))
            data=json.loads(json.loads(request.content)['messages'][1]['content'])
            items=[report_item(case['case_id']) for case in data['cases']]
            if self.judge_behavior=='partial':items=items[:-1]
            if self.judge_behavior=='foreign':items[0]['case_id']=str(uuid4())
            reply=json.dumps({'items':items})
            return httpx.Response(200,json={'choices':[{'message':{'content':reply},'finish_reason':'stop'}]})
        self.transport=httpx.MockTransport(respond);self.app.state.judge_transport=self.transport
        self.cfg={'use_generation_model':False,'base_url':'https://judge.invalid/v1','model':'judge-test','api_key':KEY,'mock_mode':False,'timeout_seconds':5}
        self.assertEqual(self.client.put(SETTINGS,json=self.cfg).status_code,200)

    def submit(self,id=None,retry_of=None,url=None):
        response=self.client.post(url or self.url,json={'request_id':id or str(uuid4()),'retry_of':retry_of})
        self.assertIn(response.status_code,[200,202],response.text)
        return response.json()

    def wait(self,id,status='succeeded'):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            response=self.client.get(self.url+'/'+id);self.assertEqual(response.status_code,200,response.text)
            run=response.json()
            if run['status'] not in ('queued','running'):
                self.assertEqual(run['status'],status,run);return run
            time.sleep(.03)
        self.fail('Judge timeout')

    def test_success_frozen_current_input_scores_rules_and_no_review_mutation(self):
        reviews_before=self.client.get(self.base+'/'+self.gid+'/reviews').json()
        run=self.wait(self.submit()['id'])
        self.assertEqual(run['model_source'],'independent');self.assertEqual(run['report']['checked_count'],3)
        self.assertEqual(run['report']['usable_count'],3);self.assertEqual(run['report']['average_score'],4)
        self.assertFalse(run['outdated']);self.assertEqual(run['prompt_version'],'case-judge-v1')
        self.assertTrue(any(f['code']=='duplicate_content' for f in run['input_snapshot']['rule_findings']))
        self.assertEqual(self.client.get(self.base+'/'+self.gid+'/reviews').json(),reviews_before)
        self.assertEqual(self.client.get(self.base+'/'+self.gid).json(),self.generation)
        self.assertNotIn(KEY,json.dumps(run));self.assertNotIn('fake-generation-key',json.dumps(run))
        body=json.loads(self.calls[0].content);self.assertEqual(body['model'],'judge-test');self.assertEqual(body['max_tokens'],6144)
        self.assertEqual(self.calls[0].headers['Authorization'],'Bearer '+KEY)
        self.assertEqual(len(self.requests),1)

    def test_active_mutex_and_same_id_replay(self):
        self.gate.clear()
        run=self.submit();self.assertTrue(self.entered.wait(3))
        self.assertEqual(self.submit(run['id'])['id'],run['id'])
        response=self.client.post(self.url,json={'request_id':str(uuid4())});self.assertEqual(response.status_code,409)
        self.gate.set();self.wait(run['id']);self.submit(run['id']);self.assertEqual(len(self.calls),1)

    def test_failure_retry_creates_new_history_and_preserves_success(self):
        first=self.wait(self.submit()['id']);self.judge_behavior=429
        failed=self.wait(self.submit()['id'],'failed');self.assertNotIn(KEY,json.dumps(failed))
        self.assertIsNone(failed['report']);self.assertEqual(self.client.get(self.url+'/'+first['id']).json()['report'],first['report'])
        self.judge_behavior='success';retried=self.wait(self.submit(retry_of=failed['id'])['id'])
        self.assertEqual(retried['retry_of'],failed['id']);self.assertEqual(len(self.client.get(self.url).json()),3)
        self.assertEqual(self.client.get(self.url+'/'+failed['id']).json()['status'],'failed')
        self.assertEqual(self.client.get(self.base+'/'+self.gid).json(),self.generation)

    def test_malformed_and_oversize_reports_fail_without_partial_results(self):
        for behavior in ['partial','foreign','oversize','timeout']:
            self.judge_behavior=behavior
            run=self.wait(self.submit()['id'],'failed');self.assertIsNone(run['report']);self.assertNotIn(KEY,json.dumps(run))
        self.assertEqual(len(self.calls),4);self.assertEqual(self.client.get(self.base+'/'+self.gid).json(),self.generation)

    def edit_case(self):
        case=self.generation['cases'][0];fields={key:value for key,value in case.items() if key!='id'};fields['title']='人工修改后的标题'
        response=self.client.put(self.base+'/'+self.gid+'/reviews/'+case['id'],json={'expected_revision':0,'action':'edit','fields':fields})
        self.assertEqual(response.status_code,200,response.text)

    def test_edit_during_judge_keeps_old_snapshot_marks_outdated(self):
        self.gate.clear();run=self.submit();self.assertTrue(self.entered.wait(3));self.edit_case();self.gate.set()
        old=self.wait(run['id']);self.assertTrue(old['outdated'])
        self.assertEqual(old['input_snapshot']['cases'][0]['fields']['title'],self.generation['cases'][0]['title'])
        fresh=self.wait(self.submit()['id']);self.assertFalse(fresh['outdated'])
        self.assertEqual(fresh['input_snapshot']['cases'][0]['fields']['title'],'人工修改后的标题')
        self.assertEqual(fresh['input_snapshot']['cases'][0]['review_revision'],1)
        self.assertEqual(self.client.get(self.url+'/'+old['id']).json()['report'],old['report'])

    def test_fallback_generation_transport_and_mock_are_explicit(self):
        response=self.client.put(SETTINGS,json={**self.cfg,'use_generation_model':True,'api_key':''})
        self.assertEqual(response.status_code,200,response.text)
        self.app.state.llm_transport=self.transport
        run=self.wait(self.submit()['id']);self.assertEqual(run['model_source'],'generation')
        self.assertEqual(self.calls[-1].headers['Authorization'],'Bearer fake-generation-key')
        self.client.put(SETTINGS,json={**self.cfg,'mock_mode':True})
        before=len(self.calls);mock=self.wait(self.submit()['id'])
        self.assertTrue(mock['mock_mode']);self.assertEqual(mock['model_source'],'independent')
        self.assertEqual(mock['report']['average_score'],3);self.assertEqual(len(self.calls),before)

    def test_restart_interruption_never_automatically_retries(self):
        self.gate.clear();run=self.submit();self.assertTrue(self.entered.wait(3))
        self.client.__exit__(None,None,None)
        self.app=create_app(self.path,judge_transport=self.transport);self.client=TestClient(self.app).__enter__()
        interrupted=self.client.get(self.url+'/'+run['id']).json()
        self.assertEqual(interrupted['status'],'interrupted');self.assertTrue(interrupted['model_call_pending'])
        self.assertEqual(len(self.calls),1)
        self.gate.set();self.wait(self.submit(retry_of=run['id'])['id']);self.assertEqual(len(self.calls),2)

    def test_queued_configuration_change_skips_paid_call(self):
        gate=threading.Event();entered=threading.Event();original=execute_judge
        async def delay(app,id):
            entered.set()
            while not gate.is_set():await asyncio.sleep(.01)
            await original(app,id)
        with patch('app.judge_runs.execute_judge',side_effect=delay):
            run=self.submit();self.assertTrue(entered.wait(3))
            self.client.put(SETTINGS,json={**self.cfg,'model':'changed'})
            gate.set();failed=self.wait(run['id'],'failed')
        self.assertIn('配置已变化',failed['error_message']);self.assertEqual(self.calls,[])

    def test_queued_input_change_skips_paid_call(self):
        gate=threading.Event();entered=threading.Event();original=execute_judge
        async def delay(app,id):
            entered.set()
            while not gate.is_set():await asyncio.sleep(.01)
            await original(app,id)
        with patch('app.judge_runs.execute_judge',side_effect=delay):
            run=self.submit();self.assertTrue(entered.wait(3));self.edit_case();gate.set();failed=self.wait(run['id'],'failed')
        self.assertIn('草稿或依据已变化',failed['error_message']);self.assertEqual(self.calls,[])

    def test_project_scope_retry_scope_validation_and_cascade(self):
        self.judge_behavior=401;failed=self.wait(self.submit()['id'],'failed')
        other=self.client.post('/api/projects',json={'name':'其他项目'}).json()['id']
        foreign=self.url.replace('/projects/'+str(self.project)+'/','/projects/'+str(other)+'/')
        self.assertEqual(self.client.get(foreign).status_code,404)
        second=self.client.post(self.base,json=self.payload()).json()['id'];url=self.base+'/'+second+'/judge-runs'
        self.assertEqual(self.client.post(url,json={'request_id':str(uuid4()),'retry_of':failed['id']}).status_code,409)
        self.assertEqual(self.client.post(url,json={'request_id':failed['id']}).status_code,409)
        self.assertEqual(self.client.post(self.url,json={'request_id':'invalid','api_key':KEY}).status_code,422)
        self.client.delete(self.requirement_url+'/'+str(self.requirement_id))
        with self.app.state.session_factory() as session:self.assertIsNone(session.get(JudgeRun,failed['id']))

    def test_input_limit_stops_before_model(self):
        with self.app.state.session_factory() as session:
            from sqlalchemy import select
            rows=list(session.scalars(select(CaseDraft).where(CaseDraft.generation_id==self.gid)))
            for i in range(5):
                row=CaseDraft(id=str(uuid4()),generation_id=self.gid,sort_order=i+3,title=str(i),priority='P1',case_type='functional',is_smoke=False,precondition='',steps=['步骤'],expected_result='结果')
                session.add(row);rows.append(row)
            for row in rows:
                row.steps=['中'*1000]*20;row.precondition='中'*2000;row.expected_result='中'*4000
            session.commit()
        response=self.client.post(self.url,json={'request_id':str(uuid4())})
        self.assertEqual(response.status_code,400,response.text);self.assertEqual(self.calls,[])
