'use strict';

const $ = (sel) => document.querySelector(sel);

const state = {
  rules: [],
  templates: [],
  currentId: null,
  template: null,      // {template, is_system}
  composition: null,   // {operator, groups:[{group_id,operator,rule_ids}]}
  dirty: false,
  revision: 1,
  isSystem: false,
  result: null,
  resultFilter: 'all',
  selectedCode: null,
  capmByCode: new Map(),
  screenEpoch: 0,
  screenPending: false,
  activeMarketGeneration: null,
  screenMarketGeneration: null,
  screenGenerationStale: false,
  capmOptions: null,
  capmOptionsLoading: false,
  capmOptionsError: '',
  capmSettings: { benchmark_id: null, rate_term: null, periods_per_year: 252 },
  uiView: 'auto',   // 'auto' | 'gate' | 'workbench' — 手动停留的数据页/工作台视图
};

let _klineBars = null;      // last full dataset drawn (for hover / zoom / resize)
let _klineCanvas = null;    // last active canvas
let _klineInfoDefault = ''; // default info text (restored on mouse leave)
let _klineView = null;      // visible window {start, end} into _klineBars
let _klineDrag = null;      // {startX, viewStart} while panning
const _klineCache = new Map(); // code+adjustment+end -> bars
let _klineRequest = 0;
let _detailIdentity = null;
let _capmOptionsRequest = 0;
let _capmOptionsPromise = null;
let _capmRequest = 0;
let _stockSelectionRequest = 0;
let _panelPreferences = {};
let _preferenceWarning = '';
const PANEL_PREFERENCES_KEY = 'stockmanager.panels.v1';
const CAPM_PREFERENCES_KEY = 'stockmanager.capm.preferences.v1';

/* ---------- small helpers ---------- */
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function termHelp(tip) {
  const el = document.createElement('span');
  el.className = 'term__help';
  el.dataset.tip = tip;
  el.textContent = '?';
  return el;
}
function toggleId(ruleId) { return 'toggle-' + ruleId; }
function paramId(ruleId, pid) { return 'param-' + ruleId + '-' + pid; }
function enabledRuleIds() {
  return state.rules.filter((r) => currentEnabled(r.rule_id)).map((r) => r.rule_id);
}
function currentEnabled(ruleId) {
  const el = document.getElementById(toggleId(ruleId));
  return el ? el.getAttribute('aria-checked') === 'true' : false;
}

function toast(message, kind) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast' + (kind ? ' toast--' + kind : '');
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 3200);
}

/* ---------- API ---------- */
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch (e) { data = { error: { message: text } }; } }
  if (!res.ok) {
    const msg = (data && data.error && data.error.message) || ('请求失败 (' + res.status + ')');
    throw new Error(msg);
  }
  return data;
}

/* ---------- template payload from editor ---------- */
function readParam(ruleId, p) {
  const el = document.getElementById(paramId(ruleId, p.parameter_id));
  if (!el) return p.default_value;
  const raw = el.value;
  switch (p.value_type) {
    case 'integer': return raw === '' ? null : Number(raw);
    case 'decimal': return raw === '' ? null : String(raw);
    case 'boolean': return el.checked;
    case 'text': return raw;
    default: return raw;
  }
}

function buildPayload() {
  const rules = {};
  const enabled = new Set();
  for (const rule of state.rules) {
    const on = currentEnabled(rule.rule_id);
    if (on) enabled.add(rule.rule_id);
    const parameters = {};
    for (const p of rule.parameters) {
      parameters[p.parameter_id] = readParam(rule.rule_id, p);
    }
    rules[rule.rule_id] = { enabled: on, parameters };
  }
  const groups = state.composition.groups
    .map((g) => ({ group_id: g.group_id, operator: g.operator, rules: g.rule_ids.filter((id) => enabled.has(id)) }))
    .filter((g) => g.rules.length > 0);
  const m = state.template.template.metadata;
  return {
    metadata: {
      schema_version: 2,
      template_id: state.currentId,
      revision: state.revision,
      name: m.name,
      description: m.description,
      timezone: m.timezone,
      technical_adjustment: m.technical_adjustment,
    },
    rules,
    composition: { operator: state.composition.operator, groups },
  };
}

function validateComposition() {
  const enabled = enabledRuleIds();
  if (enabled.length === 0) return '请至少启用一条规则。';
  const assigned = new Set();
  for (const g of state.composition.groups) {
    for (const id of g.rule_ids) { if (enabled.includes(id)) assigned.add(id); }
  }
  const unassigned = enabled.filter((id) => !assigned.has(id));
  if (unassigned.length) return '以下已启用规则未分配到任何分组：' + unassigned.join('、');
  if (state.composition.groups.filter((g) => g.rule_ids.some((id) => enabled.includes(id))).length === 0) {
    return '请至少保留一个有效组合分组。';
  }
  return null;
}

/* ---------- rendering ---------- */
function renderTemplateSelect() {
  const sel = $('#template-select');
  sel.innerHTML = '<option value="">选择模板…</option>';
  for (const t of state.templates) {
    const opt = document.createElement('option');
    opt.value = t.template_id;
    opt.textContent = t.name + (t.is_system ? '（系统）' : '') + (t.revision > 1 ? ' · rev' + t.revision : '');
    opt.selected = t.template_id === state.currentId;
    sel.appendChild(opt);
  }
}

function renderMeta() {
  const m = state.template.template.metadata;
  const tags = [];
  tags.push('<span class="badge badge--' + (state.isSystem ? 'ok' : 'pass') + '">' + (state.isSystem ? '系统模板' : '用户模板') + '</span>');
  tags.push('<span class="badge badge--muted">rev ' + state.revision + '</span>');
  tags.push('<span class="badge badge--muted">' + esc(m.technical_adjustment) + '</span>');
  $('#template-meta').innerHTML =
    '<p class="template-meta__name">' + esc(m.name) + '</p>' +
    '<p class="template-meta__desc">' + esc(m.description) + '</p>' +
    '<div class="template-meta__tags">' + tags.join('') + '</div>';
  $('#save-template').disabled = state.isSystem;
  $('#delete-template').disabled = state.isSystem;
}

function renderRules() {
  const container = $('#editor-rules');
  container.innerHTML = '';
  for (const rule of state.rules) {
    const entry = state.template.template.rules[rule.rule_id] || { enabled: false, parameters: {} };
    const card = document.createElement('div');
    card.className = 'rule-card' + (entry.enabled ? '' : ' rule-card--off');
    card.dataset.rule = rule.rule_id;

    const head = document.createElement('div');
    head.className = 'rule-card__head';
    const toggle = document.createElement('button');
    toggle.className = 'toggle';
    toggle.id = toggleId(rule.rule_id);
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', entry.enabled ? 'true' : 'false');
    toggle.type = 'button';
    toggle.title = '启用/禁用规则';
    const title = document.createElement('div');
    title.className = 'rule-card__title';
    const name = document.createElement('span');
    name.className = 'rule-card__name';
    name.textContent = rule.name;
    name.appendChild(termHelp(rule.description || rule.rule_id));
    const desc = document.createElement('span');
    desc.className = 'rule-card__desc';
    desc.textContent = rule.rule_id + ' · ' + rule.description;
    title.appendChild(name);
    title.appendChild(desc);
    head.appendChild(toggle);
    head.appendChild(title);
    card.appendChild(head);

    if (rule.parameters.length) {
      const grid = document.createElement('div');
      grid.className = 'rule-card__params';
      for (const p of rule.parameters) {
        grid.appendChild(buildParamField(rule, p, entry.parameters[p.parameter_id]));
      }
      card.appendChild(grid);
    }
    container.appendChild(card);
  }
}

function buildParamField(rule, p, value) {
  const wrap = document.createElement('label');
  wrap.className = 'field';
  const label = document.createElement('span');
  label.className = 'field__label';
  label.textContent = p.label + ' (必填)';
  label.appendChild(termHelp(p.description || p.label));
  wrap.appendChild(label);

  const current = value === undefined || value === null ? p.default_value : value;
  if (p.value_type === 'boolean') {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!current;
    input.id = paramId(rule.rule_id, p.parameter_id);
    input.style.width = 'auto';
    wrap.appendChild(input);
    const hint = document.createElement('span');
    hint.className = 'field__label';
    hint.textContent = '是（勾选表示启用）';
    wrap.appendChild(hint);
  } else {
    const input = document.createElement('input');
    input.className = 'input';
    if (p.value_type === 'integer') { input.type = 'number'; input.step = '1'; }
    else if (p.value_type === 'decimal') { input.type = 'number'; input.step = 'any'; }
    else { input.type = 'text'; }
    input.value = current == null ? '' : String(current);
    input.id = paramId(rule.rule_id, p.parameter_id);
    input.dataset.pid = p.parameter_id;
    input.addEventListener('input', markDirty);
    wrap.appendChild(input);
  }
  return wrap;
}

function renderGroups() {
  const container = $('#groups');
  container.innerHTML = '';
  const enabled = enabledRuleIds();

  state.composition.groups.forEach((group, index) => {
    const el = document.createElement('div');
    el.className = 'group';
    el.dataset.index = String(index);

    const head = document.createElement('div');
    head.className = 'group__head';
    const name = document.createElement('input');
    name.className = 'input group__name-input';
    name.value = group.group_id;
    name.dataset.field = 'group_id';
    name.style.width = '120px';
    name.addEventListener('input', (e) => { group.group_id = e.target.value.trim() || group.group_id; markDirty(); });
    const seg = document.createElement('div');
    seg.className = 'segmented';
    seg.setAttribute('role', 'group');
    for (const op of ['all', 'any']) {
      const b = document.createElement('button');
      b.className = 'segment' + (group.operator === op ? ' is-active' : '');
      b.textContent = op === 'all' ? '全部' : '任一';
      b.appendChild(termHelp(op === 'all' ? '组内所有规则都通过才算通过。' : '组内任一规则通过即算通过。'));
      b.dataset.groupOp = op;
      b.type = 'button';
      b.addEventListener('click', () => { group.operator = op; renderGroups(); markDirty(); });
      seg.appendChild(b);
    }
    const del = document.createElement('button');
    del.className = 'btn btn--danger btn--sm';
    del.textContent = '移除分组';
    del.type = 'button';
    del.addEventListener('click', () => { state.composition.groups.splice(index, 1); renderGroups(); markDirty(); });
    head.appendChild(name);
    head.appendChild(seg);
    head.appendChild(del);
    el.appendChild(head);

    const rulesWrap = document.createElement('div');
    rulesWrap.className = 'group__rules';
    for (const rule of state.rules.filter((r) => enabled.includes(r.rule_id))) {
      const active = group.rule_ids.includes(rule.rule_id);
      const chip = document.createElement('button');
      chip.className = 'rule-chip' + (active ? ' is-active' : '');
      chip.type = 'button';
      chip.textContent = rule.name;
      chip.dataset.ruleId = rule.rule_id;
      chip.addEventListener('click', () => toggleGroupMembership(group, rule.rule_id));
      rulesWrap.appendChild(chip);
    }
    el.appendChild(rulesWrap);
    container.appendChild(el);
  });
}

function toggleGroupMembership(group, ruleId) {
  const idx = group.rule_ids.indexOf(ruleId);
  if (idx >= 0) {
    group.rule_ids.splice(idx, 1);
  } else {
    for (const g of state.composition.groups) {
      g.rule_ids = g.rule_ids.filter((id) => id !== ruleId);
    }
    group.rule_ids.push(ruleId);
  }
  renderGroups();
  markDirty();
}

function setComposeOperator(op) {
  state.composition.operator = op;
  document.querySelectorAll('[data-compose]').forEach((b) => b.classList.toggle('is-active', b.dataset.compose === op));
  markDirty();
}

function syncComposeSegments() {
  document.querySelectorAll('[data-compose]').forEach((b) => b.classList.toggle('is-active', b.dataset.compose === state.composition.operator));
}

/* ---------- dirty ---------- */
function markDirty() {
  state.dirty = true;
  $('#dirty-badge').hidden = false;
}

function clearDirty() {
  state.dirty = false;
  $('#dirty-badge').hidden = true;
}

/* ---------- template lifecycle ---------- */
/**
 * @param {string} id
 * @param {{propagateError?: boolean}} options
 * @returns {Promise<void>}
 */
async function loadTemplate(id, { propagateError = false } = {}) {
  if (state.currentId && state.dirty && state.currentId !== id) {
    if (!confirm('当前模板有未保存的改动，切换将丢失这些改动。继续？')) return;
  }
  try {
    const data = await api('GET', '/api/templates/' + encodeURIComponent(id));
    state.template = data;
    state.currentId = data.template.metadata.template_id;
    state.revision = data.template.metadata.revision;
    state.isSystem = data.is_system;
    state.composition = {
      operator: data.template.composition.operator,
      groups: data.template.composition.groups.map((g) => ({ group_id: g.group_id, operator: g.operator, rule_ids: g.rules.slice() })),
    };
    clearDirty();
    renderTemplateSelect();
    renderMeta();
    renderRules();
    renderGroups();
    syncComposeSegments();
  } catch (err) {
    if (propagateError) throw err;
    toast(err.message, 'error');
  }
}

async function refreshTemplates() {
  state.templates = (await api('GET', '/api/templates')).templates;
  renderTemplateSelect();
}

async function validateTemplate() {
  const invalid = validateComposition();
  if (invalid) { toast(invalid, 'warn'); return; }
  try {
    await api('POST', '/api/templates/validate', { template: buildPayload() });
    toast('模板校验通过，可正常编译。', 'success');
  } catch (err) {
    toast('校验失败：' + err.message, 'error');
  }
}

async function saveTemplate() {
  if (state.isSystem) { toast('系统模板只读，请使用「另存为」。', 'warn'); return; }
  const invalid = validateComposition();
  if (invalid) { toast(invalid, 'warn'); return; }
  try {
    const res = await api('PUT', '/api/templates/' + encodeURIComponent(state.currentId), {
      template: buildPayload(),
      expected_revision: state.revision,
    });
    state.revision = res.revision;
    state.template.template.metadata.revision = res.revision;
    clearDirty();
    renderMeta();
    renderTemplateSelect();
    await refreshTemplates();
    toast('已保存（revision ' + res.revision + '）。', 'success');
  } catch (err) {
    if (err.message.includes('revision') || err.message.includes('conflict')) {
      toast('保存冲突：模板已在别处更新，请重新加载后再保存。', 'error');
    } else {
      toast('保存失败：' + err.message, 'error');
    }
  }
}

async function saveAsTemplate() {
  const invalid = validateComposition();
  if (invalid) { toast(invalid, 'warn'); return; }
  const newId = prompt('请输入新的模板 ID（小写字母、数字、连字符）');
  if (!newId) return;
  const name = prompt('请输入模板名称', '我的策略');
  if (!name) return;
  const payload = buildPayload();
  payload.metadata.template_id = newId.trim();
  payload.metadata.name = name.trim();
  payload.metadata.revision = 1;
  try {
    const res = await api('POST', '/api/templates', { template: payload });
    await refreshTemplates();
    await loadTemplate(res.template_id);
    toast('已创建用户模板（revision 1）。', 'success');
  } catch (err) {
    toast('创建失败：' + err.message, 'error');
  }
}

async function deleteTemplate() {
  if (state.isSystem) { toast('系统模板只读，不能删除。', 'warn'); return; }
  if (!confirm('确定删除模板「' + state.template.template.metadata.name + '」？此操作不可撤销。')) return;
  try {
    await api('DELETE', '/api/templates/' + encodeURIComponent(state.currentId), { expected_revision: state.revision });
    await refreshTemplates();
    const fallback = state.templates.find((t) => t.is_system) || { template_id: state.templates[0].template_id };
    await loadTemplate(fallback.template_id);
    toast('模板已删除。', 'success');
  } catch (err) {
    toast('删除失败：' + err.message, 'error');
  }
}

/* ---------- run screen ---------- */
/* ---------- research backtest (P5A-8) ---------- */
let btPollTimer = null;
let btRunId = null;


/* ---------- research strategy editor (P5A-8c) ---------- */
let btPolicyCatalog = null;

/** @returns {Promise<void>} */
async function loadResearchPolicies() {
  const data = await api('GET', '/api/research/policies');
  btPolicyCatalog = data.policies;
  renderStrategyPolicies();
}

const BT_KIND_LABELS = {
  entry: '入场（Entry）', exit: '退出（Exit）', rebalance: '调仓（Rebalance）',
  allocation: '仓位（Allocation）', ranking: '排名（Ranking）', execution: '执行（Execution）',
};
const BT_KIND_DEFAULTS = {
  entry: 'eligibility_enter_v1', exit: 'eligibility_exit_v1',
  rebalance: 'daily_v1', allocation: 'equal_weight_v1',
  ranking: 'turnover_20d_desc_v1', execution: 'ashare_execution_v1',
};
const BT_KINDS = ['entry', 'exit', 'rebalance', 'allocation', 'ranking', 'execution'];

function btFindPolicy(kind, policyId) {
  const items = (btPolicyCatalog && btPolicyCatalog[kind]) || [];
  return items.find(function (p) { return p.policy_id === policyId; }) || null;
}

function renderStrategyPolicies() {
  const container = $('#strategy-policies');
  if (!container || !btPolicyCatalog) return;
  container.innerHTML = '';
  BT_KINDS.forEach(function (kind) {
    const items = btPolicyCatalog[kind] || [];
    if (!items.length) return;
    const block = document.createElement('div');
    block.className = 'policy-block';
    const label = document.createElement('label');
    label.className = 'field';
    const span = document.createElement('span');
    span.className = 'field__label';
    span.textContent = BT_KIND_LABELS[kind] || kind;
    // 机制说明:问号悬停显示当前所选政策的中文机制(随选择更新)
    const tip = termHelp('');
    const select = document.createElement('select');
    select.className = 'select';
    select.id = 'bt-policy-' + kind;
    items.forEach(function (p) {
      const opt = document.createElement('option');
      opt.value = p.policy_id;
      opt.textContent = p.policy_id + ' (v' + p.version + ')';
      select.appendChild(opt);
    });
    if (BT_KIND_DEFAULTS[kind]) { select.value = BT_KIND_DEFAULTS[kind]; }
    const updateTip = function () {
      const policy = btFindPolicy(kind, select.value);
      tip.dataset.tip = policy ? policy.description : '';
    };
    updateTip();
    // 问号放入标题 span 内,与标题同一行紧挨(避免换行显示不全)
    span.appendChild(tip);
    label.appendChild(span);
    label.appendChild(select);
    block.appendChild(label);
    const params = document.createElement('div');
    params.className = 'policy-params';
    params.id = 'bt-policy-params-' + kind;
    block.appendChild(params);
    select.addEventListener('change', function () { renderPolicyParams(kind); updateTip(); });
    container.appendChild(block);
    renderPolicyParams(kind);
  });
}

function renderPolicyParams(kind) {
  const select = document.getElementById('bt-policy-' + kind);
  const container = document.getElementById('bt-policy-params-' + kind);
  if (!select || !container) return;
  const policy = btFindPolicy(kind, select.value);
  container.innerHTML = '';
  if (!policy || !policy.parameters || !policy.parameters.length) return;
  policy.parameters.forEach(function (param) {
    const label = document.createElement('label');
    label.className = 'field';
    const span = document.createElement('span');
    span.className = 'field__label';
    span.textContent = param.label + (param.required ? ' *' : '');
    if (param.description) { span.title = param.description; }
    let input;
    if (param.value_type === 'boolean') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = param.default_value === 'True' || param.default_value === 'true';
      input.className = 'input';
    } else {
      input = document.createElement('input');
      input.type = param.value_type === 'integer' ? 'number' : 'text';
      input.className = 'input';
      input.value = param.default_value == null ? '' : param.default_value;
      if (param.minimum != null && param.value_type === 'integer') { input.min = param.minimum; }
      if (param.maximum != null && param.value_type === 'integer') { input.max = param.maximum; }
      input.step = param.value_type === 'integer' ? '1' : 'any';
    }
    input.dataset.paramKind = kind;
    input.dataset.paramId = param.parameter_id;
    input.dataset.paramType = param.value_type;
    label.appendChild(span);
    label.appendChild(input);
    container.appendChild(label);
  });
}

function collectPolicies() {
  const policies = {};
  BT_KINDS.forEach(function (kind) {
    const select = document.getElementById('bt-policy-' + kind);
    if (!select) return;
    const policy = btFindPolicy(kind, select.value);
    if (!policy) return;
    const parameters = {};
    const container = document.getElementById('bt-policy-params-' + kind);
    if (container) {
      container.querySelectorAll('[data-param-id]').forEach(function (input) {
        const id = input.dataset.paramId;
        const type = input.dataset.paramType;
        if (type === 'boolean') { parameters[id] = input.checked; }
        else if (type === 'integer') {
          const raw = input.value === '' ? null : Number(input.value);
          parameters[id] = raw == null ? null : raw;
        } else { parameters[id] = input.value; }
      });
    }
    policies[kind] = { policy_id: select.value, version: policy.version, parameters: parameters };
  });
  return policies;
}


async function submitBacktest() {
  const progress = $('#bt-progress');
  const result = $('#bt-result');
  const btn = $('#run-backtest');
  if (!state.currentId) { progress.hidden = false; progress.textContent = '请先选择一个模板。'; return; }
  const windowYears = Math.min(8, Math.max(1, Math.floor(Number($('#bt-window').value) || 5)));
  const policies = collectPolicies();
  if (!policies.entry || !policies.exit || !policies.rebalance || !policies.allocation || !policies.ranking || !policies.execution) {
    progress.hidden = false;
    progress.textContent = '请完整选择六类回测政策。';
    return;
  }
  const body = {
    template_id: state.currentId,
    template_revision: state.template.template.metadata.revision,
    policies: policies,
    window_years: windowYears,
    initial_cash: $('#bt-cash').value.trim() || '1000000',
    max_positions: Math.min(500, Math.max(1, Math.floor(Number($('#bt-positions').value) || 20))),
    max_workers: Math.min(16, Math.max(1, Math.floor(Number($('#bt-workers').value) || 2))),
  };
  btn.disabled = true;
  result.hidden = true;
  progress.hidden = false;
  progress.textContent = '正在提交回测任务…';
  try {
    const data = await api('POST', '/api/research/backtests', body);
    btRunId = data.run_id;
    if (btPollTimer) clearInterval(btPollTimer);
    btPollTimer = setInterval(pollBacktest, 800);
  } catch (e) {
    progress.textContent = '提交失败：' + (e && e.message ? e.message : String(e));
    btn.disabled = false;
  }
}

async function pollBacktest() {
  if (!btRunId) return;
  const progress = $('#bt-progress');
  const result = $('#bt-result');
  try {
    const data = await api('GET', '/api/research/backtests/' + btRunId);
    progress.textContent = '任务状态：' + data.status
      + (data.progress_total ? '（' + data.progress_completed + '/' + data.progress_total + '）' : '');
    if (data.status === 'SUCCEEDED') {
      if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
      renderBacktestResult(data, result);
      progress.hidden = true;
      $('#run-backtest').disabled = false;
    } else if (data.status === 'FAILED') {
      if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
      progress.textContent = '回测失败：' + (data.error_message || '未知错误');
      $('#run-backtest').disabled = false;
    } else if (data.status === 'CANCELLED') {
      if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
      progress.textContent = '任务已取消。';
      $('#run-backtest').disabled = false;
    }
  } catch (e) {
    progress.textContent = '查询状态失败：' + String(e);
  }
}

function renderBacktestResult(data, container) {
  const m = data.metrics || {};
  const lines = [
    '初始资金 ' + (m.initial_cash || '-'),
    '期末净值 ' + (m.final_value || '-'),
    '总收益 ' + (m.total_return == null ? '不可用' : (Number(m.total_return) * 100).toFixed(2) + '%'),
    '最大回撤 ' + (m.max_drawdown == null ? '不可用' : (Number(m.max_drawdown) * 100).toFixed(2) + '%'),
    'Sharpe ' + (m.sharpe == null ? '不可用' : Number(m.sharpe).toFixed(3)),
    '成交 ' + (m.trade_count || 0) + ' 笔（胜 ' + (m.win_count || 0) + ' / 负 ' + (m.loss_count || 0) + '）',
    '总费用 ' + (m.total_fees || '0'),
  ];
  let warnings = '';
  const ws = data.warnings || [];
  if (ws.length) {
    warnings = '<br><span class="badge badge--warn">' + ws.length + ' 条执行限制警告</span><br>' + ws.slice(0, 8).map(esc).join('<br>');
  }
  container.innerHTML = '<strong>回测结果</strong><br>' + lines.join('<br>') + warnings
    + '<br><a href="#" data-bt-equity="' + btRunId + '" class="btn btn--ghost">查看净值与订单</a>';
  container.hidden = false;
  container.querySelector('[data-bt-equity]').addEventListener('click', async (ev) => {
    ev.preventDefault();
    const runId = ev.currentTarget.getAttribute('data-bt-equity');
    const eq = await api('GET', '/api/research/backtests/' + runId + '/equity');
    const od = await api('GET', '/api/research/backtests/' + runId + '/orders');
    let html = '<strong>净值序列</strong><br>' + (eq.points || []).slice(-10).map((p) => esc(p.trading_day) + ' ' + esc(p.equity)).join('<br>');
    html += '<br><strong>订单</strong><br>';
    html += (od.orders || []).slice(0, 20).map((o) => esc(o.trading_day) + ' ' + esc(o.code) + ' ' + esc(o.side) + ' ' + esc(o.shares) + '股 @' + esc(o.price) + ' ' + esc(o.status)).join('<br>') || '（无订单）';
    container.innerHTML = html + '<br><a href="#" id="bt-back" class="btn btn--ghost">返回摘要</a>';
    container.querySelector('#bt-back').addEventListener('click', (e2) => { e2.preventDefault(); renderBacktestResult(data, container); });
  });
}

function runtimeConditions() {
  const dataset = $('#dataset').value.trim();
  const tradingDay = $('#trading-day').value;
  const adjustment = $('#adjustment').value;
  const codesRaw = $('#codes').value.trim();
  const codes = codesRaw ? codesRaw.split(/[,，;；\s]+/).map((s) => s.trim()).filter(Boolean) : undefined;
  const workersInput = $('#max-workers').value;
  const workers = workersInput ? Math.min(16, Math.max(1, Math.floor(Number(workersInput) || 4))) : 4;
  $('#max-workers').value = workers;
  return { dataset_id: dataset, trading_day: tradingDay, adjustment, codes, max_workers: workers };
}

let screenPollTimer = null;
function renderScreenProgress(p) {
  const track = $('#screen-progress-track');
  const fill = $('#screen-progress-fill');
  const current = $('#screen-current');
  const active = p && p.status === 'running';
  if (!active) {
    track.hidden = true;
    current.hidden = true;
    return;
  }
  const total = Number(p.total || 0);
  const done = Number(p.done || 0);
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  track.hidden = false;
  fill.style.width = pct + '%';
  current.hidden = false;
  current.textContent = total
    ? '第 ' + done + '/' + total + ' 只 · ' + (p.current_code || '-')
    : (p.message || '正在筛选…');
}
async function pollScreenProgress() {
  try {
    const p = await api('GET', '/api/screen/progress');
    renderScreenProgress(p);
  } catch (e) { /* ignore transient poll errors */ }
}
function startScreenPolling() {
  if (screenPollTimer) return;
  pollScreenProgress();
  screenPollTimer = setInterval(pollScreenProgress, 500);
}
function stopScreenPolling() {
  if (screenPollTimer) { clearInterval(screenPollTimer); screenPollTimer = null; }
}

/** @returns {Promise<void>} */
async function runScreen() {
  const invalid = validateComposition();
  if (invalid) { toast(invalid, 'warn'); return; }
  const cond = runtimeConditions();
  if (!cond.dataset_id) { toast('请填写数据集。', 'warn'); return; }
  if (!cond.trading_day) { toast('请选择交易日。', 'warn'); return; }
  const progress = $('#screen-progress');
  progress.hidden = false;
  progress.textContent = '正在运行筛选…';
  const btn = $('#run-screen');
  btn.disabled = true;
  invalidateStockAnalysis();
  state.screenPending = true;
  renderStockDetail();
  renderCapmSettings();
  startScreenPolling();
  try {
    const before = await readMarketGeneration();
    const data = await api('POST', '/api/screen', { template: buildPayload(), ...cond });
    await readMarketGeneration();
    acceptScreenResult(data, before);
    renderResults();
    renderStockDetail();
    renderCapmSettings();
    toast('筛选完成：共 ' + data.summary.total + ' 只。', 'success');
  } catch (err) {
    state.result = null;
    renderResults();
    renderStockDetail();
    renderCapmSettings();
    toast('筛选失败：' + err.message, 'error');
  } finally {
    progress.hidden = true;
    stopScreenPolling();
    $('#screen-progress-track').hidden = true;
    $('#screen-current').hidden = true;
    btn.disabled = false;
    state.screenPending = false;
  }
}

let syncPollTimer = null;
function startSyncPolling() {
  if (syncPollTimer) return;
  pollSyncProgress();
  pollBackfillV2();
  pollPipelineProgress();
  pollCapmProgress();
  syncPollTimer = setInterval(function () {
    pollCapmProgress();
    // 新架构 pipeline 显示期间,旧进度渲染全部让位,避免每秒闪烁。
    if (pipelineProgressActive) {
      pollPipelineProgress();
      return;
    }
    pollSyncProgress();
    pollBackfillV2();
    pollPipelineProgress();
  }, 1000);
}


let backfillV2Active = false;
// 新架构 pipeline 进度正在显示时,renderSyncProgress 不得隐藏进度条。
let pipelineProgressActive = false;

async function pollBackfillV2() {
  try {
    const p = await api('GET', '/api/sync/backfill/progress');
    const active = p && p.status === 'RUNNING';
    backfillV2Active = active;
    if (!active) {
      // 空闲:由 renderSyncProgress 按原逻辑处理(Web 回补或隐藏)
      return;
    }
    const track = $('#sync-progress-track');
    const fill = $('#sync-progress-fill');
    const current = $('#sync-current');
    const meta = $('#sync-progress');
    const batchTrack = $('#sync-batch-track');
    const batchFill = $('#sync-batch-fill');
    const batchLabel = $('#sync-batch-label');
    const pct = Math.round((p.progress || 0) * 100);
    // 第一个进度条:批次内进度(当前批的日线/基本面逐代码进度)
    const b = p.batch || {};
    const bTotal = Number(b.total || 0);
    const bCompleted = Number(b.completed || 0);
    const bPct = bTotal ? Math.min(100, Math.round((bCompleted / bTotal) * 100)) : 0;
    const batchPhaseLabel = { daily_bars: '日线', fundamentals: '基本面', dividends: '分红' }[b.phase] || b.phase || '';
    if (bTotal) {
      batchTrack.hidden = false;
      batchFill.style.width = bPct + '%';
      batchLabel.hidden = false;
      batchLabel.textContent = '批次：' + batchPhaseLabel + ' ' + bCompleted + '/' + bTotal + ' · ' + (b.current_code || '-');
    } else {
      batchTrack.hidden = true;
      batchLabel.hidden = true;
    }
    // 第二个进度条:总进度(按完整入库股票数),最后一行动态显示阶段与进度
    track.hidden = false;
    fill.style.width = pct + '%';
    current.hidden = false;
    current.textContent = '八年回补 总进度 ' + pct + '%（已完整入库 ' + p.covered_days + '/' + p.total_days + ' 只股票）'
      + (batchPhaseLabel ? ' · ' + batchPhaseLabel : '');
    meta.hidden = true; // 去掉第一行静态描述,动态信息并入最后一行
  } catch (e) {
    backfillV2Active = false;
  }
}

function renderSyncProgress(p) {
  if (backfillV2Active) return; // 八年回补驱动中,由 pollBackfillV2 渲染
  if (pipelineProgressActive) return; // 新架构进度正在显示,不隐藏
  const track = $('#sync-progress-track');
  const fill = $('#sync-progress-fill');
  const current = $('#sync-current');
  const meta = $('#sync-progress');
  const batchTrack = $('#sync-batch-track');
  const batchFill = $('#sync-batch-fill');
  const batchLabel = $('#sync-batch-label');
  const active = p && (p.status === 'running' || p.status === 'error');
  if (!active) {
    track.hidden = true;
    current.hidden = true;
    meta.hidden = true;
    batchTrack.hidden = true;
    batchLabel.hidden = true;
    return;
  }
  // 第一条：当前批次内（逐代码）进度
  const bTotal = Number(p.batch_total || 0);
  const bCompleted = Number(p.batch_completed || 0);
  const bPct = bTotal ? Math.min(100, Math.round((bCompleted / bTotal) * 100)) : 0;
  const batchPhase = p.batch_phase || '';
  const batchPhaseLabel = { daily_bars: '日线', fundamentals: '基本面', dividends: '分红' }[batchPhase] || batchPhase;
  batchTrack.hidden = false;
  batchTrack.className = p.status === 'error' ? 'progress-track progress-track--error' : 'progress-track';
  batchFill.style.width = bPct + '%';
  batchLabel.hidden = false;
  batchLabel.textContent = bTotal
    ? '批次：' + batchPhaseLabel + ' ' + bCompleted + '/' + bTotal + ' · ' + (p.current_code || '-')
    : '批次：' + (p.message || '等待中…');
  // 第二条：总进度
  const total = Number(p.total || 0);
  const completed = Number(p.completed || 0);
  const pct = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  track.hidden = false;
  track.className = p.status === 'error' ? 'progress-track progress-track--error' : 'progress-track';
  fill.style.width = pct + '%';
  current.hidden = false;
  const phase = p.phase || '';
  const label = { daily_bars: '日线', fundamentals: '基本面', dividends: '分红', starting: '准备中', backfill: '回补历史' }[phase] || phase;
  const day = p.trading_day ? (' @ ' + p.trading_day) : '';
  current.textContent = total
    ? '总进度 ' + pct + '%（' + completed + '/' + total + '）· ' + label
    : '总进度：' + (p.message || '准备中…');
  meta.hidden = false;
  meta.textContent = '同步 ' + (p.dataset_id || '') + day + ' · ' + label;
}
async function pollSyncProgress() {
  try {
    const p = await api('GET', '/api/sync/progress');
    renderSyncProgress(p);
  } catch (e) { /* ignore transient poll errors */ }
}

/** @returns {Promise<void>} */
async function pollPipelineProgress() {
  try {
    const p = await api('GET', '/api/sync/pipeline/progress');
    if (p && Object.hasOwn(p, 'generation')) observeMarketGeneration(p.generation ? p.generation.generation : null);
    if (!p || p.status === 'none') {
      pipelineProgressActive = false;
      return;
    }
    pipelineProgressActive = true;
    // 回补运行状态 → 初始化按钮禁用/恢复(防重复点击;杀 runner 后
    // 计划被重置为 PLANNED,按钮随之恢复可点)。
    const running = p.status === 'RUNNING';
    setBootstrapButtonsDisabled(running, running ? '回补进行中…' : null);
    const track = $('#sync-progress-track');
    const fill = $('#sync-progress-fill');
    const current = $('#sync-current');
    const meta = $('#sync-progress');
    const batchTrack = $('#sync-batch-track');
    const batchFill = $('#sync-batch-fill');
    const batchLabel = $('#sync-batch-label');
    const pct = Math.round((p.progress || 0) * 100);
    // 批次内进度(当前 RUNNING 任务)
    const b = p.batch || {};
    const bTotal = Number(b.total || 0);
    const bCompleted = Number(b.completed || 0);
    const bPct = bTotal ? Math.min(100, Math.round((bCompleted / bTotal) * 100)) : 0;
    if (bTotal) {
      batchTrack.hidden = false;
      batchFill.style.width = bPct + '%';
      batchLabel.hidden = false;
      const phaseLabel = { daily_bars: '日线', fundamentals: '基本面', stocks: '股票池' }[b.data_type] || b.data_type || '';
      batchLabel.textContent = '当前批次：' + phaseLabel + ' ' + bCompleted + '/' + bTotal + ' · ' + (b.current_code || '-');
    } else {
      batchTrack.hidden = true;
      batchLabel.hidden = true;
    }
    track.hidden = false;
    fill.style.width = pct + '%';
    current.hidden = false;
    current.textContent = '八年回补 总进度 ' + pct + '%（已完成 ' + p.completed_tasks + '/' + p.total_tasks + ' 个任务）';
    meta.hidden = false;
    meta.textContent = '股票池 · ' + ({RUNNING: '同步中', PLANNED: '已中断 / 待继续', FAILED: '失败，点击重试',
      SUCCEEDED: '已发布'}[p.status] || p.status) + ' · ' + (p.mode || '') + ' · ' + p.target_start + ' ~ ' + p.target_end;
    if (p.errors && p.errors.length) meta.textContent += ' · ' + p.errors.slice(0, 3).join('；');
  } catch (e) { /* ignore transient */ }
}

async function shutdownServer() {
  if (!window.confirm('确定停止本服务进程吗？停止后需要重新启动才能继续使用。')) return;
  const status = $('#shutdown-status') || $('#gate-shutdown-status');
  if (!status) return;
  status.hidden = false;  status.textContent = '正在停止服务…';
  try {
    await api('POST', '/api/shutdown', { confirm: true });
  } catch (err) {
    // 服务可能在响应前就退出，连接错误同样视为已停止
  }
  status.textContent = '服务已停止，请关闭本页面。';
}

const STATUS_COLORS = {
  synced: '#2ecc71',
  running: '#f39c12',
  incomplete: '#f39c12',
  missing: '#95a5a6',
  failed: '#e74c3c',
  nontrading: '#ecf0f1',
};

/* ---------- 视图切换(数据 UI / 筛选工作台 / 回测系统)与本地库选择 ---------- */
function applyView() {
  const gate = $('#gate-view');
  const workbench = $('#workbench-view');
  const backtest = $('#backtest-view');
  if (!gate || !workbench) return;
  const show = (view) => {
    gate.hidden = view !== 'gate';
    workbench.hidden = view !== 'workbench';
    if (backtest) backtest.hidden = view !== 'backtest';
  };
  if (state.uiView === 'workbench' || state.uiView === 'gate' || state.uiView === 'backtest') {
    show(state.uiView);
  } else {
    show('gate');
  }
}

function setView(view) {
  state.uiView = view;
  applyView();
  if (view === 'gate') {
    loadInstances();   // 刷新 runner 状态
    loadSyncStatus();  // 刷新数据状态与版本区
  }
  if (view === 'backtest') {
    ensureBacktestView();
    loadSyncStatus();
  }
}

/** 回测系统视图就绪后加载其基础数据(策略目录/策略模板/筛选模板/历史)。@returns {void} */
let _backtestReady = false;
function ensureBacktestView() {
  if (_backtestReady) return;
  _backtestReady = true;
  loadBacktestWorkspace().catch((error) => {
    _backtestReady = false;
    toast('回测系统加载失败：' + (error && error.message ? error.message : String(error)), 'err');
  });
}

/* ---------- 回测系统：基础数据加载与历史列表 ---------- */
let btStrategies = [];
let btCurrentStrategy = null;   // {strategy_template_id, revision, name, description, policies}
let btEditorDirty = false;

/** @returns {Promise<void>} */
async function loadBacktestWorkspace() {
  const [policies, strategies] = await Promise.all([
    api('GET', '/api/research/policies'),
    api('GET', '/api/research/strategies'),
  ]);
  btPolicyCatalog = policies.policies;
  btStrategies = strategies.strategies;
  populateBtScreeningSelect();
  renderBtStrategySelect();
  setBtMode('eligibility');
  btUseDefaultEditor();
  await loadBacktestHistory();
  markBtDirty(false);
}

/** @returns {void} */
function populateBtScreeningSelect() {
  const select = $('#bt-screening-template');
  if (!select) return;
  select.innerHTML = '';
  const templates = state.templates || [];
  templates.forEach((item) => {
    const option = document.createElement('option');
    option.value = item.template_id;
    option.textContent = item.name + ' (rev ' + item.revision + ')';
    if (item.is_system) option.selected = true;
    select.appendChild(option);
  });
  if (!select.value && templates.length) select.value = templates[0].template_id;
}

/** @returns {void} */
function renderBtStrategySelect() {
  const select = $('#bt-strategy-select');
  const name = $('#bt-strategy-name');
  const description = $('#bt-strategy-description');
  if (!select) return;
  select.innerHTML = '';
  const items = btStrategies.length ? btStrategies
    : [{ strategy_template_id: 'default-backtest-v1', name: '默认策略', revision: 1, is_system: true }];
  items.forEach((item) => {
    const option = document.createElement('option');
    option.value = item.strategy_template_id;
    option.textContent = (item.is_system ? '内置 · ' : '') + item.name + ' (rev ' + item.revision + ')';
    select.appendChild(option);
  });
  const current = select.value || items[0].strategy_template_id;
  select.value = current;
  if (name) name.value = items.find((i) => i.strategy_template_id === current)?.name || '';
  if (description) description.value = '';
}

/** @param {boolean} dirty @returns {void} */
function markBtDirty(dirty) {
  btEditorDirty = dirty;
  const badge = $('#bt-strategy-dirty');
  if (badge) badge.hidden = !dirty;
}

/* ---------- P5C 策略编辑器(入场组/退出组/止盈档/其它政策) ---------- */
const BT_SINGLE_KINDS = ['rebalance', 'allocation', 'ranking', 'execution'];
const BT_SINGLE_LABELS = {
  rebalance: '调仓', allocation: '分配', ranking: '排名', execution: '执行',
};
const btEditor = {
  entryOperator: 'any', exitOperator: 'any',
  entry: [], exit: [], tiers: [], singles: {},
};

/** @returns {void} */
function resetBtEditor() {
  btEditor.entry = [];
  btEditor.exit = [];
  btEditor.tiers = [];
  btEditor.entryOperator = 'any';
  btEditor.exitOperator = 'any';
  btEditor.singles = {};
  BT_SINGLE_KINDS.forEach((kind) => {
    const first = (btPolicyCatalog && btPolicyCatalog[kind] && btPolicyCatalog[kind][0]);
    btEditor.singles[kind] = first ? { policy_id: first.policy_id, parameters: {} } : null;
  });
}

/** @param {object} def @returns {string} 首个政策 id */
function btFirstPolicyId(kind) {
  const items = (btPolicyCatalog && btPolicyCatalog[kind]) || [];
  return items.length ? items[0].policy_id : '';
}

/**
 * 用 canonical policies(payload 结构)填充编辑器。
 * @param {object} policies
 * @returns {void}
 */
function btEditorFromPolicies(policies) {
  resetBtEditor();
  const fill = (kind) => {
    const raw = policies[kind];
    if (!raw) return;
    if (Array.isArray(raw)) { btEditor[kind] = raw.map((item) => ({ policy_id: item.policy_id, parameters: item.parameters || {} })); return; }
    if (typeof raw === 'object' && raw.items) {
      if (kind === 'entry' || kind === 'exit') {
        btEditor[kind] = raw.items.map((item) => ({ policy_id: item.policy_id, parameters: item.parameters || {} }));
        if (raw.operator === 'all') btEditor[kind + 'Operator'] = 'all';
      } else {
        btEditor.singles[kind] = { policy_id: raw.policy_id || (raw.items && raw.items[0] && raw.items[0].policy_id), parameters: (raw.parameters || {}) };
      }
      return;
    }
    // 旧式单对象
    if (kind === 'entry' || kind === 'exit') {
      btEditor[kind] = [{ policy_id: raw.policy_id, parameters: raw.parameters || {} }];
    } else {
      btEditor.singles[kind] = { policy_id: raw.policy_id, parameters: raw.parameters || {} };
    }
  };
  ['entry', 'exit'].forEach(fill);
  BT_SINGLE_KINDS.forEach(fill);
  btEditor.tiers = (policies.take_profit_tiers || []).map((t) => ({
    take_profit_ratio: t.take_profit_ratio == null ? '' : String(t.take_profit_ratio),
    partial_ratio: t.partial_ratio == null ? '' : String(t.partial_ratio),
  }));
}

/** @returns {void} */
function renderBtEditor() {
  renderBtGroupItems('entry');
  renderBtGroupItems('exit');
  renderBtOperatorButtons();
  renderBtTiers();
  renderBtSingles();
}

/** @param {string} kind @returns {void} */
function renderBtGroupItems(kind) {
  const container = document.getElementById('bt-' + kind + '-items');
  if (!container) return;
  container.innerHTML = '';
  const catalog = (btPolicyCatalog && btPolicyCatalog[kind]) || [];
  if (!catalog.length) { container.innerHTML = '<p class="panel__hint">该组暂无可用政策。</p>'; return; }
  btEditor[kind].forEach((item, index) => {
    const policy = btFindPolicy(kind, item.policy_id) || catalog[0];
    const card = document.createElement('div');
    card.className = 'bt-policy-card';
    const head = document.createElement('div');
    head.className = 'bt-policy-card__head';
    const select = document.createElement('select');
    select.className = 'select';
    select.setAttribute('data-kind', kind);
    select.setAttribute('data-index', String(index));
    catalog.forEach((p) => {
      const option = document.createElement('option');
      option.value = p.policy_id;
      option.textContent = p.policy_id + ' (v' + p.version + ')';
      select.appendChild(option);
    });
    select.value = policy.policy_id;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn--danger btn--sm bt-remove-btn';
    remove.textContent = '删除';
    remove.setAttribute('data-bt-remove', kind + ':' + index);
    head.appendChild(select);
    head.appendChild(remove);
    card.appendChild(head);
    const desc = document.createElement('p');
    desc.className = 'bt-policy-desc';
    desc.dataset.policyDesc = kind + ':' + index;
    desc.textContent = policy.description || '';
    card.appendChild(desc);
    const params = document.createElement('div');
    params.className = 'bt-policy-card__params';
    params.dataset.params = kind + ':' + index;
    card.appendChild(params);
    container.appendChild(card);
    renderBtPolicyParams(kind, index);
  });
}

/** @param {string} kind @param {number} index @returns {void} 更新政策卡的中文机制说明 */
function refreshBtPolicyDesc(kind, index) {
  const el = document.querySelector('#bt-' + kind + '-items [data-policy-desc="' + kind + ':' + index + '"]');
  if (!el) return;
  const item = btEditor[kind][index];
  if (!item) return;
  const policy = btFindPolicy(kind, item.policy_id);
  el.textContent = policy ? policy.description || '' : '';
}

/**
 * @param {string} kind @param {number} index @param {boolean} wide
 * @returns {void}
 */
function renderBtPolicyParams(kind, index) {
  const holder = document.querySelector('#bt-' + kind + '-items [data-params="' + kind + ':' + index + '"]');
  if (!holder) return;
  holder.innerHTML = '';
  const item = btEditor[kind][index];
  if (!item) return;
  const policy = btFindPolicy(kind, item.policy_id);
  if (!policy || !policy.parameters || !policy.parameters.length) return;
  policy.parameters.forEach((param) => {
    if (kind === 'singles' || kind === 'allocation') {
      if (param.parameter_id === 'max_positions') return; // 由基础数据唯一覆盖
    }
    const label = document.createElement('label');
    label.className = 'field' + (param.parameter_id === 'cash_reserve_ratio' ? ' field--wide' : '');
    const span = document.createElement('span');
    span.className = 'field__label';
    span.textContent = param.label + (param.required ? ' *' : '');
    if (param.description) span.title = param.description;
    let input;
    if (param.value_type === 'boolean') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'input';
    } else {
      input = document.createElement('input');
      input.type = param.value_type === 'integer' ? 'number' : 'text';
      input.className = 'input';
      input.step = param.value_type === 'integer' ? '1' : 'any';
      if (param.minimum != null && param.value_type === 'integer') input.min = param.minimum;
      if (param.maximum != null && param.value_type === 'integer') input.max = param.maximum;
    }
    input.dataset.policyKind = kind;
    input.dataset.policyIndex = String(index);
    input.dataset.paramId = param.parameter_id;
    input.dataset.paramType = param.value_type;
    input.dataset.default = param.default_value == null ? '' : param.default_value;
    const existing = item.parameters && item.parameters[param.parameter_id];
    if (param.value_type === 'boolean') {
      input.checked = existing != null ? Boolean(existing) : (param.default_value === 'True' || param.default_value === 'true');
    } else {
      input.value = existing != null ? existing : (param.default_value == null ? '' : param.default_value);
    }
    label.appendChild(span);
    label.appendChild(input);
    holder.appendChild(label);
  });
  if (kind === 'allocation') {
    const note = document.createElement('p');
    note.className = 'panel__hint field--wide';
    note.textContent = '最大持仓数不在此设置：由左侧「回测基础数据」统一覆盖。';
    holder.appendChild(note);
  }
}

/** @returns {void} */
function renderBtOperatorButtons() {
  [['entry', 'btEditor.entryOperator'], ['exit', 'btEditor.exitOperator']].forEach(([kind]) => {
    document.querySelectorAll('[data-bt-op-kind="' + kind + '"]').forEach((button) => {
      const value = button.dataset.btOpValue;
      const active = btEditor[kind + 'Operator'] === value;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  });
}

/** 渲染止盈档(小数比例,与后端同一口径:0.02=2%),不做任何百分制换算。@returns {void} */
function renderBtTiers() {
  const container = $('#bt-tp-items');
  if (!container) return;
  container.innerHTML = '';
  if (!btEditor.tiers.length) {
    container.innerHTML = '<p class="panel__hint">未设置止盈档：按各退出政策整仓退出（历史默认行为）。</p>';
    return;
  }
  btEditor.tiers.forEach((tier, index) => {
    const card = document.createElement('div');
    card.className = 'bt-policy-card';
    const head = document.createElement('div');
    head.className = 'bt-policy-card__head';
    const title = document.createElement('span');
    title.className = 'field__label';
    title.textContent = '第 ' + (index + 1) + ' 档（低档优先触发）';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn--danger btn--sm bt-remove-btn';
    remove.textContent = '删除';
    remove.setAttribute('data-bt-remove-tier', String(index));
    head.appendChild(title);
    head.appendChild(remove);
    card.appendChild(head);
    const params = document.createElement('div');
    params.className = 'bt-policy-card__params';
    const addInput = (id, labelText, placeholderText) => {
      const label = document.createElement('label');
      label.className = 'field';
      const span = document.createElement('span');
      span.className = 'field__label';
      span.textContent = labelText;
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'input';
      input.value = valueOf(id);
      input.placeholder = placeholderText;
      input.dataset.tierIndex = String(index);
      input.dataset.tierField = id;
      label.appendChild(span);
      label.appendChild(input);
      params.appendChild(label);
      return input;
    };
    const valueOf = (id) => {
      const v = tier[id];
      return v == null || v === '' ? '' : String(v);
    };
    addInput('take_profit_ratio', '触发涨幅比例（小数）', '如 0.02 = 2%');
    addInput('partial_ratio', '减仓比例（小数）', '如 0.1 = 10%');
    card.appendChild(params);
    container.appendChild(card);
  });
}

/** @returns {void} */
function renderBtSingles() {
  const container = $('#bt-singles');
  if (!container) return;
  container.innerHTML = '';
  BT_SINGLE_KINDS.forEach((kind) => {
    const items = (btPolicyCatalog && btPolicyCatalog[kind]) || [];
    if (!items.length) return;
    const chosen = btEditor.singles[kind] || {};
    const policy = btFindPolicy(kind, chosen.policy_id) || items[0];
    const card = document.createElement('div');
    card.className = 'bt-single-card';
    const head = document.createElement('div');
    head.className = 'bt-single-card__head';
    const kindLabel = document.createElement('span');
    kindLabel.className = 'field__label';
    kindLabel.textContent = BT_SINGLE_LABELS[kind] || kind;
    const select = document.createElement('select');
    select.className = 'select';
    select.dataset.singleKind = kind;
    items.forEach((p) => {
      const option = document.createElement('option');
      option.value = p.policy_id;
      option.textContent = p.policy_id + ' (v' + p.version + ')';
      select.appendChild(option);
    });
    select.value = policy.policy_id;
    head.appendChild(kindLabel);
    head.appendChild(select);
    card.appendChild(head);
    const desc = document.createElement('p');
    desc.className = 'bt-policy-desc';
    desc.dataset.singleDesc = kind;
    desc.textContent = policy.description || '';
    card.appendChild(desc);
    const params = document.createElement('div');
    params.className = 'bt-single-card__params';
    params.dataset.singleParams = kind;
    card.appendChild(params);
    container.appendChild(card);
    renderBtSingleParams(kind);
  });
}

/** @param {string} kind @returns {void} 更新单项政策卡的中文机制说明 */
function refreshBtSingleDesc(kind) {
  const el = document.querySelector('#bt-singles [data-single-desc="' + kind + '"]');
  if (!el) return;
  const select = document.querySelector('#bt-singles select[data-single-kind="' + kind + '"]');
  if (!select) return;
  const policy = btFindPolicy(kind, select.value);
  el.textContent = policy ? policy.description || '' : '';
}

/** @param {string} kind @returns {void} */
function renderBtSingleParams(kind) {
  const holder = document.querySelector('#bt-singles [data-single-params="' + kind + '"]');
  if (!holder) return;
  holder.innerHTML = '';
  const select = document.querySelector('#bt-singles select[data-single-kind="' + kind + '"]');
  if (!select) return;
  const policy = btFindPolicy(kind, select.value);
  if (!policy || !policy.parameters || !policy.parameters.length) return;
  policy.parameters.forEach((param) => {
    if (param.parameter_id === 'max_positions') return; // 基础数据覆盖
    const label = document.createElement('label');
    label.className = 'field';
    const span = document.createElement('span');
    span.className = 'field__label';
    span.textContent = param.label + (param.required ? ' *' : '');
    if (param.description) span.title = param.description;
    const input = document.createElement('input');
    if (param.value_type === 'boolean') {
      input.type = 'checkbox';
      input.className = 'input';
    } else {
      input.type = param.value_type === 'integer' ? 'number' : 'text';
      input.className = 'input';
      input.step = param.value_type === 'integer' ? '1' : 'any';
    }
    input.dataset.singleKind = kind;
    input.dataset.paramId = param.parameter_id;
    input.dataset.paramType = param.value_type;
    const single = btEditor.singles[kind] || {};
    const existing = single.parameters && single.parameters[param.parameter_id];
    if (param.value_type === 'boolean') {
      input.checked = existing != null ? Boolean(existing) : (param.default_value === 'True' || param.default_value === 'true');
    } else {
      input.value = existing != null ? existing : (param.default_value == null ? '' : param.default_value);
    }
    label.appendChild(span);
    label.appendChild(input);
    holder.appendChild(label);
  });
}

/** @returns {object} 收集编辑器当前 canonical policies payload */
function collectBtPolicies() {
  const group = (kind) => {
    const operator = btEditor[kind + 'Operator'] || 'any';
    const items = (btEditor[kind] || []).map((item, index) => {
      const select = document.querySelector('#bt-' + kind + '-items [data-kind="' + kind + '"][data-index="' + index + '"]');
      const policyId = select ? select.value : item.policy_id;
      const policy = btFindPolicy(kind, policyId);
      const parameters = {};
      const holder = document.querySelector('#bt-' + kind + '-items [data-params="' + kind + ':' + index + '"]');
      if (holder) {
        holder.querySelectorAll('[data-param-id]').forEach((input) => {
          parameters[input.dataset.paramId] = btParamValue(input);
        });
      }
      return { policy_id: policyId, version: policy ? policy.version : 1, parameters };
    });
    return { operator, items };
  };
  const policies = {
    entry: group('entry'),
    exit: group('exit'),
    take_profit_tiers: (btEditor.tiers || []).map((tier, index) => {
      const read = (field) => {
        const input = document.querySelector('#bt-tp-items [data-tier-index="' + index + '"][data-tier-field="' + field + '"]');
        return input ? Number(input.value) : null;
      };
      const ratio = read('take_profit_ratio');
      const partial = read('partial_ratio');
      return {
        take_profit_ratio: ratio == null || Number.isNaN(ratio) ? '0' : String(ratio),
        partial_ratio: partial == null || Number.isNaN(partial) ? '0' : String(partial),
      };
    }),
  };
  BT_SINGLE_KINDS.forEach((kind) => {
    const select = document.querySelector('#bt-singles select[data-single-kind="' + kind + '"]');
    const chosen = btEditor.singles[kind] || {};
    const policyId = select ? select.value : (chosen.policy_id || '');
    const policy = btFindPolicy(kind, policyId);
    const parameters = {};
    const holder = document.querySelector('#bt-singles [data-single-params="' + kind + '"]');
    if (holder) {
      holder.querySelectorAll('[data-param-id]').forEach((input) => {
        parameters[input.dataset.paramId] = btParamValue(input);
      });
    }
    if ((kind === 'allocation') && parameters.max_positions == null) {
      parameters.max_positions = Math.min(500, Math.max(1, Math.floor(Number($('#bt-positions').value) || 20)));
    }
    policies[kind] = { policy_id: policyId, version: policy ? policy.version : 1, parameters };
  });
  return policies;
}

/** @param {HTMLElement} input @returns {string|number|boolean|null} */
function btParamValue(input) {
  const type = input.dataset.paramType;
  if (type === 'boolean') return input.checked;
  if (type === 'integer') {
    const raw = input.value === '' ? null : Number(input.value);
    return raw == null || Number.isNaN(raw) ? null : raw;
  }
  return input.value;
}

/** @returns {Promise<void>} 读取当前所选策略模板进编辑器 */
async function loadBtStrategy(strategyTemplateId) {
  try {
    const data = await api('GET', '/api/research/strategies/' + encodeURIComponent(strategyTemplateId));
    btCurrentStrategy = data;
    btEditorFromPolicies(data.policies);
    const nameInput = $('#bt-strategy-name');
    const description = $('#bt-strategy-description');
    if (nameInput) nameInput.value = data.name || '';
    if (description) description.value = data.description || '';
    renderBtEditor();
    markBtDirty(false);
    const status = $('#bt-strategy-status');
    if (status) { status.hidden = true; status.textContent = ''; }
  } catch (error) {
    toast('读取策略模板失败：' + (error && error.message ? error.message : String(error)), 'err');
  }
}

/** @returns {void} */
function btUseDefaultEditor() {
  const def = btStrategies.find((s) => s.is_system) || null;
  if (def) { loadBtStrategy(def.strategy_template_id); return; }
  resetBtEditor();
  btCurrentStrategy = null;
  renderBtEditor();
  markBtDirty(false);
}

/** @returns {Promise<object>} 校验本地编辑结果并返回 canonical */
async function btValidateLocal() {
  const policies = collectBtPolicies();
  if (!btEditor.entry.length || !btEditor.exit.length) {
    throw new Error('入场组与退出组至少各含一个政策。');
  }
  if (btEditor.entry.length > 5 || btEditor.exit.length > 5 || btEditor.tiers.length > 5) {
    throw new Error('每组政策与止盈档最多 5 个。');
  }
  btEditor.tiers.forEach((tier, index) => {
    const t = policies.take_profit_tiers[index];
    const tr = Number(t.take_profit_ratio);
    const pr = Number(t.partial_ratio);
    if (!(tr > 0) || !(pr > 0 && pr < 1)) {
      throw new Error('第 ' + (index + 1) + ' 档止盈：触发涨幅需 >0（小数，如 0.02=2%）；减仓比例需在 (0,1) 区间（小数，如 0.1=10%）。');
    }
  });
  const data = await api('POST', '/api/research/strategies/validate', { policies });
  return data.policies || policies;
}

/** @returns {Promise<void>} */
async function btSaveStrategy(saveAs) {
  const select = $('#bt-strategy-select');
  const nameInput = $('#bt-strategy-name');
  const description = $('#bt-strategy-description');
  if (!select || !nameInput) return;
  const status = $('#bt-strategy-status');
  const show = (text, isError) => {
    if (!status) return;
    status.hidden = false;
    status.textContent = text;
    status.className = 'panel__hint' + (isError ? ' badge--err' : '');
  };
  try {
    let normalized = await btValidateLocal();
    let name = (nameInput.value || '').trim();
    if (!name) { show('请填写策略名称。', true); return; }
    if (saveAs) {
      const result = await api('POST', '/api/research/strategies', {
        strategy_template_id: btSlugify(name),
        name,
        description: (description.value || '').trim(),
        policies: normalized,
      });
      await refreshBtStrategies();
      select.value = result.strategy_template_id;
      await loadBtStrategy(result.strategy_template_id);
      show('已另存为「' + result.strategy_template_id + '」(rev ' + result.revision + ')。');
      return;
    }
    if (!btCurrentStrategy) {
      const id = btSlugify(name);
      const result = await api('POST', '/api/research/strategies', {
        strategy_template_id: id, name, description: (description.value || '').trim(), policies: normalized,
      });
      await refreshBtStrategies();
      select.value = result.strategy_template_id;
      await loadBtStrategy(result.strategy_template_id);
      show('已创建策略「' + name + '」(rev ' + result.revision + ')。');
      return;
    }
    const result = await api('PUT', '/api/research/strategies/' + encodeURIComponent(btCurrentStrategy.strategy_template_id), {
      expected_revision: btCurrentStrategy.revision,
      name, description: (description.value || '').trim(), policies: normalized,
    });
    btCurrentStrategy.revision = result.revision;
    await refreshBtStrategies();
    select.value = btCurrentStrategy.strategy_template_id;
    markBtDirty(false);
    show('已保存「' + btCurrentStrategy.name + '」(rev ' + result.revision + ')。');
  } catch (error) {
    if (error && error.status === 409) {
      show('保存冲突：该模板已被他人/其它窗口修改，请重新载入后重试。', true);
      return;
    }
    show('保存失败：' + (error && error.message ? error.message : String(error)), true);
  }
}

/** @param {string} name @returns {string} */
function btSlugify(name) {
  const base = String(name).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-').replace(/^-+|-+$/g, '');
  const latin = base.replace(/[\u4e00-\u9fa5]/g, '').replace(/-+/g, '-').replace(/^-|-$/g, '') || 'strategy';
  let id = latin;
  let n = 2;
  while (btStrategies.some((s) => s.strategy_template_id === id)) { id = latin + '-' + n; n += 1; }
  return id;
}

/** @returns {Promise<void>} */
async function refreshBtStrategies() {
  const data = await api('GET', '/api/research/strategies');
  btStrategies = data.strategies;
  renderBtStrategySelect();
}

/** @returns {Promise<void>} */
async function btDeleteStrategy() {
  if (!btCurrentStrategy) return;
  if (btCurrentStrategy.is_system) { toast('系统内置策略不可删除。', 'warn'); return; }
  if (!confirm('删除策略「' + btCurrentStrategy.name + '」？此操作不可撤销。')) return;
  try {
    await api('DELETE', '/api/research/strategies/' + encodeURIComponent(btCurrentStrategy.strategy_template_id),
      { expected_revision: btCurrentStrategy.revision });
    btCurrentStrategy = null;
    await refreshBtStrategies();
    btUseDefaultEditor();
    const status = $('#bt-strategy-status');
    if (status) { status.hidden = true; status.textContent = ''; }
    toast('已删除策略。', 'ok');
  } catch (error) {
    toast('删除失败：' + (error && error.message ? error.message : String(error)), 'err');
  }
}

/* ---------- P5C 回测运行与结果 ---------- */
let btActiveRun = null;
let btResultPayload = null;
let btOrdersAll = [];
let btOrdersOffset = 0;
const BT_ORDERS_PAGE = 100;

/** @param {string|null} runId @returns {void} */
function setBtRun(runId) {
  btRunId = runId;
  btActiveRun = runId;
  if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
}

/** @param {boolean} busy @returns {void} */
function setBtBusy(busy) {
  const runBtn = $('#bt-run');
  const cancelBtn = $('#bt-cancel');
  if (runBtn) runBtn.disabled = busy;
  if (cancelBtn) cancelBtn.hidden = !busy;
}

/** @param {string} text @param {number|null} percent @returns {void} */
function setBtProgress(text, percent) {
  const p = $('#bt-progress');
  const track = $('#bt-progress-track');
  const fill = $('#bt-progress-fill');
  if (p) { p.hidden = false; p.textContent = text; }
  if (track && fill) {
    track.hidden = percent == null;
    fill.style.width = (percent == null ? 0 : Math.max(0, Math.min(100, percent))) + '%';
  }
}

/** @returns {Promise<void>} */
async function runNewBacktest() {
  if (!state.currentId) { toast('请先在「筛选工作台」选择一个筛选模板。', 'warn'); return; }
  const runBtn = $('#bt-run');
  setBtBusy(true);
  setBtProgress('正在准备运行参数…', null);
  try {
    const normalized = await btValidateLocal();
    const activeMode = document.querySelector('[data-bt-mode].is-active');
    const mode = activeMode ? activeMode.dataset.btMode : 'eligibility';
    const codesRaw = ($('#bt-codes') && $('#bt-codes').value.trim()) || '';
    const codes = codesRaw.split(/[,，;；\s]+/).filter(Boolean);
    if (mode === 'ignore' && !codes.length) throw new Error('忽略资格模式必须至少填 1 个股票代码。');
    const windowPreset = $('#bt-window-preset').value;
    let window_years = null; let backtest_start = null; let backtest_end = null;
    if (windowPreset === 'custom') {
      const start = $('#bt-start').value;
      const end = $('#bt-end').value;
      if (!start || !end) throw new Error('自定义窗口需要同时填写开始与结束日期。');
      if (start > end) throw new Error('开始日期不能晚于结束日期。');
      backtest_start = start;
      backtest_end = end;
    } else {
      window_years = { y1: 1, y3: 3, y5: 5, y8: 8 }[windowPreset] || 5;
    }
    const readNum = (id, fallback) => {
      const input = document.getElementById(id);
      return input ? Math.max(1, Math.floor(Number(input.value) || fallback)) : fallback;
    };
    const readDec = (id) => {
      const input = document.getElementById(id);
      const value = input ? input.value.trim() : '';
      return value === '' ? null : value;
    };
    const templateSelect = $('#bt-screening-template');
    const selectedTemplate = state.templates.find((t) => t.template_id === templateSelect.value)
      || state.templates.find((t) => t.is_system) || state.templates[0];
    if (!selectedTemplate) throw new Error('没有可用的筛选模板。');
    const body = {
      template_id: selectedTemplate.template_id,
      template_revision: selectedTemplate.revision,
      policies: normalized,
      window_years,
      backtest_start,
      backtest_end,
      initial_cash: ($('#bt-cash').value.trim() || '1000000'),
      max_positions: readNum('bt-positions', 20),
      max_workers: Math.min(16, readNum('bt-workers', 2)),
      codes: codes.join(','),
      ignore_eligibility: mode === 'ignore',
      commission_rate: readDec('bt-fee-commission'),
      stamp_duty_rate: readDec('bt-fee-stamp'),
      transfer_fee_rate: readDec('bt-fee-transfer'),
      min_commission: readDec('bt-fee-min'),
      lot_size: readNum('bt-fee-lot', 100),
    };
    if (btCurrentStrategy && !btCurrentStrategy.is_system) {
      body.strategy_template_id = btCurrentStrategy.strategy_template_id;
      body.strategy_template_revision = btCurrentStrategy.revision;
    } else {
      body.strategy_template_id = 'default-backtest-v1';
      body.strategy_template_revision = 1;
    }
    const data = await api('POST', '/api/research/backtests', body);
    setBtRun(data.run_id);
    setBtProgress('任务已提交，等待运行…', 0);
    btPollTimer = setInterval(() => { pollNewBacktest().catch(() => {}); }, 800);
  } catch (error) {
    setBtProgress('启动失败：' + (error && error.message ? error.message : String(error)), null);
    setBtBusy(false);
  }
}

/** @returns {Promise<void>} */
async function pollNewBacktest() {
  const runId = btRunId;
  if (!runId) return;
  try {
    const payload = await api('GET', '/api/research/backtests/' + runId);
    const done = Number(payload.progress_completed) || 0;
    const total = Number(payload.progress_total) || 0;
    const percent = total > 0 ? Math.round((done / total) * 100) : null;
    setBtProgress('任务状态：' + payload.status + (total > 0 ? '（' + done + '/' + total + '）' : ''), percent);
    if (payload.status === 'SUCCEEDED') {
      // 先停轮询并保留 runId,再渲染结果(不能把活动 run 一起清空)
      if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
      btRunId = null;
      btActiveRun = runId;
      setBtProgress('回测完成，正在汇总结果…', 100);
      await renderBtResult(runId);
      loadBacktestHistory();
      return;
    }
    if (payload.status === 'FAILED') {
      if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
      btRunId = null;
      setBtProgress('回测失败：' + (payload.error_message || '未知错误'), null);
      setBtBusy(false);
      return;
    }
    if (payload.status === 'CANCELLED') {
      if (btPollTimer) { clearInterval(btPollTimer); btPollTimer = null; }
      btRunId = null;
      setBtProgress('任务已取消。', null);
      setBtBusy(false);
    }
  } catch (error) {
    setBtProgress('状态查询失败：' + (error && error.message ? error.message : String(error)), null);
  }
}

/**
 * 展示一次运行的结果(供运行结束与历史回看共用)。
 * @param {string} runId @returns {Promise<void>}
 */
async function renderBtResult(runId) {
  const empty = $('#bt-result-empty');
  const metricsBox = $('#bt-result-metrics');
  const equityBox = $('#bt-equity-chart');
  const ordersHead = $('#bt-orders-head');
  const ordersBox = $('#bt-orders');
  const moreBox = $('#bt-orders-more');
  const warnings = $('#bt-warnings');
  if (empty) empty.hidden = true;
  try {
    const status = await api('GET', '/api/research/backtests/' + runId);
    if (status.status !== 'SUCCEEDED' || !status.metrics) {
      toast('该运行没有可用的回测结果。', 'warn');
      return;
    }
    btResultPayload = status;
    btActiveRun = runId;
    const metrics = status.metrics || {};
    const settings = status.settings || {};
    const subtitle = $('#bt-result-subtitle');
    if (subtitle) {
      const modeLabel = settings.mode === 'ignore_eligibility' ? '忽略资格' : '套用资格';
      subtitle.textContent = runId + ' · ' + modeLabel;
    }
    const fmt = (value) => {
      if (value == null) return '—';
      const n = Number(value);
      return Number.isNaN(n) ? value : n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
    };
    const pct = (value) => {
      if (value == null) return '—';
      const n = Number(value);
      return Number.isNaN(n) ? value : (n * 100).toFixed(2) + '%';
    };
    const metricCards = [
      ['初始资金', fmt(metrics.initial_cash)],
      ['期末资产', fmt(metrics.final_value)],
      ['总收益率', pct(metrics.total_return)],
      ['年化收益率', pct(metrics.annualized_return)],
      ['最大回撤', pct(metrics.max_drawdown)],
      ['夏普比率', fmt(metrics.sharpe)],
      ['交易次数', metrics.trade_count == null ? '—' : fmt(metrics.trade_count)],
      ['累计费用', fmt(metrics.total_fees)],
    ];
    metricsBox.innerHTML = metricCards.map(([label, value]) =>
      '<div class="bt-metric"><div class="bt-metric__label">' + label + '</div>'
      + '<div class="bt-metric__value">' + value + '</div></div>').join('');
    metricsBox.hidden = false;
    if (warnings) {
      const list = status.warnings || [];
      warnings.hidden = !list.length;
      warnings.textContent = list.length ? '提示：' + list.join('；') : '';
    }
    ordersBox.innerHTML = '';
    btOrdersAll = [];
    btOrdersOffset = 0;
    if (moreBox) moreBox.hidden = true;
    if (ordersHead) ordersHead.hidden = true;
    await loadBtOrders(runId, true);
    await drawBtEquity(status);
    setBtBusy(false);
    markBtDirty(btEditorDirty);
  } catch (error) {
    if (metricsBox) metricsBox.hidden = true;
    toast('读取结果失败：' + (error && error.message ? error.message : String(error)), 'err');
    setBtBusy(false);
  }
}

/**
 * @param {string} runId @param {boolean} first
 * @returns {Promise<void>}
 */
async function loadBtOrders(runId, first) {
  const box = $('#bt-orders');
  const head = $('#bt-orders-head');
  const moreBox = $('#bt-orders-more');
  if (!box) return;
  try {
    const data = await api('GET', '/api/research/backtests/' + runId + '/orders?offset=' + btOrdersOffset + '&limit=' + BT_ORDERS_PAGE);
    const rows = data.orders || [];
    if (!rows.length) {
      if (first) { box.hidden = true; if (head) head.hidden = true; if (moreBox) moreBox.hidden = true; }
      return;
    }
    if (first) box.innerHTML = '';
    btOrdersAll = btOrdersAll.concat(rows);
    box.hidden = false;
    if (head) head.hidden = false;
    rows.forEach((order, localIndex) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'bt-order-row';
      row.dataset.run = runId;
      row.dataset.code = order.code || '';
      row.dataset.orderIndex = String(btOrdersAll.length - rows.length + localIndex);
      const isBuy = order.side === 'BUY';
      const sideCls = isBuy ? 'bt-order-side-buy' : 'bt-order-side-sell';
      const sideText = isBuy ? '买' : '卖';
      const price = order.price == null ? '—' : Number(order.price).toFixed(3);
      const shares = order.shares == null ? '—' : fmtShares(Number(order.shares));
      const amount = order.amount == null ? '—' : '¥' + Number(order.amount).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
      const note = order.note || order.reason || '';
      const dateText = order.trading_day || order.date || '';
      row.innerHTML = '<span>' + dateText + '</span><span>' + (order.code || '')
        + '</span><span class="' + sideCls + '">' + sideText
        + '</span><span>' + price + '</span><span>' + shares + '</span><span title="' + esc(note) + '">' + esc(note) + '</span>';
      box.appendChild(row);
    });
    if (moreBox) {
      moreBox.hidden = btOrdersAll.length >= Number(data.count);
    }
  } catch (error) {
    toast('读取成交失败：' + (error && error.message ? error.message : String(error)), 'err');
  }
}

/** @param {number} shares @returns {string} */
function fmtShares(shares) {
  if (shares >= 10000) return (shares / 10000).toFixed(2) + '万';
  return String(shares);
}

/** @param {object} status @returns {void} 净值曲线(回测运行期的本地数据,简单折线) */
function drawBtEquity(status) {
  const canvas = $('#bt-equity-canvas');
  const box = $('#bt-equity-chart');
  if (!canvas || !box) return;
  box.hidden = false;
  const parent = canvas.parentNode;
  const width = Math.max(320, parent.clientWidth || 640);
  canvas.width = width * 2;
  canvas.height = 440;
  const ctx = canvas.getContext('2d');
  ctx.scale(2, 2);
  ctx.clearRect(0, 0, width, 220);
  ctx.fillStyle = '#fbfcfd';
  ctx.fillRect(0, 0, width, 220);
  const metrics = status.metrics || {};
  // 无逐点序列时只画起点→终点示意线
  const start = Number(metrics.initial_cash) || 1;
  const end = Number(metrics.final_value) || start;
  const up = end >= start;
  ctx.strokeStyle = up ? '#1e8449' : '#b33939';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(10, 110);
  ctx.lineTo(width - 10, 110 - Math.max(-90, Math.min(90, (end / start - 1) * 220)));
  ctx.stroke();
  ctx.fillStyle = '#5c6b7a';
  ctx.font = '12px sans-serif';
  ctx.fillText('净值 ' + start.toLocaleString('zh-CN') + ' → ' + end.toLocaleString('zh-CN'), 12, 24);
  ctx.fillText('(逐点净值曲线在历史回看中可用)', 12, 44);
}

/**
 * 历史回看:读取运行并把结果面板切到该 run。
 * @param {string} runId @returns {Promise<void>}
 */
async function viewBacktestRun(runId) {
  const empty = $('#bt-result-empty');
  if (empty) empty.hidden = true;
  await renderBtResult(runId);
  const resultPanel = $('#bt-result-panel');
  if (resultPanel) resultPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** @returns {void} */
function setBtFeeDefaults() {
  const values = {
    'bt-fee-commission': '0.0003', 'bt-fee-stamp': '0.0005',
    'bt-fee-transfer': '0.00001', 'bt-fee-min': '5', 'bt-fee-lot': '100',
  };
  Object.keys(values).forEach((id) => {
    const input = document.getElementById(id);
    if (input) input.value = values[id];
  });
  toast('费用与整手已恢复默认。', 'ok');
}

/** @returns {void} 单代码时分配/排名对单标的无意义:禁用并提示 */
function updateBtSingleCodeState() {
  const raw = ($('#bt-codes') && $('#bt-codes').value.trim()) || '';
  const single = raw.split(/[,，;；\s]+/).filter(Boolean).length === 1;
  ['ranking', 'allocation'].forEach((kind) => {
    const select = document.querySelector('#bt-singles select[data-single-kind="' + kind + '"]');
    if (select) select.disabled = single;
  });
  const note = $('#bt-single-note');
  if (note) {
    note.hidden = !single;
    note.textContent = single ? '单股模式：排名与分配政策不参与运行（由系统直接全额买入/持仓）。' : '';
  }
}

/** @param {string} mode @returns {void} */
function setBtMode(mode) {
  if (mode !== 'eligibility' && mode !== 'ignore') return;
  document.querySelectorAll('[data-bt-mode]').forEach((segment) => {
    const active = segment.dataset.btMode === mode;
    segment.classList.toggle('is-active', active);
    segment.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const hint = $('#bt-mode-hint');
  if (hint) {
    hint.textContent = mode === 'ignore'
      ? '忽略资格模式：必须至少填 1 个代码；整段窗口内标的始终可交易，买卖完全由入场/退出/止盈政策决定；排名/分配按代码数正常参与。'
      : '套用模板资格：仅当日通过所选筛选模板的股票可入场；填代码时资格与候选都收敛到该集合（1 只即单股回测）。';
  }
  updateBtSingleCodeState();
}

/** @param {string} runId @returns {void} */
function openBacktestRun(runId) {
  if (typeof viewBacktestRun === 'function') { viewBacktestRun(runId); return; }
  toast('结果回看功能仍在完善，请稍后再试。', 'warn');
}

/** @returns {Promise<void>} */
async function loadBacktestHistory() {
  const container = $('#bt-history-list');
  const status = $('#bt-history-status');
  if (!container) return;
  status.hidden = false;
  status.textContent = '正在读取历史运行…';
  try {
    const data = await api('GET', '/api/research/backtests');
    const runs = data.runs || [];
    const count = $('#bt-history-count');
    if (count) count.textContent = runs.length + ' 次';
    if (!runs.length) {
      container.innerHTML = '<p class="panel__hint">暂无历史运行。</p>';
      return;
    }
    container.innerHTML = runs.map((run) => {
      const r = run.result;
      const s = r && r.settings ? r.settings : {};
      const summary = r && r.summary ? r.summary : {};
      const modeLabel = s.mode === 'ignore_eligibility' ? '忽略资格' : '套用资格';
      const codeLabel = s.codes && s.codes.length ? s.codes.join(',') : '全池';
      const total = summary.total_return == null ? '-' : (Number(summary.total_return) * 100).toFixed(2) + '%';
      const strategyName = s.strategy_template_id ? '策略 ' + s.strategy_template_id : '';
      return '<div class="bt-history-row" data-run="' + esc(run.run_id) + '" tabindex="0" role="button" aria-label="回看运行 ' + esc(run.run_id) + '">'
        + '<div class="bt-history-row__head"><span>' + esc(run.run_id) + '</span><span class="badge ' + (run.status === 'SUCCEEDED' ? 'badge--ok' : run.status === 'FAILED' ? 'badge--err' : '') + '">' + esc(run.status) + '</span></div>'
        + '<div class="bt-history-row__meta">' + esc(strategyName || (s.template_id || '')) + ' · ' + esc(modeLabel)
        + ' · 代码 ' + esc(codeLabel) + ' · 收益 ' + esc(total)
        + (s.window_start ? ' · ' + esc(s.window_start) + ' ~ ' + esc(s.window_end) : '') + '</div></div>';
    }).join('');
  } catch (error) {
    status.textContent = '读取历史失败：' + (error && error.message ? error.message : String(error));
    return;
  }
  status.hidden = true;
}

/* 渲染门禁页的"本地数据库"选择区;canEnter=false(无已激活库)时不放行 */
function renderDbVersions(s, canEnter) {
  const box = $('#db-versions');
  const title = $('#db-select-title');
  const hint = $('#db-cutoff-hint');
  const enter = $('#gate-enter');
  if (!box) return;
  if (!canEnter || !s.active_generation || !s.active_generation.generation) {
    box.innerHTML = '';
    if (title) title.hidden = true;
    if (hint) hint.hidden = true;
    if (enter) enter.hidden = true;
    return;
  }
  if (title) title.hidden = false;
  const gen = String(s.active_generation.generation);
  const activated = s.active_generation.activated_at || '';
  box.innerHTML =
    '<label style="display:flex;gap:8px;align-items:flex-start;padding:6px 0;cursor:pointer">' +
      '<input type="radio" name="db-version" value="' + esc(gen) + '">' +
      '<span>generation：' + esc(gen) +
        (activated ? '<br><span style="opacity:.7">激活于 ' + esc(activated) + '</span>' : '') +
      '</span>' +
    '</label>';
  box.querySelector('input[name="db-version"]').addEventListener('change', () => {
    if (enter) enter.disabled = false;
  });
  if (hint) {
    hint.hidden = false;
    hint.textContent = '数据截至 ' + (s.latest_synced_trading_day || '-') +
      ' · ' + (s.stocks_count || 0) + ' 只股票 · 覆盖 ' +
      (s.coverage_start || '-') + ' ~ ' + (s.coverage_end || '-') +
      '。本地仅保留最新一代数据；未覆盖的交易日做筛选会被拒绝。';
  }
  if (enter) {
    enter.hidden = false;
    enter.disabled = true;   // 需先点选上面的版本(即使只有一项)
  }
}

/** @returns {Promise<void>} */
async function loadSyncStatus() {
  try {
    renderSyncStatus(await api('GET', '/api/sync/status'));
  } catch (err) {
    toast('数据状态更新失败：' + err.message, 'error');
  }
}

/** @param {Object<string, any>} s @returns {void} */
/** @param {object} s @returns {void} */
function renderSyncStatus(s) {
  observeMarketGeneration(s.active_generation ? s.active_generation.generation : null);
  // 记录当前进行中/待运行计划的模式(增量/Bootstrap),用于 runner 状态行文案
  state.activePlanMode = null;
  for (const p of (s.p5_plans || [])) {
    if (p.status === 'RUNNING' || p.status === 'PLANNED') { state.activePlanMode = String(p.mode || ''); break; }
  }
  const title = $('#sync-status-title');
  title.hidden = false;
  title.textContent = s.latest_synced_trading_day
    ? '数据状态：最近同步 ' + s.latest_synced_trading_day +
      ' · 覆盖 ' + (s.coverage_start || '-') + ' ~ ' + (s.coverage_end || '-') +
      ' · ' + (s.stocks_count || 0) + ' 只'
    : '数据状态：尚未同步本地数据';
  // 最近 30 日:每格一个色块
  const daily = $('#sync-status-daily');
  daily.hidden = false;
  daily.innerHTML = (s.recent_days || []).map((d) => {
    const color = STATUS_COLORS[d.status] || '#95a5a6';
    return '<span title="' + esc(d.day + ' ' + d.status) + '" style="display:inline-block;width:12px;height:18px;margin:1px;background:' + color + '"></span>';
  }).join('');
  $('#sync-status-daily-legend').hidden = false;
  // 更早 11 段:每段一条色带(按覆盖率着色,深=覆盖高)
  const bands = $('#sync-status-bands');
  bands.hidden = false;
  bands.style.display = 'flex';
  bands.style.gap = '2px';
  bands.innerHTML = (s.older_bands || []).map((b) => {
    const pct = Math.max(0, Math.min(1, b.coverage || 0));
    // 段内含未完全同步的天 → 整段橙色,表示还需补拉。
    let color;
    if (b.incomplete) {
      color = '#f39c12';
    } else if (pct === 0) {
      color = '#95a5a6';
    } else {
      const g = Math.round(150 + (pct * 105)); // 0%→浅绿灰,100%→深绿
      const r = Math.round(140 - (pct * 115));
      color = 'rgb(' + r + ',' + g + ',120)';
    }
    const note = b.incomplete ? '（部分未同步）' : '';
    const title = b.start + ' ~ ' + b.end + ' 覆盖率 ' + Math.round(pct * 100) + '%' + note;
    return '<span title="' + esc(title) + '" style="flex:1;height:18px;background:' + color + '"></span>';
  }).join('');
  $('#sync-status-bands-legend').hidden = false;
  // 年度覆盖:每块 1 年(八年回补后按年聚合展示)
  const years = $('#sync-status-years');
  if (years && (s.year_bands || []).length) {
    years.hidden = false;
    years.style.display = 'flex';
    years.style.gap = '2px';
    years.innerHTML = (s.year_bands || []).map((b) => {
      const pct = Math.max(0, Math.min(1, b.coverage || 0));
      let color;
      if (!b.has_data) {
        color = '#ecf0f1'; // 该年无数据:浅灰
      } else if (b.incomplete) {
        color = '#f39c12'; // 含未完全同步天:橙
      } else if (pct === 0) {
        color = '#95a5a6'; // 有数据但无完整天:灰
      } else {
        const g = Math.round(150 + (pct * 105));
        const rr = Math.round(140 - (pct * 115));
        color = 'rgb(' + rr + ',' + g + ',120)';
      }
      const note = b.incomplete ? '（部分未同步）' : '';
      const title = b.year + ' 年 · ' + (b.trading_days || 0) + ' 个交易日 · 覆盖率 '
        + Math.round(pct * 100) + '%' + note;
      return '<span title="' + esc(title) + '" style="flex:1;height:18px;background:' + color + '"></span>';
    }).join('');
    $('#sync-status-years-legend').hidden = false;
  }

  // 门禁页/工作台/回测系统三视图切换:显式视图互斥;'auto'(首次启动)按就绪态路由。
  const gate = $('#gate-view');
  const workbench = $('#workbench-view');
  const backtest = $('#backtest-view');
  const readiness = s.readiness || {};
  const ready = readiness.status === 'READY';
  const canEnter = !!(s.can_enter && s.active_generation);
  // 主 UI 顶栏:当前库与数据截止日
  const wbGen = $('#wb-active-gen');
  if (wbGen) {
    wbGen.textContent = s.latest_synced_trading_day
      ? '当前库：' + esc((s.active_generation && s.active_generation.generation) || '-') +
        ' · 数据截至 ' + s.latest_synced_trading_day
      : '';
  }
  if (state.uiView === 'workbench') {
    if (gate) gate.hidden = true;
    if (workbench) workbench.hidden = false;
    if (backtest) backtest.hidden = true;
  } else if (state.uiView === 'gate') {
    if (gate) gate.hidden = false;
    if (workbench) workbench.hidden = true;
    if (backtest) backtest.hidden = true;
  } else if (state.uiView === 'backtest') {
    if (gate) gate.hidden = true;
    if (workbench) workbench.hidden = true;
    if (backtest) backtest.hidden = false;
  } else if (gate && workbench) {
    // auto:首次启动/刷新后尚未手动选视图,按数据就绪度决定入口
    gate.hidden = ready;
    workbench.hidden = !ready;
    if (backtest) backtest.hidden = true;
  }
  const btHint = $('#bt-top-hint');
  if (btHint) {
    if (state.uiView === 'backtest') {
      btHint.hidden = ready;
      btHint.textContent = ready
        ? ''
        : '数据正在回补/同步中（回测依赖历史数据完整），可先前往「数据同步」查看进度。';
    } else {
      btHint.hidden = true;
    }
  }
  if (gate && !ready) {
    const reason = readiness.reason || '数据未就绪';
    $('#gate-title').textContent = s.latest_synced_trading_day
      ? '数据部分就绪：最近同步 ' + s.latest_synced_trading_day
      : '首次启动：本地尚无数据';
    $('#gate-readiness').textContent = '当前状态：' + (readiness.status || 'UNKNOWN') + ' — ' + reason;
    const daily = $('#gate-status-daily');
    daily.innerHTML = (s.recent_days || []).map((d) => {
      const color = STATUS_COLORS[d.status] || '#95a5a6';
      return '<span title="' + esc(d.day + ' ' + d.status) + '" style="display:inline-block;width:12px;height:18px;margin:1px;background:' + color + '"></span>';
    }).join('');
    $('#gate-status-title').hidden = false;
    $('#gate-status-title').textContent = '本地覆盖近况（绿=完整 橙=未完全同步 灰=缺失）';
  } else if (gate && state.uiView === 'gate') {
    $('#gate-title').textContent = s.latest_synced_trading_day
      ? '数据已就绪：最近同步 ' + s.latest_synced_trading_day
      : '数据已就绪';
    $('#gate-readiness').textContent = '';
  }
  renderDbVersions(s, canEnter);
}

function toggleSeedField() {
  const source = $('#bootstrap-source').value;
  const field = $('#bootstrap-seed-field');
  if (field) field.hidden = source !== 'seed';
}

async function startBootstrap() {
  const source = $('#bootstrap-source').value;
  const adjustment = $('#bootstrap-adjustment').value;
  const seedPath = $('#bootstrap-seed-path').value.trim();
  const status = $('#bootstrap-status');
  status.hidden = false;
  status.textContent = source === 'incremental'
    ? '正在增量同步…（仅补齐尾部交易日）'
    : '正在初始化…（联网模式可能耗时较长,请勿关闭页面）';
  // 防重复启动:点击后立即禁用,直到轮询确认回补不再运行。
  setBootstrapButtonsDisabled(true, '回补进行中…');
  try {
    const body = { source: source, adjustment: adjustment };
    if (source === 'seed') {
      if (!seedPath) { throw new Error('请填写种子文件路径'); }
      body.seed_path = seedPath;
    }
    const r = await api('POST', '/api/sync/bootstrap', body);
    status.textContent = '已启动：' + JSON.stringify(r);
    if (source === 'seed') {
      // seed 是同步导入,无后台 runner,完成后直接恢复按钮。
      setBootstrapButtonsDisabled(false);
    } else {
      // 独立 runner 进程在后台跑:保持禁用,轮询到空闲自动恢复;
      // 杀掉 runner 进程即停止,计划 30s 内重置为 PLANNED 后按钮恢复。
      status.textContent += '（进度见下方;杀掉 runner 进程即停止）';
      setBootstrapButtonsDisabled(true, '回补进行中…');
    }
    await loadSyncStatus();
    loadInstances();  // 刷新数据页 runner 状态行
  } catch (err) {
    status.textContent = '失败：' + (err.message || err);
    // 失败(含 409 已在运行):恢复按钮,由轮询按真实运行状态接管。
    setBootstrapButtonsDisabled(false);
  }
}

/** @returns {Promise<void>} */
async function syncCapmReferenceData() {
  const button = $('#capm-reference-sync');
  const status = $('#capm-reference-status');
  button.disabled = true;
  status.hidden = false;
  status.textContent = '正在启动 CAPM 回补 / 同步（后端按交易日历和截止时间确定范围）…';
  try {
    const result = await api('POST', '/api/sync/capm-reference', {});
    status.textContent = result.note || 'CAPM 后台任务已启动';
    await pollCapmProgress();
  } catch (error) {
    status.textContent = '同步失败：' + error.message;
    toast('指数同步失败：' + error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

let capmPollBusy = false;
let capmCoverageKey = null;
let capmCoveragePartial = false;

/** @param {object} item @returns {HTMLElement} */
function capmIndexCoverageRow(item) {
  const missing = item.missing_dates || [];
  const hasGaps = item.coverage_status === 'PARTIAL' || item.coverage_status === 'UNAVAILABLE' ||
    item.missing_count > 0 || (item.unavailable_ranges || []).length > 0;
  const row = document.createElement(hasGaps ? 'details' : 'p');
  const label = item.name + ' · ' + item.provider_code + ' → ' + item.index_id +
    ' · ' + item.return_version + ' · ' + (item.coverage_start || '缺失') + ' ~ ' +
    (item.coverage_end || '缺失') + ' · ' + item.bar_count + ' 条' +
    (item.expected_count != null ? ' / 应有 ' + item.expected_count + ' 条' : '');
  if (!hasGaps) { row.textContent = label; return row; }
  row.classList.add('sync-warning');
  row.classList.add('capm-gap-detail');
  const summary = document.createElement('summary');
  const ratio = Number(item.coverage_ratio);
  summary.textContent = label + ' · 缺 ' + (item.missing_count || missing.length) + ' 个交易日' +
    (Number.isFinite(ratio) ? ' · 覆盖 ' + (ratio * 100).toFixed(1) + '%' : '') + '（展开详情）';
  const detail = document.createElement('p');
  detail.textContent = '已发布，但此指数历史不完整；缺失日期不补零、不填充，受影响的 CAPM 窗口不可计算。' +
    ((item.unavailable_ranges || []).length ? ' 本次来源返回缺口范围：' +
      item.unavailable_ranges.map((range) => range.join(' ~ ')).join('，') + '。' : '') +
    (missing.length ? ' 全部缺失交易日（' + missing.length + ' 个）：' + missing.join('，') : '');
  row.append(summary, detail);
  return row;
}

/** @param {object} coverage @returns {void} */
function renderCapmCoverage(coverage) {
  capmCoveragePartial = ['PARTIAL', 'UNAVAILABLE'].includes(coverage.coverage_status) || coverage.gap_index_count > 0;
  const summary = $('#capm-coverage');
  summary.textContent = '已发布：' + coverage.index_count + ' 个指数 · ' +
    coverage.bar_count + ' 条日线 · ' + coverage.rate_count + ' 条利率事件' +
    (coverage.generation ? ' · generation ' + coverage.generation : ' · 暂无可用版本') +
    (capmCoveragePartial ? ' · 历史不完整：' + coverage.gap_index_count + ' 个指数存在缺口，共缺 ' +
      coverage.missing_count + ' 条指数交易日记录（非去重天数）' : '');
  summary.classList.toggle('sync-warning', capmCoveragePartial);
  $('#capm-index-coverage').replaceChildren(...coverage.indexes.map(capmIndexCoverageRow));
}

/** @returns {Promise<void>} */
async function pollCapmProgress() {
  if (capmPollBusy) return;
  capmPollBusy = true;
  const status = $('#capm-reference-status');
  try {
    const p = await api('GET', '/api/sync/pipeline/progress?dataset_id=capm');
    refreshCapmGeneration(p.generation ? p.generation.generation : null);
    const runner = p.runner || {};
    const running = ['STARTING', 'RUNNING'].includes(runner.status) || p.status === 'RUNNING';
    $('#capm-reference-sync').disabled = running;
    const state = runner.status === 'FAILED' ? 'FAILED' : (running ? 'RUNNING' :
      (runner.status === 'INTERRUPTED' ? 'PLANNED' : p.status));
    status.textContent = ({none: '尚未同步', RUNNING: '正在同步 / 等待共享 Provider 通道',
      SUCCEEDED: '同步验收通过并已发布', FAILED: '同步失败，点击按钮显式重试',
      PLANNED: '同步已中断，点击按钮继续'}[state] || state) +
      (p.target_end ? ' · ' + p.target_start + ' ~ ' + p.target_end : '');
    const errors = (p.errors || []).slice(0, 4);
    if (runner.status === 'FAILED' && runner.message) errors.unshift(runner.message);
    if (errors.length) status.textContent += ' · ' + errors.join('；');
    if (state === 'SUCCEEDED' && runner.message) status.textContent += ' · ' + runner.message;
    status.classList.toggle('sync-error', state === 'FAILED');
    $('#capm-progress-track').hidden = !p.total_tasks;
    $('#capm-progress-fill').style.width = Math.round((p.progress || 0) * 100) + '%';
    const batch = p.batch || {};
    $('#capm-progress-current').textContent = p.total_tasks ?
      '任务 ' + p.completed_tasks + '/' + p.total_tasks + (batch.data_type ? ' · ' +
      batch.data_type + ' · ' + (batch.current_code || '') : '') : (runner.message || '');
    const key = [p.plan_id, p.status, p.generation && p.generation.generation].join(':');
    if (key !== capmCoverageKey) {
      const coverage = await api('GET', '/api/sync/capm/status');
      renderCapmCoverage(coverage);
      capmCoverageKey = key;
    }
    const warnings = p.warnings || [];
    const warning = $('#capm-reference-warnings');
    warning.hidden = warnings.length === 0;
    warning.textContent = warnings.slice(0, 4).join('\n') + (warnings.length > 4 ?
      '\n另有 ' + (warnings.length - 4) + ' 项缺口，详见下方「指数传递与本地覆盖」。' : '');
    const publishedWithGaps = state === 'SUCCEEDED' && (capmCoveragePartial || warnings.length > 0);
    if (publishedWithGaps) status.textContent += ' · 历史不完整';
    status.classList.toggle('sync-warning', publishedWithGaps);
  } catch (error) {
    status.textContent = 'CAPM 状态读取失败：' + error.message;
  } finally {
    capmPollBusy = false;
  }
}

// 数据页保留唯一的股票池同步入口。
function setBootstrapButtonsDisabled(disabled, label) {
  document.querySelectorAll('#bootstrap-start').forEach((btn) => {
    btn.disabled = disabled;
    const span = btn.querySelector('.btn__label');
    if (!span) return;
    if (label) {
      span.textContent = label;
    } else {
      span.textContent = btn.dataset.origLabel || span.textContent;
    }
  });
}

async function loadVersion() {
  try {
    const v = await api('GET', '/api/version');
    const el = $('#version');
    if (v && v.version) {
      el.textContent = 'v' + v.version;
      el.className = 'badge badge--muted';
    }
  } catch (e) { /* ignore */ }
}

async function loadInstances() {
  try {
    const data = await api('GET', '/api/instances');
    const list = data.instances || [];
    // 区分主进程(本服务 web)与下载进程(回补/增量 runner)
    const isDownload = (i) => String(i.command || '').indexOf('run_backfill') !== -1;
    const mainProcs = list.filter((i) => !isDownload(i));
    const downProcs = list.filter(isDownload);
    const modeLabel = state.activePlanMode === 'INCREMENTAL' ? '增量'
      : state.activePlanMode === 'BOOTSTRAP' ? '回补'
      : state.activePlanMode === 'PLANNED' ? '待运行'
      : '数据';

    // 数据 UI(门禁页):下载 runner 状态行 + 停止按钮
    const runnerEl = $('#gate-runner-status');
    if (runnerEl) {
      if (downProcs.length) {
        runnerEl.hidden = false;
        runnerEl.innerHTML = downProcs.map((runner) =>
          (String(runner.command).includes('--dataset capm') ? 'CAPM' : '股票池') +
          '进程 · pid ' + runner.pid + ' <button class="btn btn--danger" data-runner-pid="' +
          runner.pid + '" type="button">停止同步</button>').join('<br>');
        runnerEl.querySelectorAll('button[data-runner-pid]').forEach((button) =>
          button.addEventListener('click', () => killInstance(Number(button.dataset.runnerPid))));
      } else {
        runnerEl.hidden = true;
        runnerEl.innerHTML = '';
      }
    }

    // 主 UI 底部「进程管理」
    const procMain = $('#proc-main');
    const procDownload = $('#proc-download');
    const note = $('#instances-note');
    if (procMain) {
      if (mainProcs.length) {
        procMain.hidden = false;
        procMain.innerHTML = '<b>主进程</b>' + mainProcs.map((i) => {
          const self = i.is_self ? '（当前服务）' : '';
          return ' · pid ' + i.pid + self + '<br><code>' + esc(i.command || '') + '</code>';
        }).join('');
      } else { procMain.hidden = true; procMain.innerHTML = ''; }
    }
    if (procDownload) {
      if (downProcs.length) {
        procDownload.hidden = false;
        procDownload.innerHTML = '<b>下载进程（' + modeLabel + '）</b>' + downProcs.map((i) => {
          return ' · pid ' + i.pid +
            ' <button class="btn btn--danger" data-dl-pid="' + i.pid + '" type="button">停止同步</button>' +
            '<br><code>' + esc(i.command || '') + '</code>';
        }).join('');
        procDownload.querySelectorAll('button[data-dl-pid]').forEach((btn) => {
          btn.addEventListener('click', () => killInstance(Number(btn.dataset.dlPid)));
        });
      } else { procDownload.hidden = true; procDownload.innerHTML = ''; }
    }
    if (note) {
      if (list.length) {
        note.hidden = false;
        note.textContent = '「停止同步」只结束下载进程；主进程由上方「停止服务」按钮结束。';
      } else { note.hidden = true; note.textContent = ''; }
    }
  } catch (e) { /* ignore */ }
}

async function killInstance(pid) {
  if (!confirm('确定停止该实例（pid ' + pid + '）？')) return;
  try {
    await api('POST', '/api/instances/kill', { pid });
    toast('已停止实例 pid ' + pid, 'success');
    setTimeout(loadInstances, 800);
  } catch (err) {
    toast('停止失败：' + err.message, 'error');
  }
}

/* ---------- results ---------- */
/** @returns {void} */
function renderResults() {
  const body = $('#result-body');
  const data = state.result;
  if (!data) {
    $('#result-revision').textContent = '';
    body.innerHTML = '<div class="empty-state"><p class="empty-state__title">尚未运行筛选</p><p class="empty-state__hint">设置运行条件后点击「运行筛选」。模板临时配置不会被自动保存。</p></div>';
    return;
  }
  $('#result-revision').textContent = data.template_id + ' · rev ' + data.template_revision + ' · ' + data.trading_day;
  const timing = data.elapsed_seconds != null
    ? ' · 用时 ' + (Number(data.elapsed_seconds) >= 60
        ? (Number(data.elapsed_seconds) / 60).toFixed(1) + ' 分钟'
        : Number(data.elapsed_seconds).toFixed(2) + ' 秒')
        + ' · ' + (data.max_workers != null ? data.max_workers + ' workers' : '')
    : '';
  $('#result-revision').textContent += timing;
  if (state.screenGenerationStale) $('#result-revision').textContent += ' · 股票数据版本已更新，请重新筛选';
  const filtered = data.results.filter((r) => {
    if (state.resultFilter === 'passed') return r.passed;
    if (state.resultFilter === 'failed') return !r.passed;
    return true;
  });
  const html = [
    '<div class="results-summary">',
    '<div class="stat stat--total"><div class="stat__value">' + data.summary.total + '</div><div class="stat__label">总数</div></div>',
    '<div class="stat stat--pass"><div class="stat__value">' + data.summary.passed + '</div><div class="stat__label">通过</div></div>',
    '<div class="stat stat--fail"><div class="stat__value">' + data.summary.failed + '</div><div class="stat__label">失败</div></div>',
    '</div>',
    '<div class="result-filter">',
    ['all', 'passed', 'failed'].map((f) => {
      const label = { all: '全部', passed: '通过', failed: '失败' }[f];
      return '<button class="segment' + (state.resultFilter === f ? ' is-active' : '') + '" data-filter="' + f + '">' + label + '</button>';
    }).join(''),
    '</div>',
    '<table class="table"><thead><tr><th>状态</th><th>代码</th><th>名称</th><th>交易日</th></tr></thead><tbody>',
    filtered.length ? '' : '<tr><td colspan="4" class="empty-state__hint">没有符合筛选状态的结果。</td></tr>',
  ];
  for (const r of filtered) {
    const pill = r.passed ? 'PASSED' : 'FAILED';
    html.push('<tr class="clickable' + (state.selectedCode === r.code ? ' is-selected' : '') + '" data-code="' + esc(r.code) + '" data-pill="' + pill + '" tabindex="0" aria-label="查看 ' + esc(r.code + ' ' + r.name) + ' 的分析"><td><span class="status-pill status-pill--' + pill + '">' + (r.passed ? '通过' : '未通过') + '</span></td><td>' + esc(r.code) + '</td><td>' + esc(r.name) + '</td><td>' + esc(r.trading_day) + '</td></tr>');
  }
  html.push('</tbody></table>');
  body.innerHTML = html.join('');
}

/** @param {unknown} value @returns {string} */
function formatDetailValue(value) {
  return value == null ? '—' : (typeof value === 'object' ? JSON.stringify(value) : String(value));
}

/** @returns {object|null} */
function selectedStock() {
  return state.result && state.result.results.find((item) => item.code === state.selectedCode) || null;
}

/** @returns {void} */
function renderStockDetail() {
  const r = state.screenGenerationStale ? null : selectedStock();
  const analysis = $('#stock-detail-analysis');
  if (!analysis) return;
  $('#stock-detail-empty').hidden = !!r;
  analysis.hidden = !r;
  $('#stock-detail-title').textContent = r ? r.code + ' · ' + r.name : '个股研究';
  $('#stock-detail-subtitle').textContent = state.screenGenerationStale ? '股票数据版本已更新，请重新筛选后查看个股分析。' :
    (r ? (r.passed ? '筛选通过' : '筛选未通过') + ' · ' + (r.trading_day || state.result.trading_day) +
      ' · ' + (state.result.adjustment || 'qfq').toUpperCase() : '从筛选结果中选择一只股票');
  $('#stock-detail-empty').textContent = state.screenGenerationStale ? '旧筛选结果不与新股票数据混用。请重新运行筛选。' : '请从筛选结果中选择一只股票，查看条件、K 线与 CAPM。';
  if (!r) return;
  const executions = r.rule_executions || [];
  const skippedRules = executions.filter((ex) => ex.status === 'SKIPPED' || !ex.result);
  const executedRules = executions.filter((ex) => ex.status !== 'SKIPPED' && ex.result)
    .sort((a, b) => Number(b.result.passed) - Number(a.result.passed));
  /** @param {object} ex @returns {string} */
  const renderRule = (ex) => {
    const skipped = ex.status === 'SKIPPED' || !ex.result;
    const st = skipped ? 'SKIPPED' : (ex.result.passed ? 'PASSED' : 'FAILED');
    const label = { PASSED: '通过', FAILED: '未通过', SKIPPED: '未参与' }[st];
    const name = (state.rules.find((x) => x.rule_id === ex.rule_id) || {}).name || ex.rule_id;
    let vals = '';
    if (ex.result) {
      vals = '<div class="rule-detail__vals"><span>实际值 ' + esc(formatDetailValue(ex.result.actual_value)) + '</span><span>阈值 ' + esc(formatDetailValue(ex.result.threshold)) + '</span></div>';
    }
    return '<div class="rule-detail"><div class="rule-detail__head"><span class="status-pill status-pill--' + st + '">' + label + '</span><span class="rule-detail__name">' + esc(name) + '</span></div><p class="rule-detail__reason">' + esc(ex.result ? ex.result.reason : '规则未参与本次计算') + '</p>' + vals + '</div>';
  };
  $('#stock-rules').innerHTML = executedRules.map(renderRule).join('') +
    (skippedRules.length ? '<details class="stock-rule-skipped"><summary>未参与的规则（' + skippedRules.length + '）</summary>' + skippedRules.map(renderRule).join('') + '</details>' : '') ||
    '<p class="panel__hint">本次没有逐规则明细。</p>';
  renderCapmResults();
}

/** @param {number|string|null} value @returns {string} */
function fmtPct(value) {
  if (value == null) return '—';
  return (Number(value) * 100).toFixed(3) + '%';
}

/** @param {object} r @returns {string} */
function buildCapmPanel(r) {
  const item = state.capmByCode.get(capmCacheKey(r.code));
  if (!r.passed) return '<div class="capm-panel capm-panel--muted">CAPM 仅对本次筛选通过的股票按需计算。</div>';
  const unavailable = capmSettingsError();
  if (unavailable) return '<p class="panel__hint">' + esc(unavailable) + '</p>';
  const retry = '<button class="btn btn--ghost btn--sm" data-capm-code="' + esc(r.code) + '" type="button">重新计算</button>';
  if (!item) return '<div class="capm-panel">' + retry + '<span class="panel__hint">选择该股后按当前设置自动读取本地数据计算。</span></div>';
  if (item.loading) return '<div class="capm-panel"><span class="panel__hint">CAPM 计算中…</span></div>';
  if (item.error) return '<div class="capm-panel capm-panel--error">CAPM 未完成：' + esc(item.error) + retry + '</div>';
  const benchmark = state.capmOptions.benchmarks.find((entry) => entry.index_id === state.capmSettings.benchmark_id);
  const rate = state.capmOptions.rate_terms.find((entry) => entry.term === state.capmSettings.rate_term);
  const labels = { READY: '可估计', INELIGIBLE: '历史不足', DATA_INCOMPLETE: '数据不完整', NOT_ESTIMABLE: '不可估计' };
  const rows = (item.results || []).map((result) => {
    const e = result.estimate;
    return '<tr><td>' + esc(result.window_days) + '日</td><td>' + esc(labels[result.status] || result.status) +
      '</td><td>' + (e ? esc(e.observation_count) : '—') + '</td><td>' + (e ? fmtPct(e.alpha_daily) : '—') +
      '</td><td>' + (e ? fmtPct(e.alpha_annualized) : '—') + '</td><td>' + (e ? Number(e.beta).toFixed(3) : '—') +
      '</td><td>' + (e ? Number(e.r_squared).toFixed(3) : '—') + '</td></tr>' +
      (result.reason ? '<tr><td colspan="7" class="capm-reason">' + esc(capmReason(result.reason)) + '</td></tr>' : '');
  }).join('');
  return '<div class="capm-panel"><div class="capm-panel__head"><strong>' + esc(benchmark.name) + ' · ' +
    esc(returnVersionLabel(benchmark.return_version)) + '</strong>' + retry + '</div><p class="panel__hint">' +
    esc(rate.label) + ' · α年化因子 ' + esc(state.capmSettings.periods_per_year) +
    ' · 截至 ' + esc(state.result.trading_day) + '</p><div class="capm-table-scroll"><table class="table capm-table"><thead><tr><th>自然日窗口</th><th>状态</th><th>样本数</th><th>日α</th><th>年化α</th><th>β</th><th>R²</th></tr></thead><tbody>' + rows + '</tbody></table></div></div>';
}

/** @returns {void} */
function renderCapmResults() {
  const target = $('#stock-capm-results');
  const r = selectedStock();
  if (target) target.innerHTML = r ? buildCapmPanel(r) : '';
}

/** @param {string} code @param {boolean} force @returns {Promise<void>} */
async function runCapmAnalysis(code, force = false) {
  if (!state.result || state.selectedCode !== code || state.screenPending) return;
  const selected = state.result.results.find((item) => item.code === code);
  if (!selected || !selected.passed) return;
  if (capmSettingsError()) { renderCapmResults(); return; }
  const key = capmCacheKey(code);
  const existing = state.capmByCode.get(key);
  if (existing && (existing.loading || !force)) { renderCapmResults(); return; }
  const request = ++_capmRequest;
  const epoch = state.screenEpoch;
  const settings = { ...state.capmSettings };
  const asOf = state.result.trading_day;
  state.capmByCode.set(key, { loading: true, request });
  renderCapmResults();
  /** @returns {boolean} */
  const current = () => state.screenEpoch === epoch && state.selectedCode === code &&
    key === capmCacheKey(code) && state.capmByCode.get(key)?.request === request;
  try {
    const data = await api('POST', '/api/capm/analyses', {
      stock_code: code, as_of: asOf, ...settings, windows: [30, 120, 250, 500],
    });
    if (!current()) return;
    state.capmByCode.set(key, data);
  } catch (error) {
    if (!current()) return;
    state.capmByCode.set(key, { error: error.message });
  }
  renderCapmResults();
}

/* ---------- independent detail preferences and asynchronous inputs ---------- */
/** @param {string} action @param {unknown} error @returns {void} */
function preferenceFailure(action, error) {
  _preferenceWarning = '无法' + action + '浏览器设置；本次仍可使用，刷新后可能恢复默认。' +
    (error && error.message ? ' ' + error.message : '');
  toast(_preferenceWarning, 'warn');
}

/** @param {string} key @returns {object|null} */
function readPreference(key) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('设置格式无效');
    return value;
  } catch (error) {
    preferenceFailure('读取', error);
    return null;
  }
}

/** @param {string} key @param {object} value @returns {void} */
function savePreference(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); }
  catch (error) { preferenceFailure('保存', error); }
}

/** @param {string} panelId @param {boolean} expanded @param {boolean} persist @returns {void} */
function setPanelExpanded(panelId, expanded, persist = true) {
  const panel = document.getElementById(panelId);
  const button = panel && panel.querySelector('[data-panel-toggle]');
  const content = button && document.getElementById(button.getAttribute('aria-controls'));
  if (!button || !content) return;
  button.setAttribute('aria-expanded', String(expanded));
  button.textContent = expanded ? '收起' : '展开';
  content.hidden = !expanded;
  panel.classList.toggle('panel--collapsed', !expanded);
  if (persist) {
    _panelPreferences[panelId] = expanded;
    savePreference(PANEL_PREFERENCES_KEY, _panelPreferences);
  }
  if (expanded && panelId === 'stock-detail-panel' && _klineCanvas && _klineBars) {
    window.requestAnimationFrame(() => { drawKline(_klineCanvas, _klineBars); updateKlineInfo(); });
  }
}

/** @returns {void} */
function initializePanelToggles() {
  _panelPreferences = readPreference(PANEL_PREFERENCES_KEY) || {};
  document.querySelectorAll('[data-panel-id]').forEach((panel) => {
    const button = panel.querySelector('[data-panel-toggle]');
    if (!button) return;
    setPanelExpanded(panel.id, _panelPreferences[panel.id] !== false, false);
    button.addEventListener('click', () => setPanelExpanded(panel.id, button.getAttribute('aria-expanded') !== 'true'));
  });
}

/** @returns {void} */
function initializeStockDetail() {
  const saved = readPreference(CAPM_PREFERENCES_KEY);
  if (saved) {
    state.capmSettings = {
      benchmark_id: typeof saved.benchmark_id === 'string' ? saved.benchmark_id : null,
      rate_term: typeof saved.rate_term === 'string' ? saved.rate_term : null,
      periods_per_year: Number.isSafeInteger(saved.periods_per_year) && saved.periods_per_year > 0 ? saved.periods_per_year : 252,
    };
  }
  $('#capm-periods-per-year').value = String(state.capmSettings.periods_per_year);
  for (const selector of ['#capm-benchmark', '#capm-rate-term', '#capm-periods-per-year']) {
    $(selector).addEventListener('change', changeCapmSettings);
  }
  $('#capm-options-retry').addEventListener('click', async () => {
    await loadCapmOptions(true);
    if (state.selectedCode) await runCapmAnalysis(state.selectedCode, true);
  });
  $('#stock-detail-panel').addEventListener('click', (event) => {
    const kline = event.target.closest('[data-kline-action]');
    if (kline) { handleKlineAction(kline.dataset.klineAction); return; }
    const capm = event.target.closest('[data-capm-code]');
    if (capm) runCapmAnalysis(capm.dataset.capmCode, true);
  });
  renderCapmSettings();
  renderStockDetail();
}

/** @returns {void} */
function invalidateStockAnalysis() {
  state.screenEpoch += 1;
  state.selectedCode = null;
  state.capmByCode.clear();
  state.capmOptions = null;
  state.capmOptionsLoading = false;
  state.capmOptionsError = '';
  _capmOptionsRequest += 1;
  _capmOptionsPromise = null;
  _detailIdentity = null;
  _stockSelectionRequest += 1;
  _klineRequest += 1;
  _klineBars = null;
  _klineView = null;
  _klineDrag = null;
  _klineCache.clear();
}

/** @param {string|null} generation @returns {void} */
function observeMarketGeneration(generation) {
  state.activeMarketGeneration = generation;
  if (!state.result || state.screenGenerationStale || state.screenMarketGeneration === generation) return;
  state.screenGenerationStale = true;
  invalidateStockAnalysis();
  renderResults();
  renderStockDetail();
  renderCapmSettings();
}

/** @returns {Promise<string|null>} */
async function readMarketGeneration() {
  const status = await api('GET', '/api/sync/pipeline/progress?dataset_id=market');
  const generation = status.generation ? status.generation.generation : null;
  observeMarketGeneration(generation);
  return generation;
}

/** @param {object} data @param {string|null} generation @returns {void} */
function acceptScreenResult(data, generation) {
  state.result = data;
  state.resultFilter = 'all';
  state.selectedCode = null;
  state.capmByCode.clear();
  state.screenMarketGeneration = generation;
  state.screenGenerationStale = !generation || state.activeMarketGeneration !== generation;
}

/** @param {string} code @returns {Promise<void>} */
async function selectStock(code) {
  if (state.screenPending || state.screenGenerationStale || !state.result) return;
  const r = state.result.results.find((item) => item.code === code);
  if (!r) return;
  const epoch = state.screenEpoch;
  const selection = ++_stockSelectionRequest;
  try { await readMarketGeneration(); }
  catch (error) {
    if (selection === _stockSelectionRequest) toast('无法确认本地股票版本：' + error.message, 'error');
    return;
  }
  if (selection !== _stockSelectionRequest || state.screenEpoch !== epoch || state.screenGenerationStale) return;
  if (state.selectedCode !== code) {
    // An abandoned response is ignored, so its loading marker must not block a later visit.
    for (const [key, value] of state.capmByCode) {
      if (value.loading) state.capmByCode.delete(key);
    }
  }
  state.selectedCode = code;
  renderResults();
  renderStockDetail();
  setPanelExpanded('stock-detail-panel', true);
  const panel = $('#stock-detail-panel');
  if (panel) panel.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  const identity = [state.screenEpoch, code, state.result.trading_day, state.result.adjustment].join('|');
  if (_detailIdentity !== identity) {
    _detailIdentity = identity;
    loadBars(r);
  }
  await loadCapmOptions();
  if (state.screenEpoch !== epoch || state.selectedCode !== code) return;
  await runCapmAnalysis(code);
}

/** @param {string} version @returns {string} */
function returnVersionLabel(version) {
  return { price: '价格指数', gross_total_return: '全收益指数', net_total_return: '净收益指数' }[version] || version;
}

/** @param {string} reason @returns {string} */
function capmReason(reason) {
  return {
    'stock has less than 180 natural days of qfq history': '股票前复权有效历史不足 180 个自然日，不能估计 CAPM。',
    'insufficient effective return observations': '该窗口有效收益观察不足 15 条，不能估计。',
    'market excess return has zero variance': '市场超额收益方差为零，无法估计 β。',
    'stock excess return has zero variance': '股票超额收益方差为零，无法定义 R²。',
    'returns must be finite': '收益中包含非法数值，不能估计。',
    'market and stock return counts differ': '股票与指数的有效收益观察数不一致。',
  }[reason] || reason;
}

/** @returns {string} */
function capmSettingsError() {
  if (state.screenGenerationStale) return '股票数据版本已更新，请重新筛选；旧结果不与新数据混用。';
  if (state.capmOptionsLoading) return '正在读取本地已发布的指数与利率…';
  if (state.capmOptionsError) return '本地参数读取失败：' + state.capmOptionsError;
  const data = state.capmOptions;
  if (!data) return '选择通过的股票后读取本地 CAPM 参数。';
  if (!data.generation_id) return '暂无已发布的 CAPM 数据，请在数据页先完成 CAPM 同步。';
  if (!data.benchmarks.some((item) => item.index_id === state.capmSettings.benchmark_id)) return '请选择已发布的基准指数；原选择不可用时不会自动替换。';
  const rate = data.rate_terms.find((item) => item.term === state.capmSettings.rate_term);
  if (!rate || rate.annual_rate == null || !rate.effective_on) return '请选择在筛选日期已有有效记录的利率期限；不会替代或填零。';
  if (!Number.isSafeInteger(state.capmSettings.periods_per_year) || state.capmSettings.periods_per_year <= 0) return 'α 年化因子必须是正整数。';
  return '';
}

/** @param {HTMLSelectElement} select @param {Array<object>} values @param {string|null} selected @param {(item: object) => string} identity @param {(item: object) => string} label @returns {void} */
function renderCapmSelect(select, values, selected, identity, label) {
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '请选择本地可用项';
  const choices = values.map((item) => {
    const option = document.createElement('option');
    option.value = identity(item);
    option.textContent = label(item);
    option.disabled = Object.hasOwn(item, 'annual_rate') && (item.annual_rate == null || !item.effective_on);
    return option;
  });
  select.replaceChildren(placeholder, ...choices);
  select.value = choices.some((item) => item.value === selected && !item.disabled) ? selected : '';
  select.disabled = state.capmOptionsLoading || !state.capmOptions?.generation_id || !choices.some((item) => !item.disabled);
}

/** @returns {void} */
function renderCapmSettings() {
  if (!$('#capm-options-status')) return;
  const data = state.capmOptions;
  renderCapmSelect($('#capm-benchmark'), data ? data.benchmarks : [], state.capmSettings.benchmark_id,
    (item) => item.index_id, (item) => item.name + ' · ' + returnVersionLabel(item.return_version) + ' · ' + item.provider_code +
      (item.coverage_status === 'PARTIAL' ? ' · 历史不完整' : (item.coverage_status === 'UNAVAILABLE' ? ' · 无行情' : '')));
  renderCapmSelect($('#capm-rate-term'), data ? data.rate_terms : [], state.capmSettings.rate_term,
    (item) => item.term, (item) => item.label + (item.annual_rate == null ? ' · 该日期尚未生效' : ' · ' + fmtPct(item.annual_rate)));
  $('#capm-periods-per-year').value = String(state.capmSettings.periods_per_year);
  const rate = data && data.rate_terms.find((item) => item.term === state.capmSettings.rate_term);
  $('#capm-rate-info').textContent = rate && rate.annual_rate != null ?
    '截至 ' + data.as_of + ' 生效的年利率 ' + fmtPct(rate.annual_rate) + ' · 生效于 ' + rate.effective_on +
      ' · 来源 ' + rate.source + '；历史区间按当时有效利率分段计息。' : '利率仅使用本地已发布、在分析日期生效的记录。';
  const benchmark = data && data.benchmarks.find((item) => item.index_id === state.capmSettings.benchmark_id);
  const coverageWarning = benchmark && ['PARTIAL', 'UNAVAILABLE'].includes(benchmark.coverage_status) ?
    ' 所选指数' + (benchmark.coverage_status === 'PARTIAL' ? '历史不完整' : '暂无行情') + '；仅实际数据完整的分析区间可计算，不会补值或切换基准。' : '';
  $('#capm-options-status').textContent = (capmSettingsError() || '设置供全部模板共用，仅对所选股票本地重算。') + coverageWarning +
    (_preferenceWarning ? ' ' + _preferenceWarning : '');
  $('#capm-options-retry').disabled = state.capmOptionsLoading || !state.result;
  $('#capm-options-retry').hidden = !state.capmOptionsError && !!data?.generation_id;
}

/** @param {boolean} force @returns {Promise<object|null>} */
async function loadCapmOptions(force = false) {
  if (!state.result) { renderCapmSettings(); return null; }
  const asOf = state.result.trading_day;
  if (!force && state.capmOptions && state.capmOptions.as_of === asOf) return state.capmOptions;
  if (!force && _capmOptionsPromise) return _capmOptionsPromise;
  const request = ++_capmOptionsRequest;
  const epoch = state.screenEpoch;
  state.capmOptionsLoading = true;
  state.capmOptionsError = '';
  renderCapmSettings();
  renderCapmResults();
  _capmOptionsPromise = (async () => {
    try {
      const data = await api('GET', '/api/capm/options?as_of=' + encodeURIComponent(asOf));
      if (request !== _capmOptionsRequest || epoch !== state.screenEpoch) return null;
      if (data.as_of !== asOf || !Array.isArray(data.benchmarks) || !Array.isArray(data.rate_terms)) throw new Error('本地参数响应格式无效');
      const previousGeneration = state.capmOptions?.generation_id;
      state.capmOptions = data;
      if (previousGeneration !== data.generation_id) state.capmByCode.clear();
      // A missing default stays unavailable; never replace it with the first catalog row.
      if (state.capmSettings.benchmark_id == null) state.capmSettings.benchmark_id = data.defaults.benchmark_id;
      if (state.capmSettings.rate_term == null) state.capmSettings.rate_term = data.defaults.rate_term;
      return data;
    } catch (error) {
      if (request !== _capmOptionsRequest || epoch !== state.screenEpoch) return null;
      state.capmOptions = null;
      state.capmOptionsError = error.message;
      return null;
    } finally {
      if (request === _capmOptionsRequest && epoch === state.screenEpoch) {
        state.capmOptionsLoading = false;
        _capmOptionsPromise = null;
        renderCapmSettings();
        renderCapmResults();
      }
    }
  })();
  return _capmOptionsPromise;
}

/** @returns {Promise<void>} */
async function changeCapmSettings() {
  state.capmSettings = {
    benchmark_id: state.capmOptions ? $('#capm-benchmark').value : state.capmSettings.benchmark_id,
    rate_term: state.capmOptions ? $('#capm-rate-term').value : state.capmSettings.rate_term,
    periods_per_year: Number($('#capm-periods-per-year').value),
  };
  state.capmByCode.clear();
  const error = capmSettingsError();
  if (!error) savePreference(CAPM_PREFERENCES_KEY, state.capmSettings);
  $('#capm-options-status').textContent = error || '设置已应用；仅在本浏览器保存，全部模板共用。' +
    (_preferenceWarning ? ' ' + _preferenceWarning : '');
  // Keep an invalid numeric edit visible so the user can correct it.
  if (!error) renderCapmSettings();
  renderCapmResults();
  if (!error && state.selectedCode) await runCapmAnalysis(state.selectedCode);
}

/** @param {string} code @returns {string} */
function capmCacheKey(code) {
  const result = state.result || {};
  const settings = state.capmSettings;
  return JSON.stringify([state.screenEpoch, state.screenMarketGeneration,
    code, result.trading_day, result.adjustment, state.capmOptions?.generation_id,
    settings.benchmark_id, settings.rate_term, settings.periods_per_year]);
}

/** @param {string|null} generation @returns {Promise<void>} */
async function refreshCapmGeneration(generation) {
  if (state.screenGenerationStale || !state.result || !state.selectedCode || state.capmOptionsLoading || !state.capmOptions ||
      state.capmOptions.generation_id === generation) return;
  state.capmByCode.clear();
  await loadCapmOptions(true);
  if (state.selectedCode) await runCapmAnalysis(state.selectedCode);
}

/* ---------- local K-line chart ---------- */
function limitUpDatesOf(r) {
  // 涨幅次数规则（limit_up_3m）在 actual_value.trading_days 中返回涨幅日期。
  const ex = (r.rule_executions || []).find((x) => x.rule_id === 'limit_up_3m');
  const v = ex && ex.result && ex.result.actual_value;
  return v && Array.isArray(v.trading_days) ? v.trading_days : [];
}

/** @param {object} r @returns {Promise<void>} */
async function loadBars(r) {
  const canvas = $('#kline-canvas');
  const info = $('#kline-info');
  if (!canvas || !info) return;
  const request = ++_klineRequest;
  const epoch = state.screenEpoch;
  _klineBars = null;
  _klineView = null;
  _klineDrag = null;
  info.textContent = '正在读取本地日K…';
  drawKline(canvas, []);
  /** @returns {boolean} */
  const current = () => request === _klineRequest && epoch === state.screenEpoch && state.selectedCode === r.code;
  try {
    const adj = (state.result && state.result.adjustment) || 'qfq';
    const end = r.trading_day || (state.result && state.result.trading_day) || '';
    const key = [epoch, state.screenMarketGeneration, klineKey(r.code, adj, end)].join('|');
    let bars;
    let dataEnd;
    const cached = _klineCache.get(key);
    if (cached) {
      bars = cached.bars;
      dataEnd = cached.end;
    } else {
      const query = new URLSearchParams({ code: r.code, adjustment: adj, end: end, days: '250' });
      const data = await api('GET', '/api/bars?' + query.toString());
      if (!current()) return;
      bars = data.bars || [];
      dataEnd = data.end || end;
      _klineCache.set(key, { bars, end: dataEnd });
    }
    if (!current()) return;
    let defaultInfo = bars.length ? adj.toUpperCase() + ' · ' + dataEnd : '本地暂无日K数据';
    const limitUpDates = limitUpDatesOf(r);
    if (limitUpDates.length) {
      const shown = limitUpDates.length > 8
        ? limitUpDates.slice(0, 8).join('、') + ' 等' + limitUpDates.length + '日'
        : limitUpDates.join('、');
      defaultInfo += ' · 涨幅日 ' + shown;
    }
    _klineBars = bars;
    _klineView = null;
    _klineInfoDefault = defaultInfo;
    drawKline(canvas, bars);
    bindKlineHover(canvas);
    bindKlineWheel(canvas);
    updateKlineInfo();
  } catch (err) {
    if (!current()) return;
    _klineInfoDefault = '加载失败：' + err.message;
    const info2 = $('#kline-info');
    if (info2) info2.textContent = _klineInfoDefault;
  }
}

function klineKey(code, adjustment, end) {
  return [code, adjustment, end].join('|');
}

function currentKlineView() {
  const bars = _klineBars || [];
  return _klineView || { start: 0, end: bars.length };
}

function klineVisibleBars() {
  const bars = _klineBars || [];
  const view = currentKlineView();
  return bars.slice(view.start, view.end);
}

function updateKlineInfo() {
  const info = $('#kline-info');
  if (!info) return;
  const bars = _klineBars || [];
  const view = currentKlineView();
  const visible = bars.slice(view.start, view.end);
  if (bars.length && visible.length) {
    const first = visible[0].trading_day;
    const last = visible[visible.length - 1].trading_day;
    info.textContent = (_klineInfoDefault || '') + ' · 显示 ' + first + '~' + last + ' (' + visible.length + '/' + bars.length + ')';
  } else {
    info.textContent = _klineInfoDefault || '本地暂无日K数据';
  }
}

function setKlineView(start, end) {
  const bars = _klineBars || [];
  if (!bars.length) return;
  const total = bars.length;
  const minVisible = Math.min(5, total);
  let s = Math.max(0, Math.min(total, start));
  let e = Math.max(s + 1, Math.min(total, end));
  if (e - s < minVisible) {
    if (s > total - minVisible) {
      s = Math.max(0, total - minVisible);
    } else {
      e = Math.min(total, s + minVisible);
    }
  }
  _klineView = { start: s, end: e };
  if (_klineCanvas && _klineBars) drawKline(_klineCanvas, _klineBars);
  updateKlineInfo();
}

function zoomKline(factor, anchorRatio) {
  const bars = _klineBars;
  if (!bars || bars.length < 2) return;
  const view = currentKlineView();
  const visibleCount = view.end - view.start;
  const newCount = Math.max(5, Math.min(bars.length, Math.round(visibleCount * factor)));
  const ratio = Math.max(0, Math.min(1, anchorRatio == null ? 0.5 : anchorRatio));
  const anchorIndex = view.start + ratio * (visibleCount - 1);
  let newStart = Math.round(anchorIndex - (anchorIndex - view.start) / visibleCount * newCount);
  newStart = Math.max(0, Math.min(bars.length - newCount, newStart));
  setKlineView(newStart, newStart + newCount);
}

function panKline(direction) {
  const bars = _klineBars;
  if (!bars || !bars.length) return;
  const view = currentKlineView();
  const count = view.end - view.start;
  const step = Math.max(1, Math.round(count * 0.2));
  const delta = direction < 0 ? -step : step;
  let newStart = view.start + delta;
  newStart = Math.max(0, Math.min(bars.length - count, newStart));
  setKlineView(newStart, newStart + count);
}

function resetKlineView() {
  const bars = _klineBars || [];
  _klineView = bars.length ? { start: 0, end: bars.length } : null;
  if (_klineCanvas && _klineBars) drawKline(_klineCanvas, _klineBars);
  updateKlineInfo();
}

function handleKlineAction(action) {
  switch (action) {
    case 'zoom-in':
      zoomKline(0.75, 0.5);
      break;
    case 'zoom-out':
      zoomKline(1.4, 0.5);
      break;
    case 'pan-left':
      panKline(-1);
      break;
    case 'pan-right':
      panKline(1);
      break;
    case 'reset':
      resetKlineView();
      break;
  }
}

function bindKlineHover(canvas) {
  canvas.onmousedown = (e) => {
    if (e.button !== 0) return;
    const view = currentKlineView();
    _klineDrag = { startX: e.clientX, viewStart: view.start, viewEnd: view.end };
    canvas.style.cursor = 'grabbing';
    e.preventDefault();
  };
  canvas.onmousemove = (e) => {
    const info = $('#kline-info');
    const bars = _klineBars;
    if (!bars || !bars.length || !info) return;
    if (_klineDrag) {
      const rect = canvas.getBoundingClientRect();
      const axisW = 52;
      const view = currentKlineView();
      const count = view.end - view.start;
      const slot = Math.max(1, (rect.width - axisW - 6) / count);
      const deltaBars = Math.round((e.clientX - _klineDrag.startX) / slot);
      const newStart = Math.max(0, Math.min(bars.length - count, _klineDrag.viewStart - deltaBars));
      setKlineView(newStart, newStart + count);
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const axisW = 52;
    const view = currentKlineView();
    const visible = bars.slice(view.start, view.end);
    const n = visible.length;
    if (!n) return;
    const slot = (rect.width - axisW - 6) / n;
    const i = Math.max(0, Math.min(n - 1, Math.floor((e.clientX - rect.left - axisW) / slot)));
    const b = visible[i];
    info.textContent = String(b.trading_day) + '  开 ' + b.open + '  高 ' + b.high + '  低 ' + b.low + '  收 ' + b.close + '  量 ' + b.volume;
  };
  canvas.onmouseup = () => {
    _klineDrag = null;
    canvas.style.cursor = 'grab';
  };
  canvas.onmouseleave = () => {
    if (_klineDrag) {
      _klineDrag = null;
      canvas.style.cursor = 'grab';
    }
    updateKlineInfo();
  };
  canvas.style.cursor = 'grab';
}

function bindKlineWheel(canvas) {
  canvas.onwheel = (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const axisW = 52;
    const plotL = axisW;
    const plotR = rect.width - 6;
    const ratio = plotR > plotL ? (e.clientX - rect.left - plotL) / (plotR - plotL) : 0.5;
    const factor = e.deltaY < 0 ? 0.75 : 1.4;
    zoomKline(factor, ratio);
  };
}

function drawKline(canvas, bars) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 600;
  const cssH = canvas.clientHeight || 300;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const W = cssW;
  const H = cssH;
  _klineCanvas = canvas;
  _klineBars = bars;
  if (!bars || bars.length === 0) {
    ctx.fillStyle = '#8a8f98';
    ctx.font = '13px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('本地暂无该股票日K数据', W / 2, H / 2);
    return;
  }
  if (!_klineView || _klineView.end > bars.length) {
    _klineView = { start: 0, end: bars.length };
  }
  const view = _klineView;
  const visible = bars.slice(view.start, view.end);
  if (!visible.length) {
    _klineView = { start: 0, end: bars.length };
    return drawKline(canvas, bars);
  }

  const axisW = 52;                 // left price axis
  const topPad = 10;
  const bottomPad = 18;             // date labels
  const volH = Math.max(48, Math.round(H * 0.22));
  const gap = 8;
  const priceTop = topPad;
  const priceBottom = H - bottomPad - volH - gap;
  const volTop = H - bottomPad - volH;
  const volBottom = H - bottomPad;
  const plotL = axisW;
  const plotR = W - 6;

  let minP = Infinity;
  let maxP = -Infinity;
  let maxV = 0;
  for (const b of visible) {
    const low = Number(b.low);
    const high = Number(b.high);
    const v = Number(b.volume);
    if (low < minP) minP = low;
    if (high > maxP) maxP = high;
    if (v > maxV) maxV = v;
  }
  if (!isFinite(minP) || !isFinite(maxP)) return;
  const spread = (maxP - minP) * 0.05 || 1;
  minP -= spread;
  maxP += spread;
  const priceAt = (p) => priceTop + (maxP - p) / (maxP - minP) * (priceBottom - priceTop);

  const n = visible.length;
  const slot = (plotR - plotL) / n;
  const bodyW = Math.max(1, Math.min(slot * 0.7, 14));

  // grid + price labels
  ctx.font = '10px system-ui, sans-serif';
  ctx.textAlign = 'right';
  ctx.strokeStyle = 'rgba(120, 128, 138, 0.18)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const p = minP + (maxP - minP) * i / 4;
    const y = priceAt(p);
    ctx.beginPath();
    ctx.moveTo(plotL, y);
    ctx.lineTo(plotR, y);
    ctx.stroke();
    ctx.fillStyle = '#9aa0a8';
    ctx.fillText(p.toFixed(2), plotL - 4, y + 3);
  }

  // candles + volume (A 股习惯：红涨绿跌)
  for (let i = 0; i < n; i++) {
    const b = visible[i];
    const x = plotL + slot * i + slot / 2;
    const o = Number(b.open);
    const c = Number(b.close);
    const h = Number(b.high);
    const l = Number(b.low);
    const v = Number(b.volume);
    const up = c >= o;
    const color = up ? '#d0342c' : '#1a9c50';
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    const yO = priceAt(o);
    const yC = priceAt(c);
    ctx.beginPath();
    ctx.moveTo(x, priceAt(h));
    ctx.lineTo(x, priceAt(l));
    ctx.stroke();
    const top = Math.min(yO, yC);
    const hgt = Math.max(1, Math.abs(yO - yC));
    ctx.fillRect(x - bodyW / 2, top, bodyW, hgt);
    if (maxV > 0 && v > 0) {
      const vh = (volBottom - volTop) * (v / maxV);
      ctx.fillRect(x - bodyW / 2, volBottom - vh, bodyW, vh);
    }
  }

  // date labels
  ctx.fillStyle = '#9aa0a8';
  ctx.textAlign = 'center';
  for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
    ctx.fillText(String(visible[i].trading_day).slice(5), plotL + slot * i + slot / 2, H - 6);
  }
  ctx.textAlign = 'left';
  ctx.fillText('量', plotL + 2, volTop + 10);
}

window.addEventListener('resize', () => {
  if (_klineCanvas && _klineBars) {
    drawKline(_klineCanvas, _klineBars);
    updateKlineInfo();
  }
});

/* ---------- event wiring ---------- */
/** @returns {void} */
/* ---------- 真实净值曲线(逐点 equity 分页拉取) ---------- */
/**
 * @param {string} runId @returns {Promise<Array<object>>}
 */
async function fetchBtEquity(runId) {
  const out = [];
  let offset = 0;
  for (;;) {
    const data = await api('GET', '/api/research/backtests/' + runId + '/equity?offset=' + offset + '&limit=1000');
    const points = data.points || [];
    out.push.apply(out, points);
    if (points.length < 1000 || out.length > 20000) break;
    offset += points.length;
  }
  return out;
}

/** @param {object} status @returns {Promise<void>} 回测净值曲线(逐点) */
async function drawBtEquity(status) {
  const canvas = $('#bt-equity-canvas');
  const box = $('#bt-equity-chart');
  if (!canvas || !box) return;
  const points = await fetchBtEquity(status ? status.run_id || btActiveRun : btActiveRun);
  const parent = canvas.parentNode;
  const width = Math.max(320, parent.clientWidth || 640);
  const height = 220;
  canvas.width = width * 2;
  canvas.height = height * 2;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(2, 0, 0, 2, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#fbfcfd';
  ctx.fillRect(0, 0, width, height);
  box.hidden = false;
  if (!points.length) {
    ctx.fillStyle = '#5c6b7a';
    ctx.font = '12px sans-serif';
    ctx.fillText('(该运行无净值曲线)', 12, 24);
    return;
  }
  const pad = { top: 14, right: 12, bottom: 20, left: 58 };
  const values = points.map((p) => Number(p.equity));
  let lo = Math.min.apply(null, values);
  let hi = Math.max.apply(null, values);
  if (hi - lo < 1e-9) { hi = lo * 1.001 + 1; lo = lo * 0.999 - 1; }
  const xAt = (i) => pad.left + (width - pad.left - pad.right) * (i / Math.max(1, points.length - 1));
  const yAt = (v) => pad.top + (height - pad.top - pad.bottom) * (1 - (v - lo) / (hi - lo));
  // 参考网格与坐标
  ctx.strokeStyle = '#e6ebf0';
  ctx.fillStyle = '#5c6b7a';
  ctx.font = '10px sans-serif';
  ctx.textAlign = 'right';
  for (let g = 0; g <= 4; g += 1) {
    const v = lo + (hi - lo) * (g / 4);
    const y = yAt(v);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(v.toLocaleString('zh-CN', { maximumFractionDigits: 0 }), pad.left - 4, y + 3);
  }
  ctx.textAlign = 'left';
  // 净值线
  ctx.strokeStyle = '#1f6feb';
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = xAt(i);
    const y = yAt(Number(p.equity));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  // 首末标注
  const label = (i, extra) => {
    const p = points[i];
    ctx.fillStyle = '#8a97a5';
    ctx.fillText((p.trading_day || '') + '  ' + Number(p.equity).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + (extra || ''), xAt(i) - 30, yAt(Number(p.equity)) - 8);
  };
  label(0, ' 起点');
  if (points.length > 1) label(points.length - 1, ' 终点');
}

/* ---------- 买卖点日 K 复盘浮层(P5C D29) ---------- */
const btReplay = {
  open: false, code: '', runId: '', bars: [], markers: [],
  startIdx: 0, count: 60, loaded: false,
};
const BT_REPLAY_MAX = 40; // 一屏默认最多 K 根(可缩放)

/** @param {string} code @param {string} runId @returns {Promise<void>} */
async function openBtReplay(code, runId) {
  const overlay = $('#bt-replay-overlay');
  if (!overlay || !code) return;
  overlay.hidden = false;
  btReplay.open = true;
  btReplay.code = code;
  btReplay.runId = runId;
  btReplay.loaded = false;
  const title = $('#bt-replay-title');
  if (title) title.textContent = '买卖点日K复盘 · ' + code;
  const info = $('#bt-replay-info');
  if (info) info.textContent = '正在加载本地日K与成交…';
  try {
    const ordersAll = await fetchAllBtOrders(runId);
    const settings = btResultPayload && btResultPayload.settings ? btResultPayload.settings : {};
    const start = settings.window_start || null;
    const end = settings.window_end || null;
    let bars = [];
    if (start && end) {
      const data = await api('GET', '/api/bars?code=' + encodeURIComponent(code) + '&adjustment=qfq&start=' + start + '&end=' + end);
      bars = data.bars || [];
    }
    btReplay.bars = bars;
    btReplay.markers = ordersAll
      .filter((o) => o.code === code)
      .map((o) => ({
        day: o.trading_day || o.date || '',
        side: o.side === 'BUY' ? 'buy' : 'sell',
        price: o.price == null ? null : Number(o.price),
        shares: o.shares == null ? null : Number(o.shares),
      }));
    btReplay.startIdx = Math.max(0, bars.length - BT_REPLAY_MAX);
    btReplay.count = Math.min(bars.length, BT_REPLAY_MAX);
    if (!bars.length) {
      if (info) info.textContent = '本地无该股 ' + start + ' ~ ' + end + ' 的日K数据，无法绘制。';
      return;
    }
    if (info) info.textContent = '共 ' + bars.length + ' 根日K，' + btReplay.markers.length + ' 个成交点。';
    drawBtReplayKline();
  } catch (error) {
    if (info) info.textContent = '加载失败：' + (error && error.message ? error.message : String(error));
  }
}

/** @param {string} runId @returns {Promise<Array<object>>} 分页拉全一次运行全部成交 */
async function fetchAllBtOrders(runId) {
  const out = [];
  let offset = 0;
  for (;;) {
    const data = await api('GET', '/api/research/backtests/' + runId + '/orders?offset=' + offset + '&limit=1000');
    const rows = data.orders || [];
    out.push.apply(out, rows);
    if (rows.length < 1000 || out.length > 20000) break;
    offset += rows.length;
  }
  return out;
}

/** @returns {void} */
function closeBtReplay() {
  const overlay = $('#bt-replay-overlay');
  if (overlay) overlay.hidden = true;
  btReplay.open = false;
  btReplay.bars = [];
  btReplay.markers = [];
}

/** @param {string} action @returns {void} */
function btReplayAction(action) {
  if (!btReplay.open || !btReplay.bars.length) return;
  const total = btReplay.bars.length;
  const maxCount = Math.max(10, Math.min(400, total));
  if (action === 'zoom-in') { btReplay.count = Math.max(10, Math.floor(btReplay.count / 1.4)); }
  else if (action === 'zoom-out') { btReplay.count = Math.min(maxCount, Math.ceil(btReplay.count * 1.4)); }
  else if (action === 'pan-left') { btReplay.startIdx = Math.max(0, btReplay.startIdx - Math.floor(btReplay.count / 3)); }
  else if (action === 'pan-right') { btReplay.startIdx = Math.min(total - btReplay.count, btReplay.startIdx + Math.floor(btReplay.count / 3)); }
  else if (action === 'reset') { btReplay.count = Math.min(maxCount, Math.max(10, Math.floor(total / 3) || BT_REPLAY_MAX)); btReplay.startIdx = Math.max(0, total - btReplay.count); }
  btReplay.startIdx = Math.max(0, Math.min(Math.max(0, total - btReplay.count), btReplay.startIdx));
  drawBtReplayKline();
}

/** @returns {void} */
function drawBtReplayKline() {
  const canvas = $('#bt-replay-canvas');
  if (!canvas) return;
  const parent = canvas.parentNode;
  const width = Math.max(480, parent.clientWidth || 900);
  const height = Math.max(320, Math.min(620, window.innerHeight * 0.55));
  canvas.width = width * 2;
  canvas.height = height * 2;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(2, 0, 0, 2, 0, 0);
  ctx.fillStyle = '#fbfcfd';
  ctx.fillRect(0, 0, width, height);
  const bars = btReplay.bars;
  const endIdx = Math.min(bars.length, btReplay.startIdx + btReplay.count);
  const slice = bars.slice(btReplay.startIdx, endIdx);
  if (!slice.length) return;
  const pad = { top: 26, right: 12, bottom: 26, left: 12 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const step = plotW / slice.length;
  const bodyW = Math.max(1.4, step * 0.62);
  // 价格范围(纳入标记价格)
  let lo = Infinity; let hi = -Infinity;
  slice.forEach((b) => {
    lo = Math.min(lo, Number(b.low));
    hi = Math.max(hi, Number(b.high));
  });
  btReplay.markers.forEach((m) => {
    if (!m.price) return;
    if (btReplay.bars.some((b, idx) => idx >= btReplay.startIdx && idx < endIdx && b.trading_day === m.day)) {
      lo = Math.min(lo, m.price);
      hi = Math.max(hi, m.price);
    }
  });
  if (hi - lo < 1e-9) { hi += 0.01; lo -= 0.01; }
  const yAt = (v) => pad.top + (plotH * (hi - v)) / (hi - lo);
  const xAt = (i) => pad.left + step * (i - btReplay.startIdx) + step / 2;
  // 网格
  ctx.strokeStyle = '#e6ebf0';
  ctx.fillStyle = '#8a97a5';
  ctx.font = '10px sans-serif';
  ctx.textAlign = 'left';
  for (let g = 0; g <= 5; g += 1) {
    const v = lo + (hi - lo) * (g / 5);
    const y = yAt(v);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(v.toFixed(2), 2, y - 2);
  }
  // K 线
  const dateLabels = new Set();
  const labelEvery = Math.max(1, Math.floor(slice.length / 8));
  slice.forEach((b, li) => {
    const absIdx = btReplay.startIdx + li;
    const open = Number(b.open); const close = Number(b.close);
    const high = Number(b.high); const low = Number(b.low);
    const x = xAt(absIdx);
    const up = close >= open;
    ctx.strokeStyle = up ? '#c0392b' : '#1e8449';
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yAt(high));
    ctx.lineTo(x, yAt(low));
    ctx.stroke();
    const top = yAt(Math.max(open, close));
    const bottom = yAt(Math.min(open, close));
    const h = Math.max(1, bottom - top);
    ctx.fillRect(x - bodyW / 2, top, bodyW, h);
    if (li % labelEvery === 0) dateLabels.add(absIdx);
  });
  // 日期刻度
  ctx.textAlign = 'center';
  dateLabels.forEach((absIdx) => {
    if (absIdx < btReplay.startIdx || absIdx >= endIdx) return;
    ctx.fillText(bars[absIdx].trading_day.slice(5), xAt(absIdx), height - 8);
  });
  // 买卖点标记:买▲在 K 上方、卖▼在 K 下方,附价格×股数
  const dayIndex = {};
  bars.forEach((b, idx) => { dayIndex[b.trading_day] = idx; });
  ctx.textAlign = 'left';
  btReplay.markers.forEach((m) => {
    const absIdx = dayIndex[m.day];
    if (absIdx == null || absIdx < btReplay.startIdx || absIdx >= endIdx) return;
    const x = xAt(absIdx);
    const bar = bars[absIdx];
    if (m.side === 'buy') {
      const y = yAt(Number(bar.high)) - 2;
      ctx.fillStyle = '#c0392b';
      ctx.beginPath();
      ctx.moveTo(x, y - 8); ctx.lineTo(x - 6, y + 2); ctx.lineTo(x + 6, y + 2);
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = '#8c2f22';
      ctx.fillText('B ' + (m.price == null ? '' : m.price.toFixed(2)) + (m.shares ? '×' + fmtShares(m.shares) : ''), x + 8, y - 1);
    } else {
      const y = yAt(Number(bar.low)) + 2;
      ctx.fillStyle = '#1e8449';
      ctx.beginPath();
      ctx.moveTo(x, y + 8); ctx.lineTo(x - 6, y - 2); ctx.lineTo(x + 6, y - 2);
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = '#1a5c33';
      ctx.fillText('S ' + (m.price == null ? '' : m.price.toFixed(2)) + (m.shares ? '×' + fmtShares(m.shares) : ''), x + 8, y + 10);
    }
  });
}

/** @returns {Promise<void>} 取消当前回测任务 */
async function cancelBtRun() {
  if (!btRunId) return;
  try {
    await api('POST', '/api/research/backtests/' + btRunId + '/cancel', {});
    setBtProgress('已请求取消任务…', null);
  } catch (error) {
    toast('取消失败：' + (error && error.message ? error.message : String(error)), 'err');
  }
}

function bindEvents() {
  initializePanelToggles();
  initializeStockDetail();
  const legacyRunBtn = $('#run-backtest');
  if (legacyRunBtn) legacyRunBtn.remove(); // 旧工作台回测入口已迁移至独立视图
  $('#run-screen').addEventListener('click', runScreen);
  $('#shutdown-server').addEventListener('click', shutdownServer);
  const gateShutdown = $('#gate-shutdown');
  if (gateShutdown) gateShutdown.addEventListener('click', shutdownServer);
  document.querySelectorAll('#bootstrap-start').forEach((bootBtn) => {
    const span = bootBtn.querySelector('.btn__label');
    if (span) bootBtn.dataset.origLabel = span.textContent;
    if (bootBtn === $('#bootstrap-start')) {
      bootBtn.addEventListener('click', startBootstrap);
    }
  });
  const sourceSel = $('#bootstrap-source');
  if (sourceSel) sourceSel.addEventListener('change', toggleSeedField);
  const enterBtn = $('#gate-enter');
  if (enterBtn) enterBtn.addEventListener('click', () => setView('workbench'));
  const openDataBtn = $('#open-data-ui');
  if (openDataBtn) openDataBtn.addEventListener('click', () => setView('gate'));
  const backtestOpenBtn = $('#backtest-open');
  if (backtestOpenBtn) backtestOpenBtn.addEventListener('click', () => setView('backtest'));
  const btBackWorkbench = $('#bt-back-workbench');
  if (btBackWorkbench) btBackWorkbench.addEventListener('click', () => setView('workbench'));
  const btOpenData = $('#bt-open-data');
  if (btOpenData) btOpenData.addEventListener('click', () => setView('gate'));
  const btHistoryList = $('#bt-history-list');
  if (btHistoryList) {
    btHistoryList.addEventListener('click', (event) => {
      const row = event.target.closest('[data-run]');
      if (row) openBacktestRun(row.dataset.run);
    });
  }
  const btHistoryRefresh = $('#bt-history-refresh');
  if (btHistoryRefresh) btHistoryRefresh.addEventListener('click', () => loadBacktestHistory());
  const btFeeReset = $('#bt-fee-reset');
  if (btFeeReset) btFeeReset.addEventListener('click', setBtFeeDefaults);
  const btWindowPreset = $('#bt-window-preset');
  if (btWindowPreset) btWindowPreset.addEventListener('change', () => {
    const custom = $('#bt-window-custom');
    if (custom) custom.hidden = btWindowPreset.value !== 'custom';
  });
  document.querySelectorAll('[data-bt-mode]').forEach((segment) => {
    segment.addEventListener('click', () => setBtMode(segment.dataset.btMode));
  });

  const btStrategySelect = $('#bt-strategy-select');
  if (btStrategySelect) {
    btStrategySelect.addEventListener('change', (event) => {
      if (event.target.value) loadBtStrategy(event.target.value);
    });
  }
  const btNewStrategy = $('#bt-strategy-new');
  if (btNewStrategy) btNewStrategy.addEventListener('click', () => {
    btCurrentStrategy = null;
    const nameInput = $('#bt-strategy-name');
    const description = $('#bt-strategy-description');
    if (nameInput) nameInput.value = '';
    if (description) description.value = '';
    btUseDefaultEditor();
  });
  const btSaveBtn = $('#bt-strategy-save');
  if (btSaveBtn) btSaveBtn.addEventListener('click', () => btSaveStrategy(false));
  const btSaveAsBtn = $('#bt-strategy-saveas');
  if (btSaveAsBtn) btSaveAsBtn.addEventListener('click', () => btSaveStrategy(true));
  const btValidateBtn = $('#bt-strategy-validate');
  if (btValidateBtn) {
    btValidateBtn.addEventListener('click', async () => {
      try {
        await btValidateLocal();
        toast('校验通过：政策组合与止盈档合法。', 'ok');
      } catch (error) {
        toast('校验失败：' + (error && error.message ? error.message : String(error)), 'err');
      }
    });
  }
  const btDeleteBtn = $('#bt-strategy-delete');
  if (btDeleteBtn) btDeleteBtn.addEventListener('click', btDeleteStrategy);
  const btEntryAdd = $('#bt-entry-add');
  if (btEntryAdd) {
    btEntryAdd.addEventListener('click', () => {
      if (btEditor.entry.length >= 5) { toast('入场组最多 5 个政策。', 'warn'); return; }
      const policyId = btFirstPolicyId('entry');
      if (policyId) { btEditor.entry.push({ policy_id: policyId, parameters: {} }); }
      renderBtEditor();
      markBtDirty(true);
    });
  }
  const btExitAdd = $('#bt-exit-add');
  if (btExitAdd) {
    btExitAdd.addEventListener('click', () => {
      if (btEditor.exit.length >= 5) { toast('退出组最多 5 个政策。', 'warn'); return; }
      const policyId = btFirstPolicyId('exit');
      if (policyId) { btEditor.exit.push({ policy_id: policyId, parameters: {} }); }
      renderBtEditor();
      markBtDirty(true);
    });
  }
  const btTpAdd = $('#bt-tp-add');
  if (btTpAdd) {
    btTpAdd.addEventListener('click', () => {
      if (btEditor.tiers.length >= 5) { toast('止盈档最多 5 个。', 'warn'); return; }
      btEditor.tiers.push({});
      renderBtEditor();
      markBtDirty(true);
    });
  }
  document.querySelectorAll('[data-bt-op-kind]').forEach((button) => {
    button.addEventListener('click', () => {
      const kind = button.dataset.btOpKind;
      btEditor[kind + 'Operator'] = button.dataset.btOpValue;
      renderBtOperatorButtons();
      markBtDirty(true);
    });
  });
  const btEditorRoot = $('#bt-strategy-content');
  if (btEditorRoot) {
    btEditorRoot.addEventListener('click', (event) => {
      const remove = event.target.closest('[data-bt-remove]');
      if (remove) {
        const [kind, index] = remove.dataset.btRemove.split(':');
        btEditor[kind].splice(Number(index), 1);
        renderBtEditor();
        markBtDirty(true);
        return;
      }
      const tierRemove = event.target.closest('[data-bt-remove-tier]');
      if (tierRemove) {
        btEditor.tiers.splice(Number(tierRemove.dataset.btRemoveTier), 1);
        renderBtEditor();
        markBtDirty(true);
        return;
      }
      const singleSelect = event.target.closest('select[data-single-kind]');
      if (singleSelect) {
        const kind = singleSelect.dataset.singleKind;
        btEditor.singles[kind] = { policy_id: singleSelect.value, parameters: {} };
        renderBtSingleParams(kind);
        refreshBtSingleDesc(kind);
        markBtDirty(true);
        return;
      }
      const groupSelect = event.target.closest('[data-kind]');
      if (groupSelect) {
        const kind = groupSelect.dataset.kind;
        const index = Number(groupSelect.dataset.index);
        btEditor[kind][index] = { policy_id: groupSelect.value, parameters: {} };
        renderBtPolicyParams(kind, index);
        refreshBtPolicyDesc(kind, index);
        markBtDirty(true);
        return;
      }
    });
    btEditorRoot.addEventListener('input', (event) => {
      const tierInput = event.target.closest('[data-tier-index]');
      if (tierInput) {
        const index = Number(tierInput.dataset.tierIndex);
        const field = tierInput.dataset.tierField;
        // 小数比例口径:输入/显示/保存均为同一单位(如 0.02=2%),不做换算。
        btEditor.tiers[index][field] = tierInput.value;
        markBtDirty(true);
        return;
      }
      const groupInput = event.target.closest('[data-policy-index]');
      if (groupInput) { markBtDirty(true); return; }
      const singleInput = event.target.closest('[data-single-kind]');
      if (singleInput) { markBtDirty(true); }
    });
  }
  const btRunBtn = $('#bt-run');
  if (btRunBtn) btRunBtn.addEventListener('click', runNewBacktest);
  const btCancelBtn = $('#bt-cancel');
  if (btCancelBtn) btCancelBtn.addEventListener('click', cancelBtRun);
  const btOrdersBox = $('#bt-orders');
  if (btOrdersBox) {
    btOrdersBox.addEventListener('click', (event) => {
      const row = event.target.closest('[data-run][data-code]');
      if (row && row.dataset.code) openBtReplay(row.dataset.code, row.dataset.run);
    });
  }
  const btOrdersMore = $('#bt-orders-more-btn');
  if (btOrdersMore) {
    btOrdersMore.addEventListener('click', () => {
      if (!btActiveRun) return;
      btOrdersOffset += BT_ORDERS_PAGE;
      loadBtOrders(btActiveRun, false);
    });
  }
  const btReplayClose = $('#bt-replay-close');
  if (btReplayClose) btReplayClose.addEventListener('click', closeBtReplay);
  document.querySelectorAll('#bt-replay-overlay [data-kline-action]').forEach((button) => {
    button.addEventListener('click', () => btReplayAction(button.dataset.klineAction));
  });
  const btCodesInput = $('#bt-codes');
  if (btCodesInput) btCodesInput.addEventListener('input', updateBtSingleCodeState);
  const capmSyncButton = $('#capm-reference-sync');
  if (capmSyncButton) capmSyncButton.addEventListener('click', syncCapmReferenceData);
  $('#reload-template').addEventListener('click', () => {
    if (state.currentId) loadTemplate(state.currentId);
  });
  $('#validate-template').addEventListener('click', validateTemplate);
  $('#save-template').addEventListener('click', saveTemplate);
  $('#save-as-template').addEventListener('click', saveAsTemplate);
  $('#delete-template').addEventListener('click', deleteTemplate);
  $('#add-group').addEventListener('click', () => {
    const n = state.composition.groups.length + 1;
    let gid = 'group-' + n;
    while (state.composition.groups.some((g) => g.group_id === gid)) { n += 1; gid = 'group-' + n; }
    state.composition.groups.push({ group_id: gid, operator: 'all', rule_ids: [] });
    renderGroups();
    markDirty();
  });
  $('#template-select').addEventListener('change', (e) => {
    if (e.target.value) loadTemplate(e.target.value);
  });
  document.querySelectorAll('[data-compose]').forEach((b) => {
    b.addEventListener('click', () => setComposeOperator(b.dataset.compose));
  });
  $('#result-body').addEventListener('click', (e) => {
    const filterBtn = e.target.closest('[data-filter]');
    if (filterBtn) { state.resultFilter = filterBtn.dataset.filter; renderResults(); return; }
    const row = e.target.closest('tr[data-code]');
    if (row) selectStock(row.dataset.code);
  });
  $('#result-body').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest('tr[data-code]');
    if (row) { e.preventDefault(); selectStock(row.dataset.code); }
  });
  $('#editor-rules').addEventListener('click', (e) => {
    const toggle = e.target.closest('.toggle');
    if (!toggle) return;
    const ruleId = toggle.closest('.rule-card').dataset.rule;
    const on = toggle.getAttribute('aria-checked') === 'true';
    const nowOn = !on;
    toggle.setAttribute('aria-checked', nowOn ? 'true' : 'false');
    toggle.closest('.rule-card').classList.toggle('rule-card--off', !nowOn);
    if (!nowOn) {
      // 关闭：从所有分组移除
      for (const g of state.composition.groups) { g.rule_ids = g.rule_ids.filter((id) => id !== ruleId); }
    } else {
      // 重新启用：若未分配到任何分组，自动放入一个分组，避免"未分配"报错
      const assigned = state.composition.groups.some((g) => g.rule_ids.includes(ruleId));
      if (!assigned) {
        let target = state.composition.groups.find((g) => g.rule_ids.length > 0) || state.composition.groups[0];
        if (!target) {
          let n = state.composition.groups.length + 1;
          let gid = 'group-' + n;
          while (state.composition.groups.some((g) => g.group_id === gid)) { n += 1; gid = 'group-' + n; }
          state.composition.groups.push({ group_id: gid, operator: 'all', rule_ids: [] });
          target = state.composition.groups[state.composition.groups.length - 1];
        }
        target.rule_ids.push(ruleId);
      }
    }
    renderGroups();
    markDirty();
  });
}

/* ---------- init ---------- */
/**
 * Real completed tasks, independent of elapsed time. The first error is terminal;
 * manual retry reloads the document, so old requests cannot affect a new attempt.
 * @returns {{run: <T>(index: number, task: () => Promise<T>) => Promise<T>, finish: () => void, fail: (label: string, error: unknown) => void}}
 */
function createStartupProgress() {
  const labels = ['加载规则', '读取模板', '加载策略', '检查本地数据', '准备工作台'];
  const steps = labels.map(() => 'waiting');
  const names = { waiting: '等待中', running: '进行中', done: '已完成', error: '失败', stopped: '未完成' };
  const started = performance.now();
  let terminal = false;

  /** @returns {void} */
  function render() {
    const done = steps.filter((s) => s === 'done').length;
    $('#startup-progress').value = done;
    $('#startup-count').textContent = '已完成 ' + done + '/' + labels.length + ' 项';
    labels.forEach((label, i) => {
      const item = $('#startup-step-' + i);
      item.dataset.status = steps[i];
      item.textContent = label + ' · ' + names[steps[i]];
    });
    const active = labels.filter((_, i) => steps[i] === 'running');
    $('#startup-message').textContent = active.length ? '正在' + active.join('、') + '…' : '正在准备下一步…';
    tick();
  }

  /** @returns {void} */
  function tick() {
    if (terminal) return;
    const seconds = Math.floor((performance.now() - started) / 1000);
    $('#startup-elapsed').textContent = '已等待 ' + seconds + ' 秒';
    const hint = $('#startup-hint');
    hint.hidden = seconds < 10;
    hint.textContent = steps[3] === 'running'
      ? '检查本地数据耗时较长，仍在处理中，请稍候。'
      : '加载比平时稍慢，仍在处理中，请稍候。';
    if (seconds >= 60) {
      hint.textContent += ' 你可以继续等待，或点击“重新加载”重试。';
      $('#startup-retry').hidden = false;
    }
  }

  const timer = setInterval(tick, 1000);
  $('#startup-retry').addEventListener('click', () => window.location.reload());
  render();

  /** @param {string} label @param {unknown} error @returns {void} */
  function fail(label, error) {
    if (terminal) return;
    steps.forEach((status, i) => { if (status === 'running') steps[i] = 'stopped'; });
    render();
    terminal = true;
    clearInterval(timer);
    $('#startup-view').dataset.status = 'error';
    $('#startup-view').setAttribute('aria-busy', 'false');
    $('#startup-title').textContent = '工作台加载失败';
    const reason = error instanceof Error ? error.message : String(error);
    $('#startup-message').textContent = label + '失败：' + reason;
    $('#startup-hint').hidden = false;
    $('#startup-hint').textContent = '请确认本地服务正在运行，再点击“重新加载”重试。';
    $('#startup-retry').hidden = false;
    $('#gate-view').hidden = true;
    $('#workbench-view').hidden = true;
  }

  /** @template T @param {number} index @param {() => Promise<T>} task @returns {Promise<T>} */
  async function run(index, task) {
    steps[index] = 'running';
    render();
    try {
      const result = await task();
      if (!terminal) { steps[index] = 'done'; render(); }
      return result;
    } catch (error) {
      if (!terminal) {
        steps[index] = 'error';
        render();
        fail(labels[index], error);
      }
      throw error;
    }
  }

  /** @returns {void} */
  function finish() {
    if (terminal) return;
    terminal = true;
    clearInterval(timer);
    $('#startup-view').setAttribute('aria-busy', 'false');
    $('#startup-view').hidden = true;
  }
  return { run, finish, fail };
}

/** @returns {Promise<void>} */
async function init() {
  const progress = createStartupProgress();
  try {
    bindEvents();
    const today = new Date();
    /** @param {number} n @returns {string} */
    const pad = (n) => String(n).padStart(2, '0');
    $('#trading-day').value = today.getFullYear() + '-' + pad(today.getMonth() + 1) + '-' + pad(today.getDate());
    const [rulesData, templatesData, , syncStatus] = await Promise.all([
      progress.run(0, () => api('GET', '/api/rules')),
      progress.run(1, () => api('GET', '/api/templates')),
      progress.run(2, loadResearchPolicies),
      progress.run(3, () => api('GET', '/api/sync/status')),
    ]);
    await progress.run(4, async () => {
      state.rules = rulesData.rules;
      state.templates = templatesData.templates;
      const preferred = state.templates.find((t) => t.is_system) || state.templates[0];
      if (preferred) await loadTemplate(preferred.template_id, { propagateError: true });
      else { renderTemplateSelect(); toast('未发现任何模板。', 'warn'); }
      renderSyncStatus(syncStatus);
    });
    $('#server-status').className = 'badge badge--ok';
    $('#server-status').textContent = '服务正常';
    progress.finish();
  } catch (err) {
    progress.fail('准备工作台', err);
    $('#server-status').className = 'badge badge--err';
    $('#server-status').textContent = '加载失败';
    return;
  }
  startSyncPolling();
  loadInstances();
  loadVersion();
}

document.addEventListener('DOMContentLoaded', init);
