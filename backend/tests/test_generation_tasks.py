import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import CaseGeneration, GenerationTask


class GenerationTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tasks.sqlite3'
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.calls = 0
        self.behavior = 'success'

        async def handler(request):
            self.calls += 1
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            if self.behavior == 'error':
                return httpx.Response(401, text='private-task-key must never leak')
            data = json.loads(json.loads(request.content)['messages'][1]['content'])
            kinds = ['functional'] if data['strategy'] == 'quick' else ['functional', 'boundary', 'exception']
            cases = [{'title': kind, 'priority': 'P1', 'case_type': kind, 'is_smoke': kind == 'functional',
                      'precondition': '', 'steps': ['执行操作'], 'expected_result': '符合验收标准'} for kind in kinds]
            reply = 'invalid' if self.behavior == 'invalid' else json.dumps({'cases': cases})
            return httpx.Response(200, json={'choices': [{'message': {'content': reply}, 'finish_reason': 'stop'}]})

        self.app = create_app(self.path, llm_transport=httpx.MockTransport(handler))
        self.client = TestClient(self.app).__enter__()
        self.pid = self.client.post('/api/projects', json={'name': '任务项目'}).json()['id']
        self.root = f'/api/projects/{self.pid}'
        self.client.put('/api/settings/generation', json={
            'base_url': 'https://model.example/v1', 'model': 'test-model', 'api_key': 'private-task-key', 'timeout_seconds': 5})
        self.rr, self.payload = self.make_requirement()
        self.url = self.rr + '/generation-tasks'

    def tearDown(self):
        self.release.set()
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def make_requirement(self):
        rid = self.client.post(self.root + '/requirements', json={'title': '登录', 'content': '账号登录要求'}).json()['id']
        rr = self.root + f'/requirements/{rid}'
        sheet = self.client.get(rr + '/features').json()
        sheet = self.client.put(rr + '/features', json={
            'expected_revision': 0, 'input_hash': sheet['current_hash'], 'confirmed': True,
            'items': [{'module': '账号', 'feature': '登录', 'description': '账号登录'}]}).json()
        return rr, {'request_id': str(uuid4()), 'feature_id': sheet['items'][0]['id'],
                    'expected_revision': sheet['revision'], 'input_hash': sheet['current_hash'], 'strategy': 'full'}

    def submit(self, payload=None, url=None):
        response = self.client.post(url or self.url, json=payload or self.payload)
        self.assertIn(response.status_code, (200, 202), response.text)
        return response

    def wait_status(self, status, task_id=None, url=None):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            response = self.client.get((url or self.url) + '/' + (task_id or self.payload['request_id']))
            self.assertEqual(response.status_code, 200)
            task = response.json()
            if task['status'] == status:
                return task
            time.sleep(0.02)
        self.fail(f'task did not reach {status}: {task}')

    def test_submission_is_fast_and_running_progress_survives_page_reads(self):
        self.release.clear()
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'queued')
        self.assertTrue(self.entered.wait(2))
        running = self.wait_status('running')
        self.assertEqual(running['stage'], 'generate')
        self.assertEqual(running['completed_nodes'], ['prepare'])
        self.assertEqual(running['progress'], 25)
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])
        self.assertEqual(self.client.get(self.url).json()[0]['id'], running['id'])
        self.release.set()
        succeeded = self.wait_status('succeeded')
        self.assertEqual(succeeded['progress'], 100)
        self.assertEqual(succeeded['completed_nodes'], ['prepare', 'generate', 'validate'])
        generation = self.client.get(self.rr + '/case-generations/' + running['id']).json()
        self.assertEqual(generation['case_count'], 3)
        self.assertEqual(self.calls, 1)
        self.assertNotIn('private-task-key', json.dumps(succeeded))

    def test_idempotent_submission_and_same_requirement_active_guard(self):
        self.release.clear()
        self.submit()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.client.post(self.url, json={**self.payload, 'request_id': str(uuid4())}).status_code, 409)
        self.assertEqual(self.client.post(self.url, json={**self.payload, 'strategy': 'quick'}).status_code, 409)
        self.assertEqual(self.client.post(self.rr + '/case-generations', json={**self.payload, 'request_id': str(uuid4())}).status_code, 409)
        self.release.set()
        self.wait_status('succeeded')
        self.assertEqual(self.submit().json()['status'], 'succeeded')
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.client.get(self.url).json()), 1)
        self.assertEqual(len(self.client.get(self.rr + '/case-generations').json()), 1)

    def test_failed_task_is_persisted_sanitized_and_not_retried(self):
        self.behavior = 'error'
        self.submit()
        failed = self.wait_status('failed')
        self.assertIn('拒绝访问', failed['error_message'])
        self.assertNotIn('private-task-key', json.dumps(failed))
        self.assertEqual(self.submit().json()['status'], 'failed')
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])
        self.behavior = 'success'
        payload = {**self.payload, 'request_id': str(uuid4())}
        self.submit(payload)
        self.wait_status('succeeded', payload['request_id'])
        self.assertEqual(self.calls, 2)

    def test_invalid_model_result_does_not_save_partial_drafts(self):
        self.behavior = 'invalid'
        self.submit()
        self.assertIn('校验失败', self.wait_status('failed')['error_message'])
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])

    def test_queue_configuration_change_fails_without_second_model_call(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        rr, payload = self.make_requirement()
        queued_url = rr + '/generation-tasks'
        self.submit(payload, queued_url)
        self.assertEqual(self.client.get(queued_url).json()[0]['status'], 'queued')
        self.client.put('/api/settings/generation', json={
            'base_url': 'https://model.example/v1', 'model': 'new-model', 'timeout_seconds': 5})
        self.release.set()
        self.wait_status('succeeded')
        failed = self.wait_status('failed', payload['request_id'], queued_url)
        self.assertIn('模型配置已变化', failed['error_message'])
        self.assertEqual(self.calls, 1)

    def test_requirement_changes_during_generation_fail_without_result(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        self.client.put(self.rr, json={'title': '已变更', 'content': '全新要求'})
        self.release.set()
        self.assertIn('需求已变化', self.wait_status('failed')['error_message'])
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])

    def test_scope_missing_and_cascade_deletion_do_not_stop_worker(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        other = self.client.post('/api/projects', json={'name': '其他项目'}).json()['id']
        wrong = self.url.replace(f'/projects/{self.pid}/', f'/projects/{other}/')
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.get(wrong + '/' + self.payload['request_id']).status_code, 404)
        self.assertEqual(self.client.get(self.url + '/' + str(uuid4())).status_code, 404)
        self.client.delete(self.rr)
        self.release.set()
        rr, payload = self.make_requirement()
        self.submit(payload, rr + '/generation-tasks')
        self.wait_status('succeeded', payload['request_id'], rr + '/generation-tasks')
        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(GenerationTask, self.payload['request_id']))
            self.assertIsNone(session.get(CaseGeneration, self.payload['request_id']))

    def test_shutdown_interrupts_running_task_and_restart_never_calls_model(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.path)).__enter__()
        task = self.wait_status('interrupted')
        self.assertEqual(task['stage'], 'generate')
        self.assertEqual(task['progress'], 25)
        self.assertIn('中断', task['error_message'])
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])

    def test_concurrent_submission_has_only_one_active_task(self):
        from concurrent.futures import ThreadPoolExecutor
        self.release.clear()
        payloads = [{**self.payload, 'request_id': str(uuid4())} for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda payload: self.client.post(self.url, json=payload), payloads))
        self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
        self.assertEqual(len(self.client.get(self.url).json()), 1)

    def test_mock_quick_task_finishes_without_external_model(self):
        saved = self.client.put('/api/settings/generation', json={'base_url': 'https://model.example/v1', 'model': 'test-model', 'mock_mode': True})
        self.assertEqual(saved.status_code, 200, saved.text)
        payload = {**self.payload, 'strategy': 'quick'}
        self.submit(payload)
        self.wait_status('succeeded')
        result = self.client.get(self.rr + '/case-generations/' + payload['request_id']).json()
        self.assertEqual(result['case_count'], 1)
        self.assertEqual(result['source'], 'mock')
        self.assertEqual(self.calls, 0)

    def test_existing_legacy_result_is_linked_without_regeneration(self):
        response = self.client.post(self.rr + '/case-generations', json=self.payload)
        self.assertEqual(response.status_code, 201)
        saved = self.client.put('/api/settings/generation', json={
            'base_url': 'https://model.example/v1', 'model': 'test-model', 'mock_mode': True})
        self.assertEqual(saved.status_code, 200)
        task = self.submit().json()
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['source'], 'real')
        self.assertEqual(task['model'], 'test-model')
        self.assertEqual(self.calls, 1)

    def test_unconfirmed_and_missing_configuration_never_create_tasks(self):
        self.client.put(self.rr, json={'title': '更改后', 'content': '新的需求'})
        self.assertEqual(self.client.post(self.url, json=self.payload).status_code, 409)
        self.assertEqual(self.client.get(self.url).json(), [])
        rr, payload = self.make_requirement()
        from app.models import GenerationSettings
        with self.app.state.session_factory() as session:
            session.delete(session.get(GenerationSettings, 1))
            session.commit()
        self.assertEqual(self.client.post(rr + '/generation-tasks', json=payload).status_code, 400)
        self.assertEqual(self.client.get(rr + '/generation-tasks').json(), [])

    def test_startup_reconciles_committed_generation_and_interrupts_orphans(self):
        self.submit()
        self.wait_status('succeeded')
        self.client.__exit__(None, None, None)
        with self.app.state.session_factory() as session:
            task = session.get(GenerationTask, self.payload['request_id'])
            task.status, task.progress = 'running', 90
            orphan_id = str(uuid4())
            session.add(GenerationTask(id=orphan_id, requirement_id=task.requirement_id,
                request_fingerprint=task.request_fingerprint, payload=task.payload,
                settings_signature=task.settings_signature, feature_name=task.feature_name,
                strategy=task.strategy, source=task.source, model=task.model, status='queued'))
            session.commit()
        self.client = TestClient(create_app(self.path)).__enter__()
        self.assertEqual(self.wait_status('succeeded')['progress'], 100)
        self.assertEqual(self.wait_status('interrupted', orphan_id)['progress'], 0)
        self.assertEqual(self.calls, 1)


if __name__ == '__main__':
    unittest.main()
