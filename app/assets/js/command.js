/* 指挥台：暗桩帖 + 暗语指令 + 回执 */
(function () {
  'use strict';
  const A = window.App;
  const { S, esc, codeOf, idOfCode, findBot, fmtTime, avatar, api, toast, go, I, sheet } = A;

  const Cmd = {
    action: 'comment',
    targets: [],
    topicId: '',
    subtopicId: '',
    theme: '',
    content: '',
    code: '',
    rename: '',
    filter: '',
    sending: false,
    ackAt: 0
  };
  A.Cmd = Cmd;

  const ACTIONS = [
    { k: 'post', ico: '✍️', label: '发帖' },
    { k: 'comment', ico: '💬', label: '评论' },
    { k: 'like', ico: '♡', label: '点赞' },
    { k: 'report', ico: '📍', label: '报到' },
    { k: 'pause', ico: '🌙', label: '歇着' },
    { k: 'resume', ico: '☀️', label: '干活' },
    { k: 'rename', ico: '🏷️', label: '改名' },
    { k: 'help', ico: '📖', label: '暗语' }
  ];

  function topicName(id) {
    for (const t of S.topics) {
      if (t.id === id) return t.name;
      const st = (t.subtopics || []).find(x => x.id === id);
      if (st) return t.name + '·' + st.name;
    }
    return '';
  }

  function buildMsg(bot) {
    const open = bot.name + '，';
    switch (Cmd.action) {
      case 'post':
        return open + '写条关于「' + (Cmd.theme || '今天的校园日常') + '」的帖子，发到' + (topicName(Cmd.subtopicId || Cmd.topicId) || '校园生活') + '。';
      case 'comment':
        return open + '去 ' + (Cmd.code || '——') + ' 底下说句「' + (Cmd.content || '路过，帮顶') + '」。';
      case 'like':
        return open + '给 ' + (Cmd.code || '——') + ' 捧个场。';
      case 'report':
        return open + '报个到。';
      case 'pause':
        return open + '先歇会儿，别冒头。';
      case 'resume':
        return open + '出来干活。';
      case 'rename':
        return open + '以后叫你「' + (Cmd.rename || bot.name) + '」。';
      default:
        return open + '有空吗。';
    }
  }

  function valid() {
    if (!Cmd.targets.length) return '选几个号';
    if (Cmd.action === 'post' && !Cmd.theme.trim()) return '写个主题';
    if (Cmd.action === 'comment' && (!Cmd.code.trim() || !Cmd.content.trim())) return '填帖代号和内容';
    if (Cmd.action === 'like' && !Cmd.code.trim()) return '填帖代号';
    if (Cmd.action === 'rename' && !Cmd.rename.trim()) return '填新名字';
    return '';
  }

  function banner() {
    if (!S.cfg.dropPostId) {
      return '<div class="cmd-banner"><div class="grow"><div class="k">信箱 · 自由投递</div>' +
        '<div class="v">FREE DROP</div>' +
        '<div class="k" style="margin-top:2px">回复每个号最近一条评论，不需要集中报到</div></div>' +
        '<button class="btn sm ghost" id="set-drop">改用暗桩帖</button></div>';
    }
    const ready = S.roster.filter(b => (A.Xray.anchors || {})[b.userId]).length;
    return '<div class="cmd-banner"><div class="grow"><div class="k">暗桩帖 · 悄悄话信箱</div>' +
      '<div class="v">#' + esc(S.cfg.dropPostCode || codeOf(S.cfg.dropPostId)) + '</div>' +
      '<div class="k" style="margin-top:2px">就位 ' + ready + '/' + S.roster.length + ' · 点右侧换帖</div></div>' +
      '<button class="btn sm ghost" id="set-drop">换帖</button></div>';
  }

  function targetsHtml() {
    const f = Cmd.filter.trim();
    return S.roster.map(b => {
      const anchor = (A.Xray.anchors || {})[b.userId];
      const ready = !S.cfg.dropPostId || anchor;
      const on = Cmd.targets.includes(b.key);
      if (f && !(b.name.includes(f) || b.code.includes(f.toUpperCase()) || (b.college || '').includes(f))) return '';
      return '<button class="tchip' + (on ? ' on' : '') + (!ready ? ' miss' : '') + '" data-t="' + esc(b.key) + '">' +
        '<span class="c">' + esc(b.code) + '</span>' + esc(b.name) + (ready ? '' : '·未就位') + '</button>';
    }).join('');
  }

  function paramsHtml() {
    const a = Cmd.action;
    let h = '';
    if (a === 'post') {
      const opts = [];
      S.topics.forEach(t => {
        opts.push('<option value="' + esc(t.id) + '">' + esc(t.name) + '</option>');
        (t.subtopics || []).forEach(st => opts.push('<option value="' + esc(st.id) + '">　' + esc(t.name) + '·' + esc(st.name) + '</option>'));
      });
      h += '<div class="field"><label>主题（会交给它的脑子去写）</label>' +
        '<input type="text" id="f-theme" placeholder="例如：今晚图书馆四楼占座" value="' + esc(Cmd.theme) + '"></div>' +
        '<div class="field"><label>发到哪个栏目</label><select id="f-topic"><option value="">默认栏目</option>' + opts.join('') + '</select></div>';
    }
    if (a === 'comment' || a === 'like') {
      h += '<div class="field"><label>帖子代号（# 后面那串）</label>' +
        '<input type="text" id="f-code" placeholder="例如 3F9K" value="' + esc(Cmd.code) + '"></div>';
    }
    if (a === 'comment') {
      h += '<div class="field"><label>让它说什么</label>' +
        '<textarea id="f-content" placeholder="例如：这也太真实了，我上周也这样">' + esc(Cmd.content) + '</textarea></div>';
    }
    if (a === 'rename') {
      h += '<div class="field"><label>新称呼</label><input type="text" id="f-rename" value="' + esc(Cmd.rename) + '" placeholder="新名字"></div>';
    }
    return h;
  }

  Views.cmd = function () {
    if (!Cmd.targets.length && S.cfg.cmdTargets) {
      Cmd.targets = S.cfg.cmdTargets.slice();
      delete S.cfg.cmdTargets;
    }
    const err = valid();
    const preview = Cmd.targets.length
      ? Cmd.targets.slice(0, 3).map(k => {
          const b = S.roster.find(x => x.key === k);
          return b ? '<span class="hl">' + esc(b.code) + '</span> ' + esc(buildMsg(b)) : '';
        }).join('\n') + (Cmd.targets.length > 3 ? '\n… 共 ' + Cmd.targets.length + ' 条' : '')
      : '<span style="color:#64748b">选好号之后，这里会显示要发出去的暗语</span>';

    return '<div class="pane">' +
      '<div class="card" style="padding:10px 13px;font-size:11.5px;color:#92400e;background:#fffbeb;border-color:#fde68a;line-height:1.7">' +
        '暗语通道已在主机端停用（whisper.enabled=false），这里发的指令不会被执行；收藏派单走哨兵，不需要暗语。' +
      '</div>' + banner() +
      (S.cfg.dropPostId && !S.roster.some(b => (A.Xray.anchors || {})[b.userId])
        ? '<div class="card" style="padding:11px 13px;font-size:12px;color:#92400e;background:#fffbeb;border-color:#fde68a;line-height:1.7">' +
          '还没有号报到。去 szlike_bot 跑一次 <b>python run.py whisper --rollcall</b>，它们会在暗桩帖下各留一句话。' +
          '</div>' : '') +
      '<div class="section-title">要它们干什么<span class="line"></span></div>' +
      '<div class="form"><div class="actions-grid" id="act-grid">' +
        ACTIONS.map(a => '<button data-a="' + a.k + '" class="' + (Cmd.action === a.k ? 'on' : '') + '">' +
          '<span class="ico">' + a.ico + '</span>' + a.label + '</button>').join('') +
      '</div></div>' +

      '<div class="section-title">对谁下<span class="line"></span>' +
        '<button class="tiny" id="t-all" style="color:#0b7a55">全选</button>' +
        '<button class="tiny" id="t-none" style="color:#94a3b8">清空</button></div>' +
      '<div class="form"><div class="field">' +
        '<div class="inline" style="margin-bottom:7px"><input type="text" id="t-filter" placeholder="搜称呼 / 代号 / 学院" value="' + esc(Cmd.filter) + '" style="font-size:12px">' +
          '<span class="tiny muted" id="t-count">' + Cmd.targets.length + ' 个</span></div>' +
        '<div class="target-list" id="target-list">' + targetsHtml() + '</div>' +
      '</div></div>' +

      (paramsHtml() ? '<div class="section-title">说什么<span class="line"></span></div><div class="form">' + paramsHtml() + '</div>' : '') +

      '<div class="section-title">暗语预览<span class="line"></span></div>' +
      '<div class="form"><div class="preview" id="preview">' + preview + '</div>' +
      '<button class="btn block" id="send" style="margin-top:10px;height:44px" ' + (err ? 'disabled' : '') + '>' +
        (err ? esc(err) : '发暗语 · ' + Cmd.targets.length + ' 条') + '</button>' +
      '<div class="tiny muted" style="margin-top:7px;line-height:1.6">悄悄话会以你的小号身份，回复到每个号在暗桩帖下的那条留言下面。论坛没有私信接口，这已经是唯一不惊动别人的通道。</div></div>' +

      '<div class="section-title">指令记录<span class="line"></span>' +
        '<button class="tiny" id="ack-refresh" style="color:#0b7a55">刷新回执</button></div>' +
      '<div class="card" id="log-list">' + logsHtml() + '</div>' +
    '</div>';
  };

  function logsHtml() {
    const logs = (S.cfg.logs || []).slice(0, 20);
    if (!logs.length) return '<div class="empty" style="padding:26px">还没有下发过指令</div>';
    return logs.map(l => {
      const ack = l.acks && Object.keys(l.acks).length
        ? Object.keys(l.acks).map(k => {
            const a = l.acks[k];
            return '<div class="ack">' + esc(k) + '：' + esc(a.text) + ' · ' + fmtTime(a.at) + '</div>';
          }).join('')
        : '<div class="wait">等待回执…</div>';
      return '<div class="log-item"><div class="t"><span class="code-chip">' + esc(l.id) + '</span>' +
        fmtTime(l.at) + '<span class="grow"></span><span>' + esc(l.actionLabel) + ' · ' + (l.targets || []).length + ' 个号</span></div>' +
        '<div class="m">' + esc((l.targets || []).map(t => t.code + ' ' + t.name).join('、')) + '</div>' + ack + '</div>';
    }).join('');
  }

  Views.cmdAfter = async function () {
    // 设置暗桩帖
    const sd = document.getElementById('set-drop');
    if (sd) sd.onclick = () => setDrop();
    // 动作
    document.querySelectorAll('#act-grid [data-a]').forEach(b => {
      b.onclick = () => {
        if (b.dataset.a === 'help') return help();
        Cmd.action = b.dataset.a;
        A.render();
      };
    });
    // 目标
    document.querySelectorAll('#target-list [data-t]').forEach(b => {
      b.onclick = () => {
        const k = b.dataset.t;
        const i = Cmd.targets.indexOf(k);
        if (i >= 0) Cmd.targets.splice(i, 1); else Cmd.targets.push(k);
        A.render();
      };
    });
    const tf = document.getElementById('t-filter');
    if (tf) tf.oninput = () => {
      Cmd.filter = tf.value;
      document.getElementById('target-list').innerHTML = targetsHtml();
      Views.cmdAfter();
    };
    const all = document.getElementById('t-all');
    if (all) all.onclick = () => {
      Cmd.targets = S.roster.filter(b => !S.cfg.dropPostId || (A.Xray.anchors || {})[b.userId]).map(b => b.key);
      A.render();
    };
    const none = document.getElementById('t-none');
    if (none) none.onclick = () => { Cmd.targets = []; A.render(); };

    ['theme', 'content', 'code', 'rename'].forEach(f => {
      const el = document.getElementById('f-' + f);
      if (el) el.oninput = () => {
        Cmd[f] = el.value;
        refreshPreview();
        const btn = document.getElementById('send');
        const err = valid();
        btn.disabled = !!err;
        btn.textContent = err || ('发暗语 · ' + Cmd.targets.length + ' 条');
      };
    });
    const tk = document.getElementById('f-topic');
    if (tk) {
      tk.value = Cmd.subtopicId || Cmd.topicId || '';
      tk.onchange = () => {
        const v = tk.value;
        Cmd.subtopicId = v;
        Cmd.topicId = v;
        refreshPreview();
      };
    }
    const send = document.getElementById('send');
    if (send) send.onclick = doSend;
    const ar = document.getElementById('ack-refresh');
    if (ar) ar.onclick = refreshAcks;

    if (!S.topics.length) {
      try {
        const d = await api(`query($s:ID!){topics(input:{schoolId:$s,first:100}){edges{node{id name subtopics{edges{node{id name}}}}}}}`, { s: S.schoolId });
        S.topics = (d.topics.edges || []).map(e => ({
          id: e.node.id, name: e.node.name,
          subtopics: ((e.node.subtopics && e.node.subtopics.edges) || []).map(x => ({ id: x.node.id, name: x.node.name }))
        }));
        A.render();
      } catch (e) { /* ignore */ }
    }
    if (!A.Xray.loaded && !A.Xray.loading && A.xrayReload) A.xrayReload();
  };

  function refreshPreview() {
    const p = document.getElementById('preview');
    if (!p) return;
    p.innerHTML = Cmd.targets.length
      ? Cmd.targets.slice(0, 3).map(k => {
          const b = S.roster.find(x => x.key === k);
          return b ? '<span class="hl">' + esc(b.code) + '</span> ' + esc(buildMsg(b)) : '';
        }).join('\n')
      : '<span style="color:#64748b">选好号之后，这里会显示要发出去的暗语</span>';
  }

  /* ---------- 暗桩帖 ---------- */
  function setDrop() {
    sheet({
      title: '设置暗桩帖',
      body: '<div class="sfield"><label>帖子代号（# 后面那串，或帖子里复制的 ID）</label>' +
        '<input id="drop-code" placeholder="例如 3F9K" value="' + esc(S.cfg.dropPostCode || '') + '"></div>' +
        '<div class="tiny muted" style="line-height:1.7">选一条不起眼、你会正常互动的帖子当信箱。所有号都会在这条帖子下面留一句话，之后你回复它们就是下指令。' +
        '<br>小技巧：打开任意帖子，详情页右上角有代号，点一下就能复制。</div>',
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="drop-ok">保存</button>',
      onMount(box, close) {
        box.querySelector('#drop-ok').onclick = async () => {
          const raw = box.querySelector('#drop-code').value.trim();
          const id = /^[A-Za-z0-9+/=]{10,}$/.test(raw) ? raw : idOfCode(raw, 'Post');
          if (!id) { toast('代号不对'); return; }
          close();
          S.cfg.dropPostId = id;
          S.cfg.dropPostCode = codeOf(id);
          A.saveCfg();
          A.Xray.loaded = false;
          toast('暗桩帖已设为 #' + S.cfg.dropPostCode);
          A.render();
          A.xrayReload && A.xrayReload();
          const cmdline = 'python run.py whisper --drop ' + S.cfg.dropPostCode;
          sheet({
            title: '让机器人也认这条帖子',
            body: '<div style="font-size:12.5px;color:#334155;line-height:1.8">在 <b>szlike_bot</b> 目录跑一次：' +
              '<div class="preview" style="margin:8px 0;max-height:none">' + esc(cmdline) + '<br>' +
              'python run.py whisper --rollcall</div>' +
              '第一条把暗桩帖写进 config；第二条让每个号在帖子下留一句话当信箱。之后 daemon 会自动收暗语。</div>',
            footer: '<button class="btn ghost" id="cp-cmd">复制命令</button><button class="btn" data-close>好</button>',
            onMount(box) {
              box.querySelector('#cp-cmd').onclick = () => A.copy(cmdline);
            }
          });
        };
      }
    });
  }

  /* ---------- 发送 ---------- */
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  async function anchorFor(bot) {
    const a = (A.Xray.anchors || {})[bot.userId];
    if (S.cfg.dropPostId && a) return a.commentId;
    const d = await api(`query($id:ID!){user(userId:$id){userComments(first:1,type:CREATED){edges{node{id}}}}}`,
      { id: bot.userId });
    const edges = (d.user && d.user.userComments && d.user.userComments.edges) || [];
    return edges.length ? edges[0].node.id : '';
  }

  async function doSend() {
    const err = valid();
    if (err) { toast(err); return; }
    const picked = Cmd.targets.map(k => S.roster.find(b => b.key === k)).filter(Boolean);
    const batch = picked.slice(0, 10);
    const overflow = picked.length - batch.length;

    const ok = await A.confirmBox('确认下发',
      '将给 ' + batch.length + ' 个号各发一条暗语' +
      (overflow > 0 ? '（一次最多 10 个，其余 ' + overflow + ' 个下次再发）' : '') + '。', '发出去');
    if (!ok) return;

    Cmd.sending = true;
    const btn = document.getElementById('send');
    btn.disabled = true;
    btn.textContent = '发送中…';
    const log = {
      id: 'D' + Date.now().toString(36).toUpperCase().slice(-4),
      at: new Date().toISOString(),
      action: Cmd.action,
      actionLabel: (ACTIONS.find(a => a.k === Cmd.action) || {}).label || Cmd.action,
      targets: [], acks: {}
    };
    if (Cmd.action === 'rename' && Cmd.rename) log.newName = Cmd.rename.trim();
    let fail = 0;
    for (const bot of batch) {
      const text = buildMsg(bot);
      try {
        const cid = await anchorFor(bot);
        if (!cid) { fail++; continue; }
        const d = await api(`mutation($cid:ID!,$c:String!,$i:[URI!]!,$a:Boolean!,$r:ID){
          createSubcomment(input:{commentId:$cid,content:$c,images:$i,isAnonymous:$a,replyToId:$r}){id}}`,
          { cid, c: text, i: [], a: false, r: null });
        log.targets.push({ key: bot.key, code: bot.code, name: bot.name, commentId: cid, subId: d.createSubcomment.id, text });
      } catch (e) {
        fail++;
        console.warn(bot.code, e.message);
      }
      btn.textContent = '发送中 ' + log.targets.length + '/' + batch.length;
      await sleep(900 + Math.random() * 600);      // 给接口留点余地
    }
    if (log.targets.length) {
      S.cfg.logs = [log].concat(S.cfg.logs || []).slice(0, 30);
      A.saveCfg();
    }
    toast(log.targets.length ? '已下发 ' + log.targets.length + ' 条' + (fail ? '，' + fail + ' 个跳过' : '') : '全部失败');
    Cmd.sending = false;
    A.render();
    if (log.targets.length) setTimeout(refreshAcks, 3000);
  }

  /* ---------- 回执 ---------- */
  async function refreshAcks() {
    const logs = S.cfg.logs || [];
    if (!logs.length) return;
    const pairs = [];
    logs.forEach(l => (l.targets || []).forEach(t => {
      if (!l.acks[t.code] && t.commentId) pairs.push({ l, t });
    }));
    if (!pairs.length) { toast('没有待回执的指令'); return; }
    try {
      let changed = false;
      for (const { l, t } of pairs.slice(0, 8)) {
        const sd = await api(`query($c:ID!){subcomments(commentId:$c,first:50,order:DEFAULT){
          edges{node{id content createdAt creator{id username profile{nickname}} replyTo{id}}}}}`, { c: t.commentId });
        const subs = (sd.subcomments.edges || []).map(e => e.node);
        const reply = subs.find(x => x.replyTo && x.replyTo.id === t.subId &&
          x.creator && !findBot(x.creator.id));
        if (reply) {
          l.acks[t.code] = { text: reply.content, at: reply.createdAt };
          changed = true;
        }
        await sleep(500);
      }
      if (changed) {
        // 改名指令收到回执后，本地花名册同步更新（成员更新）
        logs.forEach(l => {
          if (l.action !== 'rename' || !l.newName) return;
          const done = (l.targets || []).some(t => l.acks[t.code]);
          if (!done) return;
          (l.targets || []).forEach(t => {
            const bot = S.roster.find(b => b.key === t.key);
            if (bot) { bot.name = l.newName; t.name = l.newName; }
          });
        });
        A.store.save('roster', { list: S.roster });
        S.cfg.logs = logs;
        A.saveCfg();
        A.render();
        toast('收到新回执');
      } else {
        toast('暂无新回执');
      }
    } catch (e) { toast(e.message); }
  }

  function help() {
    sheet({
      title: '暗语表',
      body: '<div style="font-size:12.5px;line-height:1.9;color:#334155">' +
        '<p style="margin:0 0 8px">每条暗语都是日常口气，路人扫过去只会以为是闲聊。你的号只认「暗桩帖 + 回复它自己那条留言」这个组合。</p>' +
        '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
        [['写条关于「X」的帖子，发到Y。', '让号发一条主题帖，内容它自己写'],
         ['去 3F9K 底下说句「…」。', '在指定帖子里留一句话'],
         ['给 3F9K 捧个场。', '给指定帖子点赞'],
         ['报个到。', '回执一条自身状态'],
         ['先歇会儿，别冒头。', '暂停该号的全部任务'],
         ['出来干活。', '恢复该号的任务'],
         ['以后叫你「X」。', '改称呼（本地花名册）']]
          .map(r => '<tr><td style="padding:6px 0;border-bottom:1px solid #eef1f6;color:#0b7a55">' + r[0] + '</td>' +
            '<td style="padding:6px 0 6px 10px;border-bottom:1px solid #eef1f6;color:#64748b">' + r[1] + '</td></tr>').join('') +
        '</table></div>',
      footer: '<button class="btn block" data-close>知道了</button>'
    });
  }

  A.cmdRefreshAcks = refreshAcks;
  A.anchorFor = anchorFor;
})();
