from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from douyin_rag.collections import CollectionError
from douyin_rag.writer import BrowserWriter


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
                await writer.open(headless=True)
                self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 1)
                await writer.open(headless=False)
                self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])
                self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 2)
                context.close.assert_awaited_once()
                await writer.close()

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
