/* Presentation boundary: safe aggregate DTOs only. No message text, network calls,
 * identity matching, causal inference, or analysis authorization lives here. */
(() => {
  'use strict';
  const types = { first_record: '最早记录', last_record: '区间末条记录', frequent_phase: '高频阶段', gap: '记录间隔',
    plan: '计划', update: '进展', outcome: '结果', context: '背景' };
  const statuses = { planned: '提出计划', in_progress: '正在推进', realized: '自述已落实', cancelled: '自述已取消', unknown: '进展未知' };
  const levels = { reported: '当事人自述 · 未独立核实', inferred: 'AI 推断 · 待验证', insufficient: '证据不足' };
  const statusLabel = (node) => node.evidence_level !== 'reported' && ['realized', 'cancelled'].includes(node.status)
    ? (node.status === 'realized' ? '可能已落实' : '可能已取消') : statuses[node.status] || statuses.unknown;
  const instances = new WeakMap();
  const pageSize = 25;
  let dialogOwner = null, returnFocus = null, closeTimer = null;
  const dialog = () => document.getElementById('relationship-event-dialog');
  const text = (value, limit = 600) => typeof value === 'string' ? value.slice(0, limit) : '';
  const day = (value) => /^\d{4}-\d{2}-\d{2}/.test(text(value)) ? value.slice(0, 10) : '';
  const count = (value) => number(Math.max(0, Number(value) || 0));
  const range = (from, to) => day(from) ? `${day(from)}${day(to) && day(to) !== day(from) ? ` — ${day(to)}` : ''}` : '日期未记录';
  const button = (label, className = 'quiet-button') => {
    const node = el('button', className, label); node.type = 'button'; return node;
  };

  function close(immediate = false) {
    const node = dialog();
    clearTimeout(closeTimer);
    if (!node?.open) return;
    if (immediate || matchMedia('(prefers-reduced-motion: reduce)').matches) {
      node.classList.remove('rt-closing'); node.close(); dialogOwner = null;
    } else {
      node.classList.add('rt-closing');
      closeTimer = setTimeout(() => close(true), 200);
    }
  }
  function clear(target) {
    if (!target) return;
    if (dialogOwner === target) close(true);
    instances.delete(target); target.replaceChildren(); target.hidden = true;
  }
  function evidence(container, anchors) {
    const list = el('ul', 'rt-evidence');
    (Array.isArray(anchors) ? anchors : []).forEach((anchor) => {
      if (!day(anchor?.date) || !Number.isInteger(anchor.sample_index) || anchor.sample_index < 1) return;
      list.append(el('li', '', `${day(anchor.date)} · 样本序号 ${anchor.sample_index}`));
    });
    container.append(el('h3', '', '依据位置 · 不展示原话'));
    container.append(list.children.length ? list : el('p', 'rt-note', '没有可定位的依据；不能据此确认原因或进展。'));
  }
  function showEvent(view, event, trigger) {
    close(true);
    const node = dialog();
    if (!node || instances.get(view.target) !== view) return;
    returnFocus = trigger; dialogOwner = view.target;
    $('#relationship-event-title').textContent = event.title;
    const body = $('#relationship-event-body'); body.replaceChildren();
    body.append(el('p', 'rt-badge', event.basis === 'ai' ? 'AI 样本解释 · 未独立核实' : '本机统计 · 不解释动机'));
    body.append(el('p', 'rt-event-date', range(event.from, event.to)));
    body.append(el('p', '', event.summary));
    if (event.basis === 'ai') {
      body.append(el('p', 'rt-note', `${statusLabel(event.raw)} · ${levels[event.raw.evidence_level] || levels.insufficient}`));
      const related = view.events.find((item) => item.basis === 'ai' && item.raw.id === event.raw.related_event_id);
      if (related) body.append(el('p', 'rt-related', `关联计划 / 事件：${related.title}（${range(related.from, related.to)}）`));
      evidence(body, event.raw.evidence);
      body.append(el('p', 'rt-note', '只代表所选样本中的解释，不证明现实中的行动已经发生，也不代表完整关系。'));
    } else if (event.type === 'gap') {
      body.append(el('p', 'rt-note', '原因未知。记录间隔可能受归档缺漏、线下交流等影响，不能推断对方动机。'));
    }
    if (view.partial) body.append(el('p', 'rt-coverage', '部分结果：未完成的范围没有被解释，不能视为完整结论。'));
    const analyze = node.querySelector('.rt-analyze-range');
    analyze.hidden = !view.options.onSelectRange || !day(event.from) || !window.ArchiveShell?.isEnabled('analysis');
    analyze.onclick = () => {
      if (instances.get(view.target) !== view || !window.ArchiveShell?.isEnabled('analysis')) return;
      close(true); view.options.onSelectRange(day(event.from), day(event.to) || day(event.from));
    };
    node.classList.remove('rt-closing'); node.showModal();
    node.querySelector('.rt-dialog-close').focus();
  }

  function eventsFrom(local, semantic) {
    const localEvents = (Array.isArray(local?.nodes) ? local.nodes : []).filter((node) =>
      ['first_record', 'last_record', 'frequent_phase', 'gap'].includes(node.type)).map((node) => ({
      basis: 'local', type: node.type, title: types[node.type], raw: node,
      from: node.start_at || node.at, to: node.end_at || node.at,
      summary: node.type === 'gap' ? `${count(node.elapsed_days)} 天没有可用消息记录。`
        : node.type === 'frequent_phase' ? `这一阶段共有 ${count(node.message_count)} 条记录，达到本机会话的高频阈值。`
          : node.type === 'first_record' ? '所选区间内最早的可用消息记录，不代表关系开始。'
            : '所选区间内末条可用消息记录，不替代全归档最近记录。',
    }));
    const aiEvents = semantic?.generated === true && Array.isArray(semantic.events) ? semantic.events
      .filter((node) => ['plan', 'update', 'outcome', 'context'].includes(node.kind)).map((node) => ({
        basis: 'ai', type: node.kind, title: text(node.title, 140) || types[node.kind], raw: node,
        from: node.date_from, to: node.date_to, summary: text(node.summary),
      })) : [];
    return [...localEvents, ...aiEvents];
  }
  function density(target, local) {
    const weekly = (Array.isArray(local?.frequency?.weekly) ? local.frequency.weekly : [])
      .filter((week) => day(week.week_start)).slice().sort((a, b) => a.week_start.localeCompare(b.week_start));
    if (!weekly.length) { target.append(el('p', 'rt-note', '这个范围没有可绘制的周频率数据。')); return; }
    const figure = el('figure', 'rt-density');
    const caption = el('figcaption');
    caption.append(el('strong', '', '联系密度'), el('span', '', `每周消息 · 高频阈值 ${count(local.frequency.threshold)} 条`));
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 800 64'); svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `所选范围每周消息密度；${weekly.length} 个已显示活跃周，深色表示高频。不代表感情强度。`);
    const first = Date.parse(day(weekly[0].week_start)), last = Date.parse(day(weekly.at(-1).week_start));
    const span = Math.max(604800000, last - first + 604800000);
    const max = Math.max(1, ...weekly.map((week) => Number(week.count) || 0));
    weekly.forEach((week) => {
      const rect = document.createElementNS(svg.namespaceURI, 'rect');
      const height = Math.max(2, Math.min(60, (Number(week.count) || 0) / max * 60));
      const width = Math.max(.7, 604800000 / span * 800 - 3);
      [['x', (Date.parse(day(week.week_start)) - first) / span * 800], ['y', 64 - height], ['width', width], ['height', height], ['rx', Math.min(3, width / 2)]]
        .forEach(([key, value]) => rect.setAttribute(key, String(value)));
      rect.setAttribute('class', week.frequent ? 'rt-frequent' : '');
      const title = document.createElementNS(svg.namespaceURI, 'title');
      title.textContent = `${week.week_start} 起一周 · ${count(week.count)} 条${week.frequent ? ' · 高频' : ''}`;
      rect.append(title); svg.append(rect);
    });
    const endpoints = el('div', 'rt-density-dates');
    endpoints.append(el('span', '', weekly[0].week_start), el('span', '', weekly.at(-1).week_start));
    figure.append(caption, svg, endpoints, el('p', 'rt-note', '频率只反映归档中的互动密度，不等于感情强度。空白可能表示没有记录或数据未返回。'));
    target.append(figure);
  }
  function renderList(view) {
    if (dialogOwner === view.target) close(true);
    const filtered = view.events.filter((event) => view.filter.value === 'all' || event.type === view.filter.value)
      .slice().sort((a, b) => (day(a.from).localeCompare(day(b.from))) * (view.order.value === 'desc' ? -1 : 1));
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    view.page = Math.min(view.page, pages);
    view.list.replaceChildren();
    filtered.slice((view.page - 1) * pageSize, view.page * pageSize).forEach((event) => {
      const row = el('li', 'rt-stop');
      const node = button('', 'rt-node'); node.dataset.basis = event.basis;
      node.setAttribute('aria-haspopup', 'dialog'); node.setAttribute('aria-controls', 'relationship-event-dialog');
      const date = el('time', 'rt-node-date', range(event.from, event.to));
      if (day(event.from)) date.dateTime = day(event.from);
      const content = el('span', 'rt-node-content');
      const heading = el('span', 'rt-node-heading');
      heading.append(el('strong', '', event.title), el('span', `rt-badge rt-badge-${event.basis}`, event.basis === 'ai' ? 'AI 样本解释' : '本机统计'));
      content.append(heading, el('span', 'rt-node-summary', event.summary));
      if (event.basis === 'ai') content.append(el('span', 'rt-node-status', `${statusLabel(event.raw)} · ${levels[event.raw.evidence_level] || levels.insufficient}`));
      node.append(date, content, el('span', 'rt-node-open', '详情 ›'));
      node.addEventListener('click', () => showEvent(view, event, node)); row.append(node); view.list.append(row);
    });
    if (!filtered.length) view.list.append(el('li', 'rt-empty', '这个筛选下没有时间节点。可切换类型，或选择其他日期范围。'));
    view.status.textContent = `${view.page} / ${pages} 页 · ${count(filtered.length)} 个节点`;
    view.previous.disabled = view.page <= 1; view.next.disabled = view.page >= pages;
    view.target.dataset.page = String(view.page);
  }

  function render(target, options = {}) {
    if (!target) return;
    clear(target); target.hidden = false;
    const { local, semantic } = options;
    const partial = Boolean(options.partial || (semantic?.generated === true && semantic?.coverage?.complete === false));
    const header = el('header', 'rt-heading');
    header.append(el('div', '', ''), el('span', 'rt-heading-note', '记录 → 变化 → 依据'));
    header.firstChild.append(el('h2', '', '关系时间线'), el('p', 'rt-note', options.label || '沿着记录，看见互动的疏密与进展。'));
    target.append(header);
    if (partial) target.append(el('p', 'rt-coverage', '部分结果 · 仅覆盖已完成的样本，不能视为完整范围的结论。'));
    if (options.mode !== 'detail') {
      target.append(el('p', 'rt-coverage rt-sample-note', semantic?.generated === true
        ? `AI 样本解释 · 仅限 ${range(semantic.coverage?.date_from, semantic.coverage?.date_to)} 的所选样本。计划与进展未独立核实。`
        : options.mode === 'preview' ? '现在只显示本机统计；还没有调用模型解释计划与进展。'
          : '这份报告没有语义时间线；不会从旧摘要补造计划、进展或停联原因。'));
    }
    const recent = local?.last_contact;
    const recency = el('div', 'rt-recency');
    if (recent?.last_record_at) {
      recency.append(el('strong', '', `距全归档最近记录 ${count(recent.elapsed_days)} 天`),
        el('span', '', `最近记录 ${range(recent.last_record_at)} · ${options.mode === 'report' ? '报告生成时统计' : options.mode === 'preview' ? '预览时统计' : '本机统计截至'} ${text(local.as_of, 40)}`));
    } else recency.append(el('strong', '', '全归档最近联系时间未知'), el('span', '', '没有可用的非系统、非未来时间记录。'));
    recency.append(el('span', 'rt-speaker-recency', `我方最近发言：${day(recent?.last_me_at) || '未知'} · 其他参与者最近发言：${day(recent?.last_other_at) || '未知'}`));
    recency.append(el('small', '', '只表示这份归档中多久没有新记录。归档更新后请刷新，不推断现实中的联系情况。'));
    target.append(recency);
    const scope = local?.scope;
    const scopeRange = scope ? `${!scope.date_from && !scope.date_to ? '全归档 · ' : ''}${range(scope.date_from || scope.first_record_at, scope.date_to || scope.last_record_at)}` : '';
    target.append(el('p', 'rt-scope', scope ? `时间轴统计范围：${scopeRange} · ${count(scope.message_count)} 条记录。此区间的末条记录不是全归档最近联系。` : '这份记录没有本机时间统计，可刷新会话详情查看。'));
    density(target, local);
    const cause = semantic?.no_contact_reason;
    const reason = el('div', 'rt-reason');
    const hasReason = semantic?.generated === true && !partial && ['explicit', 'inferred'].includes(cause?.kind);
    reason.append(el('strong', '', hasReason ? (cause.kind === 'explicit' ? 'AI 识别的自述原因 · 未独立核实' : 'AI 推测的原因 · 待验证') : '少联系的原因未知'));
    reason.append(el('p', '', hasReason ? text(cause.summary) : '记录间隔不能解释动机。缺少充分依据时，不把猜测写成原因。'));
    if (hasReason) evidence(reason, cause.evidence);
    (Array.isArray(cause?.limitations) ? cause.limitations : []).forEach((line) => reason.append(el('p', 'rt-note', text(line))));
    target.append(reason);
    const view = { target, options, partial, events: eventsFrom(local, semantic), page: 1 };
    const controls = el('div', 'rt-controls');
    [['filter', '节点类型', [['all', '全部节点'], ...Object.entries(types).filter(([key]) => view.events.some((event) => event.type === key))]],
      ['order', '时间顺序', [['asc', '从早到晚'], ['desc', '从晚到早']]]].forEach(([key, label, choices]) => {
      const wrapper = el('label', '', label), select = el('select', `rt-${key}`);
      choices.forEach(([value, title]) => { const option = el('option', '', title); option.value = value; select.append(option); });
      view[key] = select; select.addEventListener('change', () => { view.page = 1; renderList(view); });
      wrapper.append(select); controls.append(wrapper);
    });
    target.append(controls);
    view.list = el('ol', 'rt-rail'); view.list.setAttribute('aria-label', '按时间排列的关系记录'); target.append(view.list);
    const pagination = el('div', 'rt-pagination');
    view.previous = button('上一页', 'quiet-button rt-previous'); view.next = button('下一页', 'quiet-button rt-next');
    view.status = el('span', 'rt-page-status'); view.status.setAttribute('role', 'status');
    view.previous.addEventListener('click', () => { view.page -= 1; renderList(view); });
    view.next.addEventListener('click', () => { view.page += 1; renderList(view); });
    pagination.append(view.previous, view.status, view.next); target.append(pagination);
    if (Object.values(local?.limits || {}).some((limit) => limit?.truncated)) target.append(el('p', 'rt-coverage', '本机统计已限制返回数量，当前不是全部节点或周频率；请缩小日期范围查看。'));
    const notices = [...(Array.isArray(local?.notices) ? local.notices : []),
      ...(Array.isArray(semantic?.coverage?.limitations) ? semantic.coverage.limitations : [])];
    if (notices.length) {
      const methodology = el('details', 'rt-methodology');
      methodology.append(el('summary', '', '统计口径与覆盖说明'));
      notices.forEach((line) => methodology.append(el('p', 'rt-note', text(line))));
      target.append(methodology);
    }
    instances.set(target, view); renderList(view);
  }
  dialog()?.querySelector('.rt-dialog-close').addEventListener('click', () => close());
  dialog()?.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
  dialog()?.addEventListener('close', () => {
    if (dialog().open) return;
    dialogOwner = null;
    if (returnFocus?.isConnected && returnFocus.getClientRects().length) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  });
  document.addEventListener('archive:view', () => close(true));
  document.addEventListener('archive:modules', () => close(true));
  window.RelationshipTimeline = { render, clear, close: () => close(true) };
})();
