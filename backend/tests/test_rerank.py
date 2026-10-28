import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

import test_hybrid_search as fixtures
from app.main import create_app
from app.models import KnowledgeDocument, RerankSettings, Project
from app.reranking import RerankConfig, rerank_texts, parse_results

URL='/api/settings/rerank'
KEY='private-rerank-test-key'


class RerankProtocolTests(unittest.TestCase):
    def test_complete_index_mapping_sort_and_ties(self):
        self.assertEqual(parse_results({'results':[{'index':1,'relevance_score':3.0},{'index':0,'relevance_score':3}]},2),[(0,3.0),(1,3.0)])
        self.assertEqual(parse_results({'results':[{'index':0,'relevance_score':-20},{'index':1,'relevance_score':10}]},2),[(1,10.0),(0,-20.0)])

    def test_invalid_partial_duplicate_and_nonfinite_rejected(self):
        bodies=[None,{}, {'results':[]},{'results':[{'index':0,'relevance_score':1}]}]
        for index, score in [(True,1),(2,1),(-1,1),(0,True),(0,'0.5'),(0,float('nan')),(0,float('inf')),(0,10**1000)]:
            bodies.append({'results':[{'index':index,'relevance_score':score},{'index':1,'relevance_score':1}]})
        bodies.append({'results':[{'index':0,'relevance_score':1}]*2})
        for body in bodies:
            with self.assertRaises(HTTPException): parse_results(body,2)

    def test_mock_no_network_input_guard(self):
        config=RerankConfig('','',KEY,True,5)
        result=asyncio.run(rerank_texts(config,'验证码',['退款规则','验证码过期重试']))
        self.assertEqual(result[0][0],1)
        self.assertNotIn(KEY,repr(config))
        for docs in ('text',[],[''],['a']*51):
            with self.assertRaises(ValueError): asyncio.run(rerank_texts(config,'问题',docs))


class RerankSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'settings.sqlite3'
        self.calls=[]
        self.behavior='success'
        def respond(request):
            self.calls.append(request)
            if self.behavior=='timeout': raise httpx.ReadTimeout(KEY,request=request)
            if self.behavior=='network': raise httpx.ConnectError(KEY,request=request)
            if self.behavior=='json': return httpx.Response(200,text=KEY)
            if self.behavior=='oversize': return httpx.Response(200,content=b'x'*(1024*1024+1))
            if isinstance(self.behavior,int): return httpx.Response(self.behavior,text=KEY,headers={'Location':'https://other.invalid'})
            if self.behavior=='partial': return httpx.Response(200,json={'results':[{'index':0,'relevance_score':1}]})
            return httpx.Response(200,json={'results':[{'index':0,'relevance_score':.1},{'index':1,'relevance_score':.9}], 'private':KEY})
        self.transport=httpx.MockTransport(respond)
        self.client=TestClient(create_app(self.path,rerank_transport=self.transport)).__enter__()
        self.config={'base_url':'https://rerank.invalid/v1','model':'reranker-test','api_key':KEY,'timeout_seconds':5,'mock_mode':False}

    def tearDown(self):
        self.client.__exit__(None,None,None);self.temp.cleanup()

    def save(self,**extra):
        response=self.client.put(URL,json={**self.config,**extra})
        self.assertEqual(response.status_code,200,response.text)
        return response

    def test_unconfigured_and_independent_settings(self):
        self.assertFalse(self.client.get(URL).json()['configured'])
        self.assertEqual(self.client.post(URL+'/test').status_code,400)
        before=[self.client.get('/api/settings/'+kind).json() for kind in ('generation','embedding')]
        response=self.save()
        self.assertNotIn(KEY,response.text)
        self.assertEqual(before,[self.client.get('/api/settings/'+kind).json() for kind in ('generation','embedding')])
        self.assertEqual(self.calls,[])

    def test_key_keep_replace_clear_and_root_change_guard(self):
        self.save(); self.save(api_key='')
        self.assertTrue(self.client.get(URL).json()['has_api_key'])
        response=self.client.put(URL,json={**self.config,'api_key':'','base_url':'https://other.invalid/v1'})
        self.assertEqual(response.status_code,400)
        self.save(api_key='replacement-key')
        self.client.post(URL+'/test')
        self.assertEqual(self.calls[-1].headers['Authorization'],'Bearer replacement-key')
        self.save(api_key='',clear_api_key=True)
        self.assertFalse(self.client.get(URL).json()['has_api_key'])
        self.client.post(URL+'/test');self.assertNotIn('Authorization',self.calls[-1].headers)

    def test_protocol_fixed_probe_and_no_raw_text_from_provider(self):
        self.save()
        response=self.client.post(URL+'/test')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['order'],[2,1])
        req=self.calls[0];body=json.loads(req.content)
        self.assertEqual(str(req.url),'https://rerank.invalid/v1/rerank')
        self.assertEqual(body['model'],'reranker-test')
        self.assertEqual(body['top_n'],2);self.assertFalse(body['return_documents'])
        self.assertEqual(len(body['documents']),2)
        self.assertEqual(req.headers['Authorization'],'Bearer '+KEY)
        self.assertNotIn(KEY,response.text)

    def test_validation_errors_redacted_and_no_overwrite(self):
        self.save();before=self.client.get(URL).json()
        for extra in ({'base_url':'https://a.invalid/v1/rerank'},{'base_url':'https://a.invalid/v1/embeddings'},
                      {'base_url':'https://a.invalid?'},{'base_url':'https://a.invalid#'},{'base_url':'file:///tmp/a'},
                      {'model':'\ud800'},{'api_key':'not ascii 中'},{'api_key':KEY,'clear_api_key':True},
                      {'timeout_seconds':True},{'timeout_seconds':61},{'expected_dimensions':8}):
            response=self.client.put(URL,content=json.dumps({**self.config,**extra}).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text);self.assertNotIn(KEY,response.text)
        self.assertEqual(self.client.get(URL).json(),before)

    def test_provider_errors_are_sanitized_never_retry(self):
        self.save()
        for behavior,status in [(401,502),(403,502),(429,502),(500,502),(302,502),('timeout',504),('network',502),('json',502),('oversize',502),('partial',502)]:
            self.behavior=behavior;before=len(self.calls)
            response=self.client.post(URL+'/test')
            self.assertEqual(response.status_code,status,response.text)
            self.assertNotIn(KEY,response.text);self.assertEqual(len(self.calls),before+1)

    def test_restart_persistence_and_mock_explicit(self):
        self.save(mock_mode=True)
        self.client.__exit__(None,None,None)
        self.client=TestClient(create_app(self.path,rerank_transport=self.transport)).__enter__()
        self.assertTrue(self.client.get(URL).json()['has_api_key'])
        response=self.client.post(URL+'/test')
        self.assertEqual(response.json()['mode'],'mock');self.assertIn('不代表语义',response.json()['message'])
        self.assertEqual(self.calls,[])


class RerankIntegrationTests(unittest.TestCase):
    # 复用模块 14 的独立项目/分块/模拟向量索引夹具，不重复执行它的测试。
    document_search=fixtures.HybridApiTests.search
    tearDown=fixtures.HybridApiTests.tearDown

    def setUp(self):
        fixtures.HybridApiTests.setUp(self)
        self.rerank_calls=[]
        self.rerank_behavior='success'
        self.rerank_mutate=None
        def respond(request):
            body=json.loads(request.content);self.rerank_calls.append(body)
            if self.rerank_mutate:self.rerank_mutate()
            if self.rerank_behavior=='fail':return httpx.Response(429,text=KEY)
            if self.rerank_behavior=='partial':return httpx.Response(200,json={'results':[]})
            return httpx.Response(200,json={'results':[{'index':i,'relevance_score':i+.1} for i,_ in enumerate(body['documents'])]})
        self.app.state.rerank_transport=httpx.MockTransport(respond)
        self.rcfg={'base_url':'https://rerank.invalid/v1','model':'reranker-test','api_key':KEY,'timeout_seconds':5,'mock_mode':False}
        self.assertEqual(self.client.put(URL,json=self.rcfg).status_code,200)

    def search(self,**kwargs):return self.document_search(use_rerank=True,**kwargs)

    def test_default_disabled_never_calls_rerank(self):
        result=self.document_search().json()
        self.assertEqual(result['rerank']['status'],'disabled');self.assertEqual(self.rerank_calls,[])

    def test_rerank_before_final_top_k_preserves_rrf_and_scores(self):
        before=self.document_search().json()
        response=self.search(top_k=1);self.assertEqual(response.status_code,200,response.text)
        after=response.json();hit=after['hits'][0]
        self.assertEqual(hit['chunk_id'],before['hits'][-1]['chunk_id'])
        self.assertEqual((hit['rrf_rank'],hit['rank']),(3,1))
        self.assertAlmostEqual(hit['score'],before['hits'][-1]['score'])
        self.assertEqual(hit['rerank_score'],2.1)
        self.assertEqual(after['rerank']['status'],'applied')
        self.assertEqual(self.rerank_calls[0]['documents'],[h['content'] for h in before['hits']])
        self.assertEqual(self.rerank_calls[0]['top_n'],3)

    def test_rerank_candidate_cap(self):
        result=self.search(top_k=1,rerank_k=1).json()
        self.assertEqual(result['rerank']['candidate_count'],1)
        self.assertEqual(result['hits'][0]['rrf_rank'],1)
        self.assertEqual(len(self.rerank_calls[0]['documents']),1)

    def test_fail_or_partial_restores_exact_rrf_order(self):
        before=self.document_search().json()
        for behavior in ('fail','partial'):
            self.rerank_behavior=behavior
            response=self.search();after=response.json()
            self.assertEqual(after['rerank']['status'],'fallback');self.assertTrue(after['degraded'])
            self.assertEqual([h['chunk_id'] for h in after['hits']],[h['chunk_id'] for h in before['hits']])
            self.assertTrue(all(h['rerank_score'] is None for h in after['hits']))
            self.assertNotIn(KEY,response.text)

    def test_unconfigured_fallback_without_call(self):
        with self.app.state.session_factory() as s:s.delete(s.get(RerankSettings,1));s.commit()
        result=self.search().json()
        self.assertEqual(result['rerank']['status'],'unconfigured');self.assertTrue(result['degraded'])
        self.assertEqual(self.rerank_calls,[])

    def test_document_change_during_rerank_even_failure_is_conflict(self):
        def mutate():
            with self.app.state.session_factory() as s:s.get(KnowledgeDocument,self.documents[0]['id']).revision+=1;s.commit()
        self.rerank_mutate=mutate;self.rerank_behavior='fail'
        self.assertEqual(self.search().status_code,409)

    def test_rerank_config_change_is_conflict(self):
        def mutate():
            with self.app.state.session_factory() as s:s.get(RerankSettings,1).model='changed';s.commit()
        self.rerank_mutate=mutate
        self.assertEqual(self.search().status_code,409)

    def test_deleted_project_is_not_fallback(self):
        def mutate():
            with self.app.state.session_factory() as s:s.delete(s.get(Project,self.pid));s.commit()
        self.rerank_mutate=mutate
        self.assertEqual(self.search().status_code,404)

    def test_empty_skips_rerank(self):
        pid=self.client.post('/api/projects',json={'name':'空'}).json()['id']
        result=self.client.post(f'/api/projects/{pid}/hybrid-search',json={'query':'验证码','use_rerank':True}).json()
        self.assertEqual(result['rerank']['status'],'empty');self.assertEqual(self.rerank_calls,[])

    def test_mock_status_and_validation(self):
        self.client.put(URL,json={**self.rcfg,'mock_mode':True})
        result=self.search().json()
        self.assertEqual(result['rerank']['status'],'applied');self.assertTrue(result['rerank']['mock_mode'])
        self.assertEqual(self.rerank_calls,[])
        before=len(self.calls)
        for body in ({'use_rerank':'true'},{'use_rerank':True,'rerank_k':True},{'use_rerank':True,'rerank_k':2,'top_k':5},{'rerank_k':51}):
            self.assertEqual(self.client.post(self.url,json={'query':'验证码',**body}).status_code,422)
        self.assertEqual(len(self.calls),before)
