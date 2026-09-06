/* Deterministic local aggregates only. All labels, including model summaries,
 * become text nodes; no HTML, external fonts or chart services are used. */
(() => {
  'use strict';
  const n = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const fmt = (value) => n(value).toLocaleString('zh-CN');
  const node = (tag, cls = '', text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  const svgNode = (tag, attrs = {}, text) => {
    const value = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, item]) => value.setAttribute(key, String(item)));
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  function table(headers, rows, caption) {
    const wrap = node('div', 'ai-data-table');
    const value = node('table');
    if (caption) value.append(node('caption', 'sr-only', caption));
    const head = node('thead'), tr = node('tr');
    headers.forEach((label) => { const th = node('th', '', label); th.scope = 'col'; tr.append(th); });
    head.append(tr); value.append(head);
    const body = node('tbody');
    rows.forEach((row) => {
      const line = node('tr');
      row.forEach((item, index) => {
        const cell = node(index ? 'td' : 'th', '', item);
        if (!index) cell.scope = 'row';
        line.append(cell);
      }); body.append(line);
    });
    value.append(body); wrap.append(value); return wrap;
  }
  function fallback(parent, headers, rows, title) {
    const details = node('details', 'ai-chart-data');
    details.append(node('summary', '', '查看数值表'), table(headers, rows, title)); parent.append(details);
  }
  function panel(title, subtitle, wide = false) {
    const value = node('section', `ai-chart-panel${wide ? ' ai-chart-wide' : ''}`);
    value.append(node('h3', '', title), node('p', 'field-note', subtitle)); return value;
  }
  function bars(parent, rows, { stacked = false, sparse = false } = {}) {
    if (!rows.length || !rows.some((row) => n(row.count))) {
      parent.append(node('p', 'chart-empty', '此范围暂无可绘制的记录。')); return;
    }
    const width = 800, height = 170, plotHeight = 123, inset = 10;
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${parent.querySelector('h3').textContent}，具体数值见下方数值表` });
    const max = Math.max(...rows.map((row) => n(row.count)), 1);
    const step = (width - 2 * inset) / rows.length, bw = Math.max(.5, step * .72);
    [0, .5, 1].forEach((part) => svg.append(svgNode('line', { x1: inset, x2: width - inset, y1: 15 + plotHeight * part, y2: 15 + plotHeight * part, class: 'ai-chart-gridline' })));
    rows.forEach((row, index) => {
      const x = inset + index * step + (step - bw) / 2;
      const totalHeight = plotHeight * n(row.count) / max;
      if (stacked) {
        const mine = Math.min(n(row.me), n(row.count));
        const mineHeight = plotHeight * mine / max;
        svg.append(svgNode('rect', { x, y: 138 - mineHeight, width: bw, height: mineHeight, class: 'ai-bar-me' }));
        svg.append(svgNode('rect', { x, y: 138 - totalHeight, width: bw, height: Math.max(0, totalHeight - mineHeight), class: 'ai-bar-other' }));
      } else svg.append(svgNode('rect', { x, y: 138 - totalHeight, width: bw, height: totalHeight, rx: rows.length < 30 ? 2 : 0, class: 'ai-bar-me' }));
      const labelEvery = sparse ? Math.max(1, Math.ceil(rows.length / 7)) : 1;
      if (index % labelEvery === 0 || index === rows.length - 1) {
        const first = index === 0, last = index === rows.length - 1;
        svg.append(svgNode('text', { x: first ? inset : last ? width - inset : x + bw / 2, y: 159, 'text-anchor': first ? 'start' : last ? 'end' : 'middle', class: 'ai-chart-label' }, row.label));
      }
    }); parent.append(svg);
  }
  function distribution(parent, rows) {
    const max = Math.max(...rows.map((row) => n(row.count)), 1);
    const list = node('div', 'ai-distribution');
    rows.forEach((row) => {
      const item = node('div', 'ai-distribution-row');
      item.append(node('span', '', row.label));
      const track = node('span', 'ai-distribution-track'), fill = node('span');
      fill.style.width = `${n(row.count) / max * 100}%`; track.append(fill);
      item.append(track, node('strong', '', fmt(row.count))); list.append(item);
    }); parent.append(list);
  }
  function duration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return '暂无样本';
    const value = n(seconds);
    if (value < 60) return `${Math.round(value)} 秒`;
    if (value < 3600) return `${(value / 60).toFixed(1)} 分钟`;
    return `${(value / 3600).toFixed(1)} 小时`;
  }
  function renderMetrics(target, metrics, range = '') {
    target.replaceChildren(); target.hidden = !metrics?.totals;
    if (!metrics?.totals) return;
    const totals = metrics.totals || {};
    const heading = node('div', 'section-heading'), title = node('div');
    title.append(node('p', 'eyebrow', '本机统计 / 所选范围的全部记录'), node('h2', '', '对话留下的节奏'));
    heading.append(title, node('span', 'status-badge is-ready', '本机计算 · 不发送图表数据'));
    target.append(heading, node('p', 'field-note', `${range}。统计覆盖所选范围全部消息，与 AI 的抽样或完成进度无关。`));
    const ledger = node('dl', 'ai-metric-ledger');
    [['消息', totals.messages, '条'], ['活跃日期', totals.active_days, '天'], ['对话时段', totals.sessions, '段'], ['文字量', totals.characters, '字']].forEach(([label, value, unit]) => {
      const item = node('div'); item.append(node('dt', '', label), node('dd', '', `${fmt(value)} ${unit}`)); ledger.append(item);
    }); target.append(ledger);
    const grid = node('div', 'ai-chart-grid');
    const activity = Array.isArray(metrics.activity) ? metrics.activity : [];
    const monthly = metrics.activity_granularity === 'month';
    const activityTitle = monthly ? (n(metrics.activity_month_step) > 1 ? `每 ${fmt(metrics.activity_month_step)} 个月的消息变化` : '按月的消息变化') : '按日的消息变化';
    const activityPanel = panel(activityTitle, '蓝色为我，青色为对方或其他成员；仅表示消息数量。', true);
    bars(activityPanel, activity.map((row) => ({ ...row, label: String(row.date).slice(monthly ? 0 : 5) })), { stacked: true, sparse: true });
    fallback(activityPanel, [monthly ? '月份' : '日期', '全部', '我', '对方 / 其他成员'], activity.map((row) => [row.date, fmt(row.count), fmt(row.me), fmt(row.other)]), activityTitle);
    grid.append(activityPanel);
    const hours = Array.isArray(metrics.hours) ? metrics.hours : [];
    const hoursPanel = panel('一天中的消息分布', '按本机统计时区汇总，0–23 时。');
    bars(hoursPanel, hours.map((row) => ({ ...row, label: `${row.hour}` })), { sparse: true });
    fallback(hoursPanel, ['小时', '消息数'], hours.map((row) => [`${row.hour}:00`, fmt(row.count)]), '24 小时消息分布'); grid.append(hoursPanel);
    const weekNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    const weekdays = (Array.isArray(metrics.weekdays) ? metrics.weekdays : []).map((row) => ({ ...row, label: weekNames[row.weekday] || '未知' }));
    const weekPanel = panel('一周中的消息分布', '累计消息数；各星期在所选区间内出现次数可能不同。');
    bars(weekPanel, weekdays); fallback(weekPanel, ['星期', '消息数'], weekdays.map((row) => [row.label, fmt(row.count)]), '一周消息分布'); grid.append(weekPanel);
    const typeNames = { text: '文字', image: '图片', voice: '语音', video: '视频', emoji: '表情', file: '文件', system: '系统', other: '其他' };
    const types = (Array.isArray(metrics.types) ? metrics.types : []).map((row) => ({ ...row, label: typeNames[row.type] || '其他' }));
    const typePanel = panel('消息构成', `语音 ${fmt(totals.voice)} 条，其中 ${fmt(totals.transcribed_voice)} 条有转写。`);
    distribution(typePanel, types); grid.append(typePanel);
    const senderPanel = panel('发言份额', '数量不代表投入程度；群聊中其他成员合并统计。');
    const shares = [{ label: '我', count: n(totals.me) }, { label: '对方 / 其他成员', count: n(totals.other) }];
    const total = shares.reduce((sum, row) => sum + row.count, 0);
    const track = node('div', 'ai-sender-track'); track.setAttribute('aria-hidden', 'true');
    shares.forEach((row, index) => { const portion = node('span', index ? 'ai-sender-other' : 'ai-sender-me'); portion.style.width = `${total ? row.count / total * 100 : 0}%`; track.append(portion); });
    senderPanel.append(track, table(['发言方', '消息数', '份额'], shares.map((row) => [row.label, fmt(row.count), `${total ? (row.count / total * 100).toFixed(1) : '0.0'}%`]), '发言份额'));
    grid.append(senderPanel);
    const replyPanel = panel('相邻换人消息的间隔', '这是可计算的消息时间差，不能等同实际回复速度或意愿。', true);
    const response = metrics.response_times || {};
    const medians = [['me', '我'], ['other', '对方 / 其他成员']];
    const longest = Math.max(...medians.map(([key]) => n(response[key]?.median_seconds)), 1);
    const replyBars = node('div', 'ai-reply-bars');
    medians.forEach(([key, label]) => {
      const row = node('div', 'ai-reply-row');
      row.append(node('span', '', `${label} · 中位数`));
      const track = node('span', 'ai-distribution-track'), fill = node('span');
      fill.style.width = `${n(response[key]?.median_seconds) / longest * 100}%`;
      track.setAttribute('aria-hidden', 'true'); track.append(fill);
      row.append(track, node('strong', '', duration(response[key]?.median_seconds))); replyBars.append(row);
    }); replyPanel.append(replyBars);
    replyPanel.append(table(['后发言方', '间隔样本', '中位数', '90% 的间隔不超过'], medians.map(([key, label]) => [label, fmt(response[key]?.samples), duration(response[key]?.median_seconds), duration(response[key]?.p90_seconds)]), '相邻换人消息间隔'));
    grid.append(replyPanel); target.append(grid);
    const methodology = node('details', 'ai-methodology'), methodList = node('ul');
    const notes = Array.isArray(metrics.methodology) ? metrics.methodology : Object.values(metrics.methodology || {});
    notes.filter((line) => typeof line === 'string').forEach((line) => methodList.append(node('li', '', line)));
    methodology.append(node('summary', '', '统计口径与限制'), methodList); target.append(methodology);
  }
  function renderCoverage(target, { eligible, analyzed = 0, segments = 0, completed, mode = 'sample', planned = false }) {
    target.replaceChildren();
    const label = node('div', 'ai-coverage-label');
    const knownTotal = eligible !== null && eligible !== undefined;
    label.append(node('strong', '', mode === 'full' ? '全量文字覆盖' : '抽样文字覆盖'), node('span', '', `${planned ? '计划' : '已分析'} ${fmt(analyzed)}${knownTotal ? ` / ${fmt(eligible)} 条` : ' 条 · 未记录可用文字总量'}`));
    const track = node('div', 'ai-coverage-track'); track.setAttribute('aria-hidden', 'true');
    const fill = node('span'); fill.style.width = `${Math.min(100, n(eligible) ? n(analyzed) / n(eligible) * 100 : 0)}%`; track.append(fill);
    target.append(label, track);
    if (n(segments)) target.append(node('p', 'field-note', planned ? `按时间顺序处理 ${fmt(segments)} 个分段` : `已完成 ${fmt(completed)} / ${fmt(segments)} 个分段`));
  }
  function renderSegments(target, segments) {
    target.replaceChildren();
    if (!Array.isArray(segments) || !segments.length) return;
    const details = node('details', 'ai-segment-timeline');
    details.append(node('summary', '', `逐段时间线 · ${fmt(segments.length)} 段`));
    const list = node('ol');
    const stateNames = { completed: '已完成', pending: '未开始', running: '进行中', error: '未完成', cancelled: '已停止' };
    segments.forEach((segment, index) => {
      const item = node('li', segment.state === 'completed' ? 'is-completed' : '');
      const heading = node('div', 'ai-segment-heading');
      heading.append(node('strong', '', `${index + 1} · ${segment.date_from || '日期未知'} — ${segment.date_to || '日期未知'}`), node('span', 'status-badge', `${stateNames[segment.state] || '未完成'}${segment.cached ? ' · 已缓存' : ''}`));
      item.append(heading, node('p', 'field-note', `${fmt(segment.messages)} 条 · ${fmt(segment.characters)} 字`));
      if (segment.summary) item.append(node('p', 'ai-segment-summary', String(segment.summary).slice(0, 240)));
      list.append(item);
    }); details.append(list); target.append(details);
  }
  window.AnalysisCharts = { renderMetrics, renderCoverage, renderSegments };
})();
