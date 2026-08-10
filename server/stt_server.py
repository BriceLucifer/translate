"""实时字幕后端：WebSocket 收 PCM 音频 -> VAD 切块 -> vLLM (OpenAI 兼容接口) 转写 -> 推送字幕。

依赖一个已启动的 vLLM 转写服务，例如：
    vllm serve openai/whisper-large-v3-turbo --task transcription

可选：再启动一个 vLLM 翻译服务（如 Hunyuan MT），加 --mt-url 启用双语字幕：
    vllm serve tencent/Hunyuan-MT-7B --port 8001

协议（ws://127.0.0.1:8765）：
  客户端 -> 文本 JSON: {"event": "start", "language": "zh"|null, "target": "zh"|null}
  客户端 -> 二进制: 16kHz mono int16 PCM 帧
  服务端 -> 文本 JSON: {"type": "subtitle", "committed": "...", "partial": "...",
                        "committed_tr": "..."}   # 仅在启用翻译时带 committed_tr
"""

import argparse
import asyncio
import io
import json
import logging
import wave

import httpx
import numpy as np
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stt")

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

# 切块策略参数（秒）
MIN_SPEECH = 0.5          # 短于此的语音段忽略
MAX_UNCOMMITTED = 10.0    # 未提交音频上限（也是转写窗口），超出强制提交
PARTIAL_EVERY = 1.0       # 每积累这么多新语音刷新一次
COMMIT_MARGIN = 0.5       # segment 结尾距窗口尾超过该值即视为成熟，可提交
FORCE_MARGIN = 0.2        # 强制提交时的边距

LANG_NAMES = {"zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语"}


def pcm_bytes_to_float(buf: bytes) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0


def to_wav_bytes(samples: np.ndarray) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32768.0).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return out.getvalue()


class Session:
    def __init__(self, ws, http: httpx.AsyncClient, vllm_url: str, model: str,
                 mt_url: str, mt_model: str):
        self.ws = ws
        self.http = http
        self.vllm_url = vllm_url.rstrip("/")
        self.model = model
        self.mt_url = mt_url.rstrip("/") if mt_url else ""
        self.mt_model = mt_model
        self.language = None
        self.target = None
        self.buffer = bytearray()       # 未提交音频（int16 bytes）
        self.committed = ""             # 已确认文本
        self.committed_tr = ""          # 已确认译文
        self.partial = ""
        self.new_audio_since_partial = 0.0
        self.flush_all = False          # stop 时提交全部
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.pending_final = False

    async def send_subtitle(self):
        msg = {
            "type": "subtitle",
            "committed": self.committed,
            "partial": self.partial,
        }
        if self.mt_url and self.target:
            msg["committed_tr"] = self.committed_tr
        await self.ws.send(json.dumps(msg, ensure_ascii=False))

    async def transcribe(self, samples: np.ndarray) -> dict:
        data = {"model": self.model, "response_format": "verbose_json", "temperature": "0"}
        if self.language:
            data["language"] = self.language
        files = {"file": ("audio.wav", to_wav_bytes(samples), "audio/wav")}
        resp = await self.http.post(
            f"{self.vllm_url}/v1/audio/transcriptions", data=data, files=files
        )
        resp.raise_for_status()
        return resp.json()

    async def translate(self, text: str) -> str:
        """调用 vLLM 起的翻译模型（OpenAI chat completions 兼容）。"""
        if not self.mt_url or not self.target:
            return ""
        if self.language and self.language == self.target:
            return text
        target_name = LANG_NAMES.get(self.target, self.target)
        # Hy-MT2 官方默认翻译指令格式
        prompt = f"将以下文本翻译成{target_name}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
        resp = await self.http.post(
            f"{self.mt_url}/v1/chat/completions",
            json={
                "model": self.mt_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 512,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def enqueue(self, final: bool):
        """串行处理：同一时间只跑一个转写；期间若需要提交，记 pending_final。"""
        if self.task and not self.task.done():
            if final:
                self.pending_final = True
            return
        self.task = asyncio.create_task(self._process(final))

    async def _process(self, final: bool):
        async with self.lock:
            try:
                await self._step(final)
                while self.pending_final:
                    self.pending_final = False
                    await self._step(True)
            except Exception as e:
                log.warning("transcribe failed: %s", e)

    async def _step(self, force: bool):
        """按 Whisper segment 时间戳断句：成熟 segment 提交，其余作临时字幕。"""
        dur = len(self.buffer) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        if dur < MIN_SPEECH:
            return
        window = min(dur, MAX_UNCOMMITTED)
        samples = pcm_bytes_to_float(bytes(self.buffer[: int(window * SAMPLE_RATE * 2)]))
        data = await self.transcribe(samples)
        segs = data.get("segments") or []

        if not segs:
            # 无语音（静音/纯噪声）：丢掉旧音频只留 1 秒上下文
            if dur > 4.0:
                del self.buffer[: int((dur - 1.0) * SAMPLE_RATE * 2)]
            self.partial = ""
            await self.send_subtitle()
            return

        cutoff = window if self.flush_all else window - (FORCE_MARGIN if force else COMMIT_MARGIN)
        commit = [s for s in segs if s.get("end", 0) <= cutoff]
        rest = segs[len(commit):]
        if not commit and force:
            # 强制提交：至少把最旧的 segment 结掉，防止缓冲无限增长
            if len(segs) >= 2:
                commit, rest = segs[:-1], segs[-1:]
            else:
                commit, rest = segs[:1], []

        if commit:
            text = " ".join(s.get("text", "").strip() for s in commit).strip()
            end = min(commit[-1].get("end", 0), window)
            n = int(end * SAMPLE_RATE * 2)
            del self.buffer[: n - n % 2]
            if text:
                self.committed = (self.committed + " " + text).strip() if self.committed else text
                try:
                    tr = await self.translate(text)
                except Exception as e:
                    log.warning("translate failed: %s", e)
                    tr = ""
                if tr:
                    self.committed_tr = (self.committed_tr + " " + tr).strip() if self.committed_tr else tr
            log.info("COMMIT: %s", text)

        self.partial = " ".join(s.get("text", "").strip() for s in rest).strip()
        self.new_audio_since_partial = 0.0
        await self.send_subtitle()

    async def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        self.new_audio_since_partial += len(chunk) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        total = len(self.buffer) / (SAMPLE_RATE * BYTES_PER_SAMPLE)

        if total >= MAX_UNCOMMITTED + 1.0:
            self.enqueue(final=True)
        elif self.new_audio_since_partial >= PARTIAL_EVERY and total >= MIN_SPEECH:
            self.enqueue(final=False)


async def handler(ws, http, vllm_url, model, mt_url, mt_model):
    session = Session(ws, http, vllm_url, model, mt_url, mt_model)
    log.info("client connected: %s", ws.remote_address)
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                await session.feed(msg)
            else:
                cfg = json.loads(msg)
                if cfg.get("event") == "start":
                    session.language = cfg.get("language") or None
                    session.target = cfg.get("target") or None
                    log.info("session start, language=%s target=%s",
                             session.language, session.target)
                elif cfg.get("event") == "stop":
                    session.flush_all = True
                    session.enqueue(final=True)
    except websockets.ConnectionClosed:
        pass
    finally:
        if session.task:
            await asyncio.wait([session.task], timeout=5)
        log.info("client disconnected")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--vllm", default="http://127.0.0.1:8000", help="vLLM 转写服务地址")
    parser.add_argument("--model", default="openai/whisper-large-v3-turbo")
    parser.add_argument("--mt-url", default="", help="vLLM 翻译服务地址，留空关闭翻译")
    parser.add_argument("--mt-model", default="tencent/Hy-MT2-1.8B")
    args = parser.parse_args()

    http = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    async with websockets.serve(
        lambda ws: handler(ws, http, args.vllm, args.model, args.mt_url, args.mt_model),
        "127.0.0.1", args.port, max_size=8 * 1024 * 1024,
    ):
        log.info("STT WebSocket server on ws://127.0.0.1:%d -> %s (model=%s)",
                 args.port, args.vllm, args.model)
        if args.mt_url:
            log.info("translation enabled: %s (model=%s)", args.mt_url, args.mt_model)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
