import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import CaseDraft, CaseGeneration, FeatureSheet, Requirement


class CaseGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.sqlite3"
        self.requests = []
        self.behavior = "success"
        self.after_call = None

        def handler(request):
            self.requests.append(request)
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            kinds = ["functional"] if data["strategy"] == "quick" else ["functional", "boundary", "exception"]
            cases = [{"title": data["feature"]["feature"] + "-" + kind, "priority": "P1", "case_type": kind,
                      "is_smoke": kind == "functional", "precondition": "前置条件",
                      "steps": ["执行操作"], "expected_result": "结果满足原文验收标准"} for kind in kinds]
            if self.behavior == "duplicate":
                cases[-1]["title"] = cases[0]["title"]
            if self.behavior == "empty-step":
                cases[0]["steps"] = ["  "]
            if self.behavior == "bad-coverage":
                cases = cases[:1]
            if self.behavior == "bad-quick":
                cases[0]["is_smoke"] = False
            if self.after_call:
                self.after_call()
            reply = "bad json" if self.behavior == "invalid-json" else json.dumps({"cases": cases}, ensure_ascii=False)
            if self.behavior == "fenced":
                reply = "```json\n" + reply + "\n```"
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})

        self.app = create_app(self.path, llm_transport=httpx.MockTransport(handler))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.project = self.client.post("/api/projects", json={"name": "项目 A"}).json()["id"]
        self.requirement_url = f"/api/projects/{self.project}/requirements"
        requirement = self.client.post(self.requirement_url, json={"title": "账号需求", "content": "登录和退出，遵循原文规则。"}).json()
        self.requirement_id = requirement["id"]
        self.feature_url = f"{self.requirement_url}/{self.requirement_id}/features"
        sheet = self.client.get(self.feature_url).json()
        self.sheet = self.client.put(self.feature_url, json={
            "expected_revision": 0, "input_hash": sheet["current_hash"], "confirmed": True,
            "items": [{"module": "账号", "feature": name, "description": name + "功能描述", "included": included}
                      for name, included in (("登录", True), ("退出", True), ("未选择", False))],
        }).json()
        self.base = f"{self.requirement_url}/{self.requirement_id}/case-generations"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def configure(self, mock=False):
        self.assertEqual(self.client.put("/api/settings/generation", json={
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "api_key": "fake-generation-key", "mock_mode": mock,
        }).status_code, 200)

    def payload(self, **overrides):
        return {"request_id": str(uuid4()), "feature_id": self.sheet["items"][0]["id"],
                "expected_revision": self.sheet["revision"], "input_hash": self.sheet["current_hash"],
                "strategy": "full", **overrides}

    def test_graph_generates_validates_persists_and_survives_restart(self):
        self.configure()
        self.behavior = "fenced"
        response = self.client.post(self.base, json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        generation = response.json()
        self.assertEqual(generation["graph_nodes"], ["prepare", "generate", "validate"])
        self.assertEqual(generation["case_count"], 3)
        self.assertEqual({case["case_type"] for case in generation["cases"]}, {"functional", "boundary", "exception"})
        self.assertFalse(generation["outdated"])
        self.assertEqual(generation["feature_snapshot"]["id"], self.sheet["items"][0]["id"])
        self.assertNotIn("fake-generation-key", response.text)
        request = json.loads(self.requests[-1].content)
        self.assertEqual(request["thinking"], {"type": "disabled"})
        self.assertEqual(request["max_tokens"], 4096)
        with TestClient(create_app(self.path)) as restarted:
            restored = restarted.get(self.base + "/" + generation["id"])
            self.assertEqual(restored.json(), generation)
            self.assertEqual(len(restarted.get(self.base).json()), 1)

    def test_quick_strategy_only_returns_smoke_functional_cases(self):
        self.configure()
        response = self.client.post(self.base, json=self.payload(strategy="quick"))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["case_count"], 1)
        self.assertTrue(all(case["is_smoke"] and case["case_type"] == "functional" for case in response.json()["cases"]))
        self.behavior = "bad-quick"
        self.assertEqual(self.client.post(self.base, json=self.payload(strategy="quick")).status_code, 502)

    def test_mock_uses_the_same_graph_without_network(self):
        self.configure(mock=True)
        response = self.client.post(self.base, json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["source"], "mock")
        self.assertEqual(response.json()["graph_nodes"], ["prepare", "generate", "validate"])
        self.assertTrue(all("Mock" in case["title"] for case in response.json()["cases"]))
        self.assertEqual(self.requests, [])

    def test_successful_request_replay_does_not_call_model_or_duplicate_drafts(self):
        self.configure()
        payload = self.payload()
        first = self.client.post(self.base, json=payload)
        second = self.client.post(self.base, json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.requests), 1)
        payload["strategy"] = "quick"
        self.assertEqual(self.client.post(self.base, json=payload).status_code, 409)
        self.assertEqual(len(self.client.get(self.base).json()), 1)
        with self.app.state.session_factory() as session:
            self.assertEqual(len(session.scalars(select(CaseDraft)).all()), 3)

    def test_missing_settings_unconfirmed_stale_and_unselected_are_rejected(self):
        self.assertEqual(self.client.post(self.base, json=self.payload()).status_code, 400)
        self.configure()
        excluded = self.sheet["items"][-1]["id"]
        self.assertEqual(self.client.post(self.base, json=self.payload(feature_id=excluded)).status_code, 409)
        self.assertEqual(self.client.post(self.base, json=self.payload(feature_id=str(uuid4()))).status_code, 409)
        self.assertEqual(self.client.post(self.base, json=self.payload(expected_revision=999)).status_code, 409)
        with self.app.state.session_factory() as session:
            session.get(FeatureSheet, self.requirement_id).confirmed = False
            session.commit()
        self.assertEqual(self.client.post(self.base, json=self.payload()).status_code, 409)
        with self.app.state.session_factory() as session:
            session.get(FeatureSheet, self.requirement_id).confirmed = True
            session.get(Requirement, self.requirement_id).content = "新需求原文"
            session.commit()
        self.assertEqual(self.client.post(self.base, json=self.payload()).status_code, 409)
        self.assertEqual(self.requests, [])

    def test_invalid_output_never_partially_saves_or_overwrites_history(self):
        self.configure()
        original = self.client.post(self.base, json=self.payload()).json()
        for behavior in ("invalid-json", "duplicate", "empty-step", "bad-coverage"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                self.assertEqual(self.client.post(self.base, json=self.payload()).status_code, 502)
        self.assertEqual(len(self.client.get(self.base).json()), 1)
        self.assertEqual(self.client.get(self.base + "/" + original["id"]).json(), original)

    def test_input_change_during_model_call_prevents_stale_result_persistence(self):
        self.configure()

        def edit_requirement():
            with self.app.state.session_factory() as session:
                session.get(Requirement, self.requirement_id).content = "模型等待期间改过原文"
                session.commit()

        self.after_call = edit_requirement
        self.assertEqual(self.client.post(self.base, json=self.payload()).status_code, 409)
        self.assertEqual(self.client.get(self.base).json(), [])
        with self.app.state.session_factory() as session:
            self.assertEqual(session.scalars(select(CaseDraft)).all(), [])

    def test_feature_revision_change_during_model_call_prevents_persistence(self):
        self.configure()

        def edit_features():
            with self.app.state.session_factory() as session:
                sheet = session.get(FeatureSheet, self.requirement_id)
                sheet.revision += 1
                sheet.confirmed = False
                session.commit()

        self.after_call = edit_features
        self.assertEqual(self.client.post(self.base, json=self.payload()).status_code, 409)
        self.assertEqual(self.client.get(self.base).json(), [])

    def test_feature_removal_preserves_historical_snapshots_and_marks_outdated(self):
        self.configure()
        generation = self.client.post(self.base, json=self.payload()).json()
        self.client.put(self.feature_url, json={"expected_revision": self.sheet["revision"],
            "input_hash": self.sheet["current_hash"], "items": [], "confirmed": False})
        historical = self.client.get(self.base + "/" + generation["id"]).json()
        self.assertTrue(historical["outdated"])
        self.assertEqual(historical["cases"], generation["cases"])
        self.assertEqual(historical["feature_snapshot"], generation["feature_snapshot"])
        self.assertEqual(self.client.post(self.base, json={**self.payload(), "request_id": generation["id"]}).status_code, 200)

    def test_cross_project_history_is_hidden_and_parent_delete_cascades(self):
        self.configure()
        generation = self.client.post(self.base, json=self.payload()).json()
        other_project = self.client.post("/api/projects", json={"name": "B"}).json()["id"]
        wrong = f"/api/projects/{other_project}/requirements/{self.requirement_id}/case-generations"
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.get(wrong + "/" + generation["id"]).status_code, 404)
        self.assertEqual(self.client.post(wrong, json=self.payload()).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/projects/{self.project}").status_code, 204)
        with self.app.state.session_factory() as session:
            self.assertEqual(session.scalars(select(CaseGeneration)).all(), [])
            self.assertEqual(session.scalars(select(CaseDraft)).all(), [])


if __name__ == "__main__":
    unittest.main()
