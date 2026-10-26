"""仅监听本机的网页/API 入口。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .collections import CollectionError
from .organizer import Organizer
from .writer import BrowserWriter


class SettingsInput(BaseModel):
    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=4000)
    clear_api_key: bool = False
    cookie_path: str = Field(default="", max_length=2000)
    max_folders: int = Field(default=8, ge=2, le=20)
    batch_size: int = Field(default=20, ge=1, le=50)
    interval: float = Field(default=2, ge=1, le=30)
    asr_model: Literal["base", "small"] = "small"


class ClassifyInput(BaseModel):
    restart: bool = False


class ExecuteInput(BaseModel):
    plan_id: str
    revision: int
    confirmed: bool = False


class ChatInput(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=64)


def create_app(organizer=None):
    owner = organizer or Organizer()
    login_browser = None
    shared_writer = None
    def writer_factory():
        nonlocal shared_writer
        if shared_writer is None:
            shared_writer = BrowserWriter(owner.root, owner.config()["interval"], owner.client_factory)
        shared_writer.interval = owner.config()["interval"]
        return shared_writer
    if owner.writer_factory is None:
        owner.writer_factory = writer_factory

    @asynccontextmanager
    async def lifespan(app):
        yield
        owner.pause()
        if owner.task and not owner.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(owner.task), timeout=5)
            except asyncio.TimeoutError:
                owner.task.cancel()
                await asyncio.gather(owner.task, return_exceptions=True)
        if login_browser:
            await login_browser.close()
        if shared_writer:
            await shared_writer.close()

    app = FastAPI(title="拾藏", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.state.organizer = owner

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-shicang-request") != "1" or (origin and origin != str(request.base_url).rstrip("/")):
                return JSONResponse({"message": "请从本机拾藏页面发起操作。", "code": "origin_rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(CollectionError)
    async def collection_error(request, exc):
        status = 409 if exc.code in {"busy", "stale_plan", "plan_locked"} else 400
        return JSONResponse({"message": str(exc), "code": exc.code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # 默认校验响应会回显输入；设置接口中的密钥不得被回显。
        return JSONResponse({"message": "填写内容的格式或范围不正确，请检查后重试。", "code": "invalid_input"}, status_code=422)

    @app.get("/api/state")
    async def state():
        return owner.status()

    @app.get("/api/knowledge")
    async def knowledge():
        return owner.knowledge.status()

    @app.post("/api/knowledge/index")
    async def index_content():
        if login_browser:
            raise CollectionError("请先完成登录，再处理视频内容。", "busy")
        snapshot = owner.knowledge.snapshot()
        if snapshot.get("demo"):
            raise CollectionError("示例模式已提供合成转写，请切换真实收藏后处理。", "demo_mode")
        return owner.start("index", owner.knowledge.index)

    @app.get("/api/chat/{session_id}")
    async def conversation(session_id: str):
        return owner.knowledge.session(session_id)

    @app.post("/api/chat")
    async def chat(values: ChatInput):
        try:
            return await owner.knowledge.ask(values.question, values.session_id)
        except httpx.HTTPError:
            raise CollectionError("问答模型请求失败，请检查网络或服务配置后重试。", "model_error") from None

    @app.get("/api/connection")
    async def connection():
        try:
            owner.cookies()
            return {"available": True, "message": "检测到本机登录状态，扫描时验证接口可用性。"}
        except CollectionError:
            return {"available": False, "message": "请连接抖音账号或选择已有登录状态文件。"}

    @app.put("/api/settings")
    async def settings(values: SettingsInput):
        payload = values.model_dump()
        if "asr_model" not in values.model_fields_set:
            payload.pop("asr_model")
        return owner.save_settings(payload)

    @app.post("/api/scan")
    async def scan():
        if login_browser:
            raise CollectionError("请先点击完成登录，再开始扫描。", "busy")
        return owner.start("scan", owner.scan)

    @app.post("/api/classify")
    async def classify(values: ClassifyInput):
        return owner.start("classify", lambda: owner.classify(values.restart))

    @app.patch("/api/plan")
    async def edit_plan(values: dict):
        return owner.edit_plan(values)

    @app.post("/api/execute")
    async def execute(values: ExecuteInput):
        if login_browser:
            raise CollectionError("请先点击完成登录，再执行整理。", "busy")
        if not values.confirmed:
            raise CollectionError("请先确认分类预览中的具体方案。", "confirmation_required")
        snapshot, plan = owner.data.get("snapshot"), owner.data.get("plan")
        if not snapshot or snapshot.get("demo"):
            raise CollectionError("示例模式不会写入抖音。", "demo_mode")
        if not plan or plan["id"] != values.plan_id or plan["revision"] != values.revision:
            raise CollectionError("方案已变化，请刷新后重新确认。", "stale_plan")
        return owner.start("execute", lambda: owner.execute(values.plan_id, values.revision))

    @app.post("/api/pause")
    async def pause():
        return owner.pause()

    @app.post("/api/demo")
    async def demo():
        owner.use_demo()
        return owner.status()

    @app.delete("/api/demo")
    async def clear_demo():
        owner.clear_demo()
        return owner.status()

    @app.post("/api/browser/open")
    async def open_login():
        nonlocal login_browser
        owner.require_idle()
        if login_browser:
            await login_browser.close()
        login_browser = owner.writer_factory()
        # 在 UI 中由用户打开的交互式窗口，只加载已有状态或等待扫码，不自动操作收藏。
        try:
            cookies = owner.cookies()
        except CollectionError:
            cookies = ""
        await login_browser.open(cookies)
        return {"message": "请在抖音窗口完成登录或验证，然后回到这里点击完成登录。"}

    @app.post("/api/browser/finish")
    async def finish_login():
        nonlocal login_browser
        owner.require_idle()
        if not login_browser:
            raise CollectionError("请先打开抖音登录窗口。", "login_required")
        await login_browser.save_login()
        await login_browser.close()
        login_browser = None
        return {"message": "本机登录状态已保存。请扫描收藏以核对账号和完整列表。"}

    @app.get("/api/export")
    async def export(archive: str = ""):
        if archive:
            if archive not in {record["id"] for record in owner.data.get("archives", [])}:
                raise CollectionError("未找到该次归档记录。", "record_not_found")
            payload = owner.read("archive-" + archive, None)
        else:
            payload = {"snapshot": owner.data.get("snapshot"), "plan": owner.data.get("plan"), "history": owner.data.get("history", []), "archives": owner.data.get("archives", [])}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="shicang-records.json"'})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    web = Path(__file__).resolve().parents[2] / "web"
    if not web.is_dir():
        web = Path(__file__).with_name("web")
    if web.is_dir():
        app.mount("/static", StaticFiles(directory=web), name="static")

        @app.get("/")
        async def index():
            return FileResponse(web / "index.html")

    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="在本机打开拾藏收藏整理网页")
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=options.port, access_log=False)


if __name__ == "__main__":
    main()
