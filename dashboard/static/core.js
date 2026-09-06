/* Core services. Optional workspaces consume these; they never own navigation.
 * Only view preferences may use browser storage. Contacts, notes, and tags stay
 * behind the same-origin local API and are never persisted by this client.
 */
const state = {
  currentView: 'catalog', bundles: [], activeBundle: null,
  selectedSince: null, selectedFile: null, loadSequence: 0, stateSequence: 0,
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
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok || payload.status === 'error') {
    const error = new Error(payload.error || `本地服务返回 ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
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
