// Offline browser orchestration tests, using only Node's built-in test runner.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const staticRoot = path.join(__dirname, '../../src/stock_manager/web/static');
const html = fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(staticRoot, 'app.js'), 'utf8');

/** @returns {{promise: Promise<unknown>, resolve: Function, reject: Function}} */
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/** @param {Object<string, unknown>} responses @returns {object} */
function browser(responses = {}) {
  const nodes = new Map();
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    nodes.set('#' + match[1], {
      hidden: /\bhidden\b/.test(match[0]), textContent: '', dataset: {}, value: 0,
      style: {}, attributes: {}, listeners: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, listener) { this.listeners[name] = listener; },
    });
  }
  const intervals = new Map();
  const calls = [];
  let now = 0, reloads = 0, sequence = 0;
  const context = vm.createContext({
    document: {
      querySelector: (selector) => {
        assert.ok(nodes.has(selector), 'Missing element: ' + selector);
        return nodes.get(selector);
      },
      addEventListener() {},
    },
    window: { addEventListener() {}, location: { reload() { reloads += 1; } } },
    performance: { now: () => now }, Date, console,
    setInterval(fn) { intervals.set(++sequence, fn); return sequence; },
    clearInterval(id) { intervals.delete(id); },
    fetch: async (url, options) => {
      calls.push([url, options.method]);
      assert.ok(Object.hasOwn(responses, url), 'Unexpected request: ' + url);
      const data = await responses[url];
      return { status: 200, ok: true, text: async () => JSON.stringify(data) };
    },
  });
  vm.runInContext(source, context);
  // Keep initialization/request handling real; isolate unrelated editor widgets.
  vm.runInContext(`
    bindEvents = () => {};
    renderTemplateSelect = renderMeta = renderRules = renderGroups = syncComposeSegments = () => {};
    renderStrategyPolicies = () => {};
    renderSyncStatus = (s) => {
      $('#gate-view').hidden = s.readiness.status === 'READY';
      $('#workbench-view').hidden = s.readiness.status !== 'READY';
    };
    startSyncPolling = () => {};
    loadInstances = loadVersion = () => {};
    toast = () => {};
  `, context);
  return {
    context, nodes, calls, intervals,
    init: () => vm.runInContext('init()', context),
    progress: () => vm.runInContext('createStartupProgress()', context),
    tick(ms) { now = ms; for (const fn of intervals.values()) fn(); },
    get reloads() { return reloads; },
  };
}

/** @returns {object} */
function fixtures() {
  return {
    '/api/rules': { rules: [] }, '/api/templates': { templates: [] },
    '/api/research/policies': { policies: {} },
    '/api/sync/status': { readiness: { status: 'READY' } },
  };
}

test('loading card is visible before JavaScript, both application views are hidden', () => {
  const b = browser();
  assert.equal(b.nodes.get('#startup-view').hidden, false);
  assert.equal(b.nodes.get('#gate-view').hidden, true);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.match(html, /<progress[^>]+max="5"[^>]+value="0"/);
});

test('out-of-order completions count real work; 10/60 second boundaries never retry automatically', async () => {
  const b = browser();
  const progress = b.progress();
  const slow = deferred();
  const waiting = progress.run(3, () => slow.promise);
  await progress.run(1, async () => ({}));
  await progress.run(0, async () => ({}));
  assert.equal(b.nodes.get('#startup-progress').value, 2);
  b.tick(9999);
  assert.equal(b.nodes.get('#startup-hint').hidden, true);
  b.tick(10000);
  assert.match(b.nodes.get('#startup-hint').textContent, /检查本地数据/);
  b.tick(59999);
  assert.equal(b.nodes.get('#startup-retry').hidden, true);
  b.tick(60000);
  assert.equal(b.nodes.get('#startup-retry').hidden, false);
  assert.match(b.nodes.get('#startup-elapsed').textContent, /60/);
  assert.equal(b.reloads, 0);
  b.nodes.get('#startup-retry').listeners.click();
  assert.equal(b.reloads, 1);
  slow.resolve({});
  await waiting;
  assert.equal(b.nodes.get('#startup-progress').value, 3);
  progress.finish();
  assert.equal(b.intervals.size, 0);
});

test('failure is persistent, stops the clock, and late responses cannot replace it', async () => {
  const b = browser();
  const progress = b.progress();
  const slow = deferred();
  const waiting = progress.run(3, () => slow.promise);
  await assert.rejects(progress.run(0, async () => { throw new Error('<服务离线>'); }), /服务离线/);
  assert.match(b.nodes.get('#startup-message').textContent, /加载规则.*<服务离线>/);
  const message = b.nodes.get('#startup-message').textContent;
  assert.equal(b.nodes.get('#startup-retry').hidden, false);
  assert.equal(b.intervals.size, 0);
  assert.equal(b.nodes.get('#startup-step-3').dataset.status, 'stopped');
  slow.resolve({});
  await waiting;
  b.tick(60000);
  assert.equal(b.nodes.get('#startup-message').textContent, message);
  assert.equal(b.nodes.get('#startup-progress').value, 0);
  assert.equal(b.nodes.get('#startup-view').hidden, false);
});

for (const readiness of ['READY', 'INCOMPLETE']) {
  test('empty template catalog completes and preserves data readiness: ' + readiness, async () => {
    const data = fixtures();
    data['/api/sync/status'].readiness.status = readiness;
    const b = browser(data);
    await b.init();
    assert.equal(b.nodes.get('#startup-view').hidden, true);
    assert.equal(b.nodes.get('#startup-progress').value, 5);
    assert.equal(b.nodes.get('#gate-view').hidden, readiness === 'READY');
    assert.equal(b.nodes.get('#workbench-view').hidden, readiness !== 'READY');
    assert.equal(b.intervals.size, 0);
    assert.equal(b.calls.length, 4);
    assert.ok(b.calls.every(([, method]) => method === 'GET'));
  });
}

test('local data check failure must not leave a blank main view or claim service success', async () => {
  const data = fixtures();
  const sync = deferred();
  data['/api/sync/status'] = sync.promise;
  const b = browser(data);
  const init = b.init();
  sync.reject(new Error('本地数据检查失败'));
  await init;
  assert.equal(b.nodes.get('#startup-view').hidden, false);
  assert.match(b.nodes.get('#startup-message').textContent, /检查本地数据.*本地数据检查失败/);
  assert.equal(b.nodes.get('#server-status').textContent, '加载失败');
  assert.equal(b.nodes.get('#startup-retry').hidden, false);
  assert.equal(b.nodes.get('#gate-view').hidden, true);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
});

test('default template detail remains a required step and reports a failure', async () => {
  const data = fixtures();
  data['/api/templates'].templates = [
    { template_id: 'user' }, { template_id: 'system', is_system: true },
  ];
  const detail = deferred();
  data['/api/templates/system'] = detail.promise;
  const b = browser(data);
  const init = b.init();
  // Drain async request/JSON parsing without wall-clock sleeps.
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
  assert.equal(b.nodes.get('#startup-progress').value, 4);
  assert.equal(b.nodes.get('#startup-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  detail.reject(new Error('模板已被删除'));
  await init;
  assert.match(b.nodes.get('#startup-message').textContent, /准备工作台.*模板已被删除/);
  assert.equal(b.nodes.get('#startup-retry').hidden, false);
});

for (const [endpoint, label] of [
  ['/api/rules', '加载规则'], ['/api/templates', '读取模板'], ['/api/research/policies', '加载策略'],
]) {
  test('required catalog errors remain visible: ' + endpoint, async () => {
    const data = fixtures();
    const request = deferred();
    data[endpoint] = request.promise;
    const b = browser(data);
    const init = b.init();
    request.reject(new Error('服务暂时不可用'));
    await init;
    assert.ok(b.nodes.get('#startup-message').textContent.includes(label + '失败'));
    assert.equal(b.nodes.get('#startup-view').hidden, false);
    assert.equal(b.nodes.get('#startup-retry').hidden, false);
    assert.equal(b.intervals.size, 0);
  });
}

test('rendering failure hides partially prepared workspace and never reaches 5/5', async () => {
  const b = browser(fixtures());
  vm.runInContext(`renderSyncStatus = () => {
    $('#workbench-view').hidden = false;
    throw new Error('页面准备失败');
  };`, b.context);
  await b.init();
  assert.equal(b.nodes.get('#startup-progress').value, 4);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.match(b.nodes.get('#startup-message').textContent, /准备工作台失败.*页面准备失败/);
});
