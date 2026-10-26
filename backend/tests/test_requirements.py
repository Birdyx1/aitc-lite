import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Requirement


class RequirementApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.project_a = self.client.post("/api/projects", json={"name": "项目 A"}).json()["id"]
        self.project_b = self.client.post("/api/projects", json={"name": "项目 B"}).json()["id"]
        self.base = f"/api/projects/{self.project_a}/requirements"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def create_requirement(self):
        response = self.client.post(self.base, json={
            "title": "  登录需求  ", "content": "\n  1. 手机号登录\n    - 校验验证码\n",
        })
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_crud_preserves_original_text_and_survives_restart(self):
        requirement = self.create_requirement()
        self.assertEqual(requirement["title"], "登录需求")
        self.assertEqual(requirement["content"], "\n  1. 手机号登录\n    - 校验验证码\n")
        self.assertEqual(requirement["project_id"], self.project_a)
        self.assertTrue(requirement["updated_at"].endswith("+00:00"))
        url = f"{self.base}/{requirement['id']}"
        listing = self.client.get(self.base).json()
        self.assertEqual(listing[0]["id"], requirement["id"])
        self.assertNotIn("content", listing[0])
        self.assertEqual(self.client.get(url).json(), requirement)
        updated = self.client.put(url, json={"title": "登录 v2", "content": "  新正文\n"})
        self.assertEqual(updated.status_code, 200)
        with TestClient(create_app(self.db_path)) as restarted:
            self.assertEqual(restarted.get(url).json()["content"], "  新正文\n")
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(self.base).json(), [])

    def test_cross_project_access_is_rejected(self):
        requirement = self.create_requirement()
        other_base = f"/api/projects/{self.project_b}/requirements"
        other_url = f"{other_base}/{requirement['id']}"
        self.assertEqual(self.client.get(other_base).json(), [])
        self.assertEqual(self.client.get(other_url).status_code, 404)
        self.assertEqual(self.client.put(other_url, json={"title": "覆盖", "content": "错误项目"}).status_code, 404)
        self.assertEqual(self.client.delete(other_url).status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/{requirement['id']}").json()["title"], "登录需求")

    def test_invalid_input_does_not_overwrite_content(self):
        requirement = self.create_requirement()
        url = f"{self.base}/{requirement['id']}"
        invalid_inputs = [
            {"title": "  ", "content": "正文"},
            {"title": "标题", "content": " \n\t"},
            {"title": "x" * 201, "content": "正文"},
            {"title": "标题", "content": "x" * 50001},
            {"title": "标题", "content": "正文", "project_id": self.project_b},
        ]
        for payload in invalid_inputs:
            with self.subTest(title=payload["title"][:20]):
                self.assertEqual(self.client.post(self.base, json=payload).status_code, 422)
                self.assertEqual(self.client.put(url, json=payload).status_code, 422)
        self.assertEqual(self.client.get(url).json(), requirement)
        self.assertEqual(len(self.client.get(self.base).json()), 1)

    def test_project_delete_cascades_only_its_requirements(self):
        requirement = self.create_requirement()
        other = self.client.post(f"/api/projects/{self.project_b}/requirements", json={
            "title": "B 的需求", "content": "保留内容",
        }).json()
        self.assertEqual(self.client.delete(f"/api/projects/{self.project_a}").status_code, 204)
        with self.app.state.session_factory() as session:
            self.assertIsNone(session.get(Requirement, requirement["id"]))
            self.assertIsNotNone(session.get(Requirement, other["id"]))
            self.assertEqual(len(session.scalars(select(Requirement)).all()), 1)
        self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_missing_project_or_requirement_returns_404(self):
        missing_project = "/api/projects/999999/requirements"
        self.assertEqual(self.client.get(missing_project).status_code, 404)
        self.assertEqual(self.client.post(missing_project, json={"title": "标题", "content": "正文"}).status_code, 404)
        missing = f"{self.base}/999999"
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.assertEqual(self.client.put(missing, json={"title": "标题", "content": "正文"}).status_code, 404)
        self.assertEqual(self.client.delete(missing).status_code, 404)


if __name__ == "__main__":
    unittest.main()
