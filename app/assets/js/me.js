/* 我的：账号、花名册、设置 */
(function () {
  'use strict';
  const A = window.App;
  const { S, esc, codeOf, nickOf, avatar, api, toast, sheet, I } = A;

  function accName(acc) {
    return acc.nickname || acc.username || acc.label || '账号';
  }

  Views.me = function () {
    const accs = Object.values(S.accounts || {});
    const cur = S.accounts[S.active] || {};
    return '<div class="pane">' +
      '<div class="card"><div class="profile-card">' +
        '<div class="ava" style="background:#0b1220;color:#fff">' + esc(avatar(accName(cur))) + '</div>' +
        '<div class="grow"><div class="nm">' + esc(accName(cur)) + ' <span class="bot-chip" style="background:#e8f7f1;border-color:#bfe9d9;color:#0b7a55">小号</span></div>' +
          '<div class="tiny muted">@' + esc(cur.username || '—') + (cur.institute ? ' · ' + esc(cur.institute) : '') + '</div></div>' +
        '<span class="code-chip">' + (A.HAS_NATIVE ? 'v1.0.0' : 'dev') + '</span>' +
      '</div></div>' +

      '<div class="section-title">账号池<span class="line"></span>' +
        '<button class="tiny" id="acc-add" style="color:#0b7a55">+ 添加</button></div>' +
      '<div class="card">' +
        (accs.length ? accs.map(a => '<div class="list-row" data-acc="' + esc(a.key) + '">' +
          '<div class="ava" style="width:28px;height:28px;font-size:11px;border-radius:8px">' + esc(avatar(accName(a))) + '</div>' +
          '<div class="grow"><div style="font-size:13px">' + esc(accName(a)) + (a.key === S.active ? ' <span class="tiny" style="color:#0b7a55">当前</span>' : '') + '</div>' +
          '<div class="tiny muted">' + esc(a.label || a.username) + '</div></div>' +
          '<span class="chev">›</span></div>').join('') : '<div class="empty" style="padding:24px">还没有账号</div>') +
      '</div>' +

      '<div class="section-title">花名册<span class="line"></span><span class="tiny muted">' + S.roster.length + ' 个号</span></div>' +
      '<div class="card">' +
        (S.roster.length ? S.roster.map((b, i) =>
          '<div class="list-row" data-rb="' + i + '">' +
            '<div class="grow"><div style="font-size:13px">' + esc(b.name || b.username || ('号' + (i + 1))) +
              (b.username ? ' <span class="tiny muted">@' + esc(b.username) + '</span>' : '') + '</div>' +
              '<div class="tiny muted">' + esc(b.label || b.campus || b.college || b.code || '') + '</div></div>' +
            '<span class="chev">' + (b.password ? '登录' : '—') + '</span></div>').join('')
          : '<div class="empty" style="padding:20px">还没有花名册，点下面导入</div>') +
      '</div>' +
      '<div class="card">' +
        '<div class="list-row" id="roster-scan"><span class="k">扫码导入</span><span class="v">拍电脑上的二维码</span><span class="chev">›</span></div>' +
        '<div class="list-row" id="roster-import"><span class="k">从文件导入</span><span class="v">roster.json</span><span class="chev">›</span></div>' +
        '<div class="list-row" id="roster-paste"><span class="k">粘贴导入</span><span class="v">剪贴板 JSON</span><span class="chev">›</span></div>' +
        '<div class="list-row" id="roster-export"><span class="k">导出花名册</span><span class="v">复制 JSON</span><span class="chev">›</span></div>' +
        '<div class="list-row" id="roster-reset"><span class="k">恢复内置花名册</span><span class="v">32 个号</span><span class="chev">›</span></div>' +
        '<div class="list-row" id="roster-add"><span class="k">手动加号</span><span class="v">按 user id</span><span class="chev">›</span></div>' +
      '</div>' +

      '<div class="section-title">哨兵<span class="line"></span></div>' +
      '<div class="card">' +
        '<div class="list-row"><span class="k">监听方式</span><span class="v">主机轮询收藏</span></div>' +
        '<div class="list-row"><span class="k">触发后</span><span class="v">4 个对口性格的号</span></div>' +
        '<div class="list-row"><span class="k">暗语通道</span><span class="v" style="color:#b45309">已停用</span></div>' +
        '<div style="padding:10px 14px 12px;font-size:11.5px;color:#64748b;line-height:1.8">' +
          '收藏由主机端的哨兵进程发现，App 只负责收藏，不再往帖子里发任何指令。' +
          '启停：在 szlike_bot 目录运行 <b>watch_marks.bat</b>。' +
        '</div>' +
      '</div>' +

      '<div class="section-title">暗桩<span class="line"></span></div>' +
      '<div class="card">' +
        '<div class="list-row" id="drop-row"><span class="k">暗桩帖</span>' +
          '<span class="v">' + (S.cfg.dropPostId ? '#' + esc(S.cfg.dropPostCode || codeOf(S.cfg.dropPostId)) : '未设置') + '</span><span class="chev">›</span></div>' +
        '<div class="list-row" id="ack-row"><span class="k">刷新回执</span><span class="v">悄悄话回复</span><span class="chev">›</span></div>' +
      '</div>' +

      '<div class="section-title">诊断<span class="line"></span></div>' +
      '<div class="card">' +
        '<div class="list-row" id="diag"><span class="k">接口连通性</span><span class="v" id="diag-v">点一下测试</span><span class="chev">›</span></div>' +
        '<div class="list-row"><span class="k">学校</span><span class="v">' + esc(S.schoolId) + '</span></div>' +
        '<div class="list-row"><span class="k">代号规则</span><span class="v">base36 · 可逆</span></div>' +
      '</div>' +

      '<div class="card" style="padding:12px 14px;font-size:11.5px;color:#64748b;line-height:1.8;margin-bottom:24px">' +
        '鼠洞透视 · 加强版客户端<br>只做标注、代号与暗语通道，浏览、栏目、搜索都走官方接口。' +
      '</div>' +
    '</div>';
  };

  Views.meAfter = function () {
    document.querySelectorAll('[data-acc]').forEach(el => {
      el.onclick = async () => {
        const key = el.dataset.acc;
        if (key === S.active) return;
        A.setActive(key);
        toast('已切换到 ' + accName(S.accounts[key]));
        try {
          const v = await api('query{viewer{id user{id username profile{nickname institute}} viewerVerify{school{id}}}}');
          S.viewer = v.viewer;
        } catch (e) { /* ignore */ }
        A.render();
      };
    });
    const add = document.getElementById('acc-add');
    if (add) add.onclick = addAccount;

    document.getElementById('roster-export').onclick = () => {
      A.copy(JSON.stringify(S.roster, null, 1));
    };
    document.getElementById('roster-reset').onclick = async () => {
      const ok = await A.confirmBox('恢复内置花名册', '会用 32 个内置号覆盖当前花名册，手动加的号会丢。', '恢复');
      if (!ok) return;
      try {
        const raw = A.HAS_NATIVE ? window.SZ.asset('roster.json') : '';
        S.roster = JSON.parse(raw);
        A.store.save('roster', { list: S.roster });
        A.Xray.loaded = false;
        toast('已恢复');
        A.render();
      } catch (e) { toast('恢复失败'); }
    };
    document.querySelectorAll('[data-rb]').forEach(el => {
      el.onclick = () => loginFromRoster(+el.dataset.rb);
    });
    const rbScan = document.getElementById('roster-scan');
    if (rbScan) rbScan.onclick = scanRoster;
    const rbImp = document.getElementById('roster-import');
    if (rbImp) rbImp.onclick = importRosterFile;
    const rbPaste = document.getElementById('roster-paste');
    if (rbPaste) rbPaste.onclick = pasteRoster;
    document.getElementById('roster-add').onclick = addBot;
    document.getElementById('drop-row').onclick = () => { S.tab = 'cmd'; A.render(); };
    document.getElementById('ack-row').onclick = () => A.cmdRefreshAcks && A.cmdRefreshAcks();
    document.getElementById('diag').onclick = async () => {
      const v = document.getElementById('diag-v');
      v.textContent = '测试中…';
      const t0 = Date.now();
      try {
        await api('query{viewer{id}}');
        v.textContent = '正常 · ' + (Date.now() - t0) + 'ms';
        v.style.color = '#0b7a55';
      } catch (e) {
        v.textContent = '失败：' + e.message;
        v.style.color = '#dc2626';
      }
    };
  };

  function addAccount() {
    sheet({
      title: '添加账号',
      body: '<div class="sfield"><label>用户名</label><input id="ac-u" autocomplete="off" placeholder="登录名"></div>' +
        '<div class="sfield"><label>密码</label><input id="ac-p" type="password" placeholder="密码"></div>' +
        '<div class="sfield"><label>备注</label><input id="ac-l" placeholder="例如：主机小号"></div>',
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="ac-ok">登录并保存</button>',
      onMount(box, close) {
        box.querySelector('#ac-ok').onclick = async () => {
          const u = box.querySelector('#ac-u').value.trim();
          const p = box.querySelector('#ac-p').value;
          const l = box.querySelector('#ac-l').value.trim();
          if (!u || !p) { toast('填完整'); return; }
          const btn = box.querySelector('#ac-ok');
          btn.textContent = '登录中…';
          try {
            const acc = await A.login(u, p, l);
            S.accounts[acc.key] = acc;
            A.setActive(acc.key);
            close();
            toast('已添加 ' + (acc.nickname || acc.username));
            A.render();
          } catch (e) {
            btn.textContent = '登录并保存';
            toast(e.message);
          }
        };
      }
    });
  }

  /* ---------- 花名册：导入 / 扫码 / 一键登录 ---------- */

  function applyRoster(list, src) {
    if (!Array.isArray(list) || !list.length) { toast('里面没有账号'); return; }
    S.roster = list.map((x, i) => ({
      code: x.code || ('B' + String(i + 1).padStart(2, '0')),
      key: x.key || ('x_' + i),
      name: x.name || x.label || ('号' + (i + 1)),
      label: x.label || '',
      campus: x.campus || '',
      college: x.college || '',
      username: x.username || '',
      password: x.password || '',
      userId: x.userId || x.user_id || '',
    }));
    A.store.save('roster', { list: S.roster });
    A.Xray.loaded = false;
    toast((src || '导入') + ' ' + S.roster.length + ' 个号');
    A.render();
  }

  async function loginFromRoster(i) {
    const b = S.roster[i];
    if (!b) return;
    if (!b.username || !b.password) {
      toast('这个号没带密码，先在电脑端点「手机同步」重新导一次');
      return;
    }
    toast('登录 ' + (b.name || b.username) + '…');
    try {
      await A.login(b.username, b.password, b.label || b.name);
      location.reload();
    } catch (e) { toast('登录失败：' + e.message); }
  }

  function pickFile(accept) {
    return new Promise(resolve => {
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = accept;
      inp.style.display = 'none';
      document.body.appendChild(inp);
      inp.onchange = () => {
        resolve(inp.files && inp.files[0]);
        inp.remove();
      };
      inp.click();
    });
  }

  async function importRosterFile() {
    const f = await pickFile('.json,application/json');
    if (!f) return;
    try {
      const d = JSON.parse(await f.text());
      applyRoster(Array.isArray(d) ? d : (d.list || d.roster || []), '已导入');
    } catch (e) { toast('读不出来：' + e.message); }
  }

  function pasteRoster() {
    sheet({
      title: '粘贴花名册',
      body: '<textarea id="rb-json" rows="8" placeholder="把电脑上复制的 JSON 粘进来"></textarea>',
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="rb-ok">导入</button>',
      onMount(box, close) {
        box.querySelector('#rb-ok').onclick = () => {
          try {
            const d = JSON.parse(box.querySelector('#rb-json').value);
            close();
            applyRoster(Array.isArray(d) ? d : (d.list || []), '已导入');
          } catch (e) { toast('JSON 格式不对'); }
        };
      }
    });
  }

  function scanRoster() {
    pickFile('image/*').then(async f => {
      if (!f) return;
      if (typeof jsQR !== 'function') { toast('扫码组件没加载'); return; }
      const img = await new Promise((res, rej) => {
        const u = URL.createObjectURL(f);
        const im = new Image();
        im.onload = () => { URL.revokeObjectURL(u); res(im); };
        im.onerror = () => { URL.revokeObjectURL(u); rej(new Error('图片打不开')); };
        im.src = u;
      }).catch(e => { toast(e.message); return null; });
      if (!img) return;
      // 大图先缩到 1400 以内：jsQR 在 4000px 的原图上又慢又容易糊
      const k = Math.min(1, 1400 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * k);
      c.height = Math.round(img.height * k);
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0, c.width, c.height);
      const px = ctx.getImageData(0, 0, c.width, c.height);
      const r = jsQR(px.data, px.width, px.height);
      if (!r) { toast('没扫出来，靠近点、拍清楚重试'); return; }
      const t = (r.data || '').trim();
      if (t.startsWith('SD1')) {
        const lines = t.split('\n').slice(1).filter(Boolean);
        applyRoster(lines.map((ln, i) => {
          const p = ln.split('|');
          return {
            code: 'B' + String(i + 1).padStart(2, '0'), key: 'x_' + i,
            name: p[0] || ('号' + (i + 1)), label: p[0] || '',
            username: p[1] || '', password: p[2] || '', userId: p[3] || '',
          };
        }), '扫码导入');
      } else {
        try { applyRoster(JSON.parse(t), '扫码导入'); }
        catch (e) { toast('二维码内容认不出来'); }
      }
    });
  }

  function addBot() {
    sheet({
      title: '手动加号',
      body: '<div class="sfield"><label>昵称</label><input id="rb-n" placeholder="阿砚"></div>' +
        '<div class="sfield"><label>代号（留空自动）</label><input id="rb-c" placeholder="B33"></div>' +
        '<div class="sfield"><label>user id（VXNlcjo… 或数字）</label><input id="rb-i" placeholder="用户ID"></div>' +
        '<div class="tiny muted">get user id：在帖子里看到这个号，详情页的评论代号旁会显示。也可以填用户名，加完自己核对。</div>',
      footer: '<button class="btn ghost" data-close>取消</button><button class="btn" id="rb-ok">加入</button>',
      onMount(box, close) {
        box.querySelector('#rb-ok').onclick = () => {
          const n = box.querySelector('#rb-n').value.trim();
          let c = box.querySelector('#rb-c').value.trim().toUpperCase();
          let i = box.querySelector('#rb-i').value.trim();
          if (!n || !i) { toast('昵称和 id 都要'); return; }
          if (/^\d+$/.test(i)) i = btoa('User:' + i).replace(/=+$/, '');
          if (!c) c = 'B' + String(S.roster.length + 1).padStart(2, '0');
          S.roster.push({ key: 'x_' + i.slice(-6), code: c, name: n, label: '手动添加', college: '', username: '', userId: i });
          A.store.save('roster', { list: S.roster });
          close();
          toast('已加入 ' + n);
          A.render();
        };
      }
    });
  }
})();
