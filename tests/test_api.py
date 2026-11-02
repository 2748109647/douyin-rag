from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock

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

    def test_knowledge_chat_history_and_local_request_boundaries(self):
        state = self.client.get("/api/knowledge").json()
        self.assertEqual(state["total"], 0)
        self.assertEqual(self.client.post("/api/chat", json={"question": "合成问题"}).status_code, 403)
        self.assertEqual(self.client.post("/api/knowledge/index").status_code, 403)
        self.assertEqual(self.client.post("/api/chat", headers=self.headers, json={"question": "合成问题"}).json()["code"], "snapshot_required")
        self.client.post("/api/demo", headers=self.headers)
        response = self.client.post("/api/chat", headers=self.headers, json={"question": "番茄牛腩怎么做？"})
        self.assertEqual(response.status_code, 200)
        conversation = response.json()
        self.assertIn("合成示例", conversation["messages"][-1]["content"])
        self.assertEqual(self.client.get("/api/chat/" + conversation["id"]).json(), conversation)
        self.assertEqual(self.client.post("/api/knowledge/index", headers=self.headers).json()["code"], "demo_mode")
        self.owner.data["snapshot"].update(demo=False, account_marker="other-account")
        self.assertEqual(self.client.get("/api/chat/" + conversation["id"]).json()["code"], "session_not_found")

    def test_chat_validation_and_snapshot_change_do_not_persist_partial_messages(self):
        self.client.post("/api/demo", headers=self.headers)
        for question in (" ", "x" * 2001):
            result = self.client.post("/api/chat", headers=self.headers, json={"question": question})
            self.assertIn(result.status_code, (400, 422))
        self.assertEqual(self.client.get("/api/knowledge").json()["sessions"], [])
        from douyin_rag.collections import CollectionError
        self.owner.knowledge.ask = AsyncMock(side_effect=CollectionError("正在回答", "busy"))
        self.assertEqual(self.client.post("/api/chat", headers=self.headers, json={"question": "合成问题"}).status_code, 409)

    def test_older_settings_clients_preserve_selected_transcription_model(self):
        self.client.put("/api/settings", headers=self.headers, json={"asr_model": "base"})
        result = self.client.put("/api/settings", headers=self.headers, json={"model": "mock-model"})
        self.assertEqual(result.json()["asr_model"], "base")
        result = self.client.put("/api/settings", headers=self.headers, json={"asr_model": "unsupported"})
        self.assertEqual(result.status_code, 422)

    def test_qr_login_auto_completes_and_keeps_browser_session(self):
        browser = AsyncMock()
        browser.login_state.side_effect = [
            {"status": "waiting", "message": "等待扫码", "qr_image": "data:image/png;base64,c3ludGhldGlj"},
            {"status": "authenticated", "message": "登录成功"},
        ]
        self.owner.writer_factory = lambda: browser
        result = self.client.post("/api/browser/open", headers=self.headers)
        self.assertEqual(result.json()["status"], "waiting")
        self.assertEqual(result.headers["cache-control"], "no-store")
        browser.open.assert_awaited_once_with(headless=True)
        self.assertEqual(self.client.post("/api/scan", headers=self.headers).status_code, 409)
        result = self.client.post("/api/browser/status", headers=self.headers)
        self.assertEqual(result.json()["status"], "authenticated")
        browser.save_login.assert_awaited_once()
        browser.close.assert_not_awaited()
        self.assertEqual(self.client.post("/api/browser/status", headers=self.headers).json(), result.json())
        self.assertNotIn("qr_image", result.json())

    def test_qr_refresh_cancel_verification_and_guards(self):
        browser = AsyncMock()
        browser.login_state.return_value = {"status": "expired", "message": "过期"}
        self.owner.writer_factory = lambda: browser
        for route in ("open", "status", "refresh", "visible", "cancel", "finish"):
            self.assertEqual(self.client.post("/api/browser/" + route).status_code, 403)
        self.client.post("/api/browser/open", headers=self.headers)
        result = self.client.post("/api/browser/finish", headers=self.headers)
        self.assertEqual(result.status_code, 400)
        browser.save_login.assert_not_awaited()
        self.assertEqual(self.client.post("/api/browser/refresh", headers=self.headers).json()["status"], "expired")
        browser.page.reload.assert_awaited_once()
        self.assertEqual(self.client.post("/api/browser/visible", headers=self.headers).json()["status"], "verifying")
        self.assertEqual(browser.open.call_args.kwargs, {"headless": False})
        self.assertEqual(self.client.post("/api/browser/cancel", headers=self.headers).json()["status"], "idle")
        browser.close.assert_awaited_once()
        self.assertEqual(self.client.post("/api/browser/refresh", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/api/browser/status", headers=self.headers).json()["status"], "idle")
