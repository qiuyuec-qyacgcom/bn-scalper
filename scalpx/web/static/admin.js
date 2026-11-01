/* scalpx 后台：用户权限 / 配置 / 数据 / 风控 / 审计 */
'use strict';

const AdminState = { cfgProfile: '', cfgData: null, system: null, view: 'overview' };

/* ------------------------------------------------------------------ */
/* 登录                                                                */
/* ------------------------------------------------------------------ */
async function adminLogin(ev) {
  ev.preventDefault();
  $('#loginErr').textContent = '';
  try {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: { username: $('#loginUser').value.trim(), password: $('#loginPass').value },
    });
    if (data.user.role !== 'admin') {
      $('#loginErr').textContent = '该账号不是管理员，无法进入后台';
      return;
    }
    Store.token = data.token;
    Store.user = data.user;
    await adminBoot();
  } catch (err) {
    $('#loginErr').textContent = err.message;
  }
}

async function adminBoot() {
  let me;
  try { me = await api('/api/auth/me'); } catch { return; }
  if (me.role !== 'admin') {
    forceLogout('需要管理员权限');
    return;
  }
  $('#loginView').style.display = 'none';
  $('#appView').style.display = 'flex';
  $('#userName').textContent = me.username;
  await adminLoadSystem();
  switchAdminView('overview');
}

/* ------------------------------------------------------------------ */
/* 视图                                                                */
/* ------------------------------------------------------------------ */
const ADMIN_TITLES = {
  overview: ['概览', '系统状态与任务'],
  users: ['用户与权限', '角色、状态与会话失效'],
  instruments: ['标的库', '跨资产类型的主数据：股票 / 基金 / 债券 / 期货 / 外汇 / 加密'],
  entities: ['主数据管理', '交易账户 / 策略实例 / 风控档案 / 数据源 · schema 驱动 + AI 辅助'],
  config: ['系统配置', '在线编辑并按正式加载器校验'],
  data: ['数据管理', '历史行情下载与运行清理'],
  risk: ['风控开关', '紧急停机与恢复'],
  audit: ['审计日志', '所有敏感操作可追溯'],
  system: ['系统信息', '运行环境与存储'],
};

function switchAdminView(name) {
  AdminState.view = name;
  $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  $$('.view').forEach(el => el.classList.toggle('active', el.id === 'view-' + name));
  const [t, s] = ADMIN_TITLES[name] || ['', ''];
  $('#pageTitle').textContent = t;
  $('#pageSub').textContent = s;

  if (name === 'overview') adminLoadOverview();
  else if (name === 'users') adminLoadUsers();
  else if (name === 'instruments') adminLoadInstruments();
  else if (name === 'entities') adminLoadEntities();
  else if (name === 'config') adminLoadConfig();
  else if (name === 'data') { adminLoadJobs(); adminLoadRuns(); }
  else if (name === 'risk') adminLoadRisk();
  else if (name === 'audit') adminLoadAudit();
  else if (name === 'system') adminLoadSystem(true);
}

/* ------------------------------------------------------------------ */
/* 概览                                                                */
/* ------------------------------------------------------------------ */
async function adminLoadOverview() {
  const sys = await adminLoadSystem();
  const users = await api('/api/admin/users');
  $('#ovUsers').textContent = fmt.int(users.items.length);
  const runs = sys.storage.tables.runs || 0;
  $('#ovRuns').textContent = fmt.int(runs);
  $('#ovDbSize').textContent = (sys.storage.trading_db_size / 1048576).toFixed(1) + ' MB';
  $('#ovDbPath').textContent = sys.storage.trading_db;
  $('#ovUptime').textContent = Math.floor(sys.service.uptime_s / 60) + ' 分';
  $('#ovVersion').textContent = `web ${sys.service.version} · core ${sys.service.core_version}`;

  renderTable($('#tableStats'), [
    { title: '表', key: 'name' },
    { title: '行数', align: 'right', render: r => fmt.int(r.rows) },
  ], Object.entries(sys.storage.tables).map(([name, rows]) => ({ name, rows })));

  adminRenderJobs($('#jobTable'));
}

/* ------------------------------------------------------------------ */
/* 用户                                                                */
/* ------------------------------------------------------------------ */
async function adminLoadUsers() {
  const d = await api('/api/admin/users');
  renderTable($('#userTable'), [
    { title: 'ID', key: 'id', cls: 'mono' },
    { title: '用户名', key: 'username' },
    { title: '角色', render: u => `<span class="badge ${u.role === 'admin' ? 'warn' : ''}">${u.role === 'admin' ? '管理员' : '用户'}</span>` },
    { title: '状态', render: u => `<span class="badge ${u.status === 'active' ? 'ok' : 'bad'}">${u.status === 'active' ? '启用' : '禁用'}</span>` },
    { title: '创建时间', render: u => fmt.datetime(u.created_ts * 1000), cls: 'mono' },
    { title: '最近登录', render: u => u.last_login_ts ? fmt.datetime(u.last_login_ts * 1000) : '从未', cls: 'mono' },
    {
      title: '操作', align: 'right', render: u => `
        <button class="btn sm ghost" data-act="role" data-id="${u.id}" data-role="${u.role}">${u.role === 'admin' ? '降为用户' : '提为管理员'}</button>
        <button class="btn sm ghost" data-act="status" data-id="${u.id}" data-status="${u.status}">${u.status === 'active' ? '禁用' : '启用'}</button>
        <button class="btn sm ghost" data-act="pwd" data-id="${u.id}" data-name="${u.username}">重置口令</button>
        <button class="btn sm danger" data-act="del" data-id="${u.id}" data-name="${u.username}">删除</button>`,
    },
  ], d.items);

  $$('[data-act]').forEach(btn => {
    btn.onclick = () => adminUserAction(btn.dataset.act, btn.dataset);
  });
}

async function adminUserAction(act, data) {
  try {
    if (act === 'role') {
      await api(`/api/admin/users/${data.id}`, { method: 'PATCH', body: { role: data.role === 'admin' ? 'user' : 'admin' } });
      toast('角色已更新', 'ok');
    } else if (act === 'status') {
      await api(`/api/admin/users/${data.id}`, { method: 'PATCH', body: { status: data.status === 'active' ? 'disabled' : 'active' } });
      toast('状态已更新', 'ok');
    } else if (act === 'pwd') {
      const pwd = prompt(`为 ${data.name} 设置新口令（至少 8 位）`);
      if (!pwd) return;
      await api(`/api/admin/users/${data.id}`, { method: 'PATCH', body: { password: pwd } });
      toast('口令已重置，旧会话已失效', 'ok');
    } else if (act === 'del') {
      if (!confirm(`确认删除用户 ${data.name}？该操作不可撤销。`)) return;
      await api(`/api/admin/users/${data.id}`, { method: 'DELETE' });
      toast('用户已删除', 'ok');
    }
    adminLoadUsers();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function openModal(html, onMount) {
  $('#modalBox').innerHTML = html;
  $('#modalHost').classList.add('open');
  if (onMount) onMount();
}

function closeModal() {
  $('#modalHost').classList.remove('open');
}

function adminNewUser() {
  openModal(`
    <h3>新建用户</h3>
    <div class="field"><label>用户名</label><input id="nuName" type="text" placeholder="3-32 位字母数字" /></div>
    <div class="field"><label>初始口令</label><input id="nuPass" type="password" placeholder="至少 8 位" /></div>
    <div class="field"><label>角色</label>
      <select id="nuRole"><option value="user">普通用户</option><option value="admin">管理员</option></select></div>
    <div class="actions">
      <button class="btn" id="nuCancel">取消</button>
      <button class="btn primary" id="nuOk">创建</button>
    </div>`, () => {
    $('#nuCancel').onclick = closeModal;
    $('#nuOk').onclick = async () => {
      try {
        await api('/api/admin/users', {
          method: 'POST',
          body: { username: $('#nuName').value.trim(), password: $('#nuPass').value, role: $('#nuRole').value },
        });
        closeModal();
        toast('用户已创建', 'ok');
        adminLoadUsers();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
}

/* ------------------------------------------------------------------ */
/* 配置                                                                */
/* ------------------------------------------------------------------ */
async function adminLoadConfig(profile) {
  const d = await api('/api/admin/config' + (profile ? `?profile=${profile}` : ''));
  AdminState.cfgProfile = d.profile;
  AdminState.cfgData = d.data;

  const sel = $('#cfgProfile');
  sel.innerHTML = d.profiles.map(p => `<option ${p === d.profile ? 'selected' : ''}>${p}</option>`).join('');

  renderConfigForm(d.data);
}

/** 把嵌套配置拍平成 [路径, 显示名, 值]，支持 [strategy.params] 这类二级表 */
function flattenConfig(obj, prefix = []) {
  const out = [];
  Object.entries(obj).forEach(([k, v]) => {
    const path = prefix.concat(k);
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      out.push(...flattenConfig(v, path));
    } else {
      out.push([path, path.join('.'), v]);
    }
  });
  return out;
}

function renderConfigForm(data) {
  const host = $('#cfgList');
  host.innerHTML = '';

  const leaves = flattenConfig(data);
  const groups = new Map();          // 顶层分组 -> 叶子列表
  leaves.forEach(item => {
    const top = item[0][0];
    if (!groups.has(top)) groups.set(top, []);
    groups.get(top).push(item);
  });

  groups.forEach((items, group) => {
    const details = document.createElement('details');
    details.className = 'cfg-group';
    details.open = ['run', 'market', 'risk'].includes(group);
    const fields = items.map(([path, label, v]) => {
      // 组内显示去掉顶层前缀的名字（strategy.params 内显示 a / b，而不是 strategy.params.a）
      const short = path.length > 1 ? path.slice(1).join('.') : path[0];
      return configField(path.join('.'), short, v);
    }).join('');
    details.innerHTML = `<summary>${group} <span class="dim" style="font-size:11px">(${items.length})</span></summary>
      <div class="inner">${fields}</div>`;
    host.appendChild(details);
  });
}

function configField(path, label, value) {
  const id = 'cfg_' + path.replace(/\./g, '__');
  if (typeof value === 'boolean') {
    return `<div class="field"><label>${label}</label>
      <select data-cfg="${path}" data-type="bool" id="${id}">
        <option value="true" ${value ? 'selected' : ''}>true</option>
        <option value="false" ${!value ? 'selected' : ''}>false</option>
      </select></div>`;
  }
  if (Array.isArray(value)) {
    return `<div class="field"><label>${label}</label>
      <input data-cfg="${path}" data-type="list" id="${id}" value="${value.join(', ')}" /></div>`;
  }
  if (typeof value === 'number') {
    return `<div class="field"><label>${label}</label>
      <input data-cfg="${path}" data-type="number" id="${id}" value="${value}" /></div>`;
  }
  return `<div class="field"><label>${label}</label>
    <input data-cfg="${path}" data-type="string" id="${id}" value="${String(value ?? '')}" /></div>`;
}

function collectConfig() {
  const out = JSON.parse(JSON.stringify(AdminState.cfgData));
  $$('[data-cfg]').forEach(el => {
    const path = el.dataset.cfg.split('.');
    const type = el.dataset.type;
    let value;
    if (type === 'bool') value = el.value === 'true';
    else if (type === 'number') value = Number(el.value);
    else if (type === 'list') value = el.value.split(',').map(s => s.trim()).filter(Boolean).map(s => (s !== '' && !Number.isNaN(Number(s)) ? Number(s) : s));
    else value = el.value;

    let node = out;
    for (let i = 0; i < path.length - 1; i++) {
      if (typeof node[path[i]] !== 'object' || node[path[i]] === null) node[path[i]] = {};
      node = node[path[i]];
    }
    node[path[path.length - 1]] = value;
  });
  return out;
}

async function adminSaveConfig() {
  const btn = $('#cfgSave');
  btn.disabled = true;
  btn.textContent = '校验中…';
  try {
    const d = await api('/api/admin/config', {
      method: 'PUT',
      body: { profile: AdminState.cfgProfile, data: collectConfig() },
    });
    toast(`已保存（备份 ${d.backup}）`, 'ok');
    await adminLoadConfig(AdminState.cfgProfile);
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '保存并校验';
  }
}

/* ------------------------------------------------------------------ */
/* 数据                                                                */
/* ------------------------------------------------------------------ */
async function adminLoadJobs() {
  await adminRenderJobs($('#jobTable2'));
}

async function adminRenderJobs(table) {
  let d;
  try { d = await api('/api/admin/jobs'); } catch { return; }
  renderTable(table, [
    { title: '任务', render: j => `<span class="mono" style="font-size:11px">${j.id}</span>` },
    { title: '名称', key: 'name' },
    { title: '状态', render: j => {
        const map = { queued: ['warn', '排队'], running: ['warn', '运行中'], done: ['ok', '完成'], error: ['bad', '失败'] };
        const [c, label] = map[j.status] || ['', j.status];
        return `<span class="badge ${c}">${label}</span>`;
      } },
    { title: '耗时', align: 'right', render: j => j.elapsed_s + 's' },
    { title: '结果', render: j => j.status === 'error'
        ? `<span class="dim" style="font-size:11px">${j.error}</span>`
        : (j.result ? `<span class="dim" style="font-size:11px">${JSON.stringify(j.result).slice(0, 90)}</span>` : '—') },
  ], d.items, '暂无任务');
}

async function adminLoadRuns() {
  const d = await api('/api/runs?limit=50');
  renderTable($('#runAdminTable'), [
    { title: '运行 ID', key: 'run_id', cls: 'mono' },
    { title: '策略', key: 'strategy' },
    { title: '状态', render: r => `<span class="badge ${r.status === 'finished' ? 'ok' : 'warn'}">${r.status}</span>` },
    { title: '收益', align: 'right', render: r => `<span class="${cls(r.total_return)}">${fmt.pct(r.total_return)}</span>` },
    { title: '开始', render: r => fmt.datetime(r.started_ts), cls: 'mono' },
    { title: '', align: 'right', render: r => `<button class="btn sm danger" data-del-run="${r.run_id}">删除</button>` },
  ], d.items, '暂无运行');

  $$('[data-del-run]').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm(`删除运行 ${btn.dataset.delRun} 及其全部交易/权益数据？`)) return;
      try {
        await api('/api/admin/runs/' + encodeURIComponent(btn.dataset.delRun), { method: 'DELETE' });
        toast('已删除', 'ok');
        adminLoadRuns();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
}

async function adminDownload() {
  const btn = $('#dlBtn');
  btn.disabled = true;
  btn.textContent = '下载中…';
  try {
    const d = await api('/api/admin/data/download', {
      method: 'POST',
      body: {
        kind: $('#dlKind').value,
        symbol: $('#dlSymbol').value.trim().toUpperCase(),
        interval: $('#dlInterval').value.trim(),
        start: $('#dlStart').value,
        end: $('#dlEnd').value,
      },
    });
    toast(`任务已提交：${d.job_id}`, 'ok');
    setTimeout(adminLoadJobs, 1200);
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '开始下载';
  }
}

/* ------------------------------------------------------------------ */
/* 风控                                                                */
/* ------------------------------------------------------------------ */
async function adminLoadRisk() {
  const sys = await api('/api/admin/system');
  const on = sys.risk.kill_switch;
  $('#riskValue').textContent = on ? '已停机' : '正常运行';
  $('#riskValue').className = 'value ' + (on ? 'down' : 'up');
  $('#riskKillPath').textContent = (sys.service.cwd || '') + '/data/KILL';
}

async function adminRisk(action) {
  const reason = $('#riskReason').value.trim();
  if (action === 'halt' && !confirm('确认立即停机？实盘引擎将在 5 秒内撤单平仓并退出。')) return;
  try {
    const d = await api('/api/admin/risk', { method: 'POST', body: { action, reason } });
    toast(d.kill_switch ? '已停机' : '已恢复', 'ok');
    adminLoadRisk();
  } catch (err) { toast(err.message, 'err'); }
}

/* ------------------------------------------------------------------ */
/* 审计 / 系统                                                          */
/* ------------------------------------------------------------------ */
async function adminLoadAudit() {
  const d = await api('/api/admin/audit?limit=100');
  renderTable($('#auditTable'), [
    { title: '时间', render: r => fmt.datetime(r.ts * 1000), cls: 'mono' },
    { title: '用户', key: 'username' },
    { title: '动作', render: r => `<span class="badge">${r.action}</span>` },
    { title: '对象', key: 'target' },
    { title: '详情', render: r => `<span class="dim" style="font-size:11px">${(r.detail || '').slice(0, 60)}</span>` },
    { title: 'IP', key: 'ip', cls: 'mono' },
  ], d.items, '暂无记录');
}

async function adminLoadSystem(render = false) {
  const sys = await api('/api/admin/system');
  AdminState.system = sys;
  if (render) {
    const kv = (obj) => Object.entries(obj).map(([k, v]) =>
      `<div class="k">${k}</div><div class="v">${typeof v === 'object' ? JSON.stringify(v) : v}</div>`).join('');
    $('#sysService').innerHTML = kv(sys.service);
    $('#sysStorage').innerHTML = kv({ ...sys.storage, tables: JSON.stringify(sys.storage.tables) });
    $('#sysMicro').innerHTML = kv(sys.microstructure);
  }
  return sys;
}

/* ------------------------------------------------------------------ */
/* 标的库                                                              */
/* ------------------------------------------------------------------ */
const InstState = { types: [], items: [], editing: null, filterReady: false };
const TYPE_LABELS = {};

async function adminLoadInstruments() {
  const params = new URLSearchParams();
  const q = $('#instQuery').value.trim();
  const type = $('#instTypeFilter').value;
  if (q) params.set('q', q);
  if (type) params.set('type', type);
  const qs = params.toString();

  const d = await api('/api/admin/instruments' + (qs ? '?' + qs : ''));
  InstState.types = d.types || [];
  InstState.items = d.items || [];

  if (!InstState.filterReady) {
    const sel = $('#instTypeFilter');
    d.types.forEach(t => {
      TYPE_LABELS[t.value] = t.label;
      const opt = document.createElement('option');
      opt.value = t.value;
      opt.textContent = t.label;
      sel.appendChild(opt);
    });
    InstState.filterReady = true;
  }
  renderInstrumentTable();
}

function renderInstrumentTable() {
  const filtering = !!($('#instQuery').value.trim() || $('#instTypeFilter').value);
  const rows = InstState.items;

  renderTable($('#instrumentTable'), [
    {
      title: '排序', width: '80px',
      render: (r) => {
        if (filtering) return '<span class="dim" style="font-size:11px">—</span>';
        const i = InstState.items.indexOf(r);
        return `<button class="btn sm ghost" data-move="-1" data-i="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>` +
               `<button class="btn sm ghost" data-move="1" data-i="${i}" ${i === InstState.items.length - 1 ? 'disabled' : ''}>↓</button>`;
      },
    },
    { title: '代码', render: r => `<span class="mono">${r.code}</span>` },
    { title: '名称', key: 'name' },
    { title: '类型', render: r => `<span class="badge">${TYPE_LABELS[r.asset_type] || r.asset_type}</span>` },
    { title: '市场', key: 'market' },
    { title: '计价币', key: 'currency' },
    {
      title: 'tick / step', align: 'right',
      render: r => `<span class="mono" style="font-size:11px">${r.price_tick} / ${r.qty_step}</span>`,
    },
    {
      title: '状态',
      render: r => `<span class="badge ${r.is_active ? 'ok' : 'bad'}">${r.is_active ? '启用' : '停用'}</span>`,
    },
    {
      title: '操作', align: 'right',
      render: r => `<button class="btn sm ghost" data-edit="${r.id}">编辑</button>` +
                   `<button class="btn sm danger" data-del="${r.id}" data-code="${r.code}">删除</button>`,
    },
  ], rows, '还没有标的，点右上角「新增标的」或「批量导入」');

  $('#instSummary').textContent = filtering
    ? `筛选出 ${rows.length} 条（筛选状态下不显示排序按钮）`
    : `共 ${rows.length} 条标的`;

  $$('[data-move]').forEach(btn => {
    btn.onclick = () => instMove(Number(btn.dataset.i), Number(btn.dataset.move));
  });
  $$('[data-edit]').forEach(btn => {
    btn.onclick = () => {
      const item = InstState.items.find(i => String(i.id) === btn.dataset.edit);
      if (item) instModal(item);
    };
  });
  $$('[data-del]').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm(`删除标的 ${btn.dataset.code}？引用它的回测选择会失效。`)) return;
      try {
        await api('/api/admin/instruments/' + btn.dataset.del, { method: 'DELETE' });
        toast('已删除', 'ok');
        adminLoadInstruments();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
}

async function instMove(index, delta) {
  const items = InstState.items.slice();
  const target = index + delta;
  if (target < 0 || target >= items.length) return;
  [items[index], items[target]] = [items[target], items[index]];
  try {
    await api('/api/admin/instruments/order', {
      method: 'PUT',
      body: { ids: items.map(i => i.id) },
    });
    InstState.items = items;
    renderInstrumentTable();
  } catch (err) { toast(err.message, 'err'); }
}

function instFieldHtml(cur) {
  return `
    <div class="hint-box" id="instDupHint" style="display:none"></div>
    <div class="form-grid">
      <div class="field"><label>资产类型 *</label><select id="fType"></select></div>
      <div class="field"><label>市场 *</label><select id="fMarket"></select></div>
      <div class="field"><label>代码 *</label><input id="fCode" type="text" value="${cur.code || ''}" placeholder="如 600519 / BTCUSDT / RB2410" />
        <span class="dim" style="font-size:11px" id="fCodeHint"></span></div>
      <div class="field"><label>名称 *</label><input id="fName" type="text" value="${cur.name || ''}" placeholder="如 贵州茅台" /></div>
      <div class="field"><label>计价币</label><input id="fCurrency" type="text" value="${cur.currency || ''}" placeholder="CNY / USD / USDT" /></div>
      <div class="field"><label>状态</label><select id="fActive">
        <option value="true" ${cur.is_active !== false ? 'selected' : ''}>启用</option>
        <option value="false" ${cur.is_active === false ? 'selected' : ''}>停用</option></select></div>
      <div class="field"><label>price_tick</label><input id="fTick" type="number" step="any" value="${cur.price_tick ?? 0.01}" /></div>
      <div class="field"><label>qty_step</label><input id="fStep" type="number" step="any" value="${cur.qty_step ?? 1}" /></div>
      <div class="field"><label>min_notional</label><input id="fNotional" type="number" step="any" value="${cur.min_notional ?? 0}" /></div>
      <div class="field"><label>lot_size</label><input id="fLot" type="number" step="any" value="${cur.lot_size ?? 0}" /></div>
    </div>
    <div class="field" style="margin-top:10px"><label>扩展属性（JSON，可选）</label>
      <textarea id="fMeta" rows="2" placeholder='{"exchange":"BINANCE","sector":"白酒"}'>${Object.keys(cur.meta || {}).length ? JSON.stringify(cur.meta) : ''}</textarea></div>
    <div id="instIssues" style="margin-top:10px;font-size:12px"></div>`;
}

function instReadForm() {
  const type = $('#fType').value;
  const payload = {
    asset_type: type,
    market: $('#fMarket').value,
    code: $('#fCode').value.trim(),
    name: $('#fName').value.trim(),
    currency: $('#fCurrency').value.trim(),
    price_tick: $('#fTick').value,
    qty_step: $('#fStep').value,
    min_notional: $('#fNotional').value,
    lot_size: $('#fLot').value,
    is_active: $('#fActive').value === 'true',
  };
  const meta = $('#fMeta').value.trim();
  if (meta) payload.meta = meta;
  return payload;
}

function instSyncMarkets(keep) {
  const type = $('#fType').value;
  const spec = InstState.types.find(t => t.value === type);
  const sel = $('#fMarket');
  const prev = keep || sel.value;
  sel.innerHTML = spec.markets
    .map(m => `<option value="${m.value}" ${m.value === prev ? 'selected' : ''}>${m.label}（${m.value}）</option>`)
    .join('');
  if (!sel.value && spec.markets.length) sel.value = spec.default_market;
  if (!keep && !$('#fCurrency').value.trim()) $('#fCurrency').value = spec.default_currency;
  const market = spec.markets.find(m => m.value === sel.value) || spec.markets[0];
  $('#fCodeHint').textContent = market ? market.hint : '';
}

let instValidateTimer = null;

function instScheduleValidate() {
  clearTimeout(instValidateTimer);
  instValidateTimer = setTimeout(instValidate, 350);
}

async function instValidate() {
  const payload = instReadForm();
  if (!payload.code && !payload.name) return;
  try {
    const r = await api('/api/admin/instruments/validate', { method: 'POST', body: payload });
    const issues = r.issues || [];
    $('#instIssues').innerHTML = issues.length
      ? issues.map(i => `<div class="down">• ${i.field}：${i.message}</div>`).join('')
      : '<div class="up">• 校验通过</div>';

    const hint = $('#instDupHint');
    if (r.duplicate && r.existing) {
      hint.style.display = 'block';
      hint.innerHTML = `该标的已存在：<b>${r.existing.code}</b> ${r.existing.name}（ID ${r.existing.id}），保存会被拒绝。`;
    } else {
      hint.style.display = 'none';
    }
    return r.valid && !r.duplicate;
  } catch (err) {
    $('#instIssues').innerHTML = `<div class="down">• ${err.message}</div>`;
    return false;
  }
}

function instModal(item) {
  InstState.editing = item || null;
  const isEdit = !!item;
  const first = InstState.types[0];
  const cur = item || {
    asset_type: first.value,
    market: first.default_market,
    currency: first.default_currency,
    price_tick: 0.01, qty_step: 1, min_notional: 0, lot_size: 0,
    is_active: true, meta: {},
  };

  openModal(
    `<h3>${isEdit ? '编辑标的' : '新增标的'}</h3>${instFieldHtml(cur)}
     <div class="actions">
       <button class="btn" id="instCancel">取消</button>
       <button class="btn primary" id="instSave">${isEdit ? '保存' : '添加'}</button>
     </div>`,
    () => {
      const sel = $('#fType');
      sel.innerHTML = InstState.types.map(t => `<option value="${t.value}">${t.label}</option>`).join('');
      sel.value = cur.asset_type;
      instSyncMarkets(cur.market);
      if (!isEdit) $('#fCurrency').value = cur.currency;

      sel.onchange = () => { instSyncMarkets(); instScheduleValidate(); };
      $('#fMarket').onchange = () => {
        const spec = InstState.types.find(t => t.value === $('#fType').value);
        const m = spec.markets.find(x => x.value === $('#fMarket').value);
        if (m) $('#fCodeHint').textContent = m.hint;
        instScheduleValidate();
      };
      ['#fCode', '#fName', '#fCurrency', '#fTick', '#fStep', '#fNotional', '#fLot', '#fMeta'].forEach(s => {
        const el = $(s);
        if (el) el.oninput = instScheduleValidate;
      });

      $('#instCancel').onclick = closeModal;
      $('#instSave').onclick = async () => {
        const payload = instReadForm();
        const ok = await instValidate();
        if (!ok) { toast('请先修正校验问题', 'err'); return; }
        try {
          if (isEdit) {
            await api('/api/admin/instruments/' + item.id, { method: 'PATCH', body: payload });
            toast('已保存', 'ok');
          } else {
            await api('/api/admin/instruments', { method: 'POST', body: payload });
            toast('已添加', 'ok');
          }
          closeModal();
          adminLoadInstruments();
        } catch (err) {
          const details = err.details || [];
          if (details.length) {
            $('#instIssues').innerHTML = details.map(d => `<div class="down">• ${d.field}：${d.message}</div>`).join('');
          }
          toast(err.message, 'err');
        }
      };

      setTimeout(instValidate, 200);
    }
  );
}

function instBatchModal() {
  const sample = [
    'BTCUSDT,比特币,crypto',
    '600519,贵州茅台,stock,SH',
    '510300,沪深300ETF,fund,CN',
    'RB2410,螺纹钢,futures,SHFE',
    'EURUSD,欧元美元,forex',
  ].join('\n');
  openModal(
    `<h3>批量导入</h3>
     <div class="hint-box">
       每行一条，列顺序：<b>代码,名称,类型[,市场][,计价币][,price_tick][,qty_step]</b><br />
       支持逗号 / 制表符 / 空格分隔，以 <span class="mono">#</span> 开头为注释。重复项会自动跳过并计数。
     </div>
     <textarea id="batchText" rows="9" placeholder="${sample}"></textarea>
     <div id="batchResult" style="margin-top:12px;font-size:12px;max-height:200px;overflow:auto"></div>
     <div class="actions">
       <button class="btn" id="batchCancel">关闭</button>
       <button class="btn primary" id="batchOk">开始导入</button>
     </div>`,
    () => {
      $('#batchText').value = sample;
      $('#batchCancel').onclick = closeModal;
      $('#batchOk').onclick = async () => {
        const text = $('#batchText').value;
        if (!text.trim()) { toast('请填写内容', 'err'); return; }
        try {
          const r = await api('/api/admin/instruments/batch', { method: 'POST', body: { text } });
          const STATUS = {
            created: '<span class="badge ok">新增</span>',
            duplicate: '<span class="badge warn">跳过·重复</span>',
            invalid: '<span class="badge bad">无效</span>',
          };
          $('#batchResult').innerHTML =
            `<div style="margin-bottom:8px">共 ${r.total} 行：新增 <b class="up">${r.created}</b>，
              去重 <b class="warn">${r.duplicated}</b>，无效 <b class="down">${r.invalid}</b></div>` +
            r.items.map(it => `<div style="padding:3px 0;border-top:1px solid #1b2534">
              第 ${it.line} 行 <span class="mono">${it.code || '-'}</span> ${STATUS[it.status] || it.status}
              <span class="dim">${it.message || (it.issues ? it.issues.map(i => i.message).join('；') : '')}</span>
            </div>`).join('');
          toast(`导入完成：新增 ${r.created} 条`, 'ok');
          adminLoadInstruments();
        } catch (err) { toast(err.message, 'err'); }
      };
    }
  );
}

/* ------------------------------------------------------------------ */
/* 主数据管理（schema 驱动）                                            */
/* ------------------------------------------------------------------ */
const EntState = { entities: [], spec: null, items: [], editing: null, aiMode: 'local' };

async function adminLoadEntities() {
  const d = await api('/api/admin/entities');
  EntState.entities = d.items || [];
  EntState.aiMode = (d.ai && d.ai.mode) || 'local';

  const badge = $('#aiBadge');
  badge.textContent = EntState.aiMode === 'llm' ? 'AI: 大模型' : 'AI: 本地规则';
  badge.className = 'badge ' + (EntState.aiMode === 'llm' ? 'ok' : 'warn');
  $('#aiMode').textContent = EntState.aiMode === 'llm'
    ? `模型 ${d.ai.model}`
    : '未配置模型，使用确定性规则';

  const sel = $('#entSelect');
  const current = sel.value;
  sel.innerHTML = EntState.entities
    .map(e => `<option value="${e.key}">${e.icon || ''} ${e.label}</option>`)
    .join('');
  if (current && EntState.entities.some(e => e.key === current)) sel.value = current;

  await adminLoadEntityRows();
}

async function adminLoadEntityRows() {
  const key = $('#entSelect').value;
  if (!key) return;
  const q = $('#entQuery').value.trim();
  const qs = q ? '?q=' + encodeURIComponent(q) : '';

  const d = await api(`/api/admin/entities/${key}${qs}`);
  EntState.spec = d.spec;
  EntState.items = d.items || [];
  $('#entDesc').textContent = d.spec.description;
  renderEntTable();
}

function entEnumLabel(field, value) {
  const opt = (field.options || []).find(o => String(o.value) === String(value));
  return opt ? opt.label : value;
}

function entCell(field, value) {
  if (value === null || value === undefined || value === '') return '<span class="dim">—</span>';
  if (field.type === 'bool') {
    return `<span class="badge ${value ? 'ok' : 'bad'}">${value ? '是' : '否'}</span>`;
  }
  if (field.type === 'enum') return `<span class="badge">${entEnumLabel(field, value)}</span>`;
  if (field.type === 'json') {
    const text = JSON.stringify(value);
    return `<span class="mono" style="font-size:11px">${text.length > 40 ? text.slice(0, 40) + '…' : text}</span>`;
  }
  return String(value).length > 40 ? String(value).slice(0, 40) + '…' : String(value);
}

function renderEntTable() {
  const spec = EntState.spec;
  if (!spec) return;
  const show = spec.fields.filter(f => f.name !== 'note').slice(0, 6);
  const columns = show.map(f => ({
    title: f.label,
    render: r => entCell(f, r[f.name]),
  }));
  columns.push({
    title: '操作', align: 'right',
    render: r => `<button class="btn sm ghost" data-ent-edit="${r.id}">编辑</button>` +
                 `<button class="btn sm danger" data-ent-del="${r.id}" data-ent-name="${r.name || r.id}">删除</button>`,
  });

  renderTable($('#entTable'), columns, EntState.items, '暂无数据，点右上角「新增」');
  $('#entSummary').textContent = `共 ${EntState.items.length} 条`;

  $$('[data-ent-edit]').forEach(btn => {
    btn.onclick = () => {
      const item = EntState.items.find(i => String(i.id) === btn.dataset.entEdit);
      if (item) entModal(item);
    };
  });
  $$('[data-ent-del]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.entName;
      if (!confirm(`删除「${name}」？`)) return;
      try {
        const r = await api(`/api/admin/entities/${EntState.spec.key}/${btn.dataset.entDel}`, { method: 'DELETE' });
        const ex = await aiExplain('delete', true, `已删除 ${name}`, [], r.item);
        toast('已删除', 'ok');
        showExplanation('#aiResultBox', ex);
        adminLoadEntityRows();
      } catch (err) {
        toast(err.message, 'err');
      }
    };
  });
}

function entFieldInput(field, value) {
  const id = 'ef_' + field.name;
  const v = value !== undefined && value !== null ? value : (field.default ?? '');
  const attrs = `id="${id}" data-name="${field.name}"`;

  if (field.type === 'bool') {
    return `<select ${attrs}>
      <option value="true" ${v ? 'selected' : ''}>是</option>
      <option value="false" ${!v ? 'selected' : ''}>否</option></select>`;
  }
  if (field.type === 'enum') {
    const opts = (field.options || [])
      .map(o => `<option value="${o.value}" ${String(v) === String(o.value) ? 'selected' : ''}>${o.label}</option>`)
      .join('');
    return `<select ${attrs}>${opts}</select>`;
  }
  if (field.type === 'json' || field.type === 'text') {
    const text = field.type === 'json'
      ? (typeof v === 'string' ? v : JSON.stringify(v ?? {}))
      : String(v ?? '');
    return `<textarea ${attrs} rows="${field.type === 'json' ? 3 : 2}"
      placeholder='${field.placeholder || ''}'>${text}</textarea>`;
  }
  const inputType = (field.type === 'int' || field.type === 'float') ? 'number' : 'text';
  const step = field.type === 'float' ? 'step="any"' : '';
  return `<input ${attrs} type="${inputType}" ${step} value="${v ?? ''}" placeholder="${field.placeholder || ''}" />`;
}

function entModal(item) {
  const spec = EntState.spec;
  EntState.editing = item || null;
  const isEdit = !!item;

  const body = spec.fields
    .filter(f => !f.readonly)
    .map(f => `<div class="field" style="margin-bottom:10px">
        <label>${f.label}${f.required ? ' *' : ''}</label>
        ${entFieldInput(f, item ? item[f.name] : undefined)}
        ${f.help ? `<span class="dim" style="font-size:11px">${f.help}</span>` : ''}
      </div>`)
    .join('');

  openModal(
    `<h3>${isEdit ? '编辑' : '新增'}${spec.label}</h3>
     <div style="max-height:56vh;overflow:auto;padding-right:4px">${body}</div>
     <div id="entIssues" style="margin-top:6px;font-size:12px"></div>
     <div id="entAiHints" style="margin-top:6px;font-size:12px"></div>
     <div class="actions">
       <button class="btn" id="entCancel">取消</button>
       <button class="btn ghost" id="entAiFill">AI 填充</button>
       <button class="btn ghost" id="entAiFix">AI 纠错</button>
       <button class="btn primary" id="entSave">${isEdit ? '保存' : '添加'}</button>
     </div>`,
    () => {
      $('#entCancel').onclick = closeModal;
      $$('[data-name]', $('#modalBox')).forEach(el => {
        el.oninput = () => entScheduleValidate();
        el.onchange = () => entScheduleValidate();
      });
      $('#entAiFill').onclick = entAiFill;
      $('#entAiFix').onclick = entAiFix;
      $('#entSave').onclick = entSave;
      setTimeout(entValidate, 150);
    }
  );
}

function entCollect() {
  const out = {};
  const spec = EntState.spec;
  $$('[data-name]', $('#modalBox')).forEach(el => {
    const field = spec.fields.find(f => f.name === el.dataset.name);
    if (!field) return;
    if (field.type === 'bool') out[field.name] = el.value === 'true';
    else if (field.type === 'int' || field.type === 'float') out[field.name] = el.value === '' ? null : Number(el.value);
    else out[field.name] = el.value;
  });
  return out;
}

let entValidateTimer = null;
function entScheduleValidate() {
  clearTimeout(entValidateTimer);
  entValidateTimer = setTimeout(entValidate, 350);
}

async function entValidate() {
  const body = { ...entCollect(), _id: EntState.editing ? EntState.editing.id : undefined };
  try {
    const r = await api(`/api/admin/entities/${EntState.spec.key}/validate`, { method: 'POST', body });
    const issues = r.issues || [];
    const dup = r.duplicate;
    $('#entIssues').innerHTML = [
      ...issues.map(i => `<div class="down">• ${i.field}：${i.message}</div>`),
      ...(dup ? [`<div class="down">• ${dup.message}</div>`] : []),
    ].join('') || '<div class="up">• 校验通过</div>';
    return r.valid;
  } catch (err) {
    $('#entIssues').innerHTML = `<div class="down">• ${err.message}</div>`;
    return false;
  }
}

async function entSave() {
  const payload = entCollect();
  const valid = await entValidate();
  if (!valid) {
    const ex = await aiExplain(
      EntState.editing ? 'update' : 'create', false,
      '提交前校验未通过', [], payload,
    );
    showExplanation('#entAiHints', ex);
    toast('请先修正校验问题（可点「AI 纠错」）', 'err');
    return;
  }
  const key = EntState.spec.key;
  try {
    const r = EntState.editing
      ? await api(`/api/admin/entities/${key}/${EntState.editing.id}`, { method: 'PATCH', body: payload })
      : await api(`/api/admin/entities/${key}`, { method: 'POST', body: payload });
    const ex = await aiExplain(EntState.editing ? 'update' : 'create', true, `已保存 ${r.item.name || r.item.id}`, [], r.item);
    toast('已保存', 'ok');
    closeModal();
    showExplanation('#aiResultBox', ex);
    adminLoadEntityRows();
  } catch (err) {
    const issues = err.details || [];
    $('#entIssues').innerHTML = issues.map(i => `<div class="down">• ${i.field}：${i.message}</div>`).join('')
      || `<div class="down">• ${err.message}</div>`;
    const ex = await aiExplain(EntState.editing ? 'update' : 'create', false, err.message, issues, payload);
    showExplanation('#entAiHints', ex);
    toast(err.message, 'err');
  }
}

async function entAiFill() {
  const hint = prompt('用一句话描述这条数据（例如：币安主账户，合约，5 倍杠杆）');
  if (hint === null) return;
  try {
    const r = await api('/api/admin/ai/suggest', {
      method: 'POST',
      body: { entity: EntState.spec.key, partial: entCollect(), hint },
    });
    Object.entries(r.values || {}).forEach(([name, value]) => {
      const el = $('#ef_' + name);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!value;
      else if (typeof value === 'object') el.value = JSON.stringify(value);
      else el.value = String(value);
    });
    $('#entAiHints').innerHTML =
      `<div class="dim">[${r.mode === 'llm' ? '大模型' : '本地规则'}] ${(r.notes || []).join('；')}</div>`;
    entScheduleValidate();
    toast('已填充建议值', 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function entAiFix() {
  try {
    const r = await api('/api/admin/ai/fix', {
      method: 'POST',
      body: { entity: EntState.spec.key, payload: entCollect() },
    });
    Object.entries(r.payload || {}).forEach(([name, value]) => {
      const el = $('#ef_' + name);
      if (!el) return;
      el.value = typeof value === 'object' ? JSON.stringify(value) : String(value);
    });
    const changes = (r.changes || []).map(c => `${c.field}: ${c.from} → ${c.to}（${c.why}）`).join('<br />');
    const left = (r.unresolved || []).map(u => `${u.field}: ${u.message}`).join('<br />');
    $('#entAiHints').innerHTML =
      `<div class="dim">[${r.mode === 'llm' ? '大模型' : '本地规则'}] 已修复：<br />${changes || '无'}` +
      (left ? `<br /><span class="down">仍需人工处理：<br />${left}</span>` : '') + '</div>';
    entScheduleValidate();
    toast('已尝试自动修复', 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

/* ---------------- AI：自然语言增改 + 结果解读 ---------------- */
async function aiExplain(action, ok, message, issues, data) {
  try {
    return await api('/api/admin/ai/explain', {
      method: 'POST',
      body: {
        entity: EntState.spec ? EntState.spec.key : '',
        action, ok, message, issues,
        data: data || null,
      },
    });
  } catch {
    return { summary: message, suggestions: [], severity: ok ? 'ok' : 'error' };
  }
}

function showExplanation(selector, ex) {
  const host = $(selector);
  if (!host || !ex) return;
  const color = ex.severity === 'error' ? 'down' : ex.severity === 'warn' ? 'dim' : 'up';
  host.innerHTML =
    `<div class="${color}">${ex.summary || ''}</div>` +
    (ex.suggestions && ex.suggestions.length
      ? `<ul style="margin:6px 0 0 16px;padding:0">${ex.suggestions.map(s => `<li>${s}</li>`).join('')}</ul>`
      : '');
}

async function aiParseCommand() {
  const text = $('#aiCommand').value.trim();
  if (!text) { toast('请先输入指令', 'err'); return; }
  try {
    const r = await api('/api/admin/ai/command', { method: 'POST', body: { text } });
    const plan = r.plan || {};
    const issues = r.preview_issues || [];
    const actionLabel = { create: '新增', update: '修改', delete: '删除', list: '查询' }[plan.action] || plan.action;

    $('#aiPlanBox').innerHTML =
      `<div class="hint-box" style="margin-bottom:8px">
        <b>${actionLabel} · ${plan.entity || '未识别'}</b>${plan.id ? ` · id=${plan.id}` : ''}<br />
        ${plan.summary || ''}
        ${(plan.warnings || []).map(w => `<div class="down">! ${w}</div>`).join('')}
        ${issues.length ? `<div class="down">预览校验：${issues.map(i => i.message).join('；')}</div>` : ''}
        <div class="dim" style="margin-top:6px">来源：${r.mode === 'llm' ? '大模型' : '本地规则'}（置信度 ${r.confidence ?? '-'}）</div>
      </div>
      <button class="btn primary sm" id="aiApplyBtn" ${issues.length ? 'disabled' : ''}>确认执行</button>
      <button class="btn sm ghost" id="aiFillPlanBtn">填入表单</button>`;

    $('#aiFillPlanBtn').onclick = () => {
      if (plan.entity && plan.entity !== EntState.spec?.key) {
        $('#entSelect').value = plan.entity;
        adminLoadEntityRows().then(() => entModal(null));
      } else {
        entModal(null);
      }
      setTimeout(() => {
        Object.entries(plan.data || {}).forEach(([name, value]) => {
          const el = $('#ef_' + name);
          if (el) el.value = typeof value === 'object' ? JSON.stringify(value) : String(value);
        });
      }, 300);
    };

    $('#aiApplyBtn').onclick = async () => {
      try {
        const applied = await api('/api/admin/ai/apply', { method: 'POST', body: { plan } });
        toast('执行成功', 'ok');
        showExplanation('#aiResultBox', applied.explanation);
        $('#aiPlanBox').innerHTML = '';
        adminLoadEntities();
      } catch (err) {
        const ex = (err.details && err.details.explanation) || await aiExplain(plan.action, false, err.message, [], null);
        showExplanation('#aiResultBox', ex);
        toast(err.message, 'err');
      }
    };
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */
window.initAdmin = function initAdmin() {
  $('#loginForm').addEventListener('submit', adminLogin);
  $('#logoutBtn').addEventListener('click', async (e) => {
    e.preventDefault();
    try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* 忽略 */ }
    forceLogout('');
  });

  $$('.nav-item[data-view]').forEach(el => el.addEventListener('click', () => switchAdminView(el.dataset.view)));

  $('#newUserBtn').onclick = adminNewUser;

  // 主数据管理
  $('#entSelect').onchange = adminLoadEntityRows;
  $('#entRefresh').onclick = adminLoadEntityRows;
  $('#entNew').onclick = () => entModal(null);
  $('#entQuery').addEventListener('keydown', e => { if (e.key === 'Enter') adminLoadEntityRows(); });
  $('#aiParseBtn').onclick = aiParseCommand;

  // 标的库
  $('#instNewBtn').onclick = () => instModal(null);
  $('#instBatchBtn').onclick = instBatchModal;
  $('#instRefresh').onclick = adminLoadInstruments;
  $('#instTypeFilter').onchange = adminLoadInstruments;
  $('#instQuery').addEventListener('keydown', e => {
    if (e.key === 'Enter') adminLoadInstruments();
  });
  $('#cfgProfile').onchange = e => adminLoadConfig(e.target.value);
  $('#cfgReload').onclick = () => adminLoadConfig(AdminState.cfgProfile);
  $('#cfgSave').onclick = adminSaveConfig;
  $('#dlBtn').onclick = adminDownload;
  $('#jobRefresh').onclick = adminLoadJobs;
  $('#haltBtn').onclick = () => adminRisk('halt');
  $('#resumeBtn').onclick = () => adminRisk('resume');
  $('#auditRefresh').onclick = adminLoadAudit;
  $('#modalHost').addEventListener('click', e => { if (e.target.id === 'modalHost') closeModal(); });

  const today = new Date();
  const past = new Date(Date.now() - 30 * 86400000);
  $('#dlEnd').value = today.toISOString().slice(0, 10);
  $('#dlStart').value = past.toISOString().slice(0, 10);

  if (Store.token) {
    adminBoot();
  } else {
    $('#loginView').style.display = 'flex';
  }
};
