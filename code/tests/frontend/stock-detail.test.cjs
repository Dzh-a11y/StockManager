// Offline UI contracts: independent detail, persisted panels, and local CAPM races.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../src/stock_manager/web/static/app.js'), 'utf8');
const panels = ['screen-panel', 'backtest-panel', 'template-panel', 'strategy-panel',
  'results-panel', 'backtest-results-panel', 'stock-detail-panel', 'maintenance-panel'];

/** @returns {{promise: Promise<unknown>, resolve: Function, reject: Function}} */
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/** @param {string} id @returns {object} */
function node(id = '') {
  const classes = new Set();
  return {
    id, hidden: false, textContent: '', innerHTML: '', value: '', disabled: false,
    dataset: {}, style: {}, attributes: {}, listeners: {}, children: [],
    classList: { toggle(key, active) { if (active) classes.add(key); else classes.delete(key); },
      contains(key) { return classes.has(key); } },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key] ?? null; },
    addEventListener(key, fn) { this.listeners[key] = fn; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; this.value = ''; },
    querySelector(selector) { return selector === '[data-panel-toggle]' ? this.toggle : null; },
    scrollIntoView(options) { this.scroll = options; },
  };
}

/** @param {object} settings @returns {object} */
function browser(settings = {}) {
  const nodes = new Map();
  const ids = ['result-body', 'result-revision', 'stock-detail-title', 'stock-detail-subtitle',
    'stock-detail-empty', 'stock-detail-analysis', 'stock-rules', 'stock-kline', 'kline-canvas',
    'kline-info', 'stock-capm-results', 'capm-benchmark', 'capm-rate-term', 'capm-periods-per-year',
    'capm-rate-info', 'capm-options-status', 'capm-options-retry', 'toast', 'screen-progress',
    'run-screen', 'screen-progress-track', 'screen-current'];
  ids.forEach((id) => nodes.set('#' + id, node(id)));
  const panelNodes = panels.map((id) => {
    const panel = node(id);
    panel.dataset.panelId = id;
    const contentId = id === 'stock-detail-panel' ? 'stock-detail-content' : id + '-content';
    panel.toggle = node();
    panel.toggle.setAttribute('aria-controls', contentId);
    panel.toggle.setAttribute('aria-expanded', 'true');
    nodes.set('#' + id, panel);
    nodes.set('#' + contentId, node(contentId));
    return panel;
  });
  const stored = new Map(Object.entries(settings.storage || {}));
  const requests = [], warnings = [], drawings = [];
  const context = vm.createContext({
    document: {
      querySelector: (selector) => nodes.get(selector) || null,
      querySelectorAll: (selector) => selector === '[data-panel-id]' ? panelNodes : [],
      getElementById: (id) => nodes.get('#' + id) || null,
      createElement: () => node(), addEventListener() {},
    },
    window: { addEventListener() {}, matchMedia: () => ({ matches: true }),
      requestAnimationFrame: (fn) => fn(), localStorage: {
        getItem(key) { if (settings.storageFails) throw new Error('storage denied'); return stored.get(key) || null; },
        setItem(key, value) { if (settings.storageFails) throw new Error('storage denied'); stored.set(key, value); },
      } },
    URLSearchParams, console, setTimeout, clearTimeout,
  });
  vm.runInContext(source, context);
  context.request = async (method, url, body) => {
    requests.push({ method, url, body });
    if (url === '/api/sync/pipeline/progress?dataset_id=market') {
      const generation = Object.hasOwn(settings, 'marketGeneration') ? settings.marketGeneration : 'market-1';
      return { generation: generation ? { generation } : null };
    }
    if (!settings.request) throw new Error('Unexpected request ' + url);
    return settings.request(method, url, body);
  };
  context.warn = (message) => warnings.push(message);
  context.draw = (canvas, bars) => drawings.push({ canvas, bars });
  vm.runInContext(`api = request; toast = warn;
    drawKline = (canvas, bars) => { _klineCanvas = canvas; _klineBars = bars; draw(canvas, bars); };
    bindKlineHover = bindKlineWheel = () => {};`, context);
  return { context, nodes, panelNodes, stored, requests, warnings, drawings,
    run: (code) => vm.runInContext(code, context),
    fixture: (result = screen()) => { context.fixture = result; vm.runInContext("state.result = fixture; state.screenMarketGeneration = state.activeMarketGeneration = 'market-1';", context); },
  };
}

/** @returns {object} */
function screen() {
  return { template_id: 'demo', template_revision: 1, trading_day: '2026-09-01', adjustment: 'qfq',
    summary: { total: 2, passed: 1, failed: 1 },
    results: [{ code: 'sh.600000', name: '浦发银行', trading_day: '2026-09-01', passed: true,
      rule_executions: [{ rule_id: 'pass', status: 'EXECUTED', result: { passed: true, reason: '通过', actual_value: 3, threshold: 2 } },
        { rule_id: 'skip', status: 'SKIPPED', result: null }] },
    { code: 'sh.600001', name: '测试', trading_day: '2026-09-01', passed: false,
      rule_executions: [{ rule_id: 'fail', status: 'EXECUTED', result: { passed: false, reason: '不足', actual_value: 1, threshold: 2 } }] }] };
}

/** @param {string} generation @returns {object} */
function options(generation = 'capm-1') {
  return { as_of: '2026-09-01', generation_id: generation,
    benchmarks: [{ index_id: 'hs300.price', name: '沪深300', provider_code: 'sh.000300', return_version: 'price' },
      { index_id: 'sector.price', name: '行业测试指数', provider_code: 'sh.000999', return_version: 'price' }],
    rate_terms: [{ term: '1_year', label: '整存整取一年期', annual_rate: '0.015', effective_on: '2015-10-24', source: 'Baostock' },
      { term: '3_month', label: '整存整取三个月', annual_rate: '0.011', effective_on: '2015-10-24', source: 'Baostock' }],
    defaults: { benchmark_id: 'hs300.price', rate_term: '1_year', periods_per_year: 252 } };
}

/** @param {string} id @returns {object} */
function analysis(id = 'analysis') {
  return { analysis_id: id, results: [{ window_days: 30, status: 'READY', estimate: {
    alpha_daily: '0.001', alpha_annualized: '0.252', beta: '1.2', r_squared: '0.8', observation_count: 21 } }] };
}

test('eight panels default expanded; buttons hide only their content and remember state', () => {
  const b = browser();
  b.run('initializePanelToggles()');
  for (const p of b.panelNodes) {
    const content = b.nodes.get('#' + p.toggle.getAttribute('aria-controls'));
    assert.equal(content.hidden, false);
    content.value = 'unsaved edits';
    p.toggle.listeners.click();
    assert.equal(content.hidden, true);
    assert.equal(p.toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(p.toggle.textContent, '展开');
    assert.equal(content.value, 'unsaved edits');
  }
  assert.equal(b.stored.size, 1);
  const restored = browser({ storage: Object.fromEntries(b.stored) });
  restored.run('initializePanelToggles()');
  assert.ok(restored.panelNodes.every((p) => p.toggle.getAttribute('aria-expanded') === 'false'));
});

test('storage failure is visible and never disables working panel controls', () => {
  const b = browser({ storageFails: true });
  b.run('initializePanelToggles()');
  b.panelNodes[0].toggle.listeners.click();
  assert.equal(b.panelNodes[0].toggle.getAttribute('aria-expanded'), 'false');
  assert.match(b.warnings.join(' '), /无法.*保存|无法.*读取/);
});

test('result list contains no nested detail; independent rules report passed, failed, skipped', () => {
  const b = browser(); b.fixture();
  b.run("state.selectedCode='sh.600000'; renderResults(); renderStockDetail();");
  assert.doesNotMatch(b.nodes.get('#result-body').innerHTML, /capm-panel|kline-canvas|rule-detail/);
  assert.match(b.nodes.get('#stock-detail-title').textContent, /浦发银行/);
  assert.match(b.nodes.get('#stock-rules').innerHTML, /通过/);
  assert.match(b.nodes.get('#stock-rules').innerHTML, /未参与/);
  assert.match(b.nodes.get('#stock-rules').innerHTML, /<details class="stock-rule-skipped"/);
  assert.ok(b.nodes.get('#stock-rules').innerHTML.indexOf('通过') < b.nodes.get('#stock-rules').innerHTML.indexOf('未参与'));
  b.run("state.selectedCode='sh.600001'; renderStockDetail();");
  assert.match(b.nodes.get('#stock-rules').innerHTML, /未通过/);
  assert.doesNotMatch(b.nodes.get('#stock-capm-results').innerHTML, /data-capm-code/);
});

test('selecting a passed stock expands independent detail, loads local options, automatically analyses', async () => {
  const b = browser({ request: async (_, url) => url.startsWith('/api/capm/options') ? options() :
    url.startsWith('/api/bars?') ? { bars: [] } : analysis() });
  b.fixture(); b.run('initializePanelToggles(); initializeStockDetail();');
  await b.run("selectStock('sh.600000')");
  assert.equal(b.nodes.get('#stock-detail-content').hidden, false);
  assert.equal(b.nodes.get('#stock-detail-panel').scroll.behavior, 'auto');
  assert.match(b.nodes.get('#stock-capm-results').innerHTML, /21|可估计/);
  assert.equal(b.requests.filter((r) => r.url === '/api/capm/analyses').length, 1);
  assert.ok(b.requests.every((r) => r.method === 'GET' || r.url === '/api/capm/analyses'));
});

test('parameters are forwarded, persisted across templates, and CAPM redraw preserves chart zoom', async () => {
  const b = browser({ request: async (_, url) => url.startsWith('/api/capm/options') ? options() :
    url.startsWith('/api/bars?') ? { bars: [] } : analysis() });
  b.fixture(); b.run('initializeStockDetail();');
  await b.run("selectStock('sh.600000')");
  const drawCount = b.drawings.length;
  b.run('_klineView = {start: 7, end: 20};');
  b.nodes.get('#capm-benchmark').value = 'sector.price';
  b.nodes.get('#capm-rate-term').value = '3_month';
  b.nodes.get('#capm-periods-per-year').value = '250';
  await b.run('changeCapmSettings()');
  const call = b.requests.filter((r) => r.url === '/api/capm/analyses').at(-1);
  assert.equal(call.body.benchmark_id, 'sector.price');
  assert.equal(call.body.rate_term, '3_month');
  assert.equal(call.body.periods_per_year, 250);
  assert.equal(b.drawings.length, drawCount);
  assert.equal(b.run('_klineView.start'), 7);
  assert.match(b.nodes.get('#stock-capm-results').innerHTML, /行业测试指数/);
  assert.match(b.nodes.get('#capm-rate-info').textContent, /1.100%|1.10%/);
  const c = browser({ storage: Object.fromEntries(b.stored), request: async () => options() });
  c.fixture(); c.run('initializeStockDetail();'); await c.run('loadCapmOptions()');
  assert.equal(c.nodes.get('#capm-benchmark').value, 'sector.price');
  assert.equal(c.nodes.get('#capm-periods-per-year').value, '250');
});

test('stale stock and changed-parameter CAPM responses cannot replace current detail', async () => {
  const pending = [];
  const b = browser({ request: async (_, url) => {
    if (url.startsWith('/api/capm/options')) return options();
    if (url.startsWith('/api/bars?')) return { bars: [] };
    const d = deferred(); pending.push(d); return d.promise;
  } });
  b.fixture(); b.run('initializeStockDetail();');
  const old = b.run("selectStock('sh.600000')");
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  b.nodes.get('#capm-periods-per-year').value = '250';
  const newer = b.run('changeCapmSettings()');
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  assert.equal(pending.length, 2);
  pending[1].resolve(analysis('new')); await newer;
  const markup = b.nodes.get('#stock-capm-results').innerHTML;
  pending[0].resolve(analysis('old')); await old;
  assert.equal(b.nodes.get('#stock-capm-results').innerHTML, markup);
  const again = b.run("runCapmAnalysis('sh.600000', true)");
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  await b.run("selectStock('sh.600001')");
  pending[2].reject(new Error('old stock failure')); await again;
  assert.doesNotMatch(b.nodes.get('#stock-capm-results').innerHTML, /old stock failure/);
  assert.match(b.nodes.get('#stock-detail-title').textContent, /测试/);
  const returned = b.run("selectStock('sh.600000')");
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  assert.equal(pending.length, 4, 'returning to a stock must not remain stuck on an abandoned loading cache');
  pending[3].resolve(analysis('returned')); await returned;
  assert.doesNotMatch(b.nodes.get('#stock-capm-results').innerHTML, /计算中/);
});

test('new screen invalidates in-flight requests and changed CAPM generation invalidates cached result', async () => {
  let currentOptions = options();
  const slow = deferred(); let analyses = 0;
  const b = browser({ request: async (_, url) => {
    if (url.startsWith('/api/capm/options')) return currentOptions;
    if (url.startsWith('/api/bars?')) return { bars: [] };
    analyses += 1; return analyses === 1 ? slow.promise : analysis('new-generation');
  } });
  b.fixture(); b.run('initializeStockDetail();');
  const old = b.run("selectStock('sh.600000')");
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  b.run('invalidateStockAnalysis();');
  slow.resolve(analysis('stale')); await old;
  assert.equal(b.run('state.capmByCode.size'), 0);
  await b.run("selectStock('sh.600000')");
  assert.equal(analyses, 2);
  currentOptions = options('capm-2');
  await b.run("refreshCapmGeneration('capm-2')");
  assert.equal(analyses, 3);
});

test('empty/unavailable options and failures never invent a selectable baseline or a rate', async () => {
  let response = { ...options(), generation_id: null, benchmarks: [], rate_terms: [] };
  const b = browser({ request: async () => response });
  b.fixture(); b.run('initializeStockDetail();'); await b.run('loadCapmOptions()');
  assert.equal(b.nodes.get('#capm-benchmark').disabled, true);
  assert.match(b.nodes.get('#capm-options-status').textContent, /同步|发布/);
  assert.equal(b.requests.filter((r) => r.url === '/api/capm/analyses').length, 0);
  response = options(); await b.run('loadCapmOptions(true)');
  b.nodes.get('#capm-rate-term').value = 'invented';
  await b.run('changeCapmSettings()');
  assert.match(b.nodes.get('#capm-options-status').textContent, /利率|期限/);
  assert.equal(b.requests.filter((r) => r.url === '/api/capm/analyses').length, 0);
});

test('options errors remain actionable; unavailable defaults and future-only rates cannot silently fall back', async () => {
  let fail = true;
  const noDefault = { ...options(), benchmarks: [options().benchmarks[1]],
    rate_terms: [{ ...options().rate_terms[0], annual_rate: null, effective_on: null }] };
  const b = browser({ request: async () => { if (fail) throw new Error('local offline'); return noDefault; } });
  b.fixture(); b.run('initializeStockDetail();'); await b.run('loadCapmOptions()');
  assert.match(b.nodes.get('#capm-options-status').textContent, /local offline/);
  assert.equal(b.nodes.get('#capm-options-retry').hidden, false);
  fail = false; await b.run('loadCapmOptions(true)');
  assert.equal(b.nodes.get('#capm-benchmark').value, '');
  assert.equal(b.nodes.get('#capm-rate-term').value, '');
  assert.equal(b.nodes.get('#capm-rate-term').disabled, true);
  assert.equal(b.nodes.get('#capm-rate-term').children[1].disabled, true);
  assert.equal(b.run('state.capmSettings.benchmark_id'), 'hs300.price');
  assert.match(b.nodes.get('#capm-options-status').textContent, /不会自动替换/);
});

test('real screen response has no generation: stock publication invalidates all detail until a new screen succeeds', async () => {
  const configuration = { marketGeneration: 'market-1', request: async (_, url) =>
    url.startsWith('/api/capm/options') ? options() : url.startsWith('/api/bars?') ? { bars: [{ close: '1' }] } : analysis() };
  const b = browser(configuration); b.fixture();
  assert.equal(Object.hasOwn(screen(), 'generation'), false);
  b.run('initializeStockDetail();'); await b.run("selectStock('sh.600000')");
  configuration.marketGeneration = 'market-2';
  b.run("observeMarketGeneration('market-2')");
  assert.equal(b.run('state.screenGenerationStale'), true);
  assert.equal(b.run('state.capmByCode.size'), 0);
  assert.equal(b.run('_klineBars'), null);
  assert.match(b.nodes.get('#stock-detail-subtitle').textContent, /重新筛选/);
  const count = b.requests.length;
  await b.run("selectStock('sh.600000'); runCapmAnalysis('sh.600000', true);");
  assert.equal(b.requests.length, count);
  b.run('invalidateStockAnalysis();');
  assert.equal(b.run('state.screenGenerationStale'), true, 'starting a screen is not successful revalidation');
  b.run("acceptScreenResult(fixture, 'market-2');");
  assert.equal(b.run('state.screenGenerationStale'), false);
  await b.run("selectStock('sh.600000')");
  assert.match(b.run("capmCacheKey('sh.600000')"), /market-2/);
});

test('stock version recheck on selection catches a publication even before the next poll', async () => {
  const configuration = { marketGeneration: 'market-2' };
  const b = browser(configuration); b.fixture(); b.run('initializeStockDetail();');
  await b.run("selectStock('sh.600000')");
  assert.equal(b.run('state.screenGenerationStale'), true);
  assert.ok(b.requests.every((request) => request.url === '/api/sync/pipeline/progress?dataset_id=market'));
  const unavailable = browser({ marketGeneration: null }); unavailable.fixture();
  assert.equal(await unavailable.run('readMarketGeneration()'), null);
  assert.equal(unavailable.run('state.screenGenerationStale'), true);
});

test('screen captures actual status generation before and after; failed rerun cannot restore stale detail', async () => {
  let fail = true;
  const configuration = { marketGeneration: 'market-2', request: async (_, url) => {
    assert.equal(url, '/api/screen');
    if (fail) throw new Error('screen failed');
    return screen();
  } };
  const b = browser(configuration); b.fixture();
  b.run(`initializeStockDetail(); observeMarketGeneration('market-2');
    validateComposition = () => null;
    runtimeConditions = () => ({dataset_id: 'market', trading_day: '2026-09-01', adjustment: 'qfq'});
    buildPayload = () => ({}); startScreenPolling = stopScreenPolling = () => {};`);
  await b.run('runScreen()');
  assert.equal(b.run('state.screenGenerationStale'), true);
  assert.equal(b.run('state.screenPending'), false);
  assert.equal(b.run('state.result'), null);
  fail = false; await b.run('runScreen()');
  assert.equal(b.run('state.screenMarketGeneration'), 'market-2');
  assert.equal(b.run('state.screenGenerationStale'), false);
  const calls = b.requests.slice(-3);
  assert.deepEqual(calls.map((item) => item.url), [
    '/api/sync/pipeline/progress?dataset_id=market', '/api/screen', '/api/sync/pipeline/progress?dataset_id=market']);
});

test('partial benchmark remains selectable with a coverage warning; invalid factor never posts analysis', async () => {
  const data = options(); data.benchmarks[0].coverage_status = 'PARTIAL';
  const b = browser({ request: async () => data }); b.fixture(); b.run('initializeStockDetail();');
  await b.run('loadCapmOptions()');
  assert.match(b.nodes.get('#capm-benchmark').children[1].textContent, /历史不完整/);
  assert.equal(b.nodes.get('#capm-benchmark').children[1].disabled, false);
  assert.match(b.nodes.get('#capm-options-status').textContent, /所选指数历史不完整/);
  b.nodes.get('#capm-periods-per-year').value = '1.5';
  await b.run('changeCapmSettings()');
  assert.match(b.nodes.get('#capm-options-status').textContent, /正整数/);
  assert.ok(b.requests.every((item) => item.method === 'GET'));
});

test('late options and late K-line replies are ignored after stock/screen selection changes', async () => {
  const bars = deferred(), oldOptions = deferred(); let optCalls = 0;
  const b = browser({ request: async (_, url) => {
    if (url.startsWith('/api/bars?')) return url.includes('600000') ? bars.promise : { bars: [{ close: '2' }] };
    if (url.startsWith('/api/capm/options')) return ++optCalls === 1 ? oldOptions.promise : options('capm-new');
    return analysis();
  } });
  b.fixture(); b.run('initializeStockDetail();');
  const first = b.run("selectStock('sh.600000')");
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
  b.run('invalidateStockAnalysis();');
  await b.run("selectStock('sh.600001')");
  await b.run('loadCapmOptions()');
  const title = b.nodes.get('#stock-detail-title').textContent;
  oldOptions.resolve(options('capm-old')); bars.resolve({ bars: [{ close: '1' }] }); await first;
  assert.equal(b.run('state.capmOptions.generation_id'), 'capm-new');
  assert.equal(b.nodes.get('#stock-detail-title').textContent, title);
  assert.equal(b.run('_klineBars[0].close'), '2');
});
