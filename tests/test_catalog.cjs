const test = require('node:test');
const assert = require('node:assert/strict');
const Catalog = require('../dashboard/static/catalog.js');
const defaults = { query: '', mode: 'names', kind: 'all', topic: 'all', from: '', to: '', sort: 'recent_desc' };
const items = [
  { id: 'b', contact: '项目 ＡＢＣ 10', message_count: 2, date_range: ['2026-01-01', '2026-09-01'], conversation_kind: 'direct', primary_topic: '工作与项目协作' },
  { id: 'a', contact: '项目 abc 2', message_count: 100, date_range: ['2026-03-01', '2026-06-01'], conversation_kind: 'group', primary_topic: '混合主题', candidate_topics: ['工作与项目协作'] },
  { id: 'c', contact: '<script>.*%', message_count: 10, date_range: null, conversation_kind: 'unknown' },
];
const ids = (overrides = {}, matches = null) => Catalog.select(items, { ...defaults, ...overrides }, matches).map((item) => item.id);
test('Chinese, fullwidth and multiple literal terms match without regex', () => {
  assert.deepEqual(ids({ query: '项目 abc' }), ['b', 'a']);
  assert.deepEqual(ids({ query: '.*%' }), ['c']);
  assert.deepEqual(ids({ query: 'project' }), []);
});
test('Numeric sorts use values, not lexical strings', () => {
  assert.deepEqual(ids({ sort: 'messages_desc' }), ['a', 'c', 'b']);
  assert.deepEqual(ids({ sort: 'messages_asc' }), ['b', 'c', 'a']);
});
test('Actual chat dates determine ordering; unknown remains last', () => {
  assert.deepEqual(ids(), ['b', 'a', 'c']);
  assert.deepEqual(ids({ sort: 'oldest_asc' }), ['b', 'a', 'c']);
});
test('Names use natural Chinese collation with numeric suffix', () => {
  assert.deepEqual(ids({ query: '项目', sort: 'name_asc' }), ['a', 'b']);
  assert.deepEqual(ids({ query: '项目', sort: 'name_desc' }), ['b', 'a']);
});
test('Topic candidates and type filters combine', () => {
  assert.deepEqual(ids({ topic: '工作与项目协作' }), ['b', 'a']);
  assert.deepEqual(ids({ topic: '工作与项目协作', kind: 'group' }), ['a']);
});
test('Inclusive overlap ranges exclude unknown dates and invalid range', () => {
  assert.deepEqual(ids({ from: '2026-06-01', to: '2026-06-01' }), ['b', 'a']);
  assert.deepEqual(ids({ from: '2026-08-01' }), ['b']);
  assert.deepEqual(ids({ from: '2026-08-01', to: '2026-06-01' }), []);
});
test('Content waits for server and then uses matches with exact-message dates', () => {
  const matches = new Map([['a', { match_count: 5 }], ['c', { match_count: 8 }]]);
  assert.deepEqual(ids({ query: 'keyword', mode: 'content' }), []);
  assert.deepEqual(ids({ query: 'keyword', mode: 'content', sort: 'matches_desc', from: '2026-07-01' }, matches), ['c', 'a']);
  assert.deepEqual(ids({ query: '   ', mode: 'content' }), ['b', 'a', 'c']);
});
test('Stable ties never mutate input and pagination clamps/reset-safe', () => {
  const before = JSON.stringify(items);
  ids({ sort: 'name_asc' });
  assert.equal(JSON.stringify(items), before);
  const many = Array.from({ length: 62 }, (_, i) => i);
  const page = Catalog.paginate(many, 99, 25);
  assert.equal(page.page, 3);
  assert.deepEqual(page.items, many.slice(50));
  assert.equal(Catalog.paginate([], 4, 50).page, 1);
  assert.equal(Catalog.paginate(many, -1, 1).size, 25);
});
