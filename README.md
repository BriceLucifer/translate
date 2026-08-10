# Live Subtitles

浏览器 Tab 音频 → 本地 Whisper（vLLM）→ 实时字幕浮层，可选 Hunyuan MT 本地翻译，支持视频全屏显示。

```
Chrome 扩展 (tab 音频捕获, 16kHz PCM)
        │  WebSocket  ws://127.0.0.1:8765
        ▼
stt_server.py (VAD 切块 / 滑动窗口 / 提交+临时文本)
        │  HTTP (OpenAI 兼容)
        ▼
vLLM: whisper-large-v3-turbo ──► (可选) vLLM: Hunyuan-MT-7B 翻译
```

## 环境要求（Unix / Linux / WSL2）

- Python 3.12+，[uv](https://docs.astral.sh/uv/)
- Node.js 18+，npm
- NVIDIA GPU（转写 8GB 显存起步；翻译模型另算，也可用 CPU 推理服务替代）
- ffmpeg（仅测试客户端需要）
- Chrome / Edge

> Windows 用户：vLLM 不支持原生 Windows，请在 WSL2 里跑模型服务；`stt_server.py` 是轻量桥接，Windows / Linux / WSL 都能跑（WSL2 的 localhost 会自动转发到 Windows）。

## 安装

```bash
# 1. 模型服务环境（建议单独 venv）
uv venv --python 3.12 ~/serve/.venv
source ~/serve/.venv/bin/activate
uv pip install vllm

# 2. 桥接后端
cd server
uv sync

# 3. 浏览器扩展
cd extension
npm install
npm run build        # 产物在 extension/dist
```

## 启动（按顺序，三个终端）

```bash
# T1: 转写服务（首次会下载模型 ~3GB）
vllm serve openai/whisper-large-v3-turbo --port 8000

# WSL2 额外需要两个参数：
#   VLLM_WSL2_ENABLE_PIN_MEMORY=1   启用 pinned memory（否则报 UVA is not available）
#   --gpu-memory-utilization 0.7    8GB 显卡避免和桌面/浏览器抢显存
VLLM_WSL2_ENABLE_PIN_MEMORY=1 vllm serve openai/whisper-large-v3-turbo \
    --port 8000 --gpu-memory-utilization 0.7

# T2: 翻译服务（可选；官方 GGUF + llama-server，一条命令）
#     首次自动下载 Q4_K_M 量化模型 ~1.1GB
llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M --port 8001 -c 4096
#     有 GPU 时加 -ngl 99 全量 offload（翻译延迟 ~1s -> ~0.2s，实时字幕强烈建议）
#     Windows 用 win-cuda 预编译包；注意给 Whisper 留显存（vLLM 侧 --gpu-memory-utilization 0.5）
llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M --port 8001 -c 4096 -ngl 99

# T3: 桥接后端
cd server
uv run stt_server.py                                    # 仅转写
uv run stt_server.py --mt-url http://127.0.0.1:8001     # 转写 + 翻译
```

## 使用

1. `chrome://extensions` → 打开开发者模式 → 加载已解压的扩展程序 → 选 `extension/dist`
2. 打开任意有声音的 Tab（YouTube / B站 / 会议页面…）
3. 点扩展图标 → 选「识别语言」和「翻译成」（不翻译选"不翻译"）→ 开始字幕
4. 字幕浮层显示在页面底部；**视频全屏也能看到**（自动挂进 Top Layer）

修改扩展代码后：`npm run watch` 自动重建，然后在扩展页面点刷新。

## 无浏览器链路测试

```bash
cd server
uv run test_client.py testdata/jfk_en.wav                  # 英文
uv run test_client.py testdata/asr_example_zh.wav --lang zh --target en
```

终端会实时打印 `[partial]` 临时字幕和 `[COMMIT]` / `[TRANS]` 确认字幕。

## stt_server.py 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8765 | WebSocket 监听端口 |
| `--vllm` | http://127.0.0.1:8000 | 转写服务地址 |
| `--model` | openai/whisper-large-v3-turbo | 转写模型（可换 small/medium/large-v3） |
| `--mt-url` | 空（关闭） | 翻译服务地址 |
| `--mt-model` | tencent/Hunyuan-MT-7B | 翻译模型 |

## 实时性调参（stt_server.py 顶部常量）

| 常量 | 默认 | 说明 |
|---|---|---|
| `SILENCE_END` | 0.7s | 句尾静音超过该值即提交转写 |
| `MAX_UNCOMMITTED` | 10s | 未提交音频上限，强制切块 |
| `PARTIAL_EVERY` | 1.0s | 临时字幕刷新间隔 |
| `PARTIAL_MAX` | 8.0s | 临时字幕回看的最大音频长度 |

典型端到端延迟 0.5–2s（取决于显卡与模型大小）。

## WebSocket 协议

- 客户端 → 文本：`{"event":"start","language":"zh"|null,"target":"zh"|null}`
- 客户端 → 二进制：16kHz mono int16 PCM 帧
- 服务端 → 文本：`{"type":"subtitle","committed":"...","partial":"...","committed_tr":"..."}`

## 目录结构

```
server/stt_server.py    桥接后端（uv 管理）
server/test_client.py   链路测试客户端
extension/src/          扩展源码（background/offscreen/content/popup）
extension/dist/         构建产物，浏览器加载这个目录
extension/build.mjs     esbuild 构建脚本
```
