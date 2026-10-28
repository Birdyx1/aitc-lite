import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import test_generation_tasks as fixtures
from app.checkpoints import checkpoint_trace
from app.main import create_app
from app.models import GenerationTask, GenerationTaskControl, GraphCheckpoint, GraphCheckpointWrite


class TaskRecoveryTests(unittest.TestCase):
    setUp = fixtures.GenerationTaskTests.setUp
    tearDown = fixtures.GenerationTaskTests.tearDown
    make_requirement = fixtures.GenerationTaskTests.make_requirement
    submit = fixtures.GenerationTaskTests.submit
    wait_status = fixtures.GenerationTaskTests.wait_status

    def action(self, action, payload=None, task_id=None, url=None):
        return self.client.post((url or self.url) + '/' + (task_id or self.payload['request_id']) + '/' + action,
                                json={} if payload is None else payload)

    def paused_after_model(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        pause = self.action('pause')
        self.assertEqual(pause.status_code, 200, pause.text)
        self.assertTrue(pause.json()['pause_requested'])
        self.assertEqual(pause.json()['status'], 'running')
        self.release.set()
        paused = self.wait_status('paused')
        self.assertEqual(paused['checkpoint_nodes'], ['prepare', 'generate'])
        self.assertEqual(paused['stage'], 'validate')
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])
        return paused

    def restart(self):
        transport = self.app.state.llm_transport
        self.client.__exit__(None, None, None)
        self.app = create_app(self.path, llm_transport=transport)
        self.client = TestClient(self.app).__enter__()

    def test_pause_after_inflight_model_and_resume_reuses_result_once(self):
        paused = self.paused_after_model()
        self.assertTrue(paused['can_resume'])
        self.assertFalse(paused['requires_model_retry'])
        response = self.action('resume')
        self.assertEqual(response.status_code, 200, response.text)
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 1)
        result = self.client.get(self.rr + '/case-generations/' + self.payload['request_id']).json()
        self.assertEqual(result['case_count'], 3)
        self.assertEqual(self.action('resume').json()['status'], 'succeeded')
        self.assertEqual(len(self.client.get(self.rr + '/case-generations').json()), 1)
        self.assertEqual(self.action('pause').status_code, 409)

    def test_paused_task_survives_restart_and_keeps_model_output(self):
        self.paused_after_model()
        self.restart()
        self.assertTrue(self.wait_status('paused')['can_resume'])
        self.assertEqual(self.action('resume').status_code, 200)
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 1)

    def test_queued_pause_runs_no_model_until_resume(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        rr, payload = self.make_requirement()
        url = rr + '/generation-tasks'
        self.submit(payload, url)
        response = self.action('pause', task_id=payload['request_id'], url=url)
        self.assertEqual(response.json()['status'], 'paused')
        self.release.set()
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.action('resume', task_id=payload['request_id'], url=url).status_code, 200)
        self.wait_status('succeeded', payload['request_id'], url)
        self.assertEqual(self.calls, 2)

    def test_interrupted_model_requires_explicit_permission_to_repeat(self):
        self.release.clear()
        self.submit()
        self.assertTrue(self.entered.wait(2))
        self.restart()
        interrupted = self.wait_status('interrupted')
        self.assertTrue(interrupted['requires_model_retry'])
        self.assertTrue(interrupted['can_resume'])
        self.assertEqual(self.action('resume').status_code, 409)
        self.assertEqual(self.calls, 1)
        self.release.set()
        self.assertEqual(self.action('resume', {'retry_uncertain_model_call': True}).status_code, 200)
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 2)

    def test_interrupted_after_model_checkpoint_resumes_without_model(self):
        self.paused_after_model()
        with self.app.state.session_factory() as session:
            session.get(GenerationTask, self.payload['request_id']).status = 'running'
            session.commit()
        self.restart()
        task = self.wait_status('interrupted')
        self.assertFalse(task['requires_model_retry'])
        self.assertEqual(self.action('resume').status_code, 200)
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 1)

    def test_pending_node_writes_recover_without_repeating_model(self):
        self.paused_after_model()
        tid = self.payload['request_id']
        with self.app.state.session_factory() as session:
            rows = session.scalars(select(GraphCheckpoint).where(GraphCheckpoint.task_id == tid)).all()
            for row in rows:
                if 'generate' in row.checkpoint.get('channel_values', {}).get('trace', []):
                    session.execute(delete(GraphCheckpointWrite).where(GraphCheckpointWrite.task_id == tid,
                                    GraphCheckpointWrite.checkpoint_id == row.checkpoint_id))
                    session.delete(row)
            session.get(GenerationTaskControl, tid).model_call_pending = True
            session.get(GenerationTask, tid).status = 'running'
            session.commit()
            self.assertEqual(checkpoint_trace(session, tid), ['prepare', 'generate'])
        self.restart()
        self.assertFalse(self.wait_status('interrupted')['requires_model_retry'])
        self.assertEqual(self.action('resume').status_code, 200)
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 1)

    def test_resume_revalidates_input_configuration_and_workflow(self):
        self.paused_after_model()
        tid = self.payload['request_id']
        with self.app.state.session_factory() as session:
            control = session.get(GenerationTaskControl, tid)
            version = control.workflow_version
            control.workflow_version = 'old-version'
            session.commit()
        self.assertEqual(self.action('resume').status_code, 409)
        self.assertIn('流程版本', self.wait_status('paused')['resume_block_reason'])
        with self.app.state.session_factory() as session:
            session.get(GenerationTaskControl, tid).workflow_version = version
            session.commit()
        self.client.put('/api/settings/generation', json={'base_url': 'https://model.example/v1', 'model': 'changed'})
        self.assertEqual(self.action('resume').status_code, 409)
        self.client.put('/api/settings/generation', json={
            'base_url': 'https://model.example/v1', 'model': 'test-model', 'timeout_seconds': 5})
        self.client.put(self.rr, json={'title': 'changed', 'content': '新需求'})
        self.assertEqual(self.action('resume').status_code, 409)
        self.assertEqual(self.calls, 1)

    def test_concurrent_resume_is_idempotent_and_active_other_task_blocks(self):
        self.paused_after_model()
        self.release.clear()
        other = {**self.payload, 'request_id': str(uuid4())}
        self.submit(other)
        self.assertEqual(self.action('resume').status_code, 409)
        self.release.set()
        self.wait_status('succeeded', other['request_id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.action('resume'), range(2)))
        self.assertTrue(all(r.status_code == 200 for r in responses))
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.client.get(self.rr + '/case-generations').json()), 2)

    def test_pause_after_validation_resumes_only_database_save(self):
        original = self.app.state.checkpointer.put
        tid = self.payload['request_id']
        def pause_on_validation(config, checkpoint, metadata, versions):
            result = original(config, checkpoint, metadata, versions)
            if checkpoint.get('channel_values', {}).get('trace') == ['prepare', 'generate', 'validate']:
                with self.app.state.session_factory() as session:
                    session.get(GenerationTaskControl, tid).pause_requested = True
                    session.commit()
            return result
        self.app.state.checkpointer.put = pause_on_validation
        self.submit()
        task = self.wait_status('paused')
        self.assertEqual(task['checkpoint_nodes'], ['prepare', 'generate', 'validate'])
        self.assertEqual(task['progress'], 90)
        self.assertEqual(self.client.get(self.rr + '/case-generations').json(), [])
        self.assertEqual(self.action('resume').status_code, 200)
        self.wait_status('succeeded')
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.client.get(self.rr + '/case-generations').json()), 1)

    def test_model_echoed_key_is_redacted_before_checkpoint_storage(self):
        import asyncio
        import httpx
        async def handler(request):
            self.calls += 1
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            cases = [{'title': kind, 'priority': 'P1', 'case_type': kind,
                      'is_smoke': kind == 'functional', 'precondition': '', 'steps': ['执行'],
                      'expected_result': 'private-task-key'} for kind in ('functional', 'boundary', 'exception')]
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'cases': cases})}}]})
        self.app.state.llm_transport = httpx.MockTransport(handler)
        self.paused_after_model()
        with self.app.state.session_factory() as session:
            checkpoints = session.scalars(select(GraphCheckpoint)).all()
            writes = session.scalars(select(GraphCheckpointWrite)).all()
            text = json.dumps([row.checkpoint for row in checkpoints] + [row.value for row in writes])
            self.assertNotIn('private-task-key', text)
            self.assertIn('已隐藏', text.encode().decode('unicode_escape'))

    def test_checkpoint_scope_cascade_and_no_credentials(self):
        self.paused_after_model()
        tid = self.payload['request_id']
        other = self.client.post('/api/projects', json={'name': '其他项目'}).json()['id']
        url = self.url.replace(f'/projects/{self.pid}/', f'/projects/{other}/')
        for action in ('pause', 'resume'):
            self.assertEqual(self.action(action, url=url).status_code, 404)
        with self.app.state.session_factory() as session:
            rows = session.scalars(select(GraphCheckpoint)).all()
            self.assertTrue(rows)
            text = json.dumps([r.checkpoint for r in rows])
            self.assertNotIn('private-task-key', text)
        self.client.delete(self.rr)
        with self.app.state.session_factory() as session:
            self.assertEqual(session.scalars(select(GraphCheckpoint)).all(), [])
            self.assertEqual(session.scalars(select(GraphCheckpointWrite)).all(), [])
            self.assertIsNone(session.get(GenerationTaskControl, tid))

    def test_legacy_interrupted_task_cannot_claim_checkpoint_recovery(self):
        self.paused_after_model()
        with self.app.state.session_factory() as session:
            session.delete(session.get(GenerationTaskControl, self.payload['request_id']))
            session.get(GenerationTask, self.payload['request_id']).status = 'interrupted'
            session.commit()
        task = self.wait_status('interrupted')
        self.assertFalse(task['can_resume'])
        self.assertIn('历史任务', task['resume_block_reason'])
        self.assertEqual(self.action('resume').status_code, 409)


if __name__ == '__main__':
    unittest.main()
