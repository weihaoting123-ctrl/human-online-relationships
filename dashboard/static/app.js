/* Catalog and local aggregate statistics. Shared services live in core.js;
 * navigation lives in shell.js and does not depend on optional AI code. */

function metric(label, value, unit, note) {
  const card = el('article', 'metric');
  card.append(el('div', 'metric-label', label));
  const main = el('div', 'metric-value', String(value));
  if (unit) main.append(el('span', '', unit));
  card.append(main, el('div', 'metric-note', note));
  return card;
}

function renderBundles() {
  const selected = Catalog.select(state.bundles, state.filters, state.matches);
  const page = Catalog.paginate(selected, state.filters.page, state.filters.pageSize);
  state.filters.page = page.page;
  const body = $('#catalog-body');
  const focusedBundleId = state.currentView === 'catalog' && body.contains(document.activeElement)
    ? document.activeElement.closest('.open-bundle')?.dataset.bundleId : null;
  body.replaceChildren();
  const kinds = { group: '群聊', direct: '私聊／服务会话', unknown: '类型待确认' };
  page.items.forEach((bundle) => {
    const active = state.activeBundle?.id === bundle.id;
    const row = el('tr', `catalog-row${active ? ' is-active' : ''}`);
    const contact = el('td');
    const button = el('button', 'open-bundle');
    button.type = 'button';
    button.dataset.bundleId = bundle.id;
    button.setAttribute('aria-pressed', String(active));
    button.setAttribute('aria-label', `查看 ${displayName(bundle)} 的统计`);
    const glyph = el('span', `contact-glyph ${bundle.conversation_kind || ''}`, bundle.conversation_kind === 'group' ? '群' : '聊');
    glyph.setAttribute('aria-hidden', 'true');
    const label = el('span', 'contact-label');
    const name = el('strong', '', displayName(bundle));
    name.title = displayName(bundle);
    if (bundle.library?.pinned) name.prepend(el('span', 'contact-pin', '置顶'));
    label.append(name, el('small', '', bundle.library?.alias ? `原名：${bundle.contact || '未命名会话'}` : bundle.has_stats ? '已统计' : '待统计'));
    if (bundle.library?.tag_names?.length) {
      const tags = el('span', 'contact-tags');
      bundle.library.tag_names.forEach((tag) => tags.append(el('span', 'library-tag', tag)));
      label.append(tags);
    }
    button.append(glyph, label);
    button.addEventListener('click', () => loadBundle(bundle.id, true));
    contact.append(button);
    const topic = el('td');
    topic.append(el('span', 'kind-label', kinds[bundle.conversation_kind] || kinds.unknown));
    const topicTag = el('span', 'topic-tag', bundle.primary_topic || '尚未分类');
    topicTag.title = (bundle.candidate_topics || []).join('、') || bundle.primary_topic || '尚未分类';
    topic.append(topicTag);
    const match = state.matches?.get(bundle.id);
    const matchCell = el('td', 'match-cell');
    matchCell.append(el('span', match ? 'match-count' : '', match ? `${number(match.match_count)} 条` : '—'));
    if (match?.last_match_date) matchCell.append(el('small', '', match.last_match_date));
    row.append(contact, topic, el('td', 'numeric', number(bundle.message_count)), el('td', 'date-cell', Catalog.lastDate(bundle) || '日期未知'), matchCell);
    body.append(row);
  });
  const first = page.total ? (page.page - 1) * page.size + 1 : 0;
  $('#result-summary').textContent = `${number(page.total)} / ${number(state.bundles.length)} 个会话${page.total ? ` · 显示 ${first}–${Math.min(page.page * page.size, page.total)}` : ''}`;
  $('#page-label').textContent = `第 ${page.page} / ${page.pages} 页`;
  $('#prev-page').disabled = page.page <= 1;
  $('#next-page').disabled = page.page >= page.pages;
  const waiting = state.filters.mode === 'content' && Catalog.tokens(state.filters.query).length && state.matches === null;
  const badDates = state.filters.from && state.filters.to && state.filters.from > state.filters.to;
  if (waiting) $('#result-summary').textContent = `${state.searchPhase === 'pending' ? '正在检索' : state.searchPhase === 'error' ? '检索未完成' : '等待搜索'} · 已归档 ${number(state.bundles.length)} 个会话`;
  if (badDates) $('#result-summary').textContent = '请检查日期范围';
  $('#catalog-empty').hidden = !state.catalogLoaded || !state.bundles.length || page.total > 0 || Boolean(waiting) || Boolean(badDates);
  $('#empty-state').hidden = state.currentView !== 'catalog' || !state.catalogLoaded || state.catalogPhase !== 'ready' || Boolean(state.bundles.length);
  if (!state.catalogLoaded) $('#result-summary').textContent = state.catalogPhase === 'error'
    ? '本地档案读取失败，请刷新后再试。' : '正在读取本地档案…';
  else if (state.catalogPhase === 'error') $('#result-summary').textContent += ' · 刷新失败，显示上次成功读取的档案';
  $('#total-conversations').textContent = number(state.bundles.length);
  $('#nav-count').textContent = number(state.bundles.length);
  $('#total-messages').textContent = number(state.bundles.reduce((sum, item) => sum + Number(item.message_count || 0), 0));
  // Preserve only the row focused immediately before this render. A removed
  // row or a user now editing elsewhere must never receive delayed focus.
  if (focusedBundleId) Array.from(body.querySelectorAll('.open-bundle'))
    .find((node) => node.dataset.bundleId === focusedBundleId)?.focus({ preventScroll: true });
}

function renderEnvironment(payload) {
  const badge = $('#environment-badge');
  const exporter = payload.exporter || {};
  const ready = exporter.ready === true || exporter.status === 'ok' && exporter.provider;
  const provider = exporter.provider || '本机导出器';
  badge.textContent = ready ? `${provider} 已就绪` : '导入模式可用';
  badge.classList.toggle('is-ready', ready);
  const details = $('#environment-details');
  details.replaceChildren();
  const cards = [
    ['Python', payload.python || '—'],
    ['微信导出器', ready ? `${provider} ${exporter.version || ''} · 项目内隔离` : '尚未安装 · 仍可导入已有文件'],
    ['数据目录', '当前配置的本机私有目录 · 不会上传'],
  ];
  cards.forEach(([title, value]) => {
    const item = el('div');
    item.append(el('strong', '', title), el('span', '', value));
    details.append(item);
  });
}

function searchStatus(message, error = false) {
  const node = $('#search-status');
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle('is-error', error);
}

function invalidateSearch() {
  state.searchSequence += 1;
  state.searchController?.abort();
  state.searchController = null;
  state.matches = null;
  state.searchPhase = 'idle';
  $('#search-submit').disabled = false;
  $('#search-submit').textContent = '搜索';
  if (state.filters.from && state.filters.to && state.filters.from > state.filters.to) {
    searchStatus('开始日期不能晚于结束日期，请调整日期范围。', true);
  } else if (state.filters.mode === 'content' && state.filters.query.trim()) {
    searchStatus('点击“搜索”或按 Enter，在本机查找聊天内容。首次检索需要建立私有索引。');
  } else {
    searchStatus('');
  }
}

async function searchContent() {
  invalidateSearch();
  state.filters.page = 1;
  renderBundles();
  if (!state.filters.query.trim() || state.filters.mode !== 'content') return;
  if (state.filters.from && state.filters.to && state.filters.from > state.filters.to) return;
  if (Catalog.tokens(state.filters.query).length > 8) {
    searchStatus('最多同时搜索 8 个关键词，请减少用空格分开的关键词。', true);
    return;
  }
  const sequence = state.searchSequence;
  const controller = new AbortController();
  state.searchController = controller;
  state.searchPhase = 'pending';
  $('#search-submit').disabled = true;
  $('#search-submit').textContent = '检索中…';
  searchStatus('正在本机检索。首次建立索引可能需要一些时间；原微信记录不会被修改。');
  try {
    const payload = await api('/api/search', {
      method: 'POST', signal: controller.signal,
      body: JSON.stringify({ query: state.filters.query, date_from: state.filters.from, date_to: state.filters.to }),
    });
    if (sequence !== state.searchSequence) return;
    state.matches = new Map((payload.matches || []).map((item) => [item.bundle_id, item]));
    state.searchPhase = 'ready';
    const skipped = Number(payload.skipped_conversations || 0);
    searchStatus(`已检索 ${number(payload.indexed_conversations)} 个会话、${number(payload.indexed_messages)} 条消息。只显示匹配数量，不返回原话。${skipped ? ` ${number(skipped)} 个会话暂未索引，结果可能不完整。` : ''}`, skipped > 0);
    renderBundles();
  } catch (error) {
    if (sequence !== state.searchSequence || error.name === 'AbortError') return;
    state.searchPhase = 'error';
    searchStatus(`${error.message}。可稍后重新搜索，已有档案保持不变。`, true);
  } finally {
    if (sequence === state.searchSequence) {
      state.searchController = null;
      $('#search-submit').disabled = false;
      $('#search-submit').textContent = '搜索';
    }
  }
}

function saveCatalogPreferences() {
  // Never store a query, contact, bundle ID, content, or search history.
  const { kind, topic, sort, pageSize } = state.filters;
  try { localStorage.setItem('slm-catalog-preferences-v1', JSON.stringify({ kind, topic, sort, pageSize })); }
  catch (_error) { /* Private browser modes may disallow localStorage. */ }
}

function initializeCatalog() {
  Catalog.topics.forEach((topic) => {
    const option = el('option', '', topic);
    option.value = topic;
    $('#filter-topic').append(option);
  });
  try {
    const saved = JSON.parse(localStorage.getItem('slm-catalog-preferences-v1') || '{}');
    if (['all', 'direct', 'group', 'unknown'].includes(saved.kind)) state.filters.kind = saved.kind;
    if (['all', ...Catalog.topics].includes(saved.topic)) state.filters.topic = saved.topic;
    if (Catalog.sorts.includes(saved.sort)) state.filters.sort = saved.sort;
    if ([25, 50, 100].includes(saved.pageSize)) state.filters.pageSize = saved.pageSize;
  } catch (_error) { /* Invalid saved preferences fall back to safe defaults. */ }
  const controlMap = { '#filter-kind': 'kind', '#filter-topic': 'topic', '#sort-select': 'sort', '#page-size': 'pageSize' };
  Object.entries(controlMap).forEach(([selector, key]) => {
    $(selector).value = String(state.filters[key]);
    $(selector).addEventListener('change', (event) => {
      state.filters[key] = key === 'pageSize' ? Number(event.target.value) : event.target.value;
      state.filters.page = 1;
      saveCatalogPreferences();
      renderBundles();
    });
  });
  $('#search-input').addEventListener('input', (event) => {
    state.filters.query = event.target.value;
    state.filters.page = 1;
    invalidateSearch();
    renderBundles();
  });
  $('#search-mode').addEventListener('change', (event) => {
    state.filters.mode = event.target.value;
    state.filters.page = 1;
    const content = state.filters.mode === 'content';
    $('#search-input').placeholder = content ? '输入聊天关键词，例如：项目 会议' : '搜索联系人或群聊名称…';
    $('#search-help').textContent = content
      ? '点击搜索或按 Enter。同一条消息须包含全部关键词，最多 8 个；原话与搜索词不会上传。'
      : '名称即时匹配；多个关键词用空格分开，同时满足才显示。按 / 聚焦，Esc 清空。';
    invalidateSearch();
    renderBundles();
  });
  [['#date-from', 'from'], ['#date-to', 'to']].forEach(([selector, key]) => {
    $(selector).addEventListener('change', (event) => {
      state.filters[key] = event.target.value;
      state.filters.page = 1;
      invalidateSearch();
      renderBundles();
    });
  });
  $('#catalog-search').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.filters.mode === 'content') searchContent();
    else { state.filters.page = 1; invalidateSearch(); renderBundles(); }
  });
  $('#clear-filters').addEventListener('click', () => {
    Object.assign(state.filters, { query: '', kind: 'all', topic: 'all', tag: 'all', from: '', to: '', page: 1 });
    $('#search-input').value = '';
    $('#filter-kind').value = 'all';
    $('#filter-topic').value = 'all';
    $('#filter-tag').value = 'all';
    $('#date-from').value = '';
    $('#date-to').value = '';
    invalidateSearch();
    saveCatalogPreferences();
    renderBundles();
  });
  [['#prev-page', -1], ['#next-page', 1]].forEach(([selector, delta]) => {
    $(selector).addEventListener('click', () => {
      state.filters.page += delta;
      renderBundles();
      $('.results-toolbar').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
    });
  });
  $('#close-detail').addEventListener('click', () => {
    const bundleId = state.activeBundle?.id;
    state.interactionEpoch += 1;
    state.loadSequence += 1;
    window.RelationshipTimeline?.clear($('#relationship-timeline'));
    window.ChatHeatmap?.clear($('#chat-heatmap'));
    state.activeBundle = null;
    $('#dashboard').hidden = true;
    $('#dashboard').setAttribute('aria-busy', 'false');
    renderBundles();
    const rowButton = Array.from(document.querySelectorAll('#catalog-body .open-bundle'))
      .find((node) => node.dataset.bundleId === bundleId);
    (rowButton || $('#search-input') || $('#catalog-title')).focus({ preventScroll: true });
    $('#catalog').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
  });
  document.addEventListener('keydown', (event) => {
    if (document.querySelector('dialog[open]')) return;
    const editing = event.target.closest('input,select,textarea,[contenteditable="true"]');
    if (event.key === '/' && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); window.navigateWorkspace?.('catalog'); $('#search-input').focus();
    }
    if (event.key === 'Escape' && event.target === $('#search-input')) {
      event.preventDefault();
      $('#search-input').value = ''; state.filters.query = ''; state.filters.page = 1;
      invalidateSearch(); renderBundles();
    } else if (event.key === 'Escape' && !editing && !$('#dashboard').hidden) {
      $('#close-detail').click();
    }
  });
}

function renderSync(sync = {}) {
  state.syncStatus = sync;
  const hasFailures = Number(sync.failed_conversations || 0) > 0;
  const states = {
    not_configured: '尚未启用',
    ready: '等待首次同步',
    running: '正在同步',
    completed: '同步完成',
    awaiting_wechat_restart: '等待下次微信启动',
    needs_account_selection: '等待选择账号',
    wechat_not_running: '等待微信运行',
    error: '同步需检查',
  };
  const badge = $('#sync-badge');
  const stateLabel = sync.state === 'completed' && hasFailures
    ? '部分完成'
    : (states[sync.state] || '同步需检查');
  badge.textContent = stateLabel;
  badge.classList.toggle('is-ready', (sync.state === 'completed' && !hasFailures) || sync.state === 'ready');
  badge.classList.toggle('is-running', sync.state === 'running');
  badge.classList.toggle('is-warning', Boolean(sync.attention) || hasFailures);

  const mode = sync.state === 'error' && sync.error_code === 'invalid_status'
    ? '状态文件损坏'
    : (sync.enabled
      ? (sync.mode === 'scheduled' ? '自动计划已启用' : '已启用 · 手动触发')
      : '未启用');
  const conversations = `${number(sync.imported_conversations)} 处理 · ${number(sync.skipped_conversations)} 跳过 · ${number(sync.failed_conversations)} 失败`;
  const messages = `${number(sync.imported_messages)} 新增 · ${number(sync.unchanged_messages)} 未变化`;
  $('#incremental-status').textContent = `最近一轮：${number(sync.processed_databases)} 个变更分片校对，${number(sync.skipped_databases)} 个分片跳过；${number(sync.skipped_conversations)} 个会话无需重复导出，新增 ${number(sync.imported_messages)} 条消息。源文件仍需校验，补同步的历史消息也会保留。`;
  const details = $('#sync-details');
  details.replaceChildren();
  [
    ['运行方式', mode],
    ['上次运行', formatLocalTime(sync.last_run_at)],
    ['扫描会话', `${number(sync.scanned_conversations)} 个 · ${conversations}`],
    ['本地消息', messages],
    ['本次去重', `${number(sync.deduplicated_messages)} 条重复未写入`],
    ['下次计划', sync.enabled && sync.mode === 'scheduled' ? formatLocalTime(sync.next_run_at) : '暂无计划'],
  ].forEach(([title, value]) => {
    const item = el('div');
    item.append(el('strong', '', title), el('span', '', value));
    details.append(item);
  });

  const attention = $('#sync-attention');
  const attentionMessages = {
    awaiting_wechat_restart: '自动同步已就绪。当前微信会话不会被关闭；下次正常重新启动微信时，会完成一次性本地初始化并开始导入。',
    needs_account_selection: '检测到多个微信账号。为避免导错数据，自动同步已暂停，等待你确认目标账号。',
    wechat_not_running: '当前没有检测到可读取的微信进程。微信正常运行后再同步即可。',
    error: sync.error_code === 'permission_required'
      ? '本次同步需要本机权限，未改动微信数据。'
      : '本次同步未完成；本地已有副本和微信原始记录均保持不变。',
    completed: hasFailures
      ? '部分会话未能导入；其他会话已安全归档，微信原始记录保持不变。'
      : '',
  };
  attention.textContent = attentionMessages[sync.state] || '';
  attention.hidden = !(sync.attention || hasFailures);
}

function markSyncStale() {
  const badge = $('#sync-badge');
  badge.textContent = '同步状态已失联';
  badge.classList.remove('is-ready', 'is-running');
  badge.classList.add('is-warning');
  const attention = $('#sync-attention');
  attention.textContent = '无法取得最新同步状态；下方数字是上次成功读取的快照，请刷新本机服务后再确认。';
  attention.hidden = false;
}

function renderMetrics(bundle) {
  const stats = bundle.stats || {};
  const basic = stats.basic || {};
  const grid = $('#metrics');
  grid.replaceChildren(
    metric('消息总量', number(basic.total_messages || bundle.message_count), '条', '当前本地副本的完整统计'),
    metric('覆盖天数', number(basic.total_days), '天', '包含没有消息的自然日'),
    metric('你的消息占比', number((basic.my_ratio || 0) * 100), '%', `你 ${number(basic.my_messages)} 条 · 其余 ${number(basic.their_messages)} 条`),
    metric('日均消息', number(basic.avg_daily || 0, 1), '条', `${(basic.date_range || []).join(' → ') || '—'}`),
  );
}

function svgNode(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function smoothPath(points) {
  if (!points.length) return '';
  if (points.length === 1) return `M ${points[0][0]} ${points[0][1]}`;
  let path = `M ${points[0][0]} ${points[0][1]}`;
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    const midX = (previous[0] + current[0]) / 2;
    path += ` C ${midX} ${previous[1]}, ${midX} ${current[1]}, ${current[0]} ${current[1]}`;
  }
  return path;
}

function renderPulseChart(daily = []) {
  const svg = $('#pulse-chart');
  svg.replaceChildren();
  $('#chart-empty').hidden = daily.length > 0;
  const summary = $('#chart-summary');
  if (!daily.length) {
    summary.textContent = '当前范围没有可绘制的日期数据。';
    return;
  }
  const totalMe = daily.reduce((sum, item) => sum + Number(item.me || 0), 0);
  const totalThem = daily.reduce((sum, item) => sum + Number(item.them || 0), 0);
  const silentDays = daily.filter((item) => !Number(item.me || 0) && !Number(item.them || 0)).length;
  const summaryText = `${daily[0].date} 至 ${daily[daily.length - 1].date}，共 ${daily.length} 个自然日；你 ${number(totalMe)} 条，TA ${number(totalThem)} 条，双方都未发消息 ${silentDays} 天。`;
  summary.textContent = summaryText;
  const title = svgNode('title');
  title.textContent = '双方每日消息轨迹';
  const description = svgNode('desc');
  description.textContent = summaryText;
  svg.append(title, description);
  const width = 900;
  const height = 280;
  const padX = 45;
  const center = 140;
  const usable = width - padX * 2;
  const max = Math.max(1, ...daily.flatMap((item) => [item.me || 0, item.them || 0]));
  const scale = 98 / max;
  [45, 95, 140, 185, 235].forEach((y) => svg.append(svgNode('line', { x1: padX, x2: width - padX, y1: y, y2: y, class: y === center ? 'chart-axis' : 'chart-grid' })));
  const points = daily.map((item, index) => {
    const x = padX + (daily.length === 1 ? usable / 2 : index * usable / (daily.length - 1));
    return { x, you: center - (item.me || 0) * scale, them: center + (item.them || 0) * scale };
  });
  const youPoints = points.map((item) => [item.x, item.you]);
  const themPoints = points.map((item) => [item.x, item.them]);
  const finalPoint = points[points.length - 1];
  const youArea = `${smoothPath(youPoints)} L ${finalPoint.x} ${center} L ${points[0].x} ${center} Z`;
  const themArea = `${smoothPath(themPoints)} L ${finalPoint.x} ${center} L ${points[0].x} ${center} Z`;
  svg.append(svgNode('path', { d: youArea, class: 'chart-area you' }));
  svg.append(svgNode('path', { d: themArea, class: 'chart-area them' }));
  svg.append(svgNode('path', { d: smoothPath(youPoints), class: 'chart-path you' }));
  svg.append(svgNode('path', { d: smoothPath(themPoints), class: 'chart-path them' }));
  if (points.length === 1) {
    svg.append(svgNode('circle', { cx: points[0].x, cy: points[0].you, r: 4, class: 'thread-node you' }));
    svg.append(svgNode('circle', { cx: points[0].x, cy: points[0].them, r: 4, class: 'thread-node them' }));
  }
  const labels = [0, Math.floor((daily.length - 1) / 2), daily.length - 1].filter((value, index, all) => all.indexOf(value) === index);
  labels.forEach((index) => {
    const text = svgNode('text', { x: points[index].x, y: 270, 'text-anchor': index === 0 ? 'start' : index === daily.length - 1 ? 'end' : 'middle', class: 'chart-label' });
    text.textContent = daily[index].date.slice(5);
    svg.append(text);
  });
}

function balanceRow(label, mine, theirs, mineLabel, theirsLabel) {
  const total = Math.max(Number(mine || 0) + Number(theirs || 0), 1);
  const minePct = Math.max(0, Number(mine || 0)) / total * 100;
  const theirPct = Math.max(0, Number(theirs || 0)) / total * 100;
  const row = el('div', 'balance-row');
  const head = el('div', 'balance-head');
  head.append(el('span', '', `${label} · 你 ${mineLabel}`), el('span', '', `TA ${theirsLabel}`));
  const track = el('div', 'balance-track');
  const chart = svgNode('svg', { viewBox: '0 0 100 12', preserveAspectRatio: 'none', role: 'img', 'aria-label': `${label}：你 ${mineLabel}，TA ${theirsLabel}` });
  chart.append(
    svgNode('rect', { x: 0, y: 0, width: minePct, height: 12, class: 'balance-you' }),
    svgNode('rect', { x: minePct, y: 0, width: theirPct, height: 12, class: 'balance-them' }),
    svgNode('line', { x1: 50, x2: 50, y1: 0, y2: 12, class: 'balance-midline' }),
  );
  track.append(chart);
  row.append(head, track);
  return row;
}

function renderBalance(stats = {}) {
  const basic = stats.basic || {};
  const initiative = stats.initiative || {};
  const length = stats.message_length || {};
  const goodnight = stats.goodnight || {};
  $('#balance-list').replaceChildren(
    balanceRow('消息', basic.my_messages, basic.their_messages, `${number(basic.my_messages)} 条`, `${number(basic.their_messages)} 条`),
    balanceRow('发起', initiative.my_starts, initiative.their_starts, `${number(initiative.my_starts)} 次`, `${number(initiative.their_starts)} 次`),
    balanceRow('平均字数', length.my_avg_chars, length.their_avg_chars, `${number(length.my_avg_chars, 1)} 字`, `${number(length.their_avg_chars, 1)} 字`),
    balanceRow('先说晚安', goodnight.my_goodnight, goodnight.their_goodnight, `${number(goodnight.my_goodnight)} 次`, `${number(goodnight.their_goodnight)} 次`),
  );
}

function renderHours(hourly = {}) {
  const values = Array.from({ length: 24 }, (_, hour) => {
    const entry = hourly[String(hour)] || {};
    return { me: Number(entry.me || 0), them: Number(entry.them || 0) };
  });
  const max = Math.max(1, ...values.flatMap((entry) => [entry.me, entry.them]));
  const chart = $('#hour-chart');
  chart.replaceChildren();
  const svg = svgNode('svg', { viewBox: '0 0 600 190', role: 'img' });
  const meTotal = values.reduce((sum, entry) => sum + entry.me, 0);
  const themTotal = values.reduce((sum, entry) => sum + entry.them, 0);
  const title = svgNode('title');
  title.textContent = '双方 24 小时活跃度';
  const description = svgNode('desc');
  description.textContent = `你共 ${number(meTotal)} 条，TA 共 ${number(themTotal)} 条；上下两行分别按各小时显示。`;
  svg.append(title, description, svgNode('line', { x1: 48, x2: 590, y1: 82, y2: 82, class: 'hour-axis' }));
  const meLabel = svgNode('text', { x: 4, y: 48, class: 'hour-side-label' });
  meLabel.textContent = '你';
  const themLabel = svgNode('text', { x: 4, y: 132, class: 'hour-side-label' });
  themLabel.textContent = 'TA';
  svg.append(meLabel, themLabel);
  values.forEach((entry, hour) => {
    const x = 50 + hour * 22.3;
    const meHeight = entry.me / max * 58;
    const themHeight = entry.them / max * 58;
    const meBar = svgNode('rect', { x, y: 78 - meHeight, width: 14, height: Math.max(meHeight, entry.me ? 1 : 0), rx: 2, class: 'hour-you' });
    const themBar = svgNode('rect', { x, y: 87, width: 14, height: Math.max(themHeight, entry.them ? 1 : 0), rx: 2, class: 'hour-them' });
    const meTip = svgNode('title');
    meTip.textContent = `${hour}:00 · 你 ${number(entry.me)} 条`;
    const themTip = svgNode('title');
    themTip.textContent = `${hour}:00 · TA ${number(entry.them)} 条`;
    meBar.append(meTip);
    themBar.append(themTip);
    svg.append(meBar, themBar);
    if (hour % 3 === 0) {
      const label = svgNode('text', { x: x + 7, y: 178, 'text-anchor': 'middle', class: 'hour-label' });
      label.textContent = String(hour).padStart(2, '0');
      svg.append(label);
    }
  });
  chart.setAttribute('aria-label', description.textContent);
  chart.append(svg);
}

function renderScopes(preview = {}) {
  const options = $('#scope-options');
  options.replaceChildren();
  const suggestions = preview.suggestions || [];
  const recommended = suggestions.find((item) => item.recommended) || suggestions[suggestions.length - 1];
  const selected = suggestions.find((item) => item.date_from === state.selectedSince) || recommended;
  state.selectedSince = selected?.date_from || null;
  suggestions.forEach((scope, index) => {
    const label = el('label', 'scope-option');
    const input = el('input');
    input.type = 'radio';
    input.name = 'scope';
    input.value = scope.date_from;
    input.checked = scope.date_from === selected?.date_from || (!selected && index === suggestions.length - 1);
    input.addEventListener('change', () => { state.selectedSince = input.value; });
    label.append(input, el('strong', '', scope.label), el('span', '', `${number(scope.count)} 条 · 从 ${scope.date_from}`));
    if (scope.recommended) label.append(el('em', '', '推荐范围'));
    options.append(label);
  });
  $('#scope-reason').textContent = preview.recommended_reason || '选择范围后生成本地分层证据样本。';
}

function renderAnalysis(analysis) {
  const section = $('#analysis-section');
  section.hidden = !analysis;
  if (!analysis) return;
  $('#analysis-title').textContent = analysis.relationship_label || analysis.relationship_type || '已有分析结论';
  $('#analysis-verdict').textContent = analysis.verdict || analysis.relationship_trend || '打开完整报告查看经证据锚定的结论。';
  const warnings = $('#warning-list');
  warnings.replaceChildren();
  (analysis.danger_warnings || []).forEach((warning) => {
    const item = el('div', 'warning-item');
    const level = warning.level || warning.severity;
    item.append(
      el('strong', '', warning.title || warning.type || '需要留意'),
      el('span', '', level ? `等级：${level} · 请在完整报告中核对证据。` : '请在完整报告中核对证据。'),
    );
    warnings.append(item);
  });
}

function updateSteps(bundle) {
  const reached = {
    sample: true,
    stats: Boolean(bundle?.has_stats),
    evidence: Boolean(bundle?.has_sample),
    decision: Boolean(bundle?.has_analysis),
  };
  let current = 'sample';
  ['sample', 'stats', 'evidence', 'decision'].forEach((name) => {
    if (reached[name]) current = name;
  });
  document.querySelectorAll('[data-step]').forEach((item) => {
    const active = item.dataset.step === current;
    item.classList.toggle('is-active', active);
    if (active) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  });
}

function renderBundle(bundle) {
  const summary = state.bundles.find((item) => item.id === bundle.id);
  if (summary?.library) bundle.library = summary.library;
  state.activeBundle = bundle;
  renderBundles();
  updateSteps(bundle);
  $('#empty-state').hidden = true;
  $('#dashboard').hidden = state.currentView !== 'catalog';
  $('#case-title').textContent = displayName(bundle);
  const range = bundle.date_range?.join(' → ') || bundle.preview?.date_range?.join(' → ') || '时间范围待统计';
  $('#case-meta').textContent = `${number(bundle.message_count)} 条消息 · ${range} · ${bundle.source}`;
  window.ChatHeatmap?.mount($('#chat-heatmap'), bundle.id);
  window.RelationshipTimeline?.render($('#relationship-timeline'), {
    local: bundle.relationship_timeline, mode: 'detail', label: displayName(bundle),
    onSelectRange: window.AiWorkspace ? (dateFrom, dateTo) => {
      if (state.activeBundle?.id !== bundle.id) return;
      if (window.AiWorkspace.selectRange(bundle.id, dateFrom, dateTo)) {
        window.navigateWorkspace('analysis-workspace');
        $('#ai-scope-form').scrollIntoView({ behavior: 'auto', block: 'start' });
      }
    } : null,
  });
  renderMetrics(bundle);
  renderPulseChart(bundle.timeline?.daily || []);
  renderBalance(bundle.stats || {});
  renderHours(bundle.timeline?.hourly || {});
  renderScopes(bundle.preview || {});
  renderAnalysis(bundle.analysis);
  const report = bundle.reports?.[0];
  const reportLink = $('#report-link');
  const reportButton = $('#report-button');
  reportLink.hidden = !report;
  reportButton.hidden = !bundle.has_analysis || Boolean(report);
  if (report) {
    reportLink.href = new URL(report.url, window.location.origin).href;
  }
}

async function loadBundle(bundleId, reveal = false) {
  if (reveal) state.interactionEpoch += 1;
  const epoch = state.interactionEpoch;
  const sequence = ++state.loadSequence;
  window.RelationshipTimeline?.clear($('#relationship-timeline'));
  window.ChatHeatmap?.clear($('#chat-heatmap'));
  $('#dashboard').setAttribute('aria-busy', 'true');
  showNotice('正在加载会话统计…');
  try {
    const payload = await api(`/api/bundles/${encodeURIComponent(bundleId)}`);
    if (sequence !== state.loadSequence || epoch !== state.interactionEpoch) return;
    if (!payload.bundle || payload.bundle.id !== bundleId) throw requestFailure('response', 200);
    renderBundle(payload.bundle);
    $('#notice').hidden = true;
    if (reveal && state.currentView === 'catalog') revealDetail();
  } catch (error) {
    if (sequence === state.loadSequence && epoch === state.interactionEpoch) showNotice(error.message, true);
  } finally {
    if (sequence === state.loadSequence) $('#dashboard').setAttribute('aria-busy', 'false');
  }
}

function revealDetail() {
  $('#case-title').focus({ preventScroll: true });
  $('#dashboard').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
}

async function loadState(reloadActive = false) {
  const sequence = ++state.stateSequence;
  const epoch = state.interactionEpoch;
  const activeId = state.activeBundle?.id;
  state.catalogPhase = 'loading';
  renderBundles();
  let payload;
  try {
    payload = await api('/api/state');
    if (!Array.isArray(payload.bundles) || payload.bundles.some((item) => !item || typeof item.id !== 'string')) {
      throw requestFailure('response', 200);
    }
  } catch (error) {
    if (sequence === state.stateSequence) { state.catalogPhase = 'error'; renderBundles(); }
    throw error;
  }
  if (sequence !== state.stateSequence) return false;
  state.catalogLoaded = true;
  state.catalogPhase = 'ready';
  state.bundles = payload.bundles || [];
  if (state.activeBundle && !state.bundles.some(item => item.id === state.activeBundle.id)) {
    window.ChatHeatmap?.clear($('#chat-heatmap'));
  }
  window.LibraryWorkspace?.setBundles(state.bundles);
  window.AiWorkspace?.setBundles(state.bundles);
  renderEnvironment(payload);
  renderSync(payload.sync || {});
  renderBundles();
  if (reloadActive && epoch === state.interactionEpoch && activeId === state.activeBundle?.id && state.bundles.some((item) => item.id === activeId)) {
    await loadBundle(activeId);
  } else if (!state.bundles.length) {
    updateSteps(null);
    $('#empty-state').hidden = state.currentView !== 'catalog';
    $('#dashboard').hidden = true;
  }
  return true;
}

function selectImportFile(file) {
  state.importRevision += 1;
  state.selectedFile = file || null;
  $('#file-label').textContent = file ? file.name : '选择聊天导出文件';
  $('#source-kind').value = /\.(md|txt)$/i.test(file?.name || '') ? 'markdown' : 'auto';
}
$('#chat-file').addEventListener('change', (event) => selectImportFile(event.target.files[0]));
$('#import-form').addEventListener('input', () => { state.importRevision += 1; });
$('#import-form').addEventListener('change', () => { state.importRevision += 1; });
$('#import-form').addEventListener('reset', () => {
  selectImportFile(null);
});

const dropZone = $('#drop-zone');
['dragenter', 'dragover'].forEach((type) => dropZone.addEventListener(type, (event) => {
  event.preventDefault();
  dropZone.classList.add('is-dragging');
}));
dropZone.addEventListener('dragleave', (event) => {
  event.preventDefault();
  if (event.relatedTarget && dropZone.contains(event.relatedTarget)) return;
  dropZone.classList.remove('is-dragging');
});
dropZone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropZone.classList.remove('is-dragging');
  const file = event.dataTransfer?.files[0];
  if (!file) return;
  $('#chat-file').value = '';
  selectImportFile(file);
});

function showImportResult(result, message, isError = false) {
  state.importResult = result;
  $('#import-result').hidden = false;
  $('#import-result').classList.toggle('is-error', isError);
  $('#import-result-message').textContent = message;
  $('#import-result-message').setAttribute('role', isError ? 'alert' : 'status');
  $('#import-result-open').hidden = !result?.bundleId;
}

function importUnavailable(result) {
  showImportResult(result, '导入完成；该会话目前不在可见档案中，可能位于回收站或来源暂不可用。可查看回收站或刷新核对，未自动恢复会话。');
}

$('#import-result-open').addEventListener('click', async () => {
  const result = state.importResult;
  const button = $('#import-result-open');
  if (!result?.bundleId || button.disabled) return;
  const epoch = ++state.interactionEpoch;
  const revision = state.importRevision;
  setBusy(button, true, '正在核对档案…');
  try {
    const refreshed = await loadState(false);
    if (state.importResult !== result) return;
    if (!refreshed) {
      showImportResult(result, '导入完成；档案正在更新，请再次点击“查看已导入会话”核对。');
      return;
    }
    if (!state.bundles.some((item) => item.id === result.bundleId && !item.library?.hidden_at)) {
      importUnavailable(result);
      return;
    }
    if (epoch !== state.interactionEpoch || revision !== state.importRevision) return;
    window.navigateWorkspace('catalog');
    await loadBundle(result.bundleId, true);
  } catch (_error) {
    if (state.importResult === result) showImportResult(result, '导入完成；档案刷新暂时失败，请稍后点击“查看已导入会话”核对。此操作不会重新导入。');
  } finally { setBusy(button, false); }
});

$('#import-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.importBusy) return;
  if (!window.ArchiveShell?.isEnabled('sync')) return showNotice('同步功能已停用，可在设置中启用后导入。', true);
  const file = state.selectedFile;
  if (!file) return showNotice('先选择一份聊天导出文件。', true);
  if (file.size > 50 * 1024 * 1024) return showNotice('文件超过 50 MB，请先按联系人拆分。', true);
  // Everything describing this submission is captured before asynchronous I/O.
  const submitted = {
    filename: file.name, kind: $('#source-kind').value,
    contact: $('#contact-name').value.trim(), my_name: $('#my-name').value.trim() || '我',
  };
  const revision = state.importRevision;
  const epoch = state.interactionEpoch;
  const unchanged = () => revision === state.importRevision && epoch === state.interactionEpoch && file === state.selectedFile;
  const button = $('#import-button');
  state.importBusy = true;
  setBusy(button, true, '正在本机整理…');
  let postStarted = false;
  try {
    const content = await file.text();
    postStarted = true;
    const payload = await api('/api/import', {
      method: 'POST',
      body: JSON.stringify({ ...submitted, content }),
    });
    if (!payload.bundle || typeof payload.bundle.id !== 'string' || !payload.bundle.id) {
      throw requestFailure('response', 200, true);
    }
    const result = { bundleId: payload.bundle.id };
    // A confirmed write remains confirmed even when the following GET fails.
    showImportResult(result, '导入完成，统计已在本机生成。正在刷新档案…');
    let refreshed;
    try { refreshed = await loadState(false); }
    catch (_error) {
      showImportResult(result, '导入完成；档案刷新暂时失败，请点击“查看已导入会话”核对。此操作不会重新导入。');
      return;
    }
    if (!refreshed) {
      showImportResult(result, '导入完成；档案正在更新，请点击“查看已导入会话”核对。');
      return;
    }
    if (!state.bundles.some((item) => item.id === result.bundleId && !item.library?.hidden_at)) {
      importUnavailable(result);
      return;
    }
    showImportResult(result, '导入完成，统计已在本机生成。可随时查看已导入会话。');
    if (unchanged()) {
      event.target.reset();
      window.navigateWorkspace('catalog');
      state.loadSequence += 1;
      renderBundle(payload.bundle);
      $('#dashboard').setAttribute('aria-busy', 'false');
      revealDetail();
    }
  } catch (error) {
    if (!postStarted) showImportResult(null, '无法读取所选文件，请检查文件后重新选择。尚未提交导入。', true);
    else if (error.outcomeUnknown) showImportResult(null, '无法确认本次导入是否完成。已保留输入，请先刷新档案核对，再决定是否再次提交；未自动重试。', true);
    else showImportResult(null, error.message, true);
  } finally {
    state.importBusy = false;
    setBusy(button, false);
  }
});

$('#analyze-button').addEventListener('click', async () => {
  if (!state.activeBundle) return;
  const activeId = state.activeBundle.id;
  const button = $('#analyze-button');
  setBusy(button, true, '正在计算…');
  try {
    const payload = await api(`/api/bundles/${encodeURIComponent(activeId)}/analyze`, { method: 'POST', body: '{}' });
    if (state.activeBundle?.id === activeId) renderBundle(payload.bundle);
    showNotice('全量统计已经刷新。');
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

$('#sample-button').addEventListener('click', async () => {
  if (!state.activeBundle) return;
  const activeId = state.activeBundle.id;
  const button = $('#sample-button');
  setBusy(button, true, '正在生成…');
  try {
    const payload = await api(`/api/bundles/${encodeURIComponent(activeId)}/sample`, {
      method: 'POST',
      body: JSON.stringify({ since: state.selectedSince }),
    });
    if (state.activeBundle?.id === activeId) renderBundle(payload.bundle);
    showNotice('证据样本已写入本机数据包，没有发送到模型。');
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

$('#report-button').addEventListener('click', async () => {
  if (!state.activeBundle) return;
  const activeId = state.activeBundle.id;
  const button = $('#report-button');
  setBusy(button, true, '正在生成…');
  try {
    const payload = await api(`/api/bundles/${encodeURIComponent(activeId)}/report`, { method: 'POST', body: '{}' });
    if (state.activeBundle?.id === activeId) renderBundle(payload.bundle);
    showNotice('完整报告已在本机生成。');
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

$('#refresh-button').addEventListener('click', async () => {
  const button = $('#refresh-button');
  setBusy(button, true, '刷新中…');
  invalidateSearch();
  try { await loadState(true); if (state.filters.mode === 'content' && state.filters.query.trim()) await searchContent(); }
  catch (error) { showNotice(error.message, true); }
  finally { setBusy(button, false); }
});

function renderArchive(archive) {
  const media = archive?.media || {};
  const classes = archive?.classification || {};
  const count = (n) => (Number.isFinite(Number(n)) ? Number(n) : 0).toLocaleString('zh-CN');
  const phase = media.refreshing ? '正在补充图片预览' : media.state === 'completed' ? '文件归档完成' : media.state === 'running' ? '文件归档进行中' : '等待归档状态';
  $('#archive-summary').textContent = `${phase} · ${count(media.archived_files)} / ${count(media.total_files)} 个文件 · ${count(media.viewable_images)} 个可查看图片记录 · ${count(classes.classified_conversations)} 个会话已分类。本轮附件处理 ${count(media.processed_files)}、跳过 ${count(media.skipped_existing_files)}；分类处理 ${count(classes.processed_conversations)}、跳过 ${count(classes.skipped_conversations)}。`;
  $('#total-images').textContent = media.viewable_images === undefined ? '—' : count(media.viewable_images);
  const voice = archive?.voice || {}, transcription = archive?.voice_transcription || {};
  const voiceStates = { completed: '语音已归档', running: '正在归档语音', partial: '部分语音已归档', error: '语音归档需检查', blocked: '语音归档待处理', not_started: '尚未归档语音' };
  const voiceBadge = $('#voice-status-badge');
  voiceBadge.textContent = voiceStates[voice.state] || '等待语音状态';
  voiceBadge.classList.toggle('is-ready', voice.state === 'completed');
  voiceBadge.classList.toggle('is-running', voice.state === 'running' || transcription.state === 'running');
  voiceBadge.classList.toggle('is-warning', ['error', 'partial', 'blocked'].includes(voice.state) || ['partial', 'error'].includes(transcription.state));
  $('#voice-archive-summary').textContent = `已保存 ${count(voice.archived_voice_messages)} / ${count(voice.total_voice_messages)} 条语音的录音；本机共保留 ${count(voice.unique_audio_files)} 份独立音频。${count(voice.missing_voice_messages)} 条暂未找到本机音频。`;
  const voiceCounts = $('#voice-archive-counts'); voiceCounts.replaceChildren();
  [['已完成转写', `${count(transcription.transcribed_audio)} 个音频`], ['待转写', `${count(transcription.pending_audio)} 个音频`], ['可回听', `${count(transcription.playable_audio)} 个音频`], ['已附到消息', `${count(transcription.attached_messages)} 条`]].forEach(([title, value]) => {
    const item = el('div'); item.append(el('strong', '', title), el('span', '', value)); voiceCounts.append(item);
  });
  const transcriptionStates = { running: '正在本机转写', complete: '本轮转写完成', partial: '部分音频转写失败，已保留成功结果', pending: '转写等待继续', error: '本轮转写需检查', needs_runtime: '转写准备中', needs_archive: '等待语音归档', not_started: '尚未开始转写' };
  const preparation = transcription.code === 'local_voice_runtime_missing' ? '本机模型或音频解码组件尚未就绪，已保存音频继续保留。' : '';
  $('#voice-transcribe-summary').textContent = `${transcriptionStates[transcription.state] || '等待转写状态'}：本轮处理 ${count(transcription.processed_audio)}、跳过 ${count(transcription.skipped_audio)}、失败 ${count(transcription.failed_audio)}；${count(transcription.empty_audio)} 个音频未识别到文字。${preparation}`;
}

window.setInterval(async () => {
  try {
    const payload = await api('/api/sync-status');
    renderSync(payload.sync || {});
    renderArchive(payload.archive || {});
  } catch (_error) {
    markSyncStale();
  }
}, 15000);

api('/api/sync-status').then((payload) => renderArchive(payload.archive || {})).catch(() => {
  $('#archive-summary').textContent = '归档状态暂时不可用，请稍后刷新。';
});

initializeCatalog();
loadState(false).catch((error) => {
  $('#environment-badge').textContent = '服务异常';
  showNotice(error.message, true);
});
