// Offline browser tests for the gate snapshot picker and the trading-day
// (as_of) dropdown: candidates come only from /api/sync/status registered_days.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const staticRoot = path.join(__dirname, '../../src/stock_manager/web/static');
const html = fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(staticRoot, 'app.js'), 'utf8');

/** Minimal DOM stand-in carrying the ids renderDbVersions/fillTradingDayOptions touch. */
function node(id) {
  return {
    id, hidden: false, disabled: false, value: '', textContent: '', innerHTML: '',
    dataset: {}, style: {}, listeners: {}, children: [],
    setAttribute(name, value) { this.dataset[name] = value; },
    addEventListener(name, listener) { this.listeners[name] = listener; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
}

/** @param {object} [settings] @returns {object} */
function browser(settings = {}) {
  const ids = ['db-versions', 'db-select-title', 'db-cutoff-hint', 'gate-enter', 'trading-day'];
  const nodes = new Map();
  ids.forEach((id) => nodes.set('#' + id, node(id)));
  const context = vm.createContext({
    document: {
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    window: { addEventListener() {}, matchMedia: () => ({ matches: true }) },
    URLSearchParams, console, Date,
    setInterval() { return 1; }, clearInterval() {},
  });
  vm.runInContext(source, context);
  vm.runInContext(`bindEvents = () => {};
    prepareWorkspaceResources = loadInstances = loadVersion = startSyncPolling = () => {};
    loadSyncStatus = renderSyncStatus = () => {};`, context);
  return { context, nodes, run: (code) => vm.runInContext(code, context) };
}

const STATUS = {
  latest_synced_trading_day: '2026-09-04',
  stocks_count: 5215,
  coverage_start: '2018-07-01',
  coverage_end: '2026-09-04',
  active_generation: { generation: 'cand-2023af33f6d93396-20260905130947085941', activated_at: '2026-09-05T20:22:54+08:00' },
  registered_days: [
    { trading_day: '2026-09-04', source: 'baostock', synced_at: '2026-09-06T07:57:38+08:00' },
    { trading_day: '2026-09-01', source: 'baostock', synced_at: '2026-09-02T01:01:45+08:00' },
  ],
};

test('门禁页只列出已登记快照日并默认选最新', () => {
  const b = browser();
  b.run(`renderDbVersions(${JSON.stringify(STATUS)}, true)`);
  const boxHtml = b.nodes.get('#db-versions').innerHTML;
  assert.match(boxHtml, /2026-09-04/);
  assert.match(boxHtml, /2026-09-01/);
  assert.match(boxHtml, /\(最新\)/);
  assert.match(boxHtml, /2026-09-04[\s\S]*checked/);   // 最新项默认选中
  // 候选交易日只允许已登记日(09-04/09-01),不带 value= 的其它交易日。
  const offered = [...boxHtml.matchAll(/name="db-version" value="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(offered, ['2026-09-04', '2026-09-01']);
  assert.match(boxHtml, /cand-2023af33f6d93396-20260905130947085941/);
  // 默认已选最新已登记快照 → 进入按钮直接可用。
  assert.equal(b.nodes.get('#gate-enter').hidden, false);
  assert.equal(b.nodes.get('#gate-enter').disabled, false);
});

test('选择较早快照后交易日下拉候选仍只含已登记日并选中所选', () => {
  const b = browser();
  b.run(`renderDbVersions(${JSON.stringify(STATUS)}, true)`);
  // 模拟用户点选 09-01(radio change 以所选日强制同步下拉)。
  b.run(`state.selectedSnapshotDay = '2026-09-01'; fillTradingDayOptions('2026-09-01');`);
  const selectHtml = b.nodes.get('#trading-day').innerHTML;
  const offered = [...selectHtml.matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(offered, ['2026-09-04', '2026-09-01']);
  assert.equal(b.nodes.get('#trading-day').value, '2026-09-01');
});

test('无已登记快照或不可进入时门禁不放行且下拉为空', () => {
  const b = browser();
  b.run(`renderDbVersions({ latest_synced_trading_day: null, active_generation: null,
    registered_days: [], can_enter: false }, false)`);
  assert.equal(b.nodes.get('#db-versions').innerHTML, '');
  assert.equal(b.nodes.get('#gate-enter').hidden, true);
  b.run(`state.registeredDays = []; fillTradingDayOptions();`);
  assert.equal(b.nodes.get('#trading-day').innerHTML, '<option value="">选择已登记快照日…</option>');
});
