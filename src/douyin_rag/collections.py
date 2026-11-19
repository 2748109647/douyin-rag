"""从 douyin-mcp 按调用链提取的收藏读取客户端；无搜索和互动写入。"""
import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any
import httpx
from .verification import VerifyParams, get_common_verify_params, DOUYIN_FIXED_USER_AGENT
from .signing import get_a_bogus, SignError

class CollectionError(Exception):
    def __init__(self, message, code='collection_error'):
        self.code = code
        super().__init__(message)

DataFetchError = CollectionError

SELF_PROFILE_PATH = "/aweme/v1/web/user/profile/self/"
SEC_UID_PATTERN = r"MS4wLjAB[A-Za-z0-9_-]+"

COOKIE_ATTRIBUTE_KEYS = {
    "path",
    "domain",
    "expires",
    "max-age",
    "samesite",
    "priority",
    "partitioned",
}

@dataclass(frozen=True)
class NormalizedCookie:
    """Normalized cookie parsing result."""

    value: str
    invalid_parts: int
    has_session: bool

def normalize_cookie_string(raw: str) -> NormalizedCookie:
    """Normalize a raw Cookie string into request-ready `key=value` pairs only."""
    if not raw:
        return NormalizedCookie(value="", invalid_parts=0, has_session=False)

    cleaned = raw.replace("\n", "").replace("\r", "").strip()
    if not cleaned:
        return NormalizedCookie(value="", invalid_parts=0, has_session=False)

    cookie_map: dict[str, str] = {}
    invalid_parts = 0

    for part in cleaned.split(";"):
        segment = part.strip()
        if not segment:
            continue
        if "=" not in segment:
            invalid_parts += 1
            continue

        key, value = segment.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or any(ch.isspace() for ch in key):
            invalid_parts += 1
            continue
        if key.lower() in COOKIE_ATTRIBUTE_KEYS:
            invalid_parts += 1
            continue

        cookie_map[key] = value

    value = "; ".join(f"{key}={val}" for key, val in cookie_map.items())
    has_session = any(key in cookie_map for key in ("sessionid", "sessionid_ss"))
    return NormalizedCookie(
        value=value,
        invalid_parts=invalid_parts,
        has_session=has_session,
    )

def cookie_file_path(configured=''):
    if configured or os.getenv('DOUYIN_COOKIE_PATH'):
        return Path(configured or os.environ['DOUYIN_COOKIE_PATH']).expanduser()
    return Path(os.getenv('XDG_CONFIG_HOME') or Path.home() / '.config') / 'douyinmcp' / 'cookies.txt'

def load_cookies(configured=''):
    raw = os.getenv('DOUYIN_COOKIE', '').strip()
    if not raw:
        path = cookie_file_path(configured)
        try:
            raw = path.read_text(encoding='utf-8-sig') if path.is_file() else ''
        except OSError as exc:
            raise CollectionError('无法读取指定的登录状态文件', 'login_required') from exc
    result = normalize_cookie_string(raw)
    if not result.has_session:
        raise CollectionError('尚未连接抖音。请在连接设置中选择有效登录状态文件，或打开登录窗口。', 'login_required')
    return result.value

class CollectionClient:
    def __init__(self, cookies: str, proxy: Optional[str] = None):
        normalized = normalize_cookie_string(cookies)
        if not normalized.has_session:
            raise CollectionError('登录状态缺少有效会话', 'login_required')
        self.cookies = normalized.value
        self.proxy = proxy
        self.user_agent = DOUYIN_FIXED_USER_AGENT
        self.verify_params = None
        self._cookie_ms_token = self._extract_ms_token_from_cookie(self.cookies)
        self._http_client = httpx.AsyncClient(proxy=proxy, timeout=30)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    BASE_URL = "https://www.douyin.com"

    COMMON_PARAMS = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "publish_video_strategy_type": "2",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "2560",
        "screen_height": "1440",
        "browser_language": "zh-CN",
        "browser_platform": "MacIntel",
        "browser_name": "Chrome",
        "browser_version": "135.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "135.0.0.0",
        "os_name": "Mac+OS",
        "os_version": "10.15.7",
        "cpu_core_num": "8",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "4.45",
        "effective_type": "4g",
        "round_trip_time": "100",
    }

    @staticmethod
    def _extract_ms_token_from_cookie(cookie_str: str) -> str:
        """从 Cookie 字符串中提取 msToken。"""
        if not cookie_str:
            return ""
        for part in cookie_str.split(";"):
            part = part.strip()
            if part.startswith("msToken="):
                token = part.split("=", 1)[1]
                if len(token) > 50:  # 有效的msToken至少50+字符
                    return token
        return ""

    async def close(self):
        """关闭 HTTP 客户端，释放连接池资源。"""
        if self._http_client:
            await self._http_client.aclose()

    async def _init_verify_params(self) -> None:
        """Initialize verification parameters."""
        if self.verify_params is None:
            self.verify_params = await get_common_verify_params(self.user_agent)

    def _get_headers(self, is_post: bool = False, remove_origin: bool = False) -> Dict[str, str]:
        """Get request headers."""
        headers = {
            "Accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "Cookie": self.cookies,
            "referer": "https://www.douyin.com/discover" if is_post else "https://www.douyin.com/user/self",
            "user-agent": self.user_agent,
        }

        if is_post:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["X-Secsdk-Csrf-Token"] = "DOWNGRADE"
        else:
            headers["Content-Type"] = "application/json"

        if not remove_origin and not is_post:
            headers["origin"] = "https://www.douyin.com"

        return headers

    def _get_sign(self, query_params: str, post_data: str = "") -> str:
        """
        Get a_bogus signature using local JavaScript signer.

        Args:
            query_params: URL encoded query parameters
            post_data: POST request body (empty for GET)

        Returns:
            a_bogus signature string
        """
        try:
            return get_a_bogus(query_params, post_data, self.user_agent)
        except SignError as e:
            raise DataFetchError(f"Signing failed: {e}")

    async def _request(
        self,
        method: str,
        uri: str,
        params: Dict[str, Any],
        need_sign: bool = True,
        headers: Optional[Dict[str, str]] = None,
        post_data: str = "",
        json_data: Optional[Dict[str, Any]] = None,
        web_uifid: str = "",
    ) -> Dict[str, Any]:
        """
        Make API request.

        Args:
            method: HTTP method (GET/POST)
            uri: API endpoint URI
            params: Query parameters
            need_sign: Whether to add a_bogus signature
            headers: Optional custom headers
            post_data: POST request body
            json_data: JSON request body (serialized once for signing and sending)
            web_uifid: Optional identity for collection SecSDK signing

        Returns:
            API response as dict
        """
        if json_data is not None:
            if post_data:
                raise ValueError("post_data and json_data cannot be used together")
            post_data = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))
        await self._init_verify_params()

        # Merge common params and verify params
        # msToken 优先级：Cookie真实token > 首页Set-Cookie > API生成/fake
        ms_token = self._cookie_ms_token or self.verify_params.ms_token
        all_params = {
            **self.COMMON_PARAMS,
            **params,
            "webid": self.verify_params.webid,
            "msToken": ms_token,
        }

        query_string = urllib.parse.urlencode(all_params)

        # Add signature if needed
        if need_sign:
            a_bogus = self._get_sign(query_string, post_data)
            all_params["a_bogus"] = a_bogus
            query_string = urllib.parse.urlencode(all_params)

        url = f"{self.BASE_URL}{uri}?{query_string}"
        # Core Web endpoints require UIFID and SecSDK signing in addition to a_bogus.
        if need_sign and uri.startswith("/aweme/v1/web/") and not web_uifid:
            cookie_values = dict(
                part.strip().split("=", 1)
                for part in self.cookies.split(";") if "=" in part
            )
            web_uifid = cookie_values.get("UIFID") or cookie_values.get("UIFID_TEMP", "")
        if web_uifid:
            from .signing import sign_collection_url
            url = sign_collection_url(url, web_uifid)

        if headers is None:
            headers = self._get_headers(is_post=(method.upper() == "POST"))
        if web_uifid:
            headers = {**headers, "uifid": web_uifid}
        if json_data is not None:
            headers = {**headers, "Content-Type": "application/json; charset=UTF-8"}

        # 复用持久化 HTTP 客户端连接池
        if method.upper() == "GET":
            response = await self._http_client.get(url, headers=headers)
        else:
            response = await self._http_client.post(
                url, headers=headers, content=post_data.encode("utf-8")
            )

        if response.status_code == 403 and "ArgusSecurityPlugin" in response.text:
            reason = next((name for name in (
                "Uifid Not Found", "Signature Not Found", "Signature Verify Failed",
            ) if name in response.text), "Argus 拒绝")
            raise DataFetchError(
                f"抖音拒绝了网页安全签名（{reason}），请检查 UIFID 请求头和 SecSDK 签名"
            )
        if response.status_code >= 400:
            raise DataFetchError(f"接口请求失败（HTTP {response.status_code}）")
        if response.text == "" or response.text == "blocked":
            raise DataFetchError("Request blocked or empty response")

        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise DataFetchError(f"Failed to parse response: {e}")

    async def own_account_marker(self) -> str:
        """Return a digest of the account identified by the current login."""
        try:
            result = await self._request("GET", SELF_PROFILE_PATH, {})
        except (CollectionError, httpx.HTTPError):
            result = None
        if isinstance(result, dict) and type(result.get("status_code")) is int and result["status_code"] == 0:
            user = result.get("user")
            sec_uid = user.get("sec_uid") if isinstance(user, dict) else None
            if isinstance(sec_uid, str) and re.fullmatch(SEC_UID_PATTERN, sec_uid):
                return hashlib.sha256(sec_uid.encode()).hexdigest()
        raise CollectionError("暂时无法确认本机扫描会话的抖音账号。请检查连接和登录状态后继续整理。", "account_unconfirmed")

    async def _collection_request(
        self, uri: str, cursor: int, count: int,
        collects_id: Optional[str] = None, method: str = "GET",
    ) -> Dict[str, Any]:
        """Read one collection page using the existing Cookie and signer."""
        if cursor < 0 or not 1 <= count <= 50:
            raise ValueError("cursor 必须 >= 0，count 必须在 1 到 50 之间")
        if collects_id is not None and not collects_id.strip():
            raise ValueError("collects_id 不能为空，请先获取收藏夹列表")
        await self._init_verify_params()
        params = {
            "cursor": cursor, "count": count,
            "verifyFp": self.verify_params.verify_fp,
            "fp": self.verify_params.verify_fp,
        }
        if collects_id is not None:
            params["collects_id"] = collects_id.strip()
        headers = self._get_headers(is_post=(method == "POST"))
        headers["referer"] = "https://www.douyin.com/user/self?showTab=favorite_collection"
        cookie_values = dict(
            part.strip().split("=", 1) for part in self.cookies.split(";") if "=" in part
        )
        uifid = cookie_values.get("UIFID") or cookie_values.get("UIFID_TEMP")
        if uifid:
            headers["uifid"] = uifid
        if cookie_values.get("s_v_web_id"):
            params["verifyFp"] = params["fp"] = cookie_values["s_v_web_id"]
        json_data = {**self.COMMON_PARAMS, **params} if method == "POST" else None
        result = await self._request(
            method, uri, params, headers=headers, json_data=json_data, web_uifid=uifid or "",
        )
        return self._validate_collection_response(result, uri)

    @staticmethod
    def _validate_collection_response(result, uri):
        if not isinstance(result, dict):
            raise DataFetchError("收藏接口响应格式不正确，未确认扫描结果")
        if result.get("status_code") != 0:
            raise DataFetchError(
                f"收藏接口未成功（status_code={result.get('status_code')}），"
                "请确认已登录且收藏可在网页版访问"
            )
        list_key = "collects_list" if uri.endswith("collects/list/") else "aweme_list"
        if list_key not in result or not isinstance(result[list_key], (list, type(None))):
            raise DataFetchError(f"收藏接口响应缺少有效的 {list_key}，无法确认收藏内容")
        return result

    async def get_collection_folders(self, cursor: int = 0, count: int = 20) -> Dict[str, Any]:
        """List the currently logged-in user's collection folders."""
        return await self._collection_request("/aweme/v1/web/collects/list/", cursor, count)

    async def get_collection_videos(
        self, collects_id: str, cursor: int = 0, count: int = 20,
    ) -> Dict[str, Any]:
        """List works in one collection folder (F2 UserCollectsVideo endpoint)."""
        return await self._collection_request(
            "/aweme/v1/web/collects/video/list/", cursor, count, collects_id=collects_id,
        )

    async def get_collected_videos(self, cursor: int = 0, count: int = 20) -> Dict[str, Any]:
        """List all collected works, including those outside a folder."""
        return await self._collection_request(
            "/aweme/v1/web/aweme/listcollection/", cursor, count, method="POST",
        )

    async def get_video_detail(self, aweme_id: str) -> dict:
        """即时读取作品详情，避免把会过期的媒体地址写进收藏快照。"""
        if not isinstance(aweme_id, str) or not aweme_id.isdigit():
            raise ValueError("作品 ID 必须为数字字符串")
        result = await self._request("GET", "/aweme/v1/web/aweme/detail/", {"aweme_id": aweme_id})
        detail = result.get("aweme_detail")
        if result.get("status_code") != 0 or not isinstance(detail, dict) or str(detail.get("aweme_id")) != aweme_id:
            raise CollectionError("无法读取该视频内容，可能已删除、不可访问或需要重新登录。", "content_unavailable")
        return detail

def _pick_first_url(image_data: dict) -> str:
    """从抖音图片对象中提取最合适的 URL。"""
    candidate_paths = [
        ("display_image", "url_list"),
        ("owner_watermark_image", "url_list"),
        ("download_url", "url_list"),
        ("origin_url", "url_list"),
        ("label_large", "url_list"),
    ]

    for outer_key, inner_key in candidate_paths:
        value = image_data.get(outer_key, {})
        if isinstance(value, dict) and value.get(inner_key):
            return value[inner_key][0]

    if image_data.get("url_list"):
        return image_data["url_list"][0]
    if image_data.get("download_url_list"):
        return image_data["download_url_list"][0]
    return ""

def extract_image_urls(data: dict) -> list[str]:
    """兼容多种 aweme 图文结构，提取图片链接。"""
    image_urls = []

    candidate_lists = [
        data.get("images", []),
        data.get("image_infos", []),
        data.get("image_post_info", {}).get("images", []),
        data.get("image_post_info", {}).get("image_list", []),
    ]

    for items in candidate_lists:
        if not items:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = _pick_first_url(item)
            if url:
                image_urls.append(url)
        if image_urls:
            break

    deduped = []
    seen = set()
    for url in image_urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped

def normalize_page(data: dict, collects_id: str = "") -> dict:
    videos = []
    for item in data.get("aweme_list") or []:
        author = item.get("author") or {}
        statistics = item.get("statistics") or {}
        aweme_id = str(item.get("aweme_id", ""))
        page_type = "note" if item.get("images") or item.get("image_post_info") or str(item.get("aweme_type")) == "68" else "video"
        videos.append({
            "aweme_id": aweme_id,
            "title": item.get("desc", ""),
            "author": author.get("nickname", ""),
            "sec_uid": author.get("sec_uid", ""),
            "aweme_type": item.get("aweme_type"),
            "kind": page_type,
            "cover_url": ((item.get("video") or {}).get("cover") or {}).get("url_list", [""])[0] if ((item.get("video") or {}).get("cover") or {}).get("url_list") else (extract_image_urls(item) or [""])[0],
            "image_urls": extract_image_urls(item),
            "duration": int(((item.get("video") or {}).get("duration") or 0) / 1000),
            "url": f"https://www.douyin.com/{page_type}/{aweme_id}",
            "likes": statistics.get("digg_count", 0),
            "comments": statistics.get("comment_count", 0),
            "collections": statistics.get("collect_count", 0),
        })
    result = {
        "success": True, "videos": videos, "count": len(videos),
        "cursor": data.get("cursor", 0), "has_more": bool(data.get("has_more", False)),
    }
    # Hidden/deleted works are counted separately by Douyin and omitted from
    # aweme_list. Preserve these fields so readable count is not called the total.
    for key in ("invalid_item_count", "invalid_item_text", "is_clear_invalid_item"):
        if key in data:
            result[key] = data[key]
    if collects_id:
        result["collects_id"] = collects_id
    return result
