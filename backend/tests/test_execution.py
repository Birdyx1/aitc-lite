import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import test_reviews as fixtures
from app.main import create_app
from app.models import ExecutionTask,ExecutionItem,ExecutionResultEvent,CaseReview
from app.agents.tools import overview,NoArgs


class ExecutionTests(unittest.TestCase):
    tearDown=fixtures.ReviewTests.tearDown

    def setUp(self):
        fixtures.ReviewTests.setUp(self)
        self.ids=[case['id'] for case in self.generation['cases']]
        review=self.client.get(self.base).json()['items'][0]
        fields={**review['current'],'title':'人工修改的执行标题','steps':['人工步骤一','人工步骤二']}
        self.client.put(self.base+'/'+self.ids[0],json={'action':'edit','expected_revision':0,'fields':fields}).raise_for_status()
        for index,id in enumerate(self.ids):
            self.client.put(self.base+'/'+id,json={'action':'accept','expected_revision':1 if index==0 else 0}).raise_for_status()
        self.root=f'/api/projects/{self.project_id}/execution-tasks'

    def payload(self,**changes):
        return {'task_id':str(uuid4()),'name':'登录回归','environment':'测试环境 v1.0 / Chrome','executor':'测试同学','note':'按快照人工执行','case_ids':self.ids,**changes}

    def create(self,**changes):
        response=self.client.post(self.root,json=self.payload(**changes))
        self.assertEqual(response.status_code,201,response.text);return response.json()

    def record(self,task,index=0,**changes):
        item=task['items'][index]
        data={'request_id':str(uuid4()),'expected_revision':item['revision'],'status':'passed','actual_result':'实际操作符合预期','defect_note':'',**changes}
        response=self.client.put(self.root+'/'+task['id']+'/items/'+item['id'],json=data)
        self.assertEqual(response.status_code,200,response.text);return response.json(),data

    def get(self,task):return self.client.get(self.root+'/'+task['id']).json()

    def test_snapshot_uses_current_manual_version_order_and_source(self):
        task=self.create(case_ids=list(reversed(self.ids)))
        self.assertEqual([row['snapshot']['case_id'] for row in task['items']],list(reversed(self.ids)))
        row=task['items'][-1]['snapshot']
        self.assertEqual(row['fields']['title'],'人工修改的执行标题');self.assertEqual(row['fields']['steps'],['人工步骤一','人工步骤二'])
        self.assertTrue(row['was_edited']);self.assertEqual(row['review_revision'],2);self.assertEqual(row['source'],'mock')
        self.assertFalse(row['outdated_at_creation']);self.assertEqual(task['stats']['not_run'],3);self.assertIsNone(task['stats']['pass_rate'])

    def test_create_idempotency_and_request_binding(self):
        data=self.payload();first=self.client.post(self.root,json=data)
        again=self.client.post(self.root,json=data)
        self.assertEqual(again.status_code,200);self.assertEqual(again.json(),first.json())
        self.assertEqual(self.client.post(self.root,json={**data,'name':'另一任务'}).status_code,409)
        self.assertEqual(self.client.get(self.root).json()['total'],1)
        with self.app.state.session_factory() as session:self.assertEqual(len(list(session.scalars(select(ExecutionItem)))),3)

    def test_only_accepted_current_project_cases_allowed(self):
        other=self.client.post('/api/projects',json={'name':'其他项目'}).json()['id']
        self.assertEqual(self.client.post(f'/api/projects/{other}/execution-tasks',json=self.payload()).status_code,400)
        with self.app.state.session_factory() as session:
            row=session.get(CaseReview,self.ids[1]);row.status='pending';session.commit()
        self.assertEqual(self.client.post(self.root,json=self.payload()).status_code,400)
        self.assertEqual(self.client.post(self.root,json=self.payload(case_ids=[str(uuid4())])).status_code,400)

    def test_outdated_requires_ack_and_preserves_creation_state(self):
        self.client.put(self.requirement_url,json={'title':'更新的登录需求','content':'原文变化'})
        self.assertEqual(self.client.post(self.root,json=self.payload()).status_code,409)
        task=self.create(acknowledge_outdated=True)
        self.assertTrue(all(item['snapshot']['outdated_at_creation'] for item in task['items']))
        self.assertEqual(task['items'][0]['snapshot']['requirement_title'],'登录')

    def test_source_changes_and_deletion_do_not_change_snapshot(self):
        task=self.create()
        with self.app.state.session_factory() as session:
            row=session.get(CaseReview,self.ids[0]);row.edited_case={**row.edited_case,'title':'事后修改'};session.commit()
        self.client.put(self.requirement_url,json={'title':'需求变化','content':'新需求'})
        self.client.delete(self.requirement_url)
        loaded=self.get(task);self.assertEqual(loaded['items'],task['items'])
        saved,_=self.record(loaded);self.assertEqual(saved['item']['status'],'passed')

    def test_result_status_actual_defect_and_audit(self):
        task=self.create();saved,data=self.record(task,status='failed',actual_result='登录返回 500',defect_note='BUG-12 / 待修复')
        item=saved['item'];self.assertEqual(item['revision'],1);self.assertEqual(item['defect_note'],'BUG-12 / 待修复');self.assertIsNotNone(item['recorded_at'])
        history=self.client.get(self.root+'/'+task['id']+'/items/'+item['id']+'/history').json()
        self.assertEqual(len(history),1);self.assertEqual(history[0]['status'],'failed')
        self.assertEqual(history[0]['id'],data['request_id']);self.assertEqual(self.get(task)['stats']['failed'],1)

    def test_result_replay_does_not_duplicate_or_revert_later_revision(self):
        task=self.create();first,data=self.record(task)
        url=self.root+'/'+task['id']+'/items/'+first['item']['id']
        self.assertTrue(self.client.put(url,json=data).json()['replayed'])
        task=self.get(task);self.record(task,status='failed',actual_result='复核发现异常')
        repeated=self.client.put(url,json=data).json()
        self.assertTrue(repeated['replayed']);self.assertEqual(repeated['item']['status'],'failed')
        self.assertEqual(len(self.client.get(url+'/history').json()),2)
        self.assertEqual(self.client.put(url,json={**data,'actual_result':'换内容'}).status_code,409)

    def test_result_request_cannot_be_reused_for_another_item(self):
        task=self.create();_,data=self.record(task)
        response=self.client.put(self.root+'/'+task['id']+'/items/'+task['items'][1]['id'],json=data)
        self.assertEqual(response.status_code,409)

    def test_revision_conflict_and_same_content_rejection(self):
        task=self.create();saved,_=self.record(task)
        url=self.root+'/'+task['id']+'/items/'+saved['item']['id']
        data={'request_id':str(uuid4()),'expected_revision':0,'status':'failed','actual_result':'过期修改'}
        self.assertEqual(self.client.put(url,json=data).status_code,409)
        self.assertEqual(self.client.put(url,json={**data,'expected_revision':1,'status':'passed','actual_result':'实际操作符合预期'}).status_code,400)
        self.assertEqual(self.get(task)['items'][0]['status'],'passed')

    def test_multi_page_concurrent_update_has_single_winner(self):
        task=self.create();url=self.root+'/'+task['id']+'/items/'+task['items'][0]['id']
        def save(index):return self.client.put(url,json={'request_id':str(uuid4()),'expected_revision':0,'status':'passed','actual_result':f'页面 {index}'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,[1,2]))
        self.assertEqual(sorted(results),[200,409]);self.assertEqual(self.get(task)['revision'],1)
        self.assertEqual(len(self.client.get(url+'/history').json()),1)

    def test_statistics_denominator_excludes_blocked_and_not_run(self):
        task=self.create();self.record(task,status='passed');self.record(task,1,status='failed',actual_result='错误')
        self.record(task,2,status='blocked',actual_result='环境不可用')
        stats=self.get(task)['stats'];self.assertEqual((stats['passed'],stats['failed'],stats['blocked'],stats['not_run']),(1,1,1,0))
        self.assertEqual(stats['pass_rate'],50);self.assertEqual(stats['pass_rate_denominator'],2);self.assertEqual(stats['recorded'],3)
        blocked=self.create(case_ids=[self.ids[0]]);self.record(blocked,status='blocked',actual_result='账号未准备')
        self.assertIsNone(self.get(blocked)['stats']['pass_rate'])

    def test_reset_result_preserves_history(self):
        task=self.create();self.record(task,status='failed',actual_result='错误',defect_note='BUG-1')
        task=self.get(task);saved,_=self.record(task,status='not_run',actual_result='',defect_note='')
        self.assertEqual(saved['item']['revision'],2);self.assertEqual(saved['item']['defect_note'],'')
        self.assertEqual(self.get(task)['stats']['not_run'],3)
        url=self.root+'/'+task['id']+'/items/'+task['items'][0]['id']+'/history'
        self.assertEqual([row['status'] for row in self.client.get(url).json()],['not_run','failed'])

    def test_complete_requires_all_recorded_and_current_task_revision(self):
        task=self.create();url=self.root+'/'+task['id']+'/complete'
        self.assertEqual(self.client.post(url,json={'expected_revision':0}).status_code,409)
        for index in range(3):self.record(task,index,status='blocked',actual_result='环境不可用')
        self.assertEqual(self.client.post(url,json={'expected_revision':0}).status_code,409)
        task=self.get(task);response=self.client.post(url,json={'expected_revision':task['revision']})
        self.assertEqual(response.status_code,200,response.text);done=response.json()
        self.assertEqual(done['status'],'completed');self.assertIsNotNone(done['completed_at'])
        self.assertEqual(self.client.post(url,json={'expected_revision':task['revision']}).json(),done)
        item=done['items'][0]
        self.assertEqual(self.client.put(self.root+'/'+done['id']+'/items/'+item['id'],json={'request_id':str(uuid4()),'expected_revision':item['revision'],'status':'passed','actual_result':'补充'}).status_code,409)

    def test_project_scope_task_item_history_complete(self):
        task=self.create();other=self.client.post('/api/projects',json={'name':'另一个'}).json()['id']
        foreign=f'/api/projects/{other}/execution-tasks/'+task['id'];item=task['items'][0]
        self.assertEqual(self.client.get(foreign).status_code,404)
        self.assertEqual(self.client.get(foreign+'/items/'+item['id']+'/history').status_code,404)
        self.assertEqual(self.client.post(foreign+'/complete',json={'expected_revision':0}).status_code,404)
        self.assertEqual(self.client.put(foreign+'/items/'+item['id'],json={'request_id':str(uuid4()),'expected_revision':0,'status':'passed','actual_result':'x'}).status_code,404)
        another=self.create();self.assertEqual(self.client.get(self.root+'/'+another['id']+'/items/'+item['id']+'/history').status_code,404)

    def test_persistence_restart_without_execution_or_result_changes(self):
        task=self.create();self.record(task,status='blocked',actual_result='等待数据')
        before=self.get(task);self.client.__exit__(None,None,None)
        self.app=create_app(self.path);self.client=TestClient(self.app).__enter__()
        self.assertEqual(self.get(task),before)

    def test_project_delete_cascades_tasks_items_and_events(self):
        task=self.create();self.record(task)
        self.client.delete('/api/projects/'+str(self.project_id))
        with self.app.state.session_factory() as session:
            for model in (ExecutionTask,ExecutionItem,ExecutionResultEvent):self.assertEqual(list(session.scalars(select(model))),[])

    def test_agent_overview_manual_execution_scope_and_repeated_task_counting(self):
        first=self.create();self.record(first,status='passed');self.create()
        other=self.client.post('/api/projects',json={'name':'其他'}).json()['id']
        with self.app.state.session_factory() as session:
            result=overview(session,self.project_id,NoArgs())['execution'];empty=overview(session,other,NoArgs())['execution']
        self.assertTrue(result['available']);self.assertEqual(result['recording_mode'],'manual')
        self.assertEqual(result['item_stats']['total'],6);self.assertEqual(result['item_stats']['passed'],1)
        self.assertEqual(result['item_stats']['pass_rate'],100);self.assertEqual(empty['item_stats']['total'],0)

    def test_validation_before_writes(self):
        for changes in ({'name':' '},{'environment':' '},{'name':'x'*101},{'executor':'x'*81},{'note':'x'*2001},{'case_ids':[]},{'case_ids':self.ids*2},{'case_ids':[str(uuid4()) for _ in range(201)]},{'name':'\ud800'},{'note':'a\x00b'},{'unexpected':'x'},{'acknowledge_outdated':'yes'}):
            response=self.client.post(self.root,content=json.dumps(self.payload(**changes)),headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.get(self.root).json()['total'],0)
        task=self.create();url=self.root+'/'+task['id']+'/items/'+task['items'][0]['id']
        base={'request_id':str(uuid4()),'expected_revision':0,'status':'passed','actual_result':'okay'}
        for changes in ({'actual_result':' '},{'status':'skipped'},{'expected_revision':True},{'actual_result':'x'*5001},{'defect_note':'x'*2001},{'actual_result':'\ud800'},{'status':'not_run','actual_result':'x'},{'unexpected':'x'}):
            result=self.client.put(url,content=json.dumps({**base,**changes}),headers={'Content-Type':'application/json'})
            self.assertEqual(result.status_code,422,result.text)
        self.assertEqual(self.get(task)['revision'],0)

    def test_list_pagination_and_current_statistics(self):
        for index in range(3):self.create(name=f'任务 {index}')
        result=self.client.get(self.root+'?limit=2&offset=1').json()
        self.assertEqual(result['total'],3);self.assertEqual(len(result['items']),2)
        self.assertTrue(all(task['stats']['not_run']==3 for task in result['items']))
        self.assertEqual(self.client.get(self.root+'?limit=0').status_code,422)
