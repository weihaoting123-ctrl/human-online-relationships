/* Core services. Optional workspaces consume these; they never own navigation.
 * Only view preferences may use browser storage. Contacts, notes, and tags stay
 * behind the same-origin local API and are never persisted by this client.
 */
const state = {
  currentView: 'catalog', bundles: [], activeBundle: null,
  selectedSince: null, selectedFile: null, loadSequence: 0, stateSequence: 0,
  catalogLoaded: false, catalogPhase: 'loading', interactionEpoch: 0,
  importRevision: 0, importBusy: false, importResult: null,
  searchSequence: 0, searchController: null, matches: null, searchPhase: 'idle',
  filters: { query: '', mode: 'names', kind: 'all', topic: 'all', tag: 'all', from: '', to: '', sort: 'recent_desc', page: 1, pageSize: 25 },
};
const $ = (selector) => document.querySelector(selector);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
function requestFailure(code, status = 0, write = false) {
  const messages = {
    network: '无法连接本机服务，请检查服务后刷新。',
    cancelled: '请求已取消，请刷新核对当前状态。',
    timeout: '等待本机服务超时，请刷新核对当前状态。',
    response: '本机服务响应无法读取，请刷新核对当前状态。',
    rejected: '本机服务未能完成请求，请检查后刷新。',
  };
  const error = new Error(messages[code] || messages.response);
  Object.assign(error, { code, status, outcomeUnknown: write && code !== 'rejected' });
  if (code === 'cancelled') error.name = 'AbortError';
  return error;
}
async function api(path, options = {}) {
  const write = !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase());
  let response;
  try {
    response = await fetch(path, {
      ...options, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    });
  } catch (error) {
    throw requestFailure(error.name === 'AbortError' ? 'cancelled' : error.name === 'TimeoutError' ? 'timeout' : 'network', 0, write);
  }
  let payload;
  try { payload = await response.json(); }
  catch (error) {
    throw requestFailure(error.name === 'AbortError' ? 'cancelled' : error.name === 'TimeoutError' ? 'timeout' : 'response', response.status, write);
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw requestFailure('response', response.status, write);
  }
  if (!response.ok || payload.status === 'error') {
    const error = requestFailure('rejected', response.status, write);
    // The protected local API supplies Chinese business errors (including 409
    // conflicts). Never reflect proxy HTML, exception text, or arbitrary JSON.
    const message = payload.error;
    if (typeof message === 'string' && message.length <= 240 && /[\u4e00-\u9fff]/.test(message)
        && !/[<>/\\{}\[\]\x00-\x1f]/.test(message)
        && !/[a-z]/i.test(message.replace(/\b(?:API|Key|Windows|JSON|HTTP|Markdown|\d*(?:KB|MB|GB))\b/gi, ''))) {
      error.message = message;
    }
    throw error;
  }
  return payload;
}
function scrollBehavior() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
}
function showNotice(message, isError = false) {
  const notice = $('#notice');
  notice.textContent = message;
  notice.classList.toggle('is-error', isError);
  notice.setAttribute('role', isError ? 'alert' : 'status');
  notice.hidden = false;
  window.clearTimeout(showNotice.timer);
  if (!isError) showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, 5000);
}
function setBusy(button, busy, label) {
  if (!button) return;
  // A running operation can finish while its module is disabled. Preserve the
  // latest operation state so re-enabling does not restore an obsolete lock.
  if (button.dataset.moduleDisabled) button.dataset.preModuleDisabled = String(busy);
  if (busy) {
    button.dataset.originalLabel = button.textContent;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalLabel || button.textContent;
    button.disabled = Boolean(button.dataset.moduleOperation && window.ArchiveShell && !ArchiveShell.isEnabled(button.dataset.moduleOperation));
  }
}
function number(value, digits = 0) {
  const parsed = Number(value || 0);
  return (Number.isFinite(parsed) ? parsed : 0).toLocaleString('zh-CN', { maximumFractionDigits: digits });
}
function formatLocalTime(value) {
  if (!value) return '尚无记录';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '尚无记录';
  return parsed.toLocaleString('zh-CN', { hour12: false });
}
function displayName(bundle) {
  return bundle?.library?.alias || bundle?.alias || bundle?.contact || bundle?.source?.contact || '未命名会话';
}
window.ArchiveCore = Object.freeze({ state, $, el, api, showNotice, setBusy, number, formatLocalTime, displayName });
