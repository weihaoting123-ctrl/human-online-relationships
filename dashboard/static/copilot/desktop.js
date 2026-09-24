/* Browser-only, explicit local launch. No archive, model or native grants. */
(() => {
  'use strict';
  if (window.copilotDesktop) return;
  const card = document.querySelector('#desktop-launch-card');
  const button = document.querySelector('#desktop-launch');
  const status = document.querySelector('#desktop-launch-status');
  if (!card || !button || !status) return;
  card.hidden = false;
  let busy = false;
  const messages = Object.freeze({
    DESKTOP_UNSUPPORTED: '桌面悬浮目前仅支持 Windows；仍可使用下方的浏览器助手。',
    DESKTOP_NOT_INSTALLED: '本机桌面组件未安装。此按钮不会下载依赖，请先完成本地部署。',
    DESKTOP_UNSAFE_PATH: '桌面组件位置校验未通过，已阻止启动。请检查本地部署。',
    DESKTOP_STOPPED: '已就绪，一键打开。先在设置窗口点击“跟随微信”，再切回微信。',
    DESKTOP_RUNNING: '助手已在运行。点击打开现有设置窗口，不会重复创建助手。',
    DESKTOP_STATUS_UNKNOWN: '暂时无法确认桌面助手状态；不会自动启动。',
    DESKTOP_PROBE_FAILED: '暂时无法确认桌面助手状态；不会自动重试。',
    DESKTOP_PROBE_TIMEOUT: '状态检查超时，无法确认是否在运行；不会自动重试。',
    DESKTOP_LAUNCH_REQUESTED: '已请求打开设置窗口。请点击窗口中的“跟随微信”，再切回微信。',
    DESKTOP_LAUNCH_PENDING: '打开请求正在处理中，请稍候查看桌面；不会重复启动。',
    DESKTOP_LAUNCH_FAILED: '未能打开桌面助手。请检查本地组件；不会自动重试。'
  });
  function render(value) {
    if (!value || typeof value.supported !== 'boolean' || typeof value.installed !== 'boolean' ||
        !['running', 'stopped', 'unknown', 'unavailable', 'launch_requested'].includes(value.state) ||
        !Object.hasOwn(messages, value.code)) throw new Error('invalid_status');
    button.disabled = busy || !value.supported || !value.installed || value.state === 'unavailable';
    button.textContent = ['running', 'launch_requested'].includes(value.state) ? '打开悬浮助手' : '启动悬浮助手';
    status.textContent = messages[value.code];
  }
  async function request(launch) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(launch ? '/api/copilot/desktop/launch' : '/api/copilot/desktop', {
        method: launch ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
        ...(launch ? { headers: { 'Content-Type': 'application/json' }, body: '{}' } : {}),
        signal: controller.signal
      });
      if (!response.ok) throw new Error('request_failed');
      const data = await response.json();
      if (data.status !== 'ok') throw new Error('invalid_status');
      return data.desktop;
    } finally { clearTimeout(timer); }
  }
  function fail(launch) {
    button.disabled = false;
    status.textContent = launch
      ? '打开请求未得到确认，窗口可能已启动。请先查看桌面；本次不会自动重试。'
      : '无法确认桌面助手状态，请检查本机服务。不会自动启动或重试。';
  }
  button.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    status.textContent = '正在打开桌面助手…';
    try {
      const value = await request(true);
      busy = false;
      render(value);
    } catch (_) { fail(true); }
    finally { busy = false; button.removeAttribute('aria-busy'); }
  });
  // Readiness only: loading or refreshing this page never launches a process.
  request(false).then(render).catch(() => fail(false));
})();
