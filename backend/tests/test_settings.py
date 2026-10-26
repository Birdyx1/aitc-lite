import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import GenerationSettings

URL = "/api/settings/generation"
FAKE_KEY = "test-only-not-a-real-secret"


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.requests = []
        self.behavior = "success"

        def handler(request):
            self.requests.append(request)
            if self.behavior == "timeout":
                raise httpx.ReadTimeout(FAKE_KEY, request=request)
            if self.behavior == "network":
                raise httpx.ConnectError(FAKE_KEY, request=request)
            if isinstance(self.behavior, int):
                return httpx.Response(self.behavior, text=FAKE_KEY)
            if self.behavior == "invalid-json":
                return httpx.Response(200, text=FAKE_KEY)
            if self.behavior == "invalid-structure":
                return httpx.Response(200, json={"choices": []})
            if self.behavior == "empty":
                return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})
            reply = FAKE_KEY if self.behavior == "echo-key" else "AITC_OK"
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

        self.app = create_app(self.db_path, llm_transport=httpx.MockTransport(handler))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def real_payload(self, **overrides):
        return {
            "base_url": "https://model.example.test/v1", "model": "test-model",
            "api_key": FAKE_KEY, "mock_mode": False, "timeout_seconds": 5,
            **overrides,
        }

    def test_empty_settings_do_not_enable_mock_or_make_requests(self):
        data = self.client.get(URL).json()
        self.assertFalse(data["configured"])
        self.assertFalse(data["mock_mode"])
        self.assertFalse(data["has_api_key"])
        self.assertEqual(self.client.post(URL + "/test").status_code, 400)
        self.assertEqual(self.requests, [])

    def test_key_is_hidden_preserved_replaced_cleared_and_persistent(self):
        response = self.client.put(URL, json=self.real_payload())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(FAKE_KEY, response.text)
        self.assertNotIn("api_key", response.json())
        self.assertTrue(response.json()["has_api_key"])
        self.client.put(URL, json=self.real_payload(api_key="", model="second-model"))
        with self.app.state.session_factory() as session:
            self.assertEqual(session.get(GenerationSettings, 1).api_key, FAKE_KEY)
        with TestClient(create_app(self.db_path)) as restarted:
            data = restarted.get(URL).json()
            self.assertTrue(data["has_api_key"])
            self.assertEqual(data["model"], "second-model")
            self.assertNotIn(FAKE_KEY, restarted.get(URL).text)
        self.client.put(URL, json=self.real_payload(api_key="replacement-key"))
        with self.app.state.session_factory() as session:
            self.assertEqual(session.get(GenerationSettings, 1).api_key, "replacement-key")
        cleared = self.client.put(URL, json=self.real_payload(api_key="", clear_api_key=True))
        self.assertFalse(cleared.json()["has_api_key"])
        with self.app.state.session_factory() as session:
            self.assertEqual(session.get(GenerationSettings, 1).api_key, "")

    def test_changed_endpoint_cannot_silently_reuse_old_key(self):
        self.client.put(URL, json=self.real_payload())
        changed = self.real_payload(base_url="https://other.example.test/v1", api_key="")
        self.assertEqual(self.client.put(URL, json=changed).status_code, 400)
        self.assertEqual(self.client.get(URL).json()["base_url"], "https://model.example.test/v1")
        changed["clear_api_key"] = True
        self.assertEqual(self.client.put(URL, json=changed).status_code, 200)
        self.assertFalse(self.client.get(URL).json()["has_api_key"])

    def test_mock_requires_explicit_save_and_never_calls_provider(self):
        self.assertEqual(self.client.put(URL, json={"mock_mode": True}).status_code, 200)
        result = self.client.post(URL + "/test")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["mode"], "mock")
        self.assertIn("未连接", result.json()["message"])
        self.assertEqual(self.requests, [])

    def test_real_call_uses_saved_model_and_fixed_message(self):
        self.client.put(URL, json=self.real_payload(base_url="https://model.example.test/v1/"))
        result = self.client.post(URL + "/test")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["mode"], "real")
        self.assertEqual(result.json()["reply"], "AITC_OK")
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://model.example.test/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer " + FAKE_KEY)
        import json
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "这是连接验证，请只回复 AITC_OK。"}])
        self.assertFalse(payload["stream"])
        self.assertNotIn("thinking", payload)

    def test_official_deepseek_probe_disables_thinking(self):
        import json
        for base_url in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            with self.subTest(base_url=base_url):
                self.client.put(URL, json=self.real_payload(base_url=base_url, model="deepseek-flash"))
                self.assertEqual(self.client.post(URL + "/test").status_code, 200)
                payload = json.loads(self.requests[-1].content)
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertEqual(payload["model"], "deepseek-flash")

    def test_local_service_can_be_used_without_authorization_header(self):
        self.client.put(URL, json=self.real_payload(base_url="http://127.0.0.1:11434/v1", api_key=""))
        self.assertEqual(self.client.post(URL + "/test").status_code, 200)
        self.assertNotIn("authorization", self.requests[0].headers)

    def test_provider_errors_are_sanitized_and_do_not_fall_back_to_mock(self):
        self.client.put(URL, json=self.real_payload())
        for behavior in (401, 403, 429, 500, 302, "timeout", "network", "invalid-json", "invalid-structure", "empty"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                response = self.client.post(URL + "/test")
                self.assertEqual(response.status_code, 504 if behavior == "timeout" else 502)
                self.assertNotIn(FAKE_KEY, response.text)
                self.assertNotIn('"mode":"mock"', response.text)
                self.assertFalse(self.client.get(URL).json()["mock_mode"])
        self.behavior = "echo-key"
        response = self.client.post(URL + "/test")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(FAKE_KEY, response.text)

    def test_validation_errors_do_not_echo_key_or_overwrite_saved_settings(self):
        self.client.put(URL, json=self.real_payload())
        for overrides in (
            {"base_url": "file:///tmp/model"},
            {"base_url": "https://user:password@model.example.test/v1"},
            {"base_url": "https://model.example.test/v1?api_key=" + FAKE_KEY},
            {"base_url": "https://model.example.test/v1/chat/completions"},
            {"base_url": "http://model.example.test:99999/v1"},
            {"model": " "}, {"timeout_seconds": 0}, {"timeout_seconds": 61},
            {"api_key": FAKE_KEY + "\ninvalid"},
            {"clear_api_key": True},
        ):
            with self.subTest(fields=list(overrides)):
                response = self.client.put(URL, json=self.real_payload(**overrides))
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(FAKE_KEY, response.text)
        data = self.client.get(URL).json()
        self.assertEqual(data["model"], "test-model")
        self.assertTrue(data["has_api_key"])


if __name__ == "__main__":
    unittest.main()
