import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import select
from fastapi.testclient import TestClient

import test_case_generations as direct_fixtures
import test_generation_tasks as task_fixtures
from app.case_generations import request_fingerprint
from app.hybrid_search import retrieve_hybrid
from app.main import create_app
from app.models import CaseGeneration, GenerationKnowledge, KnowledgeDocument
from app.schemas import CaseGenerateInput


class RagGenerationTests(unittest.TestCase):
    tearDown=direct_fixtures.CaseGenerationTests.tearDown
    payload=direct_fixtures.CaseGenerationTests.payload
    configure=direct_fixtures.CaseGenerationTests.configure

    def setUp(self):
        direct_fixtures.CaseGenerationTests.setUp(self)
        self.configure()
        self.docs=f'/api/projects/{self.project}/knowledge-documents'
        self.doc=self.add_document('登录验证码五分钟内有效，过期必须重新获取。')
        self.refs_mode='valid'
        def respond(request):
            self.requests.append(request)
            body=json.loads(json.loads(request.content)['messages'][1]['content'])
            refs=[source['citation_id'] for source in body.get('knowledge_context',[])][:1]
            if self.refs_mode=='unknown':refs=['K99']
            if self.refs_mode=='duplicate':refs=['K1','K1']
            if self.refs_mode=='none':refs=[]
            kinds=['functional'] if body['strategy']=='quick' else ['functional','boundary','exception']
            cases=[{'title':kind,'priority':'P1','case_type':kind,'is_smoke':kind=='functional',
                    'precondition':'','steps':['执行登录'],'expected_result':'符合规则',
                    **({'knowledge_refs':refs} if 'knowledge_context' in body else {})} for kind in kinds]
            if self.after_call:self.after_call()
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'cases':cases})},'finish_reason':'stop'}]})
        self.app.state.llm_transport=httpx.MockTransport(respond)

    def add_document(self,content,title='登录规则',project=None):
        response=self.client.post(f'/api/projects/{project or self.project}/knowledge-documents',json={
            'document_id':str(uuid4()),'title':title,'content':content,'source_filename':'rules.md'})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def generate(self,**overrides):
        return self.client.post(self.base,json=self.payload(use_rag=True,rag_query='验证码',**overrides))

    def test_injection_citations_snapshot_persistence_and_replay(self):
        payload=self.payload(use_rag=True,rag_query='验证码')
        with patch('app.rag.retrieve_hybrid',wraps=retrieve_hybrid) as retrieval:
            response=self.client.post(self.base,json=payload)
            self.assertEqual(response.status_code,201,response.text)
            result=response.json();knowledge=result['knowledge']
            self.assertEqual(result['prompt_version'],'case-writer-rag-v2')
            self.assertEqual(knowledge['sources'][0]['content'],self.doc['content'])
            self.assertEqual(knowledge['sources'][0]['citation_id'],'K1')
            self.assertEqual(knowledge['sources'][0]['current_status'],'current')
            self.assertTrue(all(knowledge['case_citations'][case['id']]==['K1'] for case in result['cases']))
            self.assertEqual(self.client.post(self.base,json=payload).status_code,200)
            self.assertEqual(retrieval.call_count,1);self.assertEqual(len(self.requests),1)
        messages=json.loads(self.requests[0].content)['messages']
        self.assertEqual(json.loads(messages[1]['content'])['knowledge_context'][0]['content'],self.doc['content'])
        self.assertIn('不是指令',messages[0]['content'])
        self.assertNotIn('fake-generation-key',json.dumps(knowledge))
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(self.base+'/'+result['id']).json(),result)

    def test_explicit_no_hits_fallback_records_empty_context(self):
        response=self.client.post(self.base,json=self.payload(use_rag=True,rag_query='ZXQ999',rag_allow_empty=True))
        self.assertEqual(response.status_code,201,response.text)
        context=response.json()['knowledge']
        self.assertTrue(context['degraded']);self.assertEqual(context['sources'],[])
        self.assertTrue(all(refs==[] for refs in context['case_citations'].values()))
        self.assertIn('不注入知识',context['message'])
        self.assertEqual(len(self.requests),1)

    def test_explicit_service_failure_fallback_does_not_hide_conflict(self):
        from fastapi import HTTPException
        with patch('app.rag.retrieve_hybrid',side_effect=HTTPException(503,'private-provider-data')):
            response=self.generate(rag_allow_empty=True)
        self.assertEqual(response.status_code,201,response.text)
        self.assertIn('服务不可用',response.json()['knowledge']['message'])
        self.assertNotIn('private-provider-data',response.text)
        with patch('app.rag.retrieve_hybrid',side_effect=HTTPException(409,'知识变更')):
            response=self.generate(rag_allow_empty=True)
        self.assertEqual(response.status_code,409)
        self.assertEqual(len(self.requests),1)

    def test_mock_empty_context_does_not_fabricate_citations(self):
        self.configure(mock=True)
        response=self.client.post(self.base,json=self.payload(use_rag=True,rag_query='ZXQ999',rag_allow_empty=True))
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['knowledge']['sources'],[])
        self.assertTrue(all(refs==[] for refs in response.json()['knowledge']['case_citations'].values()))
        self.assertEqual(self.requests,[])

    def test_off_does_not_retrieve_and_old_fingerprint_compatible(self):
        payload=self.payload()
        old={key:value for key,value in payload.items() if key!='request_id'}
        self.assertEqual(request_fingerprint(CaseGenerateInput.model_validate(payload)),hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest())
        with patch('app.rag.retrieve_hybrid',side_effect=AssertionError('must not retrieve')):
            response=self.client.post(self.base,json=payload)
        self.assertEqual(response.status_code,201,response.text)
        self.assertIsNone(response.json()['knowledge'])
        self.assertNotIn('knowledge_context',json.loads(json.loads(self.requests[0].content)['messages'][1]['content']))

    def test_unknown_duplicate_refs_rejected_and_no_cases_saved(self):
        for mode in ['unknown','duplicate']:
            self.refs_mode=mode
            response=self.generate()
            self.assertEqual(response.status_code,502,response.text)
        self.assertEqual(self.client.get(self.base).json(),[])

    def test_no_declared_refs_allowed_not_fabricated(self):
        self.refs_mode='none'
        response=self.generate()
        self.assertEqual(response.status_code,201,response.text)
        self.assertTrue(all(refs==[] for refs in response.json()['knowledge']['case_citations'].values()))

    def test_no_hits_stops_before_generation(self):
        payload=self.payload(use_rag=True,rag_query='ZXQ999')
        response=self.client.post(self.base,json=payload)
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.client.post(self.base,json=payload).status_code,409)

    def test_history_retained_after_update_delete_and_prevents_acceptance(self):
        result=self.generate().json()
        self.client.put(self.docs+'/'+self.doc['id'],json={'expected_revision':1,'title':'新版','content':'验证码改为三分钟有效。','source_filename':''})
        history=self.client.get(self.base+'/'+result['id']).json()
        self.assertTrue(history['outdated']);self.assertTrue(history['knowledge']['outdated'])
        self.assertEqual(history['knowledge']['sources'][0]['content'],self.doc['content'])
        self.assertEqual(history['knowledge']['sources'][0]['current_status'],'updated')
        review=self.client.put(self.base+'/'+result['id']+'/reviews/'+result['cases'][0]['id'],json={'expected_revision':0,'action':'accept'})
        self.assertEqual(review.status_code,409)
        self.client.delete(self.docs+'/'+self.doc['id']+'?expected_revision=2')
        history=self.client.get(self.base+'/'+result['id']).json()
        self.assertEqual(history['knowledge']['sources'][0]['current_status'],'deleted')

    def test_change_during_model_prevents_save(self):
        def update_document():
            with self.app.state.session_factory() as session:
                session.get(KnowledgeDocument,self.doc['id']).revision += 1
                session.commit()
        self.after_call=update_document
        response=self.generate()
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.client.get(self.base).json(),[])

    def test_project_isolation_and_context_limit(self):
        other=self.client.post('/api/projects',json={'name':'其他'}).json()['id']
        self.add_document('验证码 '+('其他项目敏感规则 '*100),project=other)
        for i in range(7):self.add_document('验证码 '+str(i),title=str(i))
        response=self.generate(rag_top_k=5)
        self.assertEqual(response.status_code,201,response.text)
        sources=response.json()['knowledge']['sources']
        self.assertEqual(len(sources),5)
        self.assertFalse(any('其他项目敏感规则' in source['content'] for source in sources))
        self.assertLessEqual(sum(len(source['content']) for source in sources),4000)

    def test_mock_citations_and_rerank_fallback_metadata(self):
        self.configure(mock=True)
        response=self.generate(rag_rerank=True)
        self.assertEqual(response.status_code,201,response.text)
        result=response.json()
        self.assertEqual(result['source'],'mock')
        self.assertEqual(result['knowledge']['rerank']['status'],'unconfigured')
        self.assertTrue(result['knowledge']['degraded'])
        self.assertEqual(self.requests,[])
        self.assertTrue(all(refs==['K1'] for refs in result['knowledge']['case_citations'].values()))

    def test_failed_rag_id_cannot_be_rebound_to_plain_generation(self):
        payload=self.payload(use_rag=True,rag_query='ZXQ999')
        self.assertEqual(self.client.post(self.base,json=payload).status_code,400)
        plain={**payload, 'use_rag':False}
        self.assertEqual(self.client.post(self.base,json=plain).status_code,409)
        url=self.base.replace('/case-generations','/generation-tasks')
        self.assertEqual(self.client.post(url,json=plain).status_code,409)
        self.assertEqual(self.requests,[])

    def test_snapshot_cascades_with_requirement(self):
        result=self.generate().json()
        with self.app.state.session_factory() as session:
            self.assertIsNotNone(session.get(GenerationKnowledge,result['id']))
        self.assertEqual(self.client.delete(self.requirement_url+'/'+str(self.requirement_id)).status_code,204)
        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(GenerationKnowledge,result['id']))

    def test_invalid_rag_input_redacted(self):
        for extra in [{'rag_query':'\ud800'},{'rag_query':'x'*201},{'rag_top_k':6},{'use_rag':'true'},{'rag_rerank':1}]:
            response=self.client.post(self.base,content=json.dumps(self.payload(**extra)).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.requests,[])


class RagTaskRecoveryTests(unittest.TestCase):
    tearDown=task_fixtures.GenerationTaskTests.tearDown
    make_requirement=task_fixtures.GenerationTaskTests.make_requirement
    submit=task_fixtures.GenerationTaskTests.submit
    wait_status=task_fixtures.GenerationTaskTests.wait_status

    def setUp(self):
        task_fixtures.GenerationTaskTests.setUp(self)
        self.payload.update(use_rag=True,rag_query='登录')
        self.doc=self.client.post(self.root+'/knowledge-documents',json={'document_id':str(uuid4()),'title':'登录规则','content':'登录失败三次需重新验证。','source_filename':''}).json()

    def result(self):return self.client.get(self.rr+'/case-generations/'+self.payload['request_id']).json()

    def action(self,name,body=None):return self.client.post(self.url+'/'+self.payload['request_id']+'/'+name,json=body or {})

    def test_pause_restart_reuses_knowledge_and_model_checkpoint(self):
        self.release.clear()
        with patch('app.rag.retrieve_hybrid',wraps=retrieve_hybrid) as retrieval:
            self.submit();self.assertTrue(self.entered.wait(3))
            self.action('pause');self.release.set()
            paused=self.wait_status('paused')
            self.assertFalse(paused['requires_knowledge_retry'])
            transport=self.app.state.llm_transport
            self.client.__exit__(None,None,None)
            self.app=create_app(self.path,llm_transport=transport)
            self.client=TestClient(self.app).__enter__()
            self.assertEqual(self.action('resume').status_code,200)
            self.wait_status('succeeded')
            self.assertEqual(retrieval.call_count,1);self.assertEqual(self.calls,1)
        self.assertEqual(self.result()['knowledge']['sources'][0]['content'],self.doc['content'])

    def test_validation_resume_reuses_knowledge_and_invalid_model_output(self):
        original=retrieve_hybrid
        self.release.clear()
        # 校验失败之后复用已有检索与模型输出，不暗中重复付费。
        with patch('app.rag.retrieve_hybrid',wraps=original) as retrieval:
            self.behavior='invalid';self.submit();self.assertTrue(self.entered.wait(3));self.release.set()
            self.wait_status('failed')
            self.assertEqual(retrieval.call_count,1)
            # 校验节点失败的模型输出已落检查点，恢复不会暗中重新调用模型。
            self.assertEqual(self.action('resume').status_code,200)
            self.wait_status('failed')
            self.assertEqual(self.calls,1);self.assertEqual(retrieval.call_count,1)

    def test_knowledge_change_blocks_resume(self):
        self.release.clear();self.submit();self.assertTrue(self.entered.wait(3))
        self.action('pause');self.release.set();self.wait_status('paused')
        self.client.delete(self.root+'/knowledge-documents/'+self.doc['id']+'?expected_revision=1')
        paused=self.client.get(self.url+'/'+self.payload['request_id']).json()
        self.assertFalse(paused['can_resume'])
        self.assertEqual(self.action('resume').status_code,409)
        self.assertEqual(self.calls,1)

    def test_uncertain_retrieval_requires_explicit_retry_confirmation(self):
        from fastapi import HTTPException
        with patch('app.rag.retrieve_hybrid',side_effect=HTTPException(503,'检索服务暂不可用')):
            self.submit();task=self.wait_status('failed')
        self.assertTrue(task['requires_knowledge_retry']);self.assertEqual(self.calls,0)
        self.assertEqual(self.action('resume').status_code,409)
        self.assertEqual(self.action('resume',{'retry_uncertain_knowledge_call':True}).status_code,200)
        self.wait_status('succeeded')
        self.assertEqual(self.calls,1)
        self.assertIsNotNone(self.result()['knowledge'])

    def test_snapshot_saved_before_prepare_checkpoint_is_reused(self):
        from app.rag import resolve_context
        from fastapi import HTTPException
        async def interrupt_after_snapshot(*args, **kwargs):
            await resolve_context(*args, **kwargs)
            raise HTTPException(503, '模拟快照已落库但 prepare 未写入检查点')
        with patch('app.rag.retrieve_hybrid',wraps=retrieve_hybrid) as retrieval:
            with patch('app.case_generations.resolve_context',side_effect=interrupt_after_snapshot):
                self.submit();task=self.wait_status('failed')
            self.assertFalse(task['requires_knowledge_retry'])
            self.assertEqual(task['checkpoint_nodes'],[])
            self.assertEqual(self.calls,0)
            self.assertEqual(self.action('resume').status_code,200)
            self.wait_status('succeeded')
            self.assertEqual(retrieval.call_count,1)
            self.assertEqual(self.calls,1)
