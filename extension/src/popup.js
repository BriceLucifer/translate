const toggleBtn = document.getElementById('toggle');
const statusEl = document.getElementById('status');
const langSel = document.getElementById('lang');
const targetSel = document.getElementById('target');

let capturing = false;

function refresh() {
  toggleBtn.textContent = capturing ? '停止字幕' : '开始字幕';
  toggleBtn.className = capturing ? 'stop' : '';
  langSel.disabled = targetSel.disabled = capturing;
}

chrome.storage.local.get(['language', 'target', 'prefsV2', 'backendStatus'], (s) => {
  // 旧版本可能存过空 target（不翻译），prefsV2 之前一律用 HTML 默认值（自动检测 -> 中文）
  if (s.prefsV2) {
    if (s.language !== undefined) langSel.value = s.language;
    if (s.target !== undefined) targetSel.value = s.target;
  }
  setStatus(s.backendStatus);
});

chrome.runtime.sendMessage({ type: 'status' }, (res) => {
  capturing = !!(res && res.capturing);
  refresh();
});

function setStatus(st) {
  if (capturing) {
    if (st === 'connected') { statusEl.textContent = '已连接本地后端'; statusEl.className = 'ok'; }
    else if (st === 'error') { statusEl.textContent = '后端连接失败（ws://127.0.0.1:8765）'; statusEl.className = 'err'; }
    else { statusEl.textContent = '连接中…'; statusEl.className = ''; }
  } else {
    statusEl.textContent = '';
    statusEl.className = '';
  }
}

toggleBtn.addEventListener('click', () => {
  if (capturing) {
    chrome.runtime.sendMessage({ type: 'stop' }, () => {
      capturing = false;
      setStatus(null);
      refresh();
    });
  } else {
    const language = langSel.value;
    const target = targetSel.value;
    chrome.storage.local.set({ language, target, prefsV2: true });
    chrome.runtime.sendMessage({ type: 'start', language, target }, (res) => {
      if (res && res.ok) {
        capturing = true;
        setStatus('connecting');
      } else {
        statusEl.textContent = '启动失败：' + (res && res.error ? res.error : '未知错误');
        statusEl.className = 'err';
      }
      refresh();
    });
  }
});
