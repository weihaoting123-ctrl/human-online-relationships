/* Local aggregate reader. No requests, persistence, HTML parsing or chart math. */
(() => {
  'use strict';
  const controllers = new WeakMap(), hosts = new WeakMap();
  let serial = 0, active = null;
  const textNode = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const svgNode = (tag, attrs) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  };
  const tone = (value) => value === 'me' || value === 'other' ? value : 'neutral';
  const clamp = (value, low, high) => Math.min(Math.max(low, high), Math.max(low, value));

  function detach(target) {
    controllers.get(target)?.destroy();
    target?.querySelectorAll('[data-chart-probe]').forEach((child) => controllers.get(child)?.destroy());
  }

  function reader(target, host, items, locate, anchor, draw = () => {}, release = () => {}, dock = null) {
    const original = new Map(['tabindex', 'aria-describedby', 'aria-keyshortcuts', 'data-chart-probe']
      .map((name) => [name, target.getAttribute(name)]));
    const previousTargetClass = target.classList.contains('chart-probe-target');
    const hostState = hosts.get(host) || { count: 0, existing: host.classList.contains('chart-probe-host') };
    hostState.count += 1; hosts.set(host, hostState); host.classList.add('chart-probe-host');
    target.classList.add('chart-probe-target'); target.setAttribute('data-chart-probe', '');
    target.setAttribute('tabindex', '0');
    target.setAttribute('aria-keyshortcuts', 'ArrowLeft ArrowRight ArrowUp ArrowDown Home End Escape');
    const tip = textNode('div', 'chart-probe-tooltip');
    tip.id = `chart-probe-${++serial}`; tip.setAttribute('role', 'tooltip'); tip.hidden = true;
    const title = textNode('strong', 'chart-probe-title'), list = textNode('dl', 'chart-probe-values');
    tip.append(title, list);
    const help = textNode('span', 'chart-probe-sr', '移动指针或轻点查看；聚焦后用方向键逐项读取，Home 跳到首项，End 跳到末项，Escape 关闭读数。');
    help.id = `${tip.id}-help`;
    const live = textNode('span', 'chart-probe-sr');
    live.setAttribute('aria-live', 'polite'); live.setAttribute('aria-atomic', 'true');
    target.setAttribute('aria-describedby', [original.get('aria-describedby'), help.id, tip.id].filter(Boolean).join(' '));
    host.append(tip, help, live);
    const listeners = [];
    let index = 0, visible = false, disposed = false, timer;
    const on = (node, type, callback) => {
      node.addEventListener(type, callback); listeners.push(() => node.removeEventListener(type, callback));
    };
    const stopTimer = () => { clearTimeout(timer); timer = null; };
    function hide() {
      stopTimer(); visible = false; tip.hidden = true; live.textContent = ''; draw(null);
      if (active === control) { active = null; removalObserver.disconnect(); }
    }
    function position() {
      const point = anchor(index), bounds = host.getBoundingClientRect();
      if (!point || !bounds.width) { hide(); return; }
      tip.style.maxWidth = `${Math.max(0, bounds.width - 16)}px`;
      const width = tip.offsetWidth, height = tip.offsetHeight;
      if (dock?.offsetHeight) {
        dock.style.height = `${Math.max(140, height + 16)}px`;
        tip.style.left = `${Math.max(8, (host.clientWidth - width) / 2)}px`;
        tip.style.top = `${dock.getBoundingClientRect().top - bounds.top - host.clientTop + host.scrollTop + 8}px`;
        return;
      }
      const x = point.x - bounds.left - host.clientLeft + host.scrollLeft;
      const y = point.y - bounds.top - host.clientTop + host.scrollTop;
      const preferred = x + 18 + width <= host.clientWidth - 8 ? x + 18 : x - width - 18;
      tip.style.left = `${clamp(preferred, 8, host.clientWidth - width - 8)}px`;
      tip.style.top = `${clamp(y - height / 2, 8, host.clientHeight - height - 8)}px`;
    }
    function show(next, announce = false) {
      if (disposed || !target.isConnected || next < 0 || next >= items.length) { hide(); return; }
      stopTimer();
      if (active && active !== control) active.hide();
      const changed = next !== index || !visible;
      index = next; const item = items[index];
      if (changed) {
        title.textContent = String(item.label ?? ''); list.replaceChildren();
        (item.rows || []).forEach((row) => {
          const entry = textNode('div', `chart-probe-row chart-probe-${tone(row.tone)}`);
          entry.append(textNode('dt', '', row.label ?? ''), textNode('dd', '', row.value ?? '—')); list.append(entry);
        });
      }
      visible = true; tip.hidden = false; active = control;
      removalObserver.observe(document.documentElement, { childList: true, subtree: true });
      draw(index); position();
      if (announce && visible) live.textContent = `${item.label}。${(item.rows || []).map((row) => `${row.label} ${row.value}`).join('，')}`;
    }
    function pointer(event) {
      // Suppress synthesized mouse focus/blur on a touch selection, not pan-y.
      if (event.type === 'pointerdown' && event.pointerType === 'touch') event.preventDefault();
      const next = locate(event);
      // A card may extend beyond the plot on narrow screens. Keep it readable
      // there; forward positions inside the plot so it never blocks scrubbing.
      if (next === null) {
        if (!tip.contains(event.target) && !tip.contains(event.relatedTarget) && !dock?.offsetHeight) hide();
        return;
      }
      show(next);
    }
    on(target, 'pointermove', pointer);
    on(target, 'pointerdown', pointer);
    on(target, 'pointercancel', hide);
    on(target, 'pointerleave', (event) => {
      if (event.pointerType === 'touch') return;
      if (!tip.contains(event.relatedTarget) && !dock?.contains(event.relatedTarget)) timer = setTimeout(hide, 100);
    });
    on(tip, 'pointerenter', stopTimer);
    if (target.namespaceURI === 'http://www.w3.org/2000/svg') {
      on(tip, 'pointermove', pointer); on(tip, 'pointerdown', pointer); on(tip, 'pointercancel', hide);
    }
    on(tip, 'pointerleave', (event) => {
      if (event.pointerType !== 'touch' && !target.contains(event.relatedTarget) && !dock?.contains(event.relatedTarget)) hide();
    });
    if (dock) {
      on(dock, 'pointerenter', stopTimer);
      on(dock, 'pointerleave', (event) => {
        if (event.pointerType !== 'touch' && !target.contains(event.relatedTarget) && !tip.contains(event.relatedTarget)) hide();
      });
    }
    on(target, 'focus', () => show(index, true));
    on(target, 'blur', hide);
    on(target, 'keydown', (event) => {
      if (event.target !== target || event.altKey || event.ctrlKey || event.metaKey) return;
      const delta = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
      if (event.key === 'Escape') {
        if (visible) { event.preventDefault(); event.stopPropagation(); hide(); }
        return;
      }
      if (delta === undefined && event.key !== 'Home' && event.key !== 'End') return;
      event.preventDefault(); event.stopPropagation();
      show(event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : clamp(index + delta, 0, items.length - 1), true);
    });
    const control = { target, tip, hide, destroy() {
      if (disposed) return;
      disposed = true; hide(); listeners.forEach((remove) => remove());
      tip.remove(); help.remove(); live.remove(); release();
      original.forEach((value, name) => value === null ? target.removeAttribute(name) : target.setAttribute(name, value));
      if (!previousTargetClass) target.classList.remove('chart-probe-target');
      hostState.count -= 1;
      if (!hostState.count) { if (!hostState.existing) host.classList.remove('chart-probe-host'); hosts.delete(host); }
      if (controllers.get(target) === control) controllers.delete(target);
    } };
    controllers.set(target, control);
    return control.destroy;
  }

  const removalObserver = new MutationObserver(() => {
    if (active && !active.target.isConnected) active.hide();
  });
  document.addEventListener('pointerdown', (event) => {
    // Updating a reading can replace the tapped text node before this bubbles.
    const path = event.composedPath();
    if (active && !path.includes(active.target) && !path.includes(active.tip)) active.hide();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && active) { active.hide(); event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  window.addEventListener('resize', () => active?.hide());
  window.addEventListener('blur', () => active?.hide());
  document.addEventListener('scroll', () => active?.hide(), true);

  function attach(svg, { points = [], plot } = {}) {
    detach(svg);
    const values = points.filter((point) => Number.isFinite(point.x));
    if (!svg?.parentNode || !values.length || !plot || !['left', 'right', 'top', 'bottom'].every((key) => Number.isFinite(plot[key]))) return () => {};
    const layer = svgNode('g', { class: 'chart-probe-layer', 'aria-hidden': 'true' });
    layer.style.display = 'none';
    const guide = svgNode('line', { class: 'chart-probe-guide', y1: plot.top, y2: plot.bottom });
    const dots = svgNode('g', {}); layer.append(guide, dots); svg.append(layer);
    const dock = textNode('div', 'chart-probe-dock');
    dock.setAttribute('aria-hidden', 'true'); svg.after(dock);
    function locate(event) {
      const matrix = svg.getScreenCTM();
      if (!matrix) return null;
      const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
      // Native pointer coordinates round slightly before the inverse transform.
      const tolerance = .001;
      if (p.x < plot.left - tolerance || p.x > plot.right + tolerance || p.y < plot.top - tolerance || p.y > plot.bottom + tolerance) return null;
      let lo = 0, hi = values.length - 1;
      while (lo < hi) { const mid = Math.floor((lo + hi) / 2); if (values[mid].x < p.x) lo = mid + 1; else hi = mid; }
      return lo && p.x - values[lo - 1].x <= values[lo].x - p.x ? lo - 1 : lo;
    }
    return reader(svg, svg.parentNode, values, locate, (index) => {
      const matrix = svg.getScreenCTM();
      return matrix ? new DOMPoint(values[index].x, (plot.top + plot.bottom) / 2).matrixTransform(matrix) : null;
    }, (index) => {
      layer.style.display = index === null ? 'none' : '';
      if (index === null) return;
      const point = values[index];
      guide.setAttribute('x1', point.x); guide.setAttribute('x2', point.x);
      dots.replaceChildren();
      (point.markers || []).filter((marker) => Number.isFinite(marker.y)).forEach((marker) => dots.append(svgNode('circle', {
        cx: point.x, cy: marker.y, r: 4, class: `chart-probe-dot chart-probe-${tone(marker.tone)}`,
      })));
    }, () => { layer.remove(); dock.remove(); }, dock);
  }

  function attachRows(container, items = []) {
    detach(container);
    const values = items.filter((item) => item.element && container.contains(item.element));
    if (!values.length) return () => {};
    return reader(container, container, values, (event) => {
      let selected = 0, nearest = Infinity;
      const boxes = values.map((item) => item.element.getBoundingClientRect());
      const left = Math.min(...boxes.map((box) => box.left)), right = Math.max(...boxes.map((box) => box.right));
      const top = Math.min(...boxes.map((box) => box.top)), bottom = Math.max(...boxes.map((box) => box.bottom));
      if (event.clientX < left || event.clientX > right || event.clientY < top || event.clientY > bottom) return null;
      values.forEach((item, index) => {
        const box = boxes[index];
        const distance = Math.abs(event.clientY - (box.top + box.height / 2));
        if (distance < nearest) { nearest = distance; selected = index; }
      }); return selected;
    }, (index) => {
      const box = values[index].element.getBoundingClientRect();
      return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
    }, (index) => values.forEach((item, i) => item.element.classList.toggle('chart-probe-row-active', index === i)),
    () => values.forEach((item) => item.element.classList.remove('chart-probe-row-active')));
  }
  window.ChartProbe = { attach, attachRows, detach };
})();
