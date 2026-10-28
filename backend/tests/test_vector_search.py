import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import KnowledgeDocument, VectorBuild, VectorIndex
from app.vector_search import profile, settle_vector_builds
from app.embedding_settings import load_embedding_config


class VectorSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'vectors.sqlite3'
        self.calls, self.behavior, self.gate, self.mutate = [], 'success', None, None
        self.entered = threading.Event()
        async def handler(request):
            body = json.loads(request.content)
            self.calls.append(body)
            self.entered.set()
            if self.gate:
                for _ in range(600):
                    if self.gate.is_set(): break
                    await asyncio.sleep(.01)
            if self.mutate: self.mutate()
            if self.behavior == 'fail': return httpx.Response(429, text='private-test-key')
            dim = 2 if self.behavior == 'dimension' else 3
            return httpx.Response(200, json={'data': [
                {'index': i, 'embedding': ([0, 1, 0] if '退款' in value else [1, 0, 0])[:dim]}
                for i, value in enumerate(body['input'])]})
        self.transport = httpx.MockTransport(handler)
        self.app = create_app(self.path, embedding_transport=self.transport)
        self.client = TestClient(self.app).__enter__()
        self.pid = self.client.post('/api/projects', json={'name': '向量测试'}).json()['id']
        self.base = f'/api/projects/{self.pid}/vector-index'
        self.docs = f'/api/projects/{self.pid}/knowledge-documents'
        self.config = {'base_url': 'https://embedding.invalid/v1', 'model': 'test-semantic',
                       'api_key': 'private-test-key', 'mock_mode': False, 'timeout_seconds': 5,
                       'expected_dimensions': None}
        self.assertEqual(self.client.put('/api/settings/embedding', json=self.config).status_code, 200)

    def tearDown(self):
        if self.gate: self.gate.set()
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def document(self, content='登录验证码过期需要重新获取。', title='登录规则'):
        response = self.client.post(self.docs, json={'document_id': str(uuid4()), 'title': title,
                                                   'content': content, 'source_filename': 'rules.md'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def build(self, request_id=None):
        response = self.client.post(self.base + '/builds', json={'request_id': request_id or str(uuid4())})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def wait(self, expected='succeeded'):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = self.client.get(self.base).json()
            if status['task'] and status['task']['status'] not in ('queued', 'running'):
                self.assertEqual(status['task']['status'], expected, status)
                return status
            time.sleep(.05)
        self.fail('索引任务超时')

    def search(self, query='短信失效怎么办', top_k=5):
        return self.client.post(self.base + '/search', json={'query': query, 'top_k': top_k})

    def test_ranking_sources_idempotency_and_persistence(self):
        first = self.document()
        self.document('退款只能原路返回支付账户。', '退款规则')
        self.assertEqual(self.search().status_code, 409)
        self.assertEqual(self.calls, [])
        task = self.build(); status = self.wait()
        self.assertEqual((status['status'], status['dimensions'], status['indexed_chunk_count']), ('ready', 3, 2))
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        hit = response.json()['hits'][0]
        self.assertEqual(hit['document_id'], first['id'])
        self.assertEqual(hit['content'], first['chunks'][0]['content'])
        self.assertAlmostEqual(hit['score'], 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.build(task['id'])['status'], 'succeeded')
        self.client.__exit__(None, None, None)
        self.app = create_app(self.path, embedding_transport=self.transport)
        self.client = TestClient(self.app).__enter__()
        self.assertEqual(self.client.get(self.base).json()['status'], 'ready')
        self.assertEqual(self.search('退款退到哪里', 1).json()['hits'][0]['document_title'], '退款规则')

    def test_empty_validation_no_provider_calls(self):
        self.assertEqual(self.client.get(self.base).json()['status'], 'empty')
        self.assertEqual(self.client.post(self.base+'/builds', json={'request_id': str(uuid4())}).status_code, 400)
        for payload in ({'query': ' '}, {'query': '\ud800'}, {'query': 'a'*201}, {'query': '正常', 'top_k': True}, {'query': '正常', 'top_k': 21}):
            self.assertEqual(self.client.post(self.base+'/search', content=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_document_update_delete_invalidate_before_provider_calls(self):
        doc = self.document(); self.build(); self.wait()
        calls = len(self.calls)
        response = self.client.put(self.docs+'/'+doc['id'], json={
            'expected_revision': 1, 'title': '新版', 'content': '新的验证码规则。', 'source_filename': ''})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(self.base).json()['status'], 'stale')
        self.assertEqual(self.search().status_code, 409)
        self.assertEqual(len(self.calls), calls)
        self.build(); self.wait()
        self.assertEqual(self.search().json()['hits'][0]['document_revision'], 2)
        self.client.delete(self.docs+'/'+doc['id']+'?expected_revision=2')
        self.assertEqual(self.client.get(self.base).json()['status'], 'empty')
        self.assertEqual(self.search().status_code, 409)

    def test_project_isolation_and_cascade(self):
        self.document(); self.build(); self.wait()
        other = self.client.post('/api/projects', json={'name': '其他项目'}).json()['id']
        before = len(self.calls)
        self.assertEqual(self.client.post(f'/api/projects/{other}/vector-index/search', json={'query':'验证码'}).status_code, 409)
        self.assertEqual(len(self.calls), before)
        self.client.delete(f'/api/projects/{self.pid}')
        self.assertEqual(self.search().status_code, 404)
        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(VectorIndex, self.pid))
            self.assertEqual(list(session.scalars(select(VectorBuild))), [])

    def test_profile_changes_but_key_and_timeout_do_not_invalidate(self):
        self.document(); self.build(); self.wait()
        self.client.put('/api/settings/embedding', json={**self.config, 'api_key': 'new-private-key', 'timeout_seconds': 10})
        self.assertEqual(self.client.get(self.base).json()['status'], 'ready')
        self.client.put('/api/settings/embedding', json={**self.config, 'model': 'another-model'})
        self.assertEqual(self.search().status_code, 409)
        self.assertEqual(self.client.get(self.base).json()['status'], 'stale')
        self.assertEqual(len(self.calls), 1)

    def test_failed_rebuild_preserves_old_index_and_redacts_error(self):
        self.document(); self.build(); self.wait()
        with self.app.state.session_factory() as session:
            old = session.get(VectorIndex, self.pid).collection
        self.behavior = 'fail'
        self.build(); status = self.wait('failed')
        self.assertEqual(status['status'], 'ready')
        self.assertNotIn('private-test-key', json.dumps(status))
        with self.app.state.session_factory() as session:
            self.assertEqual(session.get(VectorIndex, self.pid).collection, old)
        self.behavior = 'success'
        self.assertEqual(self.search().status_code, 200)

    def test_active_build_idempotency_and_document_race(self):
        self.document(); self.gate = threading.Event()
        task = self.build()
        self.assertTrue(self.entered.wait(10))
        self.assertIn(self.build(task['id'])['status'], ['queued','running'])
        self.assertEqual(self.client.post(self.base+'/builds', json={'request_id':str(uuid4())}).status_code, 409)
        self.document('退款规则'); self.gate.set()
        status = self.wait('failed')
        self.assertEqual(status['indexed_chunk_count'], 0)
        self.assertEqual(len(self.calls), 1)

    def test_query_race_rejects_old_source(self):
        self.document(); self.build(); self.wait()
        def mutate():
            with self.app.state.session_factory() as session:
                doc = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.project_id==self.pid))
                doc.revision += 1; session.commit()
        self.mutate = mutate
        self.assertEqual(self.search().status_code, 409)

    def test_batches_dimension_consistency(self):
        self.document('甲' * 12000)
        def mutate(): self.behavior = 'dimension' if len(self.calls)==1 else 'success'
        self.mutate = mutate
        self.build(); status = self.wait('failed')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.calls[0]['input']), 16)
        self.assertEqual(status['task']['completed'], 16)
        self.assertEqual(status['indexed_chunk_count'], 0)

    def test_restart_interrupts_without_replay(self):
        self.document()
        with self.app.state.session_factory() as session:
            task = VectorBuild(id=str(uuid4()), project_id=self.pid, status='running', corpus_signature='snapshot',
                               profile=profile(load_embedding_config(session)), total=10, completed=3)
            session.add(task); session.commit()
        settle_vector_builds(self.app)
        status = self.client.get(self.base).json()
        self.assertEqual(status['task']['status'], 'interrupted')
        self.assertEqual(status['task']['completed'], 3)
        self.assertEqual(self.calls, [])

    def test_status_uses_one_snapshot_and_releases_reader_before_response(self):
        from sqlalchemy import event, update
        self.document();self.build();self.wait()
        reader_started=threading.Event();writer_updated=threading.Event();writer_done=threading.Event()
        failures=[];threads=[]
        def writer():
            try:
                with self.app.state.session_factory() as session:
                    session.execute(update(VectorIndex).where(VectorIndex.project_id==self.pid).values(dimensions=8))
                    session.execute(update(VectorBuild).where(VectorBuild.project_id==self.pid).values(status='failed'))
                    writer_updated.set();session.commit()
            except Exception as exc:failures.append(exc)
            finally:writer_done.set()
        def after_read(connection,cursor,statement,parameters,context,many):
            if not reader_started.is_set() and statement.lstrip().startswith('SELECT') and 'FROM vector_indexes' in statement:
                reader_started.set();thread=threading.Thread(target=writer);threads.append(thread);thread.start()
                self.assertTrue(writer_updated.wait(3))
        engine=self.app.state.session_factory.kw['bind']
        event.listen(engine,'after_cursor_execute',after_read)
        try:status=self.client.get(self.base).json()
        finally:event.remove(engine,'after_cursor_execute',after_read)
        self.assertTrue(writer_done.wait(3))
        for thread in threads:thread.join()
        self.assertEqual(failures,[])
        self.assertEqual(status['dimensions'],3)
        self.assertEqual(status['task']['status'],'succeeded')
        self.assertEqual(self.client.get(self.base).json()['dimensions'],8)

    def test_mock_and_query_dimension_mismatch(self):
        self.document(); self.build(); self.wait()
        self.behavior = 'dimension'
        self.assertEqual(self.search().status_code, 409)
        self.client.put('/api/settings/embedding', json={**self.config, 'mock_mode': True, 'expected_dimensions': 8})
        self.build(); status = self.wait()
        calls = len(self.calls)
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['mock_mode'])
        self.assertIn('不具备语义', response.json()['message'])
        self.assertEqual(status['dimensions'], 8)
        self.assertEqual(len(self.calls), calls)

    def test_storage_failure_is_sanitized(self):
        self.document(); self.build(); self.wait()
        with patch.object(self.app.state.vector_store, 'query', side_effect=RuntimeError('private-test-key')):
            response = self.search()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-test-key', response.text)
