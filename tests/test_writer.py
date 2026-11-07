from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from douyin_rag.collections import CollectionError
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
            with patch("douyin_rag.writer.CollectionClient", side_effect=AssertionError("Login must not read collections")) as factory:
                result = await writer.login_state()
                self.assertEqual(result["status"], "authenticated")
                self.assertIn("扫描时确认收藏能否读取", result["message"])
                # 手机确认后，即使页面二维码尚未消失，也不阻塞登录完成。
                writer.qr_element.return_value = qr
                self.assertEqual((await writer.login_state())["status"], "authenticated")
                factory.assert_not_called()
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
            writer = BrowserWriter(directory)
            writer.folders = AsyncMock(return_value=[{"collects_id": "1", "collects_name": "合成夹"}])
            self.assertEqual(await writer.ensure_folder("合成夹"), "1")
            with self.assertRaises(CollectionError):
                writer.match_folder([{"collects_id": "1", "collects_name": "合成夹"}, {"collects_id": "2", "collects_name": "合成夹"}], "合成夹")

    async def test_move_is_verified_before_removing_original_membership(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            writer.members = AsyncMock(side_effect=[set(), {"9"}, {"9"}, set()])
            writer.move_request = AsyncMock()
            with patch("douyin_rag.writer.asyncio.sleep", new=AsyncMock()):
                await writer.move_and_verify({"aweme_id": "9", "original_folders": ["old"]}, "new", "新夹")
            calls = writer.move_request.call_args_list
            self.assertEqual(calls[0].kwargs, {"target": "new", "name": "新夹"})
            self.assertEqual(calls[1].kwargs, {"source": "old"})

    async def test_unconfirmed_target_never_removes_old_membership(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            writer.members = AsyncMock(return_value=set())
            writer.move_request = AsyncMock()
            with patch("douyin_rag.writer.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(CollectionError):
                    await writer.move_and_verify({"aweme_id": "9", "original_folders": ["old"]}, "new", "新夹")
            writer.move_request.assert_awaited_once_with("9", target="new", name="新夹")

    async def test_browser_request_uses_only_collection_move_and_reports_rejection(self):
        with TemporaryDirectory() as directory:
            writer = BrowserWriter(directory)
            writer.page = AsyncMock()
            writer.page.evaluate.return_value = {"ok": True, "http": 200, "status_code": 0}
            await writer.move_request("9", target="123", name="合成夹")
            payload = writer.page.evaluate.call_args.args[1]
            self.assertEqual(payload["path"], "/aweme/v1/web/collects/video/move/")
            self.assertEqual(payload["params"]["item_ids"], "9")
            self.assertEqual(payload["params"]["to_collects_id"], "123")
            writer.page.evaluate.return_value = {"ok": False, "http": 403}
            with self.assertRaises(CollectionError):
                await writer.move_request("9", target="123")
