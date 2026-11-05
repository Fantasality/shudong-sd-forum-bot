/* 鼠洞透视 — 核心：桥接、状态、工具、路由 */
(function () {
  'use strict';

  const HAS_NATIVE = typeof window.SZ !== 'undefined' && window.SZ && window.SZ.boot;
  const pending = new Map();
  let seq = 0;

  window.__szResolve = function (rid, payload) {
    const fn = pending.get(rid);
    if (!fn) return;
    pending.delete(rid);
    let obj;
    try { obj = JSON.parse(payload); } catch (e) { obj = { ok: false, error: '解析失败' }; }
    fn(obj);
  };

  const DEV = {
    key: 'sz_dev_',
    get(k, d) { const v = localStorage.getItem(this.key + k); return v == null ? d : v; },
    set(k, v) { localStorage.setItem(this.key + k, v); },
    boot() {
      return {
        accounts: JSON.parse(this.get('accounts', '{}')),
        cfg: JSON.parse(this.get('cfg', '{}')),
        roster: JSON.parse(this.get('roster', '{}')),
        version: 'dev'
      };
    },
    asset(name) {
      return new Promise((res) => {
        fetch(name).then(r => r.text()).then(res).catch(() => res(''));
      });
    },
    async gql(acct, query, vars) {
      const acc = JSON.parse(this.get('accounts', '{}'))[acct] || {};
      const r = await fetch('/graphql', {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: 'Bearer ' + (acc.access || '') },
        body: JSON.stringify({ query, variables: vars || {} })
      });
      return JSON.parse(await r.text());
    },
    async login(u, p, label) {
      const r = await fetch('/graphql', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ query: 'mutation L($u:String!,$p:String!){login(input:{basicInput:{username:$u,password:$p}}){accessToken refreshToken}}', variables: { u, p } })
      });
      const j = JSON.parse(await r.text());
      if (!j.data) throw new Error((j.errors && j.errors[0].message) || '登录失败');
      const all = JSON.parse(this.get('accounts', '{}'));
      const key = 'u_' + u;
      const acc = { key, username: u, password: p, label: label || u, access: j.data.login.accessToken, refresh: j.data.login.refreshToken, userId: '' };
      const me = await this.gql(key, 'query{viewer{user{id username profile{nickname institute}}}}', {});
      const user = me.data && me.data.viewer && me.data.viewer.user;
      if (user) { acc.userId = user.id; acc.username = user.username || u; acc.nickname = (user.profile && user.profile.nickname) || ''; }
      all[key] = acc; this.set('accounts', JSON.stringify(all));
      return acc;
    }
  };

  function nativeCall(fn, ...args) {
    return new Promise((resolve) => {
      const rid = 'q' + (++seq);
      pending.set(rid, resolve);
      window.SZ[fn](rid, ...args);
    });
  }

  const S = {
    ready: false,
    accounts: {},
    active: '',
    roster: [],
    cfg: { dropPostId: '', dropPostCode: '', masterKey: '' },
    viewer: null,
    schoolId: 'U2Nob29sOjIy',
    topics: [],
    view: { name: 'tab' },
    tab: 'feed',
    logs: [],
    dropPost: null,
    anchors: {},
    counts: {}
  };

  const I = {
    feed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
    xray: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/><path d="M11 8v6M8 11h6"/></svg>',
    cmd: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 9l3 3-3 3M13 15h4"/></svg>',
    me: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20c1.2-3.6 4-5.4 7.5-5.4S18.3 16.4 19.5 20"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4-4"/></svg>',
    back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>',
    heart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.6-7-9.4A3.9 3.9 0 0112 8a3.9 3.9 0 017 2.6C19 15.4 12 20 12 20z"/></svg>',
    heartOn: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 20s-7-4.6-7-9.4A3.9 3.9 0 0112 8a3.9 3.9 0 017 2.6C19 15.4 12 20 12 20z"/></svg>',
    comment: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a7.5 7.5 0 01-7.5 7.5c-1.2 0-2.4-.2-3.4-.7L4 20l1.2-4.1A7.4 7.4 0 014 12a7.5 7.5 0 017.5-7.5A7.5 7.5 0 0120 12z"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 10-2.3 5.6"/><path d="M20 5v6h-6"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6a2 2 0 012-2h8"/></svg>',
    logo: '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M6 20V11a6 6 0 0112 0v9"/><ellipse cx="12" cy="12" rx="3.1" ry="1.7" fill="#fff" stroke="none"/><circle cx="12" cy="12" r=".95" fill="#0b1220" stroke="none"/></svg>'
  };

  /* ---------- 工具 ---------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function b64decode(s) {
    try {
      let t = String(s || '').replace(/-/g, '+').replace(/_/g, '/');
      while (t.length % 4) t += '=';
      return decodeURIComponent(escape(atob(t)));
    } catch (e) { return ''; }
  }
  function b64encode(s) {
    try { return btoa(unescape(encodeURIComponent(s))).replace(/=+$/, ''); } catch (e) { return ''; }
  }

  function rawId(gid) {
    const t = b64decode(gid || '');
    const i = t.lastIndexOf(':');
    return i >= 0 ? t.slice(i + 1) : '';
  }

  // 帖代号：#3F9K  （base36 数字 id，可逆）
  function codeOf(gid) {
    const n = rawId(gid);
    if (!n || isNaN(+n)) return '';
    return (+n).toString(36).toUpperCase();
  }
  function idOfCode(code, type) {
    const clean = String(code || '').trim().replace(/^#/, '').toUpperCase();
    if (!/^[0-9A-Z]{1,10}$/.test(clean)) return '';
    const n = parseInt(clean, 36);
    if (!n || isNaN(n)) return '';
    return b64encode((type || 'Post') + ':' + n);
  }

  function nickOf(u) {
    if (!u) return '匿名用户';
    const p = u.profile || {};
    return p.nickname || u.username || '匿名用户';
  }

  function findBot(userId) {
    if (!userId) return null;
    return S.roster.find(b => b.userId === userId) || null;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 172800) return '昨天 ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    return (d.getMonth() + 1) + '-' + d.getDate() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function avatar(name) {
    return (name || '?').trim().slice(0, 1);
  }

  /* ---------- 存储 ---------- */
  const store = {
    save(key, val) {
      if (HAS_NATIVE) window.SZ.save(key, JSON.stringify(val));
      else DEV.set(key, JSON.stringify(val));
    },
    load(key, def) {
      try {
        if (HAS_NATIVE) {
          const raw = window.SZ.boot();
          const o = JSON.parse(raw || '{}');
          return o[key] && Object.keys(o[key]).length ? o[key] : def;
        }
        return JSON.parse(DEV.get(key, JSON.stringify(def)));
      } catch (e) { return def; }
    }
  };

  /* ---------- 网络 ---------- */
  async function api(query, vars) {
    const key = S.active;
    if (!key) throw new Error('未登录');
    let r;
    if (HAS_NATIVE) {
      r = await nativeCall('gql', key, query, JSON.stringify(vars || {}));
    } else {
      try {
        const body = await DEV.gql(key, query, vars);
        r = { ok: true, body };
      } catch (e) { r = { ok: false, error: e.message }; }
    }
    if (!r.ok) throw new Error(r.error || '请求失败');
    const b = r.body || {};
    if (b.errors && b.errors.length) throw new Error(b.errors[0].message || '接口报错');
    return b.data || {};
  }

  async function login(username, password, label) {
    if (HAS_NATIVE) {
      const r = await nativeCall('login', username, password, label || '');
      if (!r.ok) throw new Error(r.error || '登录失败');
      return r.account;
    }
    return DEV.login(username, password, label);
  }

  function setActive(key) {
    S.active = key;
    S.cfg.active = key;
    store.save('cfg', S.cfg);
  }

  /* ---------- 提示 ---------- */
  let toastTimer;
  function toast(msg) {
    let el = document.querySelector('.toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    requestAnimationFrame(() => el.classList.add('on'));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), 1900);
  }

  function sheet(opts) {
    const mask = document.getElementById('sheet-mask');
    const box = document.getElementById('sheet');
    box.innerHTML = '<div class="grab"></div><div class="sheet-title">' + esc(opts.title || '') +
      '<button class="x" data-close>×</button></div><div class="sheet-body">' + (opts.body || '') + '</div>' +
      (opts.footer === false ? '' : '<div class="foot">' + (opts.footer || '<button class="btn ghost" data-close>关闭</button>') + '</div>');
    mask.classList.add('on');
    mask.querySelectorAll('[data-close]').forEach(b => b.onclick = close);
    function close() { mask.classList.remove('on'); }
    if (opts.onMount) opts.onMount(box, close);
    return { close, box };
  }

  function confirmBox(title, text, okText) {
    return new Promise((resolve) => {
      const s = sheet({
        title,
        body: '<p style="font-size:13px;color:#475569;line-height:1.7;margin:4px 0 6px">' + esc(text) + '</p>',
        footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="cf-ok">' + esc(okText || '确定') + '</button>',
        onMount(box, close) {
          box.querySelector('#cf-ok').onclick = () => { close(); resolve(true); };
          box.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => resolve(false)));
        }
      });
      void s;
    });
  }

  /* ---------- 路由 ---------- */
  function go(name, params) {
    S.view = Object.assign({ name }, params || {});
    render();
    const sc = document.getElementById('screen');
    if (sc) sc.scrollTop = 0;
  }
  function back() {
    if (S.view.name !== 'tab') {
      S.view = { name: 'tab' };
      render();
      return true;
    }
    return false;
  }
  window.__szBack = function () { return back(); };

  /* ---------- 渲染入口 ---------- */
  function render() {
    const tabbar = document.getElementById('tabbar');
    if (!S.active) {
      tabbar.classList.add('hidden');
      document.getElementById('screen').innerHTML = Views.gate ? Views.gate() : '';
      if (Views.gateAfter) Views.gateAfter();
      return;
    }
    const detail = S.view.name !== 'tab';
    tabbar.classList.toggle('hidden', detail);
    document.getElementById('screen').innerHTML =
      detail ? (Views.detail ? Views.detail(S.view) : '') : Views.tab(S.tab);
    if (detail && Views.afterDetail) Views.afterDetail(S.view);
    if (!detail && Views.afterTab) Views.afterTab(S.tab);
    document.querySelectorAll('#tabbar .tab').forEach(t => {
      t.classList.toggle('on', !detail && t.dataset.tab === S.tab);
    });
  }

  /* ---------- 启动 ---------- */
  async function boot() {
    const info = HAS_NATIVE ? JSON.parse(window.SZ.boot() || '{}') : DEV.boot();
    S.accounts = info.accounts || {};
    S.cfg = Object.assign({ dropPostId: '', dropPostCode: '', masterKey: '', active: '' }, info.cfg || {});
    S.logs = S.cfg.logs || [];

    let roster = [];
    try {
      const raw = HAS_NATIVE ? window.SZ.asset('roster.json') : await DEV.asset('roster.json');
      roster = raw ? JSON.parse(raw) : [];
    } catch (e) { roster = []; }
    const custom = (info.roster && info.roster.list) || null;
    S.roster = custom && custom.length ? custom : roster;

    S.active = S.cfg.active || Object.keys(S.accounts)[0] || '';

    if (!S.active) {
      // 内置小号：首次启动自动登录
      try {
        const raw = HAS_NATIVE ? window.SZ.asset('builtin.json') : await DEV.asset('builtin.json');
        const b = raw ? JSON.parse(raw) : null;
        if (b && b.username) {
          const acc = await login(b.username, b.password, b.label);
          S.accounts[acc.key] = Object.assign({}, S.accounts[acc.key], acc);
          S.active = acc.key;
          S.cfg.active = acc.key;
          store.save('cfg', S.cfg);
        }
      } catch (e) {
        console.warn('内置小号登录失败', e);
      }
    }

    if (S.active) {
      try {
        const v = await api('query{viewer{id user{id username profile{nickname institute}} viewerVerify{school{id name}}}}');
        S.viewer = v.viewer;
        if (v.viewer && v.viewer.viewerVerify && v.viewer.viewerVerify.school) {
          S.schoolId = v.viewer.viewerVerify.school.id;
        }
      } catch (e) { console.warn(e); }
    }

    S.ready = true;
    render();
  }

  window.App = {
    S, I, esc, codeOf, idOfCode, rawId, nickOf, findBot, fmtTime, avatar,
    api, login, setActive, toast, sheet, confirmBox, go, back, render, store, boot,
    HAS_NATIVE, saveCfg() { store.save('cfg', S.cfg); }
  };
})();
