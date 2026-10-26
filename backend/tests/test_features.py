import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Feature, FeatureSheet


class FeatureApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.sqlite3"
        self.requests = []
        self.output = {"features": [{
            "module": "账号", "feature": "验证码登录", "description": "输入手机号与验证码完成登录",
            "acceptance_criteria": "成功后进入首页", "constraints": "验证码有效期待澄清", "priority": "P0",
        }]}
        self.fenced = False
        self.finish_reason = "stop"

        def handler(request):
            self.requests.append(request)
            reply = self.output if isinstance(self.output, str) else json.dumps(self.output, ensure_ascii=False)
            if self.fenced:
                reply = "```json\n" + reply + "\n```"
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": self.finish_reason}]})

        self.app = create_app(self.path, llm_transport=httpx.MockTransport(handler))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.project = self.client.post("/api/projects", json={"name": "A"}).json()["id"]
        self.other_project = self.client.post("/api/projects", json={"name": "B"}).json()["id"]
        self.requirement = self.client.post(f"/api/projects/{self.project}/requirements", json={
            "title": "登录需求", "content": "使用手机号和验证码登录，成功后进入首页。",
        }).json()["id"]
        self.other_requirement = self.client.post(f"/api/projects/{self.other_project}/requirements", json={
            "title": "其他需求", "content": "其他业务",
        }).json()["id"]
        self.base = f"/api/projects/{self.project}/requirements/{self.requirement}/features"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def configure(self, mock=False):
        response = self.client.put("/api/settings/generation", json={
            "mock_mode": mock, "base_url": "https://model.example.test/v1", "model": "test-model",
            "api_key": "fake-test-key",
        })
        self.assertEqual(response.status_code, 200)

    def payload_from(self, preview, confirmed=False):
        return {"expected_revision": preview["base_revision"], "input_hash": preview["input_hash"],
                "items": preview["items"], "source": preview["source"], "model": preview["model"],
                "confirmed": confirmed}

    def test_model_preview_is_validated_and_does_not_save_until_requested(self):
        self.configure()
        self.fenced = True
        preview = self.client.post(self.base + "/analyze")
        self.assertEqual(preview.status_code, 200)
        data = preview.json()
        self.assertEqual(data["source"], "real")
        self.assertEqual(data["items"][0]["priority"], "P0")
        self.assertEqual(self.client.get(self.base).json()["revision"], 0)
        self.assertEqual(self.client.get(self.base).json()["items"], [])
        sent = json.loads(self.requests[-1].content)
        self.assertEqual(sent["max_tokens"], 4096)
        self.assertIn("登录需求", sent["messages"][1]["content"])
        saved = self.client.put(self.base, json=self.payload_from(data))
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["revision"], 1)
        self.assertFalse(saved.json()["confirmed"])
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(self.base).json()["items"][0]["id"], data["items"][0]["id"])

    def test_mock_is_explicit_and_missing_configuration_cannot_generate(self):
        self.assertEqual(self.client.post(self.base + "/analyze").status_code, 400)
        self.configure(mock=True)
        preview = self.client.post(self.base + "/analyze")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["source"], "mock")
        self.assertIn("Mock", preview.json()["items"][0]["description"])
        self.assertEqual(self.requests, [])

    def test_edit_add_remove_select_confirm_and_invalidate_on_edit(self):
        self.configure()
        payload = self.payload_from(self.client.post(self.base + "/analyze").json())
        original_id = payload["items"][0]["id"]
        payload["items"].append({**payload["items"][0], "id": str(uuid4()), "feature": "退出登录", "included": False})
        saved = self.client.put(self.base, json=payload).json()
        payload["expected_revision"] = saved["revision"]
        payload["confirmed"] = True
        confirmed = self.client.put(self.base, json=payload)
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.json()["confirmed"])
        payload["expected_revision"] = confirmed.json()["revision"]
        payload["confirmed"] = False
        payload["items"] = payload["items"][:1]
        payload["items"][0]["description"] = "人工编辑后的功能描述"
        updated = self.client.put(self.base, json=payload)
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()["confirmed"])
        self.assertEqual(len(updated.json()["items"]), 1)
        self.assertEqual(updated.json()["items"][0]["id"], original_id)
        payload["expected_revision"] = updated.json()["revision"]
        payload["items"][0]["included"] = False
        payload["confirmed"] = True
        self.assertEqual(self.client.put(self.base, json=payload).status_code, 422)

    def test_requirement_changes_mark_sheet_stale_and_reject_old_preview(self):
        self.configure()
        payload = self.payload_from(self.client.post(self.base + "/analyze").json(), confirmed=True)
        saved = self.client.put(self.base, json=payload).json()
        self.client.put(f"/api/projects/{self.project}/requirements/{self.requirement}", json={"title": "新需求", "content": "新增密码登录"})
        sheet = self.client.get(self.base).json()
        self.assertTrue(sheet["stale"])
        self.assertFalse(sheet["confirmed"])
        self.assertEqual(sheet["items"], saved["items"])
        payload["expected_revision"] = saved["revision"]
        self.assertEqual(self.client.put(self.base, json=payload).status_code, 409)
        payload["input_hash"] = sheet["current_hash"]
        payload["confirmed"] = False
        rebased = self.client.put(self.base, json=payload)
        self.assertEqual(rebased.status_code, 200)
        self.assertFalse(rebased.json()["stale"])

    def test_revision_conflict_does_not_overwrite_saved_changes(self):
        self.configure()
        payload = self.payload_from(self.client.post(self.base + "/analyze").json())
        self.assertEqual(self.client.put(self.base, json=payload).status_code, 200)
        payload["items"][0]["feature"] = "过期页面的名称"
        self.assertEqual(self.client.put(self.base, json=payload).status_code, 409)
        self.assertEqual(self.client.get(self.base).json()["items"][0]["feature"], "验证码登录")

    def test_invalid_model_response_or_truncation_keeps_existing_features(self):
        self.configure()
        preview = self.client.post(self.base + "/analyze").json()
        original = self.client.put(self.base, json=self.payload_from(preview)).json()
        for output in ("这不是 JSON", {"features": []}, {"features": [{"module": "账号"}]},
                       {"features": [{**self.output["features"][0], "priority": "P9"}]},
                       {"features": self.output["features"] * 51}):
            with self.subTest(output=str(output)[:30]):
                self.output = output
                self.assertEqual(self.client.post(self.base + "/analyze").status_code, 502)
                self.assertEqual(self.client.get(self.base).json(), original)
        self.output = {"features": []}
        self.finish_reason = "length"
        response = self.client.post(self.base + "/analyze")
        self.assertEqual(response.status_code, 502)
        self.assertIn("长度上限", response.json()["detail"])

    def test_cross_project_access_and_feature_id_collision_are_rejected(self):
        self.configure()
        preview = self.client.post(self.base + "/analyze").json()
        self.client.put(self.base, json=self.payload_from(preview))
        wrong = f"/api/projects/{self.other_project}/requirements/{self.requirement}/features"
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.post(wrong + "/analyze").status_code, 404)
        self.assertEqual(self.client.put(wrong, json=self.payload_from(preview)).status_code, 404)
        other = f"/api/projects/{self.other_project}/requirements/{self.other_requirement}/features"
        other_sheet = self.client.get(other).json()
        payload = self.payload_from(preview)
        payload["input_hash"] = other_sheet["current_hash"]
        self.assertEqual(self.client.put(other, json=payload).status_code, 409)

    def test_requirement_delete_cascades_and_empty_manual_sheet_can_be_saved(self):
        sheet = self.client.get(self.base).json()
        payload = {"expected_revision": 0, "input_hash": sheet["current_hash"], "items": [], "source": "manual"}
        self.assertEqual(self.client.put(self.base, json=payload).status_code, 200)
        payload["expected_revision"] = 1
        payload["items"] = [{"module": "账号", "feature": "手工功能", "description": "描述"}]
        self.assertEqual(self.client.put(self.base, json=payload).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/projects/{self.project}/requirements/{self.requirement}").status_code, 204)
        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(FeatureSheet, self.requirement))
            self.assertEqual(session.scalars(select(Feature)).all(), [])


if __name__ == "__main__":
    unittest.main()
