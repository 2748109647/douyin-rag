from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient

from douyin_rag.app import create_app
from douyin_rag.organizer import Organizer


class ApiTest(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.owner = Organizer(self.temp.name)
        self.context = TestClient(create_app(self.owner), base_url="http://localhost:8765")
        self.client = self.context.__enter__()
        self.headers = {"X-Shicang-Request": "1", "Origin": "http://localhost:8765"}
    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_web_assets_health_and_no_mcp(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertIn("拾藏", self.client.get("/").text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/mcp").status_code, 404)

    def test_local_request_guards(self):
        self.assertEqual(self.client.post("/api/demo").status_code, 403)
        self.assertEqual(self.client.post("/api/demo", headers={**self.headers, "Origin": "http://evil.test"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "evil.test"}).status_code, 400)

    def test_demo_edit_and_execute_blocked(self):
        response = self.client.post("/api/demo", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        plan = response.json()["plan"]
        result = self.client.post("/api/execute", headers=self.headers, json={"plan_id": plan["id"], "revision": plan["revision"], "confirmed": True})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()["code"], "demo_mode")
        response = self.client.patch("/api/plan", headers=self.headers, json={"revision": plan["revision"], "aweme_id": next(iter(plan["assignments"])), "category": plan["categories"][-1]})
        self.assertEqual(response.status_code, 200)

    def test_keys_never_appear_in_settings_or_validation_export(self):
        key = "synthetic-secret-never-echo"
        result = self.client.put("/api/settings", headers=self.headers, json={"api_key": key, "base_url": "http://model.test/v1", "model": "mock-model"})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(key, result.text)
        self.assertNotIn(key, self.client.get("/api/state").text)
        self.assertNotIn(key, self.client.get("/api/export").text)
        self.assertNotIn(key, self.client.put("/api/settings", headers=self.headers, json={"api_key": key, "max_folders": "invalid"}).text)

    def test_confirmation_and_stale_revision(self):
        self.client.post("/api/demo", headers=self.headers)
        self.owner.data["snapshot"]["demo"] = False
        plan = self.owner.data["plan"]
        base = {"plan_id": plan["id"], "revision": plan["revision"]}
        self.assertEqual(self.client.post("/api/execute", headers=self.headers, json=base).json()["code"], "confirmation_required")
        self.assertEqual(self.client.post("/api/execute", headers=self.headers, json={**base, "confirmed": True, "revision": 0}).status_code, 409)
