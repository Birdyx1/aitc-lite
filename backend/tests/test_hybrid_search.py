import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.embedding_settings import load_embedding_config
from app.knowledge.bm25 import corpus_signature, read_chunks
from app.knowledge.rrf import fuse_rankings
from app.main import create_app
from app.models import EmbeddingSettings, KnowledgeDocument, VectorIndex
from app.vector_search import profile


class RRFTests(unittest.TestCase):
    def test_formula_dedup_and_ranks(self):
        result = fuse_rankings({'bm25':['a','b','a'],'vector':['b','c']})
        self.assertEqual([row[0] for row in result],['b','a','c'])
        self.assertAlmostEqual(result[0][1],1/62+1/61)
        self.assertEqual(result[0][2],{'bm25':2,'vector':1})
        self.assertAlmostEqual(result[1][1],1/61)

    def test_stable_ties_empty(self):
        a=fuse_rankings({'bm25':['z'],'vector':['a']})
        self.assertEqual(a,fuse_rankings({'vector':['a'],'bm25':['z']}))
        self.assertEqual([row[0] for row in a],['a','z'])
        self.assertEqual(fuse_rankings({'bm25':[],'vector':[]}),[])


class HybridApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.calls=[]
        self.fail=False
        self.mutate=None
        def respond(request):
            self.calls.append(json.loads(request.content))
            if self.mutate: self.mutate()
            if self.fail: return httpx.Response(429,text='private-test-key')
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1,0]} for i,_ in enumerate(self.calls[-1]['input'])]})
        self.app=create_app(Path(self.temp.name)/'test.sqlite3',embedding_transport=httpx.MockTransport(respond))
        self.client=TestClient(self.app).__enter__()
        self.pid=self.client.post('/api/projects',json={'name':'混合检索'}).json()['id']
        self.url=f'/api/projects/{self.pid}/hybrid-search'
        self.docs=f'/api/projects/{self.pid}/knowledge-documents'
        self.config={'base_url':'https://embedding.invalid/v1','model':'test','api_key':'private-test-key','mock_mode':False,'timeout_seconds':5,'expected_dimensions':2}
        self.client.put('/api/settings/embedding',json=self.config)
        self.documents=[]
        for title,content in [('登录规则','验证码 E1001 过期请重试。'),('短信规则','登录短信失效后需重新申请。'),('退款规则','退款原路退回付款账户。')]:
            self.documents.append(self.client.post(self.docs,json={'document_id':str(uuid4()),'title':title,'content':content,'source_filename':''}).json())
        with self.app.state.session_factory() as session:
            chunks=read_chunks(session,self.pid)
            session.add(VectorIndex(project_id=self.pid,collection='hybrid-test-index',corpus_signature=corpus_signature(chunks),profile=profile(load_embedding_config(session)),dimensions=2,chunk_count=3,model='test',mock_mode=False))
            session.commit()
        self.ids=[doc['chunks'][0]['id'] for doc in self.documents]
        self.vector_rows=[(self.ids[1],0.0),(self.ids[0],.2),(self.ids[2],1.0)]
        self.store_patch=patch.object(self.app.state.vector_store,'query',side_effect=lambda name,vec,count:self.vector_rows[:count])
        self.query_mock=self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def search(self,query='验证码 E1001',**kwargs):
        return self.client.post(self.url,json={'query':query,**kwargs})

    def test_exact_fusion_dedup_sources(self):
        response=self.search(); self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['mode'],'fused'); self.assertFalse(result['degraded'])
        self.assertEqual(result['overlap_count'],1); self.assertEqual(result['unique_candidate_count'],3)
        self.assertEqual(len({h['chunk_id'] for h in result['hits']}),3)
        first=result['hits'][0]
        self.assertEqual(first['chunk_id'],self.ids[0])
        self.assertEqual((first['bm25_rank'],first['vector_rank']),(1,2))
        self.assertAlmostEqual(first['score'],1/61+1/62)
        self.assertAlmostEqual(first['score'],first['bm25_contribution']+first['vector_contribution'])
        self.assertEqual(first['content'],self.documents[0]['content'])
        self.assertEqual(len(self.calls),1); self.assertEqual(self.calls[0]['input'],['验证码 E1001'])

    def test_candidate_window_before_final_top_k(self):
        result=self.search(top_k=1,candidate_k=2).json()
        self.assertEqual(len(result['hits']),1); self.assertEqual(result['unique_candidate_count'],2)
        self.assertEqual(result['routes']['vector']['candidate_count'],2)
        self.assertEqual(self.query_mock.call_args.args[2],2)

    def test_missing_index_degrades_without_paid_call(self):
        with self.app.state.session_factory() as s:
            s.delete(s.get(VectorIndex,self.pid)); s.commit()
        result=self.search().json()
        self.assertTrue(result['degraded']); self.assertEqual(result['mode'],'bm25_only')
        self.assertEqual(result['routes']['vector']['status'],'missing'); self.assertEqual(self.calls,[])
        self.assertIsNone(result['hits'][0]['vector_rank']); self.assertEqual(result['hits'][0]['vector_contribution'],0)

    def test_unconfigured_stale_current_bm25(self):
        with self.app.state.session_factory() as s:
            s.delete(s.get(EmbeddingSettings,1)); s.commit()
        self.assertEqual(self.search().json()['routes']['vector']['status'],'unconfigured')
        self.client.put('/api/settings/embedding',json=self.config)
        self.client.put(self.docs+'/'+self.documents[0]['id'],json={'expected_revision':1,'title':'新版','content':'验证码 E1001 新的规则。','source_filename':''})
        result=self.search().json()
        self.assertEqual(result['routes']['vector']['status'],'stale')
        self.assertEqual(result['hits'][0]['document_revision'],2)
        self.assertIn('新的规则',result['hits'][0]['content']); self.assertEqual(self.calls,[])

    def test_vector_service_and_store_failure_sanitized(self):
        self.fail=True
        response=self.search()
        self.assertEqual(response.status_code,200); self.assertEqual(response.json()['mode'],'bm25_only')
        self.assertEqual(response.json()['routes']['vector']['status'],'error'); self.assertNotIn('private-test-key',response.text)
        self.fail=False; self.query_mock.side_effect=RuntimeError('private-test-key')
        response=self.search()
        self.assertTrue(response.json()['degraded']); self.assertNotIn('private-test-key',response.text)

    def test_bm25_failure_vector_only_and_both_failed(self):
        with patch('app.hybrid_search.rank_chunks',side_effect=RuntimeError('private-test-key')):
            response=self.search()
            self.assertEqual(response.json()['mode'],'vector_only'); self.assertTrue(response.json()['degraded'])
            self.assertEqual(response.json()['routes']['bm25']['status'],'error')
            self.fail=True; response=self.search()
            self.assertEqual(response.status_code,503); self.assertNotIn('private-test-key',response.text)

    def test_natural_empty_and_no_terms_not_failure(self):
        result=self.search('ZXQ999').json()
        self.assertFalse(result['degraded']); self.assertEqual(result['mode'],'vector_only')
        self.assertEqual(result['routes']['bm25']['status'],'empty')
        result=self.search('😀').json()
        self.assertEqual(result['routes']['bm25']['status'],'no_terms'); self.assertFalse(result['degraded'])

    def test_project_isolation_empty_and_deleted(self):
        other=self.client.post('/api/projects',json={'name':'空项目'}).json()['id']
        result=self.client.post(f'/api/projects/{other}/hybrid-search',json={'query':'验证码'}).json()
        self.assertEqual(result['mode'],'empty'); self.assertEqual(result['hits'],[]); self.assertEqual(self.calls,[])
        self.client.delete(f'/api/projects/{self.pid}'); self.assertEqual(self.search().status_code,404)

    def test_document_race_rejected_even_with_vector_failure(self):
        def mutate():
            with self.app.state.session_factory() as s:
                s.get(KnowledgeDocument,self.documents[0]['id']).revision+=1; s.commit()
        self.mutate=mutate; self.fail=True
        self.assertEqual(self.search().status_code,409)

    def test_config_and_index_races_rejected(self):
        def mutate():
            with self.app.state.session_factory() as s:
                s.get(EmbeddingSettings,1).model='new-model'; s.commit()
        self.mutate=mutate; self.assertEqual(self.search().status_code,409)
        self.client.put('/api/settings/embedding',json=self.config)
        def replace_index():
            with self.app.state.session_factory() as s:
                s.get(VectorIndex,self.pid).collection='new-index'; s.commit()
        self.mutate=replace_index; self.assertEqual(self.search().status_code,409)

    def test_validation_no_embedding_calls(self):
        for extra in ({'query':' '},{'query':'x'*201},{'query':'\ud800'},{'top_k':True},{'candidate_k':51},{'candidate_k':False},{'top_k':10,'candidate_k':5},{'unknown':'secret'}):
            response=self.client.post(self.url,content=json.dumps({'query':'验证码',**extra}).encode(),headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.calls,[])

    def test_foreign_chunk_not_returned(self):
        self.vector_rows=[('foreign-project-chunk',0.0)]
        response=self.search()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['routes']['vector']['status'],'error')
        self.assertEqual([h['chunk_id'] for h in response.json()['hits']],[self.ids[0]])

    def test_chroma_integration_mock_explicit(self):
        self.store_patch.stop()
        response=self.client.post(f'/api/projects/{self.pid}/vector-index/builds',json={'request_id':str(uuid4())})
        self.assertEqual(response.status_code,202,response.text)
        for _ in range(100):
            status=self.client.get(f'/api/projects/{self.pid}/vector-index').json()
            if status['task']['status'] not in ('queued','running'): break
            time.sleep(.05)
        self.assertEqual(status['task']['status'],'succeeded',status)
        result=self.search().json(); self.assertEqual(result['mode'],'fused'); self.assertEqual(len(result['hits']),3)
        self.client.put('/api/settings/embedding',json={**self.config,'mock_mode':True})
        result=self.search().json(); self.assertTrue(result['mock_mode']); self.assertTrue(result['degraded'])
