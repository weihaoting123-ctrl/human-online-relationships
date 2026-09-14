/* Content-free calendar. No I/O, persistence, original messages or AI actions.
 * Dates are server-local ISO days; UTC is used only for calendar arithmetic. */
(() => {
  'use strict';
  const make = (tag, className = '', text) => {
    const node = document.createElement(tag); node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const button = (text, className, label = text) => {
    const node = make('button', className, text); node.type = 'button';
    node.setAttribute('aria-label', label); return node;
  };
  const number = (value) => value.toLocaleString('zh-CN');
  const iso = (year, month, day) => `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  const parseDay = (value) => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const [y, m, d] = value.split('-').map(Number);
    if (y < 2000 || y > 2100 || new Date(Date.UTC(y, m - 1, d)).toISOString().slice(0, 10) !== value) return null;
    return value;
  };
  const daysIn = (year, month) => new Date(Date.UTC(year, month, 0)).getUTCDate();
  const pad = (year, month) => (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7;
  const weekdays = ['一', '二', '三', '四', '五', '六', '日'];

  function read(local) {
    const frequency = local?.frequency, coverage = frequency?.daily_coverage;
    const asOf = parseDay(typeof local?.as_of === 'string' ? local.as_of.slice(0, 10) : null);
    if (!asOf || coverage?.complete !== true || coverage.timezone !== 'server_local'
      || coverage.basis !== 'selected_scope_non_system_non_future' || !Array.isArray(frequency.daily)) return null;
    const from = parseDay(coverage.date_from), to = parseDay(coverage.date_to);
    const empty = coverage.date_from === null && coverage.date_to === null;
    if ((!empty && (!from || !to || from > to || to > asOf)) || frequency.daily.length > 36890) return null;
    const counts = new Map();
    let max = 0, latest = null;
    for (const row of frequency.daily) {
      const date = parseDay(row?.date), count = row?.count;
      if (!date || !Number.isSafeInteger(count) || count <= 0 || empty || date < from || date > to || counts.has(date)) return null;
      counts.set(date, count); max = Math.max(max, count);
      if (!latest || date > latest) latest = date;
    }
    const selectedEnd = parseDay(local?.scope?.date_to);
    const initial = latest || (selectedEnd && selectedEnd < asOf ? selectedEnd : asOf);
    return {counts, max, from, to, asOf, initial};
  }

  function render(target, local) {
    if (!target) return;
    target.replaceChildren(); target.classList.add('rt-density', 'dc-calendar');
    const caption = make('figcaption', 'dc-heading');
    const identity = make('div', 'dc-identity');
    identity.append(make('strong', '', '联系密度'), make('span', 'dc-subtitle', '逐日消息 · 本机统计'));
    caption.append(identity); target.append(caption);
    const data = read(local);
    if (!data) {
      target.append(make('p', 'dc-unavailable rt-note', '这份记录未保存完整有效的逐日统计。旧报告保留原貌；重新打开会话或生成本机预览后可查看日历，不会自动重新分析。'));
      return;
    }
    let year = Number(data.initial.slice(0, 4)), month = Number(data.initial.slice(5, 7)), mode = 'month';
    const minYear = Math.min(year, Number((data.from || data.initial).slice(0, 4)));
    const maxYear = Math.max(year, Number((data.to || data.initial).slice(0, 4)));
    const level = (n) => n ? Math.min(4, Math.ceil(n / data.max * 4)) : 0;
    const dayState = (date) => date > data.asOf ? 'future'
      : !data.from || date < data.from || date > data.to ? 'outside'
        : data.counts.has(date) ? 'active' : 'zero';
    const countFor = (date) => data.counts.get(date) || 0;
    const dayLabel = (date) => `${date} · ${dayState(date) === 'future' ? '未来日期（统计时点之后）'
      : dayState(date) === 'outside' ? '统计范围外' : `${number(countFor(date))} 条${countFor(date) ? '' : ' · 归档中无有效消息'}`}`;
    const totals = (prefix) => {
      const first = prefix.length === 5 ? `${prefix}01-01` : `${prefix}-01`;
      const last = prefix.length === 5 ? `${prefix}12-31`
        : `${prefix}-${daysIn(Number(prefix.slice(0, 4)), Number(prefix.slice(5, 7)))}`;
      if (first > data.asOf) return '未来日期';
      if (!data.from || first > data.to || last < data.from) return '统计范围外';
      let total = 0, active = 0;
      for (const [date, count] of data.counts) if (date.startsWith(prefix)) { total += count; active++; }
      return `${number(total)} 条 · ${number(active)} 个活跃日`;
    };
    const segments = make('div', 'dc-segments'); segments.setAttribute('role', 'group');
    segments.setAttribute('aria-label', '联系密度显示方式');
    const monthView = button('月', '', '月视图'), yearView = button('年', '', '年视图');
    segments.append(monthView, yearView); caption.append(segments);
    const toolbar = make('div', 'dc-toolbar');
    const title = make('strong', 'dc-period-title');
    const navigation = make('div', 'dc-navigation');
    const yearSelect = make('select', 'dc-year'); yearSelect.setAttribute('aria-label', '联系密度年份');
    for (let y = minYear; y <= maxYear; y++) { const option = make('option', '', `${y} 年`); option.value = String(y); yearSelect.append(option); }
    const previous = button('‹', 'dc-step'), next = button('›', 'dc-step');
    navigation.append(yearSelect, previous, next); toolbar.append(title, navigation);
    const summary = make('p', 'dc-summary'); summary.setAttribute('role', 'status');
    const content = make('div', 'dc-content');
    const readout = make('p', 'dc-readout', '指向日期查看条数，也可用键盘或点按。');
    readout.setAttribute('role', 'status'); readout.setAttribute('aria-live', 'polite'); readout.setAttribute('aria-atomic', 'true');
    const legend = make('div', 'dc-legend'); legend.setAttribute('aria-label', '热度图例，颜色越深消息越多');
    legend.append(make('span', '', '少'));
    for (let i = 0; i <= 4; i++) {
      const swatch = make('span', 'dc-swatch'); swatch.dataset.level = String(i);
      swatch.title = i ? `日峰值的 ${(i - 1) * 25}%–${i * 25}%` : '0 条';
      swatch.setAttribute('aria-hidden', 'true'); legend.append(swatch);
    }
    legend.append(make('span', '', '多'), make('span', 'dc-scale', `按所选范围日峰值 ${number(data.max)} 条分档`));
    const note = make('p', 'rt-note dc-note', `颜色只表示消息数量，不等于感情强度。0 条仅表示本机归档中无有效消息，归档可能缺漏。统计截至 ${data.asOf}。`);
    target.append(toolbar, summary, content, readout, legend, note);

    function showDay(date) {
      readout.textContent = dayLabel(date);
      content.querySelectorAll('.dc-pointed').forEach((node) => node.classList.remove('dc-pointed'));
      content.querySelector(`[data-date="${date}"]`)?.classList.add('dc-pointed');
    }
    function monthGrid(y, m, mini = false) {
      const grid = make(mini ? 'span' : 'div', mini ? 'dc-mini-grid' : 'dc-grid');
      grid.setAttribute('aria-label', `${y} 年 ${m} 月每日消息`);
      if (mini) grid.setAttribute('aria-hidden', 'true');
      else weekdays.forEach((name) => grid.append(make('span', 'dc-weekday', name)));
      for (let i = 0; i < pad(y, m); i++) { const gap = make('span', 'dc-padding'); gap.setAttribute('aria-hidden', 'true'); grid.append(gap); }
      for (let d = 1; d <= daysIn(y, m); d++) {
        const date = iso(y, m, d), status = dayState(date), n = countFor(date);
        const cell = mini ? make('span', 'dc-mini-day') : button('', 'dc-day', dayLabel(date));
        cell.dataset.state = status; cell.dataset.level = String(level(n));
        if (!mini) {
          cell.dataset.date = date;
          cell.append(make('span', 'dc-day-number', String(d)), make('span', 'dc-day-count',
            status === 'future' || status === 'outside' ? '—' : number(n)));
          cell.addEventListener('pointerenter', () => showDay(date));
          cell.addEventListener('focus', () => showDay(date));
          cell.addEventListener('click', () => showDay(date));
          cell.addEventListener('keydown', (event) => {
            const shifts = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7};
            let destination = event.key === 'Home' ? 1 : event.key === 'End' ? daysIn(y, m) : d + shifts[event.key];
            if (!Number.isFinite(destination)) return;
            event.preventDefault(); destination = Math.max(1, Math.min(daysIn(y, m), destination));
            grid.querySelector(`[data-date="${iso(y, m, destination)}"]`)?.focus();
          });
        }
        grid.append(cell);
      }
      return grid;
    }
    function draw(focusFirst = false) {
      title.textContent = mode === 'month' ? `${year} 年 ${month} 月` : `${year} 年`;
      yearSelect.value = String(year);
      monthView.setAttribute('aria-pressed', String(mode === 'month'));
      yearView.setAttribute('aria-pressed', String(mode === 'year'));
      previous.setAttribute('aria-label', mode === 'month' ? '上一月' : '上一年');
      next.setAttribute('aria-label', mode === 'month' ? '下一月' : '下一年');
      previous.disabled = year === minYear && (mode === 'year' || month === 1);
      next.disabled = year === maxYear && (mode === 'year' || month === 12);
      summary.textContent = `${totals(mode === 'year' ? `${year}-` : iso(year, month, 1).slice(0, 7))} · 所选范围内`;
      readout.textContent = mode === 'month' ? '指向日期查看条数，也可用方向键或点按。' : '选择月份，查看逐日联系密度。';
      content.replaceChildren();
      if (mode === 'month') content.append(monthGrid(year, month));
      else {
        const months = make('div', 'dc-year-grid');
        for (let m = 1; m <= 12; m++) {
          const prefix = iso(year, m, 1).slice(0, 7), total = totals(prefix);
          const tile = button('', 'dc-month', `${year} 年 ${m} 月 · ${total}，查看每日`);
          tile.dataset.month = String(m);
          tile.append(make('strong', '', `${m} 月`), monthGrid(year, m, true), make('span', 'dc-month-total', total));
          tile.addEventListener('click', () => { month = m; mode = 'month'; draw(true); }); months.append(tile);
        }
        content.append(months);
      }
      if (focusFirst) content.querySelector('.dc-day')?.focus();
    }
    monthView.addEventListener('click', () => { mode = 'month'; draw(); });
    yearView.addEventListener('click', () => { mode = 'year'; draw(); });
    yearSelect.addEventListener('change', () => { year = Number(yearSelect.value); draw(); });
    const move = (direction) => {
      if (mode === 'year') year += direction;
      else { month += direction; if (month < 1) { month = 12; year--; } if (month > 12) { month = 1; year++; } }
      draw();
    };
    previous.addEventListener('click', () => move(-1)); next.addEventListener('click', () => move(1));
    draw();
  }
  window.DensityCalendar = Object.freeze({render});
})();
