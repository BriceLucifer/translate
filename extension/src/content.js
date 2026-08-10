// Content script：字幕浮层。支持双语显示与视频全屏（fullscreenchange 时把节点挂进 Top Layer）。

const OVERLAY_ID = 'live-subtitles-overlay';
let overlay = null;
let origEl = null;
let transEl = null;

function ensureOverlay() {
  if (overlay) return;
  overlay = document.createElement('div');
  overlay.id = OVERLAY_ID;
  overlay.style.display = 'none';

  // 拖拽手柄（浮层本身不拦截点击，只能拖这里移动）
  const grip = document.createElement('div');
  grip.className = 'ls-grip';
  grip.textContent = '⠿';
  grip.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    const rect = overlay.getBoundingClientRect();
    const dx = e.clientX - rect.left;
    const dy = e.clientY - rect.top;
    overlay.style.left = rect.left + 'px';
    overlay.style.top = rect.top + 'px';
    overlay.style.bottom = 'auto';
    overlay.style.transform = 'none';
    const move = (ev) => {
      overlay.style.left = Math.max(0, ev.clientX - dx) + 'px';
      overlay.style.top = Math.max(0, ev.clientY - dy) + 'px';
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', () => grip.removeEventListener('pointermove', move), { once: true });
  });
  overlay.appendChild(grip);

  origEl = document.createElement('div');
  origEl.className = 'ls-line ls-orig';
  transEl = document.createElement('div');
  transEl.className = 'ls-line ls-trans';

  // 上行原文（英语），下行译文（中文）
  overlay.appendChild(origEl);
  overlay.appendChild(transEl);
  document.documentElement.appendChild(overlay);

  // 全屏时把浮层挂到全屏元素内（否则被 Top Layer 盖住）
  document.addEventListener('fullscreenchange', () => {
    const host = document.fullscreenElement;
    (host || document.documentElement).appendChild(overlay);
  });
}

function lastSentences(text, n) {
  if (!text) return '';
  const parts = text.split(/(?<=[。！？.!?,，、;；:：])\s*/).filter(Boolean);
  return parts.slice(-n).join(' ');
}

function renderSubtitle(msg) {
  ensureOverlay();
  const orig = (lastSentences(msg.committed, 2) + ' ' + (msg.partial || '')).trim();
  origEl.textContent = orig;
  origEl.classList.toggle('ls-has-partial', !!msg.partial);

  if (msg.committed_tr !== undefined) {
    const tr = (lastSentences(msg.committed_tr, 2) + ' ' + (msg.partial_tr || '')).trim();
    transEl.textContent = tr;
    transEl.classList.toggle('ls-has-partial', !!msg.partial_tr);
    transEl.style.display = tr ? '' : 'none';
  } else {
    transEl.style.display = 'none';
  }

  // 原文和译文都为空时隐藏
  overlay.style.display = (orig || transEl.textContent) ? '' : 'none';
}

function setVisible(visible) {
  ensureOverlay();
  overlay.style.display = visible ? '' : 'none';
  if (!visible) {
    origEl.textContent = '';
    transEl.textContent = '';
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'subtitle') renderSubtitle(msg);
  if (msg.type === 'overlay') setVisible(msg.visible);
});
