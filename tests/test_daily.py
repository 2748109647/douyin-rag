import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from douyin_rag.collections import CollectionError
from douyin_rag.daily import DailyRunner, BEIJING, run_daily
from douyin_rag.organizer import OperationLock, Organizer


class DailyTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.owner = Organizer(self.temp.name)
        self.owner.cookies = lambda: "sessionid=synthetic-daily"
        self.account = hashlib.sha256(b"synthetic-account").hexdigest()
        self.ids = ["1"]
        self.members = {"9": {"1"}, "10": set()}
        self.moves = []
        self.move_error = False
        self.summary_error = False
        self.scan_error = False
        outer = self

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def own_account_marker(self): return outer.account
            async def get_collected_videos(self, **kwargs):
                if outer.scan_error:
                    raise CollectionError("合成分页失败", "incomplete_scan")
                return {"aweme_list": [outer.item(i) for i in outer.ids], "cursor": 0, "has_more": False}
            async def get_collection_folders(self, **kwargs):
                return {"collects_list": [{"collects_id": "9", "collects_name": "原收藏夹"},
                                         {"collects_id": "10", "collects_name": "知识工具"}], "cursor": 0, "has_more": False}
            async def get_collection_videos(self, collects_id, **kwargs):
                return {"aweme_list": [outer.item(i) for i in outer.members[collects_id] if i in outer.ids], "cursor": 0, "has_more": False}

        class Writer:
            async def open(self, cookies): pass
            async def close(self): pass
            async def ensure_folder(self, name):
                outer.assertEqual(name, "知识工具")
                return "10"
            async def members(self, folder): return set(outer.members[folder])
            async def move_request(self, item_ids, target=None, name="", source=None):
                outer.moves.append((list(item_ids), target, source))
                if target:
                    outer.members[target].update(item_ids)
                    if outer.move_error:
                        raise CollectionError("合成写入响应中断", "write_unconfirmed")
                else:
                    outer.members[source].difference_update(item_ids)

        self.owner.client_factory = lambda cookies: Client()
        self.owner.writer_factory = Writer
        self.owner.knowledge.transcriber = Mock(extract=AsyncMock(return_value=[{"start": 0., "end": 8., "text": "合成正文：先分析，再动手。"}]))
        self.owner.knowledge.embeddings = Mock(encode=Mock(side_effect=lambda values: [[1., 0.] for _ in values]))
        self.owner.data["plan"] = {"categories": ["知识工具", "待复核"]}
        self.owner.data["snapshot"] = {"id": "manual-preserved"}
        self.owner.persist()

        async def model(messages):
            if "日报助手" in messages[0]["content"]:
                if self.summary_error:
                    raise CollectionError("合成模型不可用", "model_error")
                return {"summary": "先分析，再动手。", "points": ["先进行分析。"]}
            payload = json.loads(messages[1]["content"])
            return {"items": [{"aweme_id": item["aweme_id"], "category": "知识工具", "reason": "合成依据", "confidence": .95}
                              for item in payload["items"]]}
        self.owner.model_json = AsyncMock(side_effect=model)
        self.runner = DailyRunner(self.owner)
        self.sleep = patch("douyin_rag.organizer.asyncio.sleep", new=AsyncMock())
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.temp.cleanup()

    @staticmethod
    def item(item_id):
        return {"aweme_id": item_id, "desc": "合成作品 " + item_id,
                "aweme_type": 68 if item_id == "3" else 0, "video": {"duration": 8000}}

    def moment(self, day=1):
        return datetime(2026, 11, day, 6, tzinfo=BEIJING)

    def add(self, item_id):
        self.ids.append(item_id)
        self.members["9"].add(item_id)

    def records(self):
        return self.owner.read("daily-state", {})

    def report(self, entry=None):
        entry = entry or self.records()["runs"][-1]
        return json.loads((self.owner.root / "daily" / entry["id"] / "report.json").read_text(encoding="utf-8"))

    async def test_first_run_only_baselines_then_summarizes_and_moves_new_videos(self):
        result = await self.runner.run(self.moment())
        self.assertEqual(result["status"], "baseline")
        self.owner.model_json.assert_not_awaited()
        self.owner.knowledge.transcriber.extract.assert_not_awaited()
        self.add("2")
        self.add("3")
        result = await self.runner.run(self.moment(2))
        self.assertEqual(result["status"], "completed")
        report = self.report()
        self.assertEqual(set(report["items"]), {"2"})
        self.assertEqual(report["items"]["2"]["category"], "知识工具")
        self.assertEqual(self.members["10"], {"2"})
        self.assertEqual(self.members["9"], {"1", "3"})
        self.assertEqual(self.owner.read("workspace", {})["snapshot"]["id"], "manual-preserved")
        self.assertFalse((self.owner.root / "daily" / self.records()["runs"][0]["id"] / "settings.json").exists())
        calls = self.owner.model_json.await_count
        moves = len(self.moves)
        await self.runner.run(self.moment(2))
        self.assertEqual(len(self.records()["runs"]), 1)
        self.assertEqual(self.owner.model_json.await_count, calls)
        self.assertEqual(len(self.moves), moves)

    async def test_empty_day_has_report_without_loading_models(self):
        await self.runner.run(self.moment())
        await self.runner.run(self.moment(2))
        self.assertEqual(self.report()["status"], "completed")
        entry = self.records()["runs"][0]
        markdown = (self.owner.root / "daily" / entry["id"] / "report.md").read_text(encoding="utf-8")
        self.assertIn("没有新增视频", markdown)
        self.owner.model_json.assert_not_awaited()
        self.owner.knowledge.transcriber.extract.assert_not_awaited()

    async def test_summary_failure_does_not_block_moves_or_next_days_new_videos(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.summary_error = True
        self.assertEqual((await self.runner.run(self.moment(2)))["status"], "partial")
        self.assertEqual(self.report()["move_status"], "completed")
        self.assertEqual(self.report()["items"]["2"]["status"], "failed")
        self.assertIn("2", self.members["10"])
        self.summary_error = False
        self.add("4")
        await self.runner.run(self.moment(3))
        self.assertTrue(all(run["status"] == "completed" for run in self.records()["runs"]))
        self.assertEqual(self.owner.knowledge.transcriber.extract.await_count, 2)
        self.assertEqual(self.members["10"], {"2", "4"})

    async def test_uncertain_write_resumes_without_repeating_successful_add(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.move_error = True
        await self.runner.run(self.moment(2))
        self.assertEqual(self.report()["move_status"], "failed")
        self.assertEqual(self.report()["items"]["2"]["status"], "ready")
        self.move_error = False
        await self.runner.run(self.moment(2))
        self.assertEqual(self.report()["status"], "completed")
        self.assertEqual(sum(target is not None for _, target, _ in self.moves), 1)
        self.assertNotIn("2", self.members["9"])

    async def test_uncollected_pending_video_is_not_added_back(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.move_error = True
        await self.runner.run(self.moment(2))
        self.ids.remove("2")
        self.members["10"].discard("2")
        previous_moves = len(self.moves)
        await self.runner.run(self.moment(2))
        self.assertEqual(len(self.moves), previous_moves)
        self.assertEqual(self.report()["items"]["2"]["status"], "skipped")
        self.assertEqual(self.report()["status"], "completed")

    async def test_failed_scan_never_advances_baseline_or_consumes_new_items(self):
        await self.runner.run(self.moment())
        before = self.records()
        self.add("2")
        self.scan_error = True
        with self.assertRaises(CollectionError):
            await self.runner.run(self.moment(2))
        self.assertEqual(self.records(), before)
        self.scan_error = False
        await self.runner.run(self.moment(2))
        self.assertIn("2", self.report()["items"])

    async def test_other_account_gets_separate_baseline_and_never_resumes_previous_moves(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.move_error = True
        await self.runner.run(self.moment(2))
        before = len(self.moves)
        self.account = hashlib.sha256(b"another-synthetic-account").hexdigest()
        self.owner.cookies = lambda: "sessionid=another-synthetic-session"
        result = await self.runner.run(self.moment(3))
        self.assertEqual(result["status"], "baseline")
        self.assertEqual(len(self.moves), before)
        self.assertEqual(len(self.records()["accounts"]), 2)

    async def test_beijing_calendar_day_controls_deduplication(self):
        # UTC 22点是北京时间次日6点。
        await self.runner.run(datetime(2026, 10, 31, 22, tzinfo=timezone.utc))
        self.add("2")
        await self.runner.run(self.moment())
        self.assertEqual(self.records()["runs"], [])
        await self.runner.run(self.moment(2))
        self.assertIn("2", self.report()["items"])

    async def test_baseline_before_six_does_not_skip_same_mornings_report(self):
        await self.runner.run(datetime(2026, 11, 1, 5, tzinfo=BEIJING))
        self.add("2")
        await self.runner.run(self.moment())
        self.assertEqual(self.report()["day"], "2026-11-01")
        self.assertIn("2", self.report()["items"])

    async def test_scan_extension_preserves_default_return_and_manual_workspace(self):
        scanner = self.runner.worker("scan-contract")
        original = {"snapshot": {"id": "manual-previous"}, "plan": {"id": "manual-plan"}}
        scanner.data.update(original)
        snapshot = await scanner.scan(save=False)
        self.assertTrue(snapshot["complete"])
        self.assertEqual(scanner.data["snapshot"], original["snapshot"])
        self.assertEqual(scanner.data["plan"], original["plan"])
        # 默认调用仍返回 None，沿用手动扫描的原返回语义。
        scanner.data["snapshot"] = None
        self.assertIsNone(await scanner.scan())

    async def test_demo_categories_do_not_leak_into_real_daily_folders(self):
        self.owner.data["snapshot"] = {"demo": True}
        self.owner.data["plan"] = {"categories": ["演示专用分类", "待复核"]}
        self.assertEqual(self.runner.categories({"folders": [{"name": "真实收藏夹"}]}, []), ["真实收藏夹", "待复核"])

    async def test_cancel_before_task_start_does_not_leak_process_lock(self):
        self.owner.start("scan", AsyncMock())
        self.owner.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.owner.task
        with OperationLock(self.owner.root):
            pass

    async def test_existing_transcript_is_reused(self):
        await self.runner.run(self.moment())
        self.add("2")
        worker = self.runner.worker("cached-transcript")
        worker.data["snapshot"] = {"complete": True, "account_marker": self.owner.account_marker(self.owner.cookies()), "items": []}
        item = {"aweme_id": "2", "duration": 8, "kind": "video"}
        worker.knowledge.save_document(worker.knowledge.account(), item, "ready", [{"start": 0, "end": 8, "text": "缓存的合成正文"}])
        await self.runner.run(self.moment(2))
        self.owner.knowledge.transcriber.extract.assert_not_awaited()
        self.assertEqual(self.report()["items"]["2"]["status"], "ready")

    async def test_long_transcript_uses_all_parts_and_recovers_after_restart(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.owner.knowledge.transcriber.extract.return_value = [{"start": 0, "end": 300, "text": "合" * 25000 + "末尾证据"}]
        await self.runner.run(self.moment(2))
        summary_calls = [call.args[0] for call in self.owner.model_json.await_args_list if "日报助手" in call.args[0][0]["content"]]
        self.assertTrue(any("末尾证据" in messages[1]["content"] for messages in summary_calls))
        self.assertTrue(all(len(messages[1]["content"]) <= 12000 for messages in summary_calls))
        new_runner = DailyRunner(self.owner)
        self.owner.model_json.reset_mock()
        await new_runner.run(self.moment(2))
        self.owner.model_json.assert_not_awaited()

    async def test_daily_run_is_excluded_by_manual_task_and_lock_releases_on_error(self):
        gate = asyncio.Event()
        self.owner.start("scan", gate.wait)
        with self.assertRaises(CollectionError) as caught:
            await self.runner.run(self.moment())
        self.assertEqual(caught.exception.code, "busy")
        gate.set()
        await self.owner.task
        self.scan_error = True
        with self.assertRaises(CollectionError):
            await self.runner.run(self.moment())
        with OperationLock(self.owner.root):
            pass

    async def test_partial_report_survives_completed_plan_before_final_checkpoint(self):
        await self.runner.run(self.moment())
        self.add("2")
        await self.runner.run(self.moment(2))
        state = self.records()
        state["runs"][0]["status"] = "partial"
        self.owner.write("daily-state", state)
        worker = self.runner.worker(state["runs"][0]["id"])
        report = worker.read("report", {})
        report["move_status"] = "pending"
        worker.write("report", report)
        before = len(self.moves)
        await self.runner.run(self.moment(2))
        self.assertEqual(len(self.moves), before)
        self.assertEqual(self.report()["status"], "completed")

    async def test_run_wrapper_saves_sanitized_network_failure_without_advancing_state(self):
        import httpx
        with patch("douyin_rag.daily.Organizer", return_value=self.owner), \
                patch.object(DailyRunner, "run", new=AsyncMock(side_effect=httpx.ConnectError("synthetic-private-url"))):
            result = await run_daily(self.owner.root)
        self.assertEqual(result["status"], "failed")
        saved = self.owner.read("daily-last-run", {})
        self.assertNotIn("synthetic-private-url", json.dumps(saved))
        self.assertFalse((self.owner.root / "daily-state.json").exists())


class LockAndValidationTest(TestCase):
    def test_lock_works_across_processes_and_releases_after_exit(self):
        with TemporaryDirectory() as directory:
            script = "from douyin_rag.organizer import OperationLock; from douyin_rag.collections import CollectionError; import sys\ntry:\n with OperationLock(sys.argv[1]): pass\nexcept CollectionError: sys.exit(7)"
            with OperationLock(directory):
                child = subprocess.run([sys.executable, "-c", script, directory], capture_output=True, timeout=20)
                self.assertEqual(child.returncode, 7, child.stderr)
                owner = Organizer(directory)
                with self.assertRaises(CollectionError):
                    owner.save_settings({"model": "test"})
            child = subprocess.run([sys.executable, "-c", script, directory], capture_output=True, timeout=20)
            self.assertEqual(child.returncode, 0, child.stderr)

    def test_summary_validation_and_markdown_never_invents_success(self):
        for value in ({}, {"summary": "ok", "points": []}, {"summary": "ok", "points": [1]},
                      {"summary": "x" * 1001, "points": ["ok"]}):
            with self.assertRaises(CollectionError):
                DailyRunner.valid_summary(value)
