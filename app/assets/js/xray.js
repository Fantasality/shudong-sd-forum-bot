/* 透视：机器人花名册 / 就位状态 / 单号档案 */
(function () {
  'use strict';
  const A = window.App;
  const { S, esc, codeOf, idOfCode, nickOf, findBot, fmtTime, avatar, api, toast, go, I } = A;

  const X = { loaded: false, loading: false, filter: 'all', info: {}, anchors: {}, dropTitle: '' };
  A.Xray = X;

  function isToday(iso) {
    if (!iso) return false;
    return Date.now() - new Date(iso).getTime() < 86400000;
  }

  function statusOf(bot) {
    const anchor = X.anchors[bot.userId];
    const info = X.info[bot.key] || {};
    const active = isToday(info.lastPostAt);
    return {
      online: !!anchor,
      active,
      label: anchor ? '就位' : '未就位',
      last: info.lastPostAt ? fmtTime(info.lastPostAt) : ''
    };
  }

  function cardHtml(bot) {
    const st = statusOf(bot);
    const filtered = X.filter === 'active' ? st.active : X.filter === 'ready' ? st.online
      : X.filter === 'missing' ? !st.online : true;
    if (!filtered) return '';
    return '<div class="botcard" data-bot="' + esc(bot.key) + '">' +
      '<div class="ava bot">' + esc(avatar(bot.name)) + '</div>' +
      '<div class="bd">' +
        '<div class="nm">' + esc(bot.name) + '<span class="code-chip">' + esc(bot.code) + '</span></div>' +
        '<div class="cl">' + esc(bot.college || bot.label) + '</div>' +
        '<div class="cl tiny ' + (st.online ? 'stok' : 'stoff') + '">' + st.label +
          ' · ' + (st.last ? st.last + '发帖' : '暂无动态') + '</div>' +
      '</div>' +
    '</div>';
  }

  Views.xray = function () {
    const total = S.roster.length;
    const ready = S.roster.filter(b => X.anchors[b.userId]).length;
    const active = S.roster.filter(b => isToday((X.info[b.key] || {}).lastPostAt)).length;
    const missing = total - ready;

    let grid = S.roster.map(cardHtml).join('');
    if (!grid) grid = '<div class="empty" style="grid-column:1/-1"><span class="big">👁️</span>这个分组下没有号</div>';

    return '<div class="pane">' +
      '<div class="xray-hero">' +
        '<div class="k">Roster Vision · 花名册透视</div>' +
        '<div class="nums">' +
          '<div><div class="n">' + total + '</div><div class="lbl">在册机器人</div></div>' +
          '<div><div class="n ' + (active ? '' : 'warn') + '">' + active + '</div><div class="lbl">今日有动作</div></div>' +
          '<div><div class="n ' + (missing ? 'bad' : '') + '">' + ready + '</div><div class="lbl">暗桩已就位</div></div>' +
        '</div>' +
        '<div class="hint">' + (S.cfg.dropPostId
          ? '暗桩帖 #' + esc(S.cfg.dropPostCode || codeOf(S.cfg.dropPostId)) + ' · 就位 ' + ready + '/' + total
          : '尚未设置暗桩帖，去「指挥台」设一个，机器人才有信箱') + '</div>' +
      '</div>' +
      '<div class="chips" id="xray-filter">' +
        chip('all', '全部') + chip('active', '今日活跃') + chip('ready', '已就位') + chip('missing', '未就位') +
      '</div>' +
      '<div class="botgrid" id="botgrid" style="margin-top:10px">' + grid + '</div>' +
      '<div class="section-title">透视说明<span class="line"></span></div>' +
      '<div class="card" style="padding:12px 14px;font-size:12px;color:#475569;line-height:1.75">' +
        '· 角标 <b style="color:#b45309">BOT B01</b> 只在你的花名册内标注，普通用户看不到任何标记。<br>' +
        '· 点开任意号可看它的公开动态与粉丝视角。<br>' +
        '· 「就位」= 该号已在暗桩帖下留过言，等待你的悄悄话。' +
      '</div>' +
    '</div>';

    function chip(k, label) {
      return '<button class="chip' + (X.filter === k ? ' on' : '') + '" data-f="' + k + '">' + label + '</button>';
    }
  };

  Views.xrayAfter = async function () {
    document.querySelectorAll('#xray-filter [data-f]').forEach(b => {
      b.onclick = () => { X.filter = b.dataset.f; A.render(); };
    });
    document.querySelectorAll('#botgrid [data-bot]').forEach(el => {
      el.onclick = () => go('bot', { key: el.dataset.bot });
    });
    if (!X.loaded && !X.loading) await loadAll();
  };

  async function loadAll() {
    if (X.loading) return;
    X.loading = true;
    A.render();
    try {
      await Promise.all([loadAnchor(), loadInfo()]);
      X.loaded = true;
    } catch (e) {
      toast('透视失败：' + e.message);
    } finally {
      X.loading = false;
      A.render();
    }
  }
  A.xrayReload = loadAll;

  // 暗桩帖下的就位锚点
  async function loadAnchor() {
    X.anchors = {};
    X.dropTitle = '';
    if (!S.cfg.dropPostId) return;
    try {
      const d = await api(`query($p:ID!){post(id:$p){id content}
        comments(postId:$p,first:50,order:DEFAULT){edges{node{id createdAt creator{id username profile{nickname}}}}}}`,
        { p: S.cfg.dropPostId });
      X.dropTitle = (d.post && d.post.content || '').slice(0, 40);
      (d.comments.edges || []).forEach(e => {
        const n = e.node;
        const bot = findBot(n.creator && n.creator.id);
        if (bot && !X.anchors[bot.userId]) {
          X.anchors[bot.userId] = { commentId: n.id, at: n.createdAt };
        }
      });
    } catch (e) { console.warn('anchor', e); }
  }

  // 32 个别名一次拿完最近发帖时间
  async function loadInfo() {
    if (!S.roster.length) return;
    const CH = 6;                       // 一批 6 个号，避开接口频率限制
    for (let i = 0; i < S.roster.length; i += CH) {
      const slice = S.roster.slice(i, i + CH);
      const parts = slice.map((b, j) =>
        'b' + j + ': user(userId:"' + b.userId + '"){ id userPosts(first:1,type:CREATED){edges{node{createdAt}}} }');
      try {
        const d = await api('query{' + parts.join(' ') + '}');
        slice.forEach((b, j) => {
          const node = d['b' + j];
          const edge = node && node.userPosts && node.userPosts.edges && node.userPosts.edges[0];
          X.info[b.key] = { lastPostAt: edge ? edge.node.createdAt : '' };
        });
      } catch (e) {
        slice.forEach(b => { if (!X.info[b.key]) X.info[b.key] = { lastPostAt: '', failed: true }; });
      }
      await new Promise(r => setTimeout(r, 850));
      A.render();
    }
  }

  /* ---------- 单号档案 ---------- */
  const BotView = { bot: null, tab: 'posts', cache: {} };

  const QT = {
    posts: `query($id:ID!,$a:String){ user(userId:$id){ userPosts(first:10,type:CREATED,after:$a){
      totalCount pageInfo{hasNextPage endCursor}
      edges{node{id content createdAt likeCount commentCount topic{name}}} } } }`,
    comments: `query($id:ID!,$a:String){ user(userId:$id){ userComments(first:12,type:CREATED,after:$a){
      totalCount pageInfo{hasNextPage endCursor}
      edges{node{id content createdAt post{id content}}} } } }`,
    subs: `query($id:ID!,$a:String){ user(userId:$id){ userSubcomments(first:12,type:CREATED,after:$a){
      totalCount pageInfo{hasNextPage endCursor}
      edges{node{id content createdAt comment{id post{id content}}}} } } }`
  };

  const EMPTY_TEXT = {
    posts: '还没发过帖',
    comments: '还没评论过',
    subs: '还没在楼中楼里说过话'
  };

  function state(tab) {
    BotView.cache[tab] = BotView.cache[tab] || { items: [], cursor: null, done: false, total: 0, loading: false };
    return BotView.cache[tab];
  }

  Views.botShell = function (v) {
    const bot = S.roster.find(b => b.key === v.key) || {};
    return '<div class="pane detail-body">' +
      '<div class="detail-head"><button class="back" id="d-back">' + I.back + '</button>' +
        '<div class="title">' + esc(bot.name || '机器人') + '</div><div class="grow"></div>' +
        '<span class="code-chip lg">' + esc(bot.code || '') + '</span></div>' +
      '<div id="detail-body"><div class="loading-row"><span class="spinner dark"></span> 读取档案</div></div>' +
    '</div>';
  };

  Views.openBot = async function (key) {
    const bot = S.roster.find(b => b.key === key);
    if (!bot) return;
    BotView.bot = bot;
    BotView.tab = 'posts';
    BotView.cache = {};
    document.getElementById('d-back').onclick = () => A.back();
    await loadProfile();
  };

  async function loadProfile() {
    const bot = BotView.bot;
    let u = {};
    try {
      const d = await api(`query($id:ID!){ user(userId:$id){ id username createdAt profile{nickname institute grade} } }`,
        { id: bot.userId });
      u = d.user || {};
    } catch (e) { /* 读不到也不影响活动列表 */ }

    const st = statusOf(bot);
    document.getElementById('detail-body').innerHTML =
      '<div class="card" style="padding:14px">' +
        '<div class="row" style="gap:12px">' +
          '<div class="ava bot" style="width:46px;height:46px;border-radius:13px;font-size:17px">' + esc(avatar(bot.name)) + '</div>' +
          '<div class="grow"><div class="row" style="gap:6px"><b style="font-size:15px">' + esc(bot.name) + '</b>' +
            '<span class="bot-chip">BOT<span class="bcode">' + esc(bot.code) + '</span></span></div>' +
            '<div class="tiny muted">' + esc(bot.label) + '</div>' +
            '<div class="tiny muted">@' + esc(u.username || bot.username) + ' · ' + esc(st.label) + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="row" style="margin-top:12px;gap:8px">' +
          '<button class="btn sm soft grow" id="b-cmd">下指令</button>' +
          '<button class="btn sm ghost grow" id="b-copy">复制 ID</button>' +
        '</div>' +
      '</div>' +
      '<div class="section-title">参与记录<span class="line"></span>' +
        '<span class="tiny muted" id="act-total"></span></div>' +
      '<div class="seg seg-wide" id="act-seg">' +
        '<button data-tab="posts" class="on">发帖</button>' +
        '<button data-tab="comments">评论</button>' +
        '<button data-tab="subs">楼中楼</button>' +
      '</div>' +
      '<div id="act-list"></div>' +
      '<div id="act-more"></div>';

    document.getElementById('b-cmd').onclick = () => {
      S.cfg.cmdTargets = [bot.key];
      S.view = { name: 'tab' };
      S.tab = 'cmd';
      A.render();
    };
    document.getElementById('b-copy').onclick = () => A.copy(bot.userId);

    document.querySelectorAll('#act-seg [data-tab]').forEach(b => {
      b.onclick = () => {
        BotView.tab = b.dataset.tab;
        document.querySelectorAll('#act-seg [data-tab]').forEach(x => x.classList.toggle('on', x === b));
        if (state(BotView.tab).items.length) renderAct();
        else loadAct(true);
      };
    });
    await loadAct(true);
  }

  async function loadAct(reset) {
    const tab = BotView.tab;
    const st = state(tab);
    if (st.loading) return;
    if (reset) { st.items = []; st.cursor = null; st.done = false; }
    if (st.done && !reset) return;
    st.loading = true;
    const list = document.getElementById('act-list');
    if (list && !st.items.length) list.innerHTML = '<div class="loading-row"><span class="spinner dark"></span> 翻记录</div>';
    try {
      const key = tab === 'posts' ? 'userPosts' : tab === 'comments' ? 'userComments' : 'userSubcomments';
      const d = await api(QT[tab], { id: BotView.bot.userId, a: reset ? null : st.cursor });
      const conn = d.user[key];
      st.items = st.items.concat((conn.edges || []).map(e => e.node));
      st.total = conn.totalCount || 0;
      st.cursor = conn.pageInfo && conn.pageInfo.endCursor;
      st.done = !(conn.pageInfo && conn.pageInfo.hasNextPage);
      renderAct();
    } catch (e) {
      if (list) list.innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
    } finally {
      st.loading = false;
    }
  }

  function actRow(tab, n) {
    if (tab === 'posts') {
      return '<div class="act" data-post="' + esc(n.id) + '">' +
        '<div class="act-hd"><span class="code-chip">#' + esc(codeOf(n.id)) + '</span>' +
          '<span class="tiny muted">' + fmtTime(n.createdAt) + '</span>' +
          '<span class="grow"></span><span class="tiny muted">♡' + (n.likeCount || 0) + ' · 💬' + (n.commentCount || 0) + '</span></div>' +
        '<div class="act-txt">' + esc(n.content || '') + '</div>' +
        '<div class="act-sub">' + esc((n.topic && n.topic.name) || '') + '</div>' +
      '</div>';
    }
    const parent = tab === 'comments' ? (n.post || {}) : ((n.comment || {}).post || {});
    return '<div class="act" data-post="' + esc(parent.id || '') + '">' +
      '<div class="act-hd"><span class="chip-mini">' + (tab === 'comments' ? '评论' : '楼中楼') + '</span>' +
        '<span class="code-chip">#' + esc(codeOf(parent.id || '')) + '</span>' +
        '<span class="tiny muted">' + fmtTime(n.createdAt) + '</span></div>' +
      '<div class="act-txt">' + esc(n.content || '') + '</div>' +
      '<div class="act-sub">原帖：' + esc((parent.content || '').slice(0, 40)) + '</div>' +
    '</div>';
  }

  function renderAct() {
    const tab = BotView.tab;
    const st = state(tab);
    const list = document.getElementById('act-list');
    if (!list) return;
    list.innerHTML = st.items.length ? st.items.map(n => actRow(tab, n)).join('') :
      '<div class="empty" style="padding:26px">' + EMPTY_TEXT[tab] + '</div>';
    const more = document.getElementById('act-more');
    more.innerHTML = st.done ? (st.items.length ? '<div class="loading-row">到底了</div>' : '') :
      '<div class="loading-row"><button class="btn ghost sm" id="act-load">加载更多</button></div>';
    const lb = document.getElementById('act-load');
    if (lb) lb.onclick = () => loadAct(false);
    const total = document.getElementById('act-total');
    if (total) total.textContent = st.total ? st.total + ' 条' : '';
    list.querySelectorAll('[data-post]').forEach(el => {
      const pid = el.dataset.post;
      if (!pid) return;
      el.onclick = () => go('post', { id: pid });
    });
  }
})();
