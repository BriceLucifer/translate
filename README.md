# Live Subtitles

浏览器 Tab 音频 → 本地 Whisper（vLLM）→ 实时双语字幕浮层（Hy-MT2 本地翻译）。

```
Chrome 扩展 (tab 音频捕获, 16kHz PCM)
        │  WebSocket  ws://127.0.0.1:8765
        ▼
stt_server.py (segment 时间戳断句 / 提交+临时文本 / 异步翻译队列)
        │  HTTP (OpenAI 兼容)
        ▼
vLLM: whisper-large-v3-turbo (GPU) ──► llama-server: Hy-MT2-1.8B (GPU/CPU)
```

## 功能

- 实时字幕：0.5s 刷新，已确认 + 临时（斜体）双态文本
- 中英双语对照：上行原文、下行译文，临时字幕也实时翻译
- 按 Whisper segment 时间戳自动断句（背景音乐场景也稳定）
- 视频全屏字幕（自动挂进 Top Layer）
- 浮层可拖拽（顶部居中手柄）
- 识别语言自动检测，翻译目标默认中文

## 环境要求

- Python 3.12+，[uv](https://docs.astral.sh/uv/)
- Node.js 18+，npm
- NVIDIA GPU（8GB 显存可同跑 whisper-turbo + Hy-MT2-1.8B；翻译也可纯 CPU）
- [llama.cpp](https://github.com/ggml-org/llama.cpp/releases)（翻译服务；GPU 版选 cuda 预编译包）
- ffmpeg（仅测试客户端需要）
- Chrome / Edge

> Windows 用户：vLLM 不支持原生 Windows，请在 WSL2 里跑转写服务；llama-server 和 `stt_server.py` 可原生跑（WSL2 localhost 自动转发到 Windows）。

## 安装

```bash
# 1. 转写模型环境（WSL2 / Linux）
uv venv --python 3.12 ~/serve/.venv
source ~/serve/.venv/bin/activate
uv pip install 'vllm[audio]'        # 注意必须带 [audio]，否则音频解析 400

# 2. 桥接后端
cd server && uv sync

# 3. 浏览器扩展
cd extension && npm install && npm run build   # 产物在 extension/dist
```

## 启动（三个终端）

```bash
# T1: 转写服务（首次下载模型 ~3GB）
vllm serve openai/whisper-large-v3-turbo --port 8000

# WSL2 额外需要：
#   VLLM_WSL2_ENABLE_PIN_MEMORY=1   启用 pinned memory（否则报 UVA is not available）
#   --gpu-memory-utilization 0.5    8GB 显卡给翻译模型和桌面留显存
VLLM_WSL2_ENABLE_PIN_MEMORY=1 vllm serve openai/whisper-large-v3-turbo \
    --port 8000 --gpu-memory-utilization 0.5

# T2: 翻译服务（官方 GGUF，首次下载 ~1.1GB；-ngl 99 用 GPU，翻译 ~1s -> ~0.2s）
llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M --port 8001 -c 4096 -ngl 99

# T3: 桥接后端
cd server
uv run stt_server.py --mt-url http://127.0.0.1:8001
# 不加 --mt-url 则仅转写不翻译
```

## 使用

1. `chrome://extensions` → 打开开发者模式 → 加载已解压的扩展程序 → 选 `extension/dist`
2. 打开任意有声音的 Tab（YouTube / B站 / 会议页面…）
3. 点扩展图标 → 默认「自动检测 → 中文」→ 开始字幕
4. 拖动浮层顶部的小横条可移动字幕位置；视频全屏时字幕自动跟随

修改扩展代码后：`npm run watch` 自动重建，然后在扩展页面点刷新。

## 无浏览器链路测试

```bash
cd server
uv run test_client.py testdata/jfk_en.wav                  # 英文（自动检测 -> 默认翻中文）
uv run test_client.py testdata/asr_example_zh.wav --target en
```

终端实时打印 `[partial]` 临时字幕和 `[COMMIT]` / `[TRANS]` 确认字幕。

## stt_server.py 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8765 | WebSocket 监听端口 |
| `--vllm` | http://127.0.0.1:8000 | 转写服务地址 |
| `--model` | openai/whisper-large-v3-turbo | 转写模型（可换 small/medium/large-v3） |
| `--mt-url` | 空（关闭） | 翻译服务地址 |
| `--mt-model` | tencent/Hy-MT2-1.8B | 翻译模型 |
| `--default-target` | zh | 客户端未指定翻译目标时的默认语言 |

## 实时性调参（stt_server.py 顶部常量）

| 常量 | 默认 | 说明 |
|---|---|---|
| `MAX_UNCOMMITTED` | 10s | 转写窗口上限（也是未提交音频上限），超出强制提交 |
| `PARTIAL_EVERY` | 0.5s | 刷新间隔 |
| `COMMIT_MARGIN` | 0.5s | segment 结尾距窗口尾超过该值即提交 |
| `FORCE_MARGIN` | 0.2s | 强制提交时的边距 |

断句机制：每 0.5s 用 `verbose_json` 转写当前窗口，segment 结尾距窗口尾 ≥0.5s 的视为成熟立即提交并翻译，剩余作为临时字幕；翻译走独立异步队列，不阻塞识别。

## WebSocket 协议

- 客户端 → 文本：`{"event":"start","language":"zh"|null,"target":"zh"|""|null}`（`""` = 不翻译，缺省 = 服务端 `--default-target`）
- 客户端 → 二进制：16kHz mono int16 PCM 帧
- 服务端 → 文本：`{"type":"subtitle","committed":"...","partial":"...","committed_tr":"...","partial_tr":"..."}`

## 目录结构

```
server/stt_server.py    桥接后端（uv 管理）
server/test_client.py   链路测试客户端
extension/src/          扩展源码（background/offscreen/content/popup）
extension/dist/         构建产物，浏览器加载这个目录
extension/build.mjs     esbuild 构建脚本
UPDATE.md               性能分析与优化路线图
```
