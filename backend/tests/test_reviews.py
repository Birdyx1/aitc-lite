import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import CaseReview


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.sqlite3"
        self.app = create_app(self.path)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.project_id = self.client.post("/api/projects", json={"name": "评审项目"}).json()["id"]
        root = f"/api/projects/{self.project_id}"
        self.requirement_id = self.client.post(root + "/requirements", json={"title": "登录", "content": "账号登录需求"}).json()["id"]
        self.requirement_url = f"{root}/requirements/{self.requirement_id}"
        sheet = self.client.get(self.requirement_url + "/features").json()
        sheet = self.client.put(self.requirement_url + "/features", json={
            "expected_revision": 0, "input_hash": sheet["current_hash"], "confirmed": True,
            "items": [{"module": "账号", "feature": "登录", "description": "账号登录"}],
        }).json()
        self.client.put("/api/settings/generation", json={"mock_mode": True})
        response = self.client.post(self.requirement_url + "/case-generations", json={
            "request_id": str(uuid4()), "feature_id": sheet["items"][0]["id"],
            "expected_revision": sheet["revision"], "input_hash": sheet["current_hash"], "strategy": "full",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.generation = response.json()
        self.generation_url = self.requirement_url + "/case-generations/" + self.generation["id"]
        self.base = self.generation_url + "/reviews"
        self.draft_id = self.generation["cases"][0]["id"]
        self.url = self.base + "/" + self.draft_id
        self.library = root + "/testcases"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def first(self):
        return self.client.get(self.base).json()["items"][0]

    def test_initial_pending_reviews_and_empty_library(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 3)
        self.assertTrue(all(item["status"] == "pending" and item["revision"] == 0 for item in response.json()["items"]))
        self.assertEqual(self.first()["original"], self.first()["current"])
        self.assertEqual(self.client.get(self.library).json(), [])

    def test_edit_preserves_original_then_accepts_once_and_survives_restart(self):
        original = self.first()["original"]
        fields = {**original, "title": "人工修订标题", "steps": ["准备输入", "执行操作"]}
        edited = self.client.put(self.url, json={"action": "edit", "expected_revision": 0, "fields": fields})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["status"], "pending")
        self.assertTrue(edited.json()["was_edited"])
        self.assertEqual(edited.json()["original"], original)
        self.assertEqual(self.client.get(self.generation_url).json()["cases"], self.generation["cases"])
        accepted = self.client.put(self.url, json={"action": "accept", "expected_revision": 1})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "accepted")
        repeated = self.client.put(self.url, json={"action": "accept", "expected_revision": 1})
        self.assertEqual(repeated.json(), accepted.json())
        library = self.client.get(self.library).json()
        self.assertEqual(len(library), 1)
        self.assertEqual(library[0]["title"], "人工修订标题")
        self.assertEqual(library[0]["source"], "mock")
        self.assertTrue(library[0]["was_edited"])
        with TestClient(create_app(self.path)) as restarted:
            self.assertEqual(restarted.get(self.library).json(), library)
        self.assertEqual(self.client.put(self.url, json={"action": "edit", "expected_revision": 2, "fields": fields}).status_code, 409)
        self.assertEqual(self.client.put(self.url, json={"action": "reject", "expected_revision": 2, "rejection_reason": "改判"}).status_code, 409)

    def test_rejection_requires_reason_and_locks_conclusion(self):
        self.assertEqual(self.client.put(self.url, json={"action": "reject", "expected_revision": 0, "rejection_reason": "  "}).status_code, 422)
        payload = {"action": "reject", "expected_revision": 0, "rejection_reason": "缺乏需求依据"}
        rejected = self.client.put(self.url, json=payload)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["status"], "rejected")
        self.assertEqual(self.client.put(self.url, json=payload).json(), rejected.json())
        self.assertEqual(self.client.put(self.url, json={"action": "accept", "expected_revision": 1}).status_code, 409)
        self.assertEqual(self.client.get(self.library).json(), [])

    def test_revision_conflict_does_not_overwrite_manual_changes(self):
        original = self.first()["current"]
        fields = {**original, "title": "先保存的修改"}
        self.assertEqual(self.client.put(self.url, json={"action": "edit", "expected_revision": 0, "fields": fields}).status_code, 200)
        fields["title"] = "过期页面修改"
        self.assertEqual(self.client.put(self.url, json={"action": "edit", "expected_revision": 0, "fields": fields}).status_code, 409)
        self.assertEqual(self.client.put(self.url, json={"action": "accept", "expected_revision": 0}).status_code, 409)
        self.assertEqual(self.first()["current"]["title"], "先保存的修改")

    def test_outdated_draft_cannot_be_accepted_but_can_be_rejected(self):
        self.client.put(self.requirement_url, json={"title": "新需求", "content": "新规则"})
        self.assertEqual(self.client.put(self.url, json={"action": "accept", "expected_revision": 0}).status_code, 409)
        self.assertEqual(self.client.put(self.url, json={"action": "reject", "expected_revision": 0, "rejection_reason": "需求已变更"}).status_code, 200)
        self.assertEqual(self.client.get(self.library).json(), [])

    def test_previously_accepted_case_remains_with_outdated_warning(self):
        self.client.put(self.url, json={"action": "accept", "expected_revision": 0})
        self.client.put(self.requirement_url, json={"title": "新需求", "content": "新规则"})
        library = self.client.get(self.library).json()
        self.assertEqual(len(library), 1)
        self.assertTrue(library[0]["outdated"])
        self.assertEqual(library[0]["requirement_title"], "登录")

    def test_invalid_edit_cannot_change_saved_case(self):
        original = self.first()
        for fields in ({**original["current"], "title": "  "}, {**original["current"], "steps": [""]},
                       {**original["current"], "priority": "P9"}):
            self.assertEqual(self.client.put(self.url, json={"action": "edit", "expected_revision": 0, "fields": fields}).status_code, 422)
        self.assertEqual(self.first(), original)
        self.assertEqual(self.client.put(self.url, json={"action": "accept", "expected_revision": 0, "fields": original["current"]}).status_code, 422)

    def test_project_and_generation_scope_and_delete_cascade(self):
        self.client.put(self.url, json={"action": "accept", "expected_revision": 0})
        other_project = self.client.post("/api/projects", json={"name": "其他项目"}).json()["id"]
        wrong = self.base.replace(f"/projects/{self.project_id}/", f"/projects/{other_project}/")
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.put(wrong + "/" + self.draft_id, json={"action": "accept", "expected_revision": 0}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/projects/{other_project}/testcases").json(), [])
        self.assertEqual(self.client.put(self.base + "/" + str(uuid4()), json={"action": "accept", "expected_revision": 0}).status_code, 404)
        self.assertEqual(self.client.delete(self.requirement_url).status_code, 204)
        self.assertEqual(self.client.get(self.library).json(), [])
        with self.app.state.session_factory() as session:
            self.assertEqual(session.scalars(select(CaseReview)).all(), [])


if __name__ == "__main__":
    unittest.main()
