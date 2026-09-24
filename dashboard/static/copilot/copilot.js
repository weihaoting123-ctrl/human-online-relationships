/* Independent, memory-only reply assistant. Only an explicit confirmation sends
 * a run request. Native window identity is opaque and never identifies a chat. */
(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const bridge = window.copilotDesktop;
  const state = {
    revision: 0, conversations: [], modules: [], configured: false, loaded: false,
    preview: null, previewBusy: false, runBusy: false, loading: false, enabling: false,
    nativeTarget: null, nativeSeen: false, nativeSequence: 0, paused: false, compactReply: '',
  };
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const safeText = (value, limit = 4000) => typeof value === 'string' ? value.slice(0, limit) : '';
  const moduleById = (id) => state.modules.find((item) => item.id === id);
  const moduleEnabled = (id) => moduleById(id)?.enabled === true;
  const selected = () => state.conversations.find((item) => item.bundle_id === $('#contact-select').value);
  const dateValue = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : '';
  function feedback(message = '', error = false) {
    $('#feedback').textContent = message;
    $('#feedback').classList.toggle('is-error', error);
    $('#feedback').setAttribute('role', error ? 'alert' : 'status');
  }
  async function request(path, body) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), path.endsWith('/run') ? 130000 : 30000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok) throw new Error('request-rejected');
      const payload = await response.json();
      if (!payload || typeof payload !== 'object' || Array.isArray(payload) || payload.status !== 'ok') throw new Error('invalid-response');
      return payload;
    } finally { window.clearTimeout(timeout); }
  }
  function nativeCall(method, value) {
    if (typeof bridge?.[method] !== 'function') return;
    try { Promise.resolve(bridge[method](value)).catch(() => feedback('窗口控制暂不可用，请在桌面端重新打开助手。', true)); }
    catch (_) { feedback('窗口控制暂不可用，请在桌面端重新打开助手。', true); }
  }
  function currentScope() {
    return {
      bundle_id: $('#contact-select').value,
      date_from: $('#date-from').value, date_to: $('#date-to').value,
      max_messages: Number($('#max-messages').value), direction: $('#direction').value,
      latest_draft: $('#latest-draft').value, binding_revision: state.revision,
    };
  }
  function validScope() {
    const item = selected();
    const scope = currentScope();
    return Boolean(item && item.date_from && item.date_to && scope.date_from && scope.date_to
      && scope.date_from >= item.date_from && scope.date_to <= item.date_to && scope.date_from <= scope.date_to
      && Number.isInteger(scope.max_messages) && scope.max_messages >= 1 && scope.max_messages <= 200
      && ['closer', 'relaxed', 'invite'].includes(scope.direction) && scope.latest_draft.length <= 4000);
  }
  function ready() {
    return !state.paused && state.loaded && state.configured && moduleEnabled('copilot') && moduleEnabled('analysis') && validScope();
  }
  function renderControls() {
    $('#prepare-preview').disabled = !ready() || state.previewBusy || state.runBusy || state.loading || state.enabling;
    $('#prepare-preview').textContent = state.previewBusy ? '正在核对范围…' : state.runBusy ? '正在生成…' : '核对本次发送范围';
    $('#confirm-run').disabled = !ready() || !state.preview || state.previewBusy || state.runBusy || state.loading;
    $('#refresh-status').disabled = state.loading || state.enabling;
    $('#enable-copilot').disabled = state.enabling || state.loading || !moduleEnabled('analysis');
    $('#enable-copilot').hidden = !moduleById('copilot') || moduleEnabled('copilot');
    let message = '选择一个会话后，核对本次发送范围。';
    if (state.paused) message = '跟随已暂停，新的生成也已暂停。恢复后需重新核对范围。';
    else if (!state.loaded) message = '本机状态未就绪，请刷新状态。';
    else if (!moduleEnabled('analysis')) message = '请先在主控制台启用 AI 分析，再使用回复助手。';
    else if (!moduleById('copilot')) message = '当前服务尚未提供回复助手模块。';
    else if (!moduleEnabled('copilot')) message = '回复助手尚未启用。启用本身不会发送聊天或调用模型。';
    else if (!state.configured) message = '请在主控制台的 AI 分析中配置模型，再刷新状态。';
    else if (selected() && !validScope()) message = '请选择归档日期内的范围，条数上限为 200。';
    else if (selected()) message = '范围预览在本机完成，确认后才会调用模型。';
    $('#module-status').textContent = message;
  }
  function invalidate({ clearPerson = false, clearDraft = false } = {}) {
    state.revision += 1;
    state.preview = null;
    $('#consent-panel').hidden = true;
    $('#preview-facts').replaceChildren();
    $('#replies-list').replaceChildren();
    state.compactReply = '';
    $('#compact-reply').textContent = '';
    $('#compact-copy').textContent = '复制';
    $('#compact-suggestion').hidden = true;
    $('#topics-list').replaceChildren();
    $('#caveats').replaceChildren();
    $('#caveats').hidden = true;
    $('#replies-empty').hidden = false;
    $('#topics-empty').hidden = false;
    if (clearPerson) $('#contact-select').value = '';
    if (clearDraft || clearPerson) $('#latest-draft').value = '';
    if (clearPerson) renderContext(true);
    feedback();
    renderControls();
  }
  function renderContext(resetDates = false) {
    const item = selected();
    $('#context-heading').textContent = item?.name || '尚未选择会话';
    $('#compact-context').textContent = item ? `归档 · ${item.name}` : '选择一份归档，准备回复';
    $('#archive-freshness').textContent = item
      ? `归档记录截至 ${item.date_to || '未知日期'} · 非实时聊天`
      : '归档不是实时聊天，日期以已有记录为准。';
    for (const [id, key] of [['date-from', 'date_from'], ['date-to', 'date_to']]) {
      const field = $('#' + id);
      field.disabled = !item?.date_from || !item?.date_to;
      field.min = item?.date_from || '';
      field.max = item?.date_to || '';
      if (resetDates) field.value = item?.[key] || '';
    }
  }
  function normalizeConversations(items, library = false) {
    if (!Array.isArray(items)) return [];
    return items.filter((item) => item && typeof item.bundle_id === 'string'
      && !item.hidden_at && !item.source_missing).map((item) => ({
      bundle_id: item.bundle_id,
      name: safeText(item.alias || item.contact_display || item.source?.contact || '未命名会话', 512),
      date_from: dateValue(library ? item.source?.date_range?.[0] : item.date_from),
      date_to: dateValue(library ? item.source?.date_range?.[1] : item.date_to),
    }));
  }
  async function refresh() {
    if (state.loading || state.enabling) return;
    state.loading = true;
    invalidate();
    renderControls();
    try {
      const [status, modules] = await Promise.all([request('/api/copilot/status'), request('/api/modules')]);
      let items = normalizeConversations(status.conversations);
      if (!Array.isArray(status.conversations)) {
        const library = await request('/api/library');
        items = normalizeConversations(library.conversations, true);
      }
      const previous = $('#contact-select').value;
      state.conversations = items;
      state.modules = Array.isArray(modules.modules) ? modules.modules : [];
      state.configured = status.configured === true;
      state.loaded = true;
      const select = $('#contact-select');
      select.replaceChildren(make('option', '', '请选择一个会话'));
      select.firstElementChild.value = '';
      for (const item of items) {
        const option = make('option', '', item.name);
        option.value = item.bundle_id;
        select.append(option);
      }
      select.value = items.some((item) => item.bundle_id === previous) ? previous : '';
      if (!select.value) $('#latest-draft').value = '';
      renderContext(true);
    } catch (_) {
      state.loaded = false;
      state.configured = false;
      state.modules = [];
      feedback('无法读取本机状态。请检查服务后刷新；不会自动发送或重试。', true);
    } finally { state.loading = false; renderControls(); }
  }
  async function enable() {
    const module = moduleById('copilot');
    if (state.enabling || state.loading || !module || !moduleEnabled('analysis') || !Number.isInteger(module.version)) return;
    state.enabling = true;
    invalidate();
    renderControls();
    try {
      const response = await request('/api/modules', { id: 'copilot', enabled: true, expected_version: module.version });
      state.modules = Array.isArray(response.modules) ? response.modules : [];
      feedback('回复助手已启用。每次生成仍需单独确认。');
    } catch (_) { feedback('未能启用回复助手。请刷新状态后核对设置。', true); }
    finally { state.enabling = false; renderControls(); }
  }
  function validPreview(payload, scope) {
    return typeof payload.preview_id === 'string' && payload.preview_id.length > 0
      && payload.binding_revision === scope.binding_revision && payload.max_calls === 1
      && payload.scope && ['bundle_id', 'date_from', 'date_to', 'max_messages', 'direction'].every((key) => payload.scope[key] === scope[key])
      && payload.recipient?.configured === true && typeof payload.recipient?.model === 'string'
      && typeof payload.recipient?.provider === 'string' && typeof payload.recipient?.endpoint === 'string'
      && Number.isInteger(payload.counts?.sample_messages) && payload.counts.sample_messages >= 0
      && Number.isInteger(payload.counts?.sample_chars) && payload.counts.sample_chars >= 0
      && dateValue(payload.counts.sample_date_from) && dateValue(payload.counts.sample_date_to)
      && payload.counts.sample_date_from >= scope.date_from && payload.counts.sample_date_to <= scope.date_to
      && payload.counts.sample_date_from <= payload.counts.sample_date_to
      && Number.isInteger(payload.counts.omitted_messages) && payload.counts.omitted_messages >= 0
      && Number.isInteger(payload.counts.truncated_messages) && payload.counts.truncated_messages >= 0;
  }
  function renderPreview(payload) {
    const facts = $('#preview-facts');
    facts.replaceChildren();
    const append = (label, value) => facts.append(make('dt', '', label), make('dd', '', value));
    append('会话与所选日期', `${selected()?.name || ''} · ${payload.scope.date_from} 至 ${payload.scope.date_to}`);
    append('实际取用日期', `${payload.counts.sample_date_from} 至 ${payload.counts.sample_date_to}`);
    append('取用文字', `${payload.counts.sample_messages} 条归档 · ${payload.counts.sample_chars} 字符 · 补充 ${payload.counts.draft_chars || 0} 字符`);
    append('取用限制', `${payload.counts.omitted_messages} 条未取用 · ${payload.counts.truncated_messages} 条被截断`);
    append('接收方与模型', `${safeText(payload.recipient.provider, 80)} · ${safeText(payload.recipient.model, 120)}`);
    append('接收地址', safeText(payload.recipient.endpoint, 300));
    append('调用计划', '最多 1 次云端调用');
    $('#consent-panel').hidden = false;
  }
  async function prepare() {
    if (!ready() || state.previewBusy || state.runBusy || state.loading) return;
    invalidate();
    const scope = currentScope();
    state.previewBusy = true;
    renderControls();
    try {
      const payload = await request('/api/copilot/preview', scope);
      if (scope.binding_revision !== state.revision) return;
      if (!validPreview(payload, scope)) throw new Error('invalid-preview');
      state.preview = payload;
      renderPreview(payload);
      feedback('范围已核对。请检查接收方，决定是否发送这一次。');
    } catch (_) {
      if (scope.binding_revision === state.revision) feedback('无法准备发送范围。请检查本机设置后重新核对；尚未调用模型。', true);
    } finally { state.previewBusy = false; renderControls(); }
  }
  function validResult(payload) {
    const styles = ['natural', 'warm', 'invite'];
    return Array.isArray(payload.replies) && payload.replies.length === 3
      && styles.every((style) => payload.replies.filter((item) => item?.style === style).length === 1)
      && payload.replies.every((item) => typeof item.text === 'string' && item.text.length <= 4000 && typeof item.reason === 'string')
      && Array.isArray(payload.topics) && payload.topics.length <= 3
      && payload.topics.every((item) => typeof item?.title === 'string' && typeof item.text === 'string')
      && Array.isArray(payload.caveats) && payload.caveats.every((item) => typeof item === 'string');
  }
  async function copyText(value, button) {
    if (!value) return;
    const revision = state.revision;
    try {
      if (bridge) {
        if (typeof bridge.copy !== 'function') throw new Error('native-copy-unavailable');
        await bridge.copy(value);
      } else await navigator.clipboard.writeText(value);
      if (revision !== state.revision) return;
      button.textContent = '已复制';
      feedback('已复制。是否发送、怎样发送，由你决定。');
    } catch (_) {
      if (revision !== state.revision) return;
      button.textContent = '复制失败';
      feedback('无法复制到剪贴板。请再次点击复制，或展开后选中文字手动复制。', true);
    }
  }
  function renderResult(payload) {
    const labels = { natural: '自然一点', warm: '温暖一点', invite: '试着邀约' };
    $('#replies-empty').hidden = true;
    for (const reply of payload.replies) {
      const card = make('article', 'reply-card');
      const header = make('header');
      const copy = make('button', 'copy-button', '复制');
      copy.type = 'button';
      copy.setAttribute('aria-label', `复制${labels[reply.style]}的回复`);
      copy.addEventListener('click', () => copyText(reply.text, copy));
      header.append(make('span', 'style-label', labels[reply.style]), copy);
      card.append(header, make('p', 'reply-text', reply.text), make('p', 'reply-reason', safeText(reply.reason, 1000)));
      $('#replies-list').append(card);
    }
    $('#topics-empty').hidden = payload.topics.length > 0;
    for (const topic of payload.topics) {
      const card = make('article', 'topic-card');
      card.append(make('h2', '', safeText(topic.title, 200)), make('p', '', safeText(topic.text, 2000)));
      $('#topics-list').append(card);
    }
    for (const caveat of payload.caveats.slice(0, 4)) $('#caveats').append(make('li', '', safeText(caveat, 1000)));
    $('#caveats').hidden = payload.caveats.length === 0;
    state.compactReply = payload.replies.find((reply) => reply.style === 'natural').text;
    $('#compact-reply').textContent = state.compactReply;
    $('#compact-suggestion').hidden = false;
  }
  async function run() {
    if (!ready() || !state.preview || state.previewBusy || state.runBusy || state.loading) return;
    const preview = state.preview;
    const revision = state.revision;
    state.preview = null;
    state.runBusy = true;
    $('#consent-panel').hidden = true;
    renderControls();
    feedback('正在生成；本次最多调用一次。你可以继续修改范围，过期结果将被忽略。');
    try {
      const payload = await request('/api/copilot/run', { preview_id: preview.preview_id, consent: true, binding_revision: revision });
      if (revision !== state.revision) return;
      if (payload.binding_revision !== revision || !validResult(payload)) throw new Error('invalid-result');
      renderResult(payload);
      feedback('已生成三种说法。先读一遍，再选择适合你的。');
    } catch (_) {
      if (revision === state.revision) feedback('请求未完成，调用可能已发生。不会自动重试；再次生成需重新预览确认，可能重复计费。', true);
    } finally { state.runBusy = false; renderControls(); }
  }
  function applyNative(snapshot) {
    if (!snapshot || !['waiting', 'bound', 'ambiguous', 'error'].includes(snapshot.state)) return;
    const wasPaused = state.paused;
    state.paused = snapshot.paused === true;
    if (state.paused && !wasPaused) invalidate();
    const target = typeof snapshot.target === 'string' ? snapshot.target : null;
    if (state.nativeSeen && target !== state.nativeTarget) {
      invalidate({ clearPerson: true });
      feedback('跟随窗口已变化。请重新选择会话并核对范围。');
    }
    state.nativeSeen = true;
    state.nativeTarget = target;
    const labels = { waiting: '等待窗口', bound: '已绑定窗口', ambiguous: '检测到多个窗口，暂未绑定', error: '窗口跟随暂不可用' };
    $('#native-status').textContent = `仅窗口跟随 · ${state.paused ? '已暂停' : labels[snapshot.state]}`;
    $('#pause-follow').textContent = state.paused ? '恢复跟随' : '暂停跟随';
    $('#pause-follow').setAttribute('aria-pressed', String(state.paused));
    $('#window-mode').value = snapshot.mode === 'float' ? 'float' : 'dock';
    const size = snapshot.effectiveSize || snapshot.size;
    const safeSize = ['expanded', 'compact', 'bubble'].includes(size) ? size : 'compact';
    document.body.dataset.size = safeSize;
    $('#bubble-open').hidden = safeSize !== 'bubble';
    $('.compact-content').hidden = safeSize !== 'compact';
    renderControls();
  }
  $('#choose-context').addEventListener('click', () => {
    nativeCall('edit', true);
    $('#context-settings').open = true;
    $('#contact-select').focus();
  });
  $('#contact-select').addEventListener('change', () => { invalidate({ clearDraft: true }); renderContext(true); renderControls(); });
  for (const selector of ['#date-from', '#date-to', '#max-messages', '#direction', '#latest-draft']) {
    $(selector).addEventListener('input', () => invalidate());
    // Native date/select controls may emit only change on some platforms.
    $(selector).addEventListener('change', () => invalidate());
  }
  document.addEventListener('pointerdown', (event) => {
    if (event.isTrusted && event.target.closest('input, textarea, select, summary')) nativeCall('edit', true);
  }, true);
  document.addEventListener('keydown', (event) => {
    if (event.isTrusted && event.key === 'Tab') nativeCall('edit', true);
    if (event.isTrusted && event.key === 'Escape') { document.activeElement?.blur(); nativeCall('edit', false); }
  });
  window.addEventListener('blur', () => nativeCall('edit', false));
  $('#prepare-preview').addEventListener('click', prepare);
  $('#compact-copy').addEventListener('click', () => copyText(state.compactReply, $('#compact-copy')));
  $('#confirm-run').addEventListener('click', run);
  $('#cancel-preview').addEventListener('click', () => invalidate());
  $('#enable-copilot').addEventListener('click', enable);
  $('#refresh-status').addEventListener('click', refresh);
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  function activateTab(tab) {
    for (const item of tabs) {
      item.setAttribute('aria-selected', String(item === tab));
      item.tabIndex = item === tab ? 0 : -1;
      $('#' + item.getAttribute('aria-controls')).hidden = item !== tab;
    }
  }
  for (const tab of tabs) {
    tab.addEventListener('click', () => activateTab(tab));
    tab.addEventListener('keydown', (event) => {
      const index = tabs.indexOf(tab);
      const next = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault(); activateTab(tabs[next]); tabs[next].focus();
    });
  }
  if (bridge) {
    document.body.dataset.native = 'true';
    $('#native-actions').hidden = false;
    $('#native-controls').hidden = false;
    $('#native-status').textContent = '仅窗口跟随 · 等待状态';
    document.body.dataset.size = 'compact';
    $('.compact-content').hidden = false;
    $('#window-mode').addEventListener('change', () => nativeCall('setMode', $('#window-mode').value));
    $('#pause-follow').addEventListener('click', () => nativeCall('pause', !state.paused));
    for (const [id, size] of [['compact-window', 'compact'], ['bubble-window', 'bubble'], ['expand-window', 'expanded'], ['bubble-open', 'expanded']]) {
      $('#' + id).addEventListener('click', () => { nativeCall('edit', false); nativeCall('setSize', size); });
    }
    $('#close-window').addEventListener('click', () => nativeCall('close'));
    let unsubscribe;
    try {
      unsubscribe = bridge.onState((snapshot) => { state.nativeSequence += 1; applyNative(snapshot); });
      const sequence = state.nativeSequence;
      Promise.resolve(bridge.state()).then((snapshot) => { if (sequence === state.nativeSequence) applyNative(snapshot); })
        .catch(() => { $('#native-status').textContent = '仅窗口跟随 · 状态暂不可用'; });
    } catch (_) { $('#native-status').textContent = '仅窗口跟随 · 状态暂不可用'; }
    window.addEventListener('beforeunload', () => { if (typeof unsubscribe === 'function') unsubscribe(); nativeCall('edit', false); });
  }
  refresh();
})();
