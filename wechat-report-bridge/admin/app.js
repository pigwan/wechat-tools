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
      view: "home",                  // home | reminders
      data: null,
      loading: false,
      timer: null,
      filters: { status: "", kind: "", delivery: "", q: "" },
      busy: {}
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
      if (!r.ok) throw new Error("HTTP " + r.status);
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
  }

  function updateAppMeta() {
    var el = $("appReminderMeta");
    if (!el) return;
    var d = state.apps.data;
    if (!d) { el.textContent = "读取中…"; return; }
    el.textContent = "进行中 " + (d.counts.active || 0) + " · 全部 " +
      (d.counts.all || 0) + " 个任务" + (d.worker ? "" : " · 发送模块未就绪");
  }

  function renderApps(main) {
    if (state.apps.view === "reminders") return renderRemindersPage(main);
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
      '<div class="app-card disabled"><div class="app-icon">🧩</div>' +
      '<div class="app-info"><h3>更多应用</h3><p>后续能力会陆续放到这里。</p>' +
      '<div class="app-meta">规划中</div></div></div></div>' +
      '<div class="apps-tip"><b>怎么用：</b>群里发送 <code>@机器人 提醒 30分钟后 交周报</code>' +
      '（可同时 @ 其他人，到期一起提醒），也可以直接私聊机器人发 <code>提醒 30分钟后 交周报</code>。<br>' +
      '<b>在哪发的就在哪回：</b>群里发起的到点在原群回复并 @；私聊发起的到点私信回复本会话，不带 @。<br>' +
      '支持每 5 分钟 / 每小时 / 每天 9点 / 每周一 10点 等循环；<b>5 分钟下限只限制循环两次触发的间隔</b>，' +
      '一次性提醒「3分钟后」也照常支持。</div>' +
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
        key: "feed:" + x.item_key, ts: x.received_at_raw, time: x.received_at_raw,
        self: false, sender: x.sender_name, sender_id: x.sender_id, avatar_url: "",
        kind: x.kind, body: x.body, title: x.title, url: x.url,
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

  /* ---------------- 设置弹层 ---------------- */
  function openSettings() {
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
    var online = st && st.status && st.status.online;
    var line = function (k, v, ok) {
      return '<div class="stat-line"><span>' + esc(k) + '</span><b class="' +
        (ok === true ? "ok" : ok === false ? "bad" : "") + '">' + esc(v) + '</b></div>';
    };
    var html = "";
    html += line("微信状态", online ? "在线" : (st ? "离线" : "未知"), online);
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
      '<button class="btn" data-act="set-logout">退出登录</button></div>';
    html += '<div class="note">数据均为本机只读镜像；同步会从微信内存读取群成员、好友与朋友圈缓存，不会向微信发送任何内容。修改采集配置需在桥接控制台 reload report_bridge 后生效。</div>';
    $("settingsBody").innerHTML = html;
  }

  function syncNow(only) {
    api("/api/social/sync", { method: "POST", body: JSON.stringify({ only: only }) })
      .then(function () { toast(only === "moments" ? "已开始刷新朋友圈" : "已开始同步资料"); });
  }

  /* ---------------- 列表宽度拖拽 ---------------- */
  function initResizer() {
    var resizer = $("listResizer");
    var list = $("list");
    if (!resizer || !list) return;
    var MIN = 120, MAX = 460, DEF = 180;
    var root = document.documentElement;
    function apply(w) {
      w = Math.max(MIN, Math.min(MAX, Math.round(w)));
      root.style.setProperty("--list-w", w + "px");
      return w;
    }
    var saved = parseInt(localStorage.getItem("listWidth") || "", 10);
    if (saved >= MIN && saved <= MAX) apply(saved);
    var dragging = false, startX = 0, startW = 0;
    function down(e) {
      if (e.button != null && e.button !== 0) return;
      dragging = true;
      startX = e.clientX;
      var cssw = parseInt(getComputedStyle(root).getPropertyValue("--list-w"), 10);
      startW = (!isNaN(cssw) && cssw > 0) ? cssw : list.getBoundingClientRect().width;
      resizer.classList.add("dragging");
      document.body.classList.add("col-resizing");
      try {
        if (resizer.setPointerCapture && e.pointerId != null) resizer.setPointerCapture(e.pointerId);
      } catch (_) {}
      if (e.preventDefault) e.preventDefault();
    }
    function move(e) {
      if (!dragging) return;
      apply(startW + (e.clientX - startX));
      if (e.preventDefault) e.preventDefault();
    }
    function up(e) {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      try {
        if (resizer.releasePointerCapture && e && e.pointerId != null) resizer.releasePointerCapture(e.pointerId);
      } catch (_) {}
      var w = parseInt(root.style.getPropertyValue("--list-w") || "", 10);
      if (w >= MIN && w <= MAX) localStorage.setItem("listWidth", String(w));
    }
    if (window.PointerEvent) {
      resizer.addEventListener("pointerdown", down);
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    } else {
      resizer.addEventListener("mousedown", down);
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    }
    // 双击手柄复位到默认宽度
    resizer.addEventListener("dblclick", function () {
      apply(DEF); localStorage.setItem("listWidth", String(DEF));
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
        if (a === "apps-refresh") { state.apps.data = null; loadReminders(false); renderApps($("main")); return; }
        if (a === "apps-back") { state.apps.view = "home"; renderApps($("main")); updateAppMeta(); return; }
        if (a === "rem-refresh") { loadReminders(false); return; }
        if (a === "rem-clear") {
          state.apps.filters = { status: "", kind: "", delivery: "", q: "" };
          renderRemindersPage($("main")); loadReminders(false); return;
        }
      }
      var appCard = e.target.closest("[data-app]");
      if (appCard && appCard.dataset.app === "reminders") { openReminderApp(); return; }
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
      if (e.target.id === "chatInput") state.chat.draft = e.target.value;
      if (e.target.id === "remSearch") {
        state.apps.filters.q = e.target.value;
        clearTimeout(state.apps.searchTimer);
        state.apps.searchTimer = setTimeout(function () { loadReminders(true); }, 300);
      }
    });
    $("main").addEventListener("change", function (e) {
      var id = e.target.id;
      if (id === "remStatus") state.apps.filters.status = e.target.value;
      else if (id === "remKind") state.apps.filters.kind = e.target.value;
      else if (id === "remDelivery") state.apps.filters.delivery = e.target.value;
      else return;
      loadReminders(false);
    });
    $("main").addEventListener("keydown", function (e) {
      if (e.target.id === "chatInput" && e.key === "Enter" && !e.shiftKey) {
        e.preventDefault(); sendMessage();
      }
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
