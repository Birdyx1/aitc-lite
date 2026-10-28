import asyncio
import json
import math
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.embedding_settings import PROBE_TEXTS
from app.embeddings import EmbeddingConfig, embed_texts, normalize_vector, parse_vectors
from app.main import create_app
from app.models import EmbeddingSettings

URL = '/api/settings/embedding'
KEY = 'fake-embedding-test-key'


class VectorValidationTests(unittest.TestCase):
    def test_index_order_and_stable_normalization(self):
        vectors = parse_vectors({'data': [{'index': 1, 'embedding': [0, 4]},
                                         {'index': 0, 'embedding': [3, 0]}]}, 2, None)
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        for vector in ([1e308, 1e308], [1e-308, 1e-308]):
            normalized = normalize_vector(vector)
            self.assertAlmostEqual(sum(number * number for number in normalized), 1)
            self.assertTrue(all(math.isfinite(number) for number in normalized))

    def test_malformed_numbers_dimensions_and_indexes_are_rejected(self):
        invalid = [None, {}, {'data': []}, {'data': [{'index': 0, 'embedding': [1]}]},
                   {'data': [{'index': 0, 'embedding': [1]}, {'index': 0, 'embedding': [1]}]},
                   {'data': [{'index': True, 'embedding': [1]}, {'index': 1, 'embedding': [1]}]}]
        for vector in ([], [0, 0], [True, 1], ['1', 1], [float('nan')], [float('inf')], [1] * 32769, [10 ** 1000]):
            invalid.append({'data': [{'index': 0, 'embedding': vector}, {'index': 1, 'embedding': [1]}]})
        invalid.append({'data': [{'index': 0, 'embedding': [1, 2]}, {'index': 1, 'embedding': [1]}]})
        for body in invalid:
            with self.assertRaises(HTTPException) as error:
                parse_vectors(body, 2, None)
            self.assertEqual(error.exception.status_code, 502)

    def test_mock_is_deterministic_explicit_and_has_no_semantic_claim(self):
        config = EmbeddingConfig('', '', '', True, 5, 8)
        first = asyncio.run(embed_texts(config, ['文本A', '文本B']))
        self.assertEqual(first, asyncio.run(embed_texts(config, ['文本A', '文本B'])))
        self.assertNotEqual(first[0], first[1])
        self.assertTrue(all(len(vector) == 8 for vector in first))
        with self.assertRaises(ValueError):
            asyncio.run(embed_texts(config, 'not-a-list'))


class EmbeddingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'embedding.sqlite3'
        self.requests = []
        self.behavior = 'success'
        def handler(request):
            self.requests.append(request)
            if self.behavior == 'timeout':
                raise httpx.ReadTimeout(KEY, request=request)
            if self.behavior == 'network':
                raise httpx.ConnectError(KEY, request=request)
            if isinstance(self.behavior, int):
                return httpx.Response(self.behavior, text=KEY, headers={'Location': 'https://other.invalid'})
            if self.behavior == 'invalid-json':
                return httpx.Response(200, text=KEY)
            if self.behavior == 'oversize':
                return httpx.Response(200, content=b'x' * (5 * 1024 * 1024 + 1))
            if self.behavior == 'invalid-vectors':
                return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [KEY]}, {'index': 1, 'embedding': [1]}]})
            return httpx.Response(200, json={'data': [
                {'index': 1, 'embedding': [0, 3, 4]}, {'index': 0, 'embedding': [3, 4, 0]}], 'private': KEY})
        self.app = create_app(self.path, embedding_transport=httpx.MockTransport(handler))
        self.client = TestClient(self.app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def payload(self, **overrides):
        return {'base_url': 'https://embedding.example/v1', 'model': 'embedding-test', 'api_key': KEY,
                'timeout_seconds': 5, 'mock_mode': False, 'expected_dimensions': None, **overrides}

    def save(self, **overrides):
        response = self.client.put(URL, json=self.payload(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_initial_configuration_is_independent_and_never_reuses_generation_key(self):
        self.client.put('/api/settings/generation', json={
            'base_url': 'https://chat.example/v1', 'model': 'chat', 'api_key': 'separate-generation-key'})
        before = self.client.get('/api/settings/generation').json()
        initial = self.client.get(URL).json()
        self.assertFalse(initial['configured'])
        self.assertFalse(initial['mock_mode'])
        self.assertFalse(initial['has_api_key'])
        self.assertEqual(self.client.post(URL + '/test').status_code, 400)
        self.save()
        self.assertEqual(self.client.get('/api/settings/generation').json(), before)
        self.assertEqual(self.requests, [])

    def test_key_lifecycle_and_restart_do_not_echo_credentials(self):
        data = self.save()
        self.assertTrue(data['has_api_key'])
        self.assertNotIn(KEY, json.dumps(data))
        self.assertNotIn('api_key', data)
        self.save(api_key='', model='updated')
        with self.app.state.session_factory() as session:
            self.assertEqual(session.get(EmbeddingSettings, 1).api_key, KEY)
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(URL).json()['model'], 'updated')
            self.assertNotIn(KEY, restarted.get(URL).text)
        self.save(api_key='replacement')
        with self.app.state.session_factory() as session:
            self.assertEqual(session.get(EmbeddingSettings, 1).api_key, 'replacement')
        self.assertFalse(self.save(api_key='', clear_api_key=True)['has_api_key'])

    def test_endpoint_change_requires_replace_or_clear(self):
        self.save()
        bad = self.payload(api_key='', base_url='https://changed.example/v1')
        self.assertEqual(self.client.put(URL, json=bad).status_code, 400)
        self.assertEqual(self.client.get(URL).json()['base_url'], 'https://embedding.example/v1')
        bad['clear_api_key'] = True
        self.assertEqual(self.client.put(URL, json=bad).status_code, 200)

    def test_explicit_mock_probe_never_calls_provider(self):
        response = self.client.put(URL, json={'mock_mode': True, 'expected_dimensions': 16})
        self.assertEqual(response.status_code, 200)
        result = self.client.post(URL + '/test').json()
        self.assertEqual(result['mode'], 'mock')
        self.assertEqual(result['dimensions'], 16)
        self.assertEqual(result['vector_count'], 2)
        self.assertIn('不具备语义', result['message'])
        self.assertEqual(self.requests, [])

    def test_real_probe_uses_exact_fixed_batch_and_separate_endpoint(self):
        self.save(base_url='https://embedding.example/v1/', expected_dimensions=3)
        response = self.client.post(URL + '/test')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['mode'], 'real')
        self.assertEqual(response.json()['dimensions'], 3)
        self.assertEqual(response.json()['vector_count'], 2)
        self.assertNotIn(KEY, response.text)
        self.assertNotIn('embedding', response.json())
        request = self.requests[0]
        self.assertEqual(str(request.url), 'https://embedding.example/v1/embeddings')
        self.assertEqual(request.headers['authorization'], 'Bearer ' + KEY)
        self.assertEqual(json.loads(request.content), {'model': 'embedding-test', 'input': PROBE_TEXTS, 'encoding_format': 'float'})

    def test_optional_dimension_is_checked_without_sending_dimensions_to_service(self):
        self.save(expected_dimensions=4)
        response = self.client.post(URL + '/test')
        self.assertEqual(response.status_code, 502)
        self.assertIn('维度不一致', response.text)
        self.assertNotIn('dimensions', json.loads(self.requests[0].content))
        self.assertEqual(self.client.get(URL).json()['expected_dimensions'], 4)

    def test_local_endpoint_can_omit_authorization(self):
        self.save(base_url='http://127.0.0.1:11434/v1', api_key='')
        self.assertEqual(self.client.post(URL + '/test').status_code, 200)
        self.assertNotIn('authorization', self.requests[0].headers)

    def test_errors_are_sanitized_without_redirect_retry_or_mock_fallback(self):
        self.save()
        for behavior in (401, 403, 429, 500, 302, 'timeout', 'network', 'invalid-json', 'oversize', 'invalid-vectors'):
            self.behavior = behavior
            before = len(self.requests)
            response = self.client.post(URL + '/test')
            self.assertEqual(response.status_code, 504 if behavior == 'timeout' else 502)
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn(KEY, response.text)
            self.assertFalse(self.client.get(URL).json()['mock_mode'])

    def test_invalid_settings_redact_input_and_preserve_saved_configuration(self):
        before = self.save()
        invalid = [{'base_url': 'file:///tmp'}, {'base_url': 'https://host/v1?'}, {'base_url': 'https://host/v1#'}, {'base_url': 'https://u:p@host/v1'},
                   {'base_url': 'https://host/v1?key=' + KEY}, {'base_url': 'https://host/v1/embeddings'},
                   {'base_url': 'https://host/v1/chat/completions'}, {'base_url': 'http://host:99999/v1'},
                   {'model': ' '}, {'model': '\ud800'}, {'api_key': KEY + '\x00'}, {'api_key': '中文key'},
                   {'expected_dimensions': 0}, {'expected_dimensions': 32769}, {'expected_dimensions': True},
                   {'expected_dimensions': '3'}, {'timeout_seconds': 61}, {'clear_api_key': True}]
        for fields in invalid:
            response = self.client.put(URL, content=json.dumps(self.payload(**fields)), headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(KEY, response.text)
            self.assertNotIn('input', response.text)
        self.assertEqual(self.client.get(URL).json(), before)


if __name__ == '__main__':
    unittest.main()
