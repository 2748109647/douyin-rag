import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, call, patch

import httpx

from douyin_rag.collections import CollectionClient, CollectionError
from douyin_rag.organizer import Organizer, demo_data


def raw(item_id, folder=False):
    return {"aweme_id": item_id, "desc": "合成标题", "aweme_type": 68 if folder else 0}


class FakeClient:
    def __init__(self, cookies):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def get_collected_videos(self, cursor=0, count=50):
        return {"aweme_list": [] if cursor == 0 else [raw("900000000000000001")], "cursor": 5, "has_more": cursor == 0, "invalid_item_count": 3}
    async def get_collection_folders(self, cursor=0, count=50):
        return {"collects_list": [{"collects_id": "9", "collects_name": "原夹"}], "cursor": 1, "has_more": False}
    async def get_collection_videos(self, collects_id, cursor=0, count=50):
        return {"aweme_list": [raw("900000000000000001"), raw("900000000000000002", True)], "cursor": 1, "has_more": False}


class OrganizerTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.owner = Organizer(self.temp.name, client_factory=FakeClient)
        self.owner.cookies = lambda: "sessionid=synthetic"
        self.owner.settings.update(base_url="http://model.test/v1", model="test-model", api_key="synthetic-key")
        self.owner.job = {"message": "test", "processed": 0, "total": None}
    def tearDown(self):
        self.temp.cleanup()

    async def test_scan_deduplicates_folder_members_and_preserves_raw_invalid_pages(self):
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            await self.owner.scan()
        snapshot = self.owner.data["snapshot"]
        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(snapshot["items"]), 2)
        self.assertEqual(snapshot["items"][0]["original_folders"], ["9"])
        self.assertEqual(snapshot["items"][1]["kind"], "note")
        self.assertEqual([p["invalid_item_count"] for p in snapshot["invalid_pages"]], [3, 3])
        self.assertNotIn("invalid_total", snapshot)

    async def test_stalled_cursor_keeps_last_complete_snapshot(self):
        previous = {"id": "last-good"}
        self.owner.data["snapshot"] = previous
        class Stalled(FakeClient):
            async def get_collected_videos(self, **kwargs):
                return {"aweme_list": [], "cursor": 0, "has_more": True}
        self.owner.client_factory = Stalled
        with self.assertRaises(CollectionError):
            await self.owner.scan()
        self.assertEqual(self.owner.data["snapshot"], previous)

    async def test_scan_retries_timed_out_page_without_restarting_pagination(self):
        fetch = AsyncMock(side_effect=[
            {"aweme_list": [raw("1")], "cursor": 5, "has_more": True},
            httpx.ReadTimeout("synthetic timeout"),
            {"aweme_list": [raw("2")], "cursor": 5, "has_more": False},
        ])
        with patch.object(FakeClient, "get_collected_videos", fetch), patch(
            "douyin_rag.organizer.asyncio.sleep", new=AsyncMock()
        ):
            self.owner.start("scan", self.owner.scan)
            await self.owner.task
        self.assertEqual(fetch.await_args_list, [call(cursor=0, count=20), call(cursor=5, count=20), call(cursor=5, count=20)])
        self.assertEqual(self.owner.job["status"], "completed")
        self.assertTrue({"1", "2"} <= {item["aweme_id"] for item in self.owner.data["snapshot"]["items"]})

    async def test_scan_reports_network_failure_after_one_retry_and_preserves_snapshot(self):
        previous = {"id": "last-good", "complete": True}
        self.owner.data["snapshot"] = previous
        fetch = AsyncMock(side_effect=httpx.ConnectError("synthetic private request details"))
        with patch.object(FakeClient, "get_collection_folders", fetch), patch(
            "douyin_rag.organizer.asyncio.sleep", new=AsyncMock()
        ):
            self.owner.start("scan", self.owner.scan)
            await self.owner.task
        self.assertEqual(fetch.await_args_list, [call(cursor=0, count=50)] * 2)
        self.assertEqual(self.owner.job["status"], "failed")
        self.assertEqual(self.owner.job["error_code"], "network_error")
        self.assertEqual(self.owner.job["message"], "读取收藏夹失败：连接失败。请检查连接后重新扫描。")
        self.assertEqual(self.owner.data["snapshot"], previous)

    async def test_scan_honors_pause_before_retry(self):
        fetch = AsyncMock(side_effect=httpx.ReadTimeout("synthetic timeout"))

        async def pause_during_wait(_interval):
            self.owner.pause()

        with patch.object(FakeClient, "get_collected_videos", fetch), patch(
            "douyin_rag.organizer.asyncio.sleep", new=pause_during_wait
        ):
            self.owner.start("scan", self.owner.scan)
            await self.owner.task
        fetch.assert_awaited_once_with(cursor=0, count=20)
        self.assertEqual(self.owner.job["status"], "paused")
        self.assertIsNone(self.owner.data["snapshot"])

    async def test_rejected_collection_page_reports_stage_and_preserves_snapshot(self):
        previous = {"id": "last-good", "complete": True}
        self.owner.data["snapshot"] = previous

        async def reject(*args, **kwargs):
            return CollectionClient._validate_collection_response({"status_code": 5}, "")

        for method, stage in (
            ("get_collected_videos", "全部收藏"),
            ("get_collection_folders", "收藏夹"),
            ("get_collection_videos", "收藏夹 原夹"),
        ):
            fetch = AsyncMock(side_effect=reject)
            with self.subTest(stage=stage), patch.object(FakeClient, method, fetch), patch(
                "douyin_rag.organizer.asyncio.sleep", new=AsyncMock()
            ):
                self.owner.start("scan", self.owner.scan)
                await self.owner.task
                self.assertEqual(self.owner.job["status"], "failed")
                self.assertEqual(self.owner.job["error_code"], "collection_error")
                self.assertIn(f"读取{stage}失败", self.owner.job["message"])
                self.assertIn("status_code=5", self.owner.job["message"])
                self.assertEqual(self.owner.data["snapshot"], previous)
                fetch.assert_awaited_once()

    async def test_local_login_scan_uses_http_client_without_opening_browser(self):
        del self.owner.cookies  # Use the real selection logic with synthetic files.
        browser = AsyncMock()
        self.owner.writer_factory = lambda: browser
        app_cookie = self.owner.root / "login.cookies"
        app_cookie.write_text("sessionid=app-session", encoding="utf-8")
        default_dir = self.owner.root / "douyinmcp"
        default_dir.mkdir()
        (default_dir / "cookies.txt").write_text("sessionid=default-session", encoding="utf-8")
        external_cookie = self.owner.root / "external.cookies"
        external_cookie.write_text("sessionid=external-session", encoding="utf-8")
        clients = []
        def factory(cookies):
            client = FakeClient(cookies)
            clients.append((cookies, client))
            return client
        self.owner.client_factory = factory
        environment = {"DOUYIN_COOKIE": "", "DOUYIN_COOKIE_PATH": "", "XDG_CONFIG_HOME": str(self.owner.root)}
        with patch.dict("os.environ", environment), patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            await self.owner.scan()
            self.assertFalse(hasattr(clients[-1][1], "browser_page"))
            self.assertEqual(clients[-1][0], "sessionid=app-session")
            self.owner.settings["cookie_path"] = str(external_cookie)
            await self.owner.scan()
            self.assertFalse(hasattr(clients[-1][1], "browser_page"))
            self.assertEqual(clients[-1][0], "sessionid=external-session")
            self.owner.settings["cookie_path"] = str(self.owner.root / "missing.cookies")
            with self.assertRaises(CollectionError):
                self.owner.cookies()
            self.owner.settings["cookie_path"] = ""
            with patch.dict("os.environ", {"DOUYIN_COOKIE_PATH": str(external_cookie)}):
                self.assertEqual(self.owner.cookies(), "sessionid=external-session")
            with patch.dict("os.environ", {"DOUYIN_COOKIE": "sessionid=env-session"}):
                self.assertEqual(self.owner.cookies(), "sessionid=env-session")
            app_cookie.write_text("ttwid=invalid-session", encoding="utf-8")
            self.assertEqual(self.owner.cookies(), "sessionid=default-session")
        browser.open.assert_not_awaited()
        browser.cookies.assert_not_awaited()

    async def test_model_protocol_fences_and_error_redaction(self):
        def reply(request):
            self.assertEqual(str(request.url), "http://model.test/v1/chat/completions")
            return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"categories":["合成分类"]}\n```'}}]})
        result = await self.owner.model_json([], transport=httpx.MockTransport(reply))
        self.assertEqual(result["categories"], ["合成分类"])
        with self.assertRaises(CollectionError) as caught:
            await self.owner.model_json([], transport=httpx.MockTransport(lambda r: httpx.Response(401, text="synthetic-key-private")))
        self.assertNotIn("private", str(caught.exception))

    async def test_subscription_model_uses_saved_chatgpt_login_and_fixed_model_without_api_key(self):
        import os
        import subprocess
        calls = []

        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1:3] == ["login", "status"]:
                return subprocess.CompletedProcess(argv, 0, stdout=b"Logged in using ChatGPT", stderr=b"")
            self.assertIn("--ephemeral", argv)
            self.assertIn("read-only", argv)
            self.assertEqual(argv[argv.index("--model")+1], "gpt-6-luna")
            self.assertIn('model_reasoning_effort="xhigh"', argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("DOUYIN_COOKIE", kwargs["env"])
            self.assertEqual(Path(argv[argv.index("--image")+1]).read_bytes(), b"synthetic-jpeg")
            Path(argv[argv.index("--output-last-message")+1]).write_text('{"verified":true}', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        with patch("douyin_rag.organizer.shutil.which", return_value="codex.exe"), \
                patch("douyin_rag.organizer.subprocess.run", side_effect=command), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-api-key", "DOUYIN_COOKIE": "synthetic-cookie"}):
            result = await self.owner.model_json([{"role": "user", "content": "合成内容"}], subscription=True, images=[b"synthetic-jpeg"])
        self.assertEqual(result, {"verified": True})
        self.assertEqual(len(calls), 2)

    async def test_subscription_rejects_api_auth_and_redacts_failed_cli_output(self):
        import subprocess
        with patch("douyin_rag.organizer.shutil.which", return_value="codex.exe"), \
                patch("douyin_rag.organizer.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=b"Logged in using API key", stderr=b"")):
            with self.assertRaises(CollectionError) as caught:
                await self.owner.model_json([], subscription=True)
            self.assertEqual(caught.exception.code, "subscription_login_required")
        responses = [subprocess.CompletedProcess([], 0, stdout=b"Logged in using ChatGPT", stderr=b""),
                     subprocess.CompletedProcess([], 1, stdout=b"synthetic-private-content", stderr=b"synthetic-secret")]
        with patch("douyin_rag.organizer.shutil.which", return_value="codex.exe"), \
                patch("douyin_rag.organizer.subprocess.run", side_effect=responses):
            with self.assertRaises(CollectionError) as caught:
                await self.owner.model_json([], subscription=True)
            self.assertEqual(caught.exception.code, "subscription_model_error")
            self.assertNotIn("synthetic", str(caught.exception))

    async def test_subscription_bounds_images_and_reports_invalid_json(self):
        import subprocess
        def command(argv, **kwargs):
            if argv[1:3] == ["login", "status"]:
                return subprocess.CompletedProcess(argv, 0, stdout=b"ChatGPT", stderr=b"")
            Path(argv[argv.index("--output-last-message")+1]).write_text('[]', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        with patch("douyin_rag.organizer.shutil.which", return_value="codex.exe"), \
                patch("douyin_rag.organizer.subprocess.run", side_effect=command) as run:
            with self.assertRaises(CollectionError) as caught:
                await self.owner.model_json([], subscription=True, images=[b"a"]*9)
            self.assertEqual(caught.exception.code, "image_input_error")
            run.assert_not_called()
            with self.assertRaises(CollectionError) as caught:
                await self.owner.model_json([], subscription=True)
            self.assertEqual(caught.exception.code, "model_output_error")

    async def test_model_retry_receives_validation_reason_without_mutating_callers_messages(self):
        messages = [{"role": "system", "content": "原始输出要求"}, {"role": "user", "content": "合成数据"}]
        self.owner.model_json = AsyncMock(side_effect=[{"valid": False}, {"valid": True}])
        def validate(value):
            if not value["valid"]:
                raise CollectionError("名称缺少原文字证据", "model_output_error")
            return value
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            result = await self.owner._classification_model_json(messages, "识别背景", validate)
        self.assertTrue(result["valid"])
        retry = self.owner.model_json.await_args_list[1].args[0]
        self.assertIn("名称缺少原文字证据", retry[0]["content"])
        self.assertEqual(retry[0]["role"], "system")
        self.assertEqual(retry[-1], messages[-1])
        self.assertEqual(messages[0]["content"], "原始输出要求")
        self.assertEqual(len(messages), 2)

    async def test_missing_model_rows_never_commits_incomplete_batch(self):
        snapshot, _ = demo_data()
        snapshot["demo"] = False
        snapshot["items"] = snapshot["items"][:1]
        self.owner.data["snapshot"] = snapshot
        self.owner.model_json = AsyncMock(side_effect=[{"categories": ["知识", "待复核"]}, *([{"items": []}] * 3)])
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()) as delay:
            with self.assertRaises(CollectionError) as caught:
                await self.owner.classify()
        self.assertEqual(caught.exception.code, "model_output_error")
        self.assertEqual(self.owner.model_json.await_count, 4)
        self.assertEqual(delay.await_args_list, [call(2), call(4)])
        self.assertEqual(self.owner.data["plan"]["assignments"], {})
        self.assertEqual(self.owner.data["plan"]["phase"], "classifying")

    async def test_classification_saves_smaller_batches_after_output_errors_or_read_timeouts(self):
        for read_timeout in (False, True):
            with self.subTest(read_timeout=read_timeout):
                snapshot, plan = demo_data()
                snapshot["demo"] = False
                plan.update(phase="classifying", assignments={})
                self.owner.data.update(snapshot=snapshot, plan=plan)
                ids = [item["aweme_id"] for item in snapshot["items"]]
                half = len(ids) // 2
                submitted = []

                async def reply(messages):
                    batch = [item["aweme_id"] for item in json.loads(messages[-1]["content"])["items"]]
                    submitted.append(batch)
                    if batch == ids:
                        if read_timeout:
                            raise httpx.ReadTimeout("synthetic timeout")
                        return {"items": []}
                    if batch == ids[half:]:
                        saved = self.owner.read("workspace", {})["plan"]["assignments"]
                        self.assertEqual(list(saved), ids[:half])
                    return {"items": [
                        {"aweme_id": item_id, "category": "待复核", "confidence": .3, "reason": "合成理由"}
                        for item_id in batch
                    ]}

                self.owner.model_json = reply
                with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
                    await self.owner.classify()
                self.assertEqual(submitted, [ids, ids, ids, ids[:half], ids[half:]])
                self.assertEqual(list(plan["assignments"]), ids)
                self.assertEqual(plan["phase"], "ready")

    async def test_classification_does_not_split_when_connection_times_out(self):
        snapshot, plan = demo_data()
        snapshot["demo"] = False
        plan.update(phase="classifying", assignments={})
        self.owner.data.update(snapshot=snapshot, plan=plan)
        self.owner.model_json = AsyncMock(side_effect=httpx.ConnectTimeout("synthetic timeout"))
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(CollectionError) as caught:
                await self.owner.classify()
        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(self.owner.model_json.await_count, 3)
        self.assertEqual(plan["assignments"], {})

    async def test_classification_resume_retries_only_the_unsaved_batch(self):
        snapshot, plan = demo_data()
        snapshot["demo"] = False
        plan["phase"] = "classifying"
        last = snapshot["items"][-1]["aweme_id"]
        plan["assignments"].pop(last)
        self.owner.data.update(snapshot=snapshot, plan=plan)
        self.owner.model_json = AsyncMock(side_effect=[
            httpx.ReadTimeout("synthetic timeout"),
            {"items": [{"aweme_id": [], "category": "待复核", "confidence": .3, "reason": "合成理由"}]},
            {"items": [{"aweme_id": last, "category": "待复核", "confidence": .3, "reason": "合成理由"}]},
        ])
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()) as delay:
            await self.owner.classify()
        self.assertEqual(self.owner.data["plan"]["phase"], "ready")
        self.assertEqual(len(plan["assignments"]), len(snapshot["items"]))
        self.assertEqual(delay.await_args_list, [call(2), call(4)])
        first, second, third = self.owner.model_json.await_args_list
        self.assertEqual([second.args[0][-1], third.args[0][-1]], [first.args[0][-1], first.args[0][-1]])
        self.assertIn("上一份结果未通过校验", third.args[0][0]["content"])
        sent = self.owner.model_json.call_args.args[0][-1]["content"]
        self.assertEqual([item["aweme_id"] for item in json.loads(sent)["items"]], [last])

    async def test_classification_retry_exhaustion_reports_stage_and_keeps_checkpoint(self):
        snapshot, plan = demo_data()
        snapshot["demo"] = False
        plan["phase"] = "classifying"
        plan["assignments"].pop(snapshot["items"][-1]["aweme_id"])
        self.owner.data.update(snapshot=snapshot, plan=plan)
        self.owner.persist()
        self.owner.model_json = AsyncMock(side_effect=httpx.ReadTimeout("synthetic private details"))
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()) as delay:
            self.owner.start("classify", self.owner.classify)
            await self.owner.task
        self.assertEqual(self.owner.model_json.await_count, 3)
        self.assertEqual(delay.await_args_list, [call(2), call(4)])
        self.assertEqual(self.owner.job["status"], "failed")
        self.assertEqual(self.owner.job["error_code"], "network_error")
        total = len(snapshot["items"])
        self.assertEqual(self.owner.job["processed"], total - 1)
        self.assertEqual(self.owner.job["message"], f"归类作品 {total}–{total}失败：模型请求超时。进度已保存，可继续分类。")
        self.assertEqual(Organizer(self.temp.name).data["plan"], plan)

    async def test_classification_can_pause_before_retrying_category_names(self):
        snapshot, _ = demo_data()
        snapshot["demo"] = False
        self.owner.data["snapshot"] = snapshot
        self.owner.model_json = AsyncMock(side_effect=httpx.ConnectError("synthetic connection error"))

        async def pause_during_wait(_delay):
            self.owner.pause()

        with patch("douyin_rag.organizer.asyncio.sleep", new=pause_during_wait):
            self.owner.start("classify", self.owner.classify)
            await self.owner.task
        self.owner.model_json.assert_awaited_once()
        self.assertEqual(self.owner.job["status"], "paused")
        self.assertIsNone(self.owner.data["plan"])

    async def test_rename_and_revision_guard(self):
        self.owner.use_demo()
        plan = self.owner.data["plan"]
        before = plan["categories"][0]
        self.owner.edit_plan({"revision": 1, "rename": {"from": before, "to": "新的名字"}})
        self.assertNotIn(before, plan["categories"])
        self.assertTrue(any(a["category"] == "新的名字" for a in plan["assignments"].values()))
        with self.assertRaises(CollectionError):
            self.owner.edit_plan({"revision": 1, "rename": {"from": "新的名字", "to": "冲突修改"}})

    async def test_execute_resume_records_only_verified_items_and_final_check(self):
        snapshot, plan = demo_data()
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()))
        self.owner.data.update(snapshot=snapshot, plan=plan)
        events = []
        old_items = {item["aweme_id"] for item in snapshot["items"] if item["original_folders"]}
        memberships = {"demo-old": old_items.copy()}
        class Writer:
            async def open(inner, cookies): pass
            async def close(inner): pass
            async def ensure_folder(inner, name): return str(plan["categories"].index(name)+1)
            async def move_request(inner, item_ids, target=None, name="", source=None):
                for item_id in item_ids:
                    self.assertEqual(plan["operations"][item_id]["status"], "inflight")
                events.append(("add" if target else "remove", target or source))
                if target:
                    memberships.setdefault(target, set()).update(item_ids)
                else:
                    memberships[source].difference_update(item_ids)
            async def members(inner, folder):
                events.append(("read", folder))
                return memberships.get(folder, set()).copy()
        self.owner.writer_factory = Writer
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(plan["phase"], "completed")
        self.assertTrue(all(operation["status"] == "completed" for operation in plan["operations"].values()))
        self.assertEqual(sum(kind == "remove" for kind, _ in events), len(old_items))
        final_add = max(index for index, event in enumerate(events) if event[0] == "add")
        first_remove = next(index for index, event in enumerate(events) if event[0] == "remove")
        targets = set(plan["folder_ids"].values())
        probe_target = next(folder for kind, folder in events if kind == "add")
        self.assertCountEqual([folder for kind, folder in events if kind == "add"], targets)
        cleared_targets = {plan["folder_ids"][plan["assignments"][item_id]["category"]] for item_id in old_items}
        self.assertEqual({folder for kind, folder in events[final_add+1:first_remove] if kind == "read"}, targets | {"demo-old"})
        for folder in targets | {"demo-old"}:
            self.assertEqual(events.count(("read", folder)), 2 + (folder in cleared_targets) + (folder == probe_target))
        plan["phase"] = "partial"
        events.clear()
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            await self.owner.execute(plan["id"], plan["revision"])
        self.assertCountEqual(events, [("read", folder) for folder in targets])

    async def test_execute_does_not_clear_old_membership_when_final_target_is_missing(self):
        snapshot, plan = demo_data()
        item = next(item for item in snapshot["items"] if item["original_folders"])
        item_id = item["aweme_id"]
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()), items=[item])
        plan["assignments"] = {item_id: plan["assignments"][item_id]}
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        writer.ensure_folder.return_value = "123"
        writer.members.side_effect = [set(), {item_id}, set(), set()]
        self.owner.writer_factory = lambda: writer
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(CollectionError) as caught:
                await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(caught.exception.code, "verification_failed")
        self.assertEqual(plan["operations"][item_id]["status"], "unconfirmed")
        self.assertEqual(plan["phase"], "partial")
        writer.move_request.assert_awaited_once_with([item_id], target="123", name=plan["assignments"][item_id]["category"])
        self.assertEqual(writer.members.await_args_list, [call("123")] * 4)

    async def test_execute_checks_target_membership_after_clearing_old_membership(self):
        snapshot, plan = demo_data()
        item = next(item for item in snapshot["items"] if item["original_folders"])
        item_id = item["aweme_id"]
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()), items=[item])
        plan["assignments"] = {item_id: plan["assignments"][item_id]}
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        writer.ensure_folder.return_value = "123"
        writer.members.side_effect = [set(), {item_id}, {item_id}, {item_id}, set(), set()]
        self.owner.writer_factory = lambda: writer
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(CollectionError) as caught:
                await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(caught.exception.code, "verification_failed")
        self.assertEqual(plan["operations"][item_id]["status"], "inflight")
        self.assertEqual(plan["phase"], "partial")
        self.assertEqual(writer.members.await_args_list, [
            call("123"), call("123"), call("123"), call("demo-old"), call("demo-old"), call("123"),
        ])

    async def test_execute_resume_skips_a_write_that_succeeded_without_a_response(self):
        snapshot, plan = demo_data()
        item = next(item for item in snapshot["items"] if not item["original_folders"])
        item_id = item["aweme_id"]
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()), items=[item])
        plan["assignments"] = {item_id: plan["assignments"][item_id]}
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        writer.ensure_folder.return_value = "123"
        writer.members.side_effect = [set(), {item_id}, {item_id}]
        writer.move_request.side_effect = CollectionError("合成响应丢失", "write_unconfirmed")
        self.owner.writer_factory = lambda: writer
        with self.assertRaises(CollectionError):
            await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(plan["operations"][item_id]["status"], "inflight")
        await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(plan["operations"][item_id]["status"], "completed")
        self.assertEqual(plan["phase"], "completed")
        self.assertEqual(writer.move_request.await_count, 1)

    async def test_execute_expands_verified_probe_to_bounded_batches(self):
        snapshot, plan = demo_data()
        ids = [str(90071992547409930 + index) for index in range(14)]
        category = plan["categories"][0]
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()),
                        items=[{"aweme_id": item_id, "original_folders": []} for item_id in ids])
        plan["assignments"] = {item_id: {"category": category} for item_id in ids}
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        writer.ensure_folder.return_value = "123"
        writer.members.side_effect = [set(), set(ids[:3]), set(ids)]
        self.owner.writer_factory = lambda: writer
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(writer.move_request.await_args_list, [
            call(ids[:3], target="123", name=category),
            call(ids[3:13], target="123", name=category),
            call(ids[13:], target="123", name=category),
        ])
        self.assertEqual(writer.members.await_count, 3)
        self.assertEqual(sleep.await_args_list, [call(5)] * 3)
        self.assertEqual(plan["phase"], "completed")

    async def test_execute_partial_probe_stops_and_resumes_only_missing_ids(self):
        snapshot, plan = demo_data()
        ids = ["11", "22", "33", "44"]
        category = plan["categories"][0]
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()),
                        items=[{"aweme_id": item_id, "original_folders": []} for item_id in ids])
        plan["assignments"] = {item_id: {"category": category} for item_id in ids}
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        writer.ensure_folder.return_value = "123"
        writer.members.side_effect = [set(), {"11", "22"}, {"11", "22"}, set(ids), set(ids)]
        self.owner.writer_factory = lambda: writer
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(CollectionError) as caught:
                await self.owner.execute(plan["id"], plan["revision"])
            self.assertEqual(caught.exception.code, "verification_failed")
            self.assertEqual(plan["phase"], "partial")
            self.assertEqual(set(plan["operations"]), set(ids[:3]))
            writer.move_request.assert_awaited_once_with(ids[:3], target="123", name=category)
            await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(writer.move_request.await_args_list[-1], call(ids[2:], target="123", name=category))
        self.assertEqual(plan["phase"], "completed")

    async def test_demo_and_account_change_never_open_writer(self):
        self.owner.use_demo()
        self.owner.writer_factory = MockWriter = AsyncMock()
        plan = self.owner.data["plan"]
        with self.assertRaises(CollectionError):
            await self.owner.execute(plan["id"], plan["revision"])
        MockWriter.assert_not_called()
        self.owner.data["snapshot"].update(demo=False, account_marker="different-session")
        with self.assertRaises(CollectionError):
            await self.owner.execute(plan["id"], plan["revision"])
        MockWriter.assert_not_called()

    async def test_ego_identity_errors_stop_before_writing(self):
        snapshot, plan = demo_data()
        cookies = self.owner.cookies()
        snapshot.update(demo=False, account_marker=self.owner.account_marker(cookies))
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        self.owner.writer_factory = lambda: writer
        for code in ("account_changed", "account_unconfirmed", "browser_session_changed"):
            with self.subTest(code=code):
                writer.open.side_effect = CollectionError("合成账号校验错误", code)
                with self.assertRaises(CollectionError) as caught:
                    await self.owner.execute(plan["id"], plan["revision"])
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(plan["phase"], "partial")
                self.assertEqual(plan["operations"], {})
        self.assertEqual(writer.open.await_count, 3)
        writer.open.assert_awaited_with(cookies)
        writer.ensure_folder.assert_not_awaited()
        writer.move_request.assert_not_awaited()
        writer.close.assert_not_awaited()

    async def test_restart_marks_running_job_interrupted_and_never_returns_key(self):
        self.owner.write("job", {"kind": "classify", "status": "running"})
        restored = Organizer(self.temp.name)
        self.assertEqual(restored.job["status"], "interrupted")
        self.assertNotIn("api_key", self.owner.public_settings())

    async def test_rescan_archives_original_memberships_and_completed_operations(self):
        snapshot, plan = demo_data()
        snapshot["demo"] = False
        plan["operations"] = {snapshot["items"][0]["aweme_id"]: {"status": "completed", "target": "new", "original_folders": ["old"]}}
        self.owner.data.update(snapshot=snapshot, plan=plan)
        with patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock()):
            await self.owner.scan()
        archive = self.owner.data["archives"][0]
        record = self.owner.read("archive-" + archive["id"], None)
        self.assertEqual(next(iter(record["plan"]["operations"].values()))["original_folders"], ["old"])
        self.assertIsNone(self.owner.data["plan"])

    async def test_rejected_write_keeps_window_and_unconfirmed_progress_for_user(self):
        snapshot, plan = demo_data()
        snapshot.update(demo=False, account_marker=self.owner.account_marker(self.owner.cookies()))
        self.owner.data.update(snapshot=snapshot, plan=plan)
        writer = AsyncMock()
        writer.ensure_folder.return_value = "123"
        writer.members.return_value = set()
        writer.move_request.side_effect = CollectionError("合成平台验证提示", "write_rejected")
        self.owner.writer_factory = lambda: writer
        with self.assertRaises(CollectionError):
            await self.owner.execute(plan["id"], plan["revision"])
        self.assertEqual(plan["phase"], "partial")
        self.assertEqual(next(iter(plan["operations"].values()))["status"], "inflight")
        writer.move_request.assert_awaited_once()
        writer.close.assert_not_awaited()
