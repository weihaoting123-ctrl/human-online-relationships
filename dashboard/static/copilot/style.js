/* Only three bounded preferences persist. Never store chat, identity or consent. */
(() => {
  'use strict';
  const key = 'copilot.reply-style.v1';
  const labels = {
    tone: { natural: '自然随口', playful: '轻松俏皮', gentle: '温柔真诚', direct: '简洁直接' },
    empathy: { restrained: '点到为止', balanced: '刚刚好', attentive: '多一点体贴' },
    length: { short: '一两句', normal: '稍展开' },
  };
  const defaults = { tone: 'natural', empathy: 'balanced', length: 'short' };
  const ids = { tone: 'reply-tone', empathy: 'reply-empathy', length: 'reply-length' };
  const valid = (value) => value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 3
    && Object.keys(defaults).every((field) => typeof value[field] === 'string'
      && Object.hasOwn(labels[field], value[field]));
  const read = () => Object.fromEntries(Object.entries(ids).map(([field, id]) => [field, document.getElementById(id).value]));
  const matches = (a, b) => valid(a) && valid(b) && Object.keys(defaults).every((field) => a[field] === b[field]);
  const describe = (value) => valid(value) ? Object.keys(defaults).map((field) => labels[field][value[field]]).join(' · ') : '风格设置不可用';
  function renderExample() {
    const value = read();
    const starts = { natural: '忙一天了，先歇会儿吧', playful: '今天的电量用完了，先充会儿电', gentle: '忙了一天，辛苦啦，先好好歇会儿', direct: '先休息，其他的明天再说' };
    const ends = { restrained: '', balanced: '，别再给自己加任务了', attentive: '。想吐槽的话我听着，不想说就先歇着' };
    document.getElementById('reply-style-example').textContent = starts[value.tone] + ends[value.empathy]
      + (value.length === 'normal' ? '。今晚就慢慢来。' : '。');
    document.getElementById('reply-style-summary').textContent = describe(value);
  }
  function init(onChange) {
    let value = defaults;
    try {
      const saved = JSON.parse(localStorage.getItem(key));
      if (valid(saved)) value = saved;
    } catch (_) { /* Unavailable or invalid storage uses safe defaults. */ }
    for (const [field, id] of Object.entries(ids)) document.getElementById(id).value = value[field];
    for (const id of Object.values(ids)) document.getElementById(id).addEventListener('change', () => {
      const current = read();
      if (!valid(current)) return;
      try { localStorage.setItem(key, JSON.stringify(current)); } catch (_) { /* Controls still work in memory. */ }
      renderExample(); onChange();
    });
    renderExample();
  }
  window.CopilotStyle = Object.freeze({ read, matches, describe, init });
})();
