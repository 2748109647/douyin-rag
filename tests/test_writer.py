import json
import os
import subprocess
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from douyin_rag.collections import CollectionError
from douyin_rag.ego_writer import EgoWriter
from douyin_rag.writer import BrowserWriter
from douyin_rag.verification import DOUYIN_FIXED_USER_AGENT


class WriterTest(IsolatedAsyncioTestCase):
    async def test_headless_scan_reuses_session_and_visible_verification_reopens_profile(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            page = Mock()
            page.is_closed.return_value = False
            page.goto = AsyncMock()
            context = AsyncMock()
            context.pages = [page]
            playwright = Mock()
            playwright.stop = AsyncMock()
            playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
            starter = Mock()
            starter.start = AsyncMock(return_value=playwright)
            with patch("douyin_rag.writer.async_playwright", return_value=starter):
                await writer.open(headless=True)
                self.assertTrue(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])
                self.assertEqual(playwright.chromium.launch_persistent_context.call_args.kwargs["user_agent"], DOUYIN_FIXED_USER_AGENT)
                await writer.open(headless=True)
                self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 1)
                await writer.open(headless=False)
                self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])
                self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 2)
                context.close.assert_awaited_once()
                await writer.close()

    async def test_qr_login_finishes_on_session_without_probing_collections(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            writer.page = Mock()
            writer.page.is_closed.return_value = False
            writer.page.locator.return_value.all = AsyncMock(return_value=[])
            writer.page.get_by_text.return_value.all = AsyncMock(return_value=[])
            qr = AsyncMock()
            qr.screenshot.return_value = b"synthetic-png"
            writer.qr_element = AsyncMock(return_value=qr)
            result = await writer.login_state()
            self.assertEqual(result["status"], "waiting")
            self.assertTrue(result["qr_image"].startswith("data:image/png;base64,"))
            expired = AsyncMock()
            expired.is_visible.return_value = True
            writer.page.get_by_text.return_value.all.return_value = [expired]
            self.assertEqual((await writer.login_state())["status"], "expired")
            writer.qr_element.return_value = None
            writer.cookies = AsyncMock(return_value="sessionid=synthetic")
            with patch("douyin_rag.collections.CollectionClient.get_collection_folders",
                       side_effect=AssertionError("Login must not read collections")) as fetch:
                result = await writer.login_state()
                self.assertEqual(result["status"], "authenticated")
                self.assertIn("扫描时确认收藏能否读取", result["message"])
                # 手机确认后，即使页面二维码尚未消失，也不阻塞登录完成。
                writer.qr_element.return_value = qr
                self.assertEqual((await writer.login_state())["status"], "authenticated")
                fetch.assert_not_called()
            writer.page.is_closed.return_value = True
            self.assertEqual((await writer.login_state())["status"], "error")

    async def test_login_cookies_include_douyin_subdomains_and_exclude_other_sites(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            writer.context = AsyncMock()
            writer.context.cookies.return_value = [
                {"name": "sessionid", "value": "synthetic", "domain": ".douyin.com"},
                {"name": "passport_csrf_token", "value": "synthetic-csrf", "domain": "passport.douyin.com"},
                {"name": "sessionid", "value": "unrelated", "domain": "notdouyin.com"},
                {"name": "other", "value": "private", "domain": "example.test"},
            ]
            self.assertEqual(await writer.cookies(), "sessionid=synthetic; passport_csrf_token=synthetic-csrf")
            writer.context.cookies.assert_awaited_once_with()
            await writer.save_login()
            self.assertEqual((writer.root / "login.cookies").read_text(encoding="utf-8"),
                             "sessionid=synthetic; passport_csrf_token=synthetic-csrf")

    async def test_qr_capture_requires_unique_visible_element_with_reasonable_size(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            writer.page = Mock()
            qr = AsyncMock()
            qr.is_visible.return_value = True
            qr.bounding_box.return_value = {"width": 180, "height": 180}
            other = AsyncMock()
            other.is_visible.return_value = False
            writer.page.locator.return_value.all = AsyncMock(return_value=[qr, other])
            self.assertIs(await writer.qr_element(), qr)
            other.is_visible.return_value = True
            other.bounding_box.return_value = {"width": 180, "height": 180}
            self.assertIsNone(await writer.qr_element())
            other.bounding_box.return_value = {"width": 900, "height": 900}
            self.assertIs(await writer.qr_element(), qr)

    async def test_existing_folder_is_reused_and_duplicates_rejected(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer._command = AsyncMock()
            writer.folders = AsyncMock(return_value=[{"collects_id": "1", "collects_name": "合成夹"}])
            self.assertEqual(await writer.ensure_folder("合成夹"), "1")
            with self.assertRaises(CollectionError):
                writer.match_folder([{"collects_id": "1", "collects_name": "合成夹"}, {"collects_id": "2", "collects_name": "合成夹"}], "合成夹")
            writer._command.assert_not_awaited()

    async def test_private_folder_is_created_then_confirmed_by_reading(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer._command = AsyncMock()
            writer.folders = AsyncMock(side_effect=[[], [], [{"collects_id": "90071992547409931", "collects_name": "合成夹"}]])
            with patch("douyin_rag.ego_writer.asyncio.sleep", new=AsyncMock()):
                self.assertEqual(await writer.ensure_folder("合成夹"), "90071992547409931")
            operation, params = writer._command.await_args.args
            self.assertEqual(operation, "create")
            self.assertEqual(params["action"], "1")
            self.assertEqual(params["collects_name"], "合成夹")
            self.assertEqual(params["secret"], "1")
            self.assertEqual(params["version_code"], "230000")
            self.assertEqual(writer.folders.await_count, 3)

    async def test_unconfirmed_create_resumes_by_querying_without_repeating_write(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer._command = AsyncMock(side_effect=CollectionError("合成请求超时", "write_unconfirmed"))
            writer.folders = AsyncMock(side_effect=[[], [{"collects_id": "1", "collects_name": "合成夹"}]])
            with self.assertRaises(CollectionError) as caught:
                await writer.ensure_folder("合成夹")
            self.assertEqual(caught.exception.code, "write_unconfirmed")
            self.assertEqual(await writer.ensure_folder("合成夹"), "1")
            self.assertEqual(writer._command.await_count, 1)

    async def test_move_is_verified_before_removing_original_membership(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer.members = AsyncMock(side_effect=[set(), {"9"}, {"9"}, set()])
            writer.move_request = AsyncMock()
            with patch("douyin_rag.ego_writer.asyncio.sleep", new=AsyncMock()):
                await writer.move_and_verify({"aweme_id": "9", "original_folders": ["old"]}, "new", "新夹")
            calls = writer.move_request.call_args_list
            self.assertEqual(calls[0].kwargs, {"target": "new", "name": "新夹"})
            self.assertEqual(calls[1].kwargs, {"source": "old"})

    async def test_unconfirmed_target_never_removes_old_membership(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer.members = AsyncMock(return_value=set())
            writer.move_request = AsyncMock()
            with patch("douyin_rag.ego_writer.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(CollectionError):
                    await writer.move_and_verify({"aweme_id": "9", "original_folders": ["old"]}, "new", "新夹")
            writer.move_request.assert_awaited_once_with("9", target="new", name="新夹")

    async def test_browser_request_uses_only_collection_move_and_reports_rejection(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer.read_cookies = "sessionid=synthetic-private"
            writer.marker = "synthetic-marker"
            with patch.dict(os.environ, {"DOUYIN_COOKIE": "sessionid=synthetic-private"}), \
                    patch("douyin_rag.ego_writer.subprocess.run") as run:
                run.return_value.stdout = b''
                run.return_value.stderr = b'SHICANG_EGO {"spaceId": 9, "handedOff": false}\r\n'
                await writer.move_request("9", target="123", name="合成夹")
                self.assertEqual(run.call_args.args[0], ["ego-browser", "nodejs"])
                source = run.call_args.kwargs["input"].decode("utf-8")
                payload = json.loads(source.splitlines()[0].removeprefix("const input = ").removesuffix(";"))
                self.assertEqual(payload["operation"], "move")
                self.assertEqual(payload["marker"], "synthetic-marker")
                self.assertEqual(payload["params"]["item_ids"], "9")
                self.assertEqual(payload["params"]["to_collects_id"], "123")
                self.assertIn("/aweme/v1/web/collects/video/move/", source)
                self.assertNotIn("synthetic-private", source)
                self.assertNotIn("DOUYIN_COOKIE", run.call_args.kwargs["env"])
                run.return_value.stderr = b'SHICANG_EGO {"spaceId": 9, "error": "write_rejected"}'
                with self.assertRaises(CollectionError) as caught:
                    await writer.move_request("9", target="123")
                self.assertEqual(caught.exception.code, "write_rejected")

    async def test_missing_cli_and_write_timeout_have_distinct_results(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            with patch("douyin_rag.ego_writer.subprocess.run", side_effect=FileNotFoundError):
                with self.assertRaises(CollectionError) as caught:
                    await writer.open("sessionid=synthetic", "marker")
                self.assertEqual(caught.exception.code, "browser_unavailable")
            with patch("douyin_rag.ego_writer.subprocess.run", side_effect=subprocess.TimeoutExpired("ego-browser", 35)):
                with self.assertRaises(CollectionError) as caught:
                    await writer.move_request("9", target="123")
                self.assertEqual(caught.exception.code, "write_unconfirmed")

    async def test_continue_takes_back_the_same_handed_off_space(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            with patch("douyin_rag.ego_writer.subprocess.run") as run:
                run.return_value.stdout = b'SHICANG_EGO {"spaceId": 12, "handedOff": true, "error": "login_required"}'
                run.return_value.stderr = b''
                with self.assertRaises(CollectionError) as caught:
                    await writer.open("sessionid=synthetic", "marker")
                self.assertEqual(caught.exception.code, "login_required")
                await writer.close()
                self.assertEqual(run.call_count, 1)
                run.return_value.stdout = b'SHICANG_EGO {"spaceId": 12, "handedOff": false}'
                await writer.open("sessionid=synthetic", "marker")
                source = run.call_args.kwargs["input"].decode("utf-8")
                self.assertIn('"spaceId": 12', source)
                self.assertIn('"takeOver": true', source)
                await writer.close()
                self.assertIsNone(writer.space_id)
