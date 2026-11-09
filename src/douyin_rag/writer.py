"""通过独立 Playwright 会话获取抖音扫码登录状态。"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout, Error as BrowserError

from .collections import CollectionError, normalize_cookie_string
from .verification import DOUYIN_FIXED_USER_AGENT


class BrowserWriter:
    HOME = "https://www.douyin.com/user/self?showTab=favorite_collection"
    QR = '#douyin_login_comp_scan_code img, #animate_qrcode_container canvas, img[alt*="二维码"], img[alt*="扫码"], [class*="qrcode"] canvas, [class*="QRCode"] canvas, img[src*="qrcode"]'

    def __init__(self, root):
        self.root = Path(root)
        self.playwright = self.context = self.page = None
        self.headless = False

    async def open(self, cookies="", *, headless=False):
        if self.page and not self.page.is_closed():
            if self.headless and not headless:
                await self.close()
            else:
                return
        if self.context:
            await self.close()
        self.root.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.headless = headless
        options = {"headless": headless, "viewport": {"width": 1440, "height": 1000}, "locale": "zh-CN",
                   "user_agent": DOUYIN_FIXED_USER_AGENT}
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

    async def qr_element(self):
        visible = []
        for element in await self.page.locator(self.QR).all():
            if await element.is_visible():
                box = await element.bounding_box()
                if box and 80 <= box["width"] <= 500 and 80 <= box["height"] <= 500:
                    visible.append(element)
        return visible[0] if len(visible) == 1 else None

    async def prepare_login(self):
        try:
            await self.page.locator(self.QR).first.wait_for(state="visible", timeout=8000)
        except BrowserTimeout:
            pass
        if await self.qr_element():
            return
        try:
            await self.cookies()
            return
        except CollectionError:
            pass
        # 只操作真实登录入口，不提交手机号、密码或自动处理验证码。
        trigger = self.page.get_by_text(re.compile(r"^(登录|立即登录|登录 / 注册)$"))
        try:
            await trigger.first.wait_for(state="visible", timeout=15000)
            await (await self.unique_visible(trigger, "登录入口")).click()
            await self.page.locator(self.QR).first.wait_for(state="visible", timeout=15000)
        except (BrowserTimeout, CollectionError):
            # 入口变化或页面未就绪时，由当前页面提供重新获取二维码的入口。
            return

    async def login_state(self):
        if not self.page or self.page.is_closed():
            return {"status": "error", "message": "登录页面已关闭，请重新获取二维码。"}
        try:
            # 与 douyin-mcp 的扫码流程一致：会话 Cookie 到达即完成登录。
            # 收藏读取属于扫描，不作为扫码成功的前置条件。
            try:
                await self.cookies()
            except CollectionError:
                pass
            else:
                return {"status": "authenticated", "message": "已保存登录状态，扫描时确认收藏能否读取。"}
            qr = await self.qr_element()
            if qr:
                expired = self.page.get_by_text(re.compile(r"二维码.*(失效|过期)|点击刷新|刷新二维码"))
                if any([await element.is_visible() for element in await expired.all()]):
                    return {"status": "expired", "message": "二维码已过期，请刷新后重新扫码。"}
                image = await qr.screenshot(type="png", timeout=5000)
                return {"status": "waiting", "message": "使用抖音 App 扫码，并在手机上确认登录。",
                        "qr_image": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}
            return {"status": "waiting", "message": "正在等待登录页面和二维码加载。"}
        except BrowserError:
            return {"status": "error", "message": "暂未获取到二维码，请重新获取。"}

    async def cookies(self):
        if not self.context:
            raise CollectionError("抖音登录窗口尚未打开。", "login_required")
        values = await self.context.cookies()
        cookie = "; ".join(f"{item['name']}={item['value']}" for item in values
                           if item["domain"].lstrip(".").lower() == "douyin.com"
                           or item["domain"].lower().endswith(".douyin.com"))
        if not normalize_cookie_string(cookie).has_session:
            raise CollectionError("请使用抖音 App 扫码，并在手机上确认登录。", "login_required")
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

    async def unique_visible(self, locator, description):
        matches = []
        for candidate in await locator.all():
            if await candidate.is_visible():
                matches.append(candidate)
        if len(matches) != 1:
            raise CollectionError(f"未能唯一定位{description}。请保持抖音窗口打开，完成登录或验证后重试。", "browser_attention")
        return matches[0]
