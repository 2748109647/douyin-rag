"""临时下载可读收藏视频，并在本机生成带时间戳的语音转写。"""
from __future__ import annotations

import asyncio
import math
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import httpx

from .collections import CollectionError, extract_image_urls
from .verification import DOUYIN_FIXED_USER_AGENT


MEDIA_DOMAINS = ("douyinvod.com", "douyincdn.com", "douyin.com", "douyinpic.com", "bytecdn.cn", "byteimg.com", "ibytedtos.com", "iesdouyin.com")
MAX_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 3600
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_NOTE_IMAGES = 100
MAX_ANALYSIS_IMAGES = 8
MAX_NOTE_TEXT = 50000


def media_url(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        valid = parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443)
        valid = valid and any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS)
        if valid:
            return value
    except (TypeError, ValueError):
        pass
    raise CollectionError("媒体地址不在允许的抖音域名范围内。", "unsafe_media_url")


def media_candidates(detail):
    video = detail.get("video") or {}
    addresses = [video.get("play_addr"), video.get("download_addr")]
    addresses += [(row or {}).get("play_addr") for row in video.get("bit_rate") or []]
    result = []
    for address in addresses:
        for value in (address or {}).get("url_list") or []:
            try:
                value = media_url(value)
            except CollectionError:
                continue
            if value not in result:
                result.append(value)
    return result[:4]


async def download_video(url, target, transport=None):
    """逐跳校验地址；不向媒体 CDN 发送登录 Cookie 或模型密钥。"""
    headers = {"User-Agent": DOUYIN_FIXED_USER_AGENT, "Referer": "https://www.douyin.com/"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, transport=transport) as client:
        async with asyncio.timeout(300):
            for _ in range(5):
                url = media_url(url)
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code != 200:
                        raise CollectionError("视频下载被拒绝，请重新登录后重试。", "media_download_failed")
                    try:
                        size = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        size = 0
                    if size > MAX_BYTES:
                        raise CollectionError("视频超过单条 256 MiB 的处理上限。", "media_too_large")
                    received = 0
                    with Path(target).open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > MAX_BYTES:
                                raise CollectionError("视频超过单条 256 MiB 的处理上限。", "media_too_large")
                            handle.write(chunk)
                    if not received:
                        raise CollectionError("没有下载到可转写的视频内容。", "empty_media")
                    return
    raise CollectionError("视频媒体重定向次数过多。", "media_download_failed")


async def download_image(url, target, transport=None):
    """逐跳校验地址；不向图片 CDN 发送登录 Cookie 或模型密钥。"""
    headers = {"User-Agent": DOUYIN_FIXED_USER_AGENT, "Referer": "https://www.douyin.com/"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, transport=transport) as client:
        async with asyncio.timeout(120):
            for _ in range(5):
                url = media_url(url)
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code != 200:
                        raise CollectionError("图片下载被拒绝，请重新登录后重试。", "image_download_failed")
                    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if content_type and not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                        raise CollectionError("图片地址返回了非图片内容。", "image_download_failed")
                    try:
                        size = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        size = 0
                    if size > MAX_IMAGE_BYTES:
                        raise CollectionError("图片超过单张 20 MiB 的处理上限。", "image_too_large")
                    received = 0
                    with Path(target).open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > MAX_IMAGE_BYTES:
                                raise CollectionError("图片超过单张 20 MiB 的处理上限。", "image_too_large")
                            handle.write(chunk)
                    if not received:
                        raise CollectionError("没有下载到可识别的图片内容。", "empty_image")
                    return
    raise CollectionError("图片媒体重定向次数过多。", "image_download_failed")


class LocalOCR:
    """本机图文识别；模型随依赖安装，不上传图片。"""

    def __init__(self, root):
        self.root = Path(root)
        self.engine = None
        self.lock = threading.Lock()

    def ensure_loaded(self):
        from rapidocr import RapidOCR
        with self.lock:
            if self.engine is None:
                self.engine = RapidOCR()

    @staticmethod
    def _top_left(box):
        try:
            points = box.tolist() if hasattr(box, "tolist") else box
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            return min(xs), min(ys)
        except (IndexError, TypeError, ValueError):
            return 0., 0.

    def recognize(self, path):
        self.ensure_loaded()
        with self.lock:
            result = self.engine(str(path))
        texts = getattr(result, "txts", None)
        boxes = getattr(result, "boxes", None)
        if not texts:
            return ""
        rows = []
        for index, text in enumerate(texts):
            value = str(text).strip()
            if not value:
                continue
            box = boxes[index] if boxes is not None and index < len(boxes) else None
            position = self._top_left(box) if box is not None else (0., 0.)
            rows.append((position[1], position[0], index, value))
        rows.sort()
        return "\n".join(row[3] for row in rows)


class LocalTranscriber:
    def __init__(self, root):
        self.root = Path(root)
        self.model = None
        self.model_name = None
        self.lock = threading.Lock()
        self.ocr = LocalOCR(self.root)

    def transcribe(self, path, model_name="small"):
        if model_name not in {"base", "small"}:
            raise CollectionError("转写模型只支持 base 或 small。", "invalid_settings")
        # 仅允许匿名下载公开模型，不使用机器上可能存在的 Hugging Face 凭据。
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        with self.lock:
            if self.model_name != model_name:
                self.model = None
                self.model_name = None
                self.model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4,
                                          download_root=str(self.root / "models" / "speech"))
                self.model_name = model_name
            audio = decode_audio(str(path), sampling_rate=16000)
            if len(audio) > MAX_SECONDS * 16000:
                raise CollectionError("视频超过单条 60 分钟的处理上限。", "media_too_long")
            segments, _ = self.model.transcribe(audio, beam_size=5, vad_filter=True, condition_on_previous_text=False)
            result = []
            for segment in segments:
                text = segment.text.strip()
                if text and math.isfinite(segment.start) and math.isfinite(segment.end):
                    result.append({"start": round(segment.start, 2), "end": round(segment.end, 2), "text": text})
            if not result:
                raise CollectionError("未识别到可用语音，首版暂不识别画面文字或无声演示。", "no_speech")
            return result

    @staticmethod
    def frames(path, duration=0):
        """最多八张有时间位置的缩略帧，用现有 PyAV 编解码。"""
        import av
        result = []
        try:
            with av.open(str(path)) as container:
                if not container.streams.video:
                    return result
                stream = container.streams.video[0]
                start = float((stream.start_time or 0) * stream.time_base)
                duration = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / av.time_base or duration
                if not math.isfinite(duration) or duration > MAX_SECONDS:
                    raise CollectionError("视频时长无法安全用于画面分析。", "media_too_long")
                end = max(0., duration - min(1., duration / 8))
                targets = [end * index / 7 for index in range(8)] if duration > 0 else [0]
                seen = set()
                for target in targets:
                    container.seek(int((start + target) / stream.time_base), stream=stream, backward=True)
                    for index, frame in enumerate(container.decode(stream)):
                        position = max(0., float(frame.time or start) - start)
                        if position + .01 < target and index < 300:
                            continue
                        stamp = round(position, 2)
                        if stamp not in seen:
                            scale = min(1., 1024 / max(frame.width, frame.height))
                            width, height = max(2, int(frame.width * scale) // 2 * 2), max(2, int(frame.height * scale) // 2 * 2)
                            image = frame.reformat(width=width, height=height, format="yuvj420p")
                            encoder = av.CodecContext.create("mjpeg", "w")
                            encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuvj420p"
                            data = b"".join(bytes(packet) for packet in encoder.encode(image) + encoder.encode(None))
                            if len(data) <= 1024 * 1024:
                                result.append({"time": stamp, "image": data})
                                seen.add(stamp)
                        break
        except CollectionError:
            raise
        except (av.error.FFmpegError, OSError, ValueError, OverflowError):
            raise CollectionError("视频画面解码失败，无法提取字幕与关键帧。", "frame_decode_error") from None
        return result

    async def extract(self, item, client, model_name="small", *, include_frames=False, transcribe_audio=True):
        if item.get("kind") != "video":
            raise CollectionError("图文内容请使用图片文字识别。", "unsupported_content")
        if item.get("duration", 0) > MAX_SECONDS:
            raise CollectionError("视频超过单条 60 分钟的处理上限。", "media_too_long")
        detail = await client.get_video_detail(item["aweme_id"])
        candidates = media_candidates(detail)
        if not candidates:
            raise CollectionError("作品详情未提供可读取的媒体地址。", "content_unavailable")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="transcribe-", dir=self.root) as directory:
            target = Path(directory) / "video.mp4"
            for offset, url in enumerate(candidates):
                try:
                    await download_video(url, target)
                    break
                except (httpx.HTTPError, TimeoutError, CollectionError):
                    if offset == len(candidates) - 1:
                        raise CollectionError("无法下载该视频，登录状态或媒体访问可能受限。", "media_download_failed") from None
            # 原生推理完成前等待当前步骤，暂停在作品之间生效；临时媒体随后自动删除。
            if not include_frames:
                return await asyncio.to_thread(self.transcribe, target, model_name)
            evidence = {"frames": [], "segments": [], "speech_error": "", "frame_error": ""}
            try:
                evidence["frames"] = await asyncio.to_thread(self.frames, target, item.get("duration", 0))
            except CollectionError as exc:
                evidence["frame_error"] = str(exc)
            if transcribe_audio:
                try:
                    evidence["segments"] = await asyncio.to_thread(self.transcribe, target, model_name)
                except CollectionError as exc:
                    if exc.code != "no_speech":
                        raise
                    evidence["speech_error"] = "未识别到可用语音。"
                except ValueError:
                    evidence["speech_error"] = "未获得可解码的语音。"
            return evidence

    @staticmethod
    def _image_preview(path, size=1024):
        import av
        try:
            with av.open(str(path)) as container:
                if not container.streams.video:
                    raise CollectionError("图片无法解码。", "image_decode_error")
                frame = next(container.decode(video=0))
                edge = max(frame.width, frame.height)
                if edge <= 0:
                    raise CollectionError("图片尺寸无法用于分析。", "image_decode_error")
                scale = min(1., size / edge)
                width = max(2, int(frame.width * scale) // 2 * 2)
                height = max(2, int(frame.height * scale) // 2 * 2)
                image = frame.reformat(width=width, height=height, format="yuvj420p")
                encoder = av.CodecContext.create("mjpeg", "w")
                encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuvj420p"
                data = b"".join(bytes(packet) for packet in encoder.encode(image) + encoder.encode(None))
        except CollectionError:
            raise
        except (StopIteration, TypeError, av.error.FFmpegError, OSError, ValueError, OverflowError):
            raise CollectionError("图片预览解码失败。", "image_decode_error") from None
        if len(data) > 1024 * 1024:
            raise CollectionError("图片预览超过单张 1 MiB 的分析上限。", "image_too_large")
        return data

    async def extract_note(self, item, client, *, recognize=True, preview=True):
        """识别图文图片文字；本地 OCR，可选生成最多八张分析预览。"""
        detail = None
        try:
            detail = await client.get_video_detail(item["aweme_id"])
        except CollectionError:
            detail = None
        urls = extract_image_urls(detail) if isinstance(detail, dict) else []
        if not urls:
            urls = [value for value in item.get("image_urls") or [] if isinstance(value, str) and value.strip()]
        seen, unique = set(), []
        for value in urls:
            if value in seen:
                continue
            seen.add(value)
            unique.append(value)
        urls = unique[:MAX_NOTE_IMAGES]
        if not urls:
            raise CollectionError("作品详情未提供可读取的图片地址。", "content_unavailable")
        if recognize:
            self.ocr.ensure_loaded()
        if len(urls) <= MAX_ANALYSIS_IMAGES:
            selected = set(range(len(urls)))
        else:
            selected = {round(index * (len(urls) - 1) / (MAX_ANALYSIS_IMAGES - 1)) for index in range(MAX_ANALYSIS_IMAGES)}
        if not preview:
            selected = set()
        texts, images, errors = [], [], []
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="transcribe-", dir=self.root) as directory:
            for index, url in enumerate(urls):
                target = Path(directory) / f"image-{index}.bin"
                try:
                    await download_image(url, target)
                    if recognize:
                        try:
                            value = await asyncio.to_thread(self.ocr.recognize, target)
                        except (OSError, ValueError):
                            errors.append(f"第 {index+1} 张图片识别失败。")
                            value = ""
                        if value.strip():
                            texts.append(value.strip())
                    if index in selected:
                        try:
                            images.append({"time": 0., "image": await asyncio.to_thread(self._image_preview, target)})
                        except CollectionError as exc:
                            errors.append(f"第 {index+1} 张图片预览失败：{exc}")
                except (httpx.HTTPError, TimeoutError, CollectionError):
                    errors.append(f"第 {index+1} 张图片读取失败。")
        return {"images": images, "text": "\n".join(texts), "error": " ".join(errors)}
