/* ===================================================================
   微信 · 本地镜像  ——  原生 JS 逻辑（无框架依赖）
   布局：左侧深色导航（聊天 / 通讯录 / 朋友圈 + 左下角设置）
         中间列表  +  右侧对话 / 朋友圈 / 资料
   =================================================================== */
(function () {
  "use strict";

  var TOKEN_KEY = "bridge_admin_token";
  var $ = function (id) { return document.getElementById(id); };
  var EMOJIS = ("😀 😁 😂 🤣 😊 😍 😘 😎 🤔 😅 😭 😡 😴 🥱 🙄 😇 🤗 " +
    "👍 👎 👏 🙏 💪 🤝 👌 ✌️ 🤞 ❤️ 💔 💕 💯 🔥 ✨ 🎉 🎁 🌹 🍺 🍻 ☕ " +
    "🐶 🐱 🌈 ☀️ 🌙 ⭐ 🍎 🍉 🏀 ⚽ 🚀 💡 📱 💻 📷 🎵 📚 ✅ ❌ ⚠️ 🆗").split(" ");

  var state = {
    nav: "chat",            // chat | contacts | moments
    contactTab: "friend",   // friend | group | official
    conversations: [],
    contacts: { friend: [], group: [], official: [] },
    contactsLoaded: { friend: false, group: false, official: false },
    moments: null,
    search: "",
    current: null,
    chat: { thread: null, draft: "", emojiOpen: false, seq: 0, timer: null, sending: false, limit: 120 },
    detail: null,
    apps: {
      view: "home",                  // home | reminders | article-push
      data: null,
      loading: false,
      timer: null,
      filters: { status: "", kind: "", delivery: "", q: "" },
      busy: {},
      push: { data: null, loading: false, busy: false },
      pushTimer: null,
      brain: {
        data: null, loading: false, logs: null, logsLoading: false, logChat: "",
        persona: null, tab: "overview", dirty: {}, scroll: {}
      }
    }
  };

  /* ---------------- 工具 ---------------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function enc(s) { return encodeURIComponent(s || ""); }

  function avatarHtml(url, name, size) {
    size = size || "";
    var letter = (String(name || "").trim() || "?").charAt(0);
    if (url) {
      return '<div class="li-avatar" style="' + (size ? "width:" + size + ";height:" + size + ";" : "") +
        '"><img src="' + esc(url) + '" alt="" onerror="this.parentNode.textContent=\'' +
        esc(letter) + '\'"></div>';
    }
    return '<div class="li-avatar" style="' + (size ? "width:" + size + ";height:" + size + ";" : "") +
      '">' + esc(letter) + "</div>";
  }

  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.classList.remove("show"); }, 2600);
  }

  function api(path, opts) {
    opts = opts || {};
    var url = path + (path.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
    var init = { method: opts.method || "GET", headers: {} };
    var tk = sessionStorage.getItem(TOKEN_KEY);
    if (tk) init.headers["Authorization"] = "Bearer " + tk;
    if (opts.body) { init.body = opts.body; init.headers["Content-Type"] = "application/json"; }
    return fetch(url, init).then(function (r) {
      if (r.status === 401) { showLogin(); throw new Error("未登录"); }
      if (!r.ok) {
        if (opts.detail) {
          return r.json().catch(function () { return {}; }).then(function (d) {
            throw new Error(d.error || ("HTTP " + r.status));
          });
        }
        throw new Error("HTTP " + r.status);
      }
      return r.json().catch(function () { return {}; });
    });
  }

  function loadMedia(filename, img) {
    if (!filename) return;
    fetch("/media/" + enc(filename) + "?t=" + Date.now(), {
      headers: (function () {
        var h = {}; var tk = sessionStorage.getItem(TOKEN_KEY);
        if (tk) h["Authorization"] = "Bearer " + tk; return h;
      })()
    }).then(function (r) {
      if (!r.ok) throw new Error("img");
      return r.blob();
    }).then(function (b) {
      var u = URL.createObjectURL(b);
      img.src = u;
      img.onclick = function () { zoomImage(u); };
    }).catch(function () {
      var ph = document.createElement("div");
      ph.className = "media-fail";
      ph.textContent = "图片未采集";
      if (img.parentNode) img.parentNode.replaceChild(ph, img);
    });
  }

  function zoomImage(url) {
    var z = document.createElement("div");
    z.className = "img-zoom";
    var im = document.createElement("img");
    im.src = url;
    z.appendChild(im);
    z.onclick = function () { URL.revokeObjectURL(url); document.body.removeChild(z); };
    document.body.appendChild(z);
  }

  /* ---------------- 登录 ---------------- */
  function showLogin() { $("login").classList.remove("hidden"); }
  function hideLogin() { $("login").classList.add("hidden"); }

  function ensureAuth() {
    var q = new URLSearchParams(location.search).get("token");
    if (q) sessionStorage.setItem(TOKEN_KEY, q);
    var t = sessionStorage.getItem(TOKEN_KEY);
    if (t) {
      return api("/api/social/state").then(function () { return true; }).catch(function () { return false; });
    }
    showLogin();
    return Promise.resolve(false);
  }

  function doLogin() {
    var user = $("loginUser").value.trim();
    var pass = $("loginPass").value.trim();
    var err = $("loginErr");
    err.textContent = "";
    if (!user || !pass) { err.textContent = "请输入账号和密码"; return; }
    api("/api/login", { method: "POST", body: JSON.stringify({ username: user, password: pass }) })
      .then(function (r) {
        sessionStorage.setItem(TOKEN_KEY, r.token); hideLogin(); boot();
      }).catch(function (e) {
        err.textContent = (e.message === "HTTP 401") ? "账号或密码错误" : ("登录失败：" + e.message);
      });
  }

  /* ---------------- 导航 ---------------- */
  function setNav(nav) {
    state.nav = nav;
    state.chat.emojiOpen = false;
    document.querySelectorAll(".rail-btn[data-nav]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.nav === nav);
    });
    $("app").classList.toggle("nav-moments", nav === "moments");
    $("app").classList.toggle("nav-apps", nav === "apps");
    $("contactTabs").classList.toggle("hidden", nav !== "contacts");
    $("searchInput").placeholder = nav === "chat" ? "搜索会话"
      : nav === "contacts" ? "搜索好友 / 群 / 公众号"
        : nav === "apps" ? "应用" : "搜索朋友圈";
    if (state.search) { state.search = ""; $("searchInput").value = ""; }
    // 离开聊天页就停掉对话轮询，回到聊天页由 renderChat 自行重建
    if (nav !== "chat" && state.chat.timer) {
      clearInterval(state.chat.timer); state.chat.timer = null;
    }
    if (nav !== "apps" && state.apps.timer) {
      clearInterval(state.apps.timer); state.apps.timer = null;
    }
    if (nav !== "apps" && state.apps.pushTimer) {
      clearInterval(state.apps.pushTimer); state.apps.pushTimer = null;
    }
    renderList();
    renderMain();
    if (nav === "apps") startReminderPoll();
  }

  function setContactTab(tab) {
    state.contactTab = tab;
    document.querySelectorAll("#contactTabs button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.ctab === tab);
    });
    renderList();
  }

  /* ---------------- 列表渲染 ---------------- */
  function renderList() {
    var body = $("listBody");
    if (state.nav === "chat") return renderConversations(body);
    if (state.nav === "contacts") return renderContacts(body);
    body.innerHTML = "";
  }

  function renderConversations(body) {
    var q = state.search.trim().toLowerCase();
    var items = state.conversations.filter(function (c) {
      if (!q) return true;
      return (c.name + " " + (c.last_preview || "")).toLowerCase().indexOf(q) >= 0;
    });
    if (!items.length) { body.innerHTML = '<div class="empty-list">暂无会话</div>'; return; }
    body.innerHTML = items.map(function (c) {
      var active = state.current === c.chat_id ? " active" : "";
      var tag = c.source_kind === "group" ? '<span class="li-tag group">群</span>'
        : c.source_kind === "official" ? '<span class="li-tag official">公众号</span>' : "";
      return '<div class="li' + active + '" data-chat="' + esc(c.chat_id) + '">' +
        avatarHtml(c.avatar_url, c.name) +
        '<div class="li-body"><div class="li-row"><span class="li-name">' + esc(c.name) +
        tag + '</span><span class="li-time">' + esc(c.last_at || "") + '</span></div>' +
        '<div class="li-preview">' + esc(c.last_preview || "") + '</div></div></div>';
    }).join("");
  }

  function renderContacts(body) {
    var tab = state.contactTab;
    var list = state.contacts[tab];
    if (!state.contactsLoaded[tab]) { loadContacts(tab); }
    var q = state.search.trim().toLowerCase();
    var items = (list || []).filter(function (r) {
      if (!q) return true;
      return (r.name + " " + (r.remark || "") + " " + (r.alias || "") + " " + (r.wxid || r.chat_id || ""))
        .toLowerCase().indexOf(q) >= 0;
    });
    if (!list) { body.innerHTML = '<div class="empty-list">加载中…</div>'; return; }
    if (!items.length) { body.innerHTML = '<div class="empty-list">暂无' +
      (tab === "friend" ? "好友" : tab === "group" ? "群聊" : "公众号") + '数据</div>'; return; }
    body.innerHTML = items.map(function (r) {
      var id = r.chat_id || r.wxid || r.room;
      var active = state.current === id ? " active" : "";
      var avatar = r.avatar_url || r.avatar_url_local || "";
      var name = r.name || r.nickname || r.remark || id;
      var sub = r.remark && r.remark !== name ? r.remark
        : r.alias ? "微信号：" + r.alias : "";
      var tag = "";
      return '<div class="li' + active + '" data-chat="' + esc(id) + '">' +
        avatarHtml(avatar, name) +
        '<div class="li-body"><div class="li-row"><span class="li-name">' + esc(name) +
        tag + '</span></div>' +
        (sub ? '<div class="li-preview">' + esc(sub) + '</div>' : '') + '</div></div>';
    }).join("");
  }

  /* ---------------- 主区渲染 ---------------- */
  function renderMain() {
    var main = $("main");
    if (state.nav === "moments") return renderMoments(main);
    if (state.nav === "apps") return renderApps(main);
    if (state.current) return renderChat(main);
    main.innerHTML = '<div class="empty-state"><div class="empty-logo">💬</div>' +
      '<p>选择一个' + (state.nav === "contacts" ? "联系人" : "会话") + '开始查看</p></div>';
  }

  function resolveChatMeta(chatId) {
    if (!chatId) return null;
    var i, list;
    list = state.conversations || [];
    for (i = 0; i < list.length; i++) {
      if (list[i].chat_id === chatId)
        return { name: list[i].name, kind: list[i].source_kind, avatar: list[i].avatar_url };
    }
    var tabs = ["friend", "group", "official"];
    for (var t = 0; t < tabs.length; t++) {
      list = state.contacts[tabs[t]] || [];
      for (i = 0; i < list.length; i++) {
        var id = list[i].chat_id || list[i].wxid || list[i].room;
        if (id === chatId) {
          var nm = list[i].name || list[i].nickname || list[i].remark || chatId;
          var av = list[i].avatar_url || list[i].avatar_url_local || "";
          var kd = tabs[t] === "group" ? "group" : tabs[t] === "official" ? "official" : "contact";
          return { name: nm, kind: kd, avatar: av };
        }
      }
    }
    return null;
  }

  function renderChat(main) {
    var c = state.chat;
    var th = c.thread || {};
    var meta = resolveChatMeta(state.current);
    var name = (meta && meta.name) || th.chat_name || state.current;
    var kind = (meta && meta.kind) || th.source_kind || "";
    var avatar = (meta && meta.avatar) || th.chat_avatar || "";
    var sub = kind === "group" ? ("群聊 · " + (th.total || th.count || 0) + " 条消息")
      : kind === "official" ? "公众号"
      : kind === "contact" ? "好友"
      : (th.source_kind === "official" ? "公众号" : "好友");
    var downgrade = th.downgrade ? '<span class="li-tag" style="background:#fa9d3b">降级模式</span>' : "";
    main.innerHTML =
      '<div class="chat-wrap">' +
      '<div class="chat-head"><div class="head-left">' +
      '<div class="head-avatar">' + avatarHtml(avatar, name, "40px") + '</div>' +
      '<div><h2>' + esc(name) + " " + downgrade + '</h2>' +
      '<div class="head-sub">' + esc(sub) + '</div></div></div>' +
      '<div class="head-actions"><button class="btn" data-act="chat-info">详情</button></div></div>' +
      '<div class="chat-body" id="chatBody"></div>' +
      '<div class="chat-composer">' +
      (c.emojiOpen ? '<div class="emoji-panel" id="emojiPanel">' +
        EMOJIS.map(function (e) { return '<button type="button" data-emoji="' + e + '">' + e + '</button>'; }).join("") +
        '</div>' : "") +
      '<div class="composer-tools"><button class="tool" data-act="chat-emoji" title="表情">😊</button>' +
      '<button class="tool" data-act="chat-image" title="图片（即将开放）" disabled>🖼️</button>' +
      '<span class="spacer"></span></div>' +
      '<div class="composer-row"><textarea id="chatInput" placeholder="输入消息，Enter 发送，Shift+Enter 换行"></textarea>' +
      '<button class="btn primary" id="btnChatSend">发送</button></div>' +
      '</div></div>';
    var box = $("chatInput");
    if (box) box.value = c.draft;
    renderBubbles();
    if (box) box.focus();
    if (!c.timer) {
      c.timer = setInterval(function () { if (state.current) loadThread(false); }, 30000);
    }
    loadThread(true);
  }

  function updateChatHead() {
    var head = document.querySelector(".chat-head");
    if (!head) return;
    var left = head.querySelector(".head-left");
    if (!left) return;
    var meta = resolveChatMeta(state.current);
    var th = state.chat.thread || {};
    var name = (meta && meta.name) || th.chat_name || state.current;
    var kind = (meta && meta.kind) || th.source_kind || "";
    var avatar = (meta && meta.avatar) || th.chat_avatar || "";
    var sub = kind === "group" ? ("群聊 · " + (th.total || th.count || 0) + " 条消息")
      : kind === "official" ? "公众号"
      : kind === "contact" ? "好友"
      : (th.source_kind === "official" ? "公众号" : "好友");
    var downgrade = th.downgrade ? '<span class="li-tag" style="background:#fa9d3b">降级模式</span>' : "";
    left.innerHTML = '<div class="head-avatar">' + avatarHtml(avatar, name, "40px") + '</div>' +
      '<div><h2>' + esc(name) + " " + downgrade + '</h2>' +
      '<div class="head-sub">' + esc(sub) + '</div></div>';
  }

  function bubbleHtml(x) {
    var av = avatarHtml(x.avatar_url, x.sender || x.chat_name, "38px");
    var inner;
    var media = x.media || x.image_filename;
    if (x.kind === "image" && media) {
      inner = '<img class="media" alt="图片" data-media="' + esc(media) + '">';
    } else if (x.kind === "link" || x.kind === "official") {
      inner = '<div class="link-title">' + esc(x.title || "链接消息") + '</div>' +
        (x.description ? '<div class="link-desc">' + esc(x.description) + '</div>' : "") +
        (x.url ? '<div class="link-url">' + esc(x.url) + '</div>' : "");
    } else {
      inner = esc(x.body || x.title || "");
    }
    var status = "";
    if (x.self) {
      var cls = "s-sending", txt = "发送中";
      if (x.status === 2) { cls = ""; txt = "已发送"; }
      else if (x.status === 3) { cls = "s-fail"; txt = (x.error || "发送失败"); }
      status = '<div class="bubble-status ' + cls + '">' + esc(txt) + '</div>';
    }
    return '<div class="bubble-row' + (x.self ? " self" : "") + '" data-key="' + esc(x.key) + '">' +
      av + '<div class="bubble-col">' +
      (x.self ? "" : '<div class="bubble-name">' + esc(x.sender || "") + '</div>') +
      '<div class="bubble">' + inner + '</div>' + status + '</div></div>';
  }

  function renderBubbles() {
    var body = $("chatBody");
    if (!body) return;
    var th = state.chat.thread;
    if (!th || !th.items || !th.items.length) {
      body.innerHTML = '<div class="empty-list">暂无采集到的消息' +
        (th && th.source_kind === "group" ? '（该群可能在微信中尚未打开过）' : '') + '</div>';
      return;
    }
    var html = "", lastDay = "";
    th.items.forEach(function (x) {
      var day = (x.time || "").slice(0, 10);
      if (day && day !== lastDay) { html += '<div class="chat-day"><span>' + esc(day) + '</span></div>'; lastDay = day; }
      html += bubbleHtml(x);
    });
    body.innerHTML = html;
    body.querySelectorAll("img.media").forEach(function (img) {
      loadMedia(img.getAttribute("data-media"), img);
    });
  }

  function renderMoments(main) {
    var m = state.moments;
    if (!m) { main.innerHTML = '<div class="empty-list">加载中…</div>'; return; }
    var rows = m.moments || [];
    var head = '<div class="moments-wrap"><div class="moments-head"><h2>朋友圈</h2>' +
      '<button class="btn" data-act="moments-refresh">刷新</button></div>' +
      '<div class="moments-body" id="momentsBody">';
    if (m.status !== "ok") {
      main.innerHTML = head + '<div class="empty-list">朋友圈暂未读取：' +
        esc(m.error || m.note || "请先在电脑微信打开朋友圈，再点刷新") + '</div></div></div>';
      return;
    }
    if (!rows.length) { main.innerHTML = head + '<div class="empty-list">暂无可显示的朋友圈</div></div></div>'; return; }
    var body = rows.map(function (r) {
      var av = avatarHtml(r.avatar_url_local, r.nickname || r.author, "40px");
      var content = r.content || r.text || "";
      var media = r.media_count > 0 ? '<div class="moment-media">含 ' + r.media_count + ' 张图片（暂不支持预览）</div>' : "";
      var foot = '<div class="moment-foot"><span>👍 ' + (r.likes_total || 0) + '</span>' +
        '<span>💬 ' + (r.comments_total || 0) + '</span>' +
        (r.private ? '<span>仅自己可见</span>' : "") + '</div>';
      return '<div class="moment">' + '<div class="moment-head">' + av +
        '<div><div class="m-name">' + esc(r.nickname || r.author || "微信用户") + '</div>' +
        '<div class="m-time">' + esc((r.time || "").replace("T", " ").slice(0, 16)) + '</div></div></div>' +
        (content ? '<div class="moment-content">' + esc(content) + '</div>' : "") + media + foot + '</div>';
    }).join("");
    main.innerHTML = head + body + '</div></div>';
  }

  /* ================= 应用：定时提醒 ================= */
  function reminderQuery() {
    var f = state.apps.filters;
    return "/api/reminders?limit=500" +
      "&status=" + enc(f.status) + "&kind=" + enc(f.kind) +
      "&delivery=" + enc(f.delivery) + "&q=" + enc(f.q);
  }

  function loadReminders(silent) {
    var a = state.apps;
    if (a.loading) return;
    a.loading = true;
    api(reminderQuery()).then(function (d) {
      a.data = d;
      a.error = "";
      if (state.nav !== "apps") return;
      if (a.view === "home") updateAppMeta();
      else { renderReminderSummary(); renderReminderList(); }
    }).catch(function (e) {
      a.error = e.message || "读取失败";
      if (state.nav === "apps" && a.view === "reminders") {
        var box = $("reminderList");
        var tip = a.error === "HTTP 404"
          ? "管理台进程仍是旧版本：请重启 admin/app.py 后再刷新（网页静态文件已更新，接口需重启才生效）。"
          : "请确认管理台已重启并加载了新接口。";
        if (box) box.innerHTML = '<div class="empty-list">读取提醒任务失败：' +
          esc(a.error) + "<br>" + esc(tip) + "</div>";
      }
    }).then(function () { a.loading = false; });
  }

  function startReminderPoll() {
    var a = state.apps;
    if (a.timer) clearInterval(a.timer);
    a.timer = setInterval(function () {
      if (state.nav !== "apps") {
        clearInterval(a.timer); a.timer = null; return;
      }
      loadReminders(true);
    }, 15000);
    if (!a.data) loadReminders(false);
    if (a.pushTimer) clearInterval(a.pushTimer);
    a.pushTimer = setInterval(function () {
      if (state.nav !== "apps") {
        clearInterval(a.pushTimer); a.pushTimer = null; return;
      }
      loadArticlePush(true);
    }, 5000);
    if (!a.push.data) loadArticlePush(false);
    if (!a.brain.data) loadBrain(false);
  }

  function loadBrain(silent) {
    var b = state.apps.brain;
    if (b.loading) return;
    b.loading = true;
    var out = {};
    var done = function () {
      b.data = out; b.loading = false;
      if (state.nav !== "apps") return;
      if (state.apps.view === "brain") {
        fillBrainConfig(out.llm || null);
        fillBrainRuntime(out.runtime || null);
        fillBrainGroups(out.groups || null);
        fillBrainPersona(out.persona || null);
        fillBrainTuning(out.tuning || null);
        // runtime 自带心跳，先刷一次状态条，不等日志接口返回
        renderBrainStatus();
      } else {
        updateAppMeta();
      }
    };
    api("/api/brain/runtime").then(function (r) { out.runtime = r; }).catch(function () {})
      .then(function () { return api("/api/brain/llm"); })
      .then(function (l) { out.llm = l; }).catch(function () {})
      .then(function () { return api("/api/brain/groups"); })
      .then(function (g) { out.groups = g; }).catch(function () {})
      .then(function () { return api("/api/brain/persona"); })
      .then(function (p) { out.persona = p; }).catch(function () {})
      .then(function () { return api("/api/brain/tuning"); })
      .then(function (t) { out.tuning = t; }).catch(function () {})
      .then(done).catch(done);
  }

  function updateAppMeta() {
    var el = $("appReminderMeta");
    if (el) {
      var d = state.apps.data;
      if (!d) el.textContent = "读取中…";
      else el.textContent = "进行中 " + (d.counts.active || 0) + " · 全部 " +
        (d.counts.all || 0) + " 个任务" + (d.worker ? "" : " · 发送模块未就绪");
    }
    var pel = $("appPushMeta");
    if (pel) {
      var p = state.apps.push.data;
      if (!p) { pel.textContent = "读取中…"; }
      else {
        var s = p.stats || {}, c = p.config || {};
        pel.textContent = (c.enabled ? "已启用" : "未启用") + " · 累计成功 " +
          (s.success || 0) + " 次" + (s.pending ? " · 待推 " + s.pending + " 批" : "") +
          (s.failed_batches ? " · 失败 " + s.failed_batches + " 批" : "");
      }
    }
    var bel = $("appBrainMeta");
    if (bel) {
      var bd = state.apps.brain.data;
      if (!bd) { bel.textContent = "读取中…"; }
      else {
        var rt = bd.runtime || {}, gg = bd.groups || {}, ll = bd.llm || {};
        var lg = state.apps.brain.logs || {};
        bel.textContent = (rt.enabled ? "已启用" : "已停用") +
          (rt.dry_run ? " · 演练中" : "") +
          " · 接管 " + (gg.selected || []).length + " 个群" +
          (lg.summary && lg.summary.total ? " · 日志 " + lg.summary.total + " 条" : "") +
          (ll.configured ? "" : " · 未配置模型");
      }
    }
  }

  function renderApps(main) {
    if (state.apps.view === "reminders") return renderRemindersPage(main);
    if (state.apps.view === "article-push") return renderArticlePushPage(main);
    if (state.apps.view === "brain") return renderBrainPage(main);
    var d = state.apps.data;
    var hint = d && d.hint ? '<div class="apps-warn">⚠️ ' + esc(d.hint) + "</div>" : "";
    main.innerHTML =
      '<div class="apps-wrap"><div class="apps-head">' +
      '<div><h2>应用</h2><div class="apps-sub">基于本地微信数据的自动化能力</div></div>' +
      '<button class="btn" data-act="apps-refresh">刷新</button></div>' +
      '<div class="apps-body">' + hint +
      '<div class="section-title">已上线</div><div class="app-grid">' +
      '<div class="app-card" data-app="reminders">' +
      '<div class="app-icon">⏰</div><div class="app-info">' +
      "<h3>定时提醒</h3>" +
      "<p>在群里 @ 机器人即可设置提醒；支持口语化时间、一次性与循环、多人同时 @、违禁内容拦截。</p>" +
      '<div class="app-meta" id="appReminderMeta">读取中…</div></div>' +
      '<div class="app-arrow">›</div></div>' +
      '<div class="app-card" data-app="article-push">' +
      '<div class="app-icon">🔗</div><div class="app-info">' +
      "<h3>文章链接推送</h3>" +
      "<p>收到公众号推送后，把文章链接以数组形式 POST 到指定 URL；地址、参数名、令牌都在这里配置。</p>" +
      '<div class="app-meta" id="appPushMeta">读取中…</div></div>' +
      '<div class="app-arrow">›</div></div>' +
      '<div class="app-card" data-app="brain">' +
      '<div class="app-icon">🐋</div><div class="app-info">' +
      "<h3>拟人大脑</h3>" +
      "<p>让机器人像真人一样在群里接话。只有勾选的群会生效；可配置大模型、运行开关、长期记忆与拟人策略。</p>" +
      '<div class="app-meta" id="appBrainMeta">读取中…</div></div>' +
      '<div class="app-arrow">›</div></div>' +
      '<div class="app-card disabled"><div class="app-icon">🧩</div>' +
      '<div class="app-info"><h3>更多应用</h3><p>后续能力会陆续放到这里。</p>' +
      '<div class="app-meta">规划中</div></div></div></div>' +
      '<div class="apps-tip"><b>怎么用：</b>群里发送 <code>@机器人 提醒 30分钟后 交周报</code>' +
      '（可同时 @ 其他人，到期一起提醒），也可以直接私聊机器人发 <code>提醒 30分钟后 交周报</code>。<br>' +
      '<b>在哪发的就在哪回：</b>群里发起的到点在原群回复并 @；私聊发起的到点私信回复本会话，不带 @。<br>' +
      '支持每 5 分钟 / 每小时 / 每天 9点 / 每周一 10点 等循环；<b>5 分钟下限只限制循环两次触发的间隔</b>，' +
      '一次性提醒「3分钟后」也照常支持。<br>' +
      '<b>文章链接推送：</b>进入应用填好接收 URL 并启用即可，请求体只有一个链接数组字段，' +
      '参数说明和发送日志都在该应用页内。</div>' +
      "</div></div>";
    updateAppMeta();
  }

  function renderRemindersPage(main) {
    var f = state.apps.filters;
    var opt = function (value, label, cur) {
      return '<option value="' + esc(value) + '"' + (value === cur ? " selected" : "") +
        ">" + esc(label) + "</option>";
    };
    main.innerHTML =
      '<div class="rem-wrap"><div class="rem-head">' +
      '<button class="btn" data-act="apps-back">‹ 返回应用</button>' +
      '<div class="rem-title"><h2>定时提醒</h2><div id="remHint" class="rem-hint"></div></div>' +
      '<button class="btn" data-act="rem-refresh">刷新</button></div>' +
      '<div class="rem-filters">' +
      '<select id="remStatus">' + opt("", "全部状态", f.status) +
      opt("active", "进行中", f.status) + opt("paused", "已停止", f.status) +
      opt("done", "已完成", f.status) + opt("error", "异常", f.status) + "</select>" +
      '<select id="remKind">' + opt("", "全部类型", f.kind) +
      opt("once", "一次性", f.kind) + opt("interval", "间隔循环", f.kind) +
      opt("daily", "每天", f.kind) + opt("weekly", "每周", f.kind) + "</select>" +
      '<select id="remDelivery">' + opt("", "全部投递", f.delivery) +
      opt("group", "群里@", f.delivery) + opt("dm", "私信", f.delivery) + "</select>" +
      '<input id="remSearch" type="search" placeholder="搜索内容 / 群 / 发起人 / 原文" value="' +
      esc(f.q) + '"><button class="btn" data-act="rem-clear">重置</button></div>' +
      '<div id="remSummary" class="rem-summary"></div>' +
      '<div id="reminderList" class="rem-list"></div></div>';
    renderReminderSummary();
    renderReminderList();
  }

  function renderReminderSummary() {
    var d = state.apps.data;
    var hint = $("remHint"), sum = $("remSummary");
    if (!d) {
      if (hint) hint.textContent = state.apps.error || "读取中…";
      if (sum) sum.textContent = "";
      return;
    }
    if (hint) {
      hint.textContent = d.hint || ("引擎已加载 · 发送模块" +
        (d.worker ? "在线" : "离线") + " · 共 " + (d.counts.all || 0) + " 条任务");
      hint.className = "rem-hint" + (d.hint ? " warn" : "");
    }
    if (sum) {
      var c = d.counts || {};
      sum.innerHTML =
        '<span>全部 <b>' + (c.all || 0) + "</b></span>" +
        '<span class="ok">进行中 <b>' + (c.active || 0) + "</b></span>" +
        '<span>已停止 <b>' + (c.paused || 0) + "</b></span>" +
        '<span>已完成 <b>' + (c.done || 0) + "</b></span>" +
        '<span class="spacer"></span><span>当前显示 <b>' + (d.total || 0) + "</b> 条</span>";
    }
  }

  function countdownText(r) {
    if (r.status !== "active") return "";
    if (r.overdue) return "已到期，等待发送模块执行";
    var s = r.countdown_sec || 0;
    if (s < 60) return s + " 秒后触发";
    if (s < 3600) return Math.floor(s / 60) + " 分钟后触发";
    if (s < 86400) return Math.floor(s / 3600) + " 小时 " +
      Math.floor((s % 3600) / 60) + " 分后触发";
    return Math.floor(s / 86400) + " 天 " + Math.floor((s % 86400) / 3600) + " 小时后触发";
  }

  function remKv(label, value, cls) {
    return '<div class="rem-kv-item' + (cls ? " " + cls : "") + '"><dt>' +
      esc(label) + "</dt><dd>" + esc(value == null || value === "" ? "—" : value) + "</dd></div>";
  }

  function reminderCard(r) {
    var busy = !!state.apps.busy[r.id];
    var st = r.status || "active";
    var badge = '<span class="rem-badge st-' + esc(st) + '">' + esc(r.status_label || st) + "</span>";
    var targets = (r.target_names_arr && r.target_names_arr.length)
      ? r.target_names_arr.join("、") : (r.targets || "—");
    var rule = [r.kind_label, r.rule_desc].filter(Boolean).join(" · ");
    var ruleExtra = [];
    if (r.interval_sec) ruleExtra.push("间隔 " + r.interval_sec + " 秒");
    if (r.weekdays) ruleExtra.push("周 " + r.weekdays);
    if (r.at_hhmm) ruleExtra.push("时刻 " + r.at_hhmm);
    var actionMain = (st === "active")
      ? '<button class="btn" data-ract="stop" data-rid="' + r.id + '"' + (busy ? " disabled" : "") + ">停止</button>"
      : '<button class="btn" data-ract="resume" data-rid="' + r.id + '"' + (busy ? " disabled" : "") + ">恢复</button>";
    return '<div class="rem-card" data-rid="' + r.id + '">' +
      '<div class="rem-card-top"><div class="rem-card-id">#' + r.id + " " + badge +
      '<span class="rem-chip">' + esc(r.delivery_label || "") + "</span></div>" +
      '<div class="rem-card-rule">' + esc(rule || "—") + "</div></div>" +
      '<div class="rem-content">' + esc(r.content || "") + "</div>" +
      '<div class="rem-next">' + (r.next_at ? "下次触发：" + esc(r.next_at) : "无下次触发") +
      (countdownText(r) ? ' <span class="rem-countdown">' + esc(countdownText(r)) + "</span>" : "") +
      "</div>" +
      '<dl class="rem-kv">' +
      remKv("来源会话", (r.chat_display || r.chat_name || r.chat_id || "—") +
        (r.chat_id ? "（" + r.chat_id + "）" : "")) +
      remKv("发起人", (r.creator_name || r.creator_id || "—") +
        (r.creator_id && r.creator_name ? "（" + r.creator_id + "）" : "")) +
      remKv("提醒对象", targets) +
      remKv("对象 wxid", r.targets || "—") +
      remKv("触发规则", r.rule_text || r.rule_desc || "—") +
      remKv("规则参数", ruleExtra.length ? ruleExtra.join(" · ") : "—") +
      remKv("上次触发", r.last_fire_at || "从未") +
      remKv("已触发次数", r.fire_count || 0) +
      remKv("投递方式", (r.delivery_label || "") +
        (r.delivery === "dm" ? " → 原会话 " + (r.dm_to || r.chat_id || "—") : "")) +
      remKv("创建时间", r.created_at_fmt || "—") +
      remKv("更新时间", r.updated_at_fmt || "—") +
      remKv("来源 / 状态", (r.source || "—") + " / " + (r.status_label || st)) +
      remKv("错误信息", r.last_error || "无", r.last_error ? "bad" : "") +
      "</dl>" +
      '<details class="rem-raw"><summary>原始指令与技术字段</summary><pre>' +
      esc("原文：" + (r.raw_text || "—") + "\nrule_json：" + (r.rule_json || "{}") +
        "\nchat_id：" + (r.chat_id || "") + "\ncreator_id：" + (r.creator_id || "") +
        "\ntargets：" + (r.targets || "") + "\ntarget_names：" + (r.target_names || "") +
        "\nnext_ts：" + (r.next_ts || 0) + "\ncreated_ts：" + (r.created_ts || 0)) +
      "</pre></details>" +
      '<div class="rem-actions">' + actionMain +
      '<button class="btn primary" data-ract="send" data-rid="' + r.id + '"' +
      (busy ? " disabled" : "") + ">立即发送</button>" +
      '<button class="btn danger" data-ract="delete" data-rid="' + r.id + '"' +
      (busy ? " disabled" : "") + ">删除</button></div></div>";
  }

  function renderReminderList() {
    var box = $("reminderList");
    if (!box) return;
    var a = state.apps, d = a.data;
    if (!d) {
      box.innerHTML = '<div class="empty-list">' +
        esc(a.error ? "读取失败：" + a.error : "加载中…") + "</div>";
      return;
    }
    if (!(d.items || []).length) {
      box.innerHTML = '<div class="empty-list">没有符合条件的提醒任务<br>' +
        "在群里 @ 机器人发送「提醒 30分钟后 交周报」即可创建一条。</div>";
      return;
    }
    box.innerHTML = d.items.map(reminderCard).join("");
  }

  function openReminderApp() {
    state.apps.view = "reminders";
    renderApps($("main"));
    loadReminders(false);
  }

  function doReminderAction(id, action) {
    var a = state.apps;
    if (a.busy[id]) return;
    if (action === "delete" && !window.confirm("确定删除提醒任务 #" + id + "？此操作不可撤销。")) return;
    a.busy[id] = true;
    renderReminderList();
    api("/api/reminders/action", {
      method: "POST", body: JSON.stringify({ id: Number(id), action: action })
    }).then(function (r) {
      if (!r.ok) throw new Error(r.error || "操作失败");
      toast(r.message || "操作成功");
      if (r.warning) setTimeout(function () { toast(r.warning); }, 1200);
    }).catch(function (e) {
      toast("操作失败：" + e.message);
    }).then(function () {
      delete a.busy[id];
      loadReminders(true);
    });
  }

  /* ================= 应用：文章链接推送 ================= */
  function apValue(id) { var el = $(id); return el ? String(el.value || "") : ""; }

  function apPayload() {
    var p = state.apps.push;
    return {
      url: apValue("apUrl").trim(),
      param_name: apValue("apParam").trim() || "urls",
      mode: apValue("apMode") || "message",
      enabled: !!($("apEnabled") && $("apEnabled").checked),
      include_shared: !!($("apShared") && $("apShared").checked),
      timeout_seconds: Number(apValue("apTimeout") || 10),
      token: apValue("apToken").trim(),
      clear_token: !!p.clearToken
    };
  }

  function fillArticlePushForm(cfg) {
    if (!cfg || state.apps.push.formDirty) return;
    var set = function (id, v) { var el = $(id); if (el) el.value = v == null ? "" : v; };
    set("apUrl", cfg.url); set("apParam", cfg.param_name);
    set("apMode", cfg.mode); set("apTimeout", cfg.timeout_seconds);
    set("apToken", "");
    var en = $("apEnabled"); if (en) en.checked = !!cfg.enabled;
    var sh = $("apShared"); if (sh) sh.checked = !!cfg.include_shared;
    var tk = $("apToken");
    if (tk) tk.placeholder = cfg.token_set ? ("已设置（" + cfg.token_hint + "），留空保持不变") : "可选，留空表示不带头";
    state.apps.push.formFilled = true;
  }

  function loadArticlePush(silent) {
    var p = state.apps.push;
    if (p.loading) return;
    p.loading = true;
    api("/api/article-push").then(function (d) {
      p.data = d; p.error = "";
      updateAppMeta();
      if (state.nav === "apps" && state.apps.view === "article-push") {
        if (!p.formFilled) fillArticlePushForm(d.config);
        renderArticlePushStatus();
        renderArticlePushLogs();
      }
    }).catch(function (e) {
      p.error = e.message || "读取失败";
      updateAppMeta();
      var pel = $("appPushMeta");
      if (pel) pel.textContent = p.error === "HTTP 404"
        ? "接口未就绪 · 需重启 8088 管理台" : "状态读取失败";
      var box = $("apStatus");
      if (box) box.innerHTML = '<div class="empty-list">读取状态失败：' + esc(p.error) +
        "<br>请确认 8088 管理台已重启并加载了新接口。</div>";
    }).then(function () { p.loading = false; });
  }

  function openArticlePushApp() {
    state.apps.view = "article-push";
    state.apps.push.formFilled = false;
    state.apps.push.formDirty = false;
    state.apps.push.clearToken = false;
    renderApps($("main"));
    loadArticlePush(false);
  }

  function openBrainApp() {
    state.apps.view = "brain";
    state.apps.brain.data = null;
    state.apps.brain.logs = null;
    state.apps.brain.logChat = "";
    // 进页面就重新从服务端拉数据，之前的未保存修改必然丢失，脏标记一起清掉
    brainClearDirty();
    renderApps($("main"));
    loadBrain(false);
    loadBrainLogs(false);
  }

  /* ---------------- 拟人大脑 · 决策日志 ---------------- */
  var BRAIN_REASONS = {
    self: "自己发的消息",
    empty: "空消息",
    at_mentioned: "被 @ 到",
    quiet: "处于安静时段",
    cooldown: "冷却中：距上次回复不足设定间隔（默认 90 秒，可在「接话节奏」改）",
    cooldown_follow: "跟进冷却中：刚在跟进窗口里说过话，等几秒再接",
    rate_limit: "10 分钟内次数已满",
    probability_miss: "概率未命中（正常跳过）",
    no_llm: "未配置模型，无法生成回复",
    forced: "手动强制回复"
  };

  // 决策 → 徽章样式与文案
  function brainDecisionBadge(decision) {
    var m = {
      at_reply: ["已回复", "ok"],
      trigger: ["已回复", "ok"],
      probability: ["已回复", "ok"],
      dry_run: ["演练回复", "dry"],
      quiet_hours: ["安静时段", "warn"],
      cooldown: ["冷却中", "warn"],
      rate_limit: ["超频跳过", "warn"],
      skip: ["跳过", "mute"]
    };
    var pair = m[decision] || [decision || "—", "mute"];
    return '<span class="brain-badge ' + pair[1] + '">' + esc(pair[0]) + "</span>";
  }

  // reason 翻成人话，让「为什么没回」一眼看懂
  function brainReasonText(reason) {
    var r = String(reason || "");
    if (!r) return "—";
    if (r.indexOf("word:") === 0) return "命中关键词「" + r.slice(5) + "」";
    if (r.indexOf("llm_error:") === 0) return "模型报错：" + r.slice(10);
    if (r.indexOf("follow p=") === 0) {
      var rest = r.slice(7).split(" turn=");
      return "跟进接话 概率 " + rest[0] + (rest[1] ? "（本段第 " + rest[1] + " 句）" : "");
    }
    if (r.indexOf("p=") === 0) return "概率命中 " + r.slice(2);
    return BRAIN_REASONS[r] || r;
  }

  function loadBrainLogs(silent) {
    var b = state.apps.brain;
    if (b.logsLoading) return;
    b.logsLoading = true;
    var url = "/api/brain/logs?limit=100";
    if (b.logChat) url += "&chat=" + encodeURIComponent(b.logChat);
    api(url).then(function (d) {
      b.logs = d; b.logsLoading = false;
      renderBrainLogs();
      // 在应用宫格时，日志条数也要反映到卡片摘要上
      if (state.nav === "apps" && state.apps.view !== "brain") updateAppMeta();
    }).catch(function (err) {
      b.logsLoading = false;
      b.logs = { logs: [], summary: {}, engine: {}, groups: [], error: err && err.message };
      renderBrainLogs();
      if (!silent) toast("日志读取失败：" + ((err && err.message) || err));
    });
  }

  function renderBrainLogs() {
    var box = $("brainLogBox");
    if (!box) { renderBrainStatus(); return; }
    var d = state.apps.brain.logs;
    if (!d) { box.innerHTML = '<div class="empty-list">加载中…</div>'; renderBrainStatus(); return; }
    if (d.error) {
      box.innerHTML = '<div class="empty-list">读取失败：' + esc(d.error) + "</div>";
      renderBrainStatus();
      return;
    }
    var e = d.engine || {}, s = d.summary || {};
    var off = e.running_state && e.running_state.indexOf("总开关已关闭") === 0;
    var stateCls = off ? "mute" : (e.alive ? "ok" : "warn");
    var stateText = e.running_state || "未知";
    if (e.last_tick_ago != null) {
      stateText += " · 心跳 " + (e.last_tick_ago < 60
        ? e.last_tick_ago + " 秒前"
        : Math.round(e.last_tick_ago / 60) + " 分钟前");
    }
    // 群筛选
    var groups = d.groups || [];
    var opts = '<option value="">全部群聊</option>' + groups.map(function (g) {
      return '<option value="' + esc(g.chat_id) + '"' +
        (g.chat_id === state.apps.brain.logChat ? " selected" : "") + ">" +
        esc(g.name || g.chat_id) + "</option>";
    }).join("");
    var head = '<div class="brain-log-status">' +
      '<span class="brain-badge ' + stateCls + '">' + esc(stateText) + "</span>" +
      (e.dry_run ? '<span class="brain-badge dry">演练模式' +
        (e.llm_mode === "real" ? " · 真模型"
          : (e.llm_mode === "mock" ? " · 模拟文本"
            : (e.llm_mode === "none" ? " · 模型待命" : ""))) +
        "</span>" : "") +
      '<span class="brain-log-num">已回复 <b class="ok-text">' + (s.replied || 0) + "</b></span>" +
      '<span class="brain-log-num">演练 <b>' + (s.dry || 0) + "</b></span>" +
      '<span class="brain-log-num">跳过 <b>' + ((s.skipped || 0) + (s.limited || 0)) + "</b></span>" +
      '<span class="brain-log-num">共 ' + (s.total || 0) + " 条</span>" +
      "</div>";
    var tools = '<div class="brain-log-tools">' +
      '<select id="brainLogChat" class="brain-group-search">' + opts + "</select>" +
      '<button class="btn btn-sm" data-act="brain-log-refresh">刷新</button>' +
      '<button class="btn btn-sm" data-act="brain-log-clear">清空日志</button>' +
      "</div>";
    var logs = d.logs || [];
    if (!logs.length) {
      // 空态副文案：进程没起 / 总开关关着，两种原因分开说，别让用户去翻系统服务
      var emptyText = "还没有决策记录。大脑每扫到一条群消息都会记一条，" +
        "包括「为什么没回」。启用后保持演练模式跑一会儿就能看到。";
      if (e.alive === false) {
        emptyText = "启动大脑进程后，这里会显示决策记录。";
      } else if (e.alive && off) {
        emptyText = "总开关关闭中，不会回复任何消息；进程仍在待机。";
      }
      box.innerHTML = head + tools +
        '<div class="empty-list">' + esc(emptyText) + "</div>";
      renderBrainStatus();
      return;
    }
    var table = '<div class="brain-log-scroll"><table class="ap-table brain-log-table"><thead><tr>' +
      "<th>时间</th><th>所在群</th><th>发送者</th><th>消息</th><th>决策</th><th>原因</th><th>回复内容</th>" +
      "</tr></thead><tbody>" + logs.map(function (l) {
        return "<tr>" +
          "<td class=\"brain-log-time\">" + esc(l.at) + "</td>" +
          '<td class="brain-log-chat" title="' + esc(l.chat_id) + '">' + esc(l.chat_name) + "</td>" +
          "<td>" + esc(l.sender || "—") + "</td>" +
          '<td class="brain-log-text" title="' + esc(l.text) + '">' + esc(l.text || "—") + "</td>" +
          "<td>" + brainDecisionBadge(l.decision) + "</td>" +
          "<td>" + esc(brainReasonText(l.reason)) + "</td>" +
          '<td class="brain-log-reply" title="' + esc(l.reply) + '">' + esc(l.reply || "—") + "</td>" +
          "</tr>";
      }).join("") + "</tbody></table></div>";
    box.innerHTML = head + tools + table;
    // 状态区、页头徽章与日志 tab 条数跟着日志一起刷新
    renderBrainStatus();
  }

  function loadBrainRuntimeLog() {
    var pre = $("brainRtLog");
    if (!pre) return;
    pre.textContent = "读取中…";
    api("/api/brain/runtime-log?lines=150").then(function (r) {
      var lines = r.lines || [];
      pre.textContent = lines.length
        ? lines.join("")
        : "brain.log 还没有内容（大脑还没启动过，或启动后还没写日志）";
      pre.scrollTop = pre.scrollHeight;
    }).catch(function (err) {
      pre.textContent = "读取失败：" + ((err && err.message) || err);
    });
  }

  function clearBrainLogs() {
    var scope = state.apps.brain.logChat;
    var tip = scope ? "只清空当前筛选群" : "清空全部群";
    if (!confirm("确认" + tip + "的决策日志？这条操作不可恢复，但不会影响长期记忆和消息记录。")) return;
    api("/api/brain/logs/clear", { method: "POST", body: JSON.stringify({ chat_id: scope }), detail: true })
      .then(function (r) {
        toast(r.hint || "已清空");
        loadBrainLogs(true);
      }).catch(function (err) { toast("清空失败：" + ((err && err.message) || err)); });
  }

  function renderArticlePushPage(main) {
    var c = (state.apps.push.data && state.apps.push.data.config) || {};
    main.innerHTML =
      '<div class="rem-wrap"><div class="rem-head">' +
      '<button class="btn" data-act="apps-back">‹ 返回应用</button>' +
      '<div class="rem-title"><h2>文章链接推送</h2>' +
      '<div id="apHint" class="rem-hint">收到公众号推送后，把文章链接 POST 到指定 URL</div></div>' +
      '<button class="btn" data-act="ap-refresh">刷新</button></div>' +
      '<div class="ap-body">' +
      '<div class="ap-card"><h3>推送配置</h3><div class="ap-form">' +
      '<label class="ap-field wide"><span>接收 URL <i>必填</i></span>' +
      '<input id="apUrl" type="url" placeholder="https://example.com/api/wechat-articles" value="' +
      esc(c.url || "") + '"></label>' +
      '<label class="ap-field"><span>数组参数名</span>' +
      '<input id="apParam" type="text" placeholder="urls" value="' +
      esc(c.param_name || "urls") + '"></label>' +
      '<label class="ap-field"><span>一次 POST 的粒度</span><select id="apMode">' +
      '<option value="message">一条推送合并成一次（多篇文章放同一数组）</option>' +
      '<option value="article">每篇文章单独一次（数组恒为 1 个链接）</option>' +
      '</select></label>' +
      '<label class="ap-field"><span>超时（秒）</span>' +
      '<input id="apTimeout" type="number" min="1" max="60" value="' +
      esc(c.timeout_seconds || 10) + '"></label>' +
      '<label class="ap-field wide"><span>Bearer Token <i>可选</i></span>' +
      '<input id="apToken" type="password" autocomplete="new-password" placeholder="可选，留空表示不带头"></label>' +
      '<label class="ap-check"><input id="apEnabled" type="checkbox"' +
      (c.enabled ? " checked" : "") + '> 启用推送</label>' +
      '<label class="ap-check"><input id="apShared" type="checkbox"' +
      (c.include_shared ? " checked" : "") + '> 也推送群内分享的公众号文章</label>' +
      '</div><div class="ap-actions">' +
      '<button class="btn primary" data-act="ap-save">保存配置</button>' +
      '<button class="btn" data-act="ap-test">测试推送</button>' +
      '<button class="btn" data-act="ap-clear-token">清除令牌</button></div>' +
      '<p class="ap-note">保存后后台线程立即按新配置执行，不用重启。默认只推「公众号推送」；' +
      '停用期间收到的文章不会补推。失败会自动重试 6 次（5s / 15s / 1min / 5min / 15min / 1h），' +
      '仍失败会留在「失败批次」里等待手动重试。测试推送会真的向对方发一条示例链接。</p></div>' +
      '<div class="ap-card"><h3>POST 参数说明</h3>' +
      '<pre id="apSpec" class="ap-spec">读取中…</pre></div>' +
      '<div class="ap-card"><h3>运行状态</h3><div id="apStatus"></div>' +
      '<div class="ap-actions">' +
      '<button class="btn" data-act="ap-act:retry_failed">重试失败批次</button>' +
      '<button class="btn" data-act="ap-act:reset_baseline">跳过积压（重置基线）</button>' +
      '<button class="btn danger" data-act="ap-act:clear_failed">清除失败批次</button>' +
      '<button class="btn danger" data-act="ap-act:clear_logs">清空日志</button></div></div>' +
      '<div class="ap-card"><h3>最近推送日志</h3><div id="apLogs" class="ap-logs"></div></div>' +
      '</div></div>';
    var mode = $("apMode"); if (mode) mode.value = c.mode || "message";
    fillArticlePushForm(state.apps.push.data && state.apps.push.data.config);
    renderArticlePushStatus();
    renderArticlePushLogs();
  }

  /* ---------------- 应用：拟人大脑 ---------------- */
  function renderBrainPage(main) {
    var bd = state.apps.brain.data || {};
    var tabs = [["overview", "概览"], ["groups", "群聊"], ["talk", "人设与节奏"], ["logs", "日志"]];
    var tabHtml = tabs.map(function (t) {
      // dirty 点：6px 琥珀点；日志 tab 额外带条数徽章（有脏点时徽章让位）
      return '<button class="brain-tab" data-act="brain-tab" data-tab="' + t[0] +
        '" id="brainTab-' + t[0] + '" role="tab" aria-selected="false" tabindex="-1"' +
        ' aria-controls="brainPane-' + t[0] + '">' + t[1] +
        '<span class="brain-tab-dirty" hidden></span>' +
        "</button>";
    }).join("");

    var paneOverview =
      '<section class="brain-pane" id="brainPane-overview" data-pane="overview"' +
      ' role="tabpanel" aria-labelledby="brainTab-overview">' +
      '<div class="brain-split">' +
      '<div class="brain-side">' +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">运行开关</h3>' +
      '<p class="brain-card-sub">总开关与演练模式，保存后约 2 秒生效</p></div></div>' +
      '<div class="brain-card-body"><div id="brainRuntimeBox" class="brain-runtime">' +
      '<div class="brain-cfg-msg">读取中…</div></div></div>' +
      "</div>" +
      "</div>" +
      '<div class="brain-main">' +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">大模型设置</h3>' +
      '<p class="brain-card-sub">接口地址、模型名与密钥，保存后约 2 秒生效</p></div></div>' +
      '<div class="brain-card-body">' +
      '<div class="brain-fields">' +
      '<div class="brain-field wide is-required">' +
      '<label class="brain-label" for="brainBaseUrl">接口地址 base_url' +
      '<span class="brain-req">*</span></label>' +
      '<input type="text" id="brainBaseUrl" value="https://api.deepseek.com"' +
      ' placeholder="https://api.deepseek.com" autocomplete="off">' +
      '<span class="brain-hint">填兼容 OpenAI 的接口地址，一般以 /v1 结尾</span></div>' +
      '<div class="brain-field is-required">' +
      '<label class="brain-label" for="brainModel">模型 model' +
      '<span class="brain-req">*</span></label>' +
      '<input type="text" id="brainModel" value="deepseek-flash"' +
      ' placeholder="deepseek-flash" autocomplete="off">' +
      '<span class="brain-hint">例如 deepseek-chat</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainApiKey">API Key</label>' +
      '<input type="password" id="brainApiKey" value="" placeholder="sk-..." autocomplete="off">' +
      '<span class="brain-hint">留空表示保持不变</span></div>' +
      "</div>" +
      '<div class="brain-actions is-sticky">' +
      '<button class="btn primary" data-act="brain-save">保存模型</button>' +
      '<button class="btn" data-act="brain-test">测试连通</button>' +
      '<div id="brainCfgMsg" class="brain-cfg-msg"></div>' +
      "</div>" +
      "</div></div>" +
      "</div></div>" +
      '<p class="brain-note">各区块各自保存、各自生效，约 2 秒后应用到下一轮决策。' +
      '演练模式下只记录不发消息，确认决策日志合理后再关闭演练。</p>' +
      "</section>";

    var paneGroups =
      '<section class="brain-pane" id="brainPane-groups" data-pane="groups"' +
      ' role="tabpanel" aria-labelledby="brainTab-groups">' +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">群聊白名单</h3>' +
      '<p class="brain-card-sub">只接管勾选的群</p></div></div>' +
      '<div class="brain-card-body"><div id="brainGroupsBox" class="brain-groups">' +
      '<div class="brain-cfg-msg">读取中…</div></div></div>' +
      "</div>" +
      '<p class="brain-note">只接管勾选的群，未勾选完全不受影响。保存后约 2 秒生效，无需重启。</p>' +
      "</section>";

    var paneTalk =
      '<section class="brain-pane" id="brainPane-talk" data-pane="talk"' +
      ' role="tabpanel" aria-labelledby="brainTab-talk">' +
      '<div class="brain-split">' +
      '<div class="brain-side">' +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">接话节奏</h3>' +
      '<p class="brain-card-sub">管它「什么时候开口」；被 @ 时不受这里限制</p></div></div>' +
      '<div class="brain-card-body">' +
      '<div class="brain-fields is-single">' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneProb">主动接话概率</label>' +
      '<input type="number" id="brainTuneProb" min="0" max="1" step="0.01">' +
      '<span class="brain-hint">填 0 ~ 1 的小数；0 = 只在被 @ 或命中关键词时说话</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneCooldown">冷却秒数</label>' +
      '<input type="number" id="brainTuneCooldown" min="0" max="86400" step="1">' +
      '<span class="brain-hint">0 ~ 86400 的整数；回完一句后这段时间内不再主动插话，被 @ 不看这一项</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneMax">10 分钟上限</label>' +
      '<input type="number" id="brainTuneMax" min="1" max="1000000" step="1">' +
      '<span class="brain-hint">1 ~ 1000000 的整数；10 分钟内最多主动插话几次，填大数等于不限制</span></div>' +
      '<p class="brain-note">下面是「跟进」：接上话之后，在一段时间内用更高的概率继续跟，接满几句就交回给人。' +
      '没有这一段的话，每句话都按开场概率重掷，聊到一半突然不吭声。</p>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneFollowWindow">跟进窗口（秒）</label>' +
      '<input type="number" id="brainTuneFollowWindow" min="0" max="3600" step="1">' +
      '<span class="brain-hint">刚说完话后的这段时间算「同一段对话」；填 0 关闭跟进</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneFollowProb">跟进时的接话概率</label>' +
      '<input type="number" id="brainTuneFollowProb" min="0" max="1" step="0.01">' +
      '<span class="brain-hint">0 ~ 1 的小数；比上面的开场概率高，才接得上</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneFollowTurns">一段对话最多连说几句</label>' +
      '<input type="number" id="brainTuneFollowTurns" min="1" max="20" step="1">' +
      '<span class="brain-hint">1 ~ 20 的整数；接满这么多句就停下，把话头交回群里</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainTuneFollowCooldown">跟进冷却（秒）</label>' +
      '<input type="number" id="brainTuneFollowCooldown" min="0" max="600" step="1">' +
      '<span class="brain-hint">0 ~ 600 的整数；跟随时用这个短冷却，不用上面那个长冷却</span></div>' +
      "</div>" +
      '<div class="brain-actions is-sticky">' +
      '<button class="btn primary" data-act="brain-tuning-save">保存节奏</button>' +
      '<button class="btn ghost" data-act="brain-tuning-reset">恢复默认</button>' +
      '<div id="brainTuningMsg" class="brain-cfg-msg"></div>' +
      "</div>" +
      "</div></div>" +
      "</div>" +
      '<div class="brain-main">' +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">人设提示词</h3>' +
      '<p class="brain-card-sub">决定它在群里怎么说话：身份、语气、口癖、称呼</p></div></div>' +
      '<div class="brain-card-body">' +
      '<div class="brain-fields">' +
      '<div class="brain-field wide">' +
      '<label class="brain-label" for="brainPersonaDefault">默认人设</label>' +
      '<textarea id="brainPersonaDefault" rows="7" maxlength="2000" placeholder="例如：你是许昌本地人，' +
      '三十来岁，爱看电影，说话短、口语化，偶尔带一句河南话，但别油腻……"></textarea>' +
      '<span class="brain-hint">所有群通用；留空则用内置模板。写清「是谁 / 说话风格 / 禁忌」三件事效果最好</span></div>' +
      '<div class="brain-field">' +
      '<label class="brain-label" for="brainPersonaGroup">单独设置某个群</label>' +
      '<span class="brain-select"><select id="brainPersonaGroup"></select></span>' +
      '<span class="brain-hint">只列已勾选接管的群</span></div>' +
      '<div class="brain-field wide">' +
      '<label class="brain-label" for="brainPersonaGroupText">该群人设</label>' +
      '<textarea id="brainPersonaGroupText" rows="4" maxlength="2000"' +
      ' placeholder="留空表示这个群沿用默认人设"></textarea>' +
      '<span class="brain-hint">留空 = 用上面的默认人设</span></div>' +
      "</div>" +
      '<div class="brain-actions is-sticky">' +
      '<button class="btn primary" data-act="brain-persona-save">保存人设</button>' +
      '<button class="btn" data-act="brain-persona-reset">填模板</button>' +
      '<span class="spacer"></span>' +
      '<button class="btn" data-act="brain-persona-group-save">保存该群</button>' +
      '<button class="btn ghost" data-act="brain-persona-group-clear">改用默认</button>' +
      '<div id="brainPersonaMsg" class="brain-cfg-msg"></div>' +
      "</div>" +
      "</div></div>" +
      "</div></div>" +
      "</section>";

    var paneLogs =
      '<section class="brain-pane" id="brainPane-logs" data-pane="logs"' +
      ' role="tabpanel" aria-labelledby="brainTab-logs">' +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">决策日志</h3>' +
      '<p class="brain-card-sub">每条群消息回没回、为什么，都在这里</p></div></div>' +
      '<div class="brain-card-body"><div id="brainLogBox" class="brain-logs">' +
      '<div class="empty-list">切到本页后加载…</div></div></div>' +
      "</div>" +
      '<div class="brain-card">' +
      '<div class="brain-card-head"><div><h3 class="brain-card-title">运行日志</h3>' +
      '<p class="brain-card-sub">大脑进程自身的输出，排错用（brain.log）</p></div>' +
      '<div class="brain-card-ops">' +
      '<button class="btn" data-act="brain-rt-log">载入日志</button>' +
      "</div></div>" +
      '<div class="brain-card-body">' +
      '<pre id="brainRtLog" class="brain-rtlog">点上方按钮加载</pre></div>' +
      "</div>" +
      "</section>";

    main.innerHTML =
      '<div class="brain-page">' +
      '<div class="brain-head">' +
      '<div class="brain-head-main">' +
      '<h2 class="brain-head-title">拟人大脑</h2>' +
      '<p class="brain-head-desc">本地旁路应用：只读群消息，按拟人策略决策后写入发送队列；关掉即恢复现状。</p>' +
      "</div>" +
      '<div class="brain-head-side">' +
      '<button class="btn ghost brain-back" data-act="apps-back">返回应用</button>' +
      '<button class="btn" data-act="brain-refresh">刷新</button>' +
      "</div>" +
      "</div>" +
      '<div class="brain-status" id="brainStatusBar" aria-live="polite"></div>' +
      '<div class="brain-tabs" id="brainTabs" role="tablist">' + tabHtml + "</div>" +
      '<div class="brain-body">' +
      paneOverview + paneGroups + paneTalk + paneLogs +
      "</div>" +
      "</div>";

    if (state.apps.brain.data) {
      fillBrainConfig(bd.llm || null);
      fillBrainRuntime(bd.runtime || null);
      fillBrainGroups(bd.groups || null);
      fillBrainPersona(bd.persona || null);
      fillBrainTuning(bd.tuning || null);
    }
    // fromRender：DOM 刚重建，别再拿新节点的 scrollTop 覆盖旧记忆
    switchBrainTab(brainTabKey(), true);
    renderBrainStatus();
    if (state.apps.brain.logs) renderBrainLogs();
  }

  /* ---------------- 拟人大脑 · 顶部 tab 切换 ---------------- */
  function brainTabKey() { return state.apps.brain.tab || "overview"; }

  function brainScrollBox() { return document.querySelector(".brain-body"); }

  // 每个 pane 各自记住滚动位置（.brain-body 是唯一滚动容器）
  function restoreBrainScroll(key) {
    var body = brainScrollBox();
    if (!body) return;
    var y = (state.apps.brain.scroll || {})[key] || 0;
    body.scrollTop = y;
    if (window.requestAnimationFrame) {
      // pane 刚由 hidden 转显示，高度变化可能把 scrollTop 夹回去，下一帧再补一次
      requestAnimationFrame(function () {
        var b = brainScrollBox();
        if (b && b.scrollTop !== y) b.scrollTop = y;
      });
    }
  }

  function switchBrainTab(key, fromRender) {
    var bar = $("brainTabs");
    if (!bar) return;
    var btns = bar.querySelectorAll(".brain-tab");
    var ok = false;
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].dataset.tab === key) { ok = true; break; }
    }
    if (!ok) key = "overview";

    // 切走前先记下当前 pane 的滚动位置
    var body = brainScrollBox();
    if (body && !fromRender) {
      if (!state.apps.brain.scroll) state.apps.brain.scroll = {};
      state.apps.brain.scroll[brainTabKey()] = body.scrollTop;
    }

    state.apps.brain.tab = key;
    for (var j = 0; j < btns.length; j++) {
      var on = btns[j].dataset.tab === key;
      btns[j].classList.toggle("active", on);
      btns[j].setAttribute("aria-selected", on ? "true" : "false");
      btns[j].tabIndex = on ? 0 : -1;
    }
    var panes = document.querySelectorAll(".brain-pane");
    for (var k = 0; k < panes.length; k++) {
      // 只用 hidden 属性（配合 .brain-pane[hidden]），不写内联 display
      panes[k].hidden = panes[k].dataset.pane !== key;
    }
    restoreBrainScroll(key);
    updateBrainTabsOverflow();
    // 这里只做显隐与滚动位置：绝不调用 loadBrain / fillBrain*
    // （fillBrainPersona 会用服务端值覆盖用户没保存的 textarea）
  }

  function updateBrainTabsOverflow() {
    var bar = $("brainTabs");
    if (!bar) return;
    var over = bar.scrollWidth > bar.clientWidth + 1;
    bar.classList.toggle("has-overflow", over);
    var act = bar.querySelector(".brain-tab.active");
    if (!act || !act.scrollIntoView) return;
    // 滚到最右时移除渐隐遮罩，否则最后一个 tab 永远被淡掉
    if (!over) return;
    var br = bar.getBoundingClientRect(), ar = act.getBoundingClientRect();
    if (ar.left < br.left || ar.right > br.right) {
      try {
        act.scrollIntoView({ block: "nearest", inline: "nearest" });
      } catch (err) { bar.scrollLeft = Math.max(0, act.offsetLeft - 20); }
    }
  }

  /* ---------------- 拟人大脑 · 状态条 ---------------- */
  function renderBrainStatus() {
    var lg = state.apps.brain.logs || {};
    var bd = state.apps.brain.data || {};
    // 心跳优先取运行开关接口（独立于日志接口，日志挂了也能显示进程死活）
    var e = (bd.runtime && bd.runtime.engine) || lg.engine || {};
    var rt = bd.runtime || {};
    var gg = bd.groups || {};
    var ll = bd.llm || {};
    var hasEngine = !!(bd.runtime && bd.runtime.engine) || !!lg.engine;
    var off = String(e.running_state || "").indexOf("总开关已关闭") === 0;

    // 1 进程态：读取中 → 未启动 → 已停用（进程还在待机）→ 运行中
    //    「待机」不等于进程退出，别让用户去翻系统服务
    var proc;
    if (!hasEngine) proc = { cls: "mute", text: "读取中…" };
    else if (!e.alive) proc = { cls: "mute", text: "进程未启动" };
    else if (off) proc = { cls: "mute", text: "已停用 · 待机" };
    else proc = { cls: "ok", text: "运行中" };

    // 2 运行模式：演练给蓝点，实发不传状态类（避免「运行中 + 实发」两个绿点）
    var dry = e.dry_run != null ? !!e.dry_run : !!rt.dry_run;
    var dryLlm = e.llm_mode ? e.llm_mode === "real" : (rt.dry_run_llm !== false);
    var mode;
    if (!bd.runtime && !bd.llm) mode = { cls: "mute", text: "—" };
    else if (dry) mode = { cls: "dry", text: dryLlm ? "演练 · 真模型" : "演练 · 模拟文本" };
    else mode = { cls: "", text: "实发" };

    // 3 模型：未配置给红点（会直接导致回复失败）
    var model;
    if (!bd.llm) model = { cls: "mute", text: "—" };
    else if (ll.configured === false || (!ll.model && !ll.configured)) {
      model = { cls: "bad", text: "未配置" };
    } else model = { cls: "", text: ll.model || "已配置" };

    // 4/5 计数项：无状态类 → 次级文字、不出圆点
    var groupsText = bd.groups ? ("接管 " + (gg.selected || []).length + " 个群") : "接管 —";
    var tickText = "无心跳记录";
    if (e.last_tick_ago != null) {
      tickText = e.last_tick_ago < 60
        ? "最后活动 " + e.last_tick_ago + " 秒前"
        : "最后活动 " + Math.round(e.last_tick_ago / 60) + " 分钟前";
    }

    var bar = $("brainStatusBar");
    if (bar) {
      var item = function (cls, text, id) {
        return '<span class="brain-status-item' + (cls ? " " + cls : "") + '"' +
          (id ? ' id="' + id + '"' : "") + ">" +
          '<span class="dot"></span><span>' + esc(text) + "</span></span>";
      };
      var sep = '<span class="brain-status-sep"></span>';
      bar.innerHTML = item(proc.cls, proc.text, "brainHeadState") + sep +
        item(mode.cls, mode.text) + sep +
        item(model.cls, model.text) + sep +
        item("", groupsText) + sep +
        item("", tickText);
    }
  }

  /* ---------------- 拟人大脑 · 未保存提示 ---------------- */
  // tab 上只出现琥珀色 dirty 点，不再挂任何文字徽章
  function brainSetTabDirty(paneKey, on) {
    var tab = document.querySelector('#brainTabs .brain-tab[data-tab="' + paneKey + '"]');
    if (!tab) return;
    var dot = tab.querySelector(".brain-tab-dirty");
    if (dot) dot.hidden = !on;
  }

  function brainMarkDirty(paneKey) {
    if (!paneKey) return;
    if (!state.apps.brain.dirty) state.apps.brain.dirty = {};
    state.apps.brain.dirty[paneKey] = true;
    brainSetTabDirty(paneKey, true);
  }

  function brainClearDirty() {
    state.apps.brain.dirty = {};
    var tabs = document.querySelectorAll("#brainTabs .brain-tab");
    for (var i = 0; i < tabs.length; i++) {
      var dot = tabs[i].querySelector(".brain-tab-dirty");
      if (dot) dot.hidden = true;
    }
  }

  function brainHasDirty() {
    return Object.keys(state.apps.brain.dirty || {}).length > 0;
  }

  // 纯「看/筛选/切换目标」的控件不算未保存的修改
  function brainIsFilterField(el) {
    if (!el) return true;
    return el.id === "brainGroupSearch" || el.id === "brainLogChat" ||
      el.id === "brainPersonaGroup";
  }

  function fillBrainConfig(b) {
    if (!$("brainBaseUrl")) return;
    b = b || {};
    $("brainBaseUrl").value = b.base_url || "https://api.deepseek.com";
    $("brainModel").value = b.model || "deepseek-flash";
    var keyLabel = $("brainCfgMsg");
    if (keyLabel && !keyLabel.textContent) {
      var input = $("brainApiKey");
      if (input) input.placeholder = b.configured ? ("已配置 " + (b.key_hint || "") + "，留空保持不变") : "sk-...";
    }
  }

  function renderArticlePushStatus() {
    var box = $("apStatus"), d = state.apps.push.data;
    if (!box) return;
    if (!d) {
      box.innerHTML = '<div class="empty-list">' +
        esc(state.apps.push.error ? "读取失败：" + state.apps.push.error : "加载中…") + "</div>";
      return;
    }
    var s = d.stats || {}, e = d.engine || {}, c = d.config || {};
    var engine = e.running ? "运行中" : "未运行（管理台需重启）";
    box.innerHTML = '<dl class="rem-kv">' +
      remKv("引擎", engine, e.running ? "" : "bad") +
      remKv("当前状态", c.enabled ? "已启用" : "未启用") +
      remKv("接收 URL", c.url || "未配置") +
      remKv("推送范围", c.include_shared ? "公众号推送 + 群内分享" : "仅公众号推送") +
      remKv("成功 / 失败", (s.success || 0) + " / " + (s.failed || 0) + " 次") +
      remKv("待推 / 失败批次", (s.pending || 0) + " / " + (s.failed_batches || 0)) +
      remKv("最近成功", s.last_push_at || "从未") +
      remKv("最近错误", s.last_error || "无", s.last_error ? "bad" : "") +
      remKv("采集基线 / 最新", (s.baseline || 0) + " / " + (s.source_max_rowid || 0)) +
      remKv("基线时间", s.baseline_at || "—") +
      "</dl>";
    var hint = $("apHint");
    if (hint) {
      hint.textContent = c.enabled
        ? "已启用 · 每 1.5 秒扫描新文章 · 只推链接数组"
        : "未启用 · 填好 URL 并勾选「启用推送」后保存";
      hint.className = "rem-hint" + (e.running ? "" : " warn");
    }
    var spec = $("apSpec");
    if (spec) {
      spec.textContent =
        "POST " + (c.url || "<接收 URL>") + "\n" +
        "Content-Type: application/json; charset=utf-8\n" +
        (c.token_set ? "Authorization: Bearer <已设置的 token>\n" : "") + "\n" +
        JSON.stringify(d.post && d.post.body ? d.post.body : { urls: ["<文章链接>"] }, null, 2) +
        "\n\n字段名：" + (c.param_name || "urls") +
        "（可在上方修改）· 值恒为字符串数组，只有一篇时也是长度 1 的数组\n" +
        "请求体不含标题、时间、公众号名等其它字段；2xx 视为成功。";
    }
  }

  function renderArticlePushLogs() {
    var box = $("apLogs"), d = state.apps.push.data;
    if (!box) return;
    if (!d) { box.innerHTML = '<div class="empty-list">加载中…</div>'; return; }
    var logs = d.logs || [];
    if (!logs.length) { box.innerHTML = '<div class="empty-list">还没有推送记录</div>'; return; }
    box.innerHTML = '<table class="ap-table"><thead><tr>' +
      '<th>时间</th><th>结果</th><th>HTTP</th><th>链接数</th><th>耗时</th><th>详情</th>' +
      '</tr></thead><tbody>' + logs.map(function (l) {
        var urls = [];
        try { urls = JSON.parse(l.urls_json || "[]"); } catch (err) { urls = []; }
        var detail = (l.error ? "[" + l.error + "] " : "") +
          (urls.length ? urls.join(" , ") : "") + (l.target ? " → " + l.target : "");
        return '<tr class="' + (l.ok ? "ok" : "bad") + '"><td>' + esc(l.at) + '</td>' +
          '<td>' + (l.is_test ? "测试" : (l.ok ? "成功" : "失败")) + '</td>' +
          '<td>' + (l.http_status || "—") + '</td><td>' + l.url_count + '</td>' +
          '<td>' + l.ms + 'ms</td><td class="ap-detail" title="' + esc(detail) + '">' +
          esc(detail) + '</td></tr>';
      }).join("") + '</tbody></table>';
  }

  function apBusy(on) {
    state.apps.push.busy = on;
    document.querySelectorAll(".ap-actions .btn").forEach(function (b) { b.disabled = on; });
  }

  function saveArticlePush() {
    var p = state.apps.push;
    if (p.busy) return;
    var payload = apPayload();
    if (!/^https?:\/\/[^\s]+$/i.test(payload.url)) { toast("请填写完整的 http(s) 接收 URL"); return; }
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,30}$/.test(payload.param_name)) {
      toast("参数名只能用字母、数字、下划线"); return;
    }
    apBusy(true);
    api("/api/article-push/config", {
      method: "POST", body: JSON.stringify(payload), detail: true
    }).then(function (r) {
      toast("已保存：" + (r.hint || "配置生效"));
      p.clearToken = false; p.formDirty = false;
      var t = $("apToken"); if (t) t.value = "";
    }).catch(function (e) {
      toast("保存失败：" + e.message);
    }).then(function () { apBusy(false); loadArticlePush(true); });
  }

  function testArticlePush() {
    var p = state.apps.push;
    if (p.busy) return;
    var payload = apPayload();
    if (!/^https?:\/\/[^\s]+$/i.test(payload.url)) { toast("请先填写接收 URL"); return; }
    apBusy(true);
    api("/api/article-push/test", {
      method: "POST", body: JSON.stringify(payload), detail: true
    }).then(function (r) {
      toast(r.ok ? ("测试成功 · HTTP " + r.http_status + " · " + r.ms + "ms")
        : ("测试失败 · " + (r.error || r.http_status)));
    }).catch(function (e) {
      toast("测试失败：" + e.message);
    }).then(function () { apBusy(false); loadArticlePush(true); });
  }

  function doArticlePushAction(action) {
    var p = state.apps.push;
    if (p.busy) return;
    var confirmText = {
      reset_baseline: "把基线移到最新？此前积压、尚未推送的文章将不再补发。",
      clear_failed: "确定清除所有失败批次？此操作不可撤销。",
      clear_logs: "确定清空推送日志？此操作不可撤销。"
    }[action];
    if (confirmText && !window.confirm(confirmText)) return;
    apBusy(true);
    api("/api/article-push/action", {
      method: "POST", body: JSON.stringify({ action: action }), detail: true
    }).then(function (r) {
      toast(r.message || "操作完成");
    }).catch(function (e) {
      toast("操作失败：" + e.message);
    }).then(function () { apBusy(false); loadArticlePush(true); });
  }

  /* ---------------- 数据加载 ---------------- */
  function loadConversations() {
    api("/api/conversations?scope=recent&limit=300").then(function (d) {
      state.conversations = d.items || [];
      if (state.nav === "chat") renderConversations($("listBody"));
    }).catch(function () {});
  }

  function loadContacts(tab) {
    state.contactsLoaded[tab] = true;
    var p;
    if (tab === "friend") p = api("/api/social/friends?limit=2000");
    else if (tab === "group") p = api("/api/social/groups");
    else p = api("/api/contacts?kind=official&limit=400");
    p.then(function (d) {
      state.contacts[tab] = tab === "friend" ? (d.friends || [])
        : tab === "group" ? (d.groups || []) : (d.contacts || []);
      if (state.nav === "contacts" && state.contactTab === tab) renderContacts($("listBody"));
    }).catch(function () { state.contactsLoaded[tab] = false; });
  }

  function loadMoments() {
    api("/api/social/moments").then(function (d) {
      state.moments = d;
      if (state.nav === "moments") renderMoments($("main"));
    }).catch(function () {});
  }

  function openChat(chatId) {
    if (!chatId) return;
    state.current = chatId;
    state.chat.thread = null;
    renderList();
    renderMain();
  }

  function loadThread(resetScroll) {
    var c = state.chat;
    if (!state.current) return;
    var seq = ++c.seq;
    var body = $("chatBody");
    var atBottom = !body || resetScroll ||
      body.scrollTop + body.clientHeight >= body.scrollHeight - 50;
    var anchor = body ? body.scrollHeight - body.scrollTop : 0;
    var chat = state.current;
    api("/api/thread?chat_id=" + enc(chat) + "&limit=" + c.limit)
      .then(function (d) { return d; })
      .catch(function () { return null; })
      .then(function (d) {
        if (seq !== c.seq || chat !== state.current) return;
        if (!d || d.error) return api("/api/feed?day=all&limit=" + c.limit + "&chat_id=" + enc(chat))
          .then(function (feed) { return threadFromFeed(feed, chat); });
        return d;
      })
      .then(function (d) {
        if (seq !== c.seq || chat !== state.current || !d) return;
        c.thread = d;
        renderBubbles();
        updateChatHead();
        var b = $("chatBody");
        if (b) {
          if (resetScroll || atBottom) b.scrollTop = b.scrollHeight;
          else b.scrollTop = b.scrollHeight - anchor;
        }
      })
      .catch(function () {});
  }

  function threadFromFeed(feed, chat) {
    var items = (feed.items || []).slice().reverse().map(function (x) {
      return {
        key: "feed:" + x.item_key, msg_key: x.item_key || "",
        ts: x.received_at_raw, received_at: x.received_at_raw,
        time: x.received_at_raw,
        self: false, is_self: false, direction: "in",
        sender: x.sender_name, sender_id: x.sender_id, avatar_url: "",
        kind: x.kind, body: x.body, text: x.body || "", title: x.title, url: x.url,
        description: x.description, media: x.image_filename || "", status: 0
      };
    });
    return {
      chat_id: chat, chat_name: feed.items && feed.items.length ? (feed.items[0].chat_id) : chat,
      source_kind: "", avatar_url_local: "", items: items, count: items.length,
      total: feed.total || items.length, has_more: false, send_enabled: true,
      sender_ready: true, image_send_enabled: false, downgrade: true
    };
  }

  function sendMessage() {
    var c = state.chat;
    if (c.sending || !state.current) return;
    var box = $("chatInput");
    var text = (box ? box.value : c.draft).trim();
    if (!text) return;
    c.sending = true;
    api("/api/send", { method: "POST", body: JSON.stringify({ chat_id: state.current, text: text }) })
      .then(function (r) {
        toast("已入队 #" + r.id + (r.warning ? " · " + r.warning : ""));
        c.draft = "";
        if (box) box.value = "";
        loadThread(true);
        pollSendStatus(r.id);
      })
      .catch(function (e) { toast("发送失败：" + e.message); })
      .then(function () { c.sending = false; });
  }

  function pollSendStatus(id) {
    var tries = 0;
    var iv = setInterval(function () {
      tries++;
      api("/api/send?id=" + id).then(function (r) {
        var el = document.querySelector('.bubble-row[data-key="outbox:' + id + '"]');
        if (el) {
          var s = el.querySelector(".bubble-status");
          if (s) {
            if (r.status === 2) { s.textContent = "已发送"; s.className = "bubble-status"; }
            else if (r.status === 3) { s.textContent = r.error || "发送失败"; s.className = "bubble-status s-fail"; }
          }
        }
        if (r.status >= 2 || tries > 20) clearInterval(iv);
      }).catch(function () { if (tries > 20) clearInterval(iv); });
    }, 1500);
  }

  /* ---------------- 资料弹窗 ---------------- */
  function openProfile(id) {
    if (!id) return;
    if (id.indexOf("@chatroom") >= 0) return openGroupProfile(id);
    api("/api/contacts?kind=&system=1&limit=1000").then(function (d) {
      var c = (d.contacts || []).filter(function (x) { return x.chat_id === id; })[0];
      var friend = (state.contacts.friend || []).filter(function (x) { return x.wxid === id; })[0];
      var card = '<div class="profile-hero">' + avatarHtml(c ? c.avatar_url : "", friend ? (friend.name || friend.remark) : id, "72px") +
        '<h3>' + esc((c && c.name) || (friend && (friend.name || friend.remark)) || id) + '</h3>' +
        '<div class="p-sub">' + (friend && friend.wechat_name ? "微信名：" + esc(friend.wechat_name) : "联系人") + '</div></div>';
      var rows = [];
      if (friend) {
        if (friend.remark) rows.push(["备注", friend.remark]);
        if (friend.alias) rows.push(["微信号", friend.alias]);
        if (friend.description) rows.push(["个性签名", friend.description]);
      }
      rows.push(["wxid", id]);
      rows.push(["类型", c ? c.kind_label : "联系人"]);
      card += '<dl class="kv">' + rows.map(function (r) {
        return "<dt>" + esc(r[0]) + "</dt><dd>" + esc(r[1]) + "</dd>"; }).join("") + "</dl>";
      card += '<div style="text-align:center;margin-top:18px"><button class="btn primary" data-act="profile-send">发消息</button></div>';
      showModal(card);
    }).catch(function () { showModal('<div class="profile-hero"><h3>' + esc(id) + '</h3></div>'); });
  }

  function openGroupProfile(room) {
    api("/api/social/group?room=" + enc(room)).then(function (d) {
      if (d.error) { showModal('<div class="empty-list">' + esc(d.error) + '</div>'); return; }
      var card = '<div class="profile-hero">' + avatarHtml(d.avatar_url_local, d.name, "72px") +
        '<h3>' + esc(d.name) + '</h3>' +
        '<div class="p-sub">' + (d.member_count || 0) + " 人 · " +
        (d.status === "ok" ? "已加载" : "未加载（在微信打开该群后同步）") + '</div></div>';
      if (d.announcement && d.announcement.text) {
        card += '<div class="section-title">群公告</div><div style="white-space:pre-wrap;line-height:1.6">' +
          esc(d.announcement.text) + '</div>';
      }
      card += '<div class="section-title">群成员（' + ((d.members || []).length) + '）</div>';
      card += '<div class="member-grid">' + (d.members || []).slice(0, 120).map(function (m) {
        var role = m.is_owner ? '<span class="role-badge owner">群主</span>'
          : m.is_admin ? '<span class="role-badge admin">管理员</span>' : "";
        return '<div class="member" data-wxid="' + esc(m.wxid) + '">' +
          avatarHtml(m.avatar_url_local, m.display_name, "52px") +
          '<div class="m-nm">' + esc(m.display_name) + role + '</div></div>';
      }).join("") + '</div>';
      showModal(card);
    }).catch(function () { showModal('<div class="empty-list">读取群资料失败</div>'); });
  }

  function showModal(html) { $("modalCard").innerHTML = html; $("modal").classList.remove("hidden"); }
  function hideModal() { $("modal").classList.add("hidden"); }

  /* ---------------- 大脑模型设置 ---------------- */
  function brainMsg(text, ok) {
    var el = $("brainCfgMsg");
    if (el) { el.textContent = text; el.className = "brain-cfg-msg " + (ok ? "ok" : "bad"); }
  }
  function saveBrainConfig() {
    var payload = {
      base_url: ($("brainBaseUrl") || {}).value || "",
      model: ($("brainModel") || {}).value || "",
      api_key: ($("brainApiKey") || {}).value || ""
    };
    if (!payload.base_url.trim()) { toast("请填写接口地址"); return; }
    if (!payload.model.trim()) { toast("请填写模型名"); return; }
    api("/api/brain/llm", { method: "POST", body: JSON.stringify(payload), detail: true })
      .then(function (r) {
        toast("大脑模型配置已保存");
        if ($("brainApiKey")) $("brainApiKey").value = "";
        brainMsg("已保存" + ((r.changed || []).indexOf("api_key") >= 0 ? "（含密钥）" : ""), true);
        brainClearDirty();
      }).catch(function (err) { brainMsg("保存失败：" + (err.message || err), false); });
  }
  function testBrainConfig() {
    brainMsg("正在测试连通…", true);
    api("/api/brain/test").then(function (r) {
      brainMsg(r.message, r.ok);
    }).catch(function (err) { brainMsg("测试失败：" + (err.message || err), false); });
  }

  /* ---------------- 大脑运行开关 ---------------- */
  function brainRuntimeMsg(text, ok) {
    var el = $("brainRuntimeMsg");
    if (el) { el.textContent = text; el.className = "brain-cfg-msg " + (ok ? "ok" : "bad"); }
  }
  function fillBrainRuntime(r) {
    var box = $("brainRuntimeBox");
    if (!box) return;
    var st = r || { enabled: false, dry_run: true, dry_run_llm: true, at_reply: true, source: "config" };
    var srcNote = st.source === "web"
      ? "当前生效：网页设置（runtime.local.json）"
      : "当前生效：config.local.json（网页尚未设置，保存一次即切换到网页开关）";
    var html = '<div class="brain-groups-note">' + esc(srcNote) + '</div>';
    html += '<label class="brain-switch-row">' +
      '<span><b>启用大脑</b><small>总开关。关闭时大脑进程即使运行也不做任何决策</small></span>' +
      '<input type="checkbox" id="brainSwitchEnabled"' + (st.enabled ? " checked" : "") + '></label>';
    html += '<label class="brain-switch-row">' +
      '<span><b>被@必回</b><small>群里真@它时一定回，不受概率、冷却、安静时段限制（演练模式下仍只记录不发）</small></span>' +
      '<input type="checkbox" id="brainSwitchAt"' + (st.at_reply !== false ? " checked" : "") + '></label>';
    html += '<label class="brain-switch-row">' +
      '<span><b>演练模式 dry_run</b><small>开启时完整跑决策与记忆，但只写日志、不发真消息</small></span>' +
      '<input type="checkbox" id="brainSwitchDry"' + (st.dry_run ? " checked" : "") + '></label>';
    html += '<label class="brain-switch-row">' +
      '<span><b>演练也调模型</b><small>演练时照样真实调用大模型，能看到真实回复效果（会消耗 token）；关闭则用固定模拟文本</small></span>' +
      '<input type="checkbox" id="brainSwitchDryLlm"' + (st.dry_run_llm !== false ? " checked" : "") + '></label>';
    html += '<div class="brain-cfg-actions">' +
      '<button class="btn" data-act="brain-runtime-save">保存开关</button></div>';
    html += '<div id="brainRuntimeMsg" class="brain-cfg-msg"></div>';
    box.innerHTML = html;
  }
  function saveBrainRuntime() {
    var payload = {
      enabled: !!($("brainSwitchEnabled") && $("brainSwitchEnabled").checked),
      dry_run: !!($("brainSwitchDry") && $("brainSwitchDry").checked),
      dry_run_llm: !!($("brainSwitchDryLlm") && $("brainSwitchDryLlm").checked),
      at_reply: !!($("brainSwitchAt") && $("brainSwitchAt").checked)
    };
    brainRuntimeMsg("正在保存…", true);
    api("/api/brain/runtime", { method: "POST", body: JSON.stringify(payload), detail: true })
      .then(function () {
        brainRuntimeMsg("已保存：大脑" + (payload.enabled ? "启用" : "停用") +
          " · 被@" + (payload.at_reply ? "必回" : "按概率") +
          " · 演练" + (payload.dry_run ? "开" : "关") +
          " · " + (payload.dry_run_llm ? "真模型" : "模拟文本") +
          "；约 2 秒后生效，无需重启。", true);
        toast("大脑运行开关已保存");
        brainClearDirty();
      }).catch(function (err) { brainRuntimeMsg("保存失败：" + (err.message || err), false); });
  }

  /* ---------------- 大脑群聊白名单 ---------------- */
  function brainGroupsMsg(text, ok) {
    var el = $("brainGroupsMsg");
    if (el) { el.textContent = text; el.className = "brain-cfg-msg " + (ok ? "ok" : "bad"); }
  }
  function fillBrainGroups(g) {
    var box = $("brainGroupsBox");
    if (!box) return;
    if (!g || !g.groups || !g.groups.length) {
      box.innerHTML = '<div class="brain-cfg-msg">暂无群聊数据；收到群消息后会自动出现在这里。</div>';
      return;
    }
    var srcNote = g.source === "web"
      ? "当前生效：网页勾选（groups.local.json）"
      : "当前生效：config.local.json（尚未在网页勾选，保存一次即切换到网页白名单）";
    var html = '<div class="brain-groups-note">' + esc(srcNote) + '</div>';
    html += '<input type="text" id="brainGroupSearch" class="brain-group-search" placeholder="搜索群名…" autocomplete="off">';
    html += '<div class="brain-group-tools">' +
      '<button class="btn btn-sm" data-act="brain-grp-all">全选</button>' +
      '<button class="btn btn-sm" data-act="brain-grp-none">全不选</button>' +
      '<span id="brainGroupCount" class="brain-group-count"></span></div>';
    html += '<div class="brain-group-list">';
    g.groups.forEach(function (grp) {
      html += '<label class="brain-group-item" data-name="' + esc((grp.name || "").toLowerCase()) + '">' +
        '<input type="checkbox" value="' + esc(grp.chat_id) + '"' + (grp.enabled ? " checked" : "") + '>' +
        '<span class="brain-group-name">' + esc(grp.name) + '</span>' +
        '<span class="brain-group-meta">' + (grp.n ? esc(grp.n) + " 条" : "—") + '</span>' +
        '</label>';
    });
    html += '</div>';
    html += '<div class="brain-cfg-actions is-sticky">' +
      '<button class="btn" data-act="brain-groups-save">保存白名单</button>' +
      '<div id="brainGroupsMsg" class="brain-cfg-msg"></div></div>';
    box.innerHTML = html;
    updateBrainGroupCount();
  }
  function updateBrainGroupCount() {
    var el = $("brainGroupCount");
    if (!el) return;
    var boxes = document.querySelectorAll("#brainGroupsBox .brain-group-item input[type=checkbox]");
    var n = 0;
    for (var i = 0; i < boxes.length; i++) if (boxes[i].checked) n++;
    el.textContent = "已勾选 " + n + " 个群";
  }

  /* ---- 人设 ---- */
  var BRAIN_BUILTIN_PERSONA =
    "你是群里一个爱接话、说话简短口语化的成员。不要自称 AI、机器人或助手。" +
    "回复控制在 1-3 句，偶尔用一两个语气词。";

  function brainPersonaGroups() {
    var g = (state.apps.brain.data && state.apps.brain.data.groups) || {};
    // 只列已勾选的群：人设是给"会被接管的群"用的
    return (g.groups || []).filter(function (x) { return x.enabled; });
  }

  function fillBrainPersona(p) {
    var ta = $("brainPersonaDefault");
    if (!ta) return;
    var pd = p || { default: "", groups: {} };
    if (!pd.groups) pd.groups = {};
    state.apps.brain.persona = pd;
    ta.value = pd.default || "";
    var sel = $("brainPersonaGroup");
    if (!sel) return;
    var groups = brainPersonaGroups();
    sel.innerHTML = groups.length
      ? groups.map(function (grp) {
        return '<option value="' + esc(grp.chat_id) + '">' + esc(grp.name) +
          (pd.groups[grp.chat_id] ? " · 已单独设置" : "") + "</option>";
      }).join("")
      : '<option value="">（还没有勾选群聊）</option>';
    fillPersonaForGroup(sel.value);
  }

  function fillPersonaForGroup(chatId) {
    var ta = $("brainPersonaGroupText");
    if (!ta) return;
    var pd = state.apps.brain.persona || {};
    var text = (pd.groups || {})[chatId] || "";
    ta.value = text;
    ta.placeholder = text ? "该群已单独设置，点下面按钮可清空"
      : "留空表示这个群沿用默认人设：" + (pd.default || BRAIN_BUILTIN_PERSONA);
  }

  function brainPersonaMsg(text, ok) {
    var el = $("brainPersonaMsg");
    if (!el) return;
    el.textContent = text;
    el.className = "brain-cfg-msg" + (ok ? " ok" : " bad");
  }

  function postPersona(body, okText) {
    brainPersonaMsg("保存中…", true);
    api("/api/brain/persona", { method: "POST", body: JSON.stringify(body), detail: true })
      .then(function () {
        brainPersonaMsg(okText, true);
        toast(okText);
        brainClearDirty();
        state.apps.brain.data = null;
        loadBrain(true);
      }).catch(function (err) {
        brainPersonaMsg("保存失败：" + ((err && err.message) || err), false);
      });
  }

  function savePersonaDefault() {
    var ta = $("brainPersonaDefault");
    if (!ta) return;
    var text = String(ta.value || "").trim();
    if (!text && !confirm("默认人设留空会回退到内置模板，确定保存？")) return;
    postPersona({ "default": text }, "默认人设已保存，下一轮决策生效");
  }

  function savePersonaGroup() {
    var sel = $("brainPersonaGroup"), ta = $("brainPersonaGroupText");
    if (!sel || !ta) return;
    if (!sel.value) { brainPersonaMsg("请先在白名单里勾选群聊", false); return; }
    var text = String(ta.value || "").trim();
    postPersona({ chat_id: sel.value, persona: text },
      text ? "该群人设已保存" : "已改回使用默认人设");
  }

  function clearPersonaGroup() {
    var sel = $("brainPersonaGroup"), ta = $("brainPersonaGroupText");
    if (!sel || !sel.value) { brainPersonaMsg("请先选择一个群", false); return; }
    if (ta) ta.value = "";
    postPersona({ chat_id: sel.value, persona: "" }, "该群已改回使用默认人设");
  }

  function resetPersonaDefault() {
    var ta = $("brainPersonaDefault");
    if (!ta) return;
    ta.value = BRAIN_BUILTIN_PERSONA;
    brainPersonaMsg("已填入内置模板，记得点「保存默认人设」", true);
  }
  /* ---- 接话节奏（开场概率 / 冷却 / 上限 / 跟进） ---- */
  var BRAIN_TUNING_DEFAULTS = {
    reply_probability: 0.28, cooldown_seconds: 90, max_replies_per_10min: 6,
    follow_window_seconds: 180, follow_probability: 0.85,
    follow_max_turns: 4, follow_cooldown_seconds: 6
  };
  var BRAIN_TUNING_LIMITS = {
    reply_probability: [0, 1],
    cooldown_seconds: [0, 86400],
    max_replies_per_10min: [1, 1000000],
    follow_window_seconds: [0, 3600],
    follow_probability: [0, 1],
    follow_max_turns: [1, 20],
    follow_cooldown_seconds: [0, 600]
  };
  var BRAIN_TUNING_FIELDS = [
    { key: "reply_probability", id: "brainTuneProb", label: "主动接话概率", isInt: false },
    { key: "cooldown_seconds", id: "brainTuneCooldown", label: "冷却秒数", isInt: true },
    { key: "max_replies_per_10min", id: "brainTuneMax", label: "10 分钟上限", isInt: true },
    { key: "follow_window_seconds", id: "brainTuneFollowWindow", label: "跟进窗口（秒）", isInt: true },
    { key: "follow_probability", id: "brainTuneFollowProb", label: "跟进时的接话概率", isInt: false },
    { key: "follow_max_turns", id: "brainTuneFollowTurns", label: "一段对话最多连说几句", isInt: true },
    { key: "follow_cooldown_seconds", id: "brainTuneFollowCooldown", label: "跟进冷却（秒）", isInt: true }
  ];

  /* 服务端会返回 limits；拿不到就用上面的兜底，保证提示和后端校验一致 */
  function brainTuneLimits(key) {
    var t = state.apps.brain && state.apps.brain.tuning;
    var lim = t && t.limits && t.limits[key];
    if (lim && lim.length === 2) return lim;
    return BRAIN_TUNING_LIMITS[key] || [0, 0];
  }

  function fillBrainTuning(t) {
    var el = $("brainTuneProb");
    if (!el) return;
    var d = t || { values: BRAIN_TUNING_DEFAULTS };
    var v = d.values || BRAIN_TUNING_DEFAULTS;
    state.apps.brain.tuning = d;
    for (var i = 0; i < BRAIN_TUNING_FIELDS.length; i++) {
      var f = BRAIN_TUNING_FIELDS[i], e = $(f.id);
      var val = v[f.key];
      if (e) e.value = (val == null ? BRAIN_TUNING_DEFAULTS[f.key] : val);
    }
    brainTuningMsg(d.source === "web"
      ? "当前生效：网页设置（tuning.local.json）"
      : "当前生效：config.local.json（网页保存一次后以网页为准）", true);
  }

  function brainTuningMsg(text, ok) {
    var el = $("brainTuningMsg");
    if (!el) return;
    el.textContent = text;
    el.className = "brain-cfg-msg" + (ok ? " ok" : " bad");
  }

  function saveBrainTuning() {
    var payload = {};
    for (var i = 0; i < BRAIN_TUNING_FIELDS.length; i++) {
      var f = BRAIN_TUNING_FIELDS[i];
      var lim = brainTuneLimits(f.key);
      var num = f.isInt ? parseInt(apValue(f.id), 10) : parseFloat(apValue(f.id));
      if (isNaN(num) || num < lim[0] || num > lim[1]) {
        brainTuningMsg(f.label + "要填 " + lim[0] + " ~ " + lim[1] +
          (f.isInt ? " 的整数" : " 之间的小数"), false);
        return;
      }
      payload[f.key] = num;
    }
    brainTuningMsg("保存中…", true);
    api("/api/brain/tuning", {
      method: "POST",
      body: JSON.stringify(payload),
      detail: true
    }).then(function (r) {
      brainTuningMsg(r.hint || "已保存，下一轮生效", true);
      toast("接话节奏已保存");
      brainClearDirty();
      state.apps.brain.data = null;
      loadBrain(true);
    }).catch(function (err) {
      brainTuningMsg("保存失败：" + ((err && err.message) || err), false);
    });
  }

  function resetBrainTuning() {
    var d = BRAIN_TUNING_DEFAULTS;
    for (var i = 0; i < BRAIN_TUNING_FIELDS.length; i++) {
      var f = BRAIN_TUNING_FIELDS[i], e = $(f.id);
      if (e) e.value = d[f.key];
    }
    brainTuningMsg("已填回默认值（开场 " + d.reply_probability + " / 冷却 " + d.cooldown_seconds +
      "s / 上限 " + d.max_replies_per_10min + " / 跟进 " + d.follow_probability + " × " +
      d.follow_max_turns + " 句 / 窗口 " + d.follow_window_seconds + "s），点「保存节奏」才生效", true);
  }

  function filterBrainGroups(keyword) {
    var q = (keyword || "").trim().toLowerCase();
    var items = document.querySelectorAll("#brainGroupsBox .brain-group-item");
    for (var i = 0; i < items.length; i++) {
      var name = items[i].getAttribute("data-name") || "";
      items[i].style.display = (!q || name.indexOf(q) >= 0) ? "" : "none";
    }
  }
  function setAllBrainGroups(checked) {
    var items = document.querySelectorAll("#brainGroupsBox .brain-group-item");
    for (var i = 0; i < items.length; i++) {
      if (items[i].style.display === "none") continue;
      var cb = items[i].querySelector("input[type=checkbox]");
      if (cb) cb.checked = checked;
    }
    updateBrainGroupCount();
  }
  function saveBrainGroups() {
    var boxes = document.querySelectorAll("#brainGroupsBox .brain-group-item input[type=checkbox]");
    var enabled = [], names = {};
    for (var i = 0; i < boxes.length; i++) {
      if (!boxes[i].checked) continue;
      var cid = boxes[i].value;
      enabled.push(cid);
      var label = boxes[i].closest(".brain-group-item");
      var nm = label && label.querySelector(".brain-group-name");
      if (nm) names[cid] = nm.textContent;
    }
    brainGroupsMsg("正在保存…", true);
    api("/api/brain/groups", { method: "POST", body: JSON.stringify({ enabled: enabled, names: names }), detail: true })
      .then(function (r) {
        brainGroupsMsg("已保存：" + (r.selected || []).length + " 个群启用大脑", true);
        toast("大脑群聊白名单已保存");
        brainClearDirty();
      }).catch(function (err) { brainGroupsMsg("保存失败：" + (err.message || err), false); });
  }

  /* ---------------- 设置弹层 ---------------- */
  function openSettings() {
    stopRelogin();
    api("/api/social/status").then(function (s) {
      api("/api/social/state").then(function (st) {
        api("/api/config").then(function (cfg) {
          renderSettings(s, st, cfg);
          $("settingsPanel").classList.remove("hidden");
        }).catch(function () { renderSettings(s, st, null); $("settingsPanel").classList.remove("hidden"); });
      }).catch(function () { renderSettings(s, null, null); $("settingsPanel").classList.remove("hidden"); });
    }).catch(function () { renderSettings(null, null, null); $("settingsPanel").classList.remove("hidden"); });
  }

  function renderSettings(s, st, cfg) {
    // online 优先取真正的微信守护健康状态（/api/social/state 顶层 online），
    // 兼容旧字段 st.status.online，避免一直误显「离线」。
    var online = st && (st.online || (st.status && st.status.online));
    var line = function (k, v, ok) {
      return '<div class="stat-line"><span>' + esc(k) + '</span><b class="' +
        (ok === true ? "ok" : ok === false ? "bad" : "") + '">' + esc(v) + '</b></div>';
    };
    var html = "";
    html += line("微信状态", online ? "在线" : (st ? "离线" : "未知"), online);
    if (!online && st && st.wechat_detail) {
      html += line("状态说明", st.wechat_detail);
    }
    if (s) {
      html += line("最近同步", s.last_sync || "—", s.last_sync);
      html += line("群聊 / 成员", (s.groups || 0) + " / " + (s.members || 0));
      html += line("好友 / 朋友圈", (s.friends || 0) + " / " + (s.moments || 0));
      html += line("微信版本", s.version || "—");
      if (s.account) html += line("当前账号", s.account);
      if (s.error) html += line("同步错误", s.error, false);
    }
    if (cfg) {
      html += '<div class="section-title">采集配置</div>';
      html += line("仅群聊文本", cfg.only_groups ? "是" : "否");
      html += line("含自己消息", cfg.include_self ? "是" : "否");
      html += line("采集公众号", cfg.collect_official ? "是" : "否");
      if (cfg.token_hint) html += line("令牌", cfg.token_hint);
    }
    html += '<div class="settings-actions">' +
      '<button class="btn" data-act="set-sync">立即同步资料</button>' +
      '<button class="btn" data-act="set-moments">刷新朋友圈</button>' +
      '<button class="btn" data-act="set-relogin">重新登录微信</button>' +
      '<button class="btn" data-act="set-logout">退出登录</button></div>';
    html += '<div id="reloginBox"></div>';
    html += '<div class="note">数据均为本机只读镜像；同步会从微信内存读取群成员、好友与朋友圈缓存，不会向微信发送任何内容。修改采集配置需在桥接控制台 reload report_bridge 后生效。「重新登录微信」只读取本机微信窗口的二维码，仍需本人用手机确认。</div>';
    $("settingsBody").innerHTML = html;
  }

  function syncNow(only) {
    api("/api/social/sync", { method: "POST", body: JSON.stringify({ only: only }) })
      .then(function () { toast(only === "moments" ? "已开始刷新朋友圈" : "已开始同步资料"); });
  }

  /* ---------------- 微信重新登录二维码 ---------------- */
  var reloginTimer = null;

  function stopRelogin() {
    if (reloginTimer) { clearTimeout(reloginTimer); reloginTimer = null; }
  }

  function reloginBox(html) {
    var box = $("reloginBox");
    if (box) box.innerHTML = html;
  }

  function pollRelogin(id) {
    if ($("settingsPanel").classList.contains("hidden")) { stopRelogin(); return; }
    api("/api/wechat-login/status?attempt_id=" + encodeURIComponent(id))
      .then(function (r) {
        if (!r || !r.ok) {
          reloginBox('<div class="note">' + esc((r && r.error) || "查询失败") + "</div>");
          stopRelogin(); return;
        }
        var d = r.data || {};
        if (d.status === "logged_in") {
          stopRelogin();
          reloginBox('<div class="note">登录成功（' + esc(d.wxid || "") +
            "），桥接会在数秒内自动恢复，可关闭本窗口。</div>");
          return;
        }
        if (d.status === "exited") {
          reloginBox('<div class="note">微信进程已退出，请在电脑上打开微信后重试。</div>');
          stopRelogin(); return;
        }
        if (d.status === "qr_available" && d.qrcode_base64) {
          reloginBox('<div style="text-align:center;padding:8px">' +
            '<img alt="微信登录二维码" style="width:210px;height:210px" ' +
            'src="data:image/png;base64,' + d.qrcode_base64 + '"></div>' +
            '<div class="note">用手机微信「扫一扫」并确认登录。二维码约 5 分钟内有效。</div>');
        } else {
          reloginBox('<div class="note">等待微信显示登录二维码…（' +
            esc(d.status || "starting") + "）</div>");
        }
        reloginTimer = setTimeout(function () { pollRelogin(id); }, 2000);
      })
      .catch(function (e) {
        reloginBox('<div class="note">查询失败：' + esc(String(e)) + "</div>");
        stopRelogin();
      });
  }

  function startRelogin() {
    stopRelogin();
    reloginBox('<div class="note">正在连接本机微信进程…</div>');
    api("/api/wechat-login/attach", { method: "POST", body: "{}" })
      .then(function (r) {
        if (!r || !r.ok) {
          var msg = (r && r.error) || "启动失败";
          if (msg.indexOf("未登录的微信进程") >= 0) msg = "当前微信已在线，无需重新登录。";
          reloginBox('<div class="note">' + esc(msg) +
            '</div><div class="settings-actions">' +
            '<button class="btn" data-act="set-relogin">重试</button></div>');
          return;
        }
        reloginBox('<div class="note">已连接微信进程 PID ' + esc(String(r.pid)) +
          "，正在读取二维码…</div>");
        pollRelogin(r.attempt_id);
      })
      .catch(function (e) {
        reloginBox('<div class="note">启动失败：' + esc(String(e)) + "</div>");
      });
  }

  /* ---------------- 列表宽度拖拽 ---------------- */
  function initResizer() {
    var resizer = $("listResizer");
    var list = $("list");
    if (!resizer || !list) return;
    var MIN = 120, MAX = 320, DEF = 180;
    // 换一个存储 key，忽略之前所有被拉得过宽的旧值，立即回到正常宽度。
    // 真正的宽度失控已由 style.css 的 .list { min-width:0 } 修好，这里只管用户主动拖拽的偏好。
    var KEY = "listWidth4";
    var root = document.documentElement;
    function apply(w) {
      w = Math.max(MIN, Math.min(MAX, Math.round(w)));
      root.style.setProperty("--list-w", w + "px");
      return w;
    }
    var saved = parseInt(localStorage.getItem(KEY) || "", 10);
    apply(saved >= MIN && saved <= MAX ? saved : DEF);
    var dragging = false, startX = 0, startW = 0, pid = null;
    function down(e) {
      if (e.button != null && e.button !== 0) return;
      dragging = true;
      startX = e.clientX;
      var cssw = parseInt(getComputedStyle(root).getPropertyValue("--list-w"), 10);
      startW = (!isNaN(cssw) && cssw > 0) ? cssw : list.getBoundingClientRect().width;
      pid = (e.pointerId != null) ? e.pointerId : null;
      resizer.classList.add("dragging");
      document.body.classList.add("col-resizing");
      if (pid != null && resizer.setPointerCapture) { try { resizer.setPointerCapture(pid); } catch (_) {} }
      if (e.preventDefault) e.preventDefault();
    }
    function move(e) {
      if (!dragging) return;
      apply(startW + (e.clientX - startX));
      if (e.preventDefault) e.preventDefault();
    }
    function up() {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      if (pid != null && resizer.releasePointerCapture) { try { resizer.releasePointerCapture(pid); } catch (_) {} }
      var w = parseInt(root.style.getPropertyValue("--list-w") || "", 10);
      if (w >= MIN && w <= MAX) localStorage.setItem(KEY, String(w));
    }
    if (window.PointerEvent) {
      resizer.addEventListener("pointerdown", down);
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
    } else {
      resizer.addEventListener("mousedown", down);
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    }
    // 双击手柄复位到默认宽度
    resizer.addEventListener("dblclick", function () {
      apply(DEF); localStorage.setItem(KEY, String(DEF));
    });
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    initResizer();
    document.querySelectorAll(".rail-btn[data-nav]").forEach(function (b) {
      b.addEventListener("click", function () { setNav(b.dataset.nav); });
    });
    $("btnSettings").addEventListener("click", function () {
      if ($("settingsPanel").classList.contains("hidden")) openSettings();
      else $("settingsPanel").classList.add("hidden");
    });
    $("settingsClose").addEventListener("click", function () { $("settingsPanel").classList.add("hidden"); });
    document.querySelectorAll("#contactTabs button").forEach(function (b) {
      b.addEventListener("click", function () { setContactTab(b.dataset.ctab); });
    });
    $("searchInput").addEventListener("input", function (e) {
      state.search = e.target.value; renderList();
    });
    $("listBody").addEventListener("click", function (e) {
      var li = e.target.closest("[data-chat]");
      if (li) { openChat(li.dataset.chat); if (window.innerWidth <= 560) $("app").classList.add("show-main"); }
    });
    $("main").addEventListener("click", function (e) {
      var act = e.target.closest("[data-act]");
      if (act) {
        var a = act.dataset.act;
        if (a === "chat-info") return openProfile(state.current);
        if (a === "chat-emoji") { state.chat.emojiOpen = !state.chat.emojiOpen; renderChat($("main")); return; }
        if (a === "chat-image") { toast("图片发送将在下一版开放"); return; }
        if (a === "moments-refresh") { syncNow("moments"); loadMoments(); return; }
        if (a === "profile-send") { hideModal(); openChat(state.current); return; }
        if (a === "apps-refresh") {
          state.apps.data = null; state.apps.push.data = null; state.apps.brain.data = null;
          loadReminders(false); loadArticlePush(false); loadBrain(false);
          renderApps($("main")); return;
        }
        if (a === "apps-back") {
          if (state.apps.view === "brain" && brainHasDirty() &&
            !confirm("有未保存的修改，离开将丢失。继续？")) return;
          brainClearDirty();
          state.apps.view = "home"; renderApps($("main")); updateAppMeta(); return;
        }
        if (a === "brain-tab") { switchBrainTab(act.dataset.tab); return; }
        if (a === "brain-refresh") {
          if (brainHasDirty() && !confirm("有未保存的修改，刷新将丢失。继续？")) return;
          brainClearDirty();
          state.apps.brain.data = null; state.apps.brain.logs = null;
          renderApps($("main")); loadBrain(false); loadBrainLogs(false); return;
        }
        if (a === "brain-log-refresh") { loadBrainLogs(false); return; }
        if (a === "brain-log-clear") { clearBrainLogs(); return; }
        if (a === "brain-rt-log") { loadBrainRuntimeLog(); return; }
        if (a === "brain-save") { saveBrainConfig(); return; }
        if (a === "brain-test") { testBrainConfig(); return; }
        if (a === "brain-runtime-save") { saveBrainRuntime(); return; }
        if (a === "brain-groups-save") { saveBrainGroups(); return; }
        if (a === "brain-persona-save") { savePersonaDefault(); return; }
        if (a === "brain-persona-group-save") { savePersonaGroup(); return; }
        if (a === "brain-persona-group-clear") { clearPersonaGroup(); return; }
        if (a === "brain-persona-reset") { resetPersonaDefault(); return; }
        if (a === "brain-tuning-save") { saveBrainTuning(); return; }
        if (a === "brain-tuning-reset") { resetBrainTuning(); return; }
        if (a === "brain-grp-all") { setAllBrainGroups(true); return; }
        if (a === "brain-grp-none") { setAllBrainGroups(false); return; }
        if (a === "ap-save") { saveArticlePush(); return; }
        if (a === "ap-test") { testArticlePush(); return; }
        if (a === "ap-refresh") { loadArticlePush(false); return; }
        if (a === "ap-clear-token") {
          state.apps.push.clearToken = true;
          var tk = $("apToken"); if (tk) tk.value = "";
          toast("已标记清除令牌，点「保存配置」生效"); return;
        }
        if (a.indexOf("ap-act:") === 0) { doArticlePushAction(a.slice(7)); return; }
        if (a === "rem-refresh") { loadReminders(false); return; }
        if (a === "rem-clear") {
          state.apps.filters = { status: "", kind: "", delivery: "", q: "" };
          renderRemindersPage($("main")); loadReminders(false); return;
        }
      }
      var appCard = e.target.closest("[data-app]");
      if (appCard && appCard.dataset.app === "reminders") { openReminderApp(); return; }
      if (appCard && appCard.dataset.app === "article-push") { openArticlePushApp(); return; }
      if (appCard && appCard.dataset.app === "brain") { openBrainApp(); return; }
      var ract = e.target.closest("[data-ract]");
      if (ract) { doReminderAction(ract.dataset.rid, ract.dataset.ract); return; }
      var emoji = e.target.closest("[data-emoji]");
      if (emoji) {
        var box = $("chatInput"); if (!box) return;
        var s = box.selectionStart, v = box.value;
        box.value = v.slice(0, s) + emoji.dataset.emoji + v.slice(box.selectionEnd);
        box.focus(); box.selectionStart = box.selectionEnd = s + emoji.dataset.emoji.length;
        return;
      }
      var member = e.target.closest(".member");
      if (member) { openProfile(member.dataset.wxid); return; }
    });
    $("main").addEventListener("input", function (e) {
      var pane = e.target.closest && e.target.closest(".brain-pane");
      if (pane && state.apps.view === "brain" && !brainIsFilterField(e.target)) {
        brainMarkDirty(pane.dataset.pane);
      }
      if (e.target.id === "chatInput") state.chat.draft = e.target.value;
      if (["apUrl", "apParam", "apMode", "apTimeout", "apToken",
        "apEnabled", "apShared"].indexOf(e.target.id) >= 0) {
        state.apps.push.formDirty = true;
      }
      if (e.target.id === "remSearch") {
        state.apps.filters.q = e.target.value;
        clearTimeout(state.apps.searchTimer);
        state.apps.searchTimer = setTimeout(function () { loadReminders(true); }, 300);
      }
    });
    $("main").addEventListener("change", function (e) {
      var id = e.target.id;
      var dirtyPane = e.target.closest && e.target.closest(".brain-pane");
      if (dirtyPane && state.apps.view === "brain" && !brainIsFilterField(e.target)) {
        brainMarkDirty(dirtyPane.dataset.pane);
      }
      if (["apMode", "apEnabled", "apShared"].indexOf(id) >= 0) {
        state.apps.push.formDirty = true; return;
      }
      if (id === "remStatus") state.apps.filters.status = e.target.value;
      else if (id === "remKind") state.apps.filters.kind = e.target.value;
      else if (id === "remDelivery") state.apps.filters.delivery = e.target.value;
      else if (id === "brainLogChat") {
        state.apps.brain.logChat = e.target.value;
        loadBrainLogs(false);
        return;
      }
      else if (id === "brainPersonaGroup") { fillPersonaForGroup(e.target.value); return; }
      else return;
      loadReminders(false);
    });
    $("main").addEventListener("keydown", function (e) {
      if (e.target.id === "chatInput" && e.key === "Enter" && !e.shiftKey) {
        e.preventDefault(); sendMessage();
        return;
      }
      // 顶部 tab 的键盘导航：左右切换，Home/End 跳首尾
      var tabEl = e.target.closest && e.target.closest(".brain-tab");
      if (!tabEl) return;
      if (["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(e.key) < 0) return;
      var all = Array.prototype.slice.call(
        document.querySelectorAll("#brainTabs .brain-tab"));
      if (!all.length) return;
      var i = all.indexOf(tabEl);
      if (i < 0) return;
      if (e.key === "ArrowLeft") i = (i - 1 + all.length) % all.length;
      else if (e.key === "ArrowRight") i = (i + 1) % all.length;
      else if (e.key === "Home") i = 0;
      else i = all.length - 1;
      e.preventDefault();
      switchBrainTab(all[i].dataset.tab);
      all[i].focus();
    });
    $("main").addEventListener("click", function (e) {
      if (e.target.id === "btnChatSend") sendMessage();
    });
    $("modalClose").addEventListener("click", hideModal);
    $("modal").addEventListener("click", function (e) { if (e.target === $("modal")) hideModal(); });
    $("settingsBody").addEventListener("click", function (e) {
      var a = e.target.closest("[data-act]"); if (!a) return;
      if (a.dataset.act === "set-sync") syncNow("auto");
      else if (a.dataset.act === "set-moments") { syncNow("moments"); loadMoments(); }
      else if (a.dataset.act === "set-relogin") startRelogin();
      else if (a.dataset.act === "set-logout") {
        api("/api/logout", { method: "POST" }).catch(function () {});
        sessionStorage.removeItem(TOKEN_KEY);
        $("settingsPanel").classList.add("hidden");
        state.current = null; state.conversations = [];
        state.contacts = { friend: [], group: [], official: [] };
        state.contactsLoaded = { friend: false, group: false, official: false };
        state.moments = null; showLogin();
      }
    });
    $("main").addEventListener("input", function (e) {
      if (e.target.id === "brainGroupSearch") filterBrainGroups(e.target.value);
    });
    $("main").addEventListener("change", function (e) {
      if (e.target.closest("#brainGroupsBox .brain-group-item")) updateBrainGroupCount();
    });
    $("btnLogin").addEventListener("click", doLogin);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { hideModal(); $("settingsPanel").classList.add("hidden"); }
    });
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    if (state.nav) setNav(state.nav);
    loadConversations();
    if (state.contactTab) loadContacts(state.contactTab);
    loadMoments();
    setInterval(loadConversations, 60000);
  }

  function start() {
    bind();
    ensureAuth().then(function (ok) { if (ok) boot(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
