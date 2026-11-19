from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock

import httpx

from douyin_rag.collections import CollectionError, CollectionClient, extract_image_urls
from douyin_rag.content import download_image, download_video, media_candidates, media_url, LocalOCR, LocalTranscriber


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

    async def test_image_download_accepts_douyinpic_and_rejects_private_or_non_image(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "photo.jpg"
            await download_image("https://p3-sign.douyinpic.com/photo.jpg", path, transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"synthetic-image", headers={"content-type": "image/jpeg"})))
            self.assertEqual(path.read_bytes(), b"synthetic-image")
            with self.assertRaises(CollectionError) as caught:
                await download_image("https://p3-sign.douyinpic.com/photo.jpg", path, transport=httpx.MockTransport(
                    lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})))
            self.assertEqual(caught.exception.code, "unsafe_media_url")
            with self.assertRaises(CollectionError) as caught:
                await download_image("https://p3-sign.douyinpic.com/photo.jpg", path, transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})))
            self.assertEqual(caught.exception.code, "image_download_failed")

    async def test_note_extraction_prefers_detail_images_and_orders_ocr_text(self):
        with TemporaryDirectory() as root:
            transcriber = LocalTranscriber(root)
            client = Mock(get_video_detail=AsyncMock(return_value={"images": [
                {"url_list": ["https://p3-sign.douyinpic.com/one.jpg"]},
                {"url_list": ["https://p3-sign.douyinpic.com/two.jpg"]}]}))
            item = {"aweme_id": "123", "kind": "note", "title": "原文案",
                    "image_urls": ["https://p3.byteimg.com/stale.jpg"]}
            transcriber.ocr.ensure_loaded = Mock()
            transcriber.ocr.recognize = Mock(side_effect=["第二张文字", "第一张文字"])
            transcriber._image_preview = Mock(return_value=b"synthetic-jpeg")
            with patch("douyin_rag.content.download_image", new=AsyncMock()):
                evidence = await transcriber.extract_note(item, client)
            self.assertEqual(evidence["text"], "第二张文字\n第一张文字")
            self.assertEqual(len(evidence["images"]), 2)
            self.assertEqual(evidence["images"][0]["image"], b"synthetic-jpeg")
            client.get_video_detail.assert_awaited_once_with("123")
            self.assertFalse(evidence["error"])

    async def test_note_extraction_without_images_fails_without_inventing_text(self):
        with TemporaryDirectory() as root:
            transcriber = LocalTranscriber(root)
            client = Mock(get_video_detail=AsyncMock(return_value={}))
            item = {"aweme_id": "123", "kind": "note", "title": "只有标题", "image_urls": []}
            with self.assertRaises(CollectionError) as caught:
                await transcriber.extract_note(item, client)
            self.assertEqual(caught.exception.code, "content_unavailable")

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
    def test_local_ocr_sorts_lines_by_position_and_ignores_empty_results(self):
        with TemporaryDirectory() as root:
            ocr = LocalOCR(root)
            ocr.ensure_loaded = Mock()
            ocr.engine = Mock(return_value=SimpleNamespace(
                txts=("下", "上"),
                boxes=[[[0, 10], [1, 10], [1, 20], [0, 20]], [[0, 0], [1, 0], [1, 5], [0, 5]]]))
            self.assertEqual(ocr.recognize("synthetic"), "上\n下")
            ocr.engine.return_value = SimpleNamespace(txts=None, boxes=None)
            self.assertEqual(ocr.recognize("synthetic"), "")

    def test_image_url_structure_variants_are_supported(self):
        self.assertEqual(extract_image_urls({"images": [{"url_list": ["https://one"]}]}), ["https://one"])
        self.assertEqual(extract_image_urls({"image_post_info": {"images": [
            {"display_image": {"url_list": ["https://two"]}}]}}), ["https://two"])
        self.assertEqual(extract_image_urls({}), [])

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
