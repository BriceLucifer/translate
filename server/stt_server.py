"""实时字幕后端：WebSocket 收 PCM 音频 -> vLLM 转写（segment 时间戳断句）-> 异步翻译 -> 推送字幕。

依赖服务：
    vllm serve openai/whisper-large-v3-turbo --port 8000
    llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M --port 8001 -ngl 99   # 可选，翻译

协议（ws://127.0.0.1:8765）：
  客户端 -> 文本 JSON: {"event": "start", "language": "zh"|null, "target": "zh"|""|null}
                      （target 缺省 = 服务端 --default-target；"" = 不翻译）
  客户端 -> 二进制: 16kHz mono int16 PCM 帧
  服务端 -> 文本 JSON: {"type": "subtitle", "committed": "...", "partial": "...",
                        "committed_tr": "...", "partial_tr": "..."}   # tr 字段仅启用翻译时带
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
PARTIAL_EVERY = 0.5       # 每积累这么多新语音刷新一次
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
        self.partial_tr = ""            # 临时字幕译文
        self._partial_tr_src = ""       # 上次翻译过的临时字幕原文
        self._partial_tr_task: asyncio.Task | None = None
        self.tr_queue: asyncio.Queue = asyncio.Queue()   # 已确认文本的翻译队列（串行保序）
        self.tr_worker: asyncio.Task | None = None
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
            msg["partial_tr"] = self.partial_tr
        await self.ws.send(json.dumps(msg, ensure_ascii=False))

    def _tr_enabled(self) -> bool:
        return bool(self.mt_url and self.target)

    async def _tr_loop(self):
        """已确认文本的翻译 worker：排队串行执行，不阻塞识别主流程。"""
        while True:
            text = await self.tr_queue.get()
            try:
                tr = await self.translate(text)
            except Exception as e:
                log.warning("translate failed: %s", e)
                continue
            if tr:
                self.committed_tr = (self.committed_tr + " " + tr).strip() if self.committed_tr else tr
                try:
                    await self.send_subtitle()
                except websockets.ConnectionClosed:
                    return

    def queue_commit_translation(self, text: str):
        if not self._tr_enabled() or not text:
            return
        if self.tr_worker is None:
            self.tr_worker = asyncio.create_task(self._tr_loop())
        self.tr_queue.put_nowait(text)

    def queue_partial_translation(self):
        """临时字幕翻译：同一时刻只跑一个，文本没变就不翻。"""
        if not self._tr_enabled() or not self.partial:
            return
        if self.partial == self._partial_tr_src:
            return
        if self._partial_tr_task and not self._partial_tr_task.done():
            return
        src = self.partial
        self._partial_tr_src = src

        async def run():
            try:
                tr = await self.translate(src)
            except Exception:
                return
            # 结果过期（期间有了新 partial 或发生了提交）就丢弃
            if self._partial_tr_src == src and tr:
                self.partial_tr = tr
                try:
                    await self.send_subtitle()
                except websockets.ConnectionClosed:
                    pass

        self._partial_tr_task = asyncio.create_task(run())

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
                self.queue_commit_translation(text)
            log.info("COMMIT: %s", text)
            # 提交后旧临时译文作废
            self.partial_tr = ""
            self._partial_tr_src = ""

        self.partial = " ".join(s.get("text", "").strip() for s in rest).strip()
        self.new_audio_since_partial = 0.0
        await self.send_subtitle()
        self.queue_partial_translation()

    async def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        self.new_audio_since_partial += len(chunk) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        total = len(self.buffer) / (SAMPLE_RATE * BYTES_PER_SAMPLE)

        if total >= MAX_UNCOMMITTED + 1.0:
            self.enqueue(final=True)
        elif self.new_audio_since_partial >= PARTIAL_EVERY and total >= MIN_SPEECH:
            self.enqueue(final=False)


async def handler(ws, http, vllm_url, model, mt_url, mt_model, default_target):
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
                    # 客户端没传 target（旧版扩展）时按服务端默认；显式传 "" 表示不翻译
                    t = cfg.get("target")
                    session.target = (t or None) if t is not None else default_target
                    log.info("session start, language=%s target=%s",
                             session.language, session.target)
                elif cfg.get("event") == "stop":
                    session.flush_all = True
                    session.enqueue(final=True)
    except websockets.ConnectionClosed:
        pass
    finally:
        for t in (session.tr_worker, session._partial_tr_task):
            if t:
                t.cancel()
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
    parser.add_argument("--default-target", default="zh", help="客户端未指定翻译目标时的默认语言")
    args = parser.parse_args()

    http = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    async with websockets.serve(
        lambda ws: handler(ws, http, args.vllm, args.model, args.mt_url, args.mt_model,
                           args.default_target),
        "127.0.0.1", args.port, max_size=8 * 1024 * 1024,
    ):
        log.info("STT WebSocket server on ws://127.0.0.1:%d -> %s (model=%s)",
                 args.port, args.vllm, args.model)
        if args.mt_url:
            log.info("translation enabled: %s (model=%s)", args.mt_url, args.mt_model)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
