"""在真实浏览器中建夹，并在浏览器会话内执行、核验收藏夹移动。"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

from .collections import CollectionClient, CollectionError, normalize_cookie_string


class BrowserWriter:
    HOME = "https://www.douyin.com/user/self?showTab=favorite_collection"
    MOVE = "/aweme/v1/web/collects/video/move/"

    def __init__(self, root, interval=2, client_factory=CollectionClient):
        self.root = Path(root)
        self.interval = interval
        self.client_factory = client_factory
        self.playwright = self.context = self.page = None

    async def open(self, cookies=""):
        if self.page and not self.page.is_closed():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        options = {"headless": False, "viewport": {"width": 1440, "height": 1000}, "locale": "zh-CN"}
        # 优先使用本机 Edge，不下载或改动已安装浏览器。
        executable = os.getenv("DOUYIN_RAG_BROWSER")
        if executable:
            options["executable_path"] = executable
        elif os.name == "nt":
            edge = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
            if edge.is_file():
                options["executable_path"] = str(edge)
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(str(self.root / "browser"), **options)
            if cookies:
                normalized = normalize_cookie_string(cookies)
                await self.context.add_cookies([{"name": part.split("=", 1)[0], "value": part.split("=", 1)[1], "domain": ".douyin.com", "path": "/"} for part in normalized.value.split("; ")])
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto(self.HOME, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            await self.close()
            raise CollectionError("无法打开抖音浏览器。请检查本机 Edge，或设置 DOUYIN_RAG_BROWSER。", "browser_unavailable") from exc

    async def cookies(self):
        if not self.context:
            raise CollectionError("抖音登录窗口尚未打开。", "login_required")
        values = await self.context.cookies("https://www.douyin.com")
        cookie = "; ".join(f"{item['name']}={item['value']}" for item in values)
        if not normalize_cookie_string(cookie).has_session:
            raise CollectionError("请在打开的抖音窗口完成登录或验证，然后点击‘完成登录’。", "login_required")
        return cookie

    async def save_login(self):
        cookie = await self.cookies()
        # 仅保存本应用运行时登录状态；不会进入项目、接口响应或日志。
        path = self.root / "login.cookies"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(cookie)

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.playwright:
                await self.playwright.stop()
            self.context = self.page = self.playwright = None

    async def all_pages(self, fetch, key):
        cursor, seen, result = 0, set(), []
        while True:
            page = await fetch(cursor=cursor, count=50)
            if "cursor" not in page or "has_more" not in page:
                raise CollectionError("写后核对缺少分页信息，未确认成功。", "verification_failed")
            result.extend(page.get(key) or [])
            if not page["has_more"]:
                return result
            following = page["cursor"]
            if following == cursor or following in seen:
                raise CollectionError("写后核对分页停滞，未确认成功。", "verification_failed")
            seen.add(cursor)
            cursor = following
            await asyncio.sleep(self.interval)

    async def folders(self):
        async with self.client_factory(await self.cookies()) as client:
            return await self.all_pages(client.get_collection_folders, "collects_list")

    async def members(self, folder_id):
        async with self.client_factory(await self.cookies()) as client:
            async def fetch(cursor, count):
                return await client.get_collection_videos(folder_id, cursor=cursor, count=count)
            return {str(row["aweme_id"]) for row in await self.all_pages(fetch, "aweme_list")}

    @staticmethod
    def match_folder(folders, name):
        matches = [str(folder["collects_id"]) for folder in folders if folder.get("collects_name") == name]
        if len(matches) > 1:
            raise CollectionError(f"存在多个同名收藏夹“{name}”，请先在抖音中区分名称。", "ambiguous_folder")
        return matches[0] if matches else None

    async def unique_visible(self, locator, description):
        matches = []
        for candidate in await locator.all():
            if await candidate.is_visible():
                matches.append(candidate)
        if len(matches) != 1:
            raise CollectionError(f"未能唯一定位{description}。请保持抖音窗口打开，完成登录或验证后重试。", "browser_attention")
        return matches[0]

    async def ensure_folder(self, name):
        found = self.match_folder(await self.folders(), name)
        if found:
            return found
        await self.page.goto(self.HOME, wait_until="domcontentloaded")
        try:
            trigger = self.page.get_by_text(re.compile(r"^(新建收藏夹|创建收藏夹)$"))
            await trigger.first.wait_for(state="visible", timeout=15000)
            await (await self.unique_visible(trigger, "新建收藏夹入口")).click()
            field = self.page.locator('input[placeholder*="收藏夹"], input[placeholder*="名称"]')
            await field.first.wait_for(state="visible", timeout=5000)
            entry = await self.unique_visible(field, "收藏夹名称输入框")
            await entry.fill(name)
            # 平台若提供公开开关，将新建夹设为私密。
            public = self.page.get_by_role("checkbox", name=re.compile("公开"))
            if await public.count() == 1 and await public.is_visible() and await public.is_checked():
                await public.uncheck()
            dialog = self.page.get_by_role("dialog")
            scope = dialog if await dialog.count() == 1 else self.page
            submit = scope.get_by_role("button", name=re.compile(r"^(创建|新建|完成|确认|确定)$"))
            await (await self.unique_visible(submit, "创建确认按钮")).click()
        except BrowserTimeout as exc:
            raise CollectionError("抖音建夹界面尚未就绪。请完成登录或验证码，若页面变化则需要更新建夹定位。", "browser_attention") from exc
        for _ in range(4):
            await asyncio.sleep(self.interval)
            found = self.match_folder(await self.folders(), name)
            if found:
                return found
        raise CollectionError("未在抖音中读到新收藏夹，未确认创建成功；重试会先查询，避免重复建夹。", "verification_failed")

    async def move_request(self, item_id, target=None, name="", source=None):
        # 参数来源：cv-cat/DouYin_Spider 的 move_collect_aweme/remove_collect_aweme。
        # 请求留在已登录网页内，让页面自身安全 SDK 处理浏览器会话相关请求头。
        params = {"aid": "6383", "device_platform": "webapp", "channel": "channel_pc_web", "item_ids": item_id, "item_type": "2", "collects_name": name}
        if target:
            params.update(to_collects_id=target, move_collects_list=target, update_collects_sort="true")
        if source:
            params["from_collects_id"] = source
        result = await self.page.evaluate(r"""async ({path, params}) => {
            const cookie = Object.fromEntries(document.cookie.split(';').map(v => v.trim().split(/=(.*)/s).slice(0,2)));
            Object.assign(params, {browser_language:navigator.language, browser_name:'Chrome',browser_version:navigator.userAgent.match(/Chrome\/([\d.]+)/)?.[1] || '', browser_platform:navigator.platform, browser_online:'true', cookie_enabled:'true', screen_width:screen.width, screen_height:screen.height, pc_client_type:'1', version_code:'170400', version_name:'17.4.0', update_version_code:'170400', platform:'PC'});
            for (const key of ['msToken']) if(cookie[key]) params[key]=cookie[key];
            if(cookie.s_v_web_id) { params.verifyFp=cookie.s_v_web_id; params.fp=cookie.s_v_web_id; }
            const headers={'content-type':'application/x-www-form-urlencoded'};
            if(cookie.UIFID || cookie.UIFID_TEMP) headers.uifid=cookie.UIFID || cookie.UIFID_TEMP;
            if(cookie.passport_csrf_token) headers['x-secsdk-csrf-token']=cookie.passport_csrf_token;
            try {
                const response=await fetch(path+'?'+new URLSearchParams(params),{method:'POST',credentials:'include',headers});
                let value; try { value=await response.json(); } catch { return {ok:false,http:response.status}; }
                return {ok:response.ok,http:response.status,status_code:value.status_code};
            } catch { return {ok:false,http:0}; }
        }""", {"path": self.MOVE, "params": params})
        if not result.get("ok") or result.get("status_code") != 0:
            raise CollectionError("抖音未接受移动请求。可能需要网页验证或新的安全签名；未标记成功，请在窗口确认状态后继续。", "write_rejected")

    async def move_and_verify(self, item, target, name):
        item_id = item["aweme_id"]
        if item_id not in await self.members(target):
            await self.move_request(item_id, target=target, name=name)
            for _ in range(3):
                await asyncio.sleep(self.interval)
                if item_id in await self.members(target):
                    break
            else:
                raise CollectionError("移动后未读到目标作品，状态尚未确认。请稍后继续，系统会先核对归属。", "verification_failed")
        # 目标已确认后再清理旧归属；仅调用夹内移出，不调用取消收藏或删除夹接口。
        for old in item.get("original_folders", []):
            if old != target and item_id in await self.members(old):
                await self.move_request(item_id, source=old)
                await asyncio.sleep(self.interval)
                if item_id in await self.members(old):
                    raise CollectionError("目标收藏夹已确认，但旧归属未移出。已暂停并保留记录。", "verification_failed")
