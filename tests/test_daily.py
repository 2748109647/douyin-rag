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
        self.owner.knowledge.transcriber = Mock(
            extract=AsyncMock(return_value={"segments": [{"start": 0., "end": 8., "text": "合成正文：先分析，再动手。"}], "frames": [], "speech_error": "", "frame_error": ""}),
            extract_note=AsyncMock(return_value={"images": [], "text": "合成图文正文：先分析，再动手。", "error": ""}))
        self.owner.knowledge.embeddings = Mock(encode=Mock(side_effect=lambda values: [[1., 0.] for _ in values]))
        self.owner.data["plan"] = {"categories": ["知识工具", "待复核"]}
        self.owner.data["snapshot"] = {"id": "manual-preserved"}
        self.owner.persist()

        async def model(messages, **kwargs):
            if "视频内容分析助手" in messages[0]["content"] or "图文内容分析助手" in messages[0]["content"]:
                if self.summary_error:
                    raise CollectionError("合成模型不可用", "model_error")
                payload = json.loads(messages[1]["content"])
                return {"content_type": "知识讲解", "work_name": "", "name_evidence": "", "visual_text": "",
                        "description": "图文讲解先分析问题再采取行动。" if payload.get("kind") == "note" else "视频讲解先分析问题再采取行动。",
                        "sources": [payload["available_sources"][0]]}
            if "日报助手" in messages[0]["content"]:
                if self.summary_error:
                    raise CollectionError("合成模型不可用", "model_error")
                if '"short_title"' in messages[0]["content"]:
                    payload = json.loads(messages[1]["content"])
                    prefix = "仅据标题：" if payload.get("analysis", {}).get("sources") == ["title"] else ""
                    return {"short_title": "先分析再动手", "summary": prefix + "先分析问题，再采取行动。"}
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
        self.assertEqual(set(report["items"]), {"2", "3"})
        self.assertEqual(report["items"]["2"]["category"], "知识工具")
        self.assertEqual(report["items"]["2"]["short_title"], "先分析再动手")
        self.assertLessEqual(len(report["items"]["2"]["summary"]), 100)
        self.assertNotIn("points", report["items"]["2"])
        self.assertEqual(report["items"]["3"]["url"], "https://www.douyin.com/note/3")
        self.assertEqual(self.members["10"], {"2", "3"})
        self.assertEqual(self.members["9"], {"1"})
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
        self.assertIn("没有新增作品", markdown)
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
        self.assertEqual(sum(call.kwargs.get("transcribe_audio", True) for call in self.owner.knowledge.transcriber.extract.await_args_list), 2)
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
        self.owner.knowledge.transcriber.extract.assert_awaited_once()
        self.assertFalse(self.owner.knowledge.transcriber.extract.call_args.kwargs["transcribe_audio"])
        self.assertEqual(self.report()["items"]["2"]["status"], "ready")

    async def test_long_transcript_uses_all_parts_and_recovers_after_restart(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.owner.knowledge.transcriber.extract.return_value["segments"] = [{"start": 0, "end": 300, "text": "合" * 25000 + "末尾证据"}]
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

    async def test_brief_summary_retries_over_one_hundred_characters_instead_of_truncating(self):
        worker = self.runner.worker("brief-retry")
        self.owner.model_json.side_effect = [
            {"short_title": "简明标题", "summary": "长" * 101},
            {"short_title": "简明标题", "summary": "保留核心结论。"},
        ]
        value = await self.runner.summary(worker, "合成证据", compact=True)
        self.assertEqual(value["summary"], "保留核心结论。")
        self.assertEqual(self.owner.model_json.await_count, 2)

    async def test_invalid_brief_never_marks_video_ready_and_does_not_block_moves(self):
        await self.runner.run(self.moment())
        self.add("2")
        original = self.owner.model_json.side_effect

        async def invalid(messages, **kwargs):
            if '"short_title"' in messages[0]["content"]:
                return {"short_title": "简明标题", "summary": "长" * 101}
            return await original(messages, **kwargs)
        self.owner.model_json.side_effect = invalid
        await self.runner.run(self.moment(2))
        report = self.report()
        self.assertEqual(report["items"]["2"]["status"], "failed")
        self.assertNotIn("summary", report["items"]["2"])
        self.assertEqual(report["move_status"], "completed")

    async def test_legacy_ready_summary_is_compacted_without_indexing_or_moving_again(self):
        await self.runner.run(self.moment())
        self.add("2")
        await self.runner.run(self.moment(2))
        state = self.records()
        entry = state["runs"][0]
        entry["status"] = "partial"
        self.owner.write("daily-state", state)
        worker = self.runner.worker(entry["id"])
        report = worker.read("report", {})
        row = report["items"]["2"]
        row.pop("short_title")
        row.update(summary="旧摘要" * 30, points=["旧的重点"])
        worker.write("report", report)
        self.owner.knowledge.transcriber.extract.reset_mock()
        self.owner.model_json.reset_mock()
        previous_moves = len(self.moves)
        await self.runner.run(self.moment(2))
        self.assertLessEqual(len(self.report()["items"]["2"]["summary"]), 100)
        self.owner.knowledge.transcriber.extract.assert_not_awaited()
        self.assertEqual(self.owner.model_json.await_count, 1)
        self.assertEqual(len(self.moves), previous_moves)

    async def test_brief_copy_keeps_original_files_and_only_displays_title_and_summary(self):
        await self.runner.run(self.moment())
        self.add("2")
        await self.runner.run(self.moment(2))
        worker = self.runner.worker(self.records()["runs"][0]["id"])
        report = worker.read("report", {})
        originals = {name: (worker.root/name).read_bytes() for name in ("report.json", "report.md", "workspace.json", "job.json")}
        report["items"]["2"].update(short_title="精简标题", summary="一句话总结。", points=["不应显示的重点"])
        DailyRunner.save_report(worker, report, brief_copy=True)
        for name, original in originals.items():
            self.assertEqual((worker.root/name).read_bytes(), original)
        brief = (worker.root/"report-brief.md").read_text(encoding="utf-8")
        self.assertIn("**[精简标题](https://www.douyin.com/video/2)**", brief)
        self.assertIn("一句话总结。", brief)
        self.assertNotIn("不应显示的重点", brief)
        self.assertNotIn("打开原视频", brief)

    async def test_failed_video_displays_a_short_honest_explanation_with_legacy_title(self):
        worker = self.runner.worker("failed-legacy-report")
        report = {"day": "2026-11-14", "move_status": "completed", "items": {
            "2": {"title": "原始标题" * 30, "url": "https://www.douyin.com/video/2", "status": "failed",
                  "error": "未识别到可用语音，首版暂不识别画面文字或无声演示。"}}}
        DailyRunner.save_report(worker, report)
        markdown = (worker.root/"report.md").read_text(encoding="utf-8")
        self.assertIn("未识别到可用语音或图文文字，无法生成总结。", markdown)
        self.assertNotIn("原始标题" * 30, markdown)
        self.assertNotIn("一句话总结", markdown)

    async def test_silent_video_uses_frames_without_claiming_speech_transcript(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.owner.knowledge.transcriber.extract.return_value = {
            "segments": [], "frames": [{"time": 0., "image": b"synthetic-jpeg"}],
            "speech_error": "未识别到可用语音。", "frame_error": ""}
        await self.runner.run(self.moment(2))
        row = self.report()["items"]["2"]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["analysis"]["sources"], ["frames"])
        worker = self.runner.worker(self.records()["runs"][0]["id"])
        self.assertEqual(worker.knowledge.document(worker.data["snapshot"]["items"][0])["status"], "failed")
        image_calls = [call for call in self.owner.model_json.await_args_list if call.kwargs.get("images")]
        self.assertEqual(image_calls[0].kwargs["images"], [b"synthetic-jpeg"])
        self.assertTrue(all(call.kwargs.get("subscription") for call in self.owner.model_json.await_args_list))

    async def test_title_only_fallback_is_explicit_in_report(self):
        await self.runner.run(self.moment())
        self.add("2")
        self.owner.knowledge.transcriber.extract.return_value = {
            "segments": [], "frames": [], "speech_error": "未获得语音。", "frame_error": "合成画面错误"}
        await self.runner.run(self.moment(2))
        row = self.report()["items"]["2"]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["analysis"]["sources"], ["title"])
        self.assertTrue(row["summary"].startswith("仅据标题："))

    async def test_old_abstract_summary_is_reanalyzed_from_evidence(self):
        await self.runner.run(self.moment())
        self.add("2")
        await self.runner.run(self.moment(2))
        state = self.records()
        state["runs"][0]["status"] = "partial"
        self.owner.write("daily-state", state)
        worker = self.runner.worker(state["runs"][0]["id"])
        report = worker.read("report", {})
        row = report["items"]["2"]
        row.pop("analysis_version")
        row.update(short_title="旧的抽象会议标题", summary="不应继续作为依据的旧泛化总结。")
        worker.write("report", report)
        self.owner.model_json.reset_mock()
        await self.runner.run(self.moment(2))
        sent = json.dumps([call.args[0] for call in self.owner.model_json.await_args_list], ensure_ascii=False)
        self.assertNotIn("不应继续作为依据的旧泛化总结", sent)
        self.assertEqual(self.report()["items"]["2"]["analysis"]["sources"], ["audio"])


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

    def test_brief_title_and_summary_character_limits_include_punctuation(self):
        valid = {"short_title": "题" * 40, "summary": "字" * 99 + "。"}
        self.assertEqual(DailyRunner.valid_summary(valid, compact=True), valid)
        for value in ({}, {"short_title": "题" * 41, "summary": "总结"},
                      {"short_title": "题", "summary": "字" * 100 + "。"},
                      {"short_title": "题", "summary": " "},
                      {"short_title": "题\n目", "summary": "总结"},
                      {"short_title": "题", "summary": ["总结"]}):
            with self.assertRaises(CollectionError):
                DailyRunner.valid_summary(value, compact=True)

    def test_movie_titles_preserve_work_name_or_explicitly_mark_unknown(self):
        context = {"content_type": "影视片段", "work_name": "合成剧", "sources": ["audio", "frames"]}
        with self.assertRaises(CollectionError):
            DailyRunner.valid_summary({"short_title": "高层会议表决起义时间", "summary": "人物在讨论行动日期。"}, compact=True, context=context)
        good = {"short_title": "《合成剧》片段：讨论起义日期", "summary": "角色在会议中争论起义日期，最终表决确定行动时间。"}
        self.assertEqual(DailyRunner.valid_summary(good, compact=True, context=context), good)
        context["work_name"] = ""
        good["short_title"] = "影视片段：起义日期争论（剧名未确认）"
        self.assertEqual(DailyRunner.valid_summary(good, compact=True, context=context), good)
        with self.assertRaises(CollectionError):
            DailyRunner.valid_summary({"short_title": "会议争论", "summary": "角色讨论日期。"}, compact=True, context=context)

    def test_analysis_requires_real_available_sources_and_textual_name_evidence(self):
        value = {"content_type": "影视片段", "work_name": "合成剧", "name_evidence": "剧名：合成剧", "visual_text": "剧名：合成剧",
                 "description": "角色在会议中争论日期。", "sources": ["frames"]}
        self.assertEqual(DailyRunner.valid_analysis(value, ["frames"], "", "")["work_name"], "合成剧")
        with self.assertRaises(CollectionError):
            DailyRunner.valid_analysis(value, ["title"], "", "")
        value["visual_text"] = ""
        with self.assertRaises(CollectionError):
            DailyRunner.valid_analysis(value, ["frames"], "", "")
        context = {"content_type": "其他", "work_name": "", "sources": ["title"]}
        with self.assertRaises(CollectionError):
            DailyRunner.valid_summary({"short_title": "标题推测", "summary": "未经标明的推断。"}, compact=True, context=context)

    def test_analysis_invalid_fields_explain_the_exact_retry_requirement(self):
        value = {"content_type": "影视片段", "work_name": None, "name_evidence": "", "visual_text": "",
                 "description": "无法确认片名的剧情片段。", "sources": ["title"]}
        with self.assertRaises(CollectionError) as caught:
            DailyRunner.valid_analysis(value, ["title"], "原标题", "")
        self.assertIn("work_name", str(caught.exception))
        self.assertIn("空字符串", str(caught.exception))
        value["work_name"] = ""
        self.assertEqual(DailyRunner.valid_analysis(value, ["title"], "原标题", "")["work_name"], "")
