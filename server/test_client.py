"""链路测试客户端：把任意音频文件按实时节奏推给 stt_server，打印字幕。

用法：
    uv run test_client.py testdata/jfk_en.wav
    uv run test_client.py testdata/asr_example_zh.wav --lang zh --target en
"""

import argparse
import asyncio
import json
import subprocess
import sys

import websockets

CHUNK_SECONDS = 0.25
CHUNK_BYTES = int(16000 * CHUNK_SECONDS) * 2  # int16


def decode_to_pcm(path: str) -> bytes:
    """用 ffmpeg 把任意音频解码为 16kHz mono s16le。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.exit(f"ffmpeg decode failed: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("--ws", default="ws://127.0.0.1:8765")
    p.add_argument("--lang", default=None)
    p.add_argument("--target", default=None)
    p.add_argument("--fast", action="store_true", help="不按实时节奏，尽快发送")
    args = p.parse_args()

    pcm = decode_to_pcm(args.audio)
    duration = len(pcm) / 32000
    print(f"audio: {args.audio} ({duration:.1f}s) -> {args.ws}")

    async with websockets.connect(args.ws, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"event": "start", "language": args.lang, "target": args.target}))
        committed_len = 0
        done = asyncio.Event()

        async def receiver():
            nonlocal committed_len
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "subtitle":
                    continue
                new = msg["committed"][committed_len:]
                committed_len = len(msg["committed"])
                if new:
                    print(f"\n[COMMIT] {new}")
                    if msg.get("committed_tr"):
                        print(f"[TRANS ] {msg['committed_tr']}")
                if msg.get("partial"):
                    print(f"[partial] {msg['partial']}   ", end="\r", flush=True)

        recv_task = asyncio.create_task(receiver())

        for i in range(0, len(pcm), CHUNK_BYTES):
            await ws.send(pcm[i:i + CHUNK_BYTES])
            if not args.fast:
                await asyncio.sleep(CHUNK_SECONDS)
        await ws.send(json.dumps({"event": "stop"}))

        # 等最后一批字幕回来
        await asyncio.sleep(3)
        recv_task.cancel()
        print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
