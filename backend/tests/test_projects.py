"""使用临时数据库验证 API 合同和跨应用重启的持久化，不接触开发数据。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


class ProjectApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.client = TestClient(create_app(self.db_path))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def test_crud_and_persistence_after_restart(self):
        self.assertEqual(self.client.get("/api/projects").json(), [])
        created = self.client.post("/api/projects", json={"name": "  商城测试  ", "description": "登录和订单"})
        self.assertEqual(created.status_code, 201)
        project = created.json()
        self.assertEqual(project["name"], "商城测试")
        self.assertTrue(project["created_at"].endswith("+00:00"))
        project_id = project["id"]
        url = f"/api/projects/{project_id}"
        self.assertEqual(self.client.get(url).json(), project)
        updated = self.client.put(url, json={"name": "订单测试", "description": "退款流程"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["description"], "退款流程")
        # 独立应用实例连接同一文件，证明数据不是保存在进程内存中。
        with TestClient(create_app(self.db_path)) as restarted:
            self.assertEqual(restarted.get(url).json()["name"], "订单测试")
            self.assertEqual(len(restarted.get("/api/projects").json()), 1)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get("/api/projects").json(), [])

    def test_invalid_input_cannot_create_or_overwrite_project(self):
        for payload in ({"name": "  "}, {"name": "a" * 101}, {"name": "正常", "description": "a" * 2001}, {"name": "正常", "unexpected": True}):
            with self.subTest(payload=str(payload)[:50]):
                self.assertEqual(self.client.post("/api/projects", json=payload).status_code, 422)
        project = self.client.post("/api/projects", json={"name": "原始名称"}).json()
        url = f"/api/projects/{project['id']}"
        self.assertEqual(self.client.put(url, json={"name": ""}).status_code, 422)
        self.assertEqual(self.client.get(url).json()["name"], "原始名称")

    def test_missing_project_returns_404(self):
        url = "/api/projects/999999"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.put(url, json={"name": "不存在"}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)


if __name__ == "__main__":
    unittest.main()

