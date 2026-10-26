from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

import httpx

from douyin_rag.collections import CollectionError, CollectionClient
from douyin_rag.content import download_video, media_candidates, media_url, LocalTranscriber


class ContentTest(IsolatedAsyncioTestCase):
    async def test_download_follows_only_allowed_redirects_and_sends_no_credentials(self):
        calls = []
        def reply(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(302, headers={"location": "https://media.douyinvod.com/clip.mp4"})
            return httpx.Response(200, content=b"synthetic-media")
        with TemporaryDirectory() as root:
            path = Path(root) / "clip.mp4"
            await download_video("https://www.douyin.com/media", path, transport=httpx.MockTransport(reply))
            self.assertEqual(path.read_bytes(), b"synthetic-media")
        self.assertTrue(all("cookie" not in r.headers and "authorization" not in r.headers for r in calls))

    async def test_redirect_to_private_host_and_large_response_are_rejected(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "clip.mp4"
            with self.assertRaises(CollectionError) as caught:
                await download_video("https://v.douyinvod.com/a", path, transport=httpx.MockTransport(
                    lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})))
            self.assertEqual(caught.exception.code, "unsafe_media_url")
            with patch("douyin_rag.content.MAX_BYTES", 4):
                with self.assertRaises(CollectionError) as caught:
                    await download_video("https://v.douyinvod.com/a", path, transport=httpx.MockTransport(
                        lambda r: httpx.Response(200, content=b"too-large")))
                self.assertEqual(caught.exception.code, "media_too_large")

    async def test_detail_validates_identity_and_does_not_accept_upstream_empty_result(self):
        client = CollectionClient("sessionid=synthetic")
        await client.close()
        from unittest.mock import AsyncMock
        client._request = AsyncMock(return_value={"status_code": 0, "aweme_detail": {"aweme_id": "999"}})
        with self.assertRaises(CollectionError):
            await client.get_video_detail("123")
        with self.assertRaises(ValueError):
            await client.get_video_detail("bad-id")
        self.assertEqual(client._request.await_count, 1)


class TranscriptionTest(TestCase):
    def test_url_validation_and_candidate_fallback(self):
        for value in ("file:///a", "https://douyinvod.com.evil.test/a", "https://user:pass@v.douyinvod.com/a", "https://v.douyinvod.com:9000/a"):
            with self.assertRaises(CollectionError):
                media_url(value)
        detail = {"video": {"play_addr": {"url_list": ["http://127.0.0.1", "https://v.douyinvod.com/one"]},
                            "download_addr": {"url_list": ["https://v.douyinvod.com/one", "https://v.douyinvod.com/two"]}}}
        self.assertEqual(len(media_candidates(detail)), 2)

    def test_timed_transcription_is_local_cached_and_rejects_no_speech(self):
        with TemporaryDirectory() as root:
            transcriber = LocalTranscriber(root)
            model = Mock()
            model.transcribe.return_value = (iter([Mock(start=1.2, end=3.4, text=" 合成内容 ")]), None)
            with patch("faster_whisper.WhisperModel", return_value=model) as factory, patch("faster_whisper.audio.decode_audio", return_value=[0.] * 16000):
                self.assertEqual(transcriber.transcribe("synthetic.wav"), [{"start": 1.2, "end": 3.4, "text": "合成内容"}])
                model.transcribe.return_value = (iter([]), None)
                with self.assertRaises(CollectionError) as caught:
                    transcriber.transcribe("synthetic.wav")
                self.assertEqual(caught.exception.code, "no_speech")
                factory.assert_called_once()
                self.assertEqual(factory.call_args.kwargs["device"], "cpu")
                self.assertEqual(factory.call_args.kwargs["compute_type"], "int8")
