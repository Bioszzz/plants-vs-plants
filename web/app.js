/* 植物斗争 —— 网页版客户端前端
   只做三件事：接 SSE 事件 → 渲染场面 → 把玩家的选择 POST 回本地桥。
   所有规则与结算都在服务端的 engine.py 里，这里一行规则都不实现。

   【v0.15】三条界面约定：
     1. 出局 ≠ 断线。我出局后决策区切成"观战中"，但场面和战报继续刷新 ——
        出局只是暂时下场，不是离桌 —— 而且【现在就能被复活】（161 生命之花）。
     2. 对局结束后，结束面板【不许】被随后的"连接已断开"事件冲掉。
        服务端发完 end 就关 socket，这是正常的；界面要留住结局和「再来一局」。
     3. 新连接 = 新的一局：场面、战报、我是否出局，全部重置。 */

'use strict';

const $ = (id) => document.getElementById(id);

let ME = 0;              // 我是几号 —— 由服务端的 hello 告诉我们
let TOTAL = 0;
let catalog = [];        // 图鉴（含代价等富数据），用来渲染选牌卡片
let busy = false;        // 防连点：提交后锁住，收到下一个 ask 才解锁
let connected = false;
let MY_ALIVE = true;     // 我还活着吗 —— 由场面里的 alive 字段决定
let MODE = 'connect';    // connect（没连上）/ play（对局中）/ over（已结束）
let lastHp = {};
let MY_NAME = '';        // 【v0.35】我给自己起的名字（空 = 不起名，用服务端默认）
// 【v0.36】"当前正开着的那道选人题" —— 让存活环可以点。
//   null = 现在没有题（环只读）；否则 { idxs, click }：
//     idxs()  → 这道题允许点哪些 idx（供环决定哪些节点高亮/可点）
//     click(i)→ 把"点了第 i 号"这件事交给这道题（等价于点下面那张卡）
//   ★ 每回答完 / 换题 / 收场时都要清掉，否则会出现"点了环却没反应/点错题"。
let RING_PICK = null;
// 【v0.36】最近一次收到的场面（setRingPick 要按它重画存活环，不必等下一帧 state）。
let lastState = null;
// 【v0.60】当前正在显示的那道题 —— 图鉴（/api/catalog）比 SSE 晚到时用它重画。
//   ★ 必踩场景：页面打开时服务端【已经有一道题在等】（试玩台、刷新页面、重连）。
//     ask 走 SSE 先到 → 选牌界面已经画出来，可 catalog 还是空数组 →
//     每张卡都是「?? / 免费」，而且永远不会自己恢复（catalog 回来时没人重画）。
let CUR_ASK = null;
// 【v0.62】上一次画题时用过的队伍签名 —— 队伍关系变了要把当前题面补画一次
//   （与 v0.60 的图鉴同一类问题：题先到、场面后到，画题时 teams 还是空的）。
let TEAM_SIG = '';

/* ================= 【v0.62】队友链可视化 =================
   数据只认引擎 state 的 teams（每个连通分图一组，组内第 0 个 = 代表元）——
   前端【不】自己算"谁跟谁一队"（那是引擎并查集的活），只负责让人一眼看见：
     · 存活环：同队同色 + 队标 + 同色连线；
     · 环下方图例：「队1：甲 · 乙」；
     · 玩家卡 / 所有选人候选卡：「同队」或「队N」徽标。
   ★ 单人分图不算"队"（一个人没有队友链）→ 不给编号、环上不画线。 */
const TEAM_COLORS = ['#e8a33d', '#5aa9e6', '#e06a8b', '#7fbf4d', '#a98ce0', '#4ec6c6'];

function teamMap(st) {
  const src = st || lastState;
  const m = {};
  if (!src || !src.teams) return m;
  let no = 0;
  src.teams.forEach(g => {
    if (!g || g.length < 2) return;
    no += 1;
    g.forEach(i => { m[i] = no; });
  });
  return m;
}

function teamColor(no) { return TEAM_COLORS[(no - 1) % TEAM_COLORS.length]; }

/* 队伍徽标。same = 与我同队；ME 还没确定座位时不显示"同队"（免得谎报）。
   ★ 对自己不写"同队"（自己跟自己不算队友，卡上本来就有「你」角标）——
     只标「队N」，用来对上环上的队色和图例。 */
function teamBadge(idx, st) {
  const m = teamMap(st);
  const no = m[idx];
  if (!no) return '';
  const same = !!ME && idx !== ME && m[ME] === no;
  return '<span class="tbadge' + (same ? ' same' : '') +
    '" style="--tc:' + teamColor(no) + '">' + (same ? '同队' : '队' + no) + '</span>';
}

/* ---------- 启动 ---------- */
fetch('/api/catalog').then(r => r.json()).then(d => {
  catalog = d.plants;
  // 【v0.49】可选规则集的勾选框由服务端元数据生成，不在 HTML 里写死。
  renderRuleOptions(d.rule_options || []);
  // 【v0.60】图鉴补上来了 → 把正在显示的那道选牌题重画一遍。
  //   ★ 只重画 plant，且只在"还没提交"时重画 —— 否则会盖掉「已提交，等待其他玩家」。
  if (CUR_ASK && CUR_ASK.kind === 'plant' && !busy) renderPlant(CUR_ASK);
});
loadNames();             // 【v0.35】把上次用的名字带出来
fetch('/api/status').then(r => r.json()).then(st => {
  if (st.connected) {
    // 【v0.59】★★ 这里【绝不能】直接调 onConn(connected:true)。
    //   onConn 是为"刚刚连上那一刻"写的：它会 resetBoard() 清空战报、再用
    //   showWait 盖掉「你的操作」。而这条 /api/status 是【异步】的 ——
    //   页面越晚打开，它越可能排在 SSE 回放【之后】才回来：
    //     ① SSE 先回放 conn/hello/历史战报/当前待答 → 界面已经正常了；
    //     ② status 回调后到 → onConn → 把战报清空、把题目换成"等待对局开始"。
    //   症状：界面永远停在"已连接，等待对局开始……"，题目一张都不出来。
    //   刷新页面（或新开标签页）必踩；试玩台多开更是必然（页面一定在连上之后开）。
    //   所以这里只做"无副作用的状态对齐"，界面内容交给 SSE 去补。
    connected = true;
    MY_ALIVE = true;
    MODE = 'play';
    $('connect').classList.add('hidden');
    $('srv-chip').classList.remove('hidden');
    $('btn-disc').classList.remove('hidden');
    if (st.host) $('srv-chip').textContent = st.host + ':' + st.port;
  } else {
    showConnect();
  }
}).catch(() => showConnect());
initVersion();
connectSSE();

function connectSSE() {
  const es = new EventSource('/api/events');
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    onEvent(ev);
  };
  es.onerror = () => {
    // 连不上本地桥（一般是这个 python 进程被关了）
    if (!connected) $('conn-err').textContent = '本地页面服务已断开，请重启 python webui.py';
  };
}

function onEvent(ev) {
  if (ev.type === 'conn') return onConn(ev);
  if (ev.state) renderBoard(ev.state);
  if (ev.type === 'hello') return onHello(ev);
  if (ev.type === 'msg') { addLog(ev.text); return; }
  if (ev.type === 'wait') return onWait(ev.text);
  if (ev.type === 'end') return showOver(ev);
  if (ev.type === 'ask') return showAsk(ev);
}

/* ---------- 连接 ---------- */
function onHello(ev) {
  ME = ev.idx;
  TOTAL = ev.total;
  updateMeChip();
}

function onConn(ev) {
  connected = !!ev.connected;
  if (connected) {
    busy = false;
    MY_ALIVE = true;
    MODE = 'play';
    $('connect').classList.add('hidden');
    $('srv-chip').classList.remove('hidden');
    $('btn-disc').classList.remove('hidden');
    if (ev.host) $('srv-chip').textContent = ev.host + ':' + ev.port;
    if (ev.idx) onHello({ idx: ev.idx, total: ev.total });
    resetBoard();                       // 新连接就是新的一局，别留着上一局的东西
    setDeckTitle('你的操作');
    showWait('已连接，等待对局开始……');
  } else {
    $('srv-chip').classList.add('hidden');
    $('btn-disc').classList.add('hidden');
    if (MODE === 'over') {
      // 关键：对局已结束，服务端关 socket 是正常的。
      // 结束面板要留着 —— 否则玩家点不到「再来一局」。
      $('me-chip').textContent = '已断开';
      return;
    }
    MODE = 'connect';
    $('me-chip').textContent = '未连接';
    showConnect(ev.text || '');
  }
}

function updateMeChip() {
  if (!ME) { $('me-chip').textContent = '未连接'; return; }
  const tail = TOTAL ? ' / 共 ' + TOTAL + ' 人' : '';
  $('me-chip').textContent = '你是 player' + ME + (MY_ALIVE ? '' : ' · 已出局') + tail;
}

function resetBoard() {
  lastHp = {};
  $('log').innerHTML = '';
  $('players').innerHTML = '';
  $('round-chip').textContent = '第 0 回合';
  $('me-chip').textContent = ME ? '你是 player' + ME : '未连接';
  // 【v0.63】★★ 上一局的"场面残留"必须一并清掉。
  //   用户 2026-10-23 报："再来一局时，人参果的埋雷提示会跨对局出现，
  //   而且顶掉存活环"。根因就是这里只清了战报和玩家卡，没清这两块：
  //     · #tags        —— 人参果段标记
  //     · #ring        —— 存活环（上一局的人还围在圈里）
  //     · #ring-legend —— 队友链图例
  //   新一局的第一帧 state 会把它们全部重画，所以这里清空是安全的。
  const tags = $('tags');
  if (tags) tags.innerHTML = '';
  const ring = $('ring');
  if (ring) ring.innerHTML = '';
  const lg = $('ring-legend');
  if (lg) lg.innerHTML = '';
  const rw = $('ringwrap');
  if (rw) rw.classList.add('hidden');
  const ex = $('ring-extra');
  if (ex) ex.innerHTML = '';
  lastState = null;      // 否则存活环会照着上一局的 state 画
  TEAM_SIG = '';         // 队伍签名归零，新局的队伍关系才会触发"补画"
  CUR_ASK = null;
  RING_PICK = null;
}

function showConnect(msg) {
  $('connect').classList.remove('hidden');
  $('conn-err').textContent = msg || '';
  setDeck('<div class="hint">未连接</div>');
}

/* ---------- 【v0.19】版次与检查更新 ----------
   版号由后端从 VERSION 文件（git tag 生成）读出来下发，这里不写死。
   检查更新是【只读查询】：只查"远端最新版是哪个号"，不会自己下载覆盖 ——
   免得把玩家自己改过的东西冲掉。真要看新东西，点进发布页。 */
function initVersion() {
  fetch('/api/version').then(r => r.json()).then(v => {
    $('ver-no').textContent = v.version || '未知';
    const b = $('btn-update');
    b.disabled = false;
    b.onclick = () => checkUpdate(b);
    // 版本号后面附上提交号，方便对照源码（前 7 位就够认）
    if (v.commit) b.title = '当前提交 ' + v.commit;
  }).catch(() => { $('ver-no').textContent = '未知'; });
}

function checkUpdate(btn) {
  btn.disabled = true;
  btn.textContent = '查询中…';
  $('ver-msg').innerHTML = '';
  fetch('/api/update-check').then(r => r.json()).then(d => {
    btn.disabled = false;
    btn.textContent = '检查更新';
    const box = $('ver-msg');
    if (!d.ok) {
      // 【v0.65】err 可能是"发布页上还没有发行版"，也可能是网络不通 ——
      //   两种情况都该把发布页地址给出去，别让玩家只能干瞪眼。
      box.innerHTML = '查不到更新信息：' + esc(d.err || '网络不通') +
        (d.page ? '<br><span class="dl">' + d.page + '</span>' : '');
      return;
    }
    if (!d.has_update) {
      box.innerHTML = '已是最新（<span class="tag">' + d.latest + '</span>）';
      return;
    }
    // 【v0.65】发行版说明是 markdown，直接塞进页面会满屏 ## 和 *。
    //   剥掉标记、压成一行、截断 90 字 —— 想细看就点发布页。
    const plain = (d.notes || '').replace(/[#*`>|]/g, '').replace(/\s+/g, ' ').trim();
    // 【v0.69】有 zip 就给「立即更新」按钮 —— 用户原话"点检查更新居然没有
    //   自动更新的选项，那这个的意义何在呢？我要能自动更新的"。
    //   ★ 没 zip 时不给按钮：光有个按不动的钮比没有更糟。
    const upd = d.can_update
      ? '<br><button class="lnk" id="btn-do-update">立即更新到 ' + d.latest + '</button>' +
        (d.size ? '<span class="qsub">（约 ' + Math.round(d.size / 1024) + ' KB）</span>' : '')
      : '';
    box.innerHTML = '有新版 <span class="tag">' + d.latest + '</span>' +
      (d.version ? '（当前 ' + d.version + '）' : '') + upd +
      '<br><span class="dl">' + (d.page || '') + '</span>' +
      (plain ? '<br><span class="qsub">' + esc(plain.slice(0, 90)) +
               (plain.length > 90 ? '…' : '') + '</span>' : '');
    const go = $('btn-do-update');
    if (go) go.onclick = () => doUpdate(go);
  }).catch(e => {
    btn.disabled = false;
    btn.textContent = '检查更新';
    $('ver-msg').textContent = '查询失败：' + e;
  });
}

// 【v0.69】一键更新：POST /api/update-install（下载+校验+备份+覆盖都在后端）。
//   ★ 这个请求可能要十几秒（下载 270 KB + 解压 + import 试跑），
//     期间按钮必须禁用，否则连点会撞上后端的 UPDATE_LOCK。
//   ★ 成功之后**必须重启**：当前进程跑的还是旧代码，页面刷新后拿到的是
//     新静态文件 + 旧后端，两边协议一旦有差异就是最难查的那类错。
function doUpdate(btn) {
  btn.disabled = true;
  btn.textContent = '更新中…（下载 + 校验）';
  const box = $('ver-msg');
  fetch('/api/update-install', { method: 'POST', body: '{}' })
    .then(r => r.json()).then(d => {
      if (!d.ok) {
        btn.disabled = false;
        btn.textContent = '重试更新';
        box.innerHTML = '更新失败：' + esc(d.err || '未知原因') +
          '<br><span class="qsub">没有改动任何文件；旧包仍在原处。</span>';
        return;
      }
      btn.textContent = '已更新';
      box.innerHTML = '已更新：<span class="tag">' + d.from + '</span> → ' +
        '<span class="tag">' + d.to + '</span>' +
        '（覆盖 ' + d.files + ' 个文件）' +
        '<br><b>请关掉这个窗口、重新双击启动脚本</b> —— 现在跑的仍是旧代码。' +
        (d.backup ? '<br><span class="qsub">旧文件备份在 ' + esc(d.backup) + '</span>' : '');
      $('ver-no').textContent = d.to;
    }).catch(e => {
      btn.disabled = false;
      btn.textContent = '重试更新';
      box.textContent = '更新失败：' + e;
    });
}

$('btn-connect').onclick = () => {
  const host = ($('ip').value || '').trim() || '127.0.0.1';
  const port = parseInt($('port').value, 10) || 8080;
  doConnect('/api/connect', { host: host, port: port });
};

/* 【v0.22】本机开一局的参数（人数 / 电脑数 / 开局种子）。
   空框一律退回默认（2 人 / 1 电脑 / 0 种子）—— 所以什么都不填就是旧行为。
   边界检查不在这里做：服务端（webui.start_local_game）会统一挡，
   这边只管把界面上的数字原样递过去。 */
/* ---------- 【v0.49】可选规则集 ----------
   数据来源 = 服务端的 plants_data.RULE_OPTIONS（随 /api/catalog 带下来）；
   勾选状态缓存在浏览器 localStorage，下次打开自动带出。

   ★ 界面只报"勾没勾"，"勾上对应哪个值"由服务端给的 on/off 决定 ——
     这样 mult_cap 那种反向开关（勾「不封顶」= 0）也不用前端知道自己是反的。
   ★ 缓存存的是【布尔】而不是 rules 值：将来 on/off 改了，缓存照样能用。 */
let RULE_OPTIONS = [];
const RULE_CACHE_KEY = 'pvp.rules.v1';

function loadRuleCache() {
  try { return JSON.parse(localStorage.getItem(RULE_CACHE_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}
function saveRuleCache(obj) {
  try { localStorage.setItem(RULE_CACHE_KEY, JSON.stringify(obj)); } catch (e) {}
}

function renderRuleOptions(opts) {
  RULE_OPTIONS = opts || [];
  const box = $('l-rules');
  if (!box) return;
  const saved = loadRuleCache();
  box.innerHTML = '';
  RULE_OPTIONS.forEach(o => {
    const lab = document.createElement('label');
    lab.className = 'rulecheck';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'l-rule-' + o.key;
    // 没存过 → 用服务端标的"默认是否勾上"（default_on，由 DEFAULT_RULES 算出来）
    cb.checked = (o.key in saved) ? !!saved[o.key] : !!o.default_on;
    cb.onchange = localLabel;
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

/* 勾选状态（布尔）—— 只用来写缓存 */
function ruleToggles() {
  const out = {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('l-rule-' + o.key);
    if (cb) out[o.key] = !!cb.checked;
  });
  return out;
}

/* 真正发给服务端的 rules（键 → 值） */
function ruleValues() {
  const out = {};
  RULE_OPTIONS.forEach(o => {
    const cb = $('l-rule-' + o.key);
    out[o.key] = (cb && cb.checked) ? o.on : o.off;
  });
  return out;
}

function localParams() {
  const num = (id, d) => {
    const v = parseInt(($(id).value || '').trim(), 10);
    return isNaN(v) ? d : v;
  };
  return { total: num('l-total', 2), bots: num('l-bots', 1),
           start_seeds: num('l-seeds', 0),
           rules: ruleValues() };      // 【v0.49】可选规则集（默认值服务端补齐）
}

/* 按钮上把参数写出来 —— 填了 3 人局却看见按钮还写着"你 vs 电脑"，
   会让人怀疑自己填了没用。 */
function localLabel() {
  const p = localParams();
  const humans = Math.max(0, p.total - p.bots);
  $('btn-local').textContent = '本机开一局（' + humans + ' 真人 + ' + p.bots + ' 电脑' +
    (p.start_seeds > 0 ? ' · 开局种子 ' + p.start_seeds : '') + '）';
}

['l-total', 'l-bots', 'l-seeds'].forEach(id => { $(id).oninput = localLabel; });
localLabel();

$('btn-local').onclick = () => {
  saveRuleCache(ruleToggles());     // 【v0.49】记住这次勾了什么
  doConnect('/api/local', localParams());
};

$('btn-disc').onclick = () => {
  busy = false;
  MODE = 'connect';
  MY_ALIVE = true;
  fetch('/api/disconnect', { method: 'POST' });
};

function doConnect(url, body) {
  // 【v0.35】连之前先把名字存下并报给服务端。
  //   ★ 为什么不放在连接成功之后：服务端"建房时快照一次名字"，
  //     要是等连上再报，可能已经错过快照（本局就不生效了）。
  //     现在这条先到（send_name 走的是已有连接，没连上就静默忽略）——
  //     对"重连已开着的房"仍然来得及。
  MY_NAME = ($('myname').value || '').trim();
  fetch('/api/names', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: MY_NAME })
  }).catch(() => {});

  $('conn-err').textContent = '连接中……';
  const btns = [$('btn-connect'), $('btn-local')];
  btns.forEach(b => { b.disabled = true; });
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json()).then(d => {
    btns.forEach(b => { b.disabled = false; });
    if (!d.ok) $('conn-err').textContent = d.err || '连接失败';
    else $('conn-err').textContent = '已连接，正在进入……';
    // ★ 连上之后再报一次名字：本机局是"先起 netserver 再连"，
    //   上面那一次发的时候连接还不存在（被静默忽略），这里补一发。
    if (d.ok && MY_NAME) postName(MY_NAME);
    // 成功的话由 SSE 的 conn 事件负责切界面
  }).catch(e => {
    btns.forEach(b => { b.disabled = false; });
    $('conn-err').textContent = '请求失败：' + e;
  });
}

/* 【v0.35】把名字报给服务端（连接已建立后调用）。 */
function postName(name) {
  fetch('/api/names', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name })
  }).catch(() => {});
}

/* 【v0.35】启动时把上次用的名字 + 最近用过的几个读出来填进输入框 / 下拉。 */
function loadNames() {
  fetch('/api/names').then(r => r.json()).then(d => {
    if (d && d.player) $('myname').value = d.player;
    const hist = $('name-history');
    if (hist && d && d.recent) {
      hist.innerHTML = d.recent.map(n => '<option value="' + esc(n) + '"></option>').join('');
    }
  }).catch(() => {});
}

/* 极简 HTML 转义（名字是玩家输入，塞进 innerHTML 前必须过一遍） */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------- 场面 ---------- */
function renderBoard(st) {
  lastState = st;          // 【v0.36】存一份最近场面：setRingPick 要按它重画环
  $('round-chip').textContent = '第 ' + st.round + ' 回合';
  const mine = st.players.find(p => p.idx === ME);
  if (mine) {
    if (MY_ALIVE && !mine.alive) {          // 这一回合我出局了
      busy = false;
      showSpectate();
    }
    MY_ALIVE = !!mine.alive;
    updateMeChip();
  }
  const box = $('players');
  box.innerHTML = '';
  st.players.forEach(p => {
    const card = document.createElement('div');
    card.className = 'pcard' + (p.alive ? '' : ' dead') + (p.idx === ME ? ' me' : '');
    const prev = lastHp[p.idx];
    if (prev !== undefined && p.hp < prev) {
      card.classList.add('hurt');                       // 掉血 → 抖一下
      setTimeout(() => card.classList.remove('hurt'), 600);
    }
    lastHp[p.idx] = p.hp;
    card.innerHTML = boardHtml(p);
    box.appendChild(card);
  });
  renderRing(st);
  renderTags(st.tag_marks || []);
  // 【v0.62】队伍关系一变 → 把当前题面补画一次（候选卡上的「同队」徽标才出得来）。
  //   ★ 与 v0.60 的图鉴同一类问题：题面走 SSE 先到、场面后到，画题的时候
  //     teams 还是空的 → 候选卡上一个队标都没有，而且没人重画。
  //   ★ 判据只认"关系真的变了"（签名比较），且只在还没提交（!busy）时重画 ——
  //     否则会清掉玩家已经点好的选择、或盖掉"已提交，等待其他玩家"。
  const sig = JSON.stringify(st.teams || []);
  if (sig !== TEAM_SIG) {
    TEAM_SIG = sig;
    if (CUR_ASK && !busy) redrawAsk();
  }
}

/* 【v0.35】存活环：还活着的人按座位围成一圈，实时摘人。
   数据来自引擎 state 的 ring 字段（= 引擎里那个 Ring 一等对象），
   ★ 界面【不许】自己按 hp 过滤一遍 —— 那会漏掉"实时摘人"这个状态
     （摘人发生在伤害落地那一刻，不是回合末）。
   不足 3 人时不画：那时候"环"没有两侧可言（与"两侧机制不触发"同一口径）。

   ★★【v0.36】点环选人：当【当前正好在问"选谁"】时，环上的节点变成【可点的目标按钮】，
   点一下就等于点了列在下面的那张卡（省得在卡片里找名字）。
   ★ 分工仍守老规矩：**环只负责"把这一次点击交给当前那道题"**，
     谁有资格当候选、点谁合法 —— 全由服务端发下来的 cands 说话；
     前端不自己按环的位置算"谁能被打"（那是引擎的事）。
   ★ 未来扩展：节点上已经挂了 data-idx，将来做三国杀那种"环形座位 + 连线/方位"
     只需要改这里的画法，选人协议不用动。 */
function renderRing(st) {
  const wrap = $('ringwrap');
  const box = $('ring');
  if (!wrap || !box) return;
  const extraBox = document.getElementById('ring-extra');
  const ids = st.ring || [];
  // 【v0.45】阈值 3 → 2：只剩两个人时也可能【连成队友】，
  //   那时"环"正是要展示"他俩是一队的"（两侧机制仍按老口径不触发）。
  if (ids.length < 2) {
    wrap.classList.add('hidden'); box.innerHTML = '';
    if (extraBox) extraBox.innerHTML = '';
    const lg0 = document.getElementById('ring-legend');
    if (lg0) lg0.innerHTML = '';
    return;
  }
  wrap.classList.remove('hidden');
  const byIdx = {};
  (st.players || []).forEach(p => { byIdx[p.idx] = p; });
  // 当前这道题允许点谁（没有题 / 不是选人类 → 空集合 = 环只读）
  const pickable = RING_PICK ? RING_PICK.idxs() : null;
  // 环形排布：按 ring 顺序均分到圆周上。用 CSS transform 定位，
  // 容器是固定大小的圆，节点绝对定位到圆心 + 半径 * (cos, sin)。
  const n = ids.length;
  // 【v0.45】用户："最好把存活环做得大一点" —— 半径 78 → 112（容器同步放大）。
  const R = 112;                      // 环半径（px）
  const pos = {};                     // idx -> {x, y}（相对圆心）
  let h = '<div class="ring-circle">';
  // 【v0.45】队友链连线层：画在节点【下面】。零尺寸 + overflow:visible，
  //   坐标直接用"相对圆心"的 x/y —— 所以这里不需要容器尺寸。
  //   ★ 数据只认引擎发下来的 st.teams（每组第 0 个 = 代表元/中心）——
  //     前端不自己算"谁跟谁一队"（那是引擎并查集的活）。
  let links = '';
  const tmap = teamMap(st);     // 【v0.62】idx -> 队号（单人分图没有编号）
  ids.forEach((idx, i) => {
    const ang = (-Math.PI / 2) + (2 * Math.PI * i / n);   // 从正上方起、顺时针
    pos[idx] = { x: Math.cos(ang) * R, y: Math.sin(ang) * R };
  });
  (st.teams || []).forEach(g => {
    const c = pos[g[0]];
    if (!c || g.length < 2) return;
    // 【v0.62】每条链用【自己那一队】的颜色（原来是统一金色，两队时看不出分组）
    const col = teamColor(tmap[g[0]] || 1);
    g.slice(1).forEach(j => {
      const t = pos[j];
      if (!t) return;
      links += '<line x1="' + c.x.toFixed(1) + '" y1="' + c.y.toFixed(1) +
        '" x2="' + t.x.toFixed(1) + '" y2="' + t.y.toFixed(1) +
        '" stroke="' + col + '"/>';
    });
  });
  if (links) {
    h += '<svg class="ring-links" width="0" height="0" aria-hidden="true">' + links + '</svg>';
  }
  ids.forEach(idx => {
    const p = byIdx[idx] || {};
    const { x, y } = pos[idx];
    const tag = p.name_tag || p.name || ('player' + idx);
    const isMe = idx === ME;
    const can = !!(pickable && !isMe && pickable.has(idx));
    const tno = tmap[idx];                       // 【v0.62】队号（无 = 单人）
    const tc = tno ? teamColor(tno) : '';
    let cls = 'ring-node';
    if (isMe) cls += ' me';
    if (can) cls += ' pickable';
    if (tno) cls += ' teamed';
    let sty = 'transform:translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px)';
    if (tc) sty += ';border-color:' + tc;
    // 【v0.36】名字+编号 = 唯一显示串（tag 里已含"(N号)"）→ 不再单画一个 idx 角标，
    //   否则会变成 "小明(2号) 2号" 这种重复。
    h += '<div class="' + cls + '" data-idx="' + idx + '" style="' + sty + '" title="' + esc(tag) + '">' +
      '<span class="rn-name">' + esc(tag) + '</span>' +
      (tno ? '<span class="rn-team" style="--tc:' + tc + '">队' + tno + '</span>' : '') +
      '</div>';
  });
  h += '</div>';
  box.innerHTML = h;
  // 把"能点的人"接上：点了就【转发给当前那道题】（等价于点下面那张卡）。
  if (pickable) {
    box.querySelectorAll('.ring-node.pickable').forEach(node => {
      node.onclick = () => RING_PICK.click(parseInt(node.dataset.idx, 10));
    });
  }
  // 【v0.44】环旁边的"额外"按钮（刀类的「空目标/不切」）。没有就清空。
  if (extraBox) {
    const ex = pickable && RING_PICK ? RING_PICK.extra : null;
    if (ex) {
      extraBox.innerHTML = '<button class="btn" id="ring-extra-btn">' +
        esc(ex.label) + '</button>';
      const b = document.getElementById('ring-extra-btn');
      if (b) b.onclick = ex.onClick;
    } else {
      extraBox.innerHTML = '';
    }
  }
  // 【v0.62】环下方的队友链图例：直接写名字，比看线最省事（也是"谁跟谁一队"的答案）。
  const lg = document.getElementById('ring-legend');
  if (lg) {
    const groups = (st.teams || []).filter(g => g && g.length >= 2);
    lg.innerHTML = groups.length
      ? '<span class="rl-title">队友链</span>' + groups.map(g => {
          const no = tmap[g[0]] || 1;
          const names = g.map(i => {
            const q = byIdx[i] || {};
            return esc(q.name_tag || q.name || ('player' + i));
          }).join(' · ');
          return '<span class="rl-item" style="--tc:' + teamColor(no) + '">' +
            '队' + no + '：' + names + '</span>';
        }).join('')
      : '';
  }
}

/* 【v0.16】人参果的段标记：哪一段被埋了、累积几笔、谁埋的、还剩几回合。
   引擎一直在 state 里发这份数据（state_json 的 tag_marks），但界面得画出来 ——
   不然玩家看不见"哪段被埋了"，也就无从决定还要不要出那一段的植物。 */
function renderTags(marks) {
  const box = $('tags');
  if (!box) return;
  if (!marks.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="tags-title">人参果标记（累积满就爆发穿透伤害）</div>' +
    marks.map(m => {
      // 【v0.63】埋雷者写名字，不写 P1 —— 和场上其他人对得上号才看得出是谁埋的
      const own = (m.owners || []).map(i => nameOf(i)).join('、');
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

  // 【v0.63】★ 血量必须有个数字 —— 用户 2026-10-23："角色卡片里有种子，防御，
  //   但居然没有血量，不合理"。上面那排小方块只是"几条命"的图示，
  //   半条命/破防之后根本读不出精确值；这里补一个和「种子」并列的数字。
  let stats = '<span class="stat">生命 <b>' + (p.hp_text || p.hp) + '</b></span>';
  stats += '<span class="stat">种子 <b>' + p.seeds + '</b></span>';
  if (p.water) stats += '<span class="stat water">水滴 <b>' + p.water + '</b></span>';
  if (p.fire) stats += '<span class="stat fire">火种 <b>' + p.fire + '/' + p.fire_burst_at + '</b></span>';
  // 【v0.18】超防：不画出来的话，对手会以为自己打中了（伤害其实被吞了）
  if (p.super_def) stats += '<span class="stat superdef">超防 <b>' + p.super_def + '</b> 回合</span>';
  // 【v0.54】[免疫]：不画出来的话，玩家看见自己的倍耗"莫名其妙没了"会当成 bug
  if (p.immune_turns) stats += '<span class="stat immune">免疫 <b>' + p.immune_turns + '</b> 回合</span>';
  // 【v0.25】消耗加成：鱼腥味 / 倍耗（生效中的层数 + 下回合才生效的待入账）
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

  return '<div class="pcard-head"><div class="pname">' + esc(p.name_tag || p.name) +
    (p.idx === ME ? '<span class="tag-me">你</span>' : '') +
    teamBadge(p.idx) +          // 【v0.62】谁跟谁一队 —— 玩家卡上也要能一眼看见
    '</div><div class="hp">' + hp + '</div></div>' +
    '<div class="stats">' + stats + '</div>' + slot;
}

/* 占位美术：按名字算色相，每株一个固定颜色。正式插画做好后替换这里即可。 */
function hueOf(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}
function artStyle(name) { return 'background: hsl(' + hueOf(name) + ', 42%, 62%)'; }

/* ---------- 决策区 ---------- */
function showAsk(ev) {
  if (ev.who !== undefined && ev.who !== ME) {
    showWait('轮到 player' + ev.who + ' 操作，请把设备交给他……');
    return;
  }
  busy = false;
  // 【v0.36】每来一道新题先清掉上一题的"环上可选人"，防止点环点到已经作废的题。
  RING_PICK = null;
  CUR_ASK = ev;            // 【v0.60】记下来：图鉴晚到时要靠它重画
  if (ev.kind === 'plant') return renderPlant(ev);
  if (ev.kind === 'release') return renderRelease(ev);
  if (ev.kind === 'charge') return renderCharge(ev);
  if (ev.kind === 'target') return renderTarget(ev);
  if (ev.kind === 'targets') return renderTargets(ev);
  // 【v0.44】连击：一次性发数分配（取代原"逐发问 N 次"）
  if (ev.kind === 'burst_split') return renderBurstSplit(ev);
  if (ev.kind === 'splash') return renderSplash(ev);
  if (ev.kind === 'water_hp') return renderWaterHp(ev);
  if (ev.kind === 'water_def') return renderWaterDef(ev);
  // 【v0.32】刀叶类（消除 / 废除）
  if (ev.kind === 'cut_target') return renderCutTarget(ev);
  if (ev.kind === 'cut_mode') return renderCutMode(ev);
  if (ev.kind === 'cut_half') return renderCutHalf(ev);
  if (ev.kind === 'cut_help') return renderCutHelp(ev);
  // 【v0.38】电系（勾人 / 闪电链）
  if (ev.kind === 'absorb') return renderAbsorb(ev);
  if (ev.kind === 'flash') return renderFlash(ev);
  // 【v0.39】猫尾草：连击分配左右两侧
  if (ev.kind === 'cat_split') return renderCatSplit(ev);
  // 【v0.45】队友连接（107 番茄）
  if (ev.kind === 'friend') return renderFriend(ev);
  // 【v0.48】死亡时的种子瓜分（★ 唯一一道问"已出局的人"的题）
  if (ev.kind === 'seed_heir') return renderSeedHeir(ev);
  // 【v0.51】161 生命之花：加血 / 复活【二选一】
  if (ev.kind === 'revive') return renderRevive(ev);
  setDeck('<div class="hint">未知的决策类型：' + ev.kind + '</div>');
}

/* 【v0.62】把当前这道题重新画一遍 —— 供"素材/场面晚到"时补给用
   （图鉴 v0.60、队伍关系 v0.62）。★ 只在 !busy 时调：重画会清掉已点好的选择。 */
function redrawAsk() {
  if (CUR_ASK) showAsk(CUR_ASK);
}

function answer(v) {
  if (busy) return;
  busy = true;
  fetch('/api/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value: v })
  }).then(r => r.json()).then(d => {
    if (!d.ok) addLog('提交失败：' + (d.err || ''));
  }).catch(() => {});
  showWait('已提交，等待其他玩家……');
}

/* 点选类按钮的通用绑定：按 kind 把 data-v 转成正确类型 */
function bindPick(sel, ev) {
  document.querySelectorAll(sel).forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(sel).forEach(x => { x.disabled = true; });
      const raw = b.dataset.v;
      let v = raw;
      if (ev.kind === 'release' || ev.kind === 'water_hp' || ev.kind === 'cut_half') v = raw === '1';
      else if (ev.kind === 'charge' || ev.kind === 'water_def' || ev.kind === 'target'
               || ev.kind === 'cut_target' || ev.kind === 'flash') v = parseInt(raw, 10);
      answer(v);
    };
  });
}

/* 【v0.36】"点存活环 = 点下面那张卡"。
   setRingPick(允许点的 idx 列表, 被点时做什么, {label,onClick}) —— 只是把环上的
   点击【转发】给当前那道题，环本身不做任何规则判断。
   传 null / 空数组 → 环退回只读（这时它只是"看谁还活着"的展示）。
   【v0.44】第三个参数 = 环旁边的"额外"按钮（目前用于刀类的「空目标/不切」）：
     需求来自用户 2026-10-12："存活环旁边最好再加一个『空目标』选项"。 */
function setRingPick(idxList, onClick, extra) {
  if (!idxList || !idxList.length) { RING_PICK = null; }
  else {
    RING_PICK = { idxs: () => new Set(idxList), click: onClick, extra: extra || null };
  }
  // 立刻按新状态重画一次（state 再来之前环也要跟着变"可点/不可点"）
  if (lastState) renderRing(lastState);
}

/* 在当前题目的候选卡里，找到 idx 对应的那张点掉（= 环点击的转发出口）。
   ★ 复用卡片自己的 onclick，所以"选中样式 / answer 提交"这些逻辑【只有一份】，
     环这边不重复实现 —— 将来改选人逻辑不会两边走样。 */
function pickOne(sel, idx) {
  const btn = document.querySelector(sel + '[data-v="' + idx + '"]');
  if (btn && !btn.disabled) btn.onclick();
}

/* ---------- 【v0.53】选牌卡片的悬停简介 ----------
   卡片里原来直接铺一整行 p.line（「生命之花」197 字 → 6 行），卡片体积被撑得很大。
   现在卡里只留 编号 / 名字 / 耗种，整段简介放在这个浮层里，悬停才出现。

   ★ 为什么是 body 下的 position:fixed 单例，而不是 .pick 里一个 :hover 显示的子元素：
     #pk-list 是 overflow-y:auto 的滚动容器，卡内绝对定位的浮层会被容器裁掉一半。
   ★ 文字直接切服务端的 p.line，前端【不】自己拼"攻 / 防 / 说明"的文案 ——
     否则 plants_data.line() 一改，卡片和浮层就会两套说法（v0.50 的教训：
     同一份数据两个出口，迟早走样）。
   ★ pointer-events:none：浮层不吃鼠标事件，横向扫过一排卡不会闪来闪去。 */
let PTIP = null;

function plantTipEl() {
  if (!PTIP) {
    PTIP = document.createElement('div');
    PTIP.id = 'ptip';
    document.body.appendChild(PTIP);
  }
  return PTIP;
}

/* p.line 的格式是「编号 名字｜耗种…｜攻 …｜防 …｜说明」：
   第一段卡片上已经有了（编号+名字）→ 扔掉；"攻/防"两段做成小胶囊；其余走正文。 */
function plantTipHtml(p) {
  const seg = String(p.line || '').split('｜');
  seg.shift();
  let chips = '', body = '';
  seg.forEach(s => {
    if (/^[攻防]\s/.test(s)) chips += '<span class="ptip-chip">' + esc(s) + '</span>';
    else body += '<div class="ptip-row">' + esc(s) + '</div>';
  });
  return '<div class="ptip-head"><span class="ptip-no">' + esc(p.no || '??') + '</span>' +
    esc(p.name) + '</div>' +
    (chips ? '<div class="ptip-chips">' + chips + '</div>' : '') + body;
}

function showPlantTip(host, p) {
  const t = plantTipEl();
  t.innerHTML = plantTipHtml(p);
  t.style.display = 'block';
  t.style.visibility = 'hidden';          // 先摆出来量高度，再决定朝下还是朝上
  const r = host.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const w = t.offsetWidth, h = t.offsetHeight;
  const left = Math.min(Math.max(8, r.left), Math.max(8, vw - w - 8));
  let top = r.bottom + 8;
  if (top + h > vh - 8) top = Math.max(8, r.top - h - 8);   // 下面放不下 → 翻到上面
  t.style.left = left + 'px';
  t.style.top = top + 'px';
  t.style.visibility = 'visible';
}

function hidePlantTip() {
  if (PTIP) PTIP.style.display = 'none';
}

/* 视口变了、或者任何一层容器滚了 → 浮层位置就失效了，直接收掉（宁可闪一下也别飘着） */
window.addEventListener('resize', hidePlantTip);
window.addEventListener('scroll', hidePlantTip, true);

function renderPlant(ev) {
  const own = ev.own || {};
  // 用服务端发来的候选清单（它才是权威的），富数据从图鉴里取
  let list = catalog;
  if (ev.catalog) {
    list = ev.catalog.map(x => catalog.find(p => p.name === x.name) ||
      { name: x.name, line: x.line, cost_seed: 0, cost_hp: 0 });
  }
  // 搜索框：植物多了以后靠它快速定位。
  //   输数字 → 按编号搜：完整编号（"45"→水蜜桃）或耗种段（"4"→41/45 全出来）；
  //   输文字 → 按名称搜（也顺带匹配说明文字，如"免疫""连击"）。
  let h = '<div class="qtip">出牌阶段 —— 选一株植物（暗选，全员同时）</div>' +
    '<div class="qsub">你的生命 ' + own.hp + ' · 种子 ' + own.seeds + '</div>' +
    '<div class="pk-search">' +
    '<input id="pk-q" type="text" placeholder="搜编号（4=耗种4，45=水蜜桃）或名称" autocomplete="off">' +
    '<span class="pk-count" id="pk-count"></span></div>' +
    '<div class="picker" id="pk-list"></div>';
  setDeck(h);
  const render = () => {
    hidePlantTip();               // 卡片马上要重画，旧浮层指向的那张已经不在了
    const q = ($('pk-q').value || '').trim();
    const shown = q ? list.filter(p => pickMatch(p, q)) : list;
    $('pk-count').textContent = q ? (shown.length + '/' + list.length + ' 株') : '';
    let cards = '';
    shown.forEach(p => {
      // 【v0.53】卡片瘦身：不再把整条 p.line 铺进卡里。
      //   「生命之花」那条 p.line 有 197 字，在 148px 宽的卡里能撑 6 行 ——
      //   一屏只看得见四五株。现在卡里只留 编号 / 名字 / 耗种，
      //   整段简介挪到【悬停浮层】（showPlantTip，见本文件 renderPlant 上方）。
      cards += '<button class="pick" data-v="' + p.name + '">' +
        '<div class="pick-top">' +
        '<span class="pick-art" style="' + artStyle(p.name) + '">' + p.name.charAt(0) + '</span>' +
        '<span><span class="pick-no">' + (p.no || '??') + '</span>' +
        '<span class="pick-name">' + p.name + '</span><br>' +
        '<span class="pick-cost">' + costText(p) + '</span></span></div></button>';
    });
    if (!cards) cards = '<div class="hint">没有匹配「' + q + '」的植物</div>';
    $('pk-list').innerHTML = cards;
    bindPick('.pick', ev);
  };
  $('pk-q').oninput = render;

  // 【v0.53】悬停 = 展开简介。
  //   用【事件委托】挂在容器上：搜索框每敲一个字都会重建卡片，逐个绑会重复绑。
  //   键盘 Tab 聚焦也走同一条路（onfocusin），不然纯键盘玩家看不到简介。
  const tipFor = (el) => {
    if (!el || el.disabled) { hidePlantTip(); return; }
    const p = list.find(x => x.name === el.dataset.v);
    if (p) showPlantTip(el, p); else hidePlantTip();
  };
  const box = $('pk-list');
  box.onpointerover = (e) => tipFor(e.target.closest('.pick'));
  box.onpointerleave = hidePlantTip;
  box.onfocusin = (e) => tipFor(e.target.closest('.pick'));
  box.onfocusout = hidePlantTip;
  box.onscroll = hidePlantTip;

  render();
}

/* 搜索匹配：数字查编号（耗种段优先命中一族），文字查名称和说明 */
function pickMatch(p, q) {
  if (/^\d+$/.test(q)) {
    return p.no === q || p.no_prefix === q;
  }
  const hay = (p.name || '') + ' ' + (p.desc || '') + ' ' + (p.line || '');
  return hay.indexOf(q) >= 0;
}

function costText(p) {
  const c = [];
  if (p.cost_seed) c.push(p.cost_seed + ' 种子');
  if (p.cost_hp) c.push((p.cost_hp / 2) + ' 条命');
  return c.length ? c.join(' + ') : '免费';
}

function renderRelease(ev) {
  setDeck('<div class="qtip">要释放【' + ev.plant + '】吗？</div>' +
    '<div class="qsub">不释放 = 本回合属性全部归零、没有任何效果，出牌时已付的代价也不退。</div>' +
    '<div class="row"><button class="btn primary" data-v="1">释放</button>' +
    '<button class="btn danger" data-v="0">不释放（当空过）</button></div>');
  bindPick('.btn', ev);
}

function renderCharge(ev) {
  const own = ev.own || {};
  const seeds = own.seeds || 0;
  // 【v0.17】说明由服务端按【当前这株】发下来（own.plant / own.hint）——
  //   以前这里写死了大喷菇的公式，人参果充能时也显示"攻击力 = 充能数 + 1"。
  const plant = own.plant || '充能植物';
  const hint = own.hint || '把种子当场换成攻防';
  // 【v0.70】被[勾取]的充能植物由勾取者代答 —— 不写出来玩家会以为是自己的植物。
  //   own.proxy 由引擎生成（_proxy_label），前端只负责显示，不做规则判断。
  const proxy = own.proxy || '';
  setDeck(qfrom(own) + '<div class="qtip">充能</div>' +
    (proxy ? '<div class="qsub devour-hint">★ ' + esc(proxy) + '</div>' : '') +
    '<div class="qsub">' + hint + '。现有 ' + seeds +
    ' 颗种子；充能数超过现有种子会【整次作废】，视为充能 0。</div>' +
    '<div class="numrow"><input id="num" type="number" min="0" max="' + seeds + '" value="0">' +
    '<button class="btn primary" id="ok">确定</button></div>');
  $('ok').onclick = () => {
    let v = parseInt($('num').value, 10);
    if (isNaN(v) || v < 0) v = 0;
    answer(v);
  };
}

function targetSub(c, noTeam) {
  // devour 是引擎算好的三态：null/undefined = 不是噬魂类；true/false = 吞得动/吞不动。
  //   有效防 / 可夺种 / 吞不吞得动 全部【引擎算、前端印】—— 前端不做任何规则判断。
  //   【v0.62】末尾补一个队伍徽标（同队 / 队N）。调用方已经自己写了"已是队友"
  //   这类文字时传 noTeam=true，免得两套说法并排。
  const tb = noTeam ? '' : teamBadge(c.idx);
  if (c.devour === null || c.devour === undefined) {
    return '生命 ' + c.hp + ' · ' + c.plant + '（防' + c.fang + '）' + tb;
  }
  const eff = (c.fang_eff === undefined) ? c.fang : c.fang_eff;
  return '生命 ' + c.hp + ' · ' + c.plant + '（有效防' + eff + '）' +
    ' · 可夺种 ' + (c.seeds || 0) + ' · ' +
    (c.devour ? '<span class="ok">★吞得动</span>'
              : '<span class="no">×吞不动</span>') + tb;
}

// 【v0.72】统一题头：先说清"这是【谁的】哪株植物在做什么"。
//   用户 2026-10-27 的截图里只有一个裸的"选择攻击目标"——
//   替被勾的聚电蓝莓选目标时，玩家根本看不出这一问跟谁有关。
//   own.from  = 那株植物的主人（代答时 = 被勾/被收的那一家）
//   own.plant = 植物名（由引擎发，前端不查表）
function qfrom(own) {
  own = own || {};
  if (!own.plant) return '';
  const who = own.from ? ('来自 <b>' + esc(own.from) + '</b> 的') : '你的';
  return '<div class="qfrom">' + who + '<b>【' + esc(own.plant) + '】</b></div>';
}

// own.proxy 非空 = 这一问是替别人答的（勾取/聚电的"如果勾取成功……"）
function qproxy(own) {
  const p = (own || {}).proxy || '';
  return p ? '<div class="qsub devour-hint">★ ' + esc(p) + '</div>' : '';
}

function renderTarget(ev) {
  const shot = ev.shot || 0;
  const own = ev.own || {};
  const tip = shot
    ? '连击第 ' + shot + '/' + ev.shots + ' 发 —— 这一发给谁？（可以重复选同一个人）'
    : '选择攻击目标';
  let h = qfrom(own) + qproxy(own);
  // 【v0.21】噬魂类：出手门槛必须摊在挑人之前，否则玩家只能瞎猜。
  //   文案由植物自己声明（服务端 own.hint），前端只搬运，不复述规则。
  if (own.hint) {
    h += '<div class="qsub devour-hint">★【' + (own.plant || '') + '】' + own.hint + '</div>';
  }
  h += '<div class="qtip">' + tip + '</div><div class="targets">';
  ev.cands.forEach(c => {
    let cls = 'tcard';
    if (c.devour === false) cls += ' hard';   // 吞不动的压暗，但【仍可点】
    h += '<button class="' + cls + '" data-v="' + c.idx + '">' +
      '<div class="t-name">' + (c.name_tag || c.name) + '</div>' +
      '<div class="t-sub">' + targetSub(c) + '</div></button>';
  });
  setDeck(h + '</div>');
  bindPick('.tcard', ev);
  // 【v0.36】这道题也允许【点存活环】来选（同一个答案出口）。
  setRingPick(ev.cands.map(c => c.idx), idx => pickOne('.tcard', idx));
}

function renderTargets(ev) {
  const need = ev.count;
  const picked = [];
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own) +
    '<div class="qtip">散连击：选 ' + need + ' 个【不同】的目标，每人挨 1 下</div>' +
    '<div class="qsub">已选 <b id="cnt">0</b>/' + need + '</div><div class="targets">';
  ev.cands.forEach(c => {
    h += '<button class="tcard" data-v="' + c.idx + '">' +
      '<div class="t-name">' + (c.name_tag || c.name) + '</div>' +
      '<div class="t-sub">' + targetSub(c) + '</div></button>';
  });
  h += '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="ok" disabled>确定</button></div>';
  setDeck(h);
  document.querySelectorAll('.tcard').forEach(b => {
    b.onclick = () => {
      const v = parseInt(b.dataset.v, 10);
      const i = picked.indexOf(v);
      if (i >= 0) { picked.splice(i, 1); b.classList.remove('sel'); }
      else {
        if (picked.length >= need) return;
        picked.push(v); b.classList.add('sel');
      }
      $('cnt').textContent = picked.length;
      $('ok').disabled = picked.length !== need;
    };
  });
  $('ok').onclick = () => {
    document.querySelectorAll('.tcard').forEach(x => { x.disabled = true; });
    answer(picked);
  };
}

/* 【v0.44】连击发数分配：列出所有可选目标，给每个填 0~N 发，合计必须 = N。
   ★ 取代老做法（逐发问 N 次）—— 141 石榴机枪 15 发要问 15 次，用户嫌蠢。
   ★ 顺手：点存活环上的目标 = 给它 +1 发（分配时不用去点小数字框）。 */
function renderBurstSplit(ev) {
  const shots = ev.shots || 1;
  const cands = ev.cands || [];
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own);
  if (own.hint) {
    h += '<div class="qsub devour-hint">★【' + (own.plant || '') + '】' + own.hint + '</div>';
  }
  h += '<div class="qtip">【' + (ev.plant || '连击植物') + '】连击 ' + shots +
    ' 发 —— 分配每发打谁 ' +
    '<span class="qsub">（可全压一人、可分散；合计 = ' + shots +
    '；点存活环上的目标 = +1 发）</span></div><div class="targets">';
  cands.forEach((c, i) => {
    h += '<div class="tcard burst" data-idx="' + c.idx + '">' +
      '<div class="t-name">' + (c.name_tag || c.name) + '</div>' +
      '<div class="t-sub">' + targetSub(c) + '</div>' +
      '<div class="alloc">' +
      '<button class="mini" data-d="-">−</button>' +
      '<input class="cnt" type="number" min="0" max="' + shots + '" value="' +
      (i === 0 ? shots : 0) + '" data-idx="' + c.idx + '">' +
      '<button class="mini" data-d="+">＋</button></div></div>';
  });
  h += '</div><div class="qsub">合计 <b id="alloc-total">' + shots + '</b> / ' + shots +
    '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="alloc-ok">确定</button>' +
    '<button class="btn" id="alloc-0">全部清零</button></div>';
  setDeck(h);

  const inputs = {};
  document.querySelectorAll('.tcard.burst .cnt').forEach(inp => {
    inputs[parseInt(inp.dataset.idx, 10)] = inp;
    inp.oninput = refresh;
  });
  document.querySelectorAll('.tcard.burst .mini').forEach(b => {
    b.onclick = () => {
      const idx = parseInt(b.closest('.tcard.burst').dataset.idx, 10);
      const inp = inputs[idx];
      let v = parseInt(inp.value, 10); if (isNaN(v)) v = 0;
      v += (b.dataset.d === '+' ? 1 : -1);
      if (v < 0) v = 0; if (v > shots) v = shots;
      inp.value = v; refresh();
    };
  });
  function total() {
    let s = 0;
    Object.keys(inputs).forEach(k => {
      const v = parseInt(inputs[k].value, 10); if (v > 0) s += v;
    });
    return s;
  }
  function refresh() {
    const s = total();
    document.getElementById('alloc-total').textContent = s;
    document.getElementById('alloc-ok').disabled = (s !== shots);
  }
  document.getElementById('alloc-0').onclick = () => {
    Object.keys(inputs).forEach(k => { inputs[k].value = 0; }); refresh();
  };
  document.getElementById('alloc-ok').onclick = () => {
    const out = [];
    Object.keys(inputs).forEach(k => {
      const v = parseInt(inputs[k].value, 10);
      if (v > 0) out.push([parseInt(k, 10), v]);
    });
    answer(out);
  };
  // 点存活环 = 给该目标 +1 发（满了就不加，让玩家自己去减）
  setRingPick(cands.map(c => c.idx), idx => {
    const inp = inputs[idx]; if (!inp) return;
    let v = parseInt(inp.value, 10); if (isNaN(v)) v = 0;
    if (v < shots) { inp.value = v + 1; refresh(); }
  });
  refresh();
}

function renderSeedHeir(ev) {
  // 【v0.48】死亡时的种子瓜分：默认平均，可以改。
  //   答案 = [[编号, 颗数], ...]，合计必须 = ev.total。
  //   ★ 这是唯一一道问"已出局的人"的题 —— 他此刻正在观战，
  //     所以文案要明确说"你留下了种子"，别让人以为是别人的操作。
  const total = ev.total || 0;
  const cands = ev.cands || [];
  const dflt = {};
  (ev.defaults || []).forEach(x => { dflt[x.idx] = x.n; });
  let h = '<div class="qtip">【种子瓜分】你留下了 <b>' + total +
    '</b> 颗种子 —— 默认这样分：</div>' +
    '<div class="qsub">合计必须 = ' + total + ' 颗；不改就照默认走。</div>' +
    '<div class="targets">';
  if (!cands.length) {
    // 【v0.50】防御：理论上引擎不会在"没有人可接"时发这道题
    //   （seed_pools 为空就直接消散、不问）。但真发下来时必须喊出来 ——
    //   v0.48 就是因为 cands 没进 payload，这里静默渲染成一片空白 + 灰色按钮，
    //   玩家只能看着屏幕干等（用户 2026-10-19 报的就是这个）。
    h += '<div class="tcard burst"><div class="t-sub">' +
      '（没有可分配的接收者 —— 请把这一屏反馈给开发者）</div></div>';
  }
  cands.forEach(c => {
    // 这一题关心的是"他现在手上多少种子"，而不是噬魂那种"可夺种"，
    //   所以副标题自己拼，不复用 targetSub（它会去念 devour 门槛）。
    const sub = '生命 ' + (c.hp || '?') + ' · 场上 ' + (c.plant || '?') +
      '（防' + (c.fang === undefined ? '?' : c.fang) + '）' +
      ' · 现有种子 ' + (c.seeds === undefined ? '?' : c.seeds);
    h += '<div class="tcard burst" data-idx="' + c.idx + '">' +
      '<div class="t-name">' + (c.name_tag || c.name) + '</div>' +
      '<div class="t-sub">' + sub + '</div>' +
      '<div class="alloc">' +
      '<button class="mini" data-d="-">−</button>' +
      '<input class="cnt" type="number" min="0" max="' + total + '" value="' +
      (dflt[c.idx] || 0) + '" data-idx="' + c.idx + '">' +
      '<button class="mini" data-d="+">＋</button></div></div>';
  });
  h += '</div><div class="qsub">合计 <b id="alloc-total">0</b> / ' + total +
    '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="alloc-ok">确定</button></div>';
  setDeck(h);

  const inputs = {};
  document.querySelectorAll('.tcard.burst .cnt').forEach(inp => {
    inputs[parseInt(inp.dataset.idx, 10)] = inp;
    inp.oninput = refresh;
  });
  document.querySelectorAll('.tcard.burst .mini').forEach(b => {
    b.onclick = () => {
      const idx = parseInt(b.closest('.tcard.burst').dataset.idx, 10);
      const inp = inputs[idx];
      let v = parseInt(inp.value, 10); if (isNaN(v)) v = 0;
      v += (b.dataset.d === '+' ? 1 : -1);
      if (v < 0) v = 0; if (v > total) v = total;
      inp.value = v; refresh();
    };
  });
  function total2() {
    let s = 0;
    Object.keys(inputs).forEach(k => {
      const v = parseInt(inputs[k].value, 10); if (v > 0) s += v;
    });
    return s;
  }
  function refresh() {
    const s = total2();
    document.getElementById('alloc-total').textContent = s;
    document.getElementById('alloc-ok').disabled = (s !== total);
  }
  document.getElementById('alloc-ok').onclick = () => {
    const out = [];
    Object.keys(inputs).forEach(k => {
      const v = parseInt(inputs[k].value, 10);
      if (v > 0) out.push([parseInt(k, 10), v]);
    });
    answer(out);
  };
  refresh();
}

function renderRevive(ev) {
  // 【v0.51】161 生命之花：加血 / 复活【二选一】。
  //   答案 = {mode:'heal'} 或 {mode:'revive', target:编号}。
  //   ★ 复活的人【不在存活环上】（他已经出局了），所以只能一排卡片按钮，
  //     不能复用 setRingPick。副标题要明确写出"复活后他会拿回什么"。
  const cands = ev.cands || [];
  const own = ev.own || {};
  const heal = ev.heal === undefined ? '?' : ev.heal;
  let h = qfrom(own) + qproxy(own) +
    '<div class="qtip">【' + (ev.plant || '生命之花') +
    '】二选一 —— 只能选一个：</div>' +
    '<div class="qsub">选「加血」就只加血；选「复活」就只把人拉回来，不再加血。' +
    '复活后他会与你<b>连成队友</b>。</div>';
  h += '<div class="row" style="margin-top:8px">' +
    '<button class="btn primary" id="rev-heal">(1) 自己 +' + heal + ' 条命</button>' +
    '</div>';
  if (cands.length) {
    h += '<div class="qsub" style="margin-top:10px">(2) 复活一个已死亡的人：</div>' +
      '<div class="targets">';
    cands.forEach(c => {
      const sub = '恢复 ' + (c.hp || '?') + ' 条命 · 取回 ' +
        (c.seeds === undefined ? '?' : c.seeds) + ' 颗种子 · 死于第 ' +
        (c.died_round === undefined ? '?' : c.died_round) + ' 回合';
      h += '<div class="tcard burst" data-idx="' + c.idx + '">' +
        '<div class="t-name">' + (c.name_tag || c.name) + '</div>' +
        '<div class="t-sub">' + sub + '</div>' +
        '<button class="btn" data-rev="' + c.idx + '">复活他</button></div>';
    });
    h += '</div>';
  } else {
    // 引擎正常情况下不会发空候选（没得救就直接按加血走）。
    //   真发下来时必须喊出来，别让它静默变成一片空白。
    h += '<div class="qsub" style="margin-top:8px">' +
      '（没有可复活的人 —— 本回合刚死的不算；只能选加血）</div>';
  }
  setDeck(h);
  document.getElementById('rev-heal').onclick = () =>
    answer({ mode: 'heal', target: null });
  document.querySelectorAll('[data-rev]').forEach(b => {
    b.onclick = () => answer({
      mode: 'revive', target: parseInt(b.dataset.rev, 10)
    });
  });
}

function renderSplash(ev) {
  // 【v0.27】溅射声明：与"散连击"同一款多选，区别是【可以一个都不选】。
  //   "两侧是谁"由服务端算好（存活环上目标的 index 相邻者，且不含你自己）——
  //   前端只排版，不做任何规则判断。
  const own = ev.plant || '溅射植物';
  const who = (ev.target && (ev.target.name_tag || ev.target.name)) || '目标';
  const sides = ev.sides || [];
  const picked = [];
  const sideName = ['前一侧', '后一侧'];
  let h = '<div class="qtip">【' + own + '】若对 ' + who +
    ' 造成伤害 → 向两侧溅射吗？<br>每侧 0.5 条命 · 暴2；一个都不勾 = 不溅射。</div>' +
    '<div class="qsub">已选 <b id="cnt">0</b>/' + sides.length + ' 侧</div>' +
    '<div class="targets">';
  sides.forEach((c, n) => {
    h += '<button class="tcard" data-v="' + c.idx + '">' +
      '<div class="t-name">[' + (sideName[n] || ('第' + (n + 1) + '侧')) + '] ' +
      (c.name_tag || c.name) + '</div>' +
      '<div class="t-sub">' + targetSub(c) + '</div></button>';
  });
  h += '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="ok">确定</button></div>';
  setDeck(h);
  document.querySelectorAll('.tcard').forEach(b => {
    b.onclick = () => {
      const v = parseInt(b.dataset.v, 10);
      const i = picked.indexOf(v);
      if (i >= 0) { picked.splice(i, 1); b.classList.remove('sel'); }
      else { picked.push(v); b.classList.add('sel'); }
      $('cnt').textContent = picked.length;
    };
  });
  $('ok').onclick = () => {
    document.querySelectorAll('.tcard').forEach(x => { x.disabled = true; });
    answer(picked);
  };
}

function renderWaterHp(ev) {
  setDeck('<div class="qtip">用 1 个水滴抵掉【' + ev.plant + '】的半条命代价吗？</div>' +
    '<div class="qsub">现有水滴 ' + ev.own.water + ' · 生命 ' + ev.own.hp +
    '。抵掉的话，出牌时先扣的那半条命会退还。</div>' +
    '<div class="row"><button class="btn primary" data-v="1">用水滴抵</button>' +
    '<button class="btn" data-v="0">照扣生命</button></div>');
  bindPick('.btn', ev);
}

function renderWaterDef(ev) {
  const w = ev.own.water;
  setDeck('<div class="qtip">水滴加固：1 个水滴 = +1 防御</div>' +
    '<div class="qsub">现有水滴 ' + w + ' · 当前防御 ' + ev.own.fang + '</div>' +
    '<div class="numrow"><input id="num" type="number" min="0" max="' + w + '" value="0">' +
    '<button class="btn primary" id="ok">确定</button></div>');
  $('ok').onclick = () => {
    let v = parseInt($('num').value, 10);
    if (isNaN(v) || v < 0) v = 0;
    if (v > w) v = w;
    answer(v);
  };
}

/* ---------- 小工具 ---------- */
function setDeck(html) { hidePlantTip(); $('deck-body').innerHTML = html; }
function setDeckTitle(t) { $('deck-title').textContent = t; }
function setErr(id, text) { const e = $(id); if (e) e.textContent = text || ''; }

/* 【v0.63】"等待别人"的那一阵，自己也要看得见 —— 用户 2026-10-23：
     "等待别人选植物的等待期，看不到自己的植物，存活环下面那个任务卡片
      完全可以显示下自己的植物" / "看自己血"。
   数据只读最近一帧 state（lastState），不问服务端任何额外东西；
   一个字段都不在这里算，全是引擎下发值的搬运。 */
function mySummaryHtml() {
  const st = lastState;
  if (!st || !st.players || !ME) return '';
  const me = st.players.find(p => p.idx === ME);
  if (!me) return '';
  let plant;
  if (!me.alive) {
    plant = '<span class="ms-warn">已出局</span>';
  } else if (!me.plant) {
    plant = '<span class="ms-dim">（空场）</span>';
  } else {
    const s = me.plant;
    let tail = '防 ' + s.fang;
    if (s.broken) tail += ' · <span class="ms-warn">已被击破</span>';
    else if (s.inert) tail += ' · <span class="ms-warn">未释放</span>';
    plant = '【' + esc(s.name) + '】' + tail;
  }
  let extra = '';
  if (me.water) extra += ' · 水滴 <b>' + me.water + '</b>';
  if (me.fire) extra += ' · 火种 <b>' + me.fire + '/' + me.fire_burst_at + '</b>';
  if (me.super_def) extra += ' · <span class="ms-super">超防 ' + me.super_def + ' 回合</span>';
  if (me.immune_turns) {
    extra += ' · <span class="ms-immune">免疫 ' + me.immune_turns + ' 回合</span>';
  }
  if (me.fishy) extra += ' · 鱼腥味 ' + me.fishy;
  if (me.mult) extra += ' · 倍耗 ' + me.mult;
  return '<div class="mysum">' +
    '<div class="ms-row"><span class="ms-k">我的植物</span>' + plant + '</div>' +
    '<div class="ms-row"><span class="ms-k">生命</span><b>' + (me.hp_text || me.hp) +
    '</b> · 种子 <b>' + me.seeds + '</b>' + extra + '</div>' +
    '</div>';
}

function showWait(text) {
  setDeckTitle('你的操作');
  setDeck('<div class="waiting"><span class="dots"><i></i><i></i><i></i></span>' +
    '<span>' + (text || '等待其他玩家决策……') + '</span></div>' + mySummaryHtml());
  setRingPick(null);      // 【v0.36】交卷/等待期间环上不该还能点人
}

/* 出局后：牌局还在继续，我不是离桌，只是没操作要做 —— 所以场面照看、战报照刷。
   ★【v0.71】复活机制已经有了（161 生命之花可把出局的人拉回来），
     所以这里不能再说"后续版本会加入" —— 要说清"现在就可能被复活"。 */
function showSpectate() {
  setDeckTitle('观战');
  setDeck('<div class="spectate">' +
    '<div class="spec-big">你已出局 · 观战中</div>' +
    '<div class="spec-sub">牌局还在继续 —— 场面和战报都会照常刷新，你只是没有操作要做。<br>' +
    '别走开：场上若有人打出【生命之花】之类的复活植物，你会<b>重新回到牌桌</b>，' +
    '到时候还有你的回合。</div></div>' + mySummaryHtml());
  setRingPick(null);      // 【v0.36】观战时环只读
}

function onWait(text) {
  if (MODE === 'over') return;            // 结束了就别再动面板
  if (!MY_ALIVE) return showSpectate();   // 我出局了：显示观战，不是"等待"
  if (!busy) showWait(text); else showWait();
}

/* 从最近一帧场面里取"名字(编号)" —— 结束消息只带 idx，名字得自己查。 */
function nameOf(idx) {
  const st = lastState;
  if (st && st.players) {
    const q = st.players.find(p => p.idx === idx);
    if (q) return q.name_tag || q.name || ('player' + idx);
  }
  return 'player' + idx;
}

/* ---------- 结束 ---------- */
function showOver(ev) {
  MODE = 'over';
  busy = true;
  setRingPick(null);      // 【v0.36】结束了 → 环退回只读
  // 【v0.45】获胜者可能是【一队人】（同一队友链共同获胜）→ 优先读 winners。
  const wins = (ev.winners && ev.winners.length) ? ev.winners
    : ((ev.winner === undefined || ev.winner === null) ? [] : [ev.winner]);
  // 【v0.63】★★ 玩家只需要知道"我赢了还是输了"，谁赢是次要的 ——
  //   用户 2026-10-23："输赢提示不够明确，其实你只要知道自己输了赢了就可以，
  //   谁赢了是次要的，应该小字。输赢最好颜色不同方便识别"。
  const iWin = wins.indexOf(ME) >= 0;
  let cls = 'draw', big = '平局';
  if (wins.length) {
    cls = iWin ? 'win' : 'lose';
    big = iWin ? '你赢了' : '你输了';
  }
  const who = wins.length
    ? (wins.map(nameOf).join('、') + ' 获胜' +
       (wins.length > 1 ? '（同一队友链，共同获胜）' : ''))
    : '没有人活到最后';
  let h = '<div class="over ' + cls + '">' +
    '<div class="big">' + big + '</div>' +
    '<div class="sub">' + esc(who) + '</div>';
  // 【v0.63】同一进程内的战绩（服务端不重启就一直累计，见 netserver 的 wins）
  const rec = ev.record;
  if (rec && rec.games) {
    const mine = rec.wins && rec.wins[nameKeyOfMe()];
    h += '<div class="sub dim">本服战绩：共 ' + rec.games + ' 局' +
      (mine ? ' · 你 ' + mine + ' 胜' : '') +
      (rec.last ? ' · 上一局：' + esc(rec.last) : '') + '</div>';
  }
  h += '<div class="btns">' +
    '<button class="btn primary" id="btn-again">再来一局</button>' +
    '<button class="btn" id="btn-other">换个服务器</button>' +
    '</div><div class="err" id="again-err"></div></div>';
  setDeckTitle('对局结束');
  setDeck(h);
  $('btn-again').onclick = doRematch;
  $('btn-other').onclick = () => {
    MODE = 'connect';
    MY_ALIVE = true;
    busy = false;
    fetch('/api/disconnect', { method: 'POST' });
    showConnect('');
  };
  addLog(ev.text || '游戏结束');
}

/* 战绩表用玩家名作键（服务端也是），这里取"我"的显示名。 */
function nameKeyOfMe() {
  const st = lastState;
  if (st && st.players) {
    const q = st.players.find(p => p.idx === ME);
    if (q) return q.name || ('player' + ME);
  }
  return 'player' + ME;
}

function doRematch() {
  const b = $('btn-again');
  if (b) b.disabled = true;
  setErr('again-err', '正在重开……');
  fetch('/api/rematch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: '{}' })
    .then(r => r.json()).then(d => {
      if (!d.ok) {
        if (b) b.disabled = false;
        setErr('again-err', d.err || '重开失败');
      }
      // 成功的话由 conn 事件接管：会 resetBoard 并切回对局界面
    })
    .catch(e => {
      if (b) b.disabled = false;
      setErr('again-err', '请求失败：' + e);
    });
}

function addLog(text) {
  if (!text) return;
  const box = $('log');
  const d = document.createElement('div');
  d.textContent = text;
  if (/损失|出局|击破|爆发|打穿|消除|互挡/.test(text)) d.className = 'dmg';
  else if (/获得|获胜|回血/.test(text)) d.className = 'good';
  else if (/——|===|\*\*\*|阶段/.test(text)) d.className = 'sys';
  box.appendChild(d);
  while (box.children.length > 300) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

/* ---------- 【v0.32】刀类（消除 / 废除）--------- */
function renderCutTarget(ev) {
  // 候选已在服务端按"射程 + 免切"过滤好，前端只排版、不做任何规则判断。
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own);
  if (own.hint) {
    h += '<div class="qsub devour-hint">★【' + (own.plant || '') + '】' + own.hint + '</div>';
  }
  h += '<div class="qtip">刀类 —— 选择要消除谁的植物</div><div class="targets">';
  ev.cands.forEach(c => {
    h += '<button class="tcard" data-v="' + c.idx + '">' +
      '<div class="t-name">' + (c.name_tag || c.name) + '</div>' +
      '<div class="t-sub">' + targetSub(c) + '</div></button>';
  });
  // 【v0.44】"不切"是一个正当答案（释放刀类不代表一定要选目标，可能只想要防御）。
  //   文案由服务端 skip_label 给，前端不复述规则。
  h += '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn" id="cut-skip">✕ ' +
    (ev.skip_label || '不切任何人（保留防御）') + '</button></div>';
  setDeck(h);
  bindPick('.tcard', ev);
  const skip = document.getElementById('cut-skip');
  if (skip) {
    skip.onclick = () => { skip.disabled = true; answer(null); };
  }
  // 【v0.36】切谁也能点环来选（与选攻击目标同一个出口）；
  //   【v0.44】环旁边多一个"空目标"按钮 = 等同于上面的「不切」。
  setRingPick(ev.cands.map(c => c.idx), idx => pickOne('.tcard', idx),
              { label: '✕ 空目标（不切）',
                onClick: () => { if (skip && !skip.disabled) skip.onclick(); } });
}

function renderCutMode(ev) {
  const tgt = ev.target || {};
  const same = ev.same || [];
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own) +
    '<div class="qtip">连切 —— 要消除 ' + (tgt.name_tag || tgt.name || '?') +
    ' 的【' + (tgt.plant || '?') + '】<br>场上同名卡共 ' + same.length + ' 株</div>' +
    '<div class="qsub">' + same.map(s => (s.name_tag || s.name) + ' 的【' + s.plant + '】').join('、') +
    '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn" data-v="single">只切这一株</button>' +
    '<button class="btn primary" data-v="all">全切同名卡（连切）</button></div>';
  setDeck(h);
  document.querySelectorAll('.row .btn').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('.row .btn').forEach(x => { x.disabled = true; });
      answer(b.dataset.v);
    };
  });
}

function renderCutHalf(ev) {
  const names = (ev.targets || []).map(t => (t.name_tag || t.name) + ' 的【' + t.plant + '】').join('、');
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own) +
    '<div class="qtip">【' + (ev.plant || '刀叶类') + '】对 ' + names +
    ' 要『切一半』还是『全切』？</div>' +
    '<div class="qsub">切一半 = 对方防御保留，但它的负面效果不再附加给你与你指定的人；' +
    '全切 = 对方直接成[空]。</div><div class="row" style="margin-top:10px">' +
    '<button class="btn" data-v="1">切一半</button>' +
    '<button class="btn primary" data-v="0">全切（成[空]）</button></div>';
  setDeck(h);
  document.querySelectorAll('.row .btn').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('.row .btn').forEach(x => { x.disabled = true; });
      answer(b.dataset.v === '1');
    };
  });
}

/* 【v0.32】切一半 → 帮谁切（多选，含自己；0~N） */
function renderCutHelp(ev) {
  const cands = ev.cands || [];
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own) +
    '<div class="qtip">【' + (ev.plant || '刀叶类') +
    '】要帮谁免疫该植物的负面效果？' +
    '<span class="qsub">（可多选，含自己；一个都不选也行）</span></div>' +
    '<div class="grid">';
  cands.forEach(c => {
    h += '<button class="tcard" data-v="' + c.idx + '" data-on="0">' +
      '<b>' + (c.name_tag || c.name) + '</b><span>生命' + c.hp + '｜' + c.plant +
      '(防' + c.fang + ')</span></button>';
  });
  h += '</div><div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="cuthelp-ok">确定</button>' +
    '<button class="btn" id="cuthelp-none">不帮任何人</button></div>';
  setDeck(h);
  const picked = new Set();
  document.querySelectorAll('.tcard').forEach(b => {
    b.onclick = () => {
      const v = b.dataset.v;
      if (picked.has(v)) { picked.delete(v); b.dataset.on = '0'; b.classList.remove('on'); }
      else { picked.add(v); b.dataset.on = '1'; b.classList.add('on'); }
    };
  });
  const ok = document.getElementById('cuthelp-ok');
  ok.onclick = () => {
    ok.disabled = true;
    document.getElementById('cuthelp-none').disabled = true;
    answer(Array.from(picked).map(x => parseInt(x, 10)).sort((a, b) => a - b));
  };
  document.getElementById('cuthelp-none').onclick = () => {
    ok.disabled = true;
    document.getElementById('cuthelp-none').disabled = true;
    answer([]);
  };
}

/* ---------- 【v0.38】电系勾人 ／ 【v0.57】103 牛角菱：勾取 ----------
   ★★ 同一个 kind "absorb" 的两种形态，靠服务端 payload 区分：
     · 没有 pick_count  → 电系勾人：任选 0~N 人（被勾者成[空]）；
     · 有   pick_count  → 勾取：必须【恰好 N 株、且同名】（一株都不选 = 换形态）。
   ★ 数量 / 分组字段 / "不选"按钮文案全部由服务端给（pick_count / group_by /
     none_label），界面只照它画 —— 规则不在界面里写死（老规矩）。
   ★★【v0.62 修】旧版把这两件事都做错了：
     ① 选中态挂的是 `.on` —— CSS 里根本没有这个类 → 点了毫无反应，
        玩家看起来就是"选不中目标"（真正的 bug 在样式对不上，不在点击）；
     ② 勾取要求"恰好 2 株同名"，可界面是无限制自由多选 → 选 3 株 / 选跨组两株
        提交上去一律被判非法 → 服务端回"输入无效，请重新选择"并把原题重发，
        而玩家怎么点都还是非法（因为界面压根不知道有这个约束）→ 只能放弃勾取。
     现在：按 plant 分组渲染、选满 N 株后别的组禁用、跨组点选自动清空重来。 */
function renderAbsorb(ev) {
  const own = ev.own || {};
  const cands = ev.cands || [];
  const need = parseInt(ev.pick_count, 10) || 0;   // 0 = 不限（电系勾人）
  let h = '';
  if (own.hint) {
    h += '<div class="qsub devour-hint">★【' + (own.plant || '') + '】' + own.hint + '</div>';
  }

  if (!need) {
    /* ---- 电系【聚电】：自由多选 0~N ---- */
    // 【v0.70】命名：57 聚电蓝莓的这个动作叫【聚电】，与牛角菱的【勾取】分开
    //   （用户 2026-10-26："电系的勾取换个名字叫[聚电]吧，以便区分"）。
    //   own.proxy 非空 = 这一株是别人勾给我的，由我代答。
    // qfrom 用 own.plant；电系 absorb 的 payload 里 plant 是顶层键，兜底接上
    // （check_payload 会盯着"引擎发了、界面没读"这类漂移）。
    h += qfrom(Object.assign({}, own, { plant: own.plant || ev.plant })) +
      '<div class="qtip">要聚电谁？' +
      '<span class="qsub">（' + (own.hint || '') + '；被收者成[空]，其出手由你决定；' +
      '一个都不收也行）</span></div>' +
      (own.proxy ? '<div class="qsub devour-hint">★ ' + esc(own.proxy) + '</div>' : '') +
      '<div class="grid">';
    cands.forEach(c => {
      h += '<button class="tcard" data-v="' + c.idx + '">' +
        '<b>' + (c.name_tag || c.name) + teamBadge(c.idx) + '</b>' +
        '<span>生命' + c.hp + '｜' + esc(c.plant) + '(防' + c.fang + ')</span></button>';
    });
    h += '</div><div class="row" style="margin-top:10px">' +
      '<button class="btn primary" id="absorb-ok">确定</button>' +
      '<button class="btn" id="absorb-none">不勾任何人</button></div>';
    setDeck(h);
    const picked = new Set();
    document.querySelectorAll('.tcard').forEach(b => {
      b.onclick = () => {
        const v = parseInt(b.dataset.v, 10);
        if (picked.has(v)) { picked.delete(v); b.classList.remove('sel'); }
        else { picked.add(v); b.classList.add('sel'); }
      };
    });
    const ok = document.getElementById('absorb-ok');
    ok.onclick = () => {
      ok.disabled = true;
      document.getElementById('absorb-none').disabled = true;
      answer(Array.from(picked).sort((a, b) => a - b));
    };
    document.getElementById('absorb-none').onclick = () => {
      ok.disabled = true;
      document.getElementById('absorb-none').disabled = true;
      answer([]);
    };
    setRingPick(cands.map(c => c.idx), idx => pickOne('.tcard', idx));
    return;
  }

  /* ---- 【v0.57】勾取：按植物名分组，恰好 need 株同组 ---- */
  // ★【v0.67】分组键由服务端给（group_by），不再写死 c.plant ——
  //   netclient 早就按 `gby or "plant"` 取值了，这里写死的话，将来服务端换一种
  //   分组口径（比如按队伍/按防御），命令行端跟着变、网页端原地不动，两端就分了岔。
  const gk = ev.group_by || 'plant';
  const groups = {};
  cands.forEach(c => { (groups[c[gk]] = groups[c[gk]] || []).push(c); });
  h += '<div class="qtip">勾取 —— 从【同一组】里选中 ' + need + ' 株</div>';
  Object.keys(groups).forEach(name => {
    const arr = groups[name];
    h += '<div class="tgroup' + (arr.length < need ? ' lack' : '') + '">' +
      '<div class="tgroup-head">' + esc(name) +
      '<span class="tgroup-n">' + arr.length + ' 株' +
      (arr.length < need ? '（不足 ' + need + ' 株，凑不齐）' : '') + '</span></div>' +
      '<div class="grid">' +
      arr.map(c => '<button class="tcard" data-v="' + c.idx + '">' +
        '<b>' + (c.name_tag || c.name) + teamBadge(c.idx) + '</b>' +
        '<span>生命' + c.hp + '｜' + esc(c.plant) + '(防' + c.fang + ')</span></button>').join('') +
      '</div></div>';
  });
  h += '<div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="absorb-ok" disabled></button>' +
    '<button class="btn" id="absorb-none">' +
    esc(ev.none_label || '不选（换另一个形态）') + '</button></div>';
  setDeck(h);

  const grpOf = {};                    // idx -> 分组键（服务端定的 group_by）
  cands.forEach(c => { grpOf[c.idx] = c[gk]; });
  const picked = new Set();
  let curGroup = null;
  const ok = document.getElementById('absorb-ok');
  const none = document.getElementById('absorb-none');

  const sync = () => {
    const n = picked.size;
    ok.disabled = (n !== need);
    ok.textContent = (n === need)
      ? '确定（勾取这 ' + need + ' 株）'
      : '确定（已选 ' + n + '/' + need + ' 株）';
    // 选满后：同组内的还能点掉重选，别的组一律禁用 —— 跨组混选永远非法。
    document.querySelectorAll('.tcard').forEach(b => {
      const i = parseInt(b.dataset.v, 10);
      b.disabled = (n >= need) && !picked.has(i);
    });
  };
  document.querySelectorAll('.tcard').forEach(b => {
    b.onclick = () => {
      const i = parseInt(b.dataset.v, 10);
      const g = grpOf[i];
      if (picked.has(i)) {
        picked.delete(i); b.classList.remove('sel');
      } else {
        if (curGroup && curGroup !== g) {
          // 换了一组 → 之前选的作废（两组混着选，服务端一定判非法）
          document.querySelectorAll('.tcard').forEach(x => {
            x.classList.remove('sel'); x.disabled = false;
          });
          picked.clear();
        }
        curGroup = g; picked.add(i); b.classList.add('sel');
      }
      if (!picked.size) curGroup = null;
      sync();
    };
  });
  ok.onclick = () => {
    if (picked.size !== need) return;
    ok.disabled = true; none.disabled = true;
    answer(Array.from(picked).sort((a, b) => a - b));
  };
  none.onclick = () => {
    ok.disabled = true; none.disabled = true;
    answer([]);
  };
  setRingPick(cands.map(c => c.idx), idx => pickOne('.tcard', idx));
  sync();
}

function renderFlash(ev) {
  // [闪电一击]：单选一侧（不弹 → null）。
  // ★★【v0.66 修】这里原来读的是 ev.cands —— 但 flash 的 payload 键名是 **sides**
  //   （PAYLOAD_CONTRACT 里登记的也是 "sides"+"target"）。读错键 → cands 恒为
  //   undefined → 一张候选卡都不画、setRingPick([]) 又把存活环退回只读 →
  //   玩家看到的是"没有往哪弹的选项、环也点不动"，直接卡死在这道题上。
  //   ★ 这是 v0.50 seed_heir / v0.62 absorb 的同一族：**契约里的键名和前端
  //     读的名字，必须有东西盯着**。Test121 现在同时钉 msg() 和本文件源码。
  //   ★ 键名不是"随便定的"——splash（溅射）也用 sides，两者是同一批"两侧"语义，
  //     所以这里跟 renderSplash 保持一致，不改成 cands。
  const tgt = ev.target || {};
  const sides = ev.sides || [];
  const sideName = ['前一侧', '后一侧'];
  // 【v0.72】own.proxy 非空 = 这道题是替被勾的那株答的（"如果勾取成功 ——
  //   你勾取的 X 的【Y】：…"）。引擎给文案，前端只显示，不做规则判断。
  const own = ev.own || {};
  const proxy = own.proxy || '';
  let h = qfrom(own) +
    (proxy ? '<div class="qsub devour-hint">★ ' + esc(proxy) + '</div>' : '') +
    '<div class="qtip">对 ' +
    (tgt.name_tag || tgt.name || '目标') +
    ' 造成伤害后 → 闪电链向哪一侧弹出？<span class="qsub">（两边之一，可都不弹）</span></div>';
  if (!sides.length) {
    h += '<div class="qsub">（场上没有可弹的两侧目标 —— 这一发不会有闪电链）</div>';
  } else {
    h += '<div class="targets">';
    sides.forEach((c, n) => {
      h += '<button class="tcard" data-v="' + c.idx + '">' +
        '<div class="t-name">[' + (sideName[n] || ('第' + (n + 1) + '侧')) + '] ' +
        (c.name_tag || c.name) + '</div>' +
        '<div class="t-sub">' + targetSub(c) + '</div></button>';
    });
    h += '</div>';
  }
  h += '<div class="row" style="margin-top:10px">' +
    '<button class="btn" id="flash-none">不弹闪电链</button></div>';
  setDeck(h);
  bindPick('.tcard', ev);
  setRingPick(sides.map(c => c.idx), idx => pickOne('.tcard', idx));
  document.getElementById('flash-none').onclick = () => {
    document.querySelectorAll('.tcard').forEach(x => { x.disabled = true; });
    document.getElementById('flash-none').disabled = true;
    answer(null);
  };
}

function renderCatSplit(ev) {
  // 【v0.39】猫尾草：连击 shots 发分配到左右两侧 → 答案 [左发数, 右发数]
  const shots = ev.shots || 1;
  const L = ev.left || {}, R = ev.right || {};
  const own = ev.own || {};
  let h = qfrom(own) + qproxy(own) +
    '<div class="qtip">【' + (ev.plant || '猫尾草') + '】连击 ' + shots +
    ' 发 → 存活环上离你最近的是左右这两个，请分配各打几发：' +
    '<span class="qsub">（左右合计 = ' + shots + '）</span></div>' +
    '<div class="row" style="gap:16px">' +
    '<div><div class="t-name">左：' + (L.name_tag || L.name || '?') + '</div>' +
    '<input type="number" id="cat-left" min="0" max="' + shots +
    '" value="' + shots + '" style="width:70px"></div>' +
    '<div><div class="t-name">右：' + (R.name_tag || R.name || '?') + '</div>' +
    '<div class="t-sub">（自动补足 = ' + shots + ' − 左侧）</div></div>' +
    '</div>';
  h += '<div class="row" style="margin-top:10px">' +
    '<button class="btn" id="cat-ok">确定</button></div>';
  setDeck(h);
  // 环上点亮左右两侧，点了也能改左侧发数（点左侧=把剩余全给左；简单起见：点击切换）
  setRingPick([L.idx, R.idx].filter(x => x !== undefined && x !== null), idx => {
    const el = document.getElementById('cat-left');
    if (el) el.value = (idx === L.idx) ? shots : 0;
  });
  document.getElementById('cat-ok').onclick = () => {
    let lc = parseInt(document.getElementById('cat-left').value, 10);
    if (isNaN(lc) || lc < 0) lc = 0;
    if (lc > shots) lc = shots;
    document.getElementById('cat-ok').disabled = true;
    answer([lc, shots - lc]);
  };
}

/* ---------- 【v0.45】队友连接（107 番茄）----------
   一道题收两样：① 连谁（可为空目标）② 多株猪笼草时偏移到哪一株。
   答案 = {target: idx|None, gate: idx|None}。
   ★★【v0.61】"是否改触发[反馈逻辑]"已删 —— 反馈是**强制**的（用户 2026-10-23），
     条件到了引擎自己触发，界面没有这个开关。服务端还会给 cands[i].merge
     = "连上他就必定触发反馈"，这里只在卡片上标一句**预告**。
   ★ 文案/能不能空目标 —— 全部由服务端 m 决定，前端不做规则判断。 */
function renderFriend(ev) {
  const own = ev.own || {};
  const cands = ev.cands || [];
  const gates = ev.gates || [];
  // target: undefined = 还没选（确定按钮灰着）；null = 明确的"空目标"
  const st = { target: undefined, gate: gates.length ? gates[0].idx : null };
  let h = qfrom(own) + qproxy(own);
  if (own.hint) {
    h += '<div class="qsub devour-hint">★【' + (own.plant || '') + '】' + own.hint + '</div>';
  }
  h += '<div class="qtip">队友连接 —— 选择要连接谁</div><div class="targets">';
  cands.forEach(c => {
    h += '<button class="tcard" data-v="' + c.idx + '" data-grp="tg">' +
      '<div class="t-name">' + (c.name_tag || c.name) +
      (c.ally ? '<span class="ok">（已是队友）</span>' : '') + '</div>' +
      '<div class="t-sub">' + targetSub(c, true) +
      (c.merge ? '<span class="no">⚠连上必触发[反馈逻辑]（自身成[空]，得 ' +
        (ev.feedback_value || 9) + ' 颗种）</span>' : '') + '</div></button>';
  });
  h += '</div>';
  if (ev.allow_empty) {
    h += '<div class="row" style="margin-top:8px">' +
      '<button class="btn" data-v="0" data-grp="tg">✕ 空目标（不连任何人、也不加防）</button>' +
      '</div>';
  }
  if (gates.length > 1) {
    h += '<div class="qtip" style="margin-top:10px">场上有多株【猪笼草】' +
      '—— 若它生效，连接偏移到哪一株</div><div class="targets">';
    gates.forEach(g => {
      h += '<button class="tcard" data-v="' + g.idx + '" data-grp="gt">' +
        '<div class="t-name">' + (g.name_tag || g.idx) + '</div>' +
        '<div class="t-sub">' + (g.plant || '猪笼草') + '</div></button>';
    });
    h += '</div>';
  }
  h += '<div class="row" style="margin-top:10px">' +
    '<button class="btn primary" id="friend-ok" disabled>确定</button></div>';
  setDeck(h);

  const ok = document.getElementById('friend-ok');
  const syncOk = () => { ok.disabled = (st.target === undefined); };
  document.querySelectorAll('[data-grp="tg"]').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('[data-grp="tg"]').forEach(x => x.classList.remove('sel'));
      b.classList.add('sel');
      const raw = b.dataset.v;
      st.target = (raw === '0' || raw === '') ? null : parseInt(raw, 10);
      syncOk();
    };
  });
  document.querySelectorAll('[data-grp="gt"]').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('[data-grp="gt"]').forEach(x => x.classList.remove('sel'));
      b.classList.add('sel');
      st.gate = parseInt(b.dataset.v, 10);
    };
  });
  // 多株门 → 默认高亮第一株（st.gate 的初值就是它，界面得跟状态一致）
  if (gates.length > 1) {
    const dflt = document.querySelector('[data-grp="gt"][data-v="' + gates[0].idx + '"]');
    if (dflt) dflt.classList.add('sel');
  }
  ok.onclick = () => {
    ok.disabled = true;
    answer({ target: st.target, gate: st.gate });
  };
  // 点存活环 = 点下面那张候选卡（同一个出口）
  setRingPick(cands.map(c => c.idx), idx => pickOne('[data-grp="tg"]', idx));
  syncOk();
}
