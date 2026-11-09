"""通过 Ego Lite 的抖音会话执行整理，沿用本机扫描会话核对结果。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

from .collections import CollectionClient, CollectionError, SEC_UID_PATTERN, SELF_PROFILE_PATH


# CLI 每轮从 stdin 接收脚本，任务空间和页面在调用之间保留。
# 会话 Cookie 仅在 Ego 侧计算指纹，CLI 输出限定为整理状态。
EGO_SCRIPT = r"""
const { createHash } = await import("node:crypto");
let spaceId = input.spaceId;
let handedOff = input.takeOver;
async function run() {
    if (input.operation === "open" && spaceId === null) {
        const delegated = (await listTaskSpaces()).filter(space =>
            space.name === input.spaceName && space.ownership === "agentDelegatedToUser");
        if (delegated.length > 1) return {error: "browser_unavailable"};
        if (delegated.length === 1) {
            spaceId = delegated[0].id;
            handedOff = true;
        }
    }
    const task = handedOff
        ? await takeOverTaskSpace(spaceId)
        : await taskSpace(spaceId ?? input.spaceName);
    spaceId = task.spaceId;
    handedOff = false;
    if (input.operation === "close") {
        await task.finish({keep: []});
        return {};
    }
    const page = task.page("p1");
    const origin = "https://www.douyin.com";
    const writePaths = {
        create: "/aweme/v1/web/collects/maintain/",
        move: "/aweme/v1/web/collects/video/move/"
    };
    if (input.operation === "open" && !(await page.url()).startsWith(origin + "/")) {
        await page.goto(origin + "/user/self?showTab=favorite_collection", {
            waitUntil: "domcontentloaded", timeout: 20000
        });
    }
    async function handOff(error) {
        await task.handOff();
        handedOff = true;
        return {error};
    }
    if (!(await page.url()).startsWith(origin + "/")) {
        return await handOff("browser_attention");
    }
    async function sessionMarker(path) {
        const {cookies} = await page.cdp("Network.getCookies", {urls: [origin + path]});
        if (!cookies.some(({name, value}) => /^(sessionid|sessionid_ss)$/.test(name) && value)) {
            return "";
        }
        const session = cookies.filter(({name}) =>
            /^(uid_tt|uid_tt_ss|sessionid|sessionid_ss|sid_tt|sid_guard)$/.test(name));
        const entries = session.map(({name, value, domain, path}) =>
            JSON.stringify([name, value, domain, path])).sort();
        return createHash("sha256").update(JSON.stringify(entries)).digest("hex");
    }
    if (input.operation === "open") {
        const marker = await sessionMarker(input.profilePath);
        if (!marker) return await handOff("login_required");
        const secUid = await page.evaluate(async ({path, pattern}) => {
            if (location.origin !== "https://www.douyin.com") return "";
            const params = new URLSearchParams({
                aid: "6383", device_platform: "webapp", channel: "channel_pc_web"
            });
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            try {
                const response = await fetch(path + "?" + params, {
                    credentials: "include", cache: "no-store", redirect: "error",
                    signal: controller.signal
                });
                if (!response.ok) return "";
                const result = await response.json();
                const id = result?.user?.sec_uid;
                return result?.status_code === 0 && typeof id === "string"
                    && new RegExp("^(?:" + pattern + ")$").test(id) ? id : "";
            } catch {
                return "";
            } finally {
                clearTimeout(timer);
            }
        }, {path: input.profilePath, pattern: input.accountPattern});
        if (!secUid) return await handOff("account_unconfirmed");
        if (await sessionMarker(input.profilePath) !== marker) {
            return await handOff("browser_session_changed");
        }
        if (createHash("sha256").update(secUid).digest("hex") !== input.accountMarker) {
            return await handOff("account_changed");
        }
        return {sessionMarker: marker};
    }

    const path = writePaths[input.operation];
    const marker = await sessionMarker(path);
    if (!marker) return await handOff("login_required");
    if (marker !== input.sessionMarker) return await handOff("browser_session_changed");
    const reply = await page.evaluate(async ({operation, path, params}) => {
        if (location.origin !== "https://www.douyin.com") return {error: "browser_attention"};
        const create = operation === "create";
        const headers = {accept: "application/json, text/plain, */*"};
        if (!create) {
            const cookie = Object.fromEntries(
                document.cookie.split(";").map(value => value.trim().split(/=(.*)/s).slice(0, 2))
            );
            Object.assign(params, {
                browser_language: navigator.language,
                browser_name: "Chrome",
                browser_version: navigator.userAgent.match(/Chrome\/([\d.]+)/)?.[1] || "",
                browser_platform: navigator.platform,
                browser_online: "true",
                screen_width: screen.width,
                screen_height: screen.height,
                pc_client_type: "1"
            });
            if (cookie.msToken) params.msToken = cookie.msToken;
            if (cookie.s_v_web_id) params.verifyFp = params.fp = cookie.s_v_web_id;
            headers["content-type"] = "application/x-www-form-urlencoded";
            if (cookie.UIFID || cookie.UIFID_TEMP) headers.uifid = cookie.UIFID || cookie.UIFID_TEMP;
            if (cookie.passport_csrf_token) headers["x-secsdk-csrf-token"] = cookie.passport_csrf_token;
        }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 12000);
        try {
            const response = await fetch(path + "?" + new URLSearchParams(params), {
                method: "POST", credentials: "include", cache: "no-store",
                redirect: "error", signal: controller.signal, headers
            });
            if ([401, 403, 429].includes(response.status)) return {error: "browser_attention"};
            if (!response.ok) return {error: "write_unconfirmed"};
            const value = await response.json();
            if (typeof value?.status_code !== "number") return {error: "write_unconfirmed"};
            return value.status_code === 0 ? {} : {error: "write_rejected"};
        } catch {
            return {error: "write_unconfirmed"};
        } finally {
            clearTimeout(timer);
        }
    }, {operation: input.operation, path, params: input.params});
    return reply.error === "browser_attention" ? await handOff(reply.error) : reply;
}
let result;
try {
    result = await run();
} catch {
    result = {error: ["create", "move"].includes(input.operation)
        ? "write_unconfirmed" : "browser_unavailable"};
}
console.log("SHICANG_EGO " + JSON.stringify({...result, spaceId, handedOff}));
"""


class EgoWriter:
    def __init__(self, root, interval=2, client_factory=CollectionClient):
        key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]
        self.space_name = f"拾藏整理 {key}"
        self.space_id = None
        self.handed_off = False
        self.interval = interval
        self.client_factory = client_factory
        self.read_cookies = ""
        self.session_marker = ""

    async def _command(self, operation, params=None, *, account_marker=None):
        payload = {
            "operation": operation, "params": params, "spaceId": self.space_id,
            "spaceName": self.space_name, "sessionMarker": self.session_marker,
            "takeOver": operation == "open" and self.handed_off,
        }
        if operation == "open":
            payload.update(accountMarker=account_marker, profilePath=SELF_PROFILE_PATH, accountPattern=SEC_UID_PATTERN)
        source = ("const input = " + json.dumps(payload) + ";\n" + EGO_SCRIPT).encode("utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("DOUYIN_")}
        try:
            completed = await asyncio.to_thread(
                subprocess.run, ["ego-browser", "nodejs"], input=source, capture_output=True,
                timeout=35, check=True, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # Windows 的 Ego CLI 将 console.log 发到 stderr，保留两种传输方式。
            prefix = b"SHICANG_EGO "
            lines = (completed.stdout + b"\n" + completed.stderr).splitlines()
            responses = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
            if len(responses) != 1:
                raise ValueError("missing or ambiguous Ego response")
            result = json.loads(responses[0])
            if not isinstance(result, dict):
                raise ValueError("invalid Ego response")
        except FileNotFoundError:
            raise CollectionError("找不到 ego-browser。请安装并打开 Ego Lite 后继续整理。", "browser_unavailable") from None
        except (OSError, subprocess.SubprocessError, ValueError):
            if operation in {"create", "move"}:
                raise CollectionError("Ego Lite 写入结果未确认，继续整理时会先查询抖音。", "write_unconfirmed") from None
            raise CollectionError("无法连接 Ego Lite 整理页面，请检查浏览器状态后继续。", "browser_unavailable") from None

        if type(result.get("spaceId")) is int:
            self.space_id = result["spaceId"]
            self.handed_off = result.get("handedOff") is True
        error = result.get("error")
        if error:
            messages = {
                "login_required": "请在 Ego Lite 的拾藏整理页面登录抖音，然后点击继续整理。",
                "account_changed": "Ego Lite 登录的是另一个抖音账号。请切换到扫描所用账号后继续整理。",
                "account_unconfirmed": "暂时无法确认 Ego Lite 中的抖音账号。请检查登录或验证状态后继续整理。",
                "browser_session_changed": "Ego Lite 登录会话刚刚变化。继续整理时会重新核对账号。",
                "browser_attention": "抖音网页需要处理。请在 Ego Lite 中检查登录或验证码，再点击继续整理。",
                "write_rejected": "抖音拒绝了整理请求。请检查 Ego Lite 中的抖音页面后继续。",
                "write_unconfirmed": "抖音写入结果未确认，继续整理时会先查询抖音。",
                "browser_unavailable": "无法使用 Ego Lite 整理页面，请检查浏览器状态后继续。",
            }
            raise CollectionError(messages.get(error, messages["browser_unavailable"]), error)
        return result

    async def open(self, cookies):
        self.read_cookies = self.session_marker = ""
        async with self.client_factory(cookies) as client:
            account_marker = await client.own_account_marker()
        result = await self._command("open", account_marker=account_marker)
        marker = result.get("sessionMarker")
        if not isinstance(marker, str) or len(marker) != 64 or any(char not in "0123456789abcdef" for char in marker):
            raise CollectionError("Ego Lite 未返回可核对的登录会话。请继续整理以重试。", "account_unconfirmed")
        self.session_marker = marker
        self.read_cookies = cookies

    async def close(self):
        self.read_cookies = self.session_marker = ""
        if self.space_id is None or self.handed_off:
            return
        try:
            await self._command("close")
        except CollectionError:
            # 整理结果已保存，保留原任务空间供后续继续或关闭。
            return
        self.space_id = None

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
        async with self.client_factory(self.read_cookies) as client:
            return await self.all_pages(client.get_collection_folders, "collects_list")

    async def members(self, folder_id):
        async with self.client_factory(self.read_cookies) as client:
            async def fetch(cursor, count):
                return await client.get_collection_videos(folder_id, cursor=cursor, count=count)
            return {str(row["aweme_id"]) for row in await self.all_pages(fetch, "aweme_list")}

    @staticmethod
    def match_folder(folders, name):
        matches = [str(folder["collects_id"]) for folder in folders if folder.get("collects_name") == name]
        if len(matches) > 1:
            raise CollectionError(f"存在多个同名收藏夹“{name}”，请先在抖音中区分名称。", "ambiguous_folder")
        return matches[0] if matches else None

    async def ensure_folder(self, name):
        found = self.match_folder(await self.folders(), name)
        if found:
            return found
        await self._command("create", {
            "action": "1", "collects_name": name, "secret": "1",
            "aid": "6383", "device_platform": "webapp", "channel": "channel_pc_web",
            "version_code": "230000", "version_name": "23.0.0",
            "update_version_code": "170400", "platform": "PC", "cookie_enabled": "true",
        })
        for _ in range(4):
            await asyncio.sleep(self.interval)
            found = self.match_folder(await self.folders(), name)
            if found:
                return found
        raise CollectionError("未在抖音中读到新收藏夹，未确认创建成功；重试会先查询，避免重复建夹。", "verification_failed")

    async def move_request(self, item_id, target=None, name="", source=None):
        # 参数来源：cv-cat/DouYin_Spider 的 move_collect_aweme/remove_collect_aweme。
        params = {
            "aid": "6383", "device_platform": "webapp", "channel": "channel_pc_web",
            "item_ids": item_id, "item_type": "2", "collects_name": name,
            "version_code": "170400", "version_name": "17.4.0",
            "update_version_code": "170400", "platform": "PC", "cookie_enabled": "true",
        }
        if target:
            params.update(to_collects_id=target, move_collects_list=target, update_collects_sort="true")
        if source:
            params["from_collects_id"] = source
        await self._command("move", params)

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
