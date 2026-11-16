/* codex.js —— 植物图鉴（独立页面）
 *
 * 数据两块，各取各的：
 *   /api/catalog      → 数值与机制（源头是 plants_data.PLANTS / PLAYABLE）
 *   /api/codex-text   → 风味文案（源头是 codex_text.CODEX_TEXT）
 *
 * ★ 为什么分成两个接口而不是合并成一个：文案和数值是两种东西，
 *   数值改一次所有引用方都要跟着变（对局、图鉴、文档），文案只影响图鉴。
 *   分开之后，改文案永远不会碰到战斗界面。
 * ★ 机制那段【原样显示 plants_data 的 desc】—— 不在这里重写、不在这里精简。
 *   重写就等于开第二个口径，规则一改就会出现「图鉴说 A、卡片说 B」。
 */

(function () {
  'use strict';

  var CATALOG = [];          // [{name, no, desc, ...}]，按编号排好序
  var TEXT = {};             // {name: {flavor, story}}
  // 【v0.76】编辑文案 —— 只有私有仓库的图鉴服务才有后端（codex_edit.py）。
  //   玩家手里的包没有那个文件 → /api/codex-edit/state 直接 404 → EDITABLE 保持 false，
  //   页面连"编辑"两个字都不会出现。
  var EDITABLE = false;
  var EDITING = null;        // 正在编辑哪一株（null = 没在编辑）
  var CHANGED = {};          // 被改过的株 → 列表上打个小点
  var CUR = null;            // 当前选中的植物名
  var Q = '';

  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* 占位美术：与战斗页 app.js 里的 hueOf / artStyle 同一套算法，
     保证同一株植物在图鉴和对局里颜色一致。 */
  function hueOf(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }
  function artStyle(name) { return 'background: hsl(' + hueOf(name) + ', 42%, 62%)'; }

  /* 编号排序：'01'→1、'45'→45、'151'→151；没编号的（辣椒）排最后 */
  function noKey(p) {
    var n = parseInt(p.no, 10);
    return isNaN(n) ? 99999 : n;
  }

  /* ---------- 载入 ---------- */
  function load() {
    var done = 0, failed = '';
    function finish() {
      // 【v0.76】从 2 个请求变成 3 个：多了「能不能编辑」那一查。
      //   它不参与失败判定（查不到就是只读，不影响看图鉴），但要等它回来
      //   再渲染列表 —— 否则"改过"的小圆点会晚一步才出现。
      if (++done < 3) return;
      if (failed) {
        $('codex-detail').innerHTML = '<div class="cx-empty">读不到图鉴数据：' + esc(failed) + '</div>';
        $('codex-count').textContent = '载入失败';
        return;
      }
      CATALOG.sort(function (a, b) { return noKey(a) - noKey(b); });
      $('codex-count').textContent = CATALOG.length + ' 株';
      renderList();
      // 支持直接分享 #水蜜桃
      var h = '';
      try { h = decodeURIComponent((location.hash || '').replace(/^#/, '')); } catch (_) { h = ''; }
      select((h && find(h)) ? h : (CATALOG[0] && CATALOG[0].name), false);
    }

    fetch('/api/catalog').then(function (r) { return r.json(); }).then(function (d) {
      // ★ /api/catalog 外面还包了一层：{plants: [...], rule_options: [...]}
      //   （rule_options 是给试玩台画规则勾选框的，图鉴用不到）
      CATALOG = Array.isArray(d) ? d : ((d && d.plants) || []);
      finish();
    }).catch(function (e) {
      failed = 'catalog：' + e; finish();
    });

    fetch('/api/codex-text').then(function (r) { return r.json(); }).then(function (d) {
      TEXT = (d && typeof d === 'object') ? d : {};
      finish();
    }).catch(function (e) {
      failed = 'codex-text：' + e; finish();
    });

    // 【v0.76】编辑能力（只有私有仓库里才有这个后端）
    fetch('/api/codex-edit/state').then(function (r) {
      // 玩家手里的包没有编辑后端 → 这个路由直接 404，response.ok 是 false
      if (!r.ok) throw new Error('readonly');
      return r.json();
    }).then(function (j) {
      EDITABLE = !!(j && j.editable);
      CHANGED = {};
      (j && j.changed || []).forEach(function (k) { CHANGED[k] = 1; });
      finish();
    }).catch(function () {
      EDITABLE = false;
      finish();
    });
  }

  function find(name) {
    for (var i = 0; i < CATALOG.length; i++) if (CATALOG[i].name === name) return CATALOG[i];
    return null;
  }

  /* ---------- 搜索 ---------- */
  /* 数字查编号：完整编号（45 → 水蜜桃）或耗种段（4 → 41/45 全出来）；
     文字查名称 + 机制 + 文案，这样一个「噬魂」能搜到全部噬魂类，
     搜「记性」也能从风味文案里找回来。 */
  function match(p, q) {
    if (/^\d+$/.test(q)) return p.no === q || p.no_prefix === q;
    var t = TEXT[p.name] || {};
    var hay = (p.name || '') + ' ' + (p.desc || '') + ' ' +
              (t.flavor || '') + ' ' + (t.story || '');
    return hay.indexOf(q) >= 0;
  }

  function visible() {
    return Q ? CATALOG.filter(function (p) { return match(p, Q); }) : CATALOG;
  }

  /* ---------- 列表 ---------- */
  function renderList() {
    var list = visible();
    var box = $('codex-list');
    if (!list.length) {
      box.innerHTML = '<div class="cx-empty">没有匹配「' + esc(Q) + '」的植物</div>';
      return;
    }
    var h = '';
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      h += '<button class="cx-item' + (p.name === CUR ? ' on' : '') + '" data-v="' + esc(p.name) + '">' +
             '<span class="cx-no">' + esc(p.no || '??') + '</span>' +
             '<span class="cx-dot" style="' + artStyle(p.name) + '">' + esc(p.name.charAt(0)) + '</span>' +
             '<span class="cx-nm">' + esc(p.name) + '</span>' +
           '</button>';
    }
    box.innerHTML = h;
    $('codex-hint').textContent = Q ? (list.length + ' / ' + CATALOG.length) : '';
  }

  /* ---------- 详情 ---------- */
  function chips(p) {
    var out = ['<span class="cx-chip">耗种 <b>' + esc(p.cost_seed) + '</b></span>'];
    if (p.cost_hp) out.push('<span class="cx-chip">耗命 <b>' + (p.cost_hp / 2) + ' 条</b></span>');
    if (p.attr) {
      var atk = p.attr;
      if (p.bao) atk += '·暴' + p.bao;
      out.push('<span class="cx-chip">攻击 <b>' + esc(atk) + '</b></span>');
      if (p.lian > 1) out.push('<span class="cx-chip">连击 <b>' + p.lian + '</b></span>');
      if (p.san > 1) out.push('<span class="cx-chip">散连击 <b>' + p.san + '</b></span>');
    } else {
      out.push('<span class="cx-chip">攻击 <b>不攻击</b></span>');
    }
    out.push('<span class="cx-chip">防御 <b>' + esc(p.fang) + '</b></span>');
    return out.join('');
  }

  function renderDetail(p) {
    var t = TEXT[p.name] || {};
    var h = '';

    h += '<div class="cx-head">' +
           '<div class="cx-art" style="' + artStyle(p.name) + '">' + esc(p.name.charAt(0)) + '</div>' +
           '<div>' +
             '<div class="cx-title">' + esc(p.name) + '</div>' +
             '<div class="cx-sub">编号 ' + esc(p.no || '待确认') + '</div>' +
           '</div>' +
         '</div>';
    h += '<div class="cx-chips">' + chips(p) + '</div>';

    // 【v0.76】编辑态：两段原文换成输入框
    if (EDITABLE && EDITING === p.name) {
      h += '<div class="cx-edit">' +
             '<div class="cx-edit-h">改这一株的文案 ' +
               '<span class="cx-edit-tip">（保存会写进 图鉴文案.json，不动 codex_text.py）</span>' +
             '</div>' +
             '<label class="cx-edit-l">风味（一句吐槽）' +
               '<textarea id="cx-e-flavor" rows="2" spellcheck="false">' +
                 esc(t.flavor || '') + '</textarea></label>' +
             '<label class="cx-edit-l">档案（背景 / 彩蛋）' +
               '<textarea id="cx-e-story" rows="6" spellcheck="false">' +
                 esc(t.story || '') + '</textarea></label>' +
             '<div class="cx-edit-btns">' +
               '<button class="btn primary" id="cx-save">保存</button>' +
               '<button class="btn" id="cx-reset">撤回（回到默认）</button>' +
               '<button class="btn" id="cx-cancel">取消</button>' +
               '<span class="cx-edit-msg" id="cx-msg"></span>' +
             '</div>' +
           '</div>';
      $('codex-detail').innerHTML = h;
      bindEditor(p);
      return;
    }

    if (t.flavor) {
      h += '<div class="cx-flavor">「' + esc(t.flavor) + '」</div>';
    } else {
      h += '<div class="cx-flavor cx-missing">（这一株还没写风味文案）</div>';
    }

    h += '<div class="cx-sec">' +
           '<div class="cx-sec-h">机制</div>' +
           '<div class="cx-desc">' + (p.desc ? esc(p.desc) : '—') + '</div>' +
         '</div>';

    if (t.story) {
      h += '<div class="cx-sec">' +
             '<div class="cx-sec-h">档案</div>' +
             '<div class="cx-story">' + esc(t.story) + '</div>' +
           '</div>';
    }

    if (EDITABLE) {
      var tag = CHANGED[p.name] ? ' <span class="cx-dot" title="改过"></span>' : '';
      h += '<div class="cx-edit-bar">' +
             '<button class="btn" id="cx-edit-on">✎ 改这两段</button>' + tag +
           '</div>';
    }

    $('codex-detail').innerHTML = h;
    if (EDITABLE && $('cx-edit-on')) {
      $('cx-edit-on').onclick = function () { EDITING = p.name; renderDetail(p); };
    }
  }

  /* ---------- 【v0.76】编辑：保存 / 撤回 / 取消 ---------- */
  function apiPost(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function bindEditor(p) {
    var msg = $('cx-msg');
    function show(s, bad) { if (msg) { msg.textContent = s; msg.className = 'cx-edit-msg' + (bad ? ' bad' : ''); } }
    function lock(b) {
      ['cx-save', 'cx-reset', 'cx-cancel'].forEach(function (id) {
        var el = $(id); if (el) el.disabled = b;
      });
    }

    $('cx-save').onclick = function () {
      lock(true); show('保存中……');
      apiPost('/api/codex-edit/save', {
        key: p.name,
        flavor: $('cx-e-flavor').value,
        story: $('cx-e-story').value
      }).then(function (j) {
        if (!j.ok) { show(j.err || '保存失败', true); lock(false); return; }
        TEXT = j.text || TEXT;
        CHANGED = {};
        ((j.state && j.state.changed) || []).forEach(function (k) { CHANGED[k] = 1; });
        EDITING = null;
        renderList();            // 列表上的小点跟着更新
        renderDetail(p);
        var m = $('cx-msg');     // 渲染完是新节点，提示要重新挂
        if (m) { m.textContent = '已保存'; m.className = 'cx-edit-msg'; }
      }).catch(function (e) { show('保存失败：' + e, true); lock(false); });
    };

    $('cx-reset').onclick = function () {
      lock(true); show('撤回中……');
      apiPost('/api/codex-edit/reset', { key: p.name }).then(function (j) {
        if (!j.ok) { show(j.err || '撤回失败', true); lock(false); return; }
        TEXT = j.text || TEXT;
        CHANGED = {};
        ((j.state && j.state.changed) || []).forEach(function (k) { CHANGED[k] = 1; });
        EDITING = null;
        renderList();
        renderDetail(p);
        var m = $('cx-msg');
        if (m) { m.textContent = '已回到默认文案'; m.className = 'cx-edit-msg'; }
      }).catch(function (e) { show('撤回失败：' + e, true); lock(false); });
    };

    $('cx-cancel').onclick = function () { EDITING = null; renderDetail(p); };

    // Ctrl+Enter 保存（写文案时手不用离开键盘）
    ['cx-e-flavor', 'cx-e-story'].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.onkeydown = function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') { ev.preventDefault(); $('cx-save').click(); }
      };
    });
  }

  /* ---------- 选中 ---------- */
  function select(name, pushHash) {
    var p = find(name);
    if (!p) return;
    CUR = name;
    renderDetail(p);

    // 列表高亮跟着走（搜索过滤后当前项可能不在列表里，所以按 data-v 找）
    var items = $('codex-list').querySelectorAll('.cx-item');
    for (var i = 0; i < items.length; i++) {
      var on = items[i].getAttribute('data-v') === name;
      items[i].classList.toggle('on', on);
      if (on && items[i].scrollIntoView) {
        try { items[i].scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (_) {}
      }
    }

    if (pushHash !== false) {
      try { history.replaceState(null, '', '#' + encodeURIComponent(name)); } catch (_) {}
    }
  }

  /* ---------- 事件 ---------- */
  $('codex-list').addEventListener('click', function (e) {
    var el = e.target.closest('.cx-item');
    if (el) select(el.getAttribute('data-v'));
  });

  var timer = null;
  $('codex-q').addEventListener('input', function () {    var v = this.value.trim();
    // 打字时不必每个字符都重排 38 项，但这点量其实无所谓 —— 留个 60ms 只是为了让
    // 中文输入法的组字过程别把列表抖来抖去。
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      Q = v;
      renderList();
      var list = visible();
      if (list.length && !list.some(function (p) { return p.name === CUR; })) {
        select(list[0].name);
      }
    }, 60);
  });

  // 上下键换植物（桌面顺手；手机上不显示键盘，无副作用）
  document.addEventListener('keydown', function (e) {
    if (e.target && e.target.id === 'codex-q') {
      if (e.key === 'Escape') { $('codex-q').value = ''; Q = ''; renderList(); return; }
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    var list = visible();
    if (!list.length) return;
    var i = 0;
    for (var k = 0; k < list.length; k++) if (list[k].name === CUR) { i = k; break; }
    i += (e.key === 'ArrowDown' ? 1 : -1);
    if (i < 0) i = 0;
    if (i >= list.length) i = list.length - 1;
    select(list[i].name);
    e.preventDefault();
  });

  window.addEventListener('hashchange', function () {
    var h = '';
    try { h = decodeURIComponent((location.hash || '').replace(/^#/, '')); } catch (_) { h = ''; }
    if (h && find(h)) select(h, false);
  });

  /* 手机屏窄，那句长 placeholder 会被截成半句 —— 窄屏换成短的。
     ★ 用 matchMedia 而不是改 CSS：placeholder 是文本内容，CSS 改不了。 */
  if (window.matchMedia && window.matchMedia('(max-width: 700px)').matches) {
    $('codex-q').placeholder = '搜编号或名称';
  }

  load();
})();
