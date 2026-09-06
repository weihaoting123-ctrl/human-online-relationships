'use strict';
(() => {
  // Rebrand the view without rewriting any previously exported private HTML.
  document.title = '语音档案 · 人类线上关系可视化';
  const records = JSON.parse(document.getElementById('voice-data').textContent);
  const el = id => document.getElementById(id);
  const labels = {done: '已转文字', pending: '待转写', empty: '未识别到文字', error: '转写失败', missing: '缺少音频', ambiguous: '关联待确认'};
  let page = 0;
  const pageSize = 40;
  const textNode = (tag, text, className) => {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function render(reset) {
    if (reset) page = 0;
    const query = el('voice-query').value.trim().normalize('NFKC').toLocaleLowerCase();
    const state = el('voice-state').value;
    const from = el('voice-from').value;
    const to = el('voice-to').value;
    const sort = el('voice-sort').value;
    const filtered = records.filter(row => (!state || row.state === state)
      && (!query || `${row.contact}\n${row.text}`.normalize('NFKC').toLocaleLowerCase().includes(query))
      && (!from || row.date.slice(0, 10) >= from)
      && (!to || (row.date && row.date.slice(0, 10) <= to)));
    filtered.sort((a, b) => sort === 'longest' ? b.duration - a.duration : sort === 'contact'
      ? a.contact.localeCompare(b.contact, 'zh-CN') || b.date.localeCompare(a.date)
      : sort === 'oldest' ? a.date.localeCompare(b.date) : b.date.localeCompare(a.date));
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.max(0, Math.min(page, pages - 1));
    const list = el('voice-list');
    list.querySelectorAll('audio').forEach(audio => audio.pause());
    list.replaceChildren();
    filtered.slice(page * pageSize, (page + 1) * pageSize).forEach(row => {
      const card = document.createElement('article');
      card.className = 'voice-card';
      const heading = document.createElement('div');
      heading.className = 'card-heading';
      heading.append(textNode('h2', row.contact), textNode('span', labels[row.state] || '待处理', 'badge ' + (row.state === 'done' ? 'done' : '')));
      card.append(heading, textNode('p', `${row.date || '日期未知'}${row.duration ? ' · ' + row.duration + ' 秒' : ''}`, 'meta'));
      if (row.audio && /^audio\/[a-f0-9]{64}\.wav$/.test(row.audio)) {
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.preload = 'none';
        audio.src = row.audio;
        audio.setAttribute('aria-label', '播放这条语音');
        audio.addEventListener('play', () => list.querySelectorAll('audio').forEach(other => {if (other !== audio) other.pause();}));
        card.append(audio);
      }
      card.append(textNode('p', row.text || (row.state === 'missing' ? '目前本机未找到对应音频；聊天中的语音记录仍然保留。' : row.state === 'empty' ? '可以播放原音核对。未识别到可用文字。' : '文字尚未生成。'), row.text ? 'transcript' : 'muted'));
      list.append(card);
    });
    if (!filtered.length) list.append(textNode('p', '没有符合条件的语音，请调整关键词或日期。', 'empty'));
    el('voice-count').textContent = `共 ${filtered.length.toLocaleString()} 条匹配记录`;
    el('voice-page').textContent = `${page + 1} / ${pages}`;
    el('voice-prev').disabled = page === 0;
    el('voice-next').disabled = page + 1 >= pages;
  }
  for (const id of ['voice-query', 'voice-state', 'voice-from', 'voice-to', 'voice-sort']) el(id).addEventListener('input', () => render(true));
  el('voice-prev').addEventListener('click', () => {page--; render(false);});
  el('voice-next').addEventListener('click', () => {page++; render(false);});
  render(true);
})();
