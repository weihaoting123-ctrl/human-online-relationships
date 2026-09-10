/* Read-only, lazy conversation activity. No storage, message text or AI calls. */
(() => {
  'use strict';
  const views = new WeakMap();
  const weekdays = ['一', '二', '三', '四', '五', '六', '日'];
  const count = (n) => Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  const level = (n, max) => n > 0 ? Math.min(4, Math.ceil(n / max * 4)) : 0;
  const parseDay = (value) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const day = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(day.valueOf()) && day.toISOString().slice(0, 10) === value ? day : null;
  };

  function invalidate(s) {
    s.revision += 1;
    s.controller?.abort();
    s.controller = null;
    s.loaded = false;
    s.results.replaceChildren();
    s.apply.disabled = false;
    s.body.setAttribute('aria-busy', 'false');
    s.retry.hidden = true;
  }

  function clear(host) {
    if (!host) return;
    const s = views.get(host);
    if (s) invalidate(s);
    host.ontoggle = null;
    host.open = false;
    host.hidden = true;
    host.replaceChildren();
    views.delete(host);
  }

  function status(s, text, error = false) {
    s.status.textContent = text;
    s.status.setAttribute('role', error ? 'alert' : 'status');
  }

  function validate(from, to) {
    const start = parseDay(from), end = parseDay(to);
    if (!start || !end || from < '2000-01-01' || to > '2100-12-31') return '请选择 2000–2100 年之间的完整起止日期。';
    if (start > end) return '开始日期不能晚于结束日期。';
    if ((end - start) / 86400000 + 1 > 366) return '单次最多展示 366 天，请缩小日期范围。';
    return '';
  }

  async function read(s, scope = {}) {
    invalidate(s);
    const revision = s.revision;
    s.lastScope = scope;
    s.controller = new AbortController();
    s.apply.disabled = true;
    s.body.setAttribute('aria-busy', 'true');
    status(s, '正在读取本机时间统计…');
    try {
      const result = await api('/api/activity/heatmap', {
        method: 'POST', body: JSON.stringify({ bundle_id: s.bundleId, ...scope }), signal: s.controller.signal,
      });
      if (views.get(s.host) !== s || revision !== s.revision) return;
      const data = result.heatmap;
      if (!data || data.version !== 1 || !Array.isArray(data.days) || data.days.length > 366
          || !Array.isArray(data.weekday_hour) || data.weekday_hour.length !== 7
          || data.weekday_hour.some(row => !Array.isArray(row) || row.length !== 24)
          || !data.scope || validate(data.scope.date_from, data.scope.date_to)
          || data.days.some(day => !parseDay(day.date) || !Number.isSafeInteger(day.count) || day.count < 0)) {
        throw new Error('Invalid heatmap response');
      }
      s.from.value = data.scope.date_from;
      s.to.value = data.scope.date_to;
      s.dirty = false;
      render(s.results, data);
      s.loaded = true;
      status(s, `已读取 ${count(data.scope.days)} 天 · 全部统计仅在本机完成。`);
    } catch (error) {
      if (views.get(s.host) !== s || revision !== s.revision) return;
      s.results.replaceChildren();
      status(s, '无法读取热度。请检查本机服务与所选范围，再点击重试；未自动重试。', true);
      s.retry.hidden = false;
    } finally {
      if (views.get(s.host) === s && revision === s.revision) {
        s.controller = null;
        s.apply.disabled = false;
        s.body.setAttribute('aria-busy', 'false');
      }
    }
  }

  function calendar(data) {
    const grid = el('div', 'ch-calendar');
    const max = Math.max(0, ...data.days.map(day => day.count));
    const months = new Map();
    const today = data.as_of.slice(0, 10);
    for (const day of data.days) {
      const month = day.date.slice(0, 7);
      if (!months.has(month)) months.set(month, []);
      months.get(month).push(day);
    }
    for (const [month, days] of months) {
      const card = el('section', 'ch-month');
      const title = el('h4', '', `${month.slice(0, 4)} 年 ${Number(month.slice(5))} 月`);
      const cells = el('div', 'ch-month-grid');
      for (const name of weekdays) cells.append(el('span', 'ch-weekday', name));
      days.forEach((day, i) => {
        const cell = el('div', 'ch-day');
        cell.dataset.level = String(level(day.count, max));
        // UTC is only used to lay out already server-local date strings.
        if (!i) cell.style.gridColumnStart = String((parseDay(day.date).getUTCDay() + 6) % 7 + 1);
        const future = day.date > today;
        cell.classList.toggle('ch-future', future);
        cell.setAttribute('aria-label', `${day.date}，${future ? '未来日期，不计消息' : `${count(day.count)} 条消息`}`);
        cell.title = cell.getAttribute('aria-label');
        cell.append(el('span', 'ch-date', String(Number(day.date.slice(8)))),
          el('span', 'ch-count', future ? '—' : count(day.count)));
        cells.append(cell);
      });
      card.append(title, cells);
      grid.append(card);
    }
    return grid;
  }

  function hourTable(data) {
    const wrapper = el('div', 'ch-hour-scroll');
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', '星期与小时消息分布，可横向滚动');
    const table = el('table', 'ch-hour-table');
    table.append(el('caption', '', '每格为该范围内对应星期与小时的消息总量，不是平均值。'));
    const head = el('thead'), headers = el('tr');
    const corner = el('th', '', '星期 / 时'); corner.scope = 'col'; headers.append(corner);
    for (let hour = 0; hour < 24; hour += 1) {
      const th = el('th', '', `${String(hour).padStart(2, '0')}`); th.scope = 'col'; headers.append(th);
    }
    head.append(headers);
    const body = el('tbody');
    const max = Math.max(0, ...data.weekday_hour.flat());
    data.weekday_hour.forEach((hours, weekday) => {
      const row = el('tr'), th = el('th', '', `周${weekdays[weekday]}`); th.scope = 'row'; row.append(th);
      hours.forEach((value, hour) => {
        const td = el('td', '', count(value));
        td.dataset.level = String(level(value, max));
        td.title = `周${weekdays[weekday]} ${hour}:00–${hour}:59 · ${count(value)} 条`;
        row.append(td);
      });
      body.append(row);
    });
    table.append(head, body); wrapper.append(table); return wrapper;
  }

  function render(target, data) {
    const summary = el('div', 'ch-summary');
    for (const [label, value, note] of [
      ['范围内消息', count(data.summary.message_count), '所有非系统消息类型'],
      ['有记录的日子', `${count(data.summary.active_days)} / ${count(data.scope.days)}`, '按归档日历日统计'],
      ['日均消息', count(data.summary.daily_average), '包含零消息日期'],
    ]) {
      const item = el('div', 'ch-stat');
      item.append(el('span', '', label), el('strong', '', value), el('small', '', note)); summary.append(item);
    }
    const coverage = el('p', 'ch-coverage', `${data.scope.date_from} — ${data.scope.date_to} · 本机时区 · 统计时点 ${data.as_of.replace('T', ' ')}`);
    const archive = el('p', 'ch-note', data.available_range.date_to
      ? `归档最新有效日期：${data.available_range.date_to}。默认 90 天以此为终点，不代表已同步到今天。`
      : '档案尚无有效消息日期，默认范围以本机今天为终点。');
    target.append(coverage, summary, archive);
    if (!data.summary.message_count) target.append(el('p', 'ch-empty', '此范围没有有效归档消息，不等于现实中没有联系。可调整日期或核对归档覆盖。'));
    const key = el('div', 'ch-legend');
    key.append(el('span', '', '单日消息 · 少'));
    for (let i = 0; i <= 4; i += 1) {
      const swatch = el('span', 'ch-swatch'); swatch.dataset.level = String(i); swatch.setAttribute('aria-hidden', 'true'); key.append(swatch);
    }
    key.append(el('span', '', '多 · 色阶相对本次峰值，数字为实际条数'));
    target.append(key, calendar(data));
    const hoursHeading = el('h3', 'ch-subtitle', '一周里，消息出现在何时');
    target.append(hoursHeading, el('p', 'ch-note', '可横向滚动查看 24 小时；这一色阶相对时段峰值，与日历单独计算。'), hourTable(data));
    if (data.top_days.length) {
      target.append(el('h3', 'ch-subtitle', '消息最多的日子'));
      const top = el('ol', 'ch-top-days');
      for (const day of data.top_days) top.append(el('li', '', `${day.date} · ${count(day.count)} 条`));
      target.append(top);
    }
    const excluded = data.excluded;
    target.append(el('p', 'ch-method', `消息频率不代表关系质量。归档空白不等于现实中没有联系。按本机时间统计，未来日期不计消息；全档案排除无效时间 ${count(excluded.invalid_timestamp)} 条、未来消息 ${count(excluded.future_timestamp)} 条、系统消息 ${count(excluded.system_messages)} 条。相同内容的真实多条记录分别计数。`));
  }

  function mount(host, bundleId) {
    if (!host) return;
    clear(host);
    const heading = el('summary', 'ch-heading');
    heading.append(el('strong', '', '聊天热度'), el('span', '', '看见日常交流的节律 · 本机统计'));
    const body = el('div', 'ch-body'), form = el('form', 'ch-form');
    const from = el('input', 'ch-from'), to = el('input', 'ch-to');
    for (const [input, text] of [[from, '开始日期'], [to, '结束日期']]) {
      input.type = 'date'; input.min = '2000-01-01'; input.max = '2100-12-31';
      const label = el('label', '', text); label.append(input); form.append(label);
    }
    const apply = el('button', 'primary-button ch-apply', '查看范围'); apply.type = 'submit';
    const reset = el('button', 'quiet-button ch-reset', '最近归档 90 天'); reset.type = 'button';
    form.append(apply, reset);
    const message = el('p', 'ch-status', '展开后读取本机统计，不调用 AI。');
    message.setAttribute('role', 'status');
    const retry = el('button', 'quiet-button ch-retry', '重试读取'); retry.type = 'button'; retry.hidden = true;
    const results = el('div', 'ch-results');
    body.append(form, el('p', 'ch-note', '首末日都计入，单次最多 366 天。统计不改变原始聊天、备份或分析范围。'), message, retry, results);
    host.append(heading, body); host.hidden = false;
    const s = { host, bundleId, body, from, to, apply, status: message, retry, results, revision: 0, loaded: false, dirty: false, controller: null, lastScope: {} };
    views.set(host, s);
    host.ontoggle = () => {
      if (views.get(host) !== s) return;
      if (host.open && s.dirty) status(s, '范围已修改，点击“查看范围”更新热度。');
      else if (host.open && !s.loaded) read(s, s.lastScope);
      if (!host.open) { invalidate(s); status(s, '展开后读取本机统计，不调用 AI。'); }
    };
    for (const input of [from, to]) input.addEventListener('input', () => {
      s.dirty = true;
      invalidate(s); status(s, '范围已修改，点击“查看范围”更新热度。');
    });
    form.noValidate = true;
    form.addEventListener('submit', event => {
      event.preventDefault();
      const error = validate(from.value, to.value);
      if (error) { invalidate(s); status(s, error, true); return; }
      read(s, { date_from: from.value, date_to: to.value });
    });
    reset.addEventListener('click', () => read(s));
    retry.addEventListener('click', () => read(s, s.lastScope));
  }

  window.ChatHeatmap = Object.freeze({ mount, clear });
})();
