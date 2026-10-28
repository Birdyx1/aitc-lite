import unittest
from uuid import uuid4
from unittest.mock import patch
from sqlalchemy import select

import test_reviews as fixtures
from app.models import (Requirement,FeatureSheet,GenerationSettings,EmbeddingSettings,JudgeSettings,
    EvaluationSample,EvaluationRun,McpServer,McpBinding,ExecutionTask)


class WorkbenchTests(unittest.TestCase):
    tearDown=fixtures.ReviewTests.tearDown

    def setUp(self):
        fixtures.ReviewTests.setUp(self)
        self.root=f'/api/projects/{self.project_id}'

    def get(self,pid=None):
        response=self.client.get(f'/api/projects/{pid or self.project_id}/workbench')
        self.assertEqual(response.status_code,200,response.text);return response.json()

    def accept_all(self):
        for case in self.generation['cases']:
            self.client.put(self.base+'/'+case['id'],json={'action':'accept','expected_revision':0}).raise_for_status()

    def test_empty_project_next_requirement_and_no_false_execution(self):
        pid=self.client.post('/api/projects',json={'name':'空项目'}).json()['id']
        result=self.get(pid)
        self.assertEqual(result['requirements'],0);self.assertEqual(result['drafts'],0)
        self.assertEqual(result['next_action']['code'],'requirement')
        self.assertEqual(result['next_action']['path'],f'/projects/{pid}/requirements')
        self.assertIsNone(result['execution']['item_stats']['pass_rate'])
        self.assertEqual(result['evaluation']['real_runs'],0)

    def test_current_review_counts_and_scoped_data(self):
        result=self.get()
        self.assertEqual(result['requirements'],1);self.assertEqual(result['confirmed_requirements'],1)
        self.assertEqual(result['current_confirmed_features'],1)
        self.assertEqual(result['drafts'],3);self.assertEqual(result['review_counts']['pending'],3)
        self.assertEqual(result['generation_sources'],{'mock':1})
        self.assertEqual(result['next_action']['code'],'review')
        self.assertIn('/requirements/'+str(self.requirement_id)+'/cases',result['next_action']['path'])
        other=self.client.post('/api/projects',json={'name':'其他'}).json()['id']
        self.assertEqual(self.get(other)['drafts'],0)
        self.assertEqual(self.client.get('/api/projects/999999/workbench').status_code,404)

    def test_stale_requirement_invalidates_confirmation_and_current_generation(self):
        self.client.put(self.requirement_url,json={'title':'新标题','content':'规则已更新'})
        result=self.get()
        self.assertEqual(result['confirmed_requirements'],0)
        self.assertEqual(result['current_features_with_generation'],0)
        self.assertEqual(result['next_action']['code'],'confirmation')
        self.assertEqual(result['generation_batches'],1)

    def test_configuration_metadata_no_key_or_connection_probe(self):
        self.client.put('/api/settings/generation',json={'base_url':'https://saved.invalid/v1','model':'configured-model','api_key':'workbench-secret','mock_mode':False})
        self.client.put('/api/settings/embedding',json={'mock_mode':True})
        with patch('app.llm.chat_completion',side_effect=AssertionError('must not call provider')):
            result=self.get()
        self.assertEqual(result['configuration']['generation']['mode'],'real')
        self.assertEqual(result['configuration']['embedding']['mode'],'mock')
        self.assertEqual(result['configuration']['judge']['source'],'generation')
        self.assertNotIn('workbench-secret',str(result));self.assertNotIn('configured-model',str(result))
        self.assertIn('未验证',result['configuration']['generation']['note'])
        self.client.put('/api/settings/judge',json={'use_generation_model':False,'mock_mode':True})
        result=self.get();self.assertEqual(result['configuration']['judge']['source'],'independent');self.assertEqual(result['configuration']['judge']['mode'],'mock')

    def test_missing_configuration_does_not_claim_ready(self):
        with self.app.state.session_factory() as session:
            session.delete(session.get(GenerationSettings,1));session.commit()
        result=self.get()
        self.assertFalse(result['configuration']['generation']['ready'])
        self.assertEqual(result['configuration']['generation']['mode'],'unconfigured')
        self.assertFalse(result['configuration']['judge']['ready'])

    def test_execution_next_action_and_actual_manual_statistics(self):
        self.accept_all();result=self.get();self.assertEqual(result['next_action']['code'],'execution')
        task=self.client.post(self.root+'/execution-tasks',json={'task_id':str(uuid4()),'name':'人工记录','environment':'演示','case_ids':[self.draft_id]}).json()
        self.assertEqual(self.get()['next_action']['path'],f"/projects/{self.project_id}/execution-tasks/{task['id']}")
        item=task['items'][0]
        saved=self.client.put(self.root+f"/execution-tasks/{task['id']}/items/{item['id']}",json={'request_id':str(uuid4()),'expected_revision':0,'status':'blocked','actual_result':'演示：环境未准备'}).json()
        self.client.post(self.root+f"/execution-tasks/{task['id']}/complete",json={'expected_revision':saved['task_revision']}).raise_for_status()
        result=self.get();self.assertEqual(result['next_action']['code'],'evaluation_sample')
        self.assertEqual(result['execution']['item_stats']['blocked'],1)
        self.assertIsNone(result['execution']['item_stats']['pass_rate'])

    def test_mcp_only_current_project_enabled_catalog_grants_count(self):
        with self.app.state.session_factory() as session:
            server=McpServer(id=str(uuid4()),name='服务',transport='http',endpoint='http://localhost/mcp',revision=2,enabled=True,
                tools=[{'name':'query','description':'查询','inputSchema':{'type':'object'}}])
            session.add(server);session.flush()
            binding=McpBinding(project_id=self.project_id,server_id=server.id,server_revision=1,tools={'query':{'enabled':True,'mode':'readonly'}})
            session.add(binding);session.commit();sid=server.id
        self.assertEqual(self.get()['mcp']['enabled_tools'],0)
        with self.app.state.session_factory() as session:
            binding=session.get(McpBinding,(self.project_id,sid));binding.server_revision=2;session.commit()
        result=self.get();self.assertEqual(result['mcp']['enabled_tools'],1)
        self.assertIn('未启动',result['mcp']['note'])
        with self.app.state.session_factory() as session:session.get(McpServer,sid).enabled=False;session.commit()
        self.assertEqual(self.get()['mcp']['enabled_tools'],0)

    def test_reads_do_not_create_business_records(self):
        self.client.delete(self.requirement_url)
        with self.app.state.session_factory() as session:
            before={model.__tablename__:len(list(session.scalars(select(model)))) for model in (Requirement,EvaluationSample,EvaluationRun,ExecutionTask)}
        for _ in range(3):self.get()
        with self.app.state.session_factory() as session:
            after={model.__tablename__:len(list(session.scalars(select(model)))) for model in (Requirement,EvaluationSample,EvaluationRun,ExecutionTask)}
        self.assertEqual(before,after)

    def test_pending_review_precedes_generating_more_confirmed_features(self):
        req=self.client.post(self.root+'/requirements',json={'title':'两项范围','content':'甲功能与乙功能'}).json();rr=self.root+f"/requirements/{req['id']}"
        sheet=self.client.get(rr+'/features').json()
        sheet=self.client.put(rr+'/features',json={'expected_revision':0,'input_hash':sheet['current_hash'],'confirmed':True,
            'items':[{'module':'模块','feature':name,'description':name} for name in ['甲功能','乙功能']]}).json()
        result=self.client.post(rr+'/case-generations',json={'request_id':str(uuid4()),'feature_id':sheet['items'][0]['id'],
            'expected_revision':sheet['revision'],'input_hash':sheet['current_hash'],'strategy':'full'})
        self.assertEqual(result.status_code,201,result.text)
        data=self.get();self.assertGreater(data['current_confirmed_features'],data['current_features_with_generation'])
        self.assertEqual(data['next_action']['code'],'review')

    def test_evaluation_modes_and_status_counts_do_not_claim_success(self):
        with self.app.state.session_factory() as session:
            for mock,status in [(True,'completed'),(False,'interrupted')]:
                session.add(EvaluationRun(id=str(uuid4()),project_id=self.project_id,name='历史运行',request_fingerprint='x',status=status,
                    config_snapshot={'mock_mode':mock},config_signature='x',engine_version='x'))
            session.commit()
        result=self.get()
        self.assertEqual(result['evaluation']['mock_runs'],1);self.assertEqual(result['evaluation']['real_runs'],1)
        self.assertEqual(result['evaluation']['runs'],{'completed':1,'interrupted':1})
        self.assertNotIn('passed',result['evaluation'])
