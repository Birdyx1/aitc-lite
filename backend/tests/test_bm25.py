import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.knowledge.bm25 import PositiveBM25, tokenize
from app.main import create_app
from app.models import KnowledgeChunk


class TokenizerAndRankingTests(unittest.TestCase):
    def test_chinese_and_identifiers_are_normalized_consistently(self):
        tokens = tokenize('验证码过期 Ｅ１００１ X-Request-ID /api/login')
        self.assertIn('验证码', tokens)
        self.assertIn('过期', tokens)
        self.assertIn('e1001', tokens)
        self.assertIn('x-request-id', tokens)
        self.assertIn('api/login', tokens)
        self.assertEqual(tokenize('E1001'), tokenize('ｅ１００１'))
        self.assertNotEqual(tokenize('E1001'), tokenize('E10010'))
        self.assertEqual(tokenize('…😀！？'), [])

    def test_positive_idf_handles_single_and_half_corpus_terms(self):
        one = PositiveBM25([['e1001']])
        self.assertAlmostEqual(one.get_scores(['e1001'])[0], math.log(1 + 0.5 / 1.5))
        two = PositiveBM25([['e1001'], ['e2002']])
        scores = two.get_scores(['e1001'])
        self.assertGreater(scores[0], 0)
        self.assertEqual(scores[1], 0)
        self.assertGreater(PositiveBM25([['shared'], ['shared']]).get_scores(['shared'])[0], 0)

    def test_formula_term_frequency_saturation_and_length_normalization(self):
        corpus = [['token', 'token', 'token'], ['token', 'filler', 'filler', 'filler'], ['other']]
        index = PositiveBM25(corpus, k1=1.5, b=0.75)
        scores = index.get_scores(['token'])
        avg = sum(map(len, corpus)) / len(corpus)
        idf = math.log1p((3 - 2 + 0.5) / (2 + 0.5))
        expected = idf * (3 * 2.5) / (3 + 1.5 * (0.25 + 0.75 * 3 / avg))
        self.assertAlmostEqual(scores[0], expected)
        self.assertGreater(scores[0], scores[1])
        self.assertEqual(scores[2], 0)


class BM25ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bm25.sqlite3'
        self.app = create_app(self.path)
        self.client = TestClient(self.app).__enter__()
        self.pid = self.client.post('/api/projects', json={'name': '检索项目'}).json()['id']
        self.other = self.client.post('/api/projects', json={'name': '其他项目'}).json()['id']
        self.base = f'/api/projects/{self.pid}/knowledge-documents'
        self.url = f'/api/projects/{self.pid}/knowledge-search'

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def add(self, text, title='业务说明', project_id=None):
        response = self.client.post(f'/api/projects/{project_id or self.pid}/knowledge-documents', json={
            'document_id': str(uuid4()), 'title': title, 'content': text, 'source_filename': '说明.md'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def search(self, query='E1001', **extra):
        response = self.client.post(self.url, json={'query': query, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_chinese_code_and_source_metadata_match_only_body(self):
        document = self.add('验证码过期返回 E1001。', title='包含仅标题关键词 HEADERONLY')
        self.add('余额不足返回 E2002。')
        self.add('退款失败返回 E3003。')
        result = self.search('Ｅ１００１')
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['query_tokens'], ['e1001'])
        self.assertEqual(len(result['hits']), 1)
        hit = result['hits'][0]
        self.assertEqual(hit['document_id'], document['id'])
        self.assertEqual(hit['document_revision'], 1)
        self.assertEqual(hit['source_filename'], '说明.md')
        self.assertEqual(hit['chunk_id'], document['chunks'][0]['id'])
        self.assertEqual(hit['content'], document['content'][hit['start_offset']:hit['end_offset']])
        self.assertGreater(hit['score'], 0)
        self.assertEqual(hit['matched_tokens'], ['e1001'])
        self.assertEqual(self.search('验证码过期')['hits'][0]['document_id'], document['id'])
        self.assertEqual(self.search('HEADERONLY')['status'], 'no_matches')
        self.assertEqual(self.search('E10010')['hits'], [])

    def test_single_document_is_searchable_and_query_repetitions_do_not_inflate_score(self):
        self.add('登录失败 E1001')
        first = self.search()
        repeated = self.search('e1001 E1001 e1001')
        self.assertEqual(first['hits'], repeated['hits'])
        self.assertEqual(repeated['query_tokens'], ['e1001'])
        self.assertEqual(first['corpus_chunk_count'], 1)
        self.assertEqual(first['indexed_chunk_count'], 1)

    def test_top_k_positive_scores_deterministic_ties_and_no_false_hits(self):
        for _ in range(4):
            self.add('E1001 shared')
        self.add('other unrelated')
        result = self.search(top_k=2)
        self.assertEqual(result['total_matches'], 4)
        self.assertEqual(len(result['hits']), 2)
        self.assertEqual([h['rank'] for h in result['hits']], [1, 2])
        self.assertTrue(all(h['score'] > 0 and h['matched_tokens'] for h in result['hits']))
        self.assertEqual(result['hits'], self.search(top_k=2)['hits'])
        self.assertEqual(self.search('unknown')['status'], 'no_matches')

    def test_projects_are_isolated_and_foreign_corpus_does_not_change_scores(self):
        self.add('E1001 登录失败')
        before = self.search()
        self.add('E1001 外部项目机密', project_id=self.other)
        after = self.search()
        self.assertEqual(before['hits'], after['hits'])
        self.assertEqual(before['corpus_signature'], after['corpus_signature'])
        self.assertNotIn('外部项目机密', json.dumps(after, ensure_ascii=False))
        self.assertEqual(self.client.post('/api/projects/999999/knowledge-search', json={'query': 'E1001'}).status_code, 404)

    def test_update_delete_and_restart_do_not_return_stale_chunks(self):
        document = self.add('旧错误 E1001')
        original_signature = self.search()['corpus_signature']
        response = self.client.put(self.base + '/' + document['id'], json={
            'title': document['title'], 'content': '新错误 E9009', 'source_filename': document['source_filename'], 'expected_revision': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search()['hits'], [])
        latest = self.search('E9009')
        self.assertNotEqual(latest['corpus_signature'], original_signature)
        self.assertEqual(latest['hits'][0]['document_revision'], 2)
        self.assertNotEqual(latest['hits'][0]['chunk_id'], document['chunks'][0]['id'])
        with TestClient(create_app(self.path)) as restarted:
            result = restarted.post(self.url, json={'query': 'E9009'}).json()
            self.assertEqual(result['hits'], latest['hits'])
        self.client.delete(self.base + '/' + document['id'], params={'expected_revision': 2})
        self.assertEqual(self.search('E9009')['status'], 'empty_corpus')
        self.assertEqual(self.search('E9009')['hits'], [])

    def test_stale_revision_rows_are_not_indexed(self):
        self.add('E1001 当前内容')
        with self.app.state.session_factory() as session:
            chunk = session.scalars(select(KnowledgeChunk)).first()
            chunk.document_revision = 0
            session.commit()
        self.assertEqual(self.search()['corpus_chunk_count'], 0)

    def test_empty_corpus_no_terms_and_no_indexable_chunks_are_distinct(self):
        self.assertEqual(self.search()['status'], 'empty_corpus')
        self.assertEqual(self.search('😀…！？')['status'], 'no_terms')
        self.add('😀…！？')
        result = self.search()
        self.assertEqual(result['status'], 'no_indexable_chunks')
        self.assertEqual(result['corpus_chunk_count'], 1)
        self.assertEqual(result['indexed_chunk_count'], 0)

    def test_invalid_queries_do_not_echo_raw_input_or_mutate_knowledge(self):
        doc = self.add('E1001')
        invalid = [{'query': '  '}, {'query': 'a' * 201}, {'query': '\ud800'}, {'query': 'bad\x00'},
                   {'query': 'e1001', 'top_k': 0}, {'query': 'e1001', 'top_k': 21},
                   {'query': 'e1001', 'top_k': True}, {'query': 'e1001', 'top_k': '5'},
                   {'query': 'e1001', 'project_id': self.other}]
        for payload in invalid:
            response = self.client.post(self.url, content=json.dumps(payload), headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('input', response.text)
        query = ' '.join(str(n) for n in range(65))
        self.assertLessEqual(len(query), 200)
        self.assertEqual(self.client.post(self.url, json={'query': query}).status_code, 400)
        self.assertEqual(self.client.get(self.base + '/' + doc['id']).json(), doc)

    def test_corpus_change_during_scoring_returns_conflict(self):
        document = self.add('E1001')
        from app.knowledge.bm25 import rank_chunks as original
        def mutate(chunks, terms):
            self.client.delete(self.base + '/' + document['id'], params={'expected_revision': 1})
            return original(chunks, terms)
        with patch('app.knowledge_search.rank_chunks', side_effect=mutate):
            response = self.client.post(self.url, json={'query': 'E1001'})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(document['content'], response.text)

    def test_scoring_failure_is_service_error_not_empty_results(self):
        self.add('E1001')
        with patch('app.knowledge_search.rank_chunks', side_effect=RuntimeError('private internal text')):
            response = self.client.post(self.url, json={'query': 'E1001'})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private internal text', response.text)


if __name__ == '__main__':
    unittest.main()
