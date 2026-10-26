import json
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx

from douyin_rag.collections import CollectionClient, CollectionError, normalize_page
from douyin_rag.signing import get_a_bogus, sign_collection_url
from douyin_rag.verification import VerifyParams, DOUYIN_FIXED_USER_AGENT


class CollectionsTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = CollectionClient("sessionid=synthetic; UIFID=identity; s_v_web_id=verify-cookie")
        await self.client.close()
        self.client.verify_params = VerifyParams(ms_token="token", webid="web", verify_fp="generated", s_v_web_id="generated")
        self.client._get_sign = Mock(return_value="bogus")

    async def asyncTearDown(self):
        await self.client.close()

    async def test_collection_queries_large_ids_and_signed_post_bytes(self):
        requests = []
        def reply(request):
            requests.append(request)
            key = "collects_list" if request.url.path.endswith("collects/list/") else "aweme_list"
            return httpx.Response(200, json={"status_code": 0, key: [], "cursor": 11, "has_more": 0})
        self.client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        await self.client.get_collection_folders(cursor=7, count=3)
        await self.client.get_collection_videos("9000000000000000001", cursor=11, count=2)
        await self.client.get_collected_videos(cursor=13, count=4)
        self.assertEqual(parse_qs(requests[0].url.query.decode())["cursor"], ["7"])
        self.assertEqual(parse_qs(requests[1].url.query.decode())["collects_id"], ["9000000000000000001"])
        self.assertEqual(requests[0].headers["uifid"], "identity")
        self.assertEqual(parse_qs(requests[0].url.query.decode())["verifyFp"], ["verify-cookie"])
        self.assertEqual(requests[-1].method, "POST")
        self.assertEqual(json.loads(requests[-1].content)["cursor"], 13)
        self.assertEqual(self.client._get_sign.call_args.args[1].encode(), requests[-1].content)

    async def test_invalid_input_makes_no_requests(self):
        self.client._request = Mock(side_effect=AssertionError("Must not request"))
        for kwargs in ({"cursor": -1}, {"count": 0}, {"count": 51}):
            with self.assertRaises(ValueError):
                await self.client.get_collection_folders(**kwargs)
        with self.assertRaises(ValueError):
            await self.client.get_collection_videos(" ")

    async def test_upstream_rejection_is_not_empty_success_or_raw_body(self):
        for status, payload in ((200, {"status_code": 1}), (200, {"status_code": 0}), (403, "ArgusSecurityPlugin Signature Not Found private-payload")):
            def reply(request):
                return httpx.Response(status, json=payload) if isinstance(payload, dict) else httpx.Response(status, text=payload)
            self.client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
            with self.assertRaises(CollectionError) as caught:
                await self.client.get_collection_folders()
            self.assertNotIn("private-payload", str(caught.exception))
            await self.client.close()


class ParseAndSignTest(TestCase):
    def test_image_post_hidden_metadata_and_large_string_id(self):
        page = normalize_page({"aweme_list": [{"aweme_id": "9000000000000000001", "desc": "合成图文", "aweme_type": 68, "author": None, "statistics": None, "images": [{"url_list": ["https://example.test/photo.jpg"]}]}], "cursor": 8, "has_more": 1, "invalid_item_count": 12, "invalid_item_text": "合成不可读提示"})
        self.assertEqual(page["videos"][0]["kind"], "note")
        self.assertEqual(page["videos"][0]["cover_url"], "https://example.test/photo.jpg")
        self.assertTrue(page["videos"][0]["url"].endswith("/note/9000000000000000001"))
        self.assertEqual(page["invalid_item_count"], 12)
        self.assertTrue(page["has_more"])

    def test_cookie_requires_session(self):
        with self.assertRaises(CollectionError):
            CollectionClient("ttwid=synthetic")

    def test_secsdk_known_vector(self):
        signed = sign_collection_url("https://www.douyin.com/aweme/v1/web/collects/list/?cursor=0&count=2&word=%E4%B8%AD%E6%96%87+%21", "test-identity", timestamp=1700000000)
        self.assertTrue(signed.endswith("x-secsdk-web-signature=3df4b5990bc97d8717950dc4d9799f62"))
        self.assertEqual(sign_collection_url(signed, "test-identity", timestamp=1700000000), signed)

    def test_extracted_runtime_actually_signs_multiple_independent_requests(self):
        for query in ("aid=6383&count=1", "aid=6383&cursor=2"):
            signature = get_a_bogus(query, "", DOUYIN_FIXED_USER_AGENT)
            self.assertIsInstance(signature, str)
            self.assertGreater(len(signature), 20)
