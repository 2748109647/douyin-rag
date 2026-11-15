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

from .collections import CollectionError
from .verification import DOUYIN_FIXED_USER_AGENT


MEDIA_DOMAINS = ("douyinvod.com", "douyincdn.com", "douyin.com", "bytecdn.cn", "byteimg.com", "ibytedtos.com", "iesdouyin.com")
MAX_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 3600


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
    raise CollectionError("视频媒体地址不在允许的抖音域名范围内。", "unsafe_media_url")


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


class LocalTranscriber:
    def __init__(self, root):
        self.root = Path(root)
        self.model = None
        self.model_name = None
        self.lock = threading.Lock()

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
            raise CollectionError("图文识别暂未包含在首版中。", "unsupported_content")
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
