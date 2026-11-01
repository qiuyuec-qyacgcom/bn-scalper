/* scalpx 前台：无框架、无构建，纯 DOM + Canvas */
'use strict';

/* ------------------------------------------------------------------ */
/* 通用工具                                                            */
/* ------------------------------------------------------------------ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const fmt = {
  pct(v, d = 2) { return (v == null || Number.isNaN(v)) ? '—' : (v * 100).toFixed(d) + '%'; },
  num(v, d = 2) { return (v == null || Number.isNaN(v)) ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); },
  int(v) { return v == null ? '—' : Number(v).toLocaleString('en-US'); },
  money(v) { return v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); },
  ts(ms) {
    if (!ms) return '—';
    const d = new Date(Number(ms));
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  },
  datetime(ms) {
    if (!ms) return '—';
    const d = new Date(Number(ms));
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  },
  sign(v, fn) { return (v > 0 ? '+' : '') + fn(v); },
};

function cls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : 'dim'; }

function toast(msg, kind = '') {
  const host = $('#toastHost');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, 3600);
}

/* ------------------------------------------------------------------ */
/* API 客户端                                                          */
/* ------------------------------------------------------------------ */
const Store = {
  get token() { return localStorage.getItem('scalpx.token') || ''; },
  set token(v) { v ? localStorage.setItem('scalpx.token', v) : localStorage.removeItem('scalpx.token'); },
  get user() { try { return JSON.parse(localStorage.getItem('scalpx.user') || 'null'); } catch { return null; } },
  set user(v) { v ? localStorage.setItem('scalpx.user', JSON.stringify(v)) : localStorage.removeItem('scalpx.user'); },
};

async function api(path, { method = 'GET', body = null } = {}) {
  const headers = { 'Accept': 'application/json' };
  if (body) headers['Content-Type'] = 'application/json';
  if (Store.token) headers['Authorization'] = 'Bearer ' + Store.token;

  const resp = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let payload = null;
  try { payload = await resp.json(); } catch { /* 非 JSON（静态资源等） */ }

  if (resp.status === 401 && path !== '/api/auth/login') {
    forceLogout('会话已过期，请重新登录');
    throw new Error('未登录');
  }
  if (!resp.ok || (payload && payload.ok === false)) {
    const msg = payload?.error?.message || `请求失败 (${resp.status})`;
    const code = payload?.error?.code || 'ERROR';
    const err = new Error(msg);
    err.code = code;
    err.details = payload?.error?.details;
    throw err;
  }
  return payload ? payload.data : null;
}

/* ------------------------------------------------------------------ */
/* 登录/登出                                                           */
/* ------------------------------------------------------------------ */
function forceLogout(message) {
  Store.token = '';
  Store.user = null;
  $('#appView').style.display = 'none';
  $('#loginView').style.display = 'flex';
  if (message) $('#loginErr').textContent = message;
}

async function doLogin(ev) {
  ev.preventDefault();
  const username = $('#loginUser').value.trim();
  const password = $('#loginPass').value;
  $('#loginErr').textContent = '';
  try {
    const data = await api('/api/auth/login', { method: 'POST', body: { username, password } });
    Store.token = data.token;
    Store.user = data.user;
    await bootstrap();
  } catch (err) {
    $('#loginErr').textContent = err.message;
  }
}

async function doLogout(ev) {
  ev.preventDefault();
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* 忽略 */ }
  forceLogout('');
}

/* ------------------------------------------------------------------ */
/* 图表：折线 + 面积 + 悬浮十字线                                       */
/* ------------------------------------------------------------------ */
function drawChart(canvas, points, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 600;
  const cssH = canvas.clientHeight || 260;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const P = { l: 58, r: 14, t: 14, b: 26 };
  const w = cssW - P.l - P.r;
  const h = cssH - P.t - P.b;

  if (!points || points.length < 2) {
    ctx.fillStyle = '#5d7189';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('暂无数据', cssW / 2, cssH / 2);
    return;
  }

  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  const pad = (yMax - yMin) * 0.12 || Math.abs(yMax || 1) * 0.01;
  yMin -= pad; yMax += pad;
  const X = v => P.l + ((v - xs[0]) / (xs[xs.length - 1] - xs[0] || 1)) * w;
  const Y = v => P.t + h - ((v - yMin) / (yMax - yMin || 1)) * h;

  // 网格
  ctx.strokeStyle = '#1b2534';
  ctx.lineWidth = 1;
  ctx.font = '11px monospace';
  ctx.fillStyle = '#5d7189';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const y = P.t + (h * i) / 4;
    ctx.beginPath(); ctx.moveTo(P.l, y); ctx.lineTo(P.l + w, y); ctx.stroke();
    const val = yMax - ((yMax - yMin) * i) / 4;
    ctx.fillText(opts.fmtY ? opts.fmtY(val) : val.toFixed(0), P.l - 8, y + 4);
  }

  // 面积
  const color = opts.color || '#3ea6ff';
  const grad = ctx.createLinearGradient(0, P.t, 0, P.t + h);
  grad.addColorStop(0, color + '44');
  grad.addColorStop(1, color + '00');
  ctx.beginPath();
  ctx.moveTo(X(xs[0]), Y(ys[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(X(xs[i]), Y(ys[i]));
  ctx.lineTo(X(xs[xs.length - 1]), P.t + h);
  ctx.lineTo(X(xs[0]), P.t + h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // 折线
  ctx.beginPath();
  ctx.moveTo(X(xs[0]), Y(ys[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(X(xs[i]), Y(ys[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // X 轴时间
  ctx.fillStyle = '#5d7189';
  ctx.textAlign = 'center';
  const ticks = 5;
  for (let i = 0; i < ticks; i++) {
    const x = P.l + (w * i) / (ticks - 1);
    const idx = Math.min(points.length - 1, Math.round(((points.length - 1) * i) / (ticks - 1)));
    ctx.fillText(opts.fmtX ? opts.fmtX(points[idx].x) : String(points[idx].x), x, cssH - 8);
  }

  // 交互
  canvas.onmousemove = ev => {
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    if (mx < P.l || mx > P.l + w) return;
    const ratio = (mx - P.l) / w;
    const idx = Math.min(points.length - 1, Math.max(0, Math.round(ratio * (points.length - 1))));
    drawChart(canvas, points, { ...opts, hover: idx });
  };
  canvas.onmouseleave = () => drawChart(canvas, points, opts);

  if (opts.hover != null && points[opts.hover]) {
    const p = points[opts.hover];
    const px = X(p.x), py = Y(p.y);
    ctx.strokeStyle = '#3ea6ff66';
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(px, P.t); ctx.lineTo(px, P.t + h); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2); ctx.fill();

    const text = (opts.fmtTip ? opts.fmtTip(p) : p.y.toFixed(2));
    ctx.font = '12px monospace';
    const tw = ctx.measureText(text).width + 16;
    const bx = Math.min(px + 10, P.l + w - tw);
    ctx.fillStyle = '#141b26';
    ctx.strokeStyle = '#223047';
    ctx.beginPath();
    ctx.roundRect ? ctx.roundRect(bx, py - 26, tw, 22, 5) : ctx.rect(bx, py - 26, tw, 22);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#e6edf7';
    ctx.textAlign = 'left';
    ctx.fillText(text, bx + 8, py - 10);
  }
}

/* ------------------------------------------------------------------ */
/* 渲染：表格                                                          */
/* ------------------------------------------------------------------ */
function renderTable(table, columns, rows, emptyText = '暂无数据') {
  if (!rows || rows.length === 0) {
    table.innerHTML = `<tbody><tr><td class="empty" colspan="${columns.length}">${emptyText}</td></tr></tbody>`;
    return;
  }
  const head = `<thead><tr>${columns.map(c => `<th style="text-align:${c.align || 'left'}">${c.title}</th>`).join('')}</tr></thead>`;
  const body = rows.map(r => `<tr>${columns.map(c => `<td style="text-align:${c.align || 'left'}" class="${c.cls || ''}">${c.render ? c.render(r) : (r[c.key] ?? '—')}</td>`).join('')}</tr>`).join('');
  table.innerHTML = head + `<tbody>${body}</tbody>`;
}

/* ------------------------------------------------------------------ */
/* 状态                                                                */
/* ------------------------------------------------------------------ */
const State = { runId: '', runs: [], view: 'dashboard', tradeOffset: 0 };

/* ------------------------------------------------------------------ */
/* 视图切换                                                            */
/* ------------------------------------------------------------------ */
const VIEW_TITLES = {
  dashboard: ['总览', '权益、回撤与出场结构'],
  trades: ['交易明细', '逐笔盈亏与出场原因'],
  signals: ['信号流', '入场判定与拒绝原因'],
  micro: ['微观结构', 'CVD 与盘口数据覆盖'],
  runs: ['运行记录', '历史回测与归属'],
};

function switchView(name) {
  State.view = name;
  $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  $$('.view').forEach(el => el.classList.toggle('active', el.id === 'view-' + name));
  const [title, sub] = VIEW_TITLES[name] || ['', ''];
  $('#pageTitle').textContent = title;
  $('#pageSub').textContent = sub;
  loadView(name);
}

function loadView(name) {
  if (name === 'dashboard') loadDashboard();
  else if (name === 'trades') loadTrades(0);
  else if (name === 'signals') loadSignals();
  else if (name === 'micro') loadMicro();
  else if (name === 'runs') loadRuns();
}

/* ------------------------------------------------------------------ */
/* 数据加载                                                            */
/* ------------------------------------------------------------------ */
async function loadRuns() {
  const data = await api('/api/runs?limit=50');
  State.runs = data.items || [];

  const sel = $('#runSelect');
  const current = State.runId;
  sel.innerHTML = State.runs.length
    ? State.runs.map(r => `<option value="${r.run_id}">${r.run_id}</option>`).join('')
    : '<option value="">（暂无运行）</option>';

  if (State.runs.length) {
    const exists = State.runs.some(r => r.run_id === current);
    State.runId = exists ? current : State.runs[0].run_id;
    sel.value = State.runId;
  } else {
    State.runId = '';
  }

  renderTable($('#runTable'), [
    { title: '运行 ID', key: 'run_id', cls: 'mono' },
    { title: '策略', key: 'strategy' },
    { title: '标的', render: r => (r.symbols || []).join(',') },
    { title: '周期', key: 'interval' },
    { title: '开始时间', render: r => fmt.datetime(r.started_ts), cls: 'mono' },
    { title: '状态', render: r => `<span class="badge ${r.status === 'finished' ? 'ok' : 'warn'}">${r.status}</span>` },
    { title: '收益', align: 'right', render: r => `<span class="${cls(r.total_return)}">${fmt.pct(r.total_return)}</span>` },
    { title: '笔数', align: 'right', render: r => fmt.int(r.trades) },
    { title: '', align: 'right', render: r => `<button class="btn sm" data-open-run="${r.run_id}">查看</button>` },
  ], State.runs, '还没有运行记录，点右上角「运行新回测」');

  $$('[data-open-run]').forEach(btn => btn.onclick = () => {
    State.runId = btn.dataset.openRun;
    $('#runSelect').value = State.runId;
    switchView('dashboard');
  });
}

async function loadDashboard() {
  if (!State.runId) {
    ['kpiReturn', 'kpiMdd', 'kpiWin', 'kpiR'].forEach(id => $('#' + id).textContent = '—');
    $('#recentTrades').innerHTML = '<tbody><tr><td class="empty">暂无运行数据</td></tr></tbody>';
    drawChart($('#equityChart'), []);
    $('#exitList').innerHTML = '<span class="dim">—</span>';
    $('#riskList').innerHTML = '<span class="dim">—</span>';
    return;
  }

  const d = await api(`/api/dashboard?run_id=${encodeURIComponent(State.runId)}`);
  const k = d.kpi || {};

  $('#kpiReturn').textContent = fmt.pct(k.total_return);
  $('#kpiReturn').className = 'value ' + cls(k.total_return);
  $('#kpiEquity').textContent = `期末权益 ${fmt.money(k.final_equity)}`;

  $('#kpiMdd').textContent = fmt.pct(-Math.abs(k.max_drawdown || 0));
  $('#kpiMdd').className = 'value down';
  $('#kpiWin').textContent = fmt.pct(k.win_rate);
  $('#kpiPf').textContent = `盈亏比 ${fmt.num(k.profit_factor)} · 夏普 ${fmt.num(k.sharpe)}`;
  $('#kpiR').textContent = fmt.num(k.avg_r, 3) + 'R';
  $('#kpiR').className = 'value ' + cls(k.avg_r);
  $('#kpiTrades').textContent = `${fmt.int(k.trades)} 笔 · 费用占权益 ${fmt.pct(k.fee_drag_pct)}`;

  const eq = (d.equity || []).map(p => ({ x: p.ts, y: p.equity }));
  drawChart($('#equityChart'), eq, {
    color: k.total_return >= 0 ? '#2ecc71' : '#ff5d5d',
    fmtY: v => fmt.money(v),
    fmtX: v => fmt.ts(v),
    fmtTip: p => `权益 ${fmt.money(p.y)} · ${fmt.datetime(p.x)}`,
  });

  const exits = d.exit_breakdown || {};
  const total = Object.values(exits).reduce((a, b) => a + b, 0) || 1;
  const NAMES = {
    stop_loss: '止损', take_profit: '止盈', time_stop: '时间止损', signal_flip: '结构反转',
    trailing_stop: '移动止损', risk_liquidation: '风控强平', manual: '手动', kill_switch: '熔断', end_of_data: '数据结束',
  };
  $('#exitList').innerHTML = Object.entries(exits).length
    ? Object.entries(exits).sort((a, b) => b[1] - a[1]).map(([k2, v]) => {
        const p = (v / total) * 100;
        return `<div style="margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;font-size:12px">
            <span>${NAMES[k2] || k2}</span><span class="mono">${v} · ${p.toFixed(0)}%</span>
          </div>
          <div style="height:4px;background:#1b2534;border-radius:2px;margin-top:4px">
            <div style="width:${p}%;height:4px;background:#3ea6ff;border-radius:2px"></div>
          </div></div>`;
      }).join('')
    : '<span class="dim">—</span>';

  const risk = d.risk_events || [];
  $('#riskList').innerHTML = risk.length
    ? risk.map(r => `<div style="margin-bottom:6px">
        <span class="badge ${r.level === 'ERROR' ? 'bad' : 'warn'}">${r.code}</span>
        <span class="dim" style="margin-left:6px">${fmt.ts(r.ts)}</span>
        <div style="color:#8aa0bb">${r.message || ''}</div></div>`).join('')
    : '<span class="dim">无</span>';

  renderTable($('#recentTrades'), [
    { title: '平仓时间', render: t => fmt.datetime(t.exit_ts), cls: 'mono' },
    { title: '标的', key: 'symbol' },
    { title: '方向', render: t => `<span class="badge ${t.side === 'long' ? 'ok' : 'bad'}">${t.side === 'long' ? '多' : '空'}</span>` },
    { title: '开仓价', align: 'right', render: t => fmt.num(t.entry_price) },
    { title: '平仓价', align: 'right', render: t => fmt.num(t.exit_price) },
    { title: '盈亏', align: 'right', render: t => `<span class="${cls(t.pnl)}">${fmt.sign(t.pnl, v => fmt.num(v))}</span>` },
    { title: 'R', align: 'right', render: t => `<span class="${cls(t.r_multiple)}">${fmt.num(t.r_multiple, 2)}</span>` },
    { title: '出场', render: t => `<span class="badge">${NAMES[t.reason] || t.reason}</span>` },
  ], d.recent_trades || [], '暂无交易');
}

async function loadTrades(offset = 0) {
  if (!State.runId) return;
  State.tradeOffset = offset;
  const limit = Number($('#tradeLimit').value || 50);
  const symbol = $('#tradeSymbol').value.trim().toUpperCase();
  const qs = new URLSearchParams({ run_id: State.runId, limit: String(limit), offset: String(offset) });
  if (symbol) qs.set('symbol', symbol);

  const d = await api('/api/trades?' + qs.toString());
  const NAMES = { stop_loss: '止损', take_profit: '止盈', time_stop: '时间止损', signal_flip: '结构反转', trailing_stop: '移动止损', risk_liquidation: '风控强平' };
  renderTable($('#tradeTable'), [
    { title: '开仓', render: t => fmt.datetime(t.entry_ts), cls: 'mono' },
    { title: '平仓', render: t => fmt.datetime(t.exit_ts), cls: 'mono' },
    { title: '标的', key: 'symbol' },
    { title: '方向', render: t => t.side === 'long' ? '多' : '空' },
    { title: '数量', align: 'right', render: t => fmt.num(t.qty, 4) },
    { title: '开仓价', align: 'right', render: t => fmt.num(t.entry_price) },
    { title: '平仓价', align: 'right', render: t => fmt.num(t.exit_price) },
    { title: '盈亏', align: 'right', render: t => `<span class="${cls(t.pnl)}">${fmt.sign(t.pnl, v => fmt.num(v))}</span>` },
    { title: 'R', align: 'right', render: t => fmt.num(t.r_multiple, 2) },
    { title: '持仓(分)', align: 'right', render: t => fmt.num(((t.exit_ts - t.entry_ts) / 60000), 1) },
    { title: '出场', render: t => NAMES[t.reason] || t.reason },
  ], d.items, '该筛选条件下没有交易');

  $('#tradePager').textContent = `共 ${d.total} 笔，当前 ${offset + 1}–${Math.min(offset + limit, d.total)}`;
}

async function loadSignals() {
  if (!State.runId) return;
  const d = await api(`/api/signals?run_id=${encodeURIComponent(State.runId)}&limit=50`);
  renderTable($('#signalTable'), [
    { title: '时间', render: s => fmt.datetime(s.ts), cls: 'mono' },
    { title: '标的', key: 'symbol' },
    { title: '动作', render: s => {
        const map = { entry: ['ok', '入场'], rejected: ['warn', '被拒'], exit: ['', '出场'] };
        const [c, label] = map[s.action] || ['', s.action];
        return `<span class="badge ${c}">${label}</span>`;
      } },
    { title: '方向', render: s => s.side === 'buy' ? '买' : s.side === 'sell' ? '卖' : '—' },
    { title: '价格', align: 'right', render: s => fmt.num(s.price) },
    { title: '微观来源', render: s => {
        const m = s.meta && s.meta.micro_mode;
        return m ? `<span class="badge">${m}</span>` : '—';
      } },
    { title: '说明', render: s => `<span class="dim" style="font-size:12px">${s.reason || (s.meta && s.meta.code) || ''}</span>` },
  ], d.items, '暂无信号记录');
}

async function loadMicro() {
  const symbol = $('#microSymbol').value.trim().toUpperCase() || '';
  const d = await api('/api/micro' + (symbol ? `?symbol=${symbol}` : ''));
  $('#microBars').textContent = fmt.int(d.flow.bars || 0);
  $('#microBooks').textContent = fmt.int(d.flow.book_snapshots || 0);
  $('#microTrades').textContent = fmt.int(d.flow.trades || 0);
  if (!symbol) $('#microSymbol').value = d.symbol || '';

  const pts = (d.cvd || []).map(p => ({ x: p.ts, y: p.cvd }));
  drawChart($('#cvdChart'), pts, {
    color: '#6ee7b7',
    fmtY: v => fmt.num(v, 0),
    fmtX: v => fmt.ts(v),
    fmtTip: p => `CVD ${fmt.num(p.y, 1)}`,
  });
}

/** 拉取标的库：填充筛选补全与回测标的选择 */
async function loadInstruments() {
  let items = [];
  try {
    const d = await api('/api/instruments?active_only=1');
    items = d.items || [];
  } catch { return; }

  const dl = $('#instrumentList');
  if (dl) {
    dl.innerHTML = items.map(i => `<option value="${i.code}">${i.name}</option>`).join('');
  }

  const sel = $('#backtestSymbols');
  if (sel) {
    const keep = new Set(Array.from(sel.selectedOptions).map(o => o.value));
    sel.innerHTML = items.length
      ? items.map(i => `<option value="${i.code}" ${keep.has(i.code) ? 'selected' : ''}>${i.code} · ${i.name}</option>`).join('')
      : '<option value="" disabled>（标的库为空，请在后台添加）</option>';
  }
}

async function submitBacktest() {
  const btn = $('#runBacktestBtn');
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const sel = $('#backtestSymbols');
    const symbols = sel ? Array.from(sel.selectedOptions).map(o => o.value).filter(Boolean) : [];
    const body = { profile: 'backtest' };
    if (symbols.length) body.symbols = symbols;
    const d = await api('/api/backtest', { method: 'POST', body });
    toast(`已提交回测任务，排队中…`, 'ok');
    pollJob(d.job_id);
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '运行新回测';
  }
}

async function pollJob(jobId) {
  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const jobs = await api('/api/admin/jobs').catch(() => null);
      // 普通用户没有 jobs 权限：退化为轮询 runs
      if (jobs && jobs.items) {
        const job = jobs.items.find(j => j.id === jobId);
        if (job) {
          if (job.status === 'done') {
            toast('回测完成', 'ok');
            await loadRuns();
            switchView('dashboard');
            return;
          }
          if (job.status === 'error') {
            toast('回测失败：' + job.error, 'err');
            return;
          }
          continue;
        }
      }
      await loadRuns();
      const latest = State.runs[0];
      if (latest && String(latest.run_id).startsWith('web-')) {
        toast('回测完成', 'ok');
        switchView('dashboard');
        return;
      }
    } catch { /* 继续等 */ }
  }
  toast('等待超时，请稍后刷新', 'err');
}

/* ------------------------------------------------------------------ */
/* 引导                                                                */
/* ------------------------------------------------------------------ */
async function bootstrap() {
  $('#loginView').style.display = 'none';
  $('#appView').style.display = 'flex';

  let me;
  try { me = await api('/api/auth/me'); } catch { return; }
  Store.user = { id: me.id, username: me.username, role: me.role };

  $('#userName').textContent = me.username;
  const roleEl = $('#userRole');
  roleEl.textContent = me.role === 'admin' ? '管理员' : '用户';
  roleEl.className = 'role-badge' + (me.role === 'admin' ? ' admin' : '');
  if (me.role === 'admin') $('#adminEntry').style.display = 'flex';

  try {
    const health = await api('/api/health');
    $('#footVersion').textContent = health.version;
  } catch { /* 忽略 */ }

  await loadInstruments();
  await loadRuns();
  switchView('dashboard');
}

function init() {
  // 后台页面复用同一个 app.js（API 客户端/格式化/表格），由 admin.js 接管初始化
  if (typeof window.initAdmin === 'function') { window.initAdmin(); return; }

  $('#loginForm').addEventListener('submit', doLogin);
  $('#logoutBtn').addEventListener('click', doLogout);

  $$('.nav-item[data-view]').forEach(el => {
    el.addEventListener('click', () => switchView(el.dataset.view));
  });
  $$('[data-view]').forEach(el => {
    if (el.classList.contains('btn')) {
      el.addEventListener('click', () => switchView(el.dataset.view));
    }
  });

  $('#runSelect').addEventListener('change', e => {
    State.runId = e.target.value;
    loadView(State.view);
  });
  $('#refreshBtn').addEventListener('click', () => loadView(State.view));
  $('#tradeQuery').addEventListener('click', () => loadTrades(0));
  $('#tradeLimit').addEventListener('change', () => loadTrades(0));
  $('#microQuery').addEventListener('click', loadMicro);
  $('#runBacktestBtn').addEventListener('click', submitBacktest);

  setInterval(() => {
    if (State.view === 'dashboard' && State.runId && Store.token) {
      loadDashboard().catch(() => {});
    }
  }, 15000);

  if (Store.token) {
    bootstrap();
  } else {
    $('#loginView').style.display = 'flex';
  }
}

document.addEventListener('DOMContentLoaded', init);
