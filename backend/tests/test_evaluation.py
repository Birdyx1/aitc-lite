import asyncio,json,tempfile,threading,time,unittest
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select
from app.main import create_app
from app.models import EvaluationRun,EvaluationSample,EvaluationResult,Requirement,CaseGeneration,CaseDraft,CaseReview,ExecutionTask
from app.workflows.case_generation import mock_reply
from app.judge_protocol import mock_report
from app.evaluation.metrics import match_points,metrics,duplicates


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'eval.sqlite'
        self.gen_calls=[];self.judge_calls=[];self.mode='normal';self.gate=threading.Event();self.gate.set();self.entered=threading.Event()
        async def respond(request):
            payload=json.loads(request.content);body=json.loads(payload['messages'][-1]['content']);is_judge='cases' in body
            (self.judge_calls if is_judge else self.gen_calls).append(payload)
            if not is_judge:
                self.entered.set()
                while not self.gate.is_set():await asyncio.sleep(.01)
            if (is_judge and self.mode=='judge_error') or (not is_judge and self.mode=='gen_error'):return httpx.Response(429,text='eval-secret-key')
            if not is_judge and self.mode=='bad_generation':reply='not valid JSON eval-secret-key'
            elif is_judge:reply=mock_report(body)
            else:
                state={**body,'source':'mock','skill_snapshot':body.get('specialist_skills',[])}
                if 'knowledge_context' in body:state['knowledge']={'sources':body['knowledge_context']}
                output=json.loads(mock_reply(state))
                for case in output['cases']:case['expected_result']='取消订单后状态为已取消。'
                reply=json.dumps(output,ensure_ascii=False)
            return httpx.Response(200,json={'choices':[{'message':{'content':reply},'finish_reason':'length' if self.mode=='length' else 'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}})
        self.transport=httpx.MockTransport(respond);self.app=create_app(self.path,llm_transport=self.transport)
        self.client=TestClient(self.app).__enter__();self.pid=self.client.post('/api/projects',json={'name':'评测项目'}).json()['id']
        self.root=f'/api/projects/{self.pid}/evaluations'
        self.client.put('/api/settings/generation',json={'base_url':'https://eval.invalid/v1','model':'qa-gen','api_key':'eval-secret-key','mock_mode':False,'timeout_seconds':10}).raise_for_status()
        self.sample=self.add()

    def tearDown(self):self.gate.set();self.client.__exit__(None,None,None);self.temp.cleanup()
    def sample_input(self,**changes):return {'name':'订单取消','requirement_title':'订单规则','requirement_content':'未支付的订单可以取消，取消后状态为已取消。','feature':{'module':'订单','feature':'取消订单','description':'验证取消订单','acceptance_criteria':'状态为已取消'},'gold_points':['状态为已取消'],**changes}
    def add(self,**changes):
        response=self.client.post(self.root+'/samples',json=self.sample_input(**changes));self.assertEqual(response.status_code,201,response.text);return response.json()
    def payload(self,**changes):return {'run_id':str(uuid4()),'name':'基线评测','sample_ids':[self.sample['id']],'mock_mode':False,**changes}
    def submit(self,**changes):
        response=self.client.post(self.root+'/runs',json=self.payload(**changes));self.assertEqual(response.status_code,202,response.text);return response.json()
    def wait(self,run,status='completed'):
        until=time.monotonic()+10
        while time.monotonic()<until:
            value=self.client.get(self.root+'/runs/'+run['id']).json()
            if value['status'] not in ('queued','running','stopping'):
                self.assertEqual(value['status'],status,value);return value
            time.sleep(.03)
        self.fail(str(value))
    def result(self,run,index=0):return self.client.get(self.root+'/runs/'+run['id']+'/results/'+run['results'][index]['id']).json()

    def test_real_protocol_generation_judge_usage_and_isolation(self):
        run=self.wait(self.submit());result=self.result(run)
        self.assertEqual((len(self.gen_calls),len(self.judge_calls)),(1,1))
        self.assertEqual(result['status'],'succeeded');self.assertEqual(len(result['cases']),3)
        self.assertTrue(result['matches'][0]['matched']);self.assertEqual(result['matches'][0]['evidence'][0]['field'],'expected_result')
        self.assertEqual(run['metrics']['scenario_recall']['percent'],100)
        self.assertEqual(run['metrics']['token_usage']['known_total_tokens'],60)
        self.assertEqual(run['metrics']['judge_completion']['numerator'],3)
        self.assertNotIn('gold_points',json.dumps(self.gen_calls[0]['messages']))
        self.assertNotIn('eval-secret-key',json.dumps(run));self.assertNotIn('eval-secret-key',json.dumps(result))
        with self.app.state.session_factory() as session:
            for model in (Requirement,CaseGeneration,CaseDraft,CaseReview,ExecutionTask):self.assertEqual(list(session.scalars(select(model))),[])

    def test_mock_override_never_calls_models_and_preserves_global_settings(self):
        run=self.wait(self.submit(mock_mode=True));self.assertTrue(run['config_snapshot']['mock_mode'])
        self.assertEqual((self.gen_calls,self.judge_calls),([],[]))
        self.assertFalse(self.client.get('/api/settings/generation').json()['mock_mode'])
        self.assertEqual(self.result(run)['judge_report']['items'][0]['relevance']['score'],3)
        self.assertIsNone(run['metrics']['token_usage']['known_total_tokens'])

    def test_independent_judge_config_and_transport(self):
        self.app.state.judge_transport=self.transport
        self.client.put('/api/settings/judge',json={'use_generation_model':False,'base_url':'https://judge.invalid/v1','model':'qa-judge','api_key':'judge-secret','mock_mode':False,'timeout_seconds':10}).raise_for_status()
        run=self.wait(self.submit());self.assertEqual(run['config_snapshot']['judge']['source'],'independent')
        self.assertEqual(self.judge_calls[0]['model'],'qa-judge');self.assertNotIn('judge-secret',json.dumps(run))

    def test_generation_failure_denominators_and_continue_batch(self):
        self.mode='gen_error';second=self.add(name='第二项',gold_points=[])
        run=self.wait(self.submit(sample_ids=[self.sample['id'],second['id']]))
        self.assertEqual(run['metrics']['generation_success'],{'numerator':0,'denominator':2,'percent':0.0})
        self.assertEqual(run['metrics']['scenario_recall']['denominator'],1);self.assertEqual(len(self.gen_calls),2);self.assertEqual(self.judge_calls,[])
        self.assertIsNone(run['metrics']['judge_usable']['percent'])

    def test_judge_failure_retains_generation_and_recall(self):
        self.mode='judge_error';run=self.wait(self.submit());result=self.result(run)
        self.assertEqual(result['status'],'partial');self.assertEqual(result['generation_status'],'succeeded');self.assertEqual(len(result['cases']),3)
        self.assertEqual(run['metrics']['generation_success']['percent'],100);self.assertEqual(run['metrics']['judge_completion']['percent'],0)
        self.assertIsNone(run['metrics']['judge_usable']['percent']);self.assertEqual(run['metrics']['scenario_recall']['percent'],100)

    def test_invalid_generation_does_not_call_judge(self):
        self.mode='bad_generation';run=self.wait(self.submit());self.assertEqual(self.result(run)['generation_status'],'failed');self.assertFalse(self.result(run)['model_call_pending']);self.assertEqual(self.judge_calls,[])

    def test_no_gold_points_has_null_recall(self):
        sample=self.add(gold_points=[]);run=self.wait(self.submit(sample_ids=[sample['id']]))
        self.assertIsNone(run['metrics']['scenario_recall']['percent']);self.assertEqual(self.result(run)['matches'],[])

    def test_sample_edit_delete_keeps_run_snapshot(self):
        run=self.wait(self.submit());before=self.result(run)
        edit=self.client.put(self.root+'/samples/'+self.sample['id'],json=self.sample_input(expected_revision=1,requirement_content='新的订单规则'))
        self.assertEqual(edit.status_code,200,edit.text)
        self.assertEqual(self.client.delete(self.root+'/samples/'+self.sample['id']+'?expected_revision=2').status_code,204)
        self.assertEqual(self.result(run),before)

    def test_sample_revision_and_schema_validation(self):
        self.assertEqual(self.client.put(self.root+'/samples/'+self.sample['id'],json=self.sample_input(expected_revision=0)).status_code,409)
        self.assertEqual(self.client.delete(self.root+'/samples/'+self.sample['id']+'?expected_revision=99').status_code,409)
        for changes in ({'name':' '},{'gold_points':['a']},{'gold_points':['重复',' 重复 ']},{'gold_points':['x'*501]},{'gold_points':['测试']*21},{'requirement_content':'\ud800'},{'feature':{'module':'m','feature':'f','description':'a\x00b'}}):
            response=self.client.post(self.root+'/samples',content=json.dumps(self.sample_input(**changes)),headers={'Content-Type':'application/json'});self.assertEqual(response.status_code,422,response.text)

    def test_run_idempotency_and_mutual_exclusion(self):
        self.gate.clear();data=self.payload();response=self.client.post(self.root+'/runs',json=data);self.assertEqual(response.status_code,202)
        self.assertTrue(self.entered.wait(3));again=self.client.post(self.root+'/runs',json=data);self.assertEqual(again.status_code,200)
        self.assertEqual(self.client.post(self.root+'/runs',json={**data,'name':'changed'}).status_code,409)
        self.assertEqual(self.client.post(self.root+'/runs',json=self.payload()).status_code,409)
        self.gate.set();self.wait(response.json());self.assertEqual(len(self.gen_calls),1)

    def test_configuration_change_stops_next_model_and_samples(self):
        second=self.add(name='样本二');self.gate.clear();run=self.submit(sample_ids=[self.sample['id'],second['id']]);self.assertTrue(self.entered.wait(3))
        self.client.put('/api/settings/generation',json={'base_url':'https://eval.invalid/v1','model':'new-model','mock_mode':False,'timeout_seconds':10})
        self.gate.set();done=self.wait(run,'interrupted');self.assertEqual(len(self.gen_calls),1);self.assertEqual(self.judge_calls,[])
        self.assertEqual(self.result(done)['generation_status'],'succeeded')

    def test_stop_keeps_inflight_result_does_not_start_next_sample(self):
        second=self.add(name='样本二');self.gate.clear();run=self.submit(sample_ids=[self.sample['id'],second['id']]);self.assertTrue(self.entered.wait(3))
        stop=self.client.post(self.root+'/runs/'+run['id']+'/stop');self.assertEqual(stop.json()['status'],'stopping')
        self.gate.set();done=self.wait(run,'interrupted');self.assertEqual(len(self.gen_calls),1);self.assertEqual(self.judge_calls,[])
        self.assertEqual(self.result(done)['generation_status'],'succeeded')

    def test_restart_interrupts_no_automatic_calls(self):
        self.gate.clear();run=self.submit();self.assertTrue(self.entered.wait(3))
        self.client.__exit__(None,None,None);self.gate.set()
        self.app=create_app(self.path,llm_transport=self.transport);self.client=TestClient(self.app).__enter__()
        done=self.client.get(self.root+'/runs/'+run['id']).json();self.assertEqual(done['status'],'interrupted');self.assertEqual(len(self.gen_calls),1)
        self.assertTrue(self.result(done)['model_call_pending'])

    def test_compare_common_only_and_changed_samples_excluded(self):
        extra=self.add(name='仅左侧');left=self.wait(self.submit(sample_ids=[self.sample['id'],extra['id']],strategy='full'))
        right=self.wait(self.submit(strategy='quick',name='冒烟对比'))
        compare=self.client.get(self.root+f"/compare?left={left['id']}&right={right['id']}").json()
        self.assertEqual(compare['common_count'],1);self.assertEqual(compare['left_only_ids'],[extra['id']])
        self.assertEqual(compare['common_metrics']['left']['sample_count'],1);self.assertEqual(compare['common_metrics']['left']['generated_case_count'],3)
        self.assertEqual(compare['common_metrics']['right']['generated_case_count'],1)
        self.assertTrue(any(row['field']=='strategy' for row in compare['config_differences']))
        self.client.put(self.root+'/samples/'+self.sample['id'],json=self.sample_input(expected_revision=1,gold_points=['新的标准点']))
        changed=self.wait(self.submit())
        data=self.client.get(self.root+f"/compare?left={left['id']}&right={changed['id']}").json()
        self.assertEqual(data['common_count'],0);self.assertIn(self.sample['id'],data['changed_sample_ids']);self.assertIsNone(data['common_metrics']['left']['generation_success']['percent'])

    def test_project_scope_for_samples_runs_results_compare(self):
        other=self.client.post('/api/projects',json={'name':'其他'}).json()['id'];foreign=f'/api/projects/{other}/evaluations';run=self.wait(self.submit())
        self.assertEqual(self.client.get(foreign+'/samples').json(),[])
        self.assertEqual(self.client.get(foreign+'/runs/'+run['id']).status_code,404)
        self.assertEqual(self.client.get(foreign+'/runs/'+run['id']+'/results/'+run['results'][0]['id']).status_code,404)
        self.assertEqual(self.client.get(foreign+f"/compare?left={run['id']}&right={run['id']}").status_code,404)
        self.assertEqual(self.client.post(foreign+'/runs',json=self.payload()).status_code,404)

    def test_skill_and_mock_rag_context_snapshots_no_external_calls(self):
        self.client.post(f'/api/projects/{self.pid}/knowledge-documents',json={'document_id':str(uuid4()),'title':'订单','content':'取消订单规则：未支付订单可以取消，状态为已取消。','source_filename':'demo.md'}).raise_for_status()
        run=self.wait(self.submit(mock_mode=True,use_rag=True,skill_ids=['api'],rag_allow_empty=False))
        result=self.result(run);self.assertEqual(result['knowledge']['mode'],'mock_keyword_only')
        self.assertTrue(result['cases'][0]['knowledge_refs']);self.assertEqual(result['cases'][0]['skill_refs'],['api'])
        self.assertTrue(run['config_snapshot']['skills'][0]['content_hash']);self.assertEqual(self.gen_calls,[])

    def test_rag_empty_policy_and_explicit_degradation(self):
        strict=self.wait(self.submit(mock_mode=True,use_rag=True));self.assertEqual(self.result(strict)['generation_status'],'failed');self.assertEqual(self.result(strict)['knowledge']['sources'],[])
        allowed=self.wait(self.submit(mock_mode=True,use_rag=True,rag_allow_empty=True))
        self.assertTrue(self.result(allowed)['knowledge']['degraded']);self.assertEqual(self.result(allowed)['knowledge']['sources'],[])

    def test_project_delete_cascades_all_eval_tables(self):
        self.wait(self.submit(mock_mode=True));self.client.delete('/api/projects/'+str(self.pid))
        with self.app.state.session_factory() as session:
            for model in (EvaluationSample,EvaluationRun,EvaluationResult):self.assertEqual(list(session.scalars(select(model))),[])

    def test_run_validation_and_real_mode_not_silent_mock(self):
        for changes in ({'sample_ids':[]},{'sample_ids':[self.sample['id']]*2},{'strategy':'bad'},{'skill_ids':['unknown']},{'mock_mode':'yes'},{'rag_rerank':True}):
            response=self.client.post(self.root+'/runs',json=self.payload(**changes));self.assertEqual(response.status_code,422,response.text)
        self.client.put('/api/settings/generation',json={'base_url':'https://eval.invalid/v1','model':'qa-gen','mock_mode':True,'timeout_seconds':10})
        self.assertEqual(self.client.post(self.root+'/runs',json=self.payload()).status_code,400)

    def test_real_rag_reuses_hybrid_context_and_citation_validation(self):
        from unittest.mock import patch
        calls=[]
        async def retrieve(pid,payload,app,session):
            from app.knowledge.bm25 import current_signature
            calls.append((pid,payload))
            return SimpleNamespace(model_dump=lambda **kw:{'hits':[{'document_id':'doc','document_title':'订单知识','document_revision':1,'chunk_id':'chunk',
                'content':'订单可以取消。','rank':1,'rrf_rank':1,'bm25_rank':1,'vector_rank':1}],
                'corpus_signature':current_signature(session,pid),'mode':'fused','degraded':False,'mock_mode':False,'rerank':{'status':'applied'}})
        with patch('app.evaluation.runtime.retrieve_hybrid',retrieve):
            run=self.wait(self.submit(use_rag=True,rag_rerank=True))
        result=self.result(run);self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],self.pid)
        self.assertTrue(calls[0][1].use_rerank);self.assertEqual(result['knowledge']['mode'],'fused')
        self.assertEqual(result['cases'][0]['knowledge_refs'],['K1'])
        self.assertEqual(result['judge_input']['knowledge']['sources'][0]['citation_id'],'K1')

    def test_single_sample_configuration_change_is_interrupted(self):
        self.gate.clear();run=self.submit();self.assertTrue(self.entered.wait(3))
        self.client.put('/api/settings/generation',json={'base_url':'https://eval.invalid/v1','model':'changed','mock_mode':False,'timeout_seconds':10})
        self.gate.set();done=self.wait(run,'interrupted');self.assertEqual(self.judge_calls,[])
        self.assertEqual(self.result(done)['generation_status'],'succeeded')

    def test_compare_active_run_is_rejected(self):
        left=self.wait(self.submit());self.gate.clear();right=self.submit();self.assertTrue(self.entered.wait(3))
        response=self.client.get(self.root+f"/compare?left={left['id']}&right={right['id']}")
        self.assertEqual(response.status_code,409);self.gate.set();self.wait(right)

    def test_retrieval_failure_before_model_has_no_unknown_model_usage(self):
        run=self.wait(self.submit(mock_mode=True,use_rag=True))
        self.assertEqual(self.result(run)['usage'],{});self.assertEqual(run['metrics']['token_usage']['unknown_usage_calls'],0)

    def test_incomplete_response_keeps_returned_usage(self):
        self.mode='length';run=self.wait(self.submit())
        self.assertEqual(self.result(run)['generation_status'],'failed')
        self.assertEqual(run['metrics']['token_usage']['known_total_tokens'],30)
        self.assertEqual(self.judge_calls,[])


class EvaluationMetricTests(unittest.TestCase):
    def test_match_requires_one_field_and_preserves_evidence(self):
        fields={'title':'订单','precondition':'取消','steps':[' ＡＢＣ 状态 '],'expected_result':'已取消'}
        cases=[{'case_id':'id1','fields':fields}]
        matches=match_points(['abc状态','取消已取消','不存在规则'],cases)
        self.assertTrue(matches[0]['matched']);self.assertEqual(matches[0]['evidence'][0]['field'],'steps[0]')
        self.assertFalse(matches[1]['matched']);self.assertFalse(matches[2]['matched'])
    def test_duplicate_count_union_and_unknown_denominators(self):
        cases=[{'case_id':id,'fields':{'title':title,'precondition':'','steps':['同一步骤'],'expected_result':'同一预期'}} for id,title in [('a','标题'),('b',' 标题 ')]]
        findings=duplicates(cases)
        row=SimpleNamespace(generation_status='succeeded',judge_status='failed',judge_report=None,cases=cases,sample_snapshot={'content':{'gold_points':[]}},matches=[],duplicate_findings=findings,timings={},usage={'generation':None})
        result=metrics([row]);self.assertEqual(result['duplicate_rate']['numerator'],1)
        self.assertIsNone(result['scenario_recall']['percent']);self.assertIsNone(result['judge_usable']['percent']);self.assertEqual(result['token_usage']['unknown_usage_calls'],1)
