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

    async def test_verification_pagination_follows_cursor_without_fixed_wait(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            fetch = AsyncMock(side_effect=[
                {"cursor": 9, "has_more": True, "aweme_list": [{"aweme_id": "1"}]},
                {"cursor": 0, "has_more": False, "aweme_list": [{"aweme_id": "2"}]},
            ])
            with patch("douyin_rag.ego_writer.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await writer.all_pages(fetch, "aweme_list")
            self.assertEqual([row["aweme_id"] for row in result], ["1", "2"])
            self.assertEqual([entry.kwargs for entry in fetch.await_args_list], [
                {"cursor": 0, "count": 50}, {"cursor": 9, "count": 50},
            ])
            sleep.assert_not_awaited()

    async def test_verification_rejects_missing_and_repeated_cursors(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            for page in ({"has_more": True}, {"cursor": 0, "has_more": True}):
                with self.subTest(page=page):
                    fetch = AsyncMock(return_value=page)
                    with self.assertRaises(CollectionError) as caught:
                        await writer.all_pages(fetch, "aweme_list")
                    self.assertEqual(caught.exception.code, "verification_failed")
                    fetch.assert_awaited_once()

    async def test_browser_request_uses_only_collection_move_and_reports_rejection(self):
        with TemporaryDirectory() as directory:
            writer = EgoWriter(directory)
            writer.read_cookies = "sessionid=synthetic-private"
            writer.session_marker = "synthetic-marker"
            with patch.dict(os.environ, {"DOUYIN_COOKIE": "sessionid=synthetic-private"}), \
                    patch("douyin_rag.ego_writer.subprocess.run") as run:
                run.return_value.stdout = b''
                run.return_value.stderr = b'TROVE_EGO {"spaceId": 9, "handedOff": false}\r\n'
                item_ids = ["90071992547409931", "90071992547409932"]
                await writer.move_request(item_ids, target="123", name="合成夹")
                self.assertEqual(run.call_args.args[0], ["ego-browser", "nodejs"])
                source = run.call_args.kwargs["input"].decode("utf-8")
                payload = json.loads(source.splitlines()[0].removeprefix("const input = ").removesuffix(";"))
                self.assertEqual(payload["operation"], "move")
                self.assertEqual(payload["sessionMarker"], "synthetic-marker")
                self.assertEqual(payload["params"]["item_ids"], item_ids)
                self.assertEqual(payload["params"]["to_collects_id"], "123")
                self.assertIn("/aweme/v1/web/collects/video/move/", source)
                self.assertNotIn("synthetic-private", source)
                self.assertNotIn("DOUYIN_COOKIE", run.call_args.kwargs["env"])
                run.return_value.stderr = b'TROVE_EGO {"spaceId": 9, "error": "write_rejected", "platformStatus": 42, "raw": "private-payload"}'
                with self.assertRaises(CollectionError) as caught:
                    await writer.move_request(item_ids, target="123")
                self.assertEqual(caught.exception.code, "write_rejected")
                self.assertIn("42", str(caught.exception))
                self.assertNotIn("private-payload", str(caught.exception))
                run.return_value.stderr = b'TROVE_EGO {"spaceId": 9, "error": "browser_attention", "httpStatus": 429, "handedOff": true}'
                with self.assertRaises(CollectionError) as caught:
                    await writer.move_request(item_ids, target="123")
                self.assertEqual(caught.exception.code, "browser_attention")
                self.assertIn("HTTP 429", str(caught.exception))
                self.assertTrue(writer.handed_off)

    async def test_missing_cli_and_write_timeout_have_distinct_results(self):
        with TemporaryDirectory() as directory:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.own_account_marker.return_value = "a" * 64
            writer = EgoWriter(directory, client_factory=Mock(return_value=client))
            with patch("douyin_rag.ego_writer.subprocess.run", side_effect=FileNotFoundError):
                with self.assertRaises(CollectionError) as caught:
                    await writer.open("sessionid=synthetic")
                self.assertEqual(caught.exception.code, "browser_unavailable")
            with patch("douyin_rag.ego_writer.subprocess.run", side_effect=subprocess.TimeoutExpired("ego-browser", 35)):
                with self.assertRaises(CollectionError) as caught:
                    await writer.move_request(["9"], target="123")
                self.assertEqual(caught.exception.code, "write_unconfirmed")

    async def test_continue_takes_back_the_same_handed_off_space(self):
        with TemporaryDirectory() as directory:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.own_account_marker.return_value = "a" * 64
            writer = EgoWriter(directory, client_factory=Mock(return_value=client))
            with patch("douyin_rag.ego_writer.subprocess.run") as run:
                run.return_value.stdout = b'TROVE_EGO {"spaceId": 12, "handedOff": true, "error": "login_required"}'
                run.return_value.stderr = b''
                with self.assertRaises(CollectionError) as caught:
                    await writer.open("sessionid=synthetic")
                self.assertEqual(caught.exception.code, "login_required")
                await writer.close()
                self.assertEqual(run.call_count, 1)
                result = {"spaceId": 12, "handedOff": False, "sessionMarker": "b" * 64}
                run.return_value.stdout = b'TROVE_EGO ' + json.dumps(result).encode()
                await writer.open("sessionid=synthetic")
                source = run.call_args.kwargs["input"].decode("utf-8")
                self.assertIn('"spaceId": 12', source)
                self.assertIn('"takeOver": true', source)
                self.assertIn('"accountMarker": "' + "a" * 64 + '"', source)
                self.assertNotIn("sessionid=synthetic", source)
                self.assertEqual(writer.session_marker, "b" * 64)
                await writer.close()
                self.assertIsNone(writer.space_id)

    async def test_unconfirmed_ego_identity_clears_previous_write_binding(self):
        with TemporaryDirectory() as directory:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.own_account_marker.return_value = "a" * 64
            writer = EgoWriter(directory, client_factory=Mock(return_value=client))
            writer.read_cookies = "sessionid=old-synthetic"
            writer.session_marker = "b" * 64
            writer._command = AsyncMock(return_value={})
            with self.assertRaises(CollectionError) as caught:
                await writer.open("sessionid=new-synthetic")
            self.assertEqual(caught.exception.code, "account_unconfirmed")
            self.assertEqual(writer.session_marker, "")
            self.assertEqual(writer.read_cookies, "")
