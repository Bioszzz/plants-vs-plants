/* 植物斗争 —— 本地试玩台前端（playtest.py 的配置页）
 *
 * 这里【只有配置与观战】，一行规则都不实现：
 *   开局 → POST /api/lab/start  → 后台起 netserver.Room + N 个座位页
 *   战报 → SSE /api/lab/events  （订阅的就是 netserver 的 ConsoleBus）
 *   座位 → GET /api/lab/status  （哪个座位连上了 / 各自页面地址）
 *
 * ★ 规则勾选框【不写死】—— 按服务端下发的 rule_options 动态生成，
 *   和 房主控制台(host.js) / 网页版(app.js) 是同一套元数据。
 *   加一条规则只改 plants_data.RULE_OPTIONS，这里不用动。
 */

'use strict';

const $ = (id) => document.getElementById(id);
const LABEL = { idle: '未开局', waiting: '等人连接', playing: '对局中', ended: '已结束' };
const CACHE_KEY = 'pvp.lab.v1';

let RULE_OPTIONS = [];
let STATUS = { state: 'idle' };
let formInit = false;
let polling = false;

/* ---------- 缓存：上次配的参数/名字/规则 ---------- */
function loadCache() {
  try { return JSON.parse(localStorage.getItem(CACHE_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}
function saveCache(obj) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(obj)); } catch (e) {}
}
function int(v, d) {
  const n = parseInt(v, 10);
  return isNaN(n) ? d : n;
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------- 真人座位数 ---------- */
function humans() {
  const t = Math.max(2, int($('f-total').value, 3));
  const b = Math.min(t, Math.max(0, int($('f-bots').value, 0)));
  return Math.max(1, t - b);
}

/* ---------- 座位名字（数量跟着人数走） ----------
 * prefer：优先用这批名字填（刷新页面时用【服务端真正在跑的】座位名，
 *        否则输入框显示"真人1/2/3"，而战报里是阿甲/阿乙/阿丙 —— 看着像配错了）。
 * 没给 prefer 就按"用户刚输入的 > 上次缓存的 > 默认"取值。 */
function renderNames(prefer) {
  const n = humans();
  const box = $('f-names');
  const keep = Array.from(box.querySelectorAll('input')).map(i => i.value);
  const cache = loadCache().names || [];
  box.innerHTML = '';
  for (let i = 0; i < n; i++) {
    const lab = document.createElement('label');
    lab.className = 'field';
    const sp = document.createElement('span');
    sp.textContent = (i + 1) + ' 号位';
    const inp = document.createElement('input');
    inp.maxLength = 16;
    inp.spellcheck = false;
    inp.autocomplete = 'off';
    inp.value = (prefer && prefer[i]) || keep[i] || cache[i] || ('真人' + (i + 1));
    lab.appendChild(sp);
    lab.appendChild(inp);
    box.appendChild(lab);
  }
}

/* ---------- 规则勾选框（动态） ---------- */
function ensureRules(opts, defs) {
  if (RULE_OPTIONS.length || !opts || !opts.length) return;
  RULE_OPTIONS = opts;
  const cache = loadCache().rules || {};
  const box = $('f-rules');
  box.innerHTML = '';
  RULE_OPTIONS.forEach(o => {
    const lab = document.createElement('label');
    lab.className = 'rulecheck';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'f-rule-' + o.key;
    cb.checked = (o.key in cache) ? !!cache[o.key] : !!o.default_on;
    lab.appendChild(cb);
    // 【v0.73】名称 + 一行简介（一直显示，不再只藏在 title 里）——
    //   用户 2026-10-27："规则集添加简介"。勾选框只占左边一格，文字竖排两行。
    const tx = document.createElement('div');
    tx.className = 'rule-text';
    const sp = document.createElement('span');
    sp.className = 'rule-name';
    sp.textContent = o.name;
    tx.appendChild(sp);
    if (o.desc) {
      const dc = document.createElement('span');
      dc.className = 'rule-desc';
      dc.textContent = o.desc;
      tx.appendChild(dc);
    }
    lab.appendChild(tx);
    box.appendChild(lab);
  });
}

/* 发给服务端的 rules：键 → 数值（on/off 由数据层定，前端只管勾没勾） */
function ruleValues() {
  const out = {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    out[o.key] = (cb && cb.checked) ? o.on : o.off;
  });
  return out;
}

/* 写缓存用的布尔快照 —— 存布尔而不是数值：将来 on/off 改了缓存照样能用 */
function ruleToggles() {
  const out = {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    if (cb) out[o.key] = !!cb.checked;
  });
  return out;
}

/* ---------- 座位表 ---------- */
function renderSeats(seats) {
  const box = $('seats');
  if (!seats || !seats.length) {
    box.innerHTML = '<div class="hint">还没开局 —— 开局后每个真人座位会在这里列出自己的页面地址。</div>';
    return;
  }
  box.innerHTML = seats.map(s => {
    const on = !!s.connected;
    return '<div class="seat-row">' +
      '<span class="seat-no">' + s.idx + '</span>' +
      '<b>' + esc(s.name) + '</b>' +
      '<a class="seat-url" href="' + esc(s.url) + '" target="_blank" rel="noopener">' +
        esc(s.url) + '</a>' +
      '<span class="seat-state ' + (on ? 'on' : 'off') + '">' +
        (on ? '已连接' : '未连接') + '</span>' +
      '</div>';
  }).join('');
}

/* ---------- 状态 ---------- */
function applyStatus(st) {
  if (!st) return;
  STATUS = Object.assign(STATUS, st);
  if (st.rule_options) ensureRules(st.rule_options, st.rules_default);

  const busy = (st.state === 'waiting' || st.state === 'playing');
  // ★ 只有带 seats 的那份状态才算"完整"（= GET /api/lab/status 的返回）。
  //   SSE 一订阅就先回放一条 Room 的裸 status（不带 seats），要是拿它去填表单，
  //   名字框会退回默认的"真人1/2/3"，跟战报里的真名对不上。
  if (!formInit && Array.isArray(st.seats)) { fillForm(st); formInit = true; }

  const chip = $('state-chip');
  chip.textContent = LABEL[st.state] || st.state;
  chip.className = 'chip state-' + st.state;

  $('cfg-chip').textContent = busy
    ? (st.total + ' 人（' + Math.max(0, st.total - st.bots) + ' 真人 + ' + st.bots +
       ' 电脑） · 种子 ' + st.start_seeds + ' · 端口 ' + st.game_port)
    : '—';

  $('err').textContent = st.lab_error || st.error || '';

  if (st.seats) renderSeats(st.seats);

  // ★ 座位表用 STATUS.seats，不用 st.seats：Room 自己推的 status（开局/停局时）
  //   不带 seats 字段 —— 直接读 st.seats 会把「重新打开座位页」按钮误置灰。
  const seats = STATUS.seats || [];

  // 跑着的时候只能停：参数、名字、规则一律锁住（开的是"新的一局"的参数）
  $('btn-start').disabled = busy;
  $('btn-stop').disabled = !busy;
  $('btn-open').disabled = !seats.length;
  ['f-total', 'f-bots', 'f-seeds', 'f-port'].forEach(id => { $(id).disabled = busy; });
  $('f-names').querySelectorAll('input').forEach(i => { i.disabled = busy; });
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    if (cb) cb.disabled = busy;
  });
  updateStartLabel();
}

/* 按钮上写清"这次要开几个网页"—— N 是人数减电脑数，随输入实时变 */
function updateStartLabel() {
  $('btn-start').textContent = '一键开局（开 ' + humans() + ' 个网页）';
}

/* 首次把表单填好：缓存（上次配的）优先，没缓存才用服务端当前值 */
function fillForm(st) {
  const c = loadCache();
  const busy = (st.state === 'waiting' || st.state === 'playing');
  // 局正在跑的时候以服务端为准 —— 否则输入框显示的是下次的数，看着像"这局参数不对"
  const total = busy ? (st.total || 3) : (c.total != null ? c.total : (st.total || 3));
  const bots = busy ? (st.bots || 0) : (c.bots != null ? c.bots : (st.bots || 0));
  $('f-total').value = total;
  $('f-bots').value = bots;
  $('f-seeds').value = busy ? (st.start_seeds || 0)
                            : (c.seeds != null ? c.seeds : (st.start_seeds || 0));
  $('f-port').value = busy ? (st.game_port || 0)
                           : (c.port != null ? c.port : 0);
  // 局在跑的时候名字以服务端真实座位名为准（刷新页面后不会显示成另一批名字）
  renderNames(busy && st.seats ? st.seats.map(s => s.name) : null);
}

/* ---------- 请求 ---------- */
function post(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  }).then(r => r.json()).then(d => {
    if (d.status) applyStatus(d.status);
    $('err').textContent = d.ok ? '' : (d.err || '操作失败');
    return d;
  }).catch(e => {
    $('err').textContent = '请求失败：' + e;
  });
}

function snapshot() {
  return {
    total: int($('f-total').value, 3),
    bots: int($('f-bots').value, 0),
    seeds: Math.max(0, int($('f-seeds').value, 0)),
    port: Math.max(0, int($('f-port').value, 0)),
    names: Array.from($('f-names').querySelectorAll('input')).map(i => i.value.trim()),
    rules: ruleToggles()
  };
}

$('btn-start').onclick = () => {
  const s = snapshot();
  if (s.bots > s.total) { $('err').textContent = '电脑数不能大于人数'; return; }
  saveCache(s);                        // 记住这次配的，下次打开自动带出
  post('/api/lab/start', {
    total: s.total, bots: s.bots, start_seeds: s.seeds,
    game_port: s.port, rules: ruleValues(), names: s.names
  });
};

$('btn-stop').onclick = () => post('/api/lab/stop', {});
$('btn-open').onclick = () => post('/api/lab/open', {});

/* 人数/电脑数一改就重建名字框（保留已填的值） */
['f-total', 'f-bots'].forEach(id => {
  $(id).oninput = () => { renderNames(); updateStartLabel(); };
});

/* ---------- 战报（SSE） ---------- */
function addLog(text) {
  if (!text) return;
  const box = $('log');
  const d = document.createElement('div');
  d.textContent = text;
  if (/===|获胜|共同获胜|中止/.test(text)) d.className = 'sys';
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  while (box.childNodes.length > 600) box.removeChild(box.firstChild);
}

function connectSSE() {
  const es = new EventSource('/api/lab/events');
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (ev.type === 'clear') { $('log').innerHTML = ''; return; }
    if (ev.type === 'msg') { addLog(ev.text); return; }
    if (ev.type === 'status') { applyStatus(ev); return; }
  };
  es.onerror = () => {
    // 浏览器会自动重连；这里只在页面上留一句，别弹窗
    $('err').textContent = '与试玩台后端失联（浏览器会自动重连）';
  };
}

/* ---------- 启动 ---------- */
fetch('/api/lab/status').then(r => r.json()).then(applyStatus).catch(() => {});
connectSSE();

/* 座位连接状态靠轮询：谁连上了、连的是几号位，都不在 Room 的状态里 */
setInterval(() => {
  if (polling) return;
  polling = true;
  fetch('/api/lab/status').then(r => r.json()).then(applyStatus)
    .catch(() => {}).then(() => { polling = false; });
}, 2000);
