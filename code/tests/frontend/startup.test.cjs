// Offline browser orchestration tests, using only Node's built-in test runner.
//
// 启动不再阻塞在「检查本地数据」:首屏固定进入数据/同步页(gate),
// 规则/模板/研究策略与数据状态全部在后台异步获取,失败只提示、不清空界面。
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
  // 反映 HTML 中的初始文案(测试断言依赖)。
  nodes.get('#gate-title').textContent = '数据状态：正在检查本地数据…';
  nodes.get('#server-status').textContent = '连接中…';

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
    renderSyncStatus = (s) => { __lastSyncStatus = s; };
    startSyncPolling = () => {};
    loadInstances = loadVersion = () => {};
    toast = () => {};
  `, context);
  return {
    context, nodes, calls, intervals,
    init: () => vm.runInContext('init()', context),
    run: (expr) => vm.runInContext(expr, context),
    get reloads() { return reloads; },
  };
}

/** 排空微任务队列,让链式 async/await(含已 resolve 的请求)依次落定。 */
async function drain(n = 40) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

/** @returns {object} */
function fixtures() {
  return {
    '/api/rules': { rules: [] }, '/api/templates': { templates: [] },
    '/api/research/policies': { policies: {} },
    '/api/sync/status': { readiness: { status: 'READY' } },
  };
}

test('首屏即数据/同步页:已移除加载遮罩,工作台与回测视图初始隐藏', () => {
  const b = browser();
  assert.equal(b.nodes.has('#startup-view'), false);
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.equal(b.nodes.get('#backtest-view').hidden, true);
  assert.match(html, /正在检查本地数据/);
  assert.doesNotMatch(html, /id="startup-view"/);
});

test('进入数据页后立即后台并行拉取规则/模板/策略/状态;状态慢不阻塞首屏,READY 也不自动跳转', async () => {
  const data = fixtures();
  const status = deferred();
  data['/api/sync/status'] = status.promise;
  const b = browser(data);
  b.init();
  await drain();
  // 数据状态尚未返回时,界面已停在数据页,其余三个请求全部发出。
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.deepEqual(b.calls.map(([, method]) => method), ['GET', 'GET', 'GET', 'GET']);
  assert.deepEqual(
    b.calls.map(([url]) => url).sort(),
    ['/api/research/policies', '/api/rules', '/api/sync/status', '/api/templates'],
  );
  // 状态返回 READY 也只是填充状态,不把用户从数据页拽走。
  status.resolve({ readiness: { status: 'READY' }, active_generation: null, can_enter: false });
  await drain();
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.equal(b.context.__lastSyncStatus.readiness.status, 'READY');
  assert.equal(b.nodes.get('#server-status').textContent, '服务正常');
});

test('数据状态失败不阻塞界面:停留数据页,服务徽标置为部分失败,无整页重试', async () => {
  const data = fixtures();
  const status = deferred();
  const rules = deferred(); // 保持未决:让状态失败先于资源成功落定,徽标结果确定。
  data['/api/sync/status'] = status.promise;
  data['/api/rules'] = rules.promise;
  const b = browser(data);
  b.init();
  await drain();
  status.reject(new Error('本地数据检查失败'));
  await drain();
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.equal(b.nodes.get('#server-status').textContent, '部分加载失败');
  assert.match(b.nodes.get('#gate-title').textContent, /数据状态获取失败/);
  assert.match(b.nodes.get('#gate-readiness').textContent, /本地数据检查失败/);
  assert.equal(b.reloads, 0);
});

test('规则/模板等工作台资源失败只提示,数据页仍可用', async () => {
  const data = fixtures();
  const rules = deferred();
  const status = deferred();
  data['/api/rules'] = rules.promise;
  data['/api/sync/status'] = status.promise;
  const b = browser(data);
  b.init();
  await drain();
  // 先让资源失败、再让状态成功:徽标保持“部分加载失败”,界面不清空。
  rules.reject(new Error('服务暂时不可用'));
  await drain();
  assert.equal(b.nodes.get('#server-status').textContent, '部分加载失败');
  status.resolve({ readiness: { status: 'READY' }, active_generation: null, can_enter: false });
  await drain();
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  assert.equal(b.nodes.get('#server-status').textContent, '部分加载失败');
  assert.equal(b.reloads, 0);
});

test('后台预取完成(徽标转正常)后可手动进入筛选工作台', async () => {
  const b = browser(fixtures());
  b.init();
  await drain();
  // 后台资源(规则/模板/策略)与状态都已成功落地。
  assert.equal(b.nodes.get('#server-status').textContent, '服务正常');
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  b.run('setView("workbench")');
  assert.equal(b.nodes.get('#workbench-view').hidden, false);
  assert.equal(b.nodes.get('#gate-view').hidden, true);
});

test('资源失败后再试成功,仍可进入工作台;失败期间停留在数据页', async () => {
  const data = fixtures();
  const rules = deferred();
  data['/api/rules'] = rules.promise;
  const b = browser(data);
  b.init();
  await drain();
  // 规则尚未返回:即使用户触发进入,也需等资源就绪,工作台保持隐藏。
  b.run('(async () => { await prepareWorkspaceResources(); if (_workspaceResourcesReady) setView("workbench"); })()');
  await drain();
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  // 首次失败:仍停在数据页,徽标部分失败;允许再次预取。
  rules.reject(new Error('服务暂时不可用'));
  await drain();
  assert.equal(b.nodes.get('#gate-view').hidden, false);
  b.run('(async () => { await prepareWorkspaceResources(); if (_workspaceResourcesReady) setView("workbench"); })()');
  await drain();
  assert.equal(b.nodes.get('#workbench-view').hidden, true);
  // 资源恢复后,再次触发进入即可切换。
  data['/api/rules'] = { rules: [] };
  b.run('(async () => { await prepareWorkspaceResources(); if (_workspaceResourcesReady) setView("workbench"); })()');
  await drain();
  assert.equal(b.nodes.get('#server-status').textContent, '服务正常');
  assert.equal(b.nodes.get('#workbench-view').hidden, false);
});
