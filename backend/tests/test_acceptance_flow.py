"""贯通各模块的验收烟测；只使用临时数据库、Mock 服务和拒绝联网的传输。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.main import create_app


class AcceptanceFlowTests(unittest.TestCase):
    def test_project_to_generation_review_execution_and_evaluation(self):
        attempts=[]
        def deny_network(request):
            attempts.append(request.url.host)
            raise AssertionError('Mock 集成验收不得向外部发送请求')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'acceptance.sqlite'
            transport=httpx.MockTransport(deny_network)
            app=create_app(path,llm_transport=transport,embedding_transport=transport,rerank_transport=transport,judge_transport=transport,agent_transport=transport)
            with TestClient(app) as client:
                def call(method,url,data=None,status=200):
                    response=client.request(method,url,json=data) if data is not None else client.request(method,url)
                    self.assertEqual(response.status_code,status,response.text)
                    return response.json() if response.status_code!=204 else None
                def wait(url,terminal='succeeded'):
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        value=call('GET',url)
                        if value['status'] not in ('queued','running','stopping'):
                            self.assertEqual(value['status'],terminal,value);return value
                        time.sleep(.03)
                    self.fail('跨模块烟测任务超时：'+url)
                pid=call('POST','/api/projects',{'name':'集成验收临时项目'},201)['id'];root=f'/api/projects/{pid}'
                foreign=call('POST','/api/projects',{'name':'隔离项目'},201)['id']
                call('PUT','/api/settings/generation',{'mock_mode':True})
                call('PUT','/api/settings/embedding',{'mock_mode':True,'expected_dimensions':8})
                call('PUT','/api/settings/rerank',{'mock_mode':True})
                content='登录验证码五分钟内有效，过期后拒绝登录。'
                doc=call('POST',root+'/knowledge-documents',{'document_id':str(uuid4()),'title':'验证码规则','content':content,'source_filename':'acceptance.md'},201)
                bm25=call('POST',root+'/knowledge-search',{'query':content,'top_k':3})
                self.assertTrue(bm25['hits'])
                build=call('POST',root+'/vector-index/builds',{'request_id':str(uuid4())},202)
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    index=call('GET',root+'/vector-index')
                    if index['task']['status'] not in ('queued','running'):break
                    time.sleep(.03)
                self.assertEqual(index['task']['status'],'succeeded',index)
                hybrid=call('POST',root+'/hybrid-search',{'query':content,'top_k':3,'use_rerank':True})
                self.assertTrue(hybrid['hits']);self.assertEqual(hybrid['rerank']['status'],'applied')
                req=call('POST',root+'/requirements',{'title':'验证码登录','content':content},201);rr=root+f"/requirements/{req['id']}"
                preview=call('POST',rr+'/features/analyze')
                feature={**preview['items'][0],'feature':'验证码登录','acceptance_criteria':'验证码过期时拒绝登录'}
                sheet=call('PUT',rr+'/features',{'expected_revision':preview['base_revision'],'input_hash':preview['input_hash'],
                    'confirmed':True,'source':'mock','items':[feature]})
                payload={'request_id':str(uuid4()),'feature_id':feature['id'],'expected_revision':sheet['revision'],
                    'input_hash':sheet['current_hash'],'strategy':'full','skill_ids':['api','security'],'use_rag':True,'rag_query':content,'rag_rerank':True}
                task=call('POST',rr+'/generation-tasks',payload,202)
                wait(rr+'/generation-tasks/'+task['id'])
                generation=call('GET',rr+'/case-generations/'+task['id'])
                self.assertEqual(len(generation['cases']),3);self.assertTrue(generation['knowledge']['sources'])
                self.assertEqual(len(generation['skills']['items']),2)
                gid=generation['id'];review_path=rr+'/case-generations/'+gid+'/reviews'
                first=call('GET',review_path)['items'][0];case_id=first['draft_id']
                fields={**first['current'],'title':'人工验收：验证码登录','expected_result':'模拟记录：应按输入规则核对，不代表系统已执行'}
                call('PUT',review_path+'/'+case_id,{'action':'edit','expected_revision':0,'fields':fields})
                call('PUT',review_path+'/'+case_id,{'action':'accept','expected_revision':1})
                second=call('GET',review_path)['items'][1]
                call('PUT',review_path+'/'+second['draft_id'],{'action':'reject','expected_revision':0,'rejection_reason':'集成验收模拟驳回'})
                accepted=call('GET',root+'/testcases');self.assertEqual(len(accepted),1);self.assertEqual(accepted[0]['title'],fields['title'])
                export=client.post(root+'/testcases/export',json={'case_ids':[case_id],'format':'markdown'})
                self.assertEqual(export.status_code,200,export.text);self.assertIn('人工验收',export.content.decode())
                judge=call('POST',rr+'/case-generations/'+gid+'/judge-runs',{'request_id':str(uuid4())},202)
                report=wait(rr+'/case-generations/'+gid+'/judge-runs/'+judge['id']);self.assertEqual(report['checked_count'],3)
                execution=call('POST',root+'/execution-tasks',{'task_id':str(uuid4()),'name':'人工记录闭环','environment':'仅集成验收，不执行真实系统','case_ids':[case_id]},201)
                item=execution['items'][0]
                recorded=call('PUT',root+'/execution-tasks/'+execution['id']+'/items/'+item['id'],{'request_id':str(uuid4()),'expected_revision':0,'status':'passed','actual_result':'模拟人工填报，仅验证记录闭环'})
                done=call('POST',root+'/execution-tasks/'+execution['id']+'/complete',{'expected_revision':recorded['task_revision']})
                self.assertEqual(done['stats']['passed'],1)
                eval_root=root+'/evaluations'
                sample=call('POST',eval_root+'/samples',{'name':'验证码评测','requirement_title':'验证码登录','requirement_content':content,
                    'feature':{'module':'账号','feature':'验证码登录','description':'验证验证码登录'},'gold_points':['验证码登录']},201)
                eval_runs=[]
                for strategy in ('full','quick'):
                    run=call('POST',eval_root+'/runs',{'run_id':str(uuid4()),'name':strategy,'sample_ids':[sample['id']],'strategy':strategy,'mock_mode':True},202)
                    eval_runs.append(wait(eval_root+'/runs/'+run['id'],'completed'))
                comparison=call('GET',eval_root+f"/compare?left={eval_runs[0]['id']}&right={eval_runs[1]['id']}")
                self.assertEqual(comparison['common_count'],1)
                self.assertEqual(comparison['common_metrics']['left']['generated_case_count'],3)
                self.assertEqual(comparison['common_metrics']['right']['generated_case_count'],1)
                self.assertEqual(len(call('GET',rr+'/case-generations')),1)
                sid=str(uuid4());call('POST',root+'/agent-sessions',{'session_id':sid},201)
                turn=call('POST',root+'/agent-sessions/'+sid+'/turns',{'request_id':str(uuid4()),'message':'项目概况'},202)
                answer=wait(root+'/agent-sessions/'+sid+'/turns/'+turn['id'])
                self.assertEqual(answer['tools'][0]['result']['data']['execution']['item_stats']['passed'],1)
                self.assertEqual(client.get(f'/api/projects/{foreign}/execution-tasks/'+execution['id']).status_code,404)
                call('DELETE',rr,status=204)
                self.assertEqual(call('GET',root+'/execution-tasks/'+execution['id'])['items'][0]['snapshot']['fields']['title'],fields['title'])
                self.assertEqual(call('GET',eval_root+'/runs/'+eval_runs[0]['id'])['status'],'completed')
                self.assertEqual(attempts,[])
            with TestClient(create_app(path,llm_transport=transport)) as restarted:
                self.assertEqual(restarted.get(root+'/execution-tasks/'+execution['id']).json()['status'],'completed')
                self.assertEqual(restarted.get(eval_root+'/runs/'+eval_runs[0]['id']).json()['metrics'],eval_runs[0]['metrics'])
            self.assertEqual(attempts,[])
