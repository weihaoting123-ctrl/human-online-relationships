/* Pure catalog operations; no data persistence, DOM access or network. */
(function (root) {
  'use strict';
  const topics = ['工作与项目协作', '商户与业务合作', '交易与物流售后', '付款与账务', '生活与社交', '混合主题', '待确认／低信号'];
  const sorts = ['recent_desc', 'oldest_asc', 'messages_desc', 'messages_asc', 'name_asc', 'name_desc', 'matches_desc'];
  const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
  const normalize = (value) => String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN');
  const tokens = (value) => normalize(value).trim().split(/\s+/u).filter(Boolean);
  const amount = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const date = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : '';
  const firstDate = (bundle) => date(bundle.date_range?.[0]);
  const lastDate = (bundle) => date(bundle.date_range?.[1]);
  function compareDate(a, b, direction) {
    if (!a || !b) return a ? -1 : b ? 1 : 0; // Unknown dates stay last in either direction.
    return a.localeCompare(b) * direction;
  }
  function select(bundles, filters, matches = null) {
    const terms = tokens(filters.query);
    const contentSearch = filters.mode === 'content' && terms.length > 0;
    const from = date(filters.from);
    const to = date(filters.to);
    if (from && to && from > to) return [];
    const selected = bundles.filter((bundle) => {
      if (filters.kind !== 'all' && (bundle.conversation_kind || 'unknown') !== filters.kind) return false;
      if (filters.topic !== 'all' && bundle.primary_topic !== filters.topic && !(bundle.candidate_topics || []).includes(filters.topic)) return false;
      if (bundle.library?.hidden_at || bundle.source_missing) return false;
      if (filters.tag && filters.tag !== 'all' && !(bundle.library?.tags || []).includes(filters.tag)) return false;
      if (contentSearch) return Boolean(matches?.get(bundle.id)?.match_count > 0);
      const searchable = [bundle.contact, bundle.library?.alias, ...(bundle.library?.tag_names || [])].join(' ');
      if (!terms.every((term) => normalize(searchable).includes(term))) return false;
      if ((from || to) && (!firstDate(bundle) || !lastDate(bundle))) return false;
      if (from && lastDate(bundle) < from || to && firstDate(bundle) > to) return false;
      return true;
    });
    const tie = (a, b) => collator.compare(String(a.contact || ''), String(b.contact || '')) || String(a.id).localeCompare(String(b.id));
    return selected.sort((a, b) => {
      const pinned = Number(Boolean(b.library?.pinned)) - Number(Boolean(a.library?.pinned));
      if (pinned) return pinned;
      let order = 0;
      switch (filters.sort) {
        case 'oldest_asc': order = compareDate(firstDate(a), firstDate(b), 1); break;
        case 'messages_desc': order = amount(b.message_count) - amount(a.message_count); break;
        case 'messages_asc': order = amount(a.message_count) - amount(b.message_count); break;
        case 'name_asc': order = collator.compare(String(a.library?.alias || a.contact || ''), String(b.library?.alias || b.contact || '')); break;
        case 'name_desc': order = collator.compare(String(b.library?.alias || b.contact || ''), String(a.library?.alias || a.contact || '')); break;
        case 'matches_desc': order = amount(matches?.get(b.id)?.match_count) - amount(matches?.get(a.id)?.match_count); break;
        default: order = compareDate(lastDate(a), lastDate(b), -1);
      }
      return order || tie(a, b);
    });
  }
  function paginate(items, page, pageSize) {
    const size = [25, 50, 100].includes(Number(pageSize)) ? Number(pageSize) : 25;
    const pages = Math.max(1, Math.ceil(items.length / size));
    const current = Math.min(pages, Math.max(1, Math.floor(Number(page) || 1)));
    return { items: items.slice((current - 1) * size, current * size), page: current, pages, total: items.length, size };
  }
  const api = { topics, sorts, tokens, normalize, select, paginate, firstDate, lastDate };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Catalog = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
