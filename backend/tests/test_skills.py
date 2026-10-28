import copy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

import test_case_generations as direct_fixtures
import test_generation_tasks as task_fixtures
from app.main import create_app
from app.models import GenerationSkills
from app.specialists import registry


class RegistryTests(unittest.TestCase):
    def test_fixed_catalog_and_copy_isolation(self):
        items=registry.snapshots(['security','api'])
        self.assertEqual([item['id'] for item in items],['api','security'])
        self.assertTrue(all(len(item['content_hash'])==64 for item in items))
        items[0]['prompt']='modified copy'
        self.assertNotEqual(registry.snapshots(['api'])[0]['prompt'],'modified copy')
        with self.assertRaises(Exception):registry.snapshots(['dynamic-plugin'])

    def test_independent_handlers_respect_strategy(self):
        feature={'module':'账号','feature':'登录'}
        api=registry.build_contexts(registry.snapshots(['api']),feature,'full')[0]
        security=registry.build_contexts(registry.snapshots(['security']),feature,'quick')[0]
        self.assertEqual(api['scope'],'账号 / 登录')
        self.assertIn('请求/响应契约',api['checks'])
        self.assertEqual(security['checks'],['有权限身份的正常主路径'])


class SkillGenerationTests(unittest.TestCase):
    tearDown=direct_fixtures.CaseGenerationTests.tearDown
    configure=direct_fixtures.CaseGenerationTests.configure
    payload=direct_fixtures.CaseGenerationTests.payload

    def setUp(self):
        direct_fixtures.CaseGenerationTests.setUp(self)
        self.configure()
        self.tag_mode='valid'
        def respond(request):
            self.requests.append(request)
            body=json.loads(json.loads(request.content)['messages'][1]['content'])
            skills=[item['id'] for item in body.get('specialist_skills',[])]
            refs=skills
            if self.tag_mode=='unknown':refs=['unselected']
            if self.tag_mode=='duplicate':refs=['api','api']
            if self.tag_mode=='empty':refs=[]
            kinds=['functional'] if body['strategy']=='quick' else ['functional','boundary','exception']
            cases=[{'title':kind,'priority':'P1','case_type':kind,'is_smoke':kind=='functional','precondition':'',
                    'steps':['执行操作'],'expected_result':'符合已声明规则',
                    **({'skill_refs':refs} if skills else {}),
                    **({'knowledge_refs':['K1']} if body.get('knowledge_context') else {})} for kind in kinds]
            if self.after_call:self.after_call()
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'cases':cases})},'finish_reason':'stop'}]})
        self.app.state.llm_transport=httpx.MockTransport(respond)

    def generate(self,ids,**overrides):return self.client.post(self.base,json=self.payload(skill_ids=ids,**overrides))

    def test_catalog_no_secret_prompt_or_model_calls(self):
        response=self.client.get('/api/skills')
        self.assertEqual(response.status_code,200)
        self.assertEqual([item['id'] for item in response.json()],['api','security'])
        self.assertTrue(all('prompt' not in item for item in response.json()))
        self.assertEqual(self.requests,[])

    def test_api_prompt_handler_version_and_tags(self):
        response=self.generate(['api'])
        self.assertEqual(response.status_code,201,response.text)
        result=response.json()
        self.assertEqual(result['prompt_version'],'case-writer-skills-v3')
        self.assertEqual(result['skill_names'],['接口测试'])
        self.assertTrue(all(tags==['api'] for tags in result['skills']['case_tags'].values()))
        messages=json.loads(self.requests[0].content)['messages']
        self.assertIn('接口测试专项（api）',messages[0]['content'])
        self.assertNotIn('安全与权限专项（security）',messages[0]['content'])
        self.assertEqual(json.loads(messages[1]['content'])['specialist_skills'][0]['id'],'api')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(result['graph_nodes'],['prepare','generate','validate'])
        self.assertNotIn('fake-generation-key',json.dumps(result['skills']))

    def test_security_quick_keeps_smoke_contract(self):
        response=self.generate(['security'],strategy='quick')
        self.assertEqual(response.status_code,201,response.text)
        result=response.json()
        self.assertEqual(result['case_count'],1)
        self.assertTrue(result['cases'][0]['is_smoke'])
        self.assertEqual(result['cases'][0]['case_type'],'functional')
        body=json.loads(json.loads(self.requests[0].content)['messages'][1]['content'])
        self.assertEqual(body['specialist_skills'][0]['checks'],['有权限身份的正常主路径'])

    def test_combined_canonical_replay_one_model_call(self):
        payload=self.payload(skill_ids=['security','api'])
        response=self.client.post(self.base,json=payload)
        self.assertEqual(response.status_code,201,response.text)
        result=response.json()
        self.assertEqual([s['id'] for s in result['skills']['items']],['api','security'])
        self.assertTrue(all(tags==['api','security'] for tags in result['skills']['case_tags'].values()))
        payload['skill_ids']=['api','security']
        self.assertEqual(self.client.post(self.base,json=payload).status_code,200)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.client.get(self.base).json()[0]['skill_names'],['接口测试','安全与权限'])

    def test_ordinary_unchanged_no_snapshot_or_specialist_prompt(self):
        response=self.generate([])
        self.assertEqual(response.status_code,201,response.text)
        self.assertIsNone(response.json()['skills'])
        self.assertEqual(response.json()['skill_names'],[])
        self.assertEqual(response.json()['prompt_version'],'case-writer-v1')
        body=json.loads(json.loads(self.requests[0].content)['messages'][1]['content'])
        self.assertNotIn('specialist_skills',body)

    def test_unknown_duplicate_or_wrong_input_rejected_before_call(self):
        for ids in (['dynamic'],['api','api'],['api','security','api'],'api',[True],None,['\ud800']):
            response=self.client.post(self.base,content=json.dumps(self.payload(skill_ids=ids)).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.requests,[])

    def test_bad_model_tags_do_not_save_or_silently_drop(self):
        for mode in ['unknown','duplicate']:
            self.tag_mode=mode
            self.assertEqual(self.generate(['api']).status_code,502)
        self.assertEqual(self.client.get(self.base).json(),[])

    def test_empty_coverage_declaration_does_not_invent_tags(self):
        self.tag_mode='empty'
        result=self.generate(['api']).json()
        self.assertTrue(all(tags==[] for tags in result['skills']['case_tags'].values()))

    def test_rag_and_skills_share_call_with_separate_references(self):
        self.client.post(f'/api/projects/{self.project}/knowledge-documents',json={'document_id':str(uuid4()),'title':'接口登录','content':'登录接口需验证声明的账号权限。','source_filename':''})
        response=self.generate(['api','security'],use_rag=True,rag_query='登录')
        self.assertEqual(response.status_code,201,response.text)
        result=response.json()
        self.assertEqual(result['prompt_version'],'case-writer-rag-skills-v3')
        self.assertTrue(all(tags==['api','security'] for tags in result['skills']['case_tags'].values()))
        self.assertTrue(all(refs==['K1'] for refs in result['knowledge']['case_citations'].values()))
        self.assertEqual(len(self.requests),1)
        body=json.loads(json.loads(self.requests[0].content)['messages'][1]['content'])
        self.assertIn('knowledge_context',body);self.assertIn('specialist_skills',body)

    def test_mock_marks_selected_specialists(self):
        self.configure(mock=True)
        result=self.generate(['api','security']).json()
        self.assertEqual(result['source'],'mock')
        self.assertTrue(all('接口测试 / 安全与权限' in case['title'] for case in result['cases']))
        self.assertTrue(all(tags==['api','security'] for tags in result['skills']['case_tags'].values()))
        self.assertEqual(self.requests,[])

    def test_persistence_and_requirement_cascade(self):
        result=self.generate(['api']).json()
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(self.base+'/'+result['id']).json(),result)
        self.client.delete(self.requirement_url+'/'+str(self.requirement_id))
        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(GenerationSkills,result['id']))

    def test_failed_request_cannot_rebind_specialists(self):
        self.tag_mode='unknown'
        payload=self.payload(skill_ids=['api'])
        self.assertEqual(self.client.post(self.base,json=payload).status_code,502)
        payload['skill_ids']=[]
        self.assertEqual(self.client.post(self.base,json=payload).status_code,409)
        self.assertEqual(self.client.post(self.base.replace('/case-generations','/generation-tasks'),json=payload).status_code,409)
        self.assertEqual(len(self.requests),1)


class SkillTaskTests(unittest.TestCase):
    tearDown=task_fixtures.GenerationTaskTests.tearDown
    make_requirement=task_fixtures.GenerationTaskTests.make_requirement
    submit=task_fixtures.GenerationTaskTests.submit
    wait_status=task_fixtures.GenerationTaskTests.wait_status

    def setUp(self):
        task_fixtures.GenerationTaskTests.setUp(self)
        self.payload['skill_ids']=['api','security']

    def action(self,name):return self.client.post(self.url+'/'+self.payload['request_id']+'/'+name,json={})

    def test_submission_snapshot_and_pause_restart_reuse_model(self):
        self.release.clear();submitted=self.submit().json()
        self.assertEqual(submitted['skill_ids'],['api','security'])
        with self.app.state.session_factory() as session:
            saved=session.get(GenerationSkills,self.payload['request_id'])
            original=copy.deepcopy(saved.snapshot)
            self.assertEqual([s['id'] for s in original],['api','security'])
        self.assertTrue(self.entered.wait(3));self.action('pause');self.release.set();self.wait_status('paused')
        transport=self.app.state.llm_transport
        self.client.__exit__(None,None,None)
        self.app=create_app(self.path,llm_transport=transport);self.client=TestClient(self.app).__enter__()
        self.assertEqual(self.action('resume').status_code,200);self.wait_status('succeeded')
        result=self.client.get(self.rr+'/case-generations/'+self.payload['request_id']).json()
        self.assertEqual(result['skills']['items'],original)
        self.assertEqual(self.calls,1)

    def test_version_change_blocks_resume_without_overwriting_snapshot(self):
        self.release.clear();self.submit();self.assertTrue(self.entered.wait(3))
        self.action('pause');self.release.set();self.wait_status('paused')
        changed={**registry._REGISTRY['api'],'content_hash':'0'*64}
        with patch.dict(registry._REGISTRY,api=changed):
            task=self.client.get(self.url+'/'+self.payload['request_id']).json()
            self.assertFalse(task['can_resume'])
            self.assertEqual(self.action('resume').status_code,409)
        self.assertEqual(self.action('resume').status_code,200);self.wait_status('succeeded')
        self.assertEqual(self.calls,1)
