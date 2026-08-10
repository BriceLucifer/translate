// Offscreen 文档：拿到 tab 音频流 -> AudioWorklet 转 16kHz int16 PCM -> WebSocket 发给本地后端。

const WS_URL = 'ws://127.0.0.1:8765';

let ws = null;
let audioCtx = null;
let mediaStream = null;
let workletNode = null;

function setStatus(status) {
  chrome.runtime.sendMessage({ type: 'backend-status', status });
}

async function startCapture(streamId, language, target) {
  await stopCapture();

  // tab 音频流（旧式 mandatory 约束是 tabCapture 的要求）
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
      },
    },
    video: false,
  });

  // 直接建 16kHz 的 AudioContext，浏览器负责重采样
  audioCtx = new AudioContext({ sampleRate: 16000 });
  const source = audioCtx.createMediaStreamSource(mediaStream);

  await audioCtx.audioWorklet.addModule('pcm-worklet.js');
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-capture');

  source.connect(workletNode);
  // 采集后 tab 声音会走这里，接回输出让用户继续听到声音
  source.connect(audioCtx.destination);

  ws = new WebSocket(WS_URL);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    setStatus('connected');
    ws.send(JSON.stringify({ event: 'start', language, target }));
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'subtitle') chrome.runtime.sendMessage(msg);
  };
  ws.onerror = () => setStatus('error');
  ws.onclose = () => setStatus('closed');

  workletNode.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(e.data); // int16 PCM ArrayBuffer
    }
  };
}

async function stopCapture() {
  if (ws) {
    try {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ event: 'stop' }));
      ws.close();
    } catch (_) {}
    ws = null;
  }
  if (workletNode) { workletNode.disconnect(); workletNode = null; }
  if (audioCtx) { await audioCtx.close().catch(() => {}); audioCtx = null; }
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'start-capture') {
    startCapture(msg.streamId, msg.language).catch((e) => {
      console.error('capture failed', e);
      setStatus('error');
    });
  }
  if (msg.type === 'stop-capture') stopCapture();
});
