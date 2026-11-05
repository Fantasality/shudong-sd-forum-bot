window.__errors = [];
window.onerror = function (m, src, line) { window.__errors.push(m + ' @' + line); };
/* 启动与外壳 */
(function () {
  'use strict';
  const A = window.App;
  const { S, esc, avatar, toast, api } = A;

  function updateTop() {
    const acc = S.accounts[S.active];
    const el = document.getElementById('top-sub');
    if (!el) return;
    el.textContent = acc ? '小号 · ' + (acc.nickname || acc.username) : '未登录';
  }

  function gate() {
    return '<div class="gate">' +
      '<div class="logo-big"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M6 20V11a6 6 0 0112 0v9"/><ellipse cx="12" cy="12" rx="3.1" ry="1.7" fill="#fff" stroke="none"/>' +
        '<circle cx="12" cy="12" r=".95" fill="#0b1220" stroke="none"/></svg></div>' +
      '<h2>鼠洞透视</h2><p>内置小号没登上，手动补一个账号</p>' +
      '<div class="sfield"><label>用户名</label><input id="g-u" placeholder="登录名" autocomplete="off"></div>' +
      '<div class="sfield"><label>密码</label><input id="g-p" type="password" placeholder="密码"></div>' +
      '<button class="btn block" id="g-go" style="height:44px;margin-top:6px">登录</button>' +
      '<div class="tiny muted" style="text-align:center;margin-top:14px;line-height:1.7">登录后所有请求都用这个号的 token<br>想管多个号，去「我的」里加</div>' +
    '</div>';
  }

  Views.gate = gate;

  Views.gateAfter = function () {
    const btn = document.getElementById('g-go');
    if (!btn) return;
    btn.onclick = async () => {
      const u = document.getElementById('g-u').value.trim();
      const p = document.getElementById('g-p').value;
      if (!u || !p) { toast('填完整'); return; }
      btn.textContent = '登录中…';
      try {
        const acc = await A.login(u, p, '小号');
        S.accounts[acc.key] = acc;
        A.setActive(acc.key);
        S.viewer = null;
        try {
          const v = await api('query{viewer{id user{id username profile{nickname institute}} viewerVerify{school{id}}}}');
          S.viewer = v.viewer;
        } catch (e) { /* ignore */ }
        A.render();
      } catch (e) {
        btn.textContent = '登录';
        toast(e.message);
      }
    };
  };

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#tabbar .tab').forEach(t => {
      t.onclick = () => {
        S.tab = t.dataset.tab;
        S.view = { name: 'tab' };
        A.render();
      };
    });
    document.getElementById('top-refresh').onclick = async () => {
      toast('刷新中…');
      if (S.tab === 'feed' && A.renderFeedList) A.renderFeedList();
      else if (S.tab === 'xray' && A.xrayReload) A.xrayReload();
      else if (S.tab === 'cmd' && A.cmdRefreshAcks) A.cmdRefreshAcks();
      else A.render();
    };
    const mask = document.getElementById('sheet-mask');
    mask.addEventListener('click', (e) => { if (e.target === mask) mask.classList.remove('on'); });

    A.boot().then(updateTop);
    setInterval(updateTop, 1200);
  });
})();

/* 临时诊断：?diag=1 时把超宽元素写进 title */
if (location.search.includes('diag')) {
  setTimeout(() => {
    const vw = document.documentElement.clientWidth;
    const wide = [];
    document.querySelectorAll('*').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 || r.width > vw + 1) {
        wide.push(el.tagName + '.' + (el.className || '') + ':' + Math.round(r.width) + '/' + Math.round(r.right));
      }
    });
    document.title = 'VW=' + vw + ' SW=' + document.documentElement.scrollWidth + ' | ' + wide.slice(0, 12).join(' ; ');
  }, 2500);
}
