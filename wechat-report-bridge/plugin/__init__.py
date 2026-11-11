"""群消息采集插件：本地缓冲、批量上报远端、群内报告回复。

部署方式：
  把本目录整个复制为 helper 项目的 plugins/report_bridge/，
  再按 config.example.json 在 bot_config.json 中填写 plugins.report_bridge 段。

设计要点：
  - 群消息量大，逐条实时 POST 会压垮 4 GB 内存的机器，因此先落本地 SQLite，
    按条数或时间间隔批量上报；上报失败保留 synced=0，下次自动重试。
  - 每条消息带 msg_key（SHA-256），远端用 UNIQUE 约束做幂等去重。
  - 链接单独归档：文本里粘贴的网址用正则提取；分享卡片（type 49）通过包装
    原生 monitor_handler 拿到 XML，只保留标题/URL/摘要/来源，绝不采集 CDN
    地址、aeskey、filekey 等凭据。两类都写 links 表并随消息一起批量上报。
  - 公众号消息单独归档：helper 的 TextRouter.convert 会直接丢弃 gh_ 开头的
    会话，公众号推送连 monitor_handler 都到不了。因此在更底层包装原生
    _map_message，在 convert 之前只读旁路解析公众号图文（含一条推送里的
    多篇文章）与文字消息，写入 official 表；群聊里分享的公众号文章也归入
    同一张表。只保留公开展示字段，绝不采集任何凭据或私有地址。
  - 本地库同时用于即时群内简报，不依赖远端是否在线。
  - 插件只使用 Python 标准库，不给本机增加额外依赖。
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_key     TEXT NOT NULL UNIQUE,
    chat_id     TEXT NOT NULL,
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    is_group    INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_LINKS_TABLE = """
CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link_key    TEXT NOT NULL UNIQUE,
    chat_id     TEXT NOT NULL,
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    is_group    INTEGER NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL DEFAULT 'card',
    app_type    TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_OFFICIAL_TABLE = """
CREATE TABLE IF NOT EXISTS official (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    official_key  TEXT NOT NULL UNIQUE,
    chat_id       TEXT NOT NULL,
    publisher_id  TEXT NOT NULL DEFAULT '',
    publisher_name TEXT NOT NULL DEFAULT '',
    sender_id     TEXT NOT NULL DEFAULT '',
    sender_name   TEXT NOT NULL DEFAULT '',
    is_group      INTEGER NOT NULL DEFAULT 0,
    scene         TEXT NOT NULL DEFAULT 'push',
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    digest        TEXT NOT NULL DEFAULT '',
    cover_url     TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL DEFAULT '',
    received_at   TEXT NOT NULL,
    synced        INTEGER NOT NULL DEFAULT 0
)
"""

# ---- 链接 / 公众号解析（只保留公开展示字段，绝不携带 CDN 地址、aeskey 等凭据）----
_MAX_XML_BYTES = 64 * 1024
_MAX_FIELD = 512
_URL_RE = re.compile(r"https?://[^\s<>\"'\u3000-\u303f\uff00-\uffef]+", re.I)
_WWW_RE = re.compile(r"(?i)(?<![a-z0-9.])www\.[a-z0-9.-]+\.[a-z]{2,}[^\s<>\"'\u3000-\u303f\uff00-\uffef]*")
_TRAILING = ".,;:!?)]}>\u3001\u3002\uff0c\uff0e\uff09\u300b\u3011\u201d\u2019'\""
# 公众号正文与封面的域名白名单；其它域名一律不采集
_OFFICIAL_HOSTS = frozenset(("mp.weixin.qq.com",))
_COVER_HOSTS = frozenset(("mmbiz.qpic.cn", "mmbiz.qlogo.cn", "mmhead.qpic.cn"))
_GH_RE = re.compile(r"^gh_[A-Za-z0-9_.-]{1,124}$")
# 订阅号聚合/系统会话，helper 的 TextRouter 也会丢弃它们
_SYSTEM_PEERS = frozenset(("weixin", "fmessage", "newsapp", "floatbottle",
                           "brandsessionholder", "medianote"))


def _clean(value, limit=_MAX_FIELD):
    text = str(value or "").strip()
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)[:limit]


def safe_link_url(value, hosts=None):
    """只放行公网 http(s) 链接，去掉 fragment，过滤本机/内网地址。

    hosts 给出时额外要求 hostname 命中白名单（用于公众号文章与封面）。
    """
    text = _clean(value, 2048)
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower().strip("[]")
    if (not host or host in ("localhost", "0.0.0.0", "::1")
            or host.startswith(("127.", "10.", "192.168.", "169.254."))):
        return ""
    if hosts is not None and host not in hosts:
        return ""
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc,
                                    parts.path, parts.query, ""))


def parse_link_card(raw):
    """解析微信 type 49 分享卡片，返回脱敏字段；非链接/非法 XML 返回 None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    if len(raw.encode("utf-8", "replace")) > _MAX_XML_BYTES:
        return None
    upper = raw.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return None
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        # 群消息的 XML 偶尔带 "wxid_xxx:\n" 之类的前缀；从第一个标签处重试
        start = min((pos for pos in (raw.find("<msg"), raw.find("<appmsg")) if pos >= 0),
                    default=-1)
        if start < 0:
            return None
        try:
            root = ET.fromstring(raw[start:])
        except (ET.ParseError, ValueError):
            return None
    app = next(root.iter("appmsg"), None)
    if app is None:
        return None

    def field(name):
        node = app.find(name)
        return _clean(node.text if node is not None else "")

    app_type = field("type")
    # type 6 是文件附件、19 是合并转发、2000 是转账，都不是链接卡片
    if app_type in ("6", "19", "2000"):
        return None
    url = safe_link_url(field("url")) or safe_link_url(field("lowurl"))
    if not url:
        return None
    title = field("title")
    return {
        "app_type": app_type,
        "title": title or url,
        "url": url,
        "description": field("des") or field("description"),
        "source": field("sourcename") or field("appname"),
    }


def extract_text_urls(text):
    """从文本消息里提取网址，按出现顺序去重。"""
    found = []
    for pattern in (_URL_RE, _WWW_RE):
        for match in pattern.finditer(text or ""):
            raw = match.group(0).rstrip(_TRAILING)
            if raw.lower().startswith("www."):
                raw = "https://" + raw
            url = safe_link_url(raw)
            if url and url not in found:
                found.append(url)
    return found[:10]


def _node_text(node, name):
    """只读白名单字段：先找直接子节点，再兜底找同名后代。"""
    child = node.find(name) if node is not None else None
    if child is None and node is not None:
        child = next(node.iter(name), None)
    return _clean(child.text if child is not None else "")


def _pub_time(value):
    """公众号 pub_time 可能是 Unix 秒/毫秒，也可能是现成的日期串。"""
    text = _clean(value, 32)
    if not text:
        return ""
    if text.isdigit() and len(text) in (10, 13):
        try:
            seconds = int(text) / (1000 if len(text) == 13 else 1)
            return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            return ""
    return text[:32]


def parse_official_message(raw, fallback_publisher=""):
    """解析公众号图文消息（单篇 / 一条推送里的多篇），返回脱敏字段。

    只读取 title/url/digest/cover/pub_time/appname 等公开展示字段；
    cdn*、aeskey、filekey、native_url 等凭据与私有地址一律不采集。
    非公众号文章、非法 XML 或无有效文章时返回 None。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    if len(raw.encode("utf-8", "replace")) > _MAX_XML_BYTES:
        return None
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        return None
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        start = min((pos for pos in (raw.find("<msg"), raw.find("<appmsg")) if pos >= 0),
                    default=-1)
        if start < 0:
            return None
        try:
            root = ET.fromstring(raw[start:])
        except (ET.ParseError, ValueError):
            return None
    app = next(root.iter("appmsg"), None)
    if app is None:
        return None
    if _node_text(app, "type") in ("6", "19", "2000"):
        return None

    # 公众号身份：优先 XML 里的 fromusername / username，其次原生消息里的 gh_ 会话
    publisher_id = ""
    for candidate in (_node_text(root, "fromusername"),
                      _node_text(app, "username"), str(fallback_publisher or "")):
        if _GH_RE.match(candidate):
            publisher_id = candidate
            break

    publisher_name = ""
    for info in root.iter("appinfo"):
        publisher_name = _node_text(info, "appname")
        if publisher_name:
            break
    if not publisher_name:
        reader = next(root.iter("mmreader"), None)
        category = next(reader.iter("category"), None) if reader is not None else None
        if category is not None:
            publisher_name = _clean(category.get("name"))
    if not publisher_name:
        publisher_name = (_node_text(app, "sourcename")
                          or _node_text(app, "sourcedisplayname")
                          or _node_text(app, "appname"))

    articles = []
    mm = next(root.iter("mmreader"), None)
    nodes = list(mm.iter("item")) if mm is not None else []
    if not nodes:
        nodes = [app]           # 单篇图文：字段直接在 appmsg 下
    for node in nodes[:20]:
        url = ""
        for name in ("url", "longurl", "shorturl"):
            url = safe_link_url(_node_text(node, name), _OFFICIAL_HOSTS)
            if url:
                break
        if not url:
            continue
        cover = ""
        for name in ("cover", "cover_235_1", "cover_1_1", "thumburl"):
            cover = safe_link_url(_node_text(node, name), _COVER_HOSTS)
            if cover:
                break
        article = {
            "title": _node_text(node, "title") or url,
            "url": url,
            "digest": _node_text(node, "digest") or _node_text(node, "des"),
            "cover_url": cover,
            "published_at": _pub_time(_node_text(node, "pub_time")),
        }
        if article["url"] not in [a["url"] for a in articles]:
            articles.append(article)
    if not articles:
        return None
    return {"publisher_id": publisher_id, "publisher_name": publisher_name[:128],
            "articles": articles}


_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    at_list     TEXT NOT NULL DEFAULT '',
    at_names    TEXT NOT NULL DEFAULT '',
    status      INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
)
"""

# 定时提醒任务：放在 send_outbox.sqlite3，管理台与插件 worker 共享同一个库
_REMINDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       TEXT NOT NULL,
    chat_name     TEXT NOT NULL DEFAULT '',
    creator_id    TEXT NOT NULL DEFAULT '',
    creator_name  TEXT NOT NULL DEFAULT '',
    targets       TEXT NOT NULL DEFAULT '',
    target_names  TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL,
    raw_text      TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'once',
    rule_json     TEXT NOT NULL DEFAULT '{}',
    rule_text     TEXT NOT NULL DEFAULT '',
    interval_sec  INTEGER NOT NULL DEFAULT 0,
    weekdays      TEXT NOT NULL DEFAULT '',
    at_hhmm       TEXT NOT NULL DEFAULT '',
    next_ts       REAL NOT NULL,
    last_fire_ts  REAL NOT NULL DEFAULT 0,
    fire_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    delivery      TEXT NOT NULL DEFAULT 'group',
    dm_to         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'group',
    created_ts    REAL NOT NULL,
    created_at    TEXT NOT NULL DEFAULT '',
    updated_ts    REAL NOT NULL,
    last_error    TEXT NOT NULL DEFAULT ''
)
"""


def _load_core():
    """从插件自身目录加载 reminder_core（不依赖包导入，热重载也稳）。"""
    import importlib.util
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminder_core.py")
    try:
        spec = importlib.util.spec_from_file_location("_wb_reminder_core", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


_core = _load_core()


def _ensure_columns(conn):
    """老库补齐新增列，避免升级后插入失败。"""
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
        if "at_names" not in cols:
            conn.execute("ALTER TABLE outbox ADD COLUMN at_names TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except Exception:
        pass


class Plugin:
    """helper 插件入口，接口约定见 docs/development/plugins.md。"""

    def on_load(self, ctx):
        cfg = ctx.config or {}
        self.endpoint = str(cfg.get("endpoint") or "")
        self.token = str(cfg.get("token") or "")
        self.batch_size = max(1, int(cfg.get("batch_size", 50)))
        self.flush_seconds = max(5, int(cfg.get("flush_seconds", 30)))
        self.only_groups = bool(cfg.get("only_groups", True))
        self.include_self = bool(cfg.get("include_self", False))
        # 公众号会话不是群，必须独立于 only_groups；仍可用这个开关整体关闭
        self.collect_official = bool(cfg.get("collect_official", True))
        self.group_allowlist = set(str(x) for x in (cfg.get("group_allowlist") or []))
        self.report_keywords = [str(x) for x in (cfg.get("report_keywords") or ["/报告"])]
        self.top_n = max(1, int(cfg.get("report_top_n", 5)))
        self.timeout = max(1, int(cfg.get("http_timeout_seconds", 5)))

        root = getattr(ctx, "project_root", None) or os.path.dirname(os.path.dirname(__file__))
        runtime = os.path.join(root, ".runtime")
        os.makedirs(runtime, exist_ok=True)
        self.db = sqlite3.connect(
            os.path.join(runtime, "report_buffer.sqlite3"),
            check_same_thread=False,
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(_CREATE_TABLE)
        self.db.execute(_CREATE_LINKS_TABLE)
        self.db.execute(_CREATE_OFFICIAL_TABLE)
        self.db.commit()

        # 本地/内网接收端不该走系统代理；显式禁用，避免代理不可达时误判为远端故障
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._last_flush = time.time()
        self._backoff_until = 0.0
        self._pending = 0
        self._pending_links = 0
        self._pending_official = 0
        # 未配置远端时仍可本地采集与群内简报，方便先跑通再接服务器
        self._ready = bool(self.endpoint and self.token and self.token != "CHANGE_ME")

        # 主动发消息队列：send.py / send.bat 写入，本线程领取并调用 helper 发送。
        # 用 SQLite 原子领取而非新开 HTTP 端口，热重载时新旧实例短暂共存也不会重复发送。
        self.ctx = ctx
        self._outbox_path = os.path.join(runtime, "send_outbox.sqlite3")
        self._heartbeat_path = os.path.join(runtime, "outbox_worker.json")
        self._stop_event = threading.Event()

        # ---- 定时提醒：词库 ----
        # 投递方式不读配置：群里 @ 发起 -> 回原群并 @；私聊发起 -> 私信回原会话。
        self.reminder_lexicon = {}
        self._reminders_ready = False
        if _core:
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
            self.reminder_lexicon = _core.load_lexicon([
                os.path.join(data_dir, "tencent_sensitive_words.txt"),
                os.path.join(data_dir, "illegal_keywords.txt"),
            ])
        try:
            init_conn = sqlite3.connect(self._outbox_path, timeout=5)
            init_conn.execute("PRAGMA journal_mode=WAL")
            init_conn.execute(_OUTBOX_SCHEMA)
            init_conn.execute(_REMINDERS_SCHEMA)
            _ensure_columns(init_conn)
            init_conn.commit()
            init_conn.close()
            self._reminders_ready = bool(_core)
        except sqlite3.Error:
            pass

        self._worker = threading.Thread(
            target=self._outbox_loop, name="report-bridge-outbox", daemon=True)
        self._worker.start()
        self._install_native_hook()
        self._install_monitor_hook()

    # ---- 公众号消息：在 TextRouter.convert 之前挂只读旁路 ----
    def _install_native_hook(self):
        """helper 会丢弃 gh_ 会话，只有 _map_message 能见到原始消息。

        _receive_loop 里是 self._map_message(message)，实例属性动态查找，
        因此包装实例方法即可拿到 ctypes 原始消息，且完全不改 helper 源码。
        """
        self._native_raw = None
        self._native_original = None
        service = getattr(self.ctx, "_service", None)
        backend = getattr(service, "_backend", None)
        raw = getattr(backend, "raw", None)
        original = getattr(raw, "_map_message", None) if raw is not None else None
        if original is None or not callable(original):
            return False
        # 热重载时旧实例可能还没卸载，跳过旧包装接回最原始的方法
        target = getattr(original, "_wb_official_original", original)
        if not callable(target):
            return False
        plugin = self

        def hooked(message, _orig=target):
            try:
                plugin._on_native_message(message)
            except Exception:
                pass
            return _orig(message)

        hooked._wb_official_hook = True
        hooked._wb_official_original = target
        hooked._wb_official_owner = plugin
        self._native_raw = raw
        self._native_original = target
        raw._map_message = hooked
        return True

    def _restore_native_hook(self):
        raw = getattr(self, "_native_raw", None)
        if raw is None:
            return
        current = getattr(raw, "_map_message", None)
        if getattr(current, "_wb_official_owner", None) is self:
            raw._map_message = self._native_original
        self._native_raw = None

    @staticmethod
    def _native_bytes(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")

    def _on_native_message(self, message):
        """原生接收线程里执行：先极快地筛掉无关消息，再解析公众号内容。"""
        if not self.collect_official:
            return
        msg_type = getattr(message, "type", 0)
        if msg_type not in (1, 49):
            return
        sender = self._native_bytes(getattr(message, "sender", b"")).strip("\x00 ")
        receiver = self._native_bytes(getattr(message, "receiver", b"")).strip("\x00 ")
        peer = ""
        if sender.startswith("gh_") or sender in _SYSTEM_PEERS:
            peer = sender
        elif receiver.startswith("gh_") or receiver in _SYSTEM_PEERS:
            peer = receiver
        if not peer:
            return
        publisher = peer if peer.startswith("gh_") else ""

        if msg_type == 1:
            text = _clean(self._native_bytes(getattr(message, "text", b"")), 2000)
            # 模板/系统消息大多是 XML 或提示语，只有纯文本才有归档价值
            if text and "<" not in text:
                self._record_official(
                    chat_id=peer, publisher_id=publisher, scene="text",
                    title=text[:512], url="", digest=text[:2048])
            return

        # 不预先按字符截断，避免把 XML 截成不闭合；解析器内部按字节数拒绝超长
        raw_xml = self._native_bytes(getattr(message, "image_xml", b""))
        parsed = parse_official_message(raw_xml, publisher)
        if not parsed:
            return
        for article in parsed["articles"]:
            self._record_official(
                chat_id=peer, publisher_id=publisher,
                publisher_name=parsed["publisher_name"], scene="push", **article)
        try:
            self.ctx.logger.info("公众号采集 | 会话ID=%s | 名称=%s | %d 篇 | 标题=%s",
                                 peer, parsed["publisher_name"][:40],
                                 len(parsed["articles"]),
                                 parsed["articles"][0]["title"][:60])
        except Exception:
            pass

    # ---- 分享卡片：包装原生 monitor_handler，无需改动 helper 源码或重启微信 ----
    def _install_monitor_hook(self):
        """type 49 的原始 XML 只经过 monitor_handler；在这里挂一个只读旁路。"""
        self._monitor_raw = None
        self._monitor_original = None
        service = getattr(self.ctx, "_service", None)
        backend = getattr(service, "_backend", None)
        raw = getattr(backend, "raw", None)
        if raw is None or not hasattr(raw, "monitor_handler"):
            return False
        previous = raw.monitor_handler
        # 热重载时旧实例可能还没卸载；跳过旧包装，直接接到最原始的 handler 上
        target = getattr(previous, "_wb_link_original", previous)
        if not callable(target):
            return False
        plugin = self

        def hooked(data):
            try:
                plugin._on_monitor(data)
            except Exception:
                pass
            return target(data)

        hooked._wb_link_hook = True
        hooked._wb_link_original = target
        hooked._wb_link_owner = plugin
        self._monitor_raw = raw
        self._monitor_original = target
        raw.monitor_handler = hooked
        return True

    def _restore_monitor_hook(self):
        raw = getattr(self, "_monitor_raw", None)
        if raw is None:
            return
        current = getattr(raw, "monitor_handler", None)
        if getattr(current, "_wb_link_owner", None) is self:
            raw.monitor_handler = self._monitor_original
        self._monitor_raw = None

    def _on_monitor(self, data):
        """在原生消息线程里执行，必须极快且绝不抛异常。"""
        if not isinstance(data, dict) or data.get("wx_type") != 49:
            return
        raw = data.get("raw_content")
        card = parse_link_card(raw)
        if not card:
            return
        chat_id = str(data.get("chat_id") or "")
        sender_id = str(data.get("sender_id") or "")
        sender_name = str(data.get("sender_wechat_name") or "")
        is_group = bool(data.get("room_wxid"))
        # 公众号文章分享卡片既算链接，也归入公众号消息流
        official = parse_official_message(raw) if self.collect_official else None
        self._record_link(
            chat_id=chat_id, sender_id=sender_id, sender_name=sender_name,
            is_group=is_group, kind="official" if official else "card", **card)
        if official:
            for article in official["articles"]:
                self._record_official(
                    chat_id=chat_id, publisher_id=official["publisher_id"],
                    publisher_name=official["publisher_name"], scene="shared",
                    sender_id=sender_id, sender_name=sender_name,
                    is_group=is_group, **article)
        try:
            self.ctx.logger.info("%s | 会话ID=%s | %s | 标题=%s",
                                 "公众号采集" if official else "链接采集",
                                 chat_id,
                                 "公众号文章分享" if official else "分享卡片",
                                 card["title"][:60])
        except Exception:
            pass

    def _record_link(self, chat_id, sender_id, sender_name, is_group, kind,
                     url, title, description="", source="", app_type=""):
        if not chat_id or not url:
            return
        if self.only_groups and not is_group:
            return
        if self.group_allowlist and chat_id not in self.group_allowlist:
            return
        received_at = datetime.now().astimezone().isoformat(timespec="seconds")
        link_key = hashlib.sha256("|".join(
            (chat_id, sender_id, received_at[:19], kind, url, title)
        ).encode("utf-8")).hexdigest()
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO links "
                "(link_key, chat_id, sender_id, sender_name, is_group, kind, app_type, "
                "title, url, description, source, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (link_key, chat_id, sender_id, sender_name, 1 if is_group else 0,
                 kind, app_type, title, url, description, source, received_at),
            )
            self.db.commit()
            self._pending_links = self.db.execute(
                "SELECT COUNT(*) FROM links WHERE synced = 0").fetchone()[0]

    def _record_official(self, chat_id, publisher_id, scene, title, url="",
                         digest="", cover_url="", published_at="",
                         publisher_name="", sender_id="", sender_name="",
                         is_group=False):
        """公众号消息统一落地：push（公众号推送）/ shared（群里分享）/ text。"""
        if not self.collect_official or not chat_id:
            return
        if scene == "shared":
            if self.only_groups and not is_group:
                return
            if self.group_allowlist and chat_id not in self.group_allowlist:
                return
        url = safe_link_url(url, _OFFICIAL_HOSTS) if url else ""
        if scene != "text" and not url:
            return
        title = _clean(title, _MAX_FIELD)
        if not title:
            return
        received_at = datetime.now().astimezone().isoformat(timespec="seconds")
        official_key = hashlib.sha256("|".join(
            (chat_id, publisher_id, received_at[:19], scene, url, title)
        ).encode("utf-8")).hexdigest()
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO official "
                "(official_key, chat_id, publisher_id, publisher_name, sender_id, "
                "sender_name, is_group, scene, title, url, digest, cover_url, "
                "published_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (official_key, chat_id, publisher_id, publisher_name,
                 sender_id, sender_name, 1 if is_group else 0, scene,
                 title, url, _clean(digest, 2048),
                 safe_link_url(cover_url, _COVER_HOSTS), published_at[:32],
                 received_at),
            )
            self.db.commit()
            self._pending_official = self.db.execute(
                "SELECT COUNT(*) FROM official WHERE synced = 0").fetchone()[0]

    def _write_heartbeat(self, running):
        """给管理台判断「发送模块是否真的加载了」用，避免旧插件误报就绪。"""
        payload = {"running": bool(running), "ts": time.time(),
                   "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "pid": os.getpid(),
                   # 让管理台能确认两条旁路真的挂在了运行中的桥接上
                   "link_hook": bool(getattr(self, "_monitor_raw", None)),
                   "official_hook": bool(getattr(self, "_native_raw", None)),
                   # 提醒引擎：管理台据此提示「需要 reload report_bridge」
                   "reminders": bool(getattr(self, "_reminders_ready", False))}
        try:
            tmp = self._heartbeat_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self._heartbeat_path)
        except OSError:
            pass

    def _reminder_tick(self, conn):
        """扫描到期提醒，写进发送队列并推进/结束任务。"""
        if _core is None:
            return
        now = time.time()
        with conn:
            rows = conn.execute(
                "SELECT id, chat_id, chat_name, targets, target_names, content, "
                "rule_json, kind, next_ts, delivery, dm_to, rule_text "
                "FROM reminders WHERE status = 'active' AND next_ts <= ? "
                "ORDER BY next_ts LIMIT 20", (now,)).fetchall()
            for row in rows:
                (rid, chat_id, chat_name, targets, target_names, content,
                 rule_json, kind, old_next, delivery, dm_to, rule_text) = row
                item = {
                    "chat_id": chat_id, "chat_name": chat_name,
                    "targets": targets, "target_names": target_names,
                    "content": content, "delivery": delivery, "dm_to": dm_to,
                }
                sent = _core.build_delivery(item)
                conn.execute(
                    "INSERT INTO outbox (chat_id, text, at_list, at_names, status, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (sent["chat_id"], sent["text"], sent["at_list"], sent["at_names"],
                     datetime.now().astimezone().isoformat(timespec="seconds")))
                if kind == "once":
                    conn.execute(
                        "UPDATE reminders SET status = 'done', last_fire_ts = ?, "
                        "fire_count = fire_count + 1, updated_ts = ? WHERE id = ?",
                        (now, now, rid))
                else:
                    following = _core.next_fire(rule_json, old_next)
                    if not following or following <= now:
                        following = now + 300
                    conn.execute(
                        "UPDATE reminders SET next_ts = ?, last_fire_ts = ?, "
                        "fire_count = fire_count + 1, updated_ts = ? WHERE id = ?",
                        (following, now, now, rid))

    def _outbox_loop(self):
        conn = sqlite3.connect(self._outbox_path, timeout=5, check_same_thread=False)
        ticks = 0
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_OUTBOX_SCHEMA)
            conn.execute(_REMINDERS_SCHEMA)
            _ensure_columns(conn)
            conn.commit()
            self._write_heartbeat(True)
            while not self._stop_event.wait(0.5):
                try:
                    self._outbox_tick(conn)
                except Exception:
                    # 单条失败不影响队列继续工作
                    pass
                try:
                    self._reminder_tick(conn)
                except Exception:
                    pass
                ticks += 1
                if ticks % 4 == 0:      # 每 2 秒刷新一次心跳
                    self._write_heartbeat(True)
                # 每 5 秒把待上报的链接/公众号消息推走（worker 线程，不卡原生消息）
                if ticks % 10 == 0:
                    try:
                        with self._lock:
                            has_extra = (self._pending_links > 0
                                         or self._pending_official > 0)
                        if has_extra:
                            self._flush()
                    except Exception:
                        pass
        finally:
            self._write_heartbeat(False)
            conn.close()

    def _outbox_tick(self, conn):
        from datetime import timedelta
        cutoff = (datetime.now().astimezone() - timedelta(seconds=60)).isoformat(timespec="seconds")
        # 崩溃残留的「处理中」记录，超过 60 秒放回队列
        conn.execute("UPDATE outbox SET status = 0 WHERE status = 1 AND created_at < ?",
                     (cutoff,))
        with conn:
            row = conn.execute(
                "SELECT id, chat_id, text, at_list, at_names FROM outbox "
                "WHERE status = 0 ORDER BY id LIMIT 1").fetchone()
            if not row:
                return
            claimed = conn.execute(
                "UPDATE outbox SET status = 1 WHERE id = ? AND status = 0",
                (row[0],)).rowcount
        if claimed != 1:
            return

        rid, chat_id, text, at_list, at_names = row
        try:
            mentions = [x.strip() for x in str(at_list or "").split(",") if x.strip()]
            names = [x.strip() for x in str(at_names or "").split(",") if x.strip()]
            if mentions and hasattr(self.ctx, "send_at_text"):
                if len(names) == len(mentions):
                    try:
                        self.ctx.send_at_text(chat_id, text, mentions, display_names=names)
                    except TypeError:
                        self.ctx.send_at_text(chat_id, text, mentions)
                else:
                    self.ctx.send_at_text(chat_id, text, mentions)
            else:
                self.ctx.send_text(chat_id, text)
            status, error = 2, ""
        except Exception as exc:
            status, error = 3, str(exc)[:300]
        with conn:
            conn.execute(
                "UPDATE outbox SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, error,
                 datetime.now().astimezone().isoformat(timespec="seconds"), rid))

    def _maybe_reminder(self, message, ctx, chat_id, text, sender_id, sender_name, is_group):
        """识别提醒指令并建任务。

        投递方式跟随来源：群里 @ 发起 -> 回原群并 @；私聊发起 -> 私信回原会话（不 @）。
        群里必须真正 @ 了机器人；私聊不需要 @，只要有提醒触发词即可。
        """
        if _core is None:
            return False
        data = getattr(message, "data", None) or {}
        self_wxid = str(data.get("self_wxid") or "")
        at_members = [str(x) for x in (data.get("at_members") or ()) if x]
        at_self = bool(data.get("is_at_self")) or (bool(self_wxid) and self_wxid in at_members)
        if is_group and not at_self:
            # 若该版本没给出 @名单，则退化为「文本以 @ 开头」
            if at_members or not text.startswith("@"):
                return False
        # 只有包含提醒触发词的消息才算指令，避免普通聊天误建任务
        if not any(word in text for word in _core.TRIGGER_WORDS):
            return False

        parsed = _core.parse_command(text)
        if not parsed.get("ok"):
            return self._reminder_reply(ctx, chat_id, "❌ " + str(parsed.get("error") or "指令无法识别"))

        if self.reminder_lexicon:
            hit = _core.find_banned(parsed["content"], self.reminder_lexicon)
            if hit:
                return self._reminder_reply(ctx, chat_id, "❌ 内容涉及违禁词或违法违规信息，无法设置提醒。")

        # @名单来自微信原生 msgsource（真实 wxid），比解析昵称可靠。
        # 群：提醒对象 = 发起人 + 被 @ 的其他人，排除机器人自己。
        # 私聊：对象就是发起人本人，仅用于列表展示，投递时不 @。
        targets, seen = [], set()
        for wxid in [sender_id] + (at_members if is_group else []):
            if wxid and wxid != self_wxid and wxid not in seen:
                seen.add(wxid)
                targets.append(wxid)
        if not targets:
            return False
        # 框架 send_at_text 一次最多 @ 8 人
        extra = len(targets) - 8
        targets = targets[:8]
        names = [self._display_name(ctx, wxid, chat_id) for wxid in targets]
        chat_name = (str(getattr(self, "_chat_display_name", None) or "") or chat_id) \
            if is_group else (sender_name or chat_id)
        now = datetime.now().astimezone()
        ts = now.timestamp()
        delivery = "group" if is_group else "dm"
        dm_to = "" if is_group else chat_id
        source = "group" if is_group else "dm"
        try:
            conn = sqlite3.connect(self._outbox_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_REMINDERS_SCHEMA)
            cur = conn.execute(
                "INSERT INTO reminders (chat_id, chat_name, creator_id, creator_name, "
                "targets, target_names, content, raw_text, kind, rule_json, rule_text, "
                "interval_sec, weekdays, at_hhmm, next_ts, status, delivery, dm_to, "
                "source, created_ts, created_at, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, "
                "?, ?, ?, ?)",
                (chat_id, chat_name, sender_id, sender_name,
                 ",".join(targets), ",".join(names), parsed["content"], parsed["raw_text"],
                 parsed["kind"], parsed["rule_json"], parsed["rule_text"],
                 parsed["interval_sec"], ",".join(str(x) for x in parsed["weekdays"]),
                 parsed["at_hhmm"], parsed["next_ts"], delivery, dm_to, source,
                 ts, now.isoformat(timespec="seconds"), ts))
            rid = cur.lastrowid
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            return self._reminder_reply(ctx, chat_id, "❌ 提醒保存失败：%s" % str(exc)[:100])

        when = datetime.fromtimestamp(parsed["next_ts"]).strftime("%m-%d %H:%M")
        if is_group:
            delivery_note = "原群回复并 @（%d 人）" % len(names)
        else:
            delivery_note = "私信回复本会话（不 @）"
        lines = [
            "✅ 提醒已创建 #%d" % rid,
            "内容：%s" % parsed["content"],
            "时间：%s（%s）" % (when, parsed["rule_text"]),
            "对象：%s（%d 人）" % ("、".join(names), len(names)),
            "方式：%s" % delivery_note,
        ]
        if is_group and extra > 0:
            lines.append("提示：@ 人数超过上限 8 人，已忽略 %d 人" % extra)
        return self._reminder_reply(ctx, chat_id, "\n".join(lines))

    def _reminder_reply(self, ctx, chat_id, text):
        try:
            ctx.send_text(chat_id, text)
        except Exception:
            pass
        return True

    def on_message(self, message, ctx):
        if getattr(message, "type", None) != 1:
            return
        if not self.include_self and bool(getattr(message, "is_self", False)):
            return
        is_group = bool(getattr(message, "is_group", False))
        chat_id = str(getattr(message, "chat_id", "") or "")
        text = str(getattr(message, "text", "") or "").strip()
        if not text:
            return
        sender_id = str(getattr(message, "sender_id", "") or "")
        sender_name = self._display_name(ctx, sender_id, chat_id)

        # 提醒指令先处理：群里 @ 与私聊都生效，不受 only_groups / 群白名单影响。
        # 「在哪发的就在哪回」由 _maybe_reminder 按 is_group 决定；采集仍按原开关走。
        try:
            self._maybe_reminder(message, ctx, chat_id, text, sender_id, sender_name, is_group)
        except Exception:
            pass

        if self.only_groups and not is_group:
            return
        if self.group_allowlist and chat_id not in self.group_allowlist:
            return
        received_at = datetime.now().astimezone().isoformat(timespec="seconds")

        # 文本里直接粘贴的网址也单独归档，便于日报汇总所有分享内容
        for url in extract_text_urls(text):
            self._record_link(
                chat_id=chat_id, sender_id=sender_id, sender_name=sender_name,
                is_group=is_group, kind="text", url=url, title="",
                description=_clean(text, 200))

        raw = "|".join((chat_id, sender_id, received_at, text))
        msg_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO messages "
                "(msg_key, chat_id, sender_id, sender_name, is_group, text, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_key, chat_id, sender_id, sender_name, 1 if is_group else 0,
                 text, received_at),
            )
            self.db.commit()
            self._pending = self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE synced = 0"
            ).fetchone()[0]
            due = (
                self._pending >= self.batch_size
                or (time.time() - self._last_flush) >= self.flush_seconds
            )

        if any(k in text for k in self.report_keywords):
            self._reply_report(ctx, message, chat_id)

        if due:
            self._flush()

    def _display_name(self, ctx, sender_id, chat_id):
        members = getattr(ctx, "members", None)
        if members is not None and hasattr(members, "display_name"):
            try:
                return str(members.display_name(sender_id, chat_id) or sender_id)
            except Exception:
                pass
        return sender_id

    def _flush(self):
        """把未同步的文本与链接一起 POST 到远端；失败则保留，等待下次重试。"""
        with self._flush_lock:
            with self._lock:
                self._last_flush = time.time()
                # 远端故障期间做指数式退避，避免每条新消息都触发一次 5 秒超时
                if not self._ready or time.time() < self._backoff_until:
                    return
                rows = self.db.execute(
                    "SELECT id, msg_key, chat_id, sender_id, sender_name, is_group, "
                    "text, received_at FROM messages WHERE synced = 0 "
                    "ORDER BY id LIMIT ?",
                    (self.batch_size,),
                ).fetchall()
                link_rows = self.db.execute(
                    "SELECT id, link_key, chat_id, sender_id, sender_name, is_group, "
                    "kind, app_type, title, url, description, source, received_at "
                    "FROM links WHERE synced = 0 ORDER BY id LIMIT ?",
                    (self.batch_size,),
                ).fetchall()
                official_rows = self.db.execute(
                    "SELECT id, official_key, chat_id, publisher_id, publisher_name, "
                    "sender_id, sender_name, is_group, scene, title, url, digest, "
                    "cover_url, published_at, received_at FROM official "
                    "WHERE synced = 0 ORDER BY id LIMIT ?",
                    (self.batch_size,),
                ).fetchall()
                if not rows and not link_rows and not official_rows:
                    return
                items = [
                    {
                        "msg_key": r[1], "chat_id": r[2], "sender_id": r[3],
                        "sender_name": r[4], "is_group": bool(r[5]),
                        "text": r[6], "received_at": r[7],
                    }
                    for r in rows
                ]
                links = [
                    {
                        "link_key": r[1], "chat_id": r[2], "sender_id": r[3],
                        "sender_name": r[4], "is_group": bool(r[5]),
                        "kind": r[6], "app_type": r[7], "title": r[8],
                        "url": r[9], "description": r[10], "source": r[11],
                        "received_at": r[12],
                    }
                    for r in link_rows
                ]
                official = [
                    {
                        "official_key": r[1], "chat_id": r[2],
                        "publisher_id": r[3], "publisher_name": r[4],
                        "sender_id": r[5], "sender_name": r[6],
                        "is_group": bool(r[7]), "scene": r[8], "title": r[9],
                        "url": r[10], "digest": r[11], "cover_url": r[12],
                        "published_at": r[13], "received_at": r[14],
                    }
                    for r in official_rows
                ]
                ids = [r[0] for r in rows]
                link_ids = [r[0] for r in link_rows]
                official_ids = [r[0] for r in official_rows]

            payload = json.dumps({"messages": items, "links": links,
                                  "official": official},
                                 ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": "Bearer " + self.token,
                },
                method="POST",
            )
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise OSError("unexpected status %s" % response.status)
                    response.read()
            except (urllib.error.URLError, OSError, ValueError):
                with self._lock:
                    self._backoff_until = time.time() + min(self.flush_seconds * 2, 300)
                return

            with self._lock:
                self._backoff_until = 0.0
                if ids:
                    self.db.executemany(
                        "UPDATE messages SET synced = 1 WHERE id = ?",
                        [(i,) for i in ids])
                if link_ids:
                    self.db.executemany(
                        "UPDATE links SET synced = 1 WHERE id = ?",
                        [(i,) for i in link_ids])
                if official_ids:
                    self.db.executemany(
                        "UPDATE official SET synced = 1 WHERE id = ?",
                        [(i,) for i in official_ids])
                self.db.commit()
                self._pending = self.db.execute(
                    "SELECT COUNT(*) FROM messages WHERE synced = 0"
                ).fetchone()[0]
                self._pending_links = self.db.execute(
                    "SELECT COUNT(*) FROM links WHERE synced = 0"
                ).fetchone()[0]
                self._pending_official = self.db.execute(
                    "SELECT COUNT(*) FROM official WHERE synced = 0"
                ).fetchone()[0]

    def _build_report(self, chat_id=None, day=None):
        day = day or date.today().isoformat()
        sql = ("SELECT sender_name, chat_id, text, received_at FROM messages "
               "WHERE substr(received_at, 1, 10) = ?")
        args = [day]
        if chat_id:
            sql += " AND chat_id = ?"
            args.append(chat_id)
        with self._lock:
            rows = self.db.execute(sql, tuple(args)).fetchall()

        if not rows:
            return "%s 暂无消息记录。" % day

        senders = Counter((r[0] or "未知") for r in rows)
        hours = Counter(r[3][11:13] for r in rows if len(r[3]) >= 13)
        groups = Counter(r[1] for r in rows)

        lines = [
            "%s 群消息简报" % day,
            "共 %d 条，%d 人发言，涉及 %d 个群" % (
                len(rows), len(senders), len(groups)),
            "活跃 Top%d：" % self.top_n,
        ]
        for name, count in senders.most_common(self.top_n):
            lines.append("  %s：%d 条" % (name, count))
        if hours:
            hour, count = hours.most_common(1)[0]
            lines.append("高峰时段：%s 点（%d 条）" % (hour, count))
        lines.append("完整日报见远端服务器 /api/report。")
        return "\n".join(lines)

    def _reply_report(self, ctx, message, chat_id):
        text = self._build_report(chat_id if chat_id else None)
        try:
            if bool(getattr(message, "is_group", False)):
                ctx.reply_at(message, text)
            else:
                ctx.reply(message, text)
        except Exception:
            # 回复失败不影响采集主流程
            pass

    def scheduled_flush(self, params, ctx):
        self._flush()
        with self._lock:
            pending, pending_links, pending_official = (
                self._pending, self._pending_links, self._pending_official)
        return "已触发上报，剩余待发 %d 条文本 / %d 条链接 / %d 条公众号消息" % (
            pending, pending_links, pending_official)

    def scheduled_report(self, params, ctx):
        chat_id = str(params.get("chat_id") or "")
        day = str(params.get("day") or date.today().isoformat())
        text = self._build_report(chat_id or None, day)
        if chat_id:
            ctx.send_text(chat_id, text)
        return text

    def scheduled_send(self, params, ctx):
        """供管理台手动执行 / 定时任务：向指定会话发一段文本。"""
        chat_id = str(params.get("chat_id") or "")
        text = str(params.get("text") or "")
        if not chat_id or not text:
            return "缺少 chat_id 或 text"
        ctx.send_text(chat_id, text)
        return "已发送"

    def on_unload(self, ctx):
        self._restore_native_hook()
        self._restore_monitor_hook()
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2)
        self._write_heartbeat(False)
        try:
            self._flush()
        finally:
            with self._lock:
                self.db.close()
