import json
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from douyin_rag.collections import CollectionError
from douyin_rag.organizer import Organizer, demo_data
from douyin_rag.rag import KnowledgeBase, chunks_from_segments, NO_EVIDENCE


class Embeddings:
    def encode(self, texts, query=False):
        return [[1., 0.] if any(w in text for w in ("番茄", "西红柿", "牛腩", "炖")) else
                [0., 1.] if "收纳" in text else [-1., -1.] for text in texts]


class RagTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.owner = Organizer(self.temp.name)
        snapshot, _ = demo_data()
        snapshot.update(demo=False, account_marker="synthetic-account")
        snapshot["items"] = snapshot["items"][:3]
        self.owner.data["snapshot"] = snapshot
        self.kb = KnowledgeBase(self.owner, embeddings=Embeddings())
        self.owner.knowledge = self.kb
        self.owner.model_json = AsyncMock(return_value={"answer": "先炒番茄，再加入牛腩炖煮。[1]", "citations": ["1"]})
        self.add(snapshot["items"][2], "番茄牛腩先炒番茄至出汁，再加牛腩，小火炖煮。", [1., 0.])
        self.add(snapshot["items"][1], "收纳按照使用频率安排位置。", [0., 1.])

    def tearDown(self):
        self.temp.cleanup()

    def add(self, item, text, vector):
        segments = [{"start": 10., "end": 28., "text": text}]
        self.kb.save_document(self.kb.account(), item, "ready", segments, chunks_from_segments(segments), [vector])

    async def test_semantic_answer_citations_followup_and_persistence(self):
        conversation = await self.kb.ask("西红柿炖牛肉怎么做？")
        self.assertEqual(conversation["messages"][-1]["sources"][0]["start"], 10.)
        self.assertIn("/video/", conversation["messages"][-1]["sources"][0]["url"])
        again = await self.kb.ask("那具体怎么操作？", conversation["id"])
        self.assertEqual(len(again["messages"]), 4)
        sent = json.loads(self.owner.model_json.call_args.args[0][-1]["content"])
        self.assertEqual(sent["previous_questions"], ["西红柿炖牛肉怎么做？"])
        restored = KnowledgeBase(Organizer(self.temp.name), embeddings=Embeddings())
        restored.owner.data = self.owner.data
        self.assertEqual(restored.session(conversation["id"])["messages"], again["messages"])

    async def test_unrelated_question_does_not_call_model_or_reuse_previous_topic(self):
        conversation = await self.kb.ask("番茄牛腩怎么做？")
        self.owner.model_json.reset_mock()
        result = await self.kb.ask("火星轨道是什么？", conversation["id"])
        self.assertEqual(result["messages"][-1]["content"], NO_EVIDENCE)
        self.owner.model_json.assert_not_awaited()

    async def test_invalid_citations_and_model_failure_do_not_save_round(self):
        for response in ({"answer": "编造内容 [99]", "citations": ["99"]}, {"answer": "无标注", "citations": ["1"]}):
            self.owner.model_json.return_value = response
            with self.assertRaises(CollectionError):
                await self.kb.ask("番茄牛腩怎么做？")
        self.assertEqual(self.kb.status()["sessions"], [])

    async def test_empty_citations_force_no_evidence_response(self):
        self.owner.model_json.return_value = {"answer": "模型擅自给出的常识回答", "citations": []}
        result = await self.kb.ask("番茄牛腩怎么做？")
        self.assertEqual(result["messages"][-1]["content"], NO_EVIDENCE)
        self.assertEqual(result["messages"][-1]["sources"], [])

    async def test_snapshot_switch_during_model_request_discards_result(self):
        async def switch_snapshot(messages):
            self.owner.data["snapshot"] = {**self.owner.data["snapshot"], "id": "new-snapshot"}
            return {"answer": "合成回答 [1]", "citations": ["1"]}
        self.owner.model_json.side_effect = switch_snapshot
        with self.assertRaises(CollectionError) as caught:
            await self.kb.ask("番茄牛腩怎么做？")
        self.assertEqual(caught.exception.code, "snapshot_changed")
        self.assertEqual(self.kb.status()["sessions"], [])

    async def test_invalid_vector_batch_rolls_back_previous_complete_document(self):
        item = self.owner.data["snapshot"]["items"][2]
        segments = [{"start": 0., "end": 2., "text": "无效新内容"}]
        with self.assertRaises(CollectionError):
            self.kb.save_document(self.kb.account(), item, "ready", segments, chunks_from_segments(segments), [])
        sources = self.kb.retrieve("番茄牛腩", self.kb.snapshot())
        self.assertIn("先炒番茄", sources[0]["text"])

    async def test_removed_items_account_and_model_changes_never_retrieve_old_content(self):
        conversation = await self.kb.ask("番茄牛腩怎么做？")
        self.owner.data["snapshot"]["items"].pop()
        self.assertEqual(self.kb.retrieve("番茄牛腩", self.kb.snapshot()), [])
        self.owner.settings["asr_model"] = "base"
        self.assertEqual(self.kb.status()["ready"], 0)
        self.owner.data["snapshot"]["account_marker"] = "other-account"
        with self.assertRaises(CollectionError):
            self.kb.session(conversation["id"])

    async def test_demo_never_loads_models_or_calls_cloud(self):
        self.owner.use_demo()
        self.kb.embeddings.encode = lambda *args, **kwargs: self.fail("Demo must not load embeddings")
        result = await self.kb.ask("番茄牛腩怎么做？")
        self.assertIn("合成示例", result["messages"][-1]["content"])
        self.assertTrue(result["messages"][-1]["sources"])
        self.assertEqual(result["messages"][-1]["sources"][0]["url"], "")
        self.owner.model_json.assert_not_awaited()

    async def test_index_incremental_retry_and_account_guard(self):
        self.owner.cookies = lambda: "sessionid=synthetic"
        self.owner.data["snapshot"]["account_marker"] = self.owner.account_marker(self.owner.cookies())
        class Client:
            async def __aenter__(inner): return inner
            async def __aexit__(inner, *args): pass
        self.owner.client_factory = lambda cookies: Client()
        self.kb.transcriber = AsyncMock()
        self.kb.transcriber.extract.side_effect = [CollectionError("合成不可读作品", "content_unavailable"),
            [{"start": 0., "end": 8., "text": "收纳方法"}], [{"start": 0., "end": 8., "text": "番茄炖牛腩"}]]
        self.owner.job = {"processed": 0, "total": None}
        with patch("douyin_rag.rag.asyncio.sleep", new=AsyncMock()):
            await self.kb.index()
        self.assertEqual(self.kb.status()["ready"], 2)
        self.assertEqual(self.kb.status()["failed"], 1)
        self.kb.transcriber.extract.reset_mock(side_effect=True)
        self.kb.transcriber.extract.return_value = [{"start": 0., "end": 6., "text": "浏览器工具"}]
        with patch("douyin_rag.rag.asyncio.sleep", new=AsyncMock()):
            await self.kb.index()
        self.kb.transcriber.extract.assert_awaited_once()
        self.assertEqual(self.kb.status()["ready"], 3)
        self.owner.data["snapshot"]["account_marker"] = "changed"
        with self.assertRaises(CollectionError):
            await self.kb.index()

    async def test_note_index_uses_local_ocr_and_note_url(self):
        self.owner.cookies = lambda: "sessionid=synthetic-note"
        self.owner.data["snapshot"]["account_marker"] = self.owner.account_marker(self.owner.cookies())
        note = {"aweme_id": "note1", "title": "图文标题", "author": "图文作者", "kind": "note",
                "image_urls": ["https://p3-sign.douyinpic.com/one.jpg"],
                "url": "https://www.douyin.com/note/note1"}
        self.owner.data["snapshot"]["items"].append(note)
        class Client:
            async def __aenter__(inner): return inner
            async def __aexit__(inner, *args): pass
        self.owner.client_factory = lambda cookies: Client()
        self.kb.transcriber = Mock(extract_note=AsyncMock(return_value={
            "images": [{"time": 0., "image": b"synthetic-jpeg"}], "text": "图片文字内容", "error": ""}))
        self.owner.job = {"processed": 0, "total": None}
        with patch("douyin_rag.rag.asyncio.sleep", new=AsyncMock()):
            await self.kb.index(item_ids=["note1"])
        document = self.kb.document(note)
        self.assertEqual(document["status"], "ready")
        self.assertIn("图文标题", document["segments"][0]["text"])
        self.assertIn("图片文字内容", document["segments"][0]["text"])
        self.assertEqual(self.kb.status()["unsupported"], 0)
        sources = self.kb.retrieve("图片文字内容", self.kb.snapshot())
        self.assertTrue(sources)
        self.assertEqual(sources[0]["url"], "https://www.douyin.com/note/note1")
        self.assertEqual(sources[0]["kind"], "note")
        visual = {}
        self.kb.transcriber.extract_note.reset_mock()
        with patch("douyin_rag.rag.asyncio.sleep", new=AsyncMock()):
            await self.kb.index(item_ids=["note1"], visual=visual)
        self.assertEqual(visual["note1"]["frames"][0]["image"], b"synthetic-jpeg")
        self.assertFalse(self.kb.transcriber.extract_note.call_args.kwargs["recognize"])
        self.assertTrue(self.kb.transcriber.extract_note.call_args.kwargs["preview"])

    async def test_note_fingerprint_does_not_change_with_speech_model(self):
        note = {"aweme_id": "note1", "title": "图文标题", "kind": "note", "duration": 0}
        before = self.kb.fingerprint(note)
        self.owner.settings["asr_model"] = "base"
        self.assertEqual(self.kb.fingerprint(note), before)

    async def test_failed_frame_download_does_not_replace_cached_speech_with_failure(self):
        self.owner.cookies = lambda: "sessionid=synthetic"
        self.owner.data["snapshot"]["account_marker"] = self.owner.account_marker(self.owner.cookies())
        item = self.owner.data["snapshot"]["items"][2]
        self.add(item, "已缓存的语音正文", [1., 0.])
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        self.owner.client_factory = lambda cookies: Client()
        self.owner.job = {"processed": 0, "total": None}
        self.kb.transcriber = AsyncMock()
        self.kb.transcriber.extract.side_effect = CollectionError("合成画面下载失败", "media_download_failed")
        visual = {}
        with patch("douyin_rag.rag.asyncio.sleep", new=AsyncMock()):
            await self.kb.index(item_ids=[item["aweme_id"]], visual=visual)
        self.assertEqual(self.kb.document(item)["status"], "ready")
        self.assertEqual(self.kb.document(item)["segments"][0]["text"], "已缓存的语音正文")
        self.assertFalse(self.kb.transcriber.extract.call_args.kwargs["transcribe_audio"])
        self.assertEqual(visual[item["aweme_id"]]["frame_error"], "合成画面下载失败")
        self.kb.transcriber.extract.side_effect = RuntimeError("合成本机解码器不可用")
        with patch("douyin_rag.rag.asyncio.sleep", new=AsyncMock()):
            await self.kb.index(item_ids=[item["aweme_id"]], visual=visual)
        self.assertEqual(self.kb.document(item)["status"], "ready")
        self.assertEqual(self.kb.document(item)["segments"][0]["text"], "已缓存的语音正文")


class ChunksTest(TestCase):
    def test_chunking_preserves_content_and_time_boundaries(self):
        segments = [{"start": i*10, "end": (i+1)*10, "text": str(i)*200} for i in range(4)]
        chunks = chunks_from_segments(segments)
        self.assertTrue(all(len(row["text"]) <= 450 for row in chunks))
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[-1]["end"], 40)
        for segment in segments:
            self.assertTrue(any(segment["text"] in row["text"] for row in chunks))
