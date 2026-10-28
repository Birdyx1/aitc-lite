import json
import random
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.knowledge.chunking import CHUNK_OVERLAP, CHUNK_SIZE, split_document, text_hash
from app.main import create_app
from app.models import KnowledgeChunk, KnowledgeDocument


class ChunkingTests(unittest.TestCase):
    def test_short_text_keeps_whitespace_and_source_offsets(self):
        text = '\n  # 登录\r\n  手机号验证\n'
        chunks = split_document('document', 1, text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]['content'], text)
        self.assertEqual((chunks[0]['start_offset'], chunks[0]['end_offset']), (0, len(text)))
        self.assertEqual(chunks[0]['content_hash'], text_hash(text))

    def test_long_unbroken_text_is_bounded_with_exact_overlap(self):
        chunks = split_document('document', 1, '中' * 2500)
        self.assertTrue(all(len(chunk['content']) <= CHUNK_SIZE for chunk in chunks))
        self.assertEqual(chunks[-1]['end_offset'], 2500)
        for first, second in zip(chunks, chunks[1:]):
            self.assertEqual(first['end_offset'] - second['start_offset'], CHUNK_OVERLAP)
        self.assertEqual([chunk['position'] for chunk in chunks], list(range(1, len(chunks) + 1)))

    def test_paragraph_boundary_preferred_to_sentence_and_hard_cut(self):
        text = '甲' * 550 + '\n\n' + '乙' * 170 + '。' + '丙' * 500
        chunks = split_document('document', 1, text)
        self.assertEqual(chunks[0]['end_offset'], 552)
        self.assertEqual(chunks[0]['content'], text[:552])

    def test_ids_stable_within_revision_and_change_for_other_versions_or_documents(self):
        first = split_document('one', 1, '内容' * 500)
        self.assertEqual(first, split_document('one', 1, '内容' * 500))
        ids = {chunk['id'] for chunk in first}
        self.assertTrue(ids.isdisjoint({chunk['id'] for chunk in split_document('one', 2, '内容' * 500)}))
        self.assertTrue(ids.isdisjoint({chunk['id'] for chunk in split_document('two', 1, '内容' * 500)}))

    def test_varied_unicode_chunks_preserve_all_non_whitespace_content(self):
        rng = random.Random(42)
        for length in (1, 799, 800, 801, 1600, 10000):
            text = ''.join(rng.choice(['中', 'A', '😀', '\n', '\r', ' ', '。', 'é']) for _ in range(length))
            chunks = split_document('document', 1, text)
            covered = set()
            for chunk in chunks:
                start, end = chunk['start_offset'], chunk['end_offset']
                self.assertEqual(chunk['content'], text[start:end])
                self.assertLessEqual(end - start, CHUNK_SIZE)
                self.assertTrue(chunk['content'].strip())
                covered.update(range(start, end))
            self.assertTrue(all(i in covered for i, char in enumerate(text) if not char.isspace()))

    def test_blank_windows_are_skipped_without_losing_meaningful_tail(self):
        text = '开头' + ' ' * 2500 + '末尾😀'
        chunks = split_document('document', 1, text)
        self.assertTrue(all(chunk['content'].strip() for chunk in chunks))
        self.assertTrue(chunks[-1]['content'].endswith('末尾😀'))
        self.assertEqual(split_document('document', 1, ' ' * 2000), [])


class KnowledgeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'knowledge.sqlite3'
        self.app = create_app(self.path)
        self.client = TestClient(self.app).__enter__()
        self.pid = self.client.post('/api/projects', json={'name': '知识项目'}).json()['id']
        self.other = self.client.post('/api/projects', json={'name': '其他项目'}).json()['id']
        self.base = f'/api/projects/{self.pid}/knowledge-documents'
        self.payload = {'document_id': str(uuid4()), 'title': ' 登录规则 ',
                        'content': '\n  账号需验证。\r\n' + '业务规则。' * 350, 'source_filename': '登录规则.md'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def create(self):
        response = self.client.post(self.base, json=self.payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def update_payload(self, document, **overrides):
        return {'title': document['title'], 'content': document['content'],
                'source_filename': document['source_filename'], 'expected_revision': document['revision'], **overrides}

    def test_crud_atomic_rechunk_noop_and_restart(self):
        original = self.create()
        url = self.base + '/' + original['id']
        self.assertEqual(original['title'], '登录规则')
        self.assertEqual(original['content'], self.payload['content'])
        self.assertEqual(original['char_count'], len(original['content']))
        listing = self.client.get(self.base).json()[0]
        self.assertNotIn('content', listing)
        self.assertNotIn('chunks', listing)
        self.assertEqual(listing['chunk_count'], len(original['chunks']))
        self.assertEqual(self.client.put(url, json=self.update_payload(original)).json(), original)
        updated = self.client.put(url, json=self.update_payload(original, content='  新规则😀\n')).json()
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['chunk_count'], 1)
        self.assertEqual(updated['chunks'][0]['content'], '  新规则😀\n')
        self.assertEqual(updated['chunks'][0]['document_revision'], 2)
        old_ids = {item['id'] for item in original['chunks']}
        self.assertNotIn(updated['chunks'][0]['id'], old_ids)
        with self.app.state.session_factory() as session:
            self.assertTrue(old_ids.isdisjoint({item.id for item in session.scalars(select(KnowledgeChunk)).all()}))
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(url).json(), updated)
        self.assertEqual(self.client.delete(url, params={'expected_revision': 2}).status_code, 204)
        self.assertEqual(self.client.get(self.base).json(), [])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_duplicate_create_replays_and_changed_payload_conflicts(self):
        document = self.create()
        replay = self.client.post(self.base, json=self.payload)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), document)
        self.assertEqual(self.client.post(self.base, json={**self.payload, 'content': '不同正文'}).status_code, 409)
        with self.app.state.session_factory() as session:
            self.assertEqual(len(session.scalars(select(KnowledgeDocument)).all()), 1)
            self.assertEqual(len(session.scalars(select(KnowledgeChunk)).all()), document['chunk_count'])

    def test_concurrent_create_and_update_do_not_duplicate_or_overwrite(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.client.post(self.base, json=self.payload), range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 201])
        document = responses[0].json()
        url = self.base + '/' + document['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda title: self.client.put(url, json=self.update_payload(document, title=title)), ['先到', '后到']))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(self.client.get(url).json()['revision'], 2)

    def test_stale_update_and_delete_require_current_revision(self):
        document = self.create()
        url = self.base + '/' + document['id']
        saved = self.client.put(url, json=self.update_payload(document, content='先保存的修改')).json()
        self.assertEqual(self.client.put(url, json=self.update_payload(document, content='过期覆盖')).status_code, 409)
        self.assertEqual(self.client.delete(url, params={'expected_revision': 1}).status_code, 409)
        self.assertEqual(self.client.delete(url).status_code, 422)
        self.assertEqual(self.client.get(url).json(), saved)

    def test_cross_project_and_missing_document_are_hidden(self):
        document = self.create()
        other_base = f'/api/projects/{self.other}/knowledge-documents'
        wrong = other_base + '/' + document['id']
        self.assertEqual(self.client.get(other_base).json(), [])
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.put(wrong, json=self.update_payload(document)).status_code, 404)
        self.assertEqual(self.client.delete(wrong, params={'expected_revision': 1}).status_code, 404)
        self.assertEqual(self.client.post(other_base, json=self.payload).status_code, 409)
        self.assertEqual(self.client.get(self.base + '/' + str(uuid4())).status_code, 404)
        missing = '/api/projects/999999/knowledge-documents'
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.assertEqual(self.client.post(missing, json={**self.payload, 'document_id': str(uuid4())}).status_code, 404)

    def test_invalid_text_and_source_never_modify_saved_document(self):
        document = self.create()
        url = self.base + '/' + document['id']
        invalid = [{'title': '  '}, {'title': 'a' * 201}, {'content': '\n\t '}, {'content': 'a' * 100001},
                   {'content': 'bad\x00text'}, {'content': '\ud800'}, {'source_filename': '../secret.txt'},
                   {'source_filename': 'C:\\secret.txt'}, {'source_filename': 'bad\nname.md'}, {'extra': True}]
        for fields in invalid:
            self.assertEqual(self.client.put(url, content=json.dumps(self.update_payload(document, **fields)), headers={'Content-Type': 'application/json'}).status_code, 422)
        self.assertEqual(self.client.get(url).json(), document)

    def test_failed_chunk_write_rolls_back_document_and_old_chunks(self):
        document = self.create()
        url = self.base + '/' + document['id']
        client = TestClient(self.app, raise_server_exceptions=False)
        try:
            with patch('app.knowledge_documents.KnowledgeChunk', side_effect=RuntimeError('injected failure')):
                response = client.put(url, json=self.update_payload(document, content='本次应整体回滚'))
                self.assertEqual(response.status_code, 500)
        finally:
            client.close()
        self.assertEqual(self.client.get(url).json(), document)

    def test_project_delete_cascades_only_its_knowledge(self):
        self.create()
        other = self.client.post(f'/api/projects/{self.other}/knowledge-documents', json={
            **self.payload, 'document_id': str(uuid4())}).json()
        self.client.delete(f'/api/projects/{self.pid}')
        with self.app.state.session_factory() as session:
            self.assertEqual([item.id for item in session.scalars(select(KnowledgeDocument)).all()], [other['id']])
            self.assertEqual(len(session.scalars(select(KnowledgeChunk)).all()), other['chunk_count'])


if __name__ == '__main__':
    unittest.main()
