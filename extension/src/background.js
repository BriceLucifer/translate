// Service worker：编排 tabCapture、offscreen 文档，并把字幕转发给 content script。

let captureTabId = null;

async function ensureOffscreen() {
  const has = await chrome.offscreen.hasDocument();
  if (has) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: 'Capture tab audio for real-time transcription',
  });
}

async function startCapture(tabId, language, target) {
  if (captureTabId !== null) await stopCapture();
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
  await ensureOffscreen();
  captureTabId = tabId;
  chrome.runtime.sendMessage({ type: 'start-capture', streamId, language, target });
  // 通知该 tab 的 content script 显示浮层
  chrome.tabs.sendMessage(tabId, { type: 'overlay', visible: true }).catch(() => {});
}

async function stopCapture() {
  if (captureTabId === null) return;
  const tabId = captureTabId;
  captureTabId = null;
  chrome.runtime.sendMessage({ type: 'stop-capture' });
  chrome.tabs.sendMessage(tabId, { type: 'overlay', visible: false }).catch(() => {});
  if (await chrome.offscreen.hasDocument()) {
    // 等 offscreen 收尾后再关
    setTimeout(() => chrome.offscreen.closeDocument().catch(() => {}), 500);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'start') {
    chrome.tabs.query({ active: true, currentWindow: true }, async ([tab]) => {
      try {
        await startCapture(tab.id, msg.language || null, msg.target || null);
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    });
    return true; // 异步 sendResponse
  }
  if (msg.type === 'stop') {
    stopCapture().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === 'status') {
    sendResponse({ capturing: captureTabId !== null });
    return false;
  }
  // offscreen -> 目标 tab 的字幕/状态转发
  if (msg.type === 'subtitle' && captureTabId !== null) {
    chrome.tabs.sendMessage(captureTabId, msg).catch(() => {});
  }
  if (msg.type === 'backend-status') {
    chrome.storage.local.set({ backendStatus: msg.status });
  }
});

// tab 关闭或跳转时停止采集
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === captureTabId) stopCapture();
});
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (tabId === captureTabId && info.status === 'loading') stopCapture();
});
