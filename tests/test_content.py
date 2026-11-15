from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock

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

    async def test_visual_collection_keeps_frames_when_no_speech_and_can_reuse_audio(self):
        with TemporaryDirectory() as root:
            transcriber = LocalTranscriber(root)
            client = Mock(get_video_detail=AsyncMock(return_value={"video": {"play_addr": {"url_list": ["https://v.douyinvod.com/one"]}}}))
            item = {"aweme_id": "123", "kind": "video", "duration": 5}
            frames = [{"time": 0., "image": b"synthetic-jpeg"}]
            with patch("douyin_rag.content.download_video", new=AsyncMock()), \
                    patch.object(transcriber, "frames", return_value=frames), \
                    patch.object(transcriber, "transcribe", side_effect=CollectionError("合成无语音", "no_speech")) as speech:
                evidence = await transcriber.extract(item, client, include_frames=True)
                self.assertEqual(evidence["frames"], frames)
                self.assertEqual(evidence["segments"], [])
                self.assertTrue(evidence["speech_error"])
                speech.reset_mock()
                await transcriber.extract(item, client, include_frames=True, transcribe_audio=False)
                speech.assert_not_called()
            self.assertFalse(list(Path(root).glob("transcribe-*")))


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

    def test_real_synthetic_video_produces_bounded_timestamped_jpeg_frames(self):
        import av
        from fractions import Fraction
        from io import BytesIO
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.mp4"
            with av.open(str(path), "w") as container:
                stream = container.add_stream("mpeg4", rate=4)
                stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
                for index in range(16):
                    frame = av.VideoFrame(64, 48, "rgb24")
                    frame.planes[0].update(bytes([index * 10]) * frame.planes[0].buffer_size)
                    frame.pts, frame.time_base = index, Fraction(1, 4)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode(None):
                    container.mux(packet)
            frames = LocalTranscriber.frames(path, 4)
            self.assertGreater(len(frames), 1)
            self.assertLessEqual(len(frames), 8)
            self.assertEqual([frame["time"] for frame in frames], sorted(set(frame["time"] for frame in frames)))
            for frame in frames:
                self.assertLessEqual(len(frame["image"]), 1024 * 1024)
                with av.open(BytesIO(frame["image"])) as image:
                    decoded = next(image.decode(video=0))
                    self.assertLessEqual(max(decoded.width, decoded.height), 1024)
