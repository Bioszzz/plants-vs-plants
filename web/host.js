/* 植物斗争 —— 房主控制台前端
   房主在这里配参数、点「开局」，然后【观战】：看战报 + 场面。

   房主不下场 —— 服务端那边他只是一个 Observer（见 netserver.py::ConsoleObserver），
   引擎从头到尾不会问他要决策。规则与结算全在 engine.py，这里一行规则都不实现。 */

'use strict';

const $ = (id) => document.getElementById(id);
const LABEL = { idle: '未开局', waiting: '等人连接', playing: '对局中', ended: '已结束' };

let STATUS = { state: 'idle' };
let lastHp = {};      // 上一条场面的血量，用来判断"谁被打了"（抖一下）
let formInit = false; // 表单只在页面加载后回填一次，免得把房主正在填的数字冲掉
let savedAt = 0;      // 【v0.43】上次点「确定」的时间，用来给回执加个"✓ 已保存"的高亮

/* ---------- 【v0.49】可选规则集 ----------
   数据来源 = netserver 的 status.rule_options（= plants_data.RULE_OPTIONS），
   勾选框由它【动态生成】；加一条规则只改数据层，这里不用动。
   勾选状态缓存在浏览器 localStorage，下次打开控制台自动带出。

   ★ 缓存存布尔、不存 rules 值：将来 on/off 改了，缓存照样能用。
   ★ 与「服务端已保存的设置」怎么共存：只有服务端那份【偏离默认】时才覆盖
     勾选框（见 applyStatus 里的 rules_default 判断）—— 否则会拿"全默认"
     把房主上次的勾选冲掉。 */
let RULE_OPTIONS = [];
const RULE_CACHE_KEY = 'pvp.rules.v1';

function loadRuleCache() {
  try { return JSON.parse(localStorage.getItem(RULE_CACHE_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}
function saveRuleCache(obj) {
  try { localStorage.setItem(RULE_CACHE_KEY, JSON.stringify(obj)); } catch (e) {}
}

function ensureRuleBox(opts) {
  if (RULE_OPTIONS.length || !opts || !opts.length) return;
  RULE_OPTIONS = opts;
  const box = $('f-rules');
  const saved = loadRuleCache();
  box.innerHTML = '';
  RULE_OPTIONS.forEach(o => {
    const lab = document.createElement('label');
    lab.className = 'rulecheck';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'f-rule-' + o.key;
    cb.checked = (o.key in saved) ? !!saved[o.key] : !!o.default_on;
    lab.appendChild(cb);
    const sp = document.createElement('span');
    sp.textContent = o.name;
    if (o.desc) sp.title = o.desc;
    lab.appendChild(sp);
    box.appendChild(lab);
  });
}

/* 把服务端的一份 rules 铺到勾选框上 */
function applyRulesToUI(rules) {
  rules = rules || {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    if (cb) cb.checked = (rules[o.key] === undefined) ? !!o.default_on
                                                    : (rules[o.key] === o.on);
  });
}

/* 勾选状态（布尔）—— 只用来写缓存 */
function ruleToggles() {
  const out = {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    if (cb) out[o.key] = !!cb.checked;
  });
  return out;
}

/* 真正发给服务端的 rules（键 → 值） */
function ruleValues() {
  const out = {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    out[o.key] = (cb && cb.checked) ? o.on : o.off;
  });
  return out;
}

/* 两份 rules 是否完全一样（只比登记过的键） */
function sameRules(a, b) {
  a = a || {}; b = b || {};
  return RULE_OPTIONS.every(o => (a[o.key] || 0) === (b[o.key] || 0));
}

/* ---------- 启动 ---------- */
fetch('/api/host/status').then(r => r.json()).then(applyStatus).catch(() => {});
connectSSE();

function connectSSE() {
  const es = new EventSource('/api/host/events');
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    onEvent(ev);
  };
  es.onerror = () => {
    $('setup-err').textContent = '与控制台后端失联了（服务端可能被关了）。';
  };
}

function onEvent(ev) {
  if (ev.type === 'status') return applyStatus(ev);
  if (ev.type === 'clear') {          // 新的一局：战报从头来
    $('log').innerHTML = '';
    lastHp = {};
    $('players').innerHTML = '';
    $('tags').innerHTML = '';
    $('round-chip').textContent = '第 0 回合';
    return;
  }
  if (ev.type === 'msg') {
    if (ev.state) renderBoard(ev.state);   // 结构化场面 → 画卡片
    addLog(ev.text);                        // 文本 → 进战报
  }
}

/* ---------- 房间状态 ---------- */
function applyStatus(st) {
  STATUS = st;
  // 【v0.49】先把规则勾选框按服务端元数据建出来（只建一次）
  ensureRuleBox(st.rule_options);
  const chip = $('state-chip');
  chip.textContent = LABEL[st.state] || st.state;
  chip.className = 'chip state-' + st.state;
  // 【v0.25】房间规则不同的项也标在状态条上（默认的倍耗封顶3层不占地方）
  //   ★【v0.52】改成按元数据动态拼：老写法写死 `if (rules.mult_cap === 0)
  //     加上 ' · 倍耗不封顶'`，只认第一条规则 —— 后加的「充能难题」
  //     「丝瓜万岁」「生命之神」开了也不会显示在这里（与 main.py / USAGE
  //     是同一个病：界面各写一份清单）。
  let seedTxt = '开局种子 ' + (st.start_seeds || 0);
  const _R = st.rules || {}, _D = st.rules_default || {};
  const _opts = st.rule_options || RULE_OPTIONS || [];
  _opts.forEach(o => {
    if (_R[o.key] !== undefined && _D[o.key] !== undefined && _R[o.key] !== _D[o.key]) {
      seedTxt += ' · ' + o.name;
    }
  });
  $('seed-chip').textContent = seedTxt;

  // 按钮：跑着的时候只能「停止」，闲着的时候才能「开局」/ 改设置。
  $('btn-save').disabled = !!st.busy;
  $('btn-start').disabled = !st.can_start;
  $('btn-stop').disabled = !st.busy;
  // 【v0.49】规则勾选框也一起禁用（列表是动态的，所以按 RULE_OPTIONS 遍历，
  //   不许再写死 id —— 老代码写死了 f-uncap/f-segvar，加第三条就会漏禁）
  ['f-total', 'f-bots', 'f-seeds', 'f-port'].forEach(id => {
    $(id).disabled = !!st.busy;
  });
  RULE_OPTIONS.forEach(o => {
    const cb = $('f-rule-' + o.key);
    if (cb) cb.disabled = !!st.busy;
  });
  if (!formInit && st.can_start) {
    fillForm(st);
    formInit = true;
  } else if (st.rules && st.rules_default && !sameRules(st.rules, st.rules_default)) {
    // 【v0.49】服务端那份【偏离默认】→ 以它为准（房主上次存过的设置比浏览器
    //   缓存更权威）；全默认则【不覆盖】—— 否则会把缓存的勾选冲掉。
    applyRulesToUI(st.rules);
  }
  $('setup-err').textContent = st.error || '';
  renderSaved(st);        // 【v0.43】"当前设置（已保存）"回执
  renderJoined(st);       // 【v0.43】已连接的玩家
  tip(st);
}

/* 【v0.43】把服务端【真正存着的】参数与规则原样回显出来 ——
   房主调整完不再靠猜"到底生效没有"，这里显示的就是开局会用的那一份。
   rulesText：只列"非默认"的开关（全默认就写"全默认"）。 */
function rulesText(rules) {
  rules = rules || {};
  const on = [];
  // 【v0.49】按元数据遍历 —— 不再一条条 if，加规则不用改这里。
  RULE_OPTIONS.forEach(o => {
    const base = o.default_on ? o.on : o.off;          // 该项的默认值
    const v = (rules[o.key] === undefined) ? base : rules[o.key];
    if (v !== base) on.push(o.name);
  });
  return on.length ? on.join('、') : '全默认';
}

function configText(st) {
  const total = st.total || 0, bots = st.bots || 0;
  return total + ' 人（' + Math.max(0, total - bots) + ' 真人 + ' + bots + ' 电脑）' +
    ' · 开局种子 ' + (st.start_seeds || 0) +
    ' · 游戏端口 ' + st.game_port +
    ' · 规则：' + rulesText(st.rules);
}

function renderSaved(st) {
  const fresh = (Date.now() - savedAt) < 3000;
  const box = $('setup-saved');
  box.innerHTML = (fresh ? '✓ 已保存：' : '当前设置（已保存）：') + configText(st);
  box.className = 'saved' + (fresh ? ' ok' : '');
}

/* 【v0.43】已连接的玩家：编号 + 名称 + 来源地址 + 进度。
   房主"连了几个人却没见能开局"时，靠这一行能立刻看出：收没收到、收到谁、
   从哪台机器连的（连错端口 / 连错机器一眼就露馅）。 */
function renderJoined(st) {
  const box = $('joined-list');
  if (st.state !== 'waiting' && st.state !== 'playing') { box.innerHTML = ''; return; }
  const humans = st.humans || 0;
  const list = st.joined_names || [];
  const items = list.map(j => {
    const nm = (j.name && j.name !== ('player' + j.idx)) ? ('（' + j.name + '）') : '';
    return 'player' + j.idx + nm + (j.addr ? ' <span class="from">← ' + j.addr + '</span>' : '');
  }).join('、');
  box.innerHTML = '已连接 <b>' + list.length + '/' + humans + '</b>' +
    (items ? '：' + items : '（还没有客户端连上）');
}

function fillForm(st) {
  $('f-total').value = st.total;
  $('f-bots').value = st.bots;
  $('f-seeds').value = st.start_seeds || 0;
  $('f-port').value = st.game_port;
  // 【v0.49】规则勾选框：服务端【已经存着的】那份优先（首次加载时若它还是
  //   全默认，就保留浏览器缓存里的上次勾选 —— ensureRuleBox 已经铺好了）。
  const rules = st.rules || {};
  const base = st.rules_default || {};
  if (!sameRules(rules, base)) applyRulesToUI(rules);
}

/* 极简 HTML 转义（玩家名是用户输入，塞进 innerHTML 前必须过一遍） */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* 【v0.63】本服战绩：服务端不重启就一直累计（名字 -> 胜场）。 */
function recordText(st) {
  const w = st.wins || {};
  const keys = Object.keys(w).sort((a, b) => w[b] - w[a]);
  if (!keys.length) return '';
  return '　·　战绩：' + keys.map(k => esc(k) + ' ' + w[k] + ' 胜').join('、');
}

function tip(st) {
  const box = $('conn-tip');
  const addr = '<code>' + (st.host_ip || '本机') + ':' + st.game_port + '</code>';
  // 【v0.43】把"控制台端口"也讲出来 —— 两个端口各干嘛的，摆一起就不会看岔。
  const cport = st.console_port
    ? '　控制台页面：<code>http://' + (st.host_ip || '本机') + ':' + st.console_port + '/</code>'
    : '';
  if (st.state === 'idle') {
    box.innerHTML = '改完参数记得先点「确定（保存设置）」；点「开局」后，让玩家连接 ' + addr +
      '：控制台版 <code>netclient.py</code> · 网页版 <code>webui.py</code>。' + cport;
    return;
  }
  const who = st.bots ? '（另有 ' + st.bots + ' 个电脑）' : '';
  // 【v0.63】打完一局后服务端【不退出】—— 这时也是 waiting，
  //   但语气完全不同（不是在等第一局的人，而是在等大家点「再来一局」）。
  if (st.state === 'waiting' && st.replay_ready) {
    box.innerHTML = '【上一局】' + esc(st.last_summary || '已结束') +
      '　·　本服共打了 <b>' + st.games + '</b> 局' + recordText(st) +
      '。<br>服务端【没有关】—— 玩家直接点「再来一局」重连 ' + addr +
      ' 就是下一局（人数 / 规则 / 开局种子 / 座位名全部沿用）。<br>' +
      '想改参数就先点「停止」，回到「未开局」再改。' + cport;
    return;
  }
  if (st.state === 'waiting') {
    box.innerHTML = '正在等玩家连接 ' + addr + ' —— 已连接 <b>' +
      (st.joined || 0) + '/' + (st.humans || 0) + '</b>' + who +
      '。<br>（等人期间「开局」会灰着；不想等了就点「停止」中止本局，' +
      '回到「未开局」后再改参数重开。）' + cport;
  } else if (st.state === 'playing') {
    box.innerHTML = '对局进行中' + who + ' · 端口 <code>' + st.game_port + '</code>' +
      '（房主观战中，不参与决策）。<br>想中止就点「停止」。' + cport;
  } else {
    // 【v0.45】结局文案：winners 可能不止一个人（同一队友链共同获胜）。
    let who2;
    if (st.winners && st.winners.length) {
      who2 = st.winners.map(w => 'player' + w).join('、') +
        (st.winners.length > 1 ? ' 共同获胜' : ' 获胜');
    } else if (st.winner) {
      who2 = 'player' + st.winner + ' 获胜';
    } else {
      who2 = '本局结束（平局 / 中止）';
    }
    box.innerHTML = who2 + recordText(st) +
      ' · 改完参数记得先点「确定（保存设置）」再开一局。' + cport;
  }
}

/* ---------- 保存设置 / 开局 / 停止 ---------- */
function readForm() {
  const body = {
    total: parseInt($('f-total').value, 10),
    bots: parseInt($('f-bots').value, 10),
    start_seeds: parseInt($('f-seeds').value, 10),
    game_port: parseInt($('f-port').value, 10),
    // 【v0.49】可选规则集：界面只报"勾没勾"，具体值由元数据的 on/off 决定
    //   （服务端还会再折一遍 normalize_rules，这里只是把房主的选择传过去）。
    rules: ruleValues()
  };
  // 空框一律当 0 —— 后面服务端还会再校验一遍，这里只是少一次白跑
  ['total', 'bots', 'start_seeds', 'game_port'].forEach(k => {
    if (isNaN(body[k])) body[k] = 0;
  });
  return body;
}

// 【v0.43】「确定（保存设置）」：把参数与规则存进房间（不开局），
//   服务端折好后回显 —— 房主立刻能看到"生效的到底是哪几条规则"。
$('btn-save').onclick = () => {
  $('setup-err').textContent = '';
  $('btn-save').disabled = true;
  post('/api/host/config', readForm()).then(d => {
    $('btn-save').disabled = false;
    // 【v0.49】只要服务端收下了，就把这次勾选记进浏览器缓存
    if (d.ok) saveRuleCache(ruleToggles());
    if (d.status) { savedAt = Date.now(); applyStatus(d.status); }
    if (!d.ok) $('setup-err').textContent = d.err || '保存失败';
  });
};

$('btn-start').onclick = () => {
  const body = readForm();
  $('setup-err').textContent = '正在开局……';
  $('btn-start').disabled = true;
  post('/api/host/start', body).then(d => {
    // 【v0.49】开局成功也记一份 —— 直接用「开局」跳过「确定」是很常见的走法
    if (d.ok) saveRuleCache(ruleToggles());
    if (d.status) applyStatus(d.status);
    if (!d.ok) $('setup-err').textContent = d.err || '开局失败';
    else $('setup-err').textContent = '';
  });
};

$('btn-stop').onclick = () => {
  $('btn-stop').disabled = true;
  post('/api/host/stop', {}).then(d => {
    if (d.status) applyStatus(d.status);
    if (!d.ok) $('setup-err').textContent = d.err || '停止失败';
  });
};

function post(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).catch(e => ({ ok: false, err: '请求失败：' + e }));
}

/* ---------- 场面（和网页客户端同一套排版）---------- */
function renderBoard(st) {
  $('round-chip').textContent = '第 ' + st.round + ' 回合';
  const box = $('players');
  box.innerHTML = '';
  st.players.forEach(p => {
    const card = document.createElement('div');
    card.className = 'pcard' + (p.alive ? '' : ' dead');
    const prev = lastHp[p.idx];
    if (prev !== undefined && p.hp < prev) {
      card.classList.add('hurt');                       // 掉血 → 抖一下
      setTimeout(() => card.classList.remove('hurt'), 600);
    }
    lastHp[p.idx] = p.hp;
    card.innerHTML = boardHtml(p);
    box.appendChild(card);
  });
  renderTags(st.tag_marks || []);
}

function renderTags(marks) {
  const box = $('tags');
  if (!marks.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="tags-title">人参果标记（累积满就爆发穿透伤害）</div>' +
    marks.map(m => {
      const own = (m.owners || []).map(i => 'P' + i).join('、');
      let tail = own ? ' · 埋雷 ' + own : '';
      if (m.rounds_left !== null && m.rounds_left !== undefined) {
        tail += ' · 剩 ' + m.rounds_left + ' 回合';
      }
      return '<div class="tagrow' + (m.count >= m.threshold ? ' hot' : '') + '">' +
        '<span class="seg">段 ' + m.seg + '</span>' +
        '<span class="cnt">' + m.count + '/' + m.threshold + '</span>' +
        '<span class="own">' + tail + '</span></div>';
    }).join('');
}

function boardHtml(p) {
  let hp = '';
  const full = Math.floor(p.hp / 2), half = p.hp % 2;
  for (let i = 0; i < full; i++) hp += '<i></i>';
  if (half) hp += '<i class="half"></i>';
  if (!p.hp) hp = '<i class="empty"></i>';

  let stats = '<span class="stat">种子 <b>' + p.seeds + '</b></span>';
  if (p.water) stats += '<span class="stat water">水滴 <b>' + p.water + '</b></span>';
  if (p.fire) stats += '<span class="stat fire">火种 <b>' + p.fire + '/' + p.fire_burst_at + '</b></span>';
  if (p.super_def) stats += '<span class="stat superdef">超防 <b>' + p.super_def + '</b> 回合</span>';
  // 【v0.25】消耗加成：鱼腥味 / 倍耗（生效中的层数；下回合才生效的用 pending 标记）
  if (p.fishy) stats += '<span class="stat">鱼腥味 <b>' + p.fishy + '</b></span>';
  if (p.mult) stats += '<span class="stat">倍耗 <b>' + p.mult + '</b>(×' + (p.mult_rate || 1) + ')</span>';
  if (p.fishy_next || p.mult_next) {
    const pend = [];
    if (p.fishy_next) pend.push('鱼腥味+' + p.fishy_next);
    if (p.mult_next) pend.push('倍耗+' + p.mult_next);
    stats += '<span class="stat">下回合 ' + pend.join(' ') + '</span>';
  }

  let slot;
  if (!p.alive) slot = '<div class="slot-sub out">已出局</div>';
  else if (!p.plant) slot = '<div class="slot-sub">（空场）</div>';
  else {
    const s = p.plant;
    let sub = '防御 ' + s.fang;
    if (s.broken) sub += ' · <span class="broken">已被击破</span>';
    else if (s.inert) sub += ' · <span class="inert">未释放</span>';
    slot = '<div class="slot">' +
      '<div class="plant-art" style="' + artStyle(s.name) + '">' + s.name.charAt(0) + '</div>' +
      '<div class="slot-info"><div class="slot-name">' + s.name + '</div>' +
      '<div class="slot-sub">' + sub + '</div></div></div>';
  }

  return '<div class="pcard-head"><div class="pname">' + p.name +
    '</div><div class="hp">' + hp + '</div></div>' +
    '<div class="stats">' + stats + '</div>' + slot;
}

function hueOf(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}
function artStyle(name) { return 'background: hsl(' + hueOf(name) + ', 42%, 62%)'; }

/* ---------- 战报 ---------- */
function addLog(text) {
  if (!text) return;
  const box = $('log');
  const d = document.createElement('div');
  d.textContent = text;
  if (/损失|出局|击破|爆发|打穿|中止/.test(text)) d.className = 'dmg';
  else if (/获得|获胜|回血|已连接/.test(text)) d.className = 'good';
  else if (/——|===|\*\*\*|阶段|房间已开|开局种子|房间规则/.test(text)) d.className = 'sys';
  box.appendChild(d);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}
