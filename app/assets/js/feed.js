/* 广场 + 帖子详情 */
(function () {
  'use strict';
  const A = window.App;
  const { S, esc, codeOf, nickOf, findBot, fmtTime, avatar, api, toast, go, I } = A;

  const Feed = {
    items: [], cursor: null, loading: false, done: false, loaded: false,
    topicId: '', subtopicId: '', keyword: '', order: 'LATEST', botOnly: false, total: 0
  };
  A.Feed = Feed;

  const Q_POSTS = `query($s:ID,$t:ID,$st:ID,$k:String,$o:PostOrder,$a:String){
    posts(filter:{schoolId:$s,topicId:$t,subtopicId:$st,keyword:$k},orderBy:$o,first:12,after:$a){
      totalCount pageInfo{hasNextPage endCursor}
      edges{node{id content images isAnonymous createdAt likeCount commentCount viewerHasLiked
        creator{id username profile{nickname}} topic{id name} subtopic{id name}}}
    } }`;

  function botOf(u) { return findBot(u && u.id); }

  function postCard(p) {
    const b = botOf(p.creator);
    const nick = nickOf(p.creator);
    const imgs = (p.images || []).slice(0, 3).map(src =>
      '<img src="' + esc(src) + '" loading="lazy" alt="">').join('');
    return '<article class="post' + (b ? ' bot' : '') + '" data-post="' + esc(p.id) + '">' +
      '<div class="post-hd">' +
        '<div class="ava' + (b ? ' bot' : '') + '">' + esc(avatar(nick)) + '</div>' +
        '<div class="who">' +
          '<div class="name-line"><span class="nick">' + esc(nick) + '</span>' +
            (b ? '<span class="bot-chip">BOT<span class="bcode">' + esc(b.code) + '</span></span>' : '') +
          '</div>' +
          '<div class="meta">' + (b || p.isAnonymous ? esc(b ? b.label : '匿名') + ' · ' : '') + fmtTime(p.createdAt) + '</div>' +
        '</div>' +
        '<div class="rg"><span class="code-chip" data-code="' + esc(codeOf(p.id)) + '">#' + esc(codeOf(p.id)) + '</span></div>' +
      '</div>' +
      '<div class="content">' + esc(p.content || '') + '</div>' +
      (imgs ? '<div class="imgs">' + imgs + '</div>' : '') +
      '<div class="post-ft">' +
        '<span class="tag">' + esc((p.topic && p.topic.name) || '') + (p.subtopic ? ' / ' + esc(p.subtopic.name) : '') + '</span>' +
        '<span class="stat">' + I.heart.replace('<svg', '<svg style="width:13px;height:13px"') + (p.likeCount || 0) + '</span>' +
        '<span class="stat">' + I.comment.replace('<svg', '<svg style="width:13px;height:13px"') + (p.commentCount || 0) + '</span>' +
      '</div>' +
    '</article>';
  }

  function topicsHtml() {
    let h = '<div class="chips" id="topic-chips">' +
      '<button class="chip' + (!Feed.topicId ? ' on' : '') + '" data-topic="">全部栏目</button>';
    S.topics.forEach(t => {
      h += '<button class="chip' + (Feed.topicId === t.id ? ' on' : '') + '" data-topic="' + esc(t.id) + '">' + esc(t.name) + '</button>';
    });
    h += '</div>';
    const cur = S.topics.find(t => t.id === Feed.topicId);
    if (cur && cur.subtopics && cur.subtopics.length) {
      h += '<div class="chips" style="margin-top:7px" id="subtopic-chips">' +
        '<button class="chip' + (!Feed.subtopicId ? ' brand' : ' brand') + '" data-sub="">' + esc(cur.name) + '·全部</button>';
      cur.subtopics.forEach(st => {
        h += '<button class="chip' + (Feed.subtopicId === st.id ? ' on' : '') + '" data-sub="' + esc(st.id) + '">' + esc(st.name) + '</button>';
      });
      h += '</div>';
    }
    return h;
  }

  function feedHtml() {
    return '<div class="pane">' +
      '<div class="searchbar">' +
        '<div class="box">' + I.search +
          '<input id="q" placeholder="搜帖子 / 关键词" value="' + esc(Feed.keyword) + '">' +
          (Feed.keyword ? '<button class="clear" id="q-clear">×</button>' : '') +
        '</div>' +
        '<button class="btn sm" id="q-go" style="height:36px">搜索</button>' +
      '</div>' +
      topicsHtml() +
      '<div class="toolrow">' +
        '<div class="seg" id="order-seg">' +
          '<button data-order="LATEST" class="' + (Feed.order === 'LATEST' ? 'on' : '') + '">最新</button>' +
          '<button data-order="RANK" class="' + (Feed.order === 'RANK' ? 'on' : '') + '">热榜</button>' +
        '</div>' +
        '<button class="bot-toggle' + (Feed.botOnly ? ' on' : '') + '" id="bot-only">' +
          '<span class="sw"></span>只看我的号</button>' +
      '</div>' +
      '<div class="feed-hint" id="feed-hint"></div>' +
      '<div id="feed-list">' + visible().map(postCard).join('') + '</div>' +
      '<div id="feed-more" class="loading-row"></div>' +
    '</div>';
  }

  async function loadTopics() {
    if (S.topics.length) return;
    try {
      const d = await api(`query($s:ID!){topics(input:{schoolId:$s,first:100}){edges{node{id name
        subtopics{edges{node{id name}}}}}}}`, { s: S.schoolId });
      S.topics = (d.topics.edges || []).map(e => ({
        id: e.node.id, name: e.node.name,
        subtopics: ((e.node.subtopics && e.node.subtopics.edges) || []).map(x => ({ id: x.node.id, name: x.node.name }))
      }));
    } catch (e) { console.warn('topics', e); }
  }

  async function loadPosts(reset) {
    if (Feed.loading) return;
    if (reset) { Feed.items = []; Feed.cursor = null; Feed.done = false; }
    if (Feed.done) return;
    Feed.loading = true;
    const more = document.getElementById('feed-more');
    if (more) more.innerHTML = '<span class="spinner dark"></span> 正在翻页';
    try {
      const d = await api(Q_POSTS, {
        s: S.schoolId, t: Feed.topicId || null, st: Feed.subtopicId || null,
        k: Feed.keyword || null, o: Feed.order, a: Feed.cursor
      });
      const conn = d.posts;
      const list = (conn.edges || []).map(e => e.node);
      Feed.items = Feed.items.concat(list);
      Feed.total = conn.totalCount || 0;
      Feed.cursor = conn.pageInfo && conn.pageInfo.endCursor;
      Feed.done = !(conn.pageInfo && conn.pageInfo.hasNextPage);
      Feed.loaded = true;
      const box = document.getElementById('feed-list');
      if (box) {
        box.innerHTML = visible().map(postCard).join('') ||
          '<div class="empty"><span class="big">🕳️</span>这个洞口还没有帖子</div>';
        bindPosts(box);
      }
      const hint = document.getElementById('feed-hint');
      if (hint) {
        hint.textContent = (Feed.keyword ? '搜到 ' : '共 ') + Feed.total + ' 帖' +
          (Feed.botOnly ? ' · 仅显示我的号' : '') + ' · 已载入 ' + visible().length;
      }
      if (more) more.textContent = Feed.done ? (Feed.items.length ? '到底了' : '') : '';
      if (Feed.botOnly && visible().length < 6 && !Feed.done) setTimeout(() => loadPosts(false), 60);
    } catch (e) {
      if (more) more.innerHTML = '<span style="color:#dc2626">加载失败：' + esc(e.message) + '</span>';
    } finally {
      Feed.loading = false;
    }
  }

  function bindPosts(root) {
    root.querySelectorAll('[data-post]').forEach(el => {
      el.onclick = (ev) => {
        if (ev.target.closest('[data-code]')) {
          const c = ev.target.closest('[data-code]').dataset.code;
          copy('# ' + c);
          return;
        }
        go('post', { id: el.dataset.post });
      };
    });
  }

  function copy(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      toast('已复制 ' + text);
    } catch (e) { toast(text); }
  }
  A.copy = copy;

  function visible() {
    return Feed.botOnly ? Feed.items.filter(p => findBot(p.creator && p.creator.id)) : Feed.items;
  }

  /* ---------- tab 渲染 ---------- */
  window.Views = window.Views || {};

  Views.tab = function (tab) {
    if (tab === 'feed') return feedHtml();
    if (tab === 'xray') return Views.xray();
    if (tab === 'cmd') return Views.cmd();
    return Views.me();
  };

  Views.afterTab = function (tab) {
    if (tab === 'feed') afterFeed();
    if (tab === 'xray' && Views.xrayAfter) Views.xrayAfter();
    if (tab === 'cmd' && Views.cmdAfter) Views.cmdAfter();
    if (tab === 'me' && Views.meAfter) Views.meAfter();
  };

  function afterFeed() {
    bindPosts(document.getElementById('feed-list'));
    const q = document.getElementById('q');
    const goBtn = document.getElementById('q-go');
    const fire = () => {
      Feed.keyword = q.value.trim();
      Feed.loaded = false;
      renderFeedList();
    };
    if (goBtn) goBtn.onclick = fire;
    if (q) q.onkeydown = (e) => { if (e.key === 'Enter') { q.blur(); fire(); } };
    const clr = document.getElementById('q-clear');
    if (clr) clr.onclick = () => { q.value = ''; Feed.keyword = ''; renderFeedList(); };

    document.querySelectorAll('#topic-chips [data-topic]').forEach(b => {
      b.onclick = () => {
        Feed.topicId = b.dataset.topic;
        Feed.subtopicId = '';
        const sc = document.getElementById('screen');
        const y = sc.scrollTop;
        A.render();
        document.getElementById('screen').scrollTop = Math.min(y, 60);
        loadPosts(true);
      };
    });
    document.querySelectorAll('#subtopic-chips [data-sub]').forEach(b => {
      b.onclick = () => {
        Feed.subtopicId = b.dataset.sub;
        A.render();
        loadPosts(true);
      };
    });
    document.querySelectorAll('#order-seg [data-order]').forEach(b => {
      b.onclick = () => { Feed.order = b.dataset.order; A.render(); loadPosts(true); };
    });
    const bt = document.getElementById('bot-only');
    if (bt) bt.onclick = () => { Feed.botOnly = !Feed.botOnly; A.render(); };

    const sc = document.getElementById('screen');
    sc.onscroll = () => {
      if (sc.scrollTop + sc.clientHeight > sc.scrollHeight - 420) loadPosts(false);
    };

    if (!feedBooted) {
      feedBooted = true;
      (async () => {
        await loadTopics();
        A.render();
        await loadPosts(true);
      })();
    }
  }

  let feedBooted = false;

  function renderFeedList() {
    A.render();
    loadPosts(true);
  }
  A.renderFeedList = renderFeedList;

  /* ---------- 帖子详情 ---------- */
  const PostView = { post: null, comments: [], cursor: null, done: false, reply: null, anon: false };

  Views.postShell = function (v) {
    return '<div class="pane detail-body">' +
      '<div class="detail-head"><button class="back" id="d-back">' + I.back + '</button>' +
        '<div class="title">帖子</div><div class="grow"></div>' +
        '<span class="code-chip lg" id="d-code">#…</span></div>' +
      '<div id="detail-body"><div class="loading-row"><span class="spinner dark"></span> 正在钻洞</div></div>' +
      '</div>' +
      '<div class="composer" id="composer" style="display:none">' +
        '<div class="inputwrap"><div class="reply-hint" id="reply-hint" style="display:none">' +
          '<span id="reply-to"></span><button class="x" id="reply-cancel">×</button></div>' +
          '<input id="c-input" placeholder="说点什么…"></div>' +
        '<button class="anon-tag" id="anon">实名</button>' +
        '<button class="send" id="c-send" disabled>发送</button>' +
      '</div>';
  };

  Views.afterDetail = function (v) {
    const backBtn = document.getElementById('d-back');
    if (backBtn) backBtn.onclick = () => A.back();
    if (v.name === 'post') openPost(v.id);
    if (v.name === 'bot' && Views.openBot) Views.openBot(v.key);
  };

  async function openPost(id) {
    try {
      const d = await api(`query($id:ID!){post(id:$id){id content images isAnonymous createdAt likeCount
        commentCount viewerHasLiked viewerHasMarked viewerCanDelete creator{id username profile{nickname}}
        topic{id name} subtopic{id name}}}`, { id });
      PostView.post = d.post;
      PostView.comments = []; PostView.cursor = null; PostView.done = false; PostView.reply = null;
      document.getElementById('d-code').textContent = '#' + codeOf(id);
      renderPost();
      await loadComments(true);
    } catch (e) {
      document.getElementById('detail-body').innerHTML =
        '<div class="empty"><span class="big">⚠️</span>' + esc(e.message) + '</div>';
    }
  }

  function renderPost() {
    const p = PostView.post;
    const b = findBot(p.creator && p.creator.id);
    const nick = nickOf(p.creator);
    const imgs = (p.images || []).map(src => '<img src="' + esc(src) + '" alt="">').join('');
    document.getElementById('detail-body').innerHTML =
      '<div class="post-full">' +
        '<div class="post-hd">' +
          '<div class="ava' + (b ? ' bot' : '') + '">' + esc(avatar(nick)) + '</div>' +
          '<div class="who"><div class="name-line"><span class="nick">' + esc(nick) + '</span>' +
            (b ? '<span class="bot-chip">BOT<span class="bcode">' + esc(b.code) + '</span></span>' : '') +
          '</div><div class="meta">' + (b ? esc(b.label) + ' · ' : '') + fmtTime(p.createdAt) +
          ' · ' + esc((p.topic && p.topic.name) || '') + (p.subtopic ? ' / ' + esc(p.subtopic.name) : '') + '</div></div>' +
        '</div>' +
        '<div class="content">' + esc(p.content || '') + '</div>' +
        (imgs ? '<div class="imgs">' + imgs + '</div>' : '') +
        '<div class="full-actions">' +
          '<button class="btn ghost" id="p-like">' + I.heart + ' <span>' + (p.likeCount || 0) + '</span></button>' +
          '<button class="btn ghost" id="p-mark">' + (p.viewerHasMarked ? '★ 已收藏' : '☆ 收藏') + '</button>' +
          '<button class="btn soft" id="p-drop">设暗桩帖</button>' +
        '</div>' +
        '<div class="raid-hint" id="raid-hint">收藏后，主机端哨兵会派 4 个号来评论</div>' +
      '</div>' +
      '<div class="section-title">评论 <span class="muted tiny" id="c-count">' + (p.commentCount || 0) + '</span><span class="line"></span></div>' +
      '<div id="comment-list"></div>' +
      '<div id="comment-more"></div>';

    document.getElementById('p-back') && (document.getElementById('p-back').onclick = A.back);
    const likeBtn = document.getElementById('p-like');
    likeBtn.onclick = async () => {
      try {
        if (p.viewerHasLiked) await api('mutation($id:ID!){removeLike(input:{id:$id}){__typename}}', { id: p.id });
        else await api('mutation($id:ID!){addLike(input:{id:$id}){__typename}}', { id: p.id });
        p.viewerHasLiked = !p.viewerHasLiked;
        p.likeCount += p.viewerHasLiked ? 1 : -1;
        renderPost();
      } catch (e) { toast(e.message); }
    };
    const markBtn = document.getElementById('p-mark');
    markBtn.onclick = async () => {
      markBtn.disabled = true;
      try {
        if (p.viewerHasMarked) {
          await api('mutation($pid:ID!){removeMark(input:{postId:$pid}){__typename}}', { pid: p.id });
          p.viewerHasMarked = false;
          toast('已取消收藏');
        } else {
          await api('mutation($pid:ID!){addMark(input:{postId:$pid}){id}}', { pid: p.id });
          p.viewerHasMarked = true;
          toast('已收藏，主机会派人来');
        }
        renderPost();
      } catch (e) { toast(e.message); markBtn.disabled = false; }
    };
    document.getElementById('p-drop').onclick = () => {
      S.cfg.dropPostId = p.id;
      S.cfg.dropPostCode = codeOf(p.id);
      A.saveCfg();
      toast('已设为暗桩帖 #' + codeOf(p.id));
    };
    const comp = document.getElementById('composer');
    comp.style.display = 'flex';
    bindComposer();
  }

  function commentHtml(c) {
    const b = findBot(c.creator && c.creator.id);
    const nick = nickOf(c.creator);
    const subs = ((c.subcomments && c.subcomments.edges) || []).map(e => e.node);
    const mine = S.viewer && c.creator && c.creator.id === S.viewer.user.id;
    return '<div class="comment' + (b ? ' bot' : '') + '" data-cid="' + esc(c.id) + '">' +
      '<div class="comment-hd">' +
        '<div class="ava' + (b ? ' bot' : '') + '" style="width:26px;height:26px;font-size:11px;border-radius:8px">' + esc(avatar(nick)) + '</div>' +
        '<span class="nick">' + esc(nick) + '</span>' +
        (b ? '<span class="bot-chip">BOT<span class="bcode">' + esc(b.code) + '</span></span>' : '') +
        (mine ? '<span class="tiny muted">我</span>' : '') +
        '<span class="rg"><span class="code-chip">C-' + esc(codeOf(c.id)) + '</span>' +
          '<span class="tiny muted">' + fmtTime(c.createdAt) + '</span></span>' +
      '</div>' +
      '<div class="text">' + esc(c.content || '') + '</div>' +
      (subs.length ? '<div class="subs">' + subs.map(s => subHtml(s)).join('') +
        (c.subcommentCount > subs.length ? '<button class="more" data-expand="' + esc(c.id) + '">查看全部 ' + c.subcommentCount + ' 条回复</button>' : '') +
        '</div>' : '') +
      '<div class="row" style="margin-top:7px;gap:14px">' +
        '<button class="tiny muted" data-reply="' + esc(c.id) + '">回复</button>' +
        '<button class="tiny muted" data-like-c="' + esc(c.id) + '">♡ ' + (c.likeCount || 0) + '</button>' +
      '</div>' +
    '</div>';
  }

  function subHtml(s) {
    const b = findBot(s.creator && s.creator.id);
    const nick = nickOf(s.creator);
    const mine = S.viewer && s.creator && s.creator.id === S.viewer.user.id;
    return '<div class="sub' + (b ? ' bot' : '') + (mine ? ' mine' : '') + '" data-cid="' + esc(s.id) + '">' +
      '<span class="nick">' + esc(nick) + (b ? '·' + esc(b.code) : '') + '：</span>' +
      '<span class="txt">' + (s.replyTo ? '<span class="to">回复：</span>' : '') +
      esc(s.content || '') + '</span></div>';
  }

  async function loadComments(reset) {
    if (reset) { PostView.comments = []; PostView.cursor = null; PostView.done = false; }
    const post = PostView.post;
    const list = document.getElementById('comment-list');
    try {
      const d = await api(`query($pid:ID!,$a:String){comments(postId:$pid,first:20,order:LATEST,after:$a){
        pageInfo{hasNextPage endCursor}
        edges{node{id content createdAt likeCount subcommentCount viewerHasLiked
          creator{id username profile{nickname}}
          subcomments(first:3){edges{node{id content createdAt replyTo{id} creator{id username profile{nickname}}}}}
        }}}}`, { pid: post.id, a: PostView.cursor });
      const conn = d.comments;
      PostView.comments = PostView.comments.concat((conn.edges || []).map(e => e.node));
      PostView.cursor = conn.pageInfo && conn.pageInfo.endCursor;
      PostView.done = !(conn.pageInfo && conn.pageInfo.hasNextPage);
      list.innerHTML = PostView.comments.map(commentHtml).join('') ||
        '<div class="empty" style="padding:26px">还没有人说话，去抢个楼</div>';
      const more = document.getElementById('comment-more');
      more.innerHTML = PostView.done ? '' : '<div class="loading-row"><button class="btn ghost sm" id="c-more">加载更多评论</button></div>';
      const mb = document.getElementById('c-more');
      if (mb) mb.onclick = () => loadComments(false);
      document.querySelectorAll('[data-reply]').forEach(btn => {
        btn.onclick = () => {
          const c = PostView.comments.find(x => x.id === btn.dataset.reply);
          if (!c) return;
          const b = findBot(c.creator.id);
          PostView.reply = { commentId: c.id, name: nickOf(c.creator) + (b ? '·' + b.code : '') };
          const hint = document.getElementById('reply-hint');
          hint.style.display = 'flex';
          document.getElementById('reply-to').textContent = '回复 ' + PostView.reply.name;
          document.getElementById('c-input').focus();
        };
      });
      document.querySelectorAll('[data-expand]').forEach(btn => {
        btn.onclick = () => expandSub(btn, btn.dataset.expand);
      });
    } catch (e) {
      list.innerHTML = '<div class="empty"><span class="big">⚠️</span>评论没拉下来：' + esc(e.message) +
        '<div style="margin-top:12px"><button class="btn ghost sm" id="c-retry">重试</button></div></div>';
      const rb = document.getElementById('c-retry');
      if (rb) rb.onclick = () => loadComments(true);
    }
  }

  async function expandSub(btn, commentId) {
    btn.textContent = '加载中…';
    try {
      const d = await api(`query($c:ID!){subcomments(commentId:$c,first:50,order:DEFAULT){
        edges{node{id content createdAt replyTo{id creator{id}} creator{id username profile{nickname}}}}}}`, { c: commentId });
      const subs = (d.subcomments.edges || []).map(e => e.node);
      const wrap = btn.closest('.subs');
      wrap.innerHTML = subs.map(subHtml).join('');
    } catch (e) { btn.textContent = e.message; }
  }

  function bindComposer() {
    const input = document.getElementById('c-input');
    const send = document.getElementById('c-send');
    const anon = document.getElementById('anon');
    const cancel = document.getElementById('reply-cancel');
    if (!input) return;
    const upd = () => { send.disabled = !input.value.trim(); };
    input.oninput = upd;
    anon.classList.toggle('on', PostView.anon);
    anon.textContent = PostView.anon ? '匿名' : '实名';
    anon.onclick = () => {
      PostView.anon = !PostView.anon;
      anon.classList.toggle('on', PostView.anon);
      anon.textContent = PostView.anon ? '匿名' : '实名';
    };
    cancel.onclick = () => {
      PostView.reply = null;
      document.getElementById('reply-hint').style.display = 'none';
    };
    send.onclick = async () => {
      const text = input.value.trim();
      if (!text) return;
      send.disabled = true;
      send.textContent = '…';
      try {
        if (PostView.reply) {
          await api(`mutation($cid:ID!,$c:String!,$i:[URI!]!,$a:Boolean!,$r:ID){
            createSubcomment(input:{commentId:$cid,content:$c,images:$i,isAnonymous:$a,replyToId:$r}){id}}`,
            { cid: PostView.reply.commentId, c: text, i: [], a: PostView.anon, r: null });
        } else {
          await api(`mutation($pid:ID!,$c:String!,$i:[URI!]!,$a:Boolean!){createComment(input:{postId:$pid,content:$c,images:$i,isAnonymous:$a}){id}}`,
            { pid: PostView.post.id, c: text, i: [], a: PostView.anon });
        }
        input.value = '';
        PostView.reply = null;
        document.getElementById('reply-hint').style.display = 'none';
        toast('发出去了');
        await loadComments(true);
      } catch (e) { toast(e.message); }
      send.textContent = '发送';
      send.disabled = false;
    };
  }

  // 供透视页复用
  A.postCard = postCard;
  A.PostView = PostView;

  Views.detail = function (v) {
    if (v.name === 'post') return Views.postShell(v);
    if (v.name === 'bot' && Views.botShell) return Views.botShell(v);
    return '';
  };
})();
