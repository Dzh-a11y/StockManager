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
};

let _klineBars = null;      // last full dataset drawn (for hover / zoom / resize)
let _klineCanvas = null;    // last active canvas
let _klineInfoDefault = ''; // default info text (restored on mouse leave)
let _klineView = null;      // visible window {start, end} into _klineBars
let _klineDrag = null;      // {startX, viewStart} while panning
const _klineCache = new Map(); // code+adjustment+end -> bars

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
async function loadTemplate(id) {
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
  startScreenPolling();
  try {
    const data = await api('POST', '/api/screen', { template: buildPayload(), ...cond });
    state.result = data;
    state.resultFilter = 'all';
    state.selectedCode = null;
    renderResults();
    toast('筛选完成：共 ' + data.summary.total + ' 只。', 'success');
  } catch (err) {
    state.result = null;
    renderResults();
    toast('筛选失败：' + err.message, 'error');
  } finally {
    progress.hidden = true;
    stopScreenPolling();
    $('#screen-progress-track').hidden = true;
    $('#screen-current').hidden = true;
    btn.disabled = false;
  }
}

let syncPollTimer = null;
function startSyncPolling() {
  if (syncPollTimer) return;
  pollSyncProgress();
  syncPollTimer = setInterval(pollSyncProgress, 1000);
}
function renderSyncProgress(p) {
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

async function shutdownServer() {
  if (!window.confirm('确定停止本服务进程吗？停止后需要重新启动才能继续使用。')) return;
  const status = $('#shutdown-status');
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

async function loadSyncStatus() {
  try {
    const s = await api('GET', '/api/sync/status');
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
  } catch (e) { /* ignore */ }
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
    const label = $('#instances-label');
    const ul = $('#instances-list');
    const note = $('#instances-note');
    if (!list.length) {
      label.hidden = true;
      ul.hidden = true;
      ul.innerHTML = '';
      note.hidden = true;
      return;
    }
    label.hidden = false;
    label.textContent = '当前 ' + list.length + ' 个相同实例在运行';
    ul.hidden = false;
    ul.innerHTML = list.map((item) => {
      const self = item.is_self ? '（当前服务）' : '';
      return '<li>' + esc('pid ' + item.pid) + ' ' + self +
        ' <button class="btn btn--danger" data-pid="' + item.pid + '">停止</button>' +
        '<code>' + esc(item.command || '') + '</code></li>';
    }).join('');
    ul.querySelectorAll('button[data-pid]').forEach((btn) => {
      btn.addEventListener('click', () => killInstance(Number(btn.dataset.pid)));
    });
    note.hidden = false;
    note.textContent = '「停止」会结束该实例进程；若停止的是当前服务，页面会断开，需重新启动。';
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
    html.push('<tr class="clickable" data-code="' + esc(r.code) + '" data-pill="' + pill + '"><td><span class="status-pill status-pill--' + pill + '">' + pill + '</span></td><td>' + esc(r.code) + '</td><td>' + esc(r.name) + '</td><td>' + esc(r.trading_day) + '</td></tr>');
  }
  html.push('</tbody></table>');
  if (state.selectedCode) {
    const r = data.results.find((x) => x.code === state.selectedCode);
    if (r) html.push(buildDetail(r));
  }
  body.innerHTML = html.join('');
  if (state.selectedCode) {
    const r = data.results.find((x) => x.code === state.selectedCode);
    if (r) loadBars(r);
  }
}

function buildDetail(r) {
  const pill = r.passed ? 'PASSED' : 'FAILED';
  let inner = '<div class="detail-panel__head"><span class="detail-panel__title">' + esc(r.code) + ' · ' + esc(r.name) + '</span><span class="status-pill status-pill--' + pill + '">' + pill + '</span></div>';
  inner += r.rule_executions.map((ex) => {
    const st = ex.status;
    const name = (state.rules.find((x) => x.rule_id === ex.rule_id) || {}).name || ex.rule_id;
    let vals = '';
    if (ex.result) {
      vals = '<div class="rule-detail__vals"><span>实际值 ' + esc(ex.result.actual_value) + '</span><span>阈值 ' + esc(ex.result.threshold) + '</span></div>';
    }
    return '<div class="rule-detail"><div class="rule-detail__head"><span class="status-pill status-pill--' + st + '">' + st + '</span><span class="rule-detail__name">' + esc(name) + '</span></div><p class="rule-detail__reason">' + esc(ex.result ? ex.result.reason : '规则未参与（SKIPPED）') + '</p>' + vals + '</div>';
  }).join('');
  inner += '<div class="kline-panel">' +
    '<div class="kline-panel__head"><span class="kline-panel__title">本地日K · 成交量</span>' +
    '<span class="kline-toolbar">' +
    '<button class="kline-btn" data-kline-action="zoom-out" type="button" title="缩小">−</button>' +
    '<button class="kline-btn" data-kline-action="pan-left" type="button" title="左移">←</button>' +
    '<button class="kline-btn" data-kline-action="pan-right" type="button" title="右移">→</button>' +
    '<button class="kline-btn" data-kline-action="zoom-in" type="button" title="放大">＋</button>' +
    '<button class="kline-btn" data-kline-action="reset" type="button" title="复位">复位</button>' +
    '</span>' +
    '<span id="kline-info" class="kline-panel__info">加载中…</span>' +
    '</div>' +
    '<canvas id="kline-canvas" class="kline-canvas"></canvas>' +
    '</div>';
  return '<div class="detail-panel">' + inner + '</div>';
}

/* ---------- local K-line chart ---------- */
function limitUpDatesOf(r) {
  // 涨幅次数规则（limit_up_3m）在 actual_value.trading_days 中返回涨幅日期。
  const ex = (r.rule_executions || []).find((x) => x.rule_id === 'limit_up_3m');
  const v = ex && ex.result && ex.result.actual_value;
  return v && Array.isArray(v.trading_days) ? v.trading_days : [];
}

async function loadBars(r) {
  const canvas = $('#kline-canvas');
  const info = $('#kline-info');
  if (!canvas || !info) return;
  try {
    const adj = (state.result && state.result.adjustment) || 'qfq';
    const end = r.trading_day || (state.result && state.result.trading_day) || '';
    const key = klineKey(r.code, adj, end);
    let bars;
    let dataEnd;
    const cached = _klineCache.get(key);
    if (cached) {
      bars = cached.bars;
      dataEnd = cached.end;
    } else {
      const query = new URLSearchParams({ code: r.code, adjustment: adj, end: end, days: '250' });
      const data = await api('GET', '/api/bars?' + query.toString());
      bars = data.bars || [];
      dataEnd = data.end || end;
      _klineCache.set(key, { bars, end: dataEnd });
    }
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
function bindEvents() {
  $('#run-screen').addEventListener('click', runScreen);
  $('#shutdown-server').addEventListener('click', shutdownServer);
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
    const klineBtn = e.target.closest('[data-kline-action]');
    if (klineBtn) { handleKlineAction(klineBtn.dataset.klineAction); return; }
    const filterBtn = e.target.closest('[data-filter]');
    if (filterBtn) { state.resultFilter = filterBtn.dataset.filter; renderResults(); return; }
    const row = e.target.closest('tr[data-code]');
    if (row) { state.selectedCode = row.dataset.code; renderResults(); }
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
async function init() {
  bindEvents();
  startSyncPolling();
  loadInstances();
  loadSyncStatus();
  const today = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  $('#trading-day').value = today.getFullYear() + '-' + pad(today.getMonth() + 1) + '-' + pad(today.getDate());
  $('#server-status').textContent = '本机离线';
  try {
    const [rulesData, templatesData] = await Promise.all([
      api('GET', '/api/rules'),
      api('GET', '/api/templates'),
    ]);
    state.rules = rulesData.rules;
    state.templates = templatesData.templates;
    $('#server-status').className = 'badge badge--ok';
    $('#server-status').textContent = '服务正常';
    const preferred = state.templates.find((t) => t.is_system) || state.templates[0];
    if (preferred) await loadTemplate(preferred.template_id);
    else toast('未发现任何模板。', 'warn');
  } catch (err) {
    $('#server-status').className = 'badge badge--err';
    $('#server-status').textContent = '连接失败';
    toast('初始化失败：' + err.message, 'error');
  }
  loadVersion();
}

document.addEventListener('DOMContentLoaded', init);
