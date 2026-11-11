"""本地 Web 管理台：纯 Python 标准库，零第三方依赖。

默认只监听 127.0.0.1:8088，不对外暴露。网页登录统一使用账号密码，
登录后签发 8 小时会话令牌；/api/* 也接受 config.local.json 中的长期 Bearer
token，但仅用于程序化调用（脚本、外部集成），页面不再提供令牌登录入口。

    python admin/app.py            # 启动，打印访问地址
    python admin/app.py --open     # 启动并打开浏览器

账号密码（明文不落盘，必须配置，否则网页无法登录）：
    python admin/app.py --set-login 账号 密码
生成 admin/auth.local.json（PBKDF2-HMAC-SHA256 散列），重启管理台后生效。

数据来源全部是本机已有的文件：
    server/messages.sqlite3                     正式消息库
    vendor/helper-main/.runtime/report_buffer.sqlite3   本地缓冲
    vendor/helper-main/.runtime/send_outbox.sqlite3    发送队列
    vendor/helper-main/.runtime/outbox_worker.json     发送 worker 心跳
    vendor/helper-main/bot_config.json                  运行配置
    vendor/helper-main/logs/bot.log                    运行日志
    admin/chats.auto.json                              自动同步的真实群名/头像（只读微信内存）
    admin/chats.local.json                             手工覆盖（优先级最高）
    admin/avatars/                                     真实头像图片（自动下载）
    admin/sync_status.json                             最近一次同步状态
    admin/social_data.json                             群成员 / 好友 / 朋友圈（只读微信内存）
    admin/social_status.json                           社交资料同步状态
    admin/announcements.local.json                     手工维护的群公告
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
BRIDGE = ROOT.parent
WORKSPACE = BRIDGE.parent
SERVER = BRIDGE / "server"
sys.path.insert(0, str(SERVER))
from report import build_stats, to_markdown  # noqa: E402

HELPER = WORKSPACE / "vendor" / "helper-main"
DB_PATH = Path(os.environ.get("BRIDGE_DB", str(SERVER / "messages.sqlite3")))
BUFFER_DB = HELPER / ".runtime" / "report_buffer.sqlite3"
OUTBOX_DB = Path(os.environ.get("BRIDGE_OUTBOX", str(HELPER / ".runtime" / "send_outbox.sqlite3")))
BOT_CONFIG = HELPER / "bot_config.json"
LOCAL_CONFIG = BRIDGE / "config.local.json"
LOG_FILE = HELPER / "logs" / "bot.log"

# 群名/头像优先级：手工覆盖 > 微信内存自动同步 > helper 的 group_names。
CHAT_META_FILE = ROOT / "chats.local.json"
CHAT_AUTO_FILE = ROOT / "chats.auto.json"
SYNC_STATUS_FILE = ROOT / "sync_status.json"
SYNC_SCRIPT = ROOT / "sync_wechat_meta.py"
SYNC_INTERVAL = 30 * 60          # 后台自动同步间隔（秒）
SYNC_TIMEOUT = 240               # 单次同步最长耗时
AVATAR_DIR = ROOT / "avatars"
MEMBERS_CONFIG = HELPER / "members_config.json"

# 社交资料（群成员 / 好友 / 朋友圈）：与群名同步分开的独立脚本、独立锁、独立节奏。
SOCIAL_SCRIPT = ROOT / "sync_wechat_social.py"
SOCIAL_DATA_FILE = ROOT / "social_data.json"
SOCIAL_STATUS_FILE = ROOT / "social_status.json"
ANNOUNCEMENTS_FILE = ROOT / "announcements.local.json"
SOCIAL_INTERVAL = 30 * 60        # 群成员 + 好友自动同步间隔（秒）
SOCIAL_TIMEOUT = 300             # 单次同步最长耗时
SOCIAL_SCHEDULE_DELAY = 45       # 启动后错开首批同步，避免和群名同步抢进程句柄
WORKER_HEARTBEAT = HELPER / ".runtime" / "outbox_worker.json"
WECHAT_HEALTH_FILE = ROOT / "wechat_health.json"
AVATAR_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif"}
CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_@.\-]{2,80}$")
WORKER_TTL = 15          # 心跳超过 15 秒视为发送模块未加载
# 发送候选排序：文件传输助手 / 自己 / 群 / 好友 / 公众号
KIND_ORDER = {"helper": 0, "group": 1, "contact": 2, "official": 3, "system": 4}
KIND_LABELS = {"helper": "文件传输助手", "group": "群聊", "contact": "好友",
               "official": "公众号", "system": "系统"}

# 微信聊天图片：helper 解密后落在 .runtime/images/account-*/image-<序号>-<md5>.<ext>。
# 管理台只读这些文件，不改动采集链路；/media/* 与 /api/* 一样需要鉴权。
IMAGE_DIR = HELPER / ".runtime" / "images"
THUMB_DIR = ROOT / ".thumbs"
MEDIA_NAME_RE = re.compile(
    r"^image-\d+-[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$", re.I)
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".gif": "image/gif",
               ".webp": "image/webp"}
MEDIA_LOG_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+"
    r"INFO\s+\[NativeText\]\s+图片接收\s+\|\s+"
    r"会话ID=(?P<chat_id>.*?)\s+\|\s+状态=(?P<status>.*?)\s+\|\s+"
    r"文件=(?P<file>.*)$")
MEDIA_THUMB_WIDTHS = (160, 320, 640)
_image_cache = {"at": 0.0, "records": []}
_image_lock = threading.Lock()

# 账号密码登录：明文密码不落盘，只保存 PBKDF2-HMAC-SHA256 散列。
# 生成/修改：python admin/app.py --set-login 账号 密码
AUTH_FILE = ROOT / "auth.local.json"
PBKDF2_ITERATIONS = 200000
SESSION_TTL = 8 * 3600          # 登录会话有效期（秒）
MAX_SESSIONS = 32
LOGIN_MAX_FAILS = 5             # 连续失败次数上限
LOGIN_LOCK_SECONDS = 30         # 触发后锁定秒数
_state_lock = threading.Lock()
_sessions = {}                   # sid -> 过期时间戳
_failed_logins = {"count": 0, "until": 0.0}
_sync_lock = threading.Lock()    # 同一时刻只允许一个微信内存同步
_social_lock = threading.Lock()  # 社交资料同步独立加锁
_social_cache = {"mtime": None, "data": {}}
_social_cache_lock = threading.Lock()

# 仿微信 PC 的单一界面（原生 JS，无框架依赖）。旧版 React 管理台与 social 页面已移除。
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

OUTBOX_SCHEMA = """
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

# 定时提醒任务表：与 report_bridge 插件共用同一个 send_outbox.sqlite3。
# 字段必须和插件 _REMINDERS_SCHEMA 保持一致；这里建表是 IF NOT EXISTS，
# 插件没启动时管理台也能正常展示空列表。
REMINDERS_SCHEMA = """
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

# 提醒核心逻辑（口语化时间解析 / 投递格式化）放在插件目录，管理台与插件共用同一份，
# 避免「直接发送」和到期自动发送的格式不一致。
REMINDER_CORE_FILE = BRIDGE / "plugin" / "reminder_core.py"
_reminder_core_cache = {"module": None}

# 只允许修改这些字段，避免 Web 端写入任意配置
EDITABLE_NUMBERS = {"batch_size": (1, 500), "flush_seconds": (5, 3600),
                    "http_timeout_seconds": (1, 60), "report_top_n": (1, 50)}
EDITABLE_BOOLS = {"only_groups", "include_self", "collect_official"}
EDITABLE_LISTS = {"chat_allowlist", "group_allowlist", "report_keywords"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def reminder_core():
    """动态加载插件目录里的 reminder_core.py（纯标准库，无副作用）。"""
    cached = _reminder_core_cache["module"]
    if cached is not None:
        return cached
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_wb_admin_reminder_core", str(REMINDER_CORE_FILE))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _reminder_core_cache["module"] = module
        return module
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return None


def _fallback_delivery(row):
    """核心模块缺失时的最小兜底，保证「直接发送」仍然可用。"""
    content = str(row.get("content") or "").strip()
    if str(row.get("delivery") or "group") == "dm":
        # 私聊发起的提醒：回原私聊会话，不带 @
        dm_to = (str(row.get("dm_to") or "").strip()
                 or str(row.get("chat_id") or "").strip())
        return {"chat_id": dm_to,
                "text": "⏰ 定时提醒\n" + content,
                "at_list": "", "at_names": ""}
    return {"chat_id": str(row.get("chat_id") or ""),
            "text": "⏰ " + content,
            "at_list": str(row.get("targets") or ""),
            "at_names": str(row.get("target_names") or "")}


def reminder_delivery(row):
    core = reminder_core()
    build = getattr(core, "build_delivery", None) if core else None
    if callable(build):
        try:
            return build(row)
        except Exception:
            pass
    return _fallback_delivery(row)


def reminder_desc(rule_json, delivery):
    core = reminder_core()
    fn = getattr(core, "describe", None) if core else None
    if callable(fn):
        try:
            return fn(rule_json, delivery)
        except Exception:
            pass
    return str(rule_json or "")


# 公网暴露时可用 ADMIN_TOKEN 给管理台单独设令牌，与接收端的 BRIDGE_TOKEN 分离。
# 不设置任何环境变量时，行为与原来完全一致，本地用法不受影响。
PUBLIC_MODE = os.environ.get("ADMIN_PUBLIC", "").strip() == "1"
READONLY_MODE = os.environ.get("ADMIN_READONLY", "").strip() == "1"


def token():
    override = os.environ.get("ADMIN_TOKEN", "").strip()
    if override:
        return override
    return str(read_json(LOCAL_CONFIG).get("token") or "")


def auth_record():
    """读取账号密码散列；文件不存在或格式不完整时返回 None（保持旧行为）。"""
    try:
        data = read_json(AUTH_FILE)
        if data.get("username") and data.get("salt") and data.get("hash"):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_auth(username, password):
    if not username or not password:
        raise ValueError("账号和密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    data = {
        "username": str(username),
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "iterations": PBKDF2_ITERATIONS,
        "_note": "PBKDF2-HMAC-SHA256；明文密码不落盘。用 --set-login 更新。",
    }
    AUTH_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        AUTH_FILE.chmod(0o600)
    except OSError:
        pass


def verify_login(username, password):
    record = auth_record()
    if record is None:
        return False
    if not hmac.compare_digest(str(username), str(record["username"])):
        return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        got = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), salt,
            int(record.get("iterations", PBKDF2_ITERATIONS)))
        return hmac.compare_digest(got, expected)
    except (ValueError, KeyError, TypeError):
        return False


def login_gate():
    """全局失败限流：不依赖可伪造的 X-Forwarded-For，frp 回环下同样有效。"""
    now = time.time()
    with _state_lock:
        if _failed_logins["until"] > now:
            return int(_failed_logins["until"] - now) + 1
        if _failed_logins["until"]:
            _failed_logins["count"] = 0
            _failed_logins["until"] = 0.0
    return 0


def login_fail():
    now = time.time()
    with _state_lock:
        _failed_logins["count"] += 1
        if _failed_logins["count"] >= LOGIN_MAX_FAILS:
            _failed_logins["until"] = now + LOGIN_LOCK_SECONDS
            _failed_logins["count"] = 0


def login_ok():
    with _state_lock:
        _failed_logins["count"] = 0
        _failed_logins["until"] = 0.0


def new_session():
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with _state_lock:
        for key, expire in list(_sessions.items()):
            if expire <= now:
                _sessions.pop(key, None)
        if len(_sessions) >= MAX_SESSIONS:
            _sessions.pop(min(_sessions, key=_sessions.get), None)
        _sessions[sid] = now + SESSION_TTL
    return sid


def session_valid(sid):
    if not sid:
        return False
    with _state_lock:
        expire = _sessions.get(sid)
        if not expire:
            return False
        if expire <= time.time():
            _sessions.pop(sid, None)
            return False
        return True


def drop_session(sid):
    with _state_lock:
        _sessions.pop(sid, None)


def connect(path):
    if not Path(path).is_file():
        return None
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 时间 / 群名 / 头像

def fmt_time(value):
    """统一展示格式 Y-m-d H:i:s（2026-11-06 15:30:37）。

    数据库里是 ISO 8601（如 2026-11-06T11:24:10+08:00）；只格式化展示，
    不改存储，避免影响 msg_key 幂等和按 received_at 排序。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:19].replace("T", " ")


def safe_avatar_key(chat_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(chat_id))[:80]


def load_chat_meta():
    """逐字段合并群名/头像：手工覆盖 > 微信内存自动同步 > helper group_names。

    手工只填名字时不会把自动同步下来的真实头像顶掉，反之亦然。
    """
    merged = {}

    def put(key, name="", avatar=""):
        row = merged.setdefault(str(key), {"name": "", "avatar": ""})
        if name:
            row["name"] = str(name).strip()[:64]
        if avatar:
            row["avatar"] = str(avatar).strip()[:120]

    def absorb(mapping):
        if not isinstance(mapping, dict):
            return
        for key, value in mapping.items():
            if isinstance(value, dict):
                put(key, value.get("name"), value.get("avatar"))
            elif isinstance(value, str) and value.strip():
                put(key, value)

    try:
        absorb(read_json(MEMBERS_CONFIG).get("group_names") or {})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    absorb(read_json(CHAT_AUTO_FILE))
    absorb(read_json(CHAT_META_FILE))
    return {key: row for key, row in merged.items()
            if row["name"] or row["avatar"]}


def sync_status():
    status = read_json(SYNC_STATUS_FILE)
    if not isinstance(status, dict):
        status = {}
    status["busy"] = _sync_lock.locked()
    status["interval"] = SYNC_INTERVAL
    status.setdefault("last_sync", "")
    status.setdefault("error", "")
    return status


def run_sync_once():
    """独立进程执行只读同步；失败只写状态文件，不影响管理台。"""
    if not _sync_lock.acquire(blocking=False):
        return False
    try:
        subprocess.run([sys.executable, str(SYNC_SCRIPT), "--json"],
                       cwd=str(WORKSPACE), capture_output=True,
                       timeout=SYNC_TIMEOUT, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _sync_lock.release()


def auto_sync_loop():
    """启动后延迟 10 秒首次同步，之后每 SYNC_INTERVAL 秒刷新一次。"""
    time.sleep(10)
    while True:
        try:
            run_sync_once()
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL)


# ---------------------------------------------------------------- 微信社交资料
# 只读微信内存产出的 social_data.json；朋友圈与扩展资料的限制在页面上原样展示，
# 不把「读不到」包装成「没有」，也不猜测未核对的内存偏移。

SOCIAL_CAPABILITIES = {
    "group_members": "从微信群缓存只读快照读取：wxid、群昵称、微信名、群主/管理员。需先在电脑微信打开过该群。",
    "avatars": "按 wxid 与联系人缓存求交集取真实头像；未进联系人缓存的陌生人不会凭空补图。",
    "friends": "好友昵称、备注、微信号、个性描述、标签位与头像。性别/地区/签名未核对布局，统一留空。",
    "moments": "只读电脑微信已加载的朋友圈缓存（最多 50 条，含正文/时间/点赞/评论），不含图片内容；需先在电脑微信打开朋友圈。",
    "announcement": "微信客户端未开放群公告的只读接口，底层也没有已核对的内存字段。此处的公告为手工维护；群内 @所有人 消息由消息库自动汇总。",
}


def social_read_json(path, fallback):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def social_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def social_data():
    """按 mtime 缓存解析结果，避免每次 API 请求都重读整个 JSON。"""
    try:
        mtime = SOCIAL_DATA_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    with _social_cache_lock:
        if mtime is not None and _social_cache["mtime"] == mtime:
            return _social_cache["data"]
        data = social_read_json(SOCIAL_DATA_FILE, {})
        if not isinstance(data, dict):
            data = {}
        _social_cache.update(mtime=mtime, data=data)
        return data


def social_status():
    status = social_read_json(SOCIAL_STATUS_FILE, {})
    status["busy"] = _social_lock.locked()
    status["interval"] = SOCIAL_INTERVAL
    status.setdefault("last_sync", "")
    status.setdefault("error", "")
    return status


def social_avatar_url(filename):
    name = Path(str(filename or "")).name
    if not name or Path(name).suffix.lower() not in AVATAR_TYPES:
        return ""
    if not (AVATAR_DIR / name).is_file():
        return ""
    return "/avatars/" + quote(name)


def social_with_avatar(row):
    item = dict(row) if isinstance(row, dict) else {}
    item["avatar_url_local"] = social_avatar_url(item.get("avatar"))
    return item


def social_avatar_index(data):
    """author/wxid -> 本地头像路径，供朋友圈时间线与成员资料卡复用。"""
    index = {}
    for friend in data.get("friends") or []:
        if isinstance(friend, dict) and friend.get("avatar"):
            index[str(friend.get("wxid") or "")] = social_avatar_url(friend.get("avatar"))
    for group in (data.get("groups") or {}).values():
        if not isinstance(group, dict):
            continue
        for member in group.get("members") or []:
            if isinstance(member, dict) and member.get("avatar"):
                index.setdefault(str(member.get("wxid") or ""),
                                 social_avatar_url(member.get("avatar")))
    return index


def social_groups_payload():
    data = social_data()
    rows = []
    for room, group in (data.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        rows.append({
            "room": room,
            "name": group.get("name") or room,
            "avatar_url": social_avatar_url(group.get("avatar")),
            "member_count": int(group.get("member_count") or 0),
            "status": group.get("status") or "cache_not_ready",
            "note": group.get("note") or "",
            "updated_at": group.get("updated_at") or "",
        })
    rows.sort(key=lambda row: (-row["member_count"], row["name"]))
    return {"groups": rows, "total": len(rows)}


def member_name_index():
    """wxid -> 显示名。微信群成员缓存常只加载 ID，名字用本机已有数据补全。

    优先级由调用方决定：缓存里的群昵称 > 已同步的会话名 > 消息库 sender_name。
    全部来自本机已有数据，不联网、不向微信请求资料。
    """
    index = {}
    conn = connect(DB_PATH)
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT sender_id, sender_name, COUNT(*) FROM messages "
                "WHERE sender_id <> '' AND sender_name <> '' "
                "GROUP BY sender_id, sender_name").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        best = {}
        for sender_id, sender_name, count in rows:
            key = str(sender_id)
            if count > best.get(key, (0, ""))[0]:
                best[key] = (count, str(sender_name))
        index = {key: value[1] for key, value in best.items()}
    for chat_id, row in load_auto_chats().items():
        if isinstance(row, dict) and row.get("name"):
            index[str(chat_id)] = str(row["name"])
    return index


def social_group_payload(room):
    data = social_data()
    group = (data.get("groups") or {}).get(room)
    if not isinstance(group, dict):
        return {"error": "未找到该群，请先同步资料", "room": room}
    result = dict(group)
    result["avatar_url_local"] = social_avatar_url(group.get("avatar"))
    names = member_name_index()
    members = []
    for row in group.get("members") or []:
        item = social_with_avatar(row)
        display = str(item.get("nickname") or item.get("wechat_name") or "").strip()
        source = "微信缓存"
        if not display:
            guess = names.get(str(item.get("wxid") or ""))
            if guess:
                display, source = guess, "消息记录 / 会话名"
        item["display_name"] = display or str(item.get("wxid") or "未知成员")
        item["name_source"] = source
        members.append(item)
    result["members"] = members
    result["named_count"] = sum(1 for item in members
                                if item["display_name"] != item.get("wxid"))
    result["announcement"] = load_announcements().get(room, {})
    result["notices"] = group_notices(room)
    return result


def social_friends_payload(keyword="", limit=1000):
    data = social_data()
    needle = str(keyword or "").strip().casefold()
    rows = []
    for row in data.get("friends") or []:
        if not isinstance(row, dict):
            continue
        if needle and not any(needle in str(row.get(key) or "").casefold()
                              for key in ("name", "wechat_name", "remark", "alias", "wxid")):
            continue
        rows.append(social_with_avatar(row))
        if len(rows) >= limit:
            break
    return {"friends": rows, "total": len(rows)}


def social_moments_payload():
    data = social_data()
    moments = data.get("moments") if isinstance(data.get("moments"), dict) else {}
    index = social_avatar_index(data)
    rows = []
    for row in moments.get("rows") or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["avatar_url_local"] = index.get(str(item.get("author") or ""), "")
        rows.append(item)
    return {"moments": rows, "status": moments.get("status") or "empty",
            "error": moments.get("error") or "", "note": moments.get("note") or "",
            "updated_at": moments.get("updated_at") or "",
            "loaded_count": moments.get("loaded_count") or 0,
            "truncated": bool(moments.get("truncated"))}


def social_state_payload():
    data = social_data()
    return {
        "status": social_status(),
        "stats": data.get("stats") or {},
        "account": data.get("account") or "",
        "version": data.get("version") or "",
        "updated_at": data.get("updated_at") or "",
        "capabilities": SOCIAL_CAPABILITIES,
        "script_exists": SOCIAL_SCRIPT.is_file(),
    }


def load_announcements():
    data = social_read_json(ANNOUNCEMENTS_FILE, {})
    return {str(key): value for key, value in data.items()
            if isinstance(value, dict)}


def save_announcement(payload):
    if not isinstance(payload, dict):
        raise ValueError("公告格式无效")
    room = str(payload.get("room") or "").strip()
    if not room.endswith("@chatroom") or not CHAT_ID_RE.match(room):
        raise ValueError("群 ID 无效")
    text = str(payload.get("text") or "")
    text = "".join(ch for ch in text if ch in "\n\r\t" or ord(ch) >= 32)[:5000]
    author = str(payload.get("author") or "").strip()[:64]
    merged = load_announcements()
    if not text.strip():
        merged.pop(room, None)
    else:
        merged[room] = {"text": text, "author": author,
                        "updated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "manual"}
    if len(merged) > 1000:
        raise ValueError("公告数量超过上限")
    social_write_json(ANNOUNCEMENTS_FILE, merged)
    return {"room": room, "saved": bool(text.strip())}


def group_notices(room, limit=20):
    """微信没有群公告读接口；这里自动汇总消息库里该群的 @所有人 消息作为参考。"""
    conn = connect(DB_PATH)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT sender_name, sender_id, text, received_at FROM messages "
            "WHERE chat_id = ? AND text LIKE ? ORDER BY received_at DESC LIMIT 120",
            (room, "%@所有人%")).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    seen, result = set(), []
    for row in rows:
        text = str(row["text"] or "").strip()
        key = text[:160]
        if not text or key in seen:
            continue
        seen.add(key)
        result.append({"sender_name": row["sender_name"] or "",
                       "sender_id": row["sender_id"] or "",
                       "text": text[:2000],
                       "received_at": fmt_time(row["received_at"])})
        if len(result) >= limit:
            break
    return result


def run_social_once(only="auto"):
    """独立进程执行只读同步；失败只写状态文件，不影响管理台主流程。"""
    if only not in ("auto", "groups", "friends", "moments", "all"):
        only = "auto"
    if not _social_lock.acquire(blocking=False):
        return False
    try:
        subprocess.run([sys.executable, str(SOCIAL_SCRIPT), "--only", only, "--json"],
                       cwd=str(WORKSPACE), capture_output=True,
                       timeout=SOCIAL_TIMEOUT, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _social_lock.release()


def social_loop():
    """与群名同步错开启动，只跑群成员 + 好友；朋友圈由页面手动触发。"""
    time.sleep(SOCIAL_SCHEDULE_DELAY)
    while True:
        try:
            run_social_once("auto")
        except Exception:
            pass
        time.sleep(SOCIAL_INTERVAL)


def chat_display(chat_id, meta=None):
    meta = load_chat_meta() if meta is None else meta
    name = (meta.get(str(chat_id)) or {}).get("name") or ""
    if name:
        return name
    raw = str(chat_id or "")
    if raw.endswith("@chatroom"):
        return "群 " + raw[:-len("@chatroom")]
    return raw or "未知会话"


def avatar_for(chat_id, meta=None):
    """真实头像：优先备注里指定的文件，其次 admin/avatars/<会话ID>.<ext>。"""
    meta = load_chat_meta() if meta is None else meta
    custom = (meta.get(str(chat_id)) or {}).get("avatar") or ""
    if custom:
        name = Path(str(custom)).name
        if (AVATAR_TYPES.get(Path(name).suffix.lower())
                and (AVATAR_DIR / name).is_file()):
            return "/avatars/" + quote(name)
    key = safe_avatar_key(chat_id)
    for ext, _ctype in AVATAR_TYPES.items():
        if (AVATAR_DIR / (key + ext)).is_file():
            return "/avatars/" + quote(key + ext)
    return ""


def load_auto_chats():
    """微信内存同步产出的会话元数据；文件不存在/损坏时返回空字典。"""
    data = read_json(CHAT_AUTO_FILE)
    return data if isinstance(data, dict) else {}


def kind_of(chat_id, auto=None):
    auto = load_auto_chats() if auto is None else auto
    row = auto.get(str(chat_id))
    if isinstance(row, dict) and row.get("kind"):
        return str(row["kind"])
    text = str(chat_id)
    if text.endswith("@chatroom"):
        return "group"
    if text == "filehelper":
        return "helper"
    if text.startswith("gh_"):
        return "official"
    if text in ("medianote", "floatbottle", "fmessage", "mphelper", "weixin"):
        return "system"
    return "contact"


def resolve_chat_id(chat_id, auto=None):
    """把用户输入解析成真正可用于发消息的会话 ID。

    支持三类输入：
      1. 精确会话 ID：12345@chatroom / wxid_xxx / gh_xxx / 自定义微信号 pigwan
      2. 自定义微信号（Alias）：例如 PigwanAsst -> wxid_5gze617a8utg22
      3. 唯一命中的显示名 / 微信昵称：例如 婷婷 -> wxid_f75p1s81dgr812
    未知但格式合法的 ID 也放行（刚加的好友可能还没同步进缓存）。

    返回 (会话ID, 说明字典)；无法解析时返回 (None, 错误说明)。
    """
    auto = load_auto_chats() if auto is None else auto
    manual = read_json(CHAT_META_FILE)
    manual = manual if isinstance(manual, dict) else {}
    text = str(chat_id or "").strip()
    needle = text.casefold()
    if not text or len(text) > 120:
        return None, "会话不能为空或过长"

    if not CHAT_ID_RE.match(text):
        # 中文昵称/备注：只在唯一命中时才解析，避免误发
        hits = [key for key, row in auto.items()
                if isinstance(row, dict) and needle in (
                    str(row.get("name", "")).casefold(),
                    str(row.get("wechat_name", "")).casefold())]
        hits += [key for key, value in manual.items()
                 if needle == str(value if isinstance(value, str)
                                  else (value or {}).get("name", "")).casefold()
                 and key not in hits]
        if len(hits) == 1:
            return hits[0], {"from": text, "matched": "name", "known": True}
        if len(hits) > 1:
            return None, "「%s」匹配到多个会话，请在下拉里选具体联系人" % text
        return None, "会话 ID 含非法字符；请从联系人列表选择，或填真实 ID / 微信号"

    # 精确 ID（包括自定义微信号本身就是目录 key 的情况，例如 pigwan）
    if text in auto or text in manual or text == "filehelper":
        return text, {"from": text, "matched": "id", "known": True}

    # 按 Alias（自定义微信号）唯一匹配
    aliases = [key for key, row in auto.items()
               if isinstance(row, dict)
               and str(row.get("alias", "")).casefold() == needle]
    if len(aliases) == 1:
        return aliases[0], {"from": text, "matched": "alias", "known": True}
    if len(aliases) > 1:
        return None, "微信号「%s」匹配到多个会话，请在下拉里选具体联系人" % text

    # 按名字/昵称唯一匹配（大小写不敏感、精确匹配）
    names = [key for key, row in auto.items()
             if isinstance(row, dict) and needle in (
                 str(row.get("name", "")).casefold(),
                 str(row.get("wechat_name", "")).casefold())]
    if len(names) == 1:
        return names[0], {"from": text, "matched": "name", "known": True}
    if len(names) > 1:
        return None, "名称「%s」匹配到多个会话，请在下拉里选具体联系人" % text

    # 消息库里出现过的会话也视为已知
    conn = connect(DB_PATH)
    known = False
    if conn is not None:
        try:
            known = conn.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? LIMIT 1",
                (text,)).fetchone() is not None
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return text, {"from": text, "matched": "literal", "known": known}


def valid_chat_id(chat_id):
    """兼容旧调用：能否解析成可发送的会话 ID。"""
    return resolve_chat_id(chat_id)[0] is not None


def query_contacts(keyword="", include_system=False, limit=400, kinds=None):
    """全部可发送会话（群 / 好友 / 公众号 / 系统 / 文件传输助手），带名字与头像。

    kinds 给定时只保留指定分类，供通讯录二级菜单使用。
    """
    auto = load_auto_chats()
    manual = read_json(CHAT_META_FILE)
    manual = manual if isinstance(manual, dict) else {}
    ids = set(auto) | set(manual) | {"filehelper"}
    conn = connect(DB_PATH)
    counts = {}
    if conn is not None:
        try:
            counts = {row["chat_id"]: row["n"] for row in conn.execute(
                "SELECT chat_id, COUNT(*) AS n FROM messages GROUP BY chat_id")}
            ids |= set(counts)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    meta = load_chat_meta()
    needle = str(keyword or "").strip().casefold()
    rows = []
    for chat_id in ids:
        chat_id = str(chat_id)
        if not CHAT_ID_RE.match(chat_id):
            continue
        row = auto.get(chat_id) if isinstance(auto.get(chat_id), dict) else {}
        kind = kind_of(chat_id, auto)
        if kind == "system" and not include_system and not (kinds and "system" in kinds):
            continue
        if kinds and kind not in kinds:
            continue
        name = chat_display(chat_id, meta)
        if needle and needle not in " ".join((
                chat_id, name, str(row.get("wechat_name", "")),
                str(row.get("alias", "")))).casefold():
            continue
        rows.append({
            "chat_id": chat_id,
            "name": name,
            "alias": str(row.get("alias", "")),
            "wechat_name": str(row.get("wechat_name", "")),
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, ""),
            "is_self": bool(row.get("is_self")),
            "avatar_url": avatar_for(chat_id, meta),
            "message_count": counts.get(chat_id, 0),
        })
    self_rank = lambda item: (0 if item["is_self"] else 1,
                              KIND_ORDER.get(item["kind"], 9),
                              -(item["message_count"]),
                              item["name"])
    rows.sort(key=self_rank)
    return rows[:max(1, min(int(limit), 1000))]


def worker_alive():
    """发送 worker 心跳。旧插件没有 worker，也不会写心跳 -> 明确显示需 reload。"""
    try:
        data = read_json(WORKER_HEARTBEAT)
        ts = float(data.get("ts") or 0)
        alive = bool(data.get("running", True)) and 0 < time.time() - ts <= WORKER_TTL
        return alive, fmt_time(data.get("time") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return False, ""


def sender_ready():
    ok, _last = worker_alive()
    return ok


def link_hook_ready():
    """插件心跳里报告分享卡片 monitor 旁路是否已挂到运行中的桥接进程。"""
    try:
        return bool(read_json(WORKER_HEARTBEAT).get("link_hook"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def official_hook_ready():
    """公众号消息走 _map_message 底层旁路，心跳单独报告它是否挂上。"""
    try:
        return bool(read_json(WORKER_HEARTBEAT).get("official_hook"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def save_chat_names(names):
    if not isinstance(names, dict):
        raise ValueError("群名格式无效")
    existing = {}
    try:
        existing = read_json(CHAT_META_FILE)
    except (OSError, ValueError, TypeError):
        pass
    merged = {}
    for key, value in list(names.items())[:1000]:
        chat_id = str(key).strip()
        name = str(value or "").strip()
        if not chat_id or not name or not CHAT_ID_RE.match(chat_id):
            continue
        old = existing.get(chat_id) if isinstance(existing, dict) else None
        avatar = str((old or {}).get("avatar") or "") if isinstance(old, dict) else ""
        merged[chat_id] = {"name": name[:64], "avatar": avatar[:120]}
    if len(merged) > 1000:
        raise ValueError("群名数量超过上限")
    CHAT_META_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(merged)


# ---------------------------------------------------------------- 数据查询

def empty_stats(day, chat_id=None):
    return {"day": day, "chat_id": chat_id, "total": 0, "participants": 0,
            "groups": 0, "top_senders": [], "top_groups": [],
            "hourly": [{"hour": h, "count": 0} for h in range(24)],
            "keyword_hits": {}, "samples": []}


def get_stats(day, chat_id=None, top_n=5):
    conn = connect(DB_PATH)
    if conn is None:
        return empty_stats(day, chat_id)
    try:
        return build_stats(conn, day, chat_id, top_n)
    except sqlite3.OperationalError:
        return empty_stats(day, chat_id)
    finally:
        conn.close()


def decorate_stats(stats):
    meta = load_chat_meta()
    for item in stats.get("top_groups", []):
        item["name"] = chat_display(item.get("chat_id"), meta)
        item["avatar_url"] = avatar_for(item.get("chat_id"), meta)
    for item in stats.get("links", []):
        item["chat_name"] = chat_display(item.get("chat_id"), meta)
    for item in stats.get("official", []):
        publisher = item.get("publisher_id") or item.get("chat_id")
        item["chat_name"] = chat_display(item.get("chat_id"), meta)
        item["publisher_name"] = (item.get("publisher_name")
                                  or chat_display(publisher, meta))
        item["avatar_url"] = (avatar_for(publisher, meta)
                              or avatar_for(item.get("chat_id"), meta))
    return stats


def receiver_ok():
    try:
        cfg = read_json(LOCAL_CONFIG)
        url = "http://%s:%s/health" % (cfg.get("server_host", "127.0.0.1"),
                                       cfg.get("server_port", 8080))
        with urllib.request.urlopen(url, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def bridge_running():
    """判断桥接是否活跃。

    注意：.runtime/bot.lock 只有 helper 的网页管理台会持有，控制台入口
    (main.py) 并不加锁，所以不能用它判断。改为「日志近期有活动且最后一次
    是启动而非停止」。helper 只在收到消息时写日志，群里安静几十分钟是常态，
    所以阈值放宽到 6 小时，避免把正常运行的桥接误判为离线。
    """
    if not LOG_FILE.is_file():
        return False
    try:
        if time.time() - LOG_FILE.stat().st_mtime > 21600:
            return False
    except OSError:
        return False
    tail = "".join(log_tail(120))
    last_start, last_stop = tail.rfind("框架已启动"), tail.rfind("框架已停止")
    return last_start > last_stop or last_stop == -1


def log_tail(lines=60):
    if not LOG_FILE.is_file():
        return []
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readlines()[-max(1, min(int(lines), 300)):]
    except OSError:
        return []


def wechat_health():
    """读取独立守护进程写的微信连接健康状态；超过 3 分钟未更新视为守护失联。"""
    empty = {"online": False, "account": None, "detail": "守护未运行",
             "offline_since": None, "last_online": None,
             "guard_alive": False, "recoveries": 0, "wake_attempts": 0}
    try:
        data = read_json(WECHAT_HEALTH_FILE)
    except (OSError, ValueError, TypeError):
        return empty
    if not isinstance(data, dict):
        return empty
    try:
        stamp = datetime.fromisoformat(str(data.get("last_check") or ""))
        fresh = abs((datetime.now() - stamp).total_seconds()) <= 180
    except (ValueError, TypeError):
        fresh = False
    data["guard_alive"] = fresh
    if not fresh:
        data["detail"] = "守护状态已过期（超过 3 分钟未更新）"
    return data


def state_payload():
    today = date.today().isoformat()
    stats = get_stats(today)
    totals = {"messages": 0, "chats": 0, "first": "", "last": ""}
    conn = connect(DB_PATH)
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT chat_id), MIN(received_at), MAX(received_at) "
                "FROM messages").fetchone()
            totals = {"messages": row[0] or 0, "chats": row[1] or 0,
                      "first": fmt_time(row[2]), "last": fmt_time(row[3])}
            try:
                totals["links"] = conn.execute(
                    "SELECT COUNT(*) FROM links").fetchone()[0] or 0
            except sqlite3.OperationalError:
                totals["links"] = 0
            try:
                totals["official"] = conn.execute(
                    "SELECT COUNT(*) FROM official").fetchone()[0] or 0
            except sqlite3.OperationalError:
                totals["official"] = 0
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    buffer = {"total": 0, "unsynced": 0}
    conn = connect(BUFFER_DB)
    if conn is not None:
        try:
            buffer["total"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            buffer["unsynced"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE synced = 0").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    outbox = {"pending": 0, "failed": 0, "sent": 0}
    conn = connect(OUTBOX_DB)
    if conn is not None:
        try:
            for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM outbox GROUP BY status"):
                if status == 0:
                    outbox["pending"] += count
                elif status == 2:
                    outbox["sent"] += count
                elif status == 3:
                    outbox["failed"] += count
                elif status == 1:
                    outbox["pending"] += count
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    alive, last_active = worker_alive()
    health = wechat_health()
    return {"today": today, "receiver": receiver_ok(), "bridge": bridge_running(),
            "wechat_online": bool(health.get("online")),
            "wechat_offline_since": health.get("offline_since"),
            "wechat_last_online": health.get("last_online"),
            "wechat_detail": health.get("detail"),
            "guard_alive": bool(health.get("guard_alive")),
            "wechat_recoveries": health.get("recoveries", 0),
            "wechat_wake_attempts": health.get("wake_attempts", 0),
            "sender_ready": alive, "sender_last_active": last_active,
            "link_hook": link_hook_ready(),
            "official_hook": official_hook_ready(),
            "sender_hint": "" if alive else
            "桥接未加载发送模块：请在 start.bat 黑窗口输入 reload report_bridge",
            "db_exists": DB_PATH.is_file(),
            "totals": totals, "buffer": buffer,
            "outbox": outbox, "today_stats": {
                "total": stats["total"], "participants": stats["participants"],
                "groups": stats["groups"]}}


def query_messages(day, chat_id, keyword, limit, offset):
    empty = {"day": day, "count": 0, "total": 0, "messages": []}
    conn = connect(DB_PATH)
    if conn is None:
        return empty
    sql = "FROM messages WHERE substr(received_at, 1, 10) = ?"
    args = [day]
    if chat_id:
        sql += " AND chat_id = ?"
        args.append(chat_id)
    if keyword:
        sql += " AND text LIKE ? ESCAPE '\\'"
        safe = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append("%" + safe + "%")
    try:
        total = conn.execute("SELECT COUNT(*) " + sql, tuple(args)).fetchone()[0]
        rows = conn.execute(
            "SELECT msg_key, chat_id, sender_id, sender_name, is_group, text, received_at "
            + sql + " ORDER BY received_at DESC, msg_key DESC LIMIT ? OFFSET ?",
            tuple(args + [limit, offset])).fetchall()
        meta = load_chat_meta()
        messages = []
        for row in rows:
            item = dict(row)
            item["received_at"] = fmt_time(item["received_at"])
            item["chat_name"] = chat_display(item["chat_id"], meta)
            item["avatar_url"] = avatar_for(item["chat_id"], meta)
            messages.append(item)
        return {"day": day, "count": len(messages), "total": total,
                "messages": messages}
    except sqlite3.OperationalError:
        return empty
    finally:
        conn.close()


def query_links(day, chat_id, keyword, limit, offset, kind=""):
    empty = {"day": day, "count": 0, "total": 0, "links": []}
    conn = connect(DB_PATH)
    if conn is None:
        return empty
    sql = "FROM links WHERE 1 = 1"
    args = []
    if day and day != "all":
        sql += " AND substr(received_at, 1, 10) = ?"
        args.append(day)
    if chat_id:
        sql += " AND chat_id = ?"
        args.append(chat_id)
    if kind in ("card", "text", "official"):
        sql += " AND kind = ?"
        args.append(kind)
    if keyword:
        sql += (" AND (title LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\' "
                "OR description LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\')")
        safe = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.extend(["%" + safe + "%"] * 4)
    try:
        total = conn.execute("SELECT COUNT(*) " + sql, tuple(args)).fetchone()[0]
        rows = conn.execute(
            "SELECT link_key, chat_id, sender_id, sender_name, is_group, kind, "
            "app_type, title, url, description, source, received_at "
            + sql + " ORDER BY received_at DESC, link_key DESC LIMIT ? OFFSET ?",
            tuple(args + [limit, offset])).fetchall()
        meta = load_chat_meta()
        items = []
        for row in rows:
            item = dict(row)
            item["received_at"] = fmt_time(item["received_at"])
            item["chat_name"] = chat_display(item["chat_id"], meta)
            item["avatar_url"] = avatar_for(item["chat_id"], meta)
            item["sender_label"] = item.get("sender_name") or item.get("sender_id") or ""
            items.append(item)
        return {"day": day, "count": len(items), "total": total, "links": items}
    except sqlite3.OperationalError:
        return empty
    finally:
        conn.close()


def query_official(day, chat_id, keyword, scene, limit, offset):
    """公众号消息流：推送图文、群内分享的公众号文章、公众号文字消息。"""
    empty = {"day": day, "count": 0, "total": 0, "official": []}
    conn = connect(DB_PATH)
    if conn is None:
        return empty
    sql = "FROM official WHERE 1 = 1"
    args = []
    if day and day != "all":
        sql += " AND substr(received_at, 1, 10) = ?"
        args.append(day)
    if chat_id:
        sql += " AND (chat_id = ? OR publisher_id = ?)"
        args.extend([chat_id, chat_id])
    if scene in ("push", "shared", "text"):
        sql += " AND scene = ?"
        args.append(scene)
    if keyword:
        sql += (" AND (title LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\' "
                "OR digest LIKE ? ESCAPE '\\' OR publisher_name LIKE ? ESCAPE '\\')")
        safe = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.extend(["%" + safe + "%"] * 4)
    try:
        total = conn.execute("SELECT COUNT(*) " + sql, tuple(args)).fetchone()[0]
        rows = conn.execute(
            "SELECT official_key, chat_id, publisher_id, publisher_name, sender_id, "
            "sender_name, is_group, scene, title, url, digest, cover_url, "
            "published_at, received_at " + sql +
            " ORDER BY received_at DESC, official_key DESC LIMIT ? OFFSET ?",
            tuple(args + [limit, offset])).fetchall()
    except sqlite3.OperationalError:
        return empty
    finally:
        conn.close()
    meta = load_chat_meta()
    items = []
    for row in rows:
        item = dict(row)
        publisher = item.get("publisher_id") or item.get("chat_id")
        item["publisher_name"] = (item.get("publisher_name")
                                  or chat_display(publisher, meta))
        item["avatar_url"] = (avatar_for(publisher, meta)
                              or avatar_for(item.get("chat_id"), meta))
        item["chat_name"] = chat_display(item.get("chat_id"), meta)
        item["sender_label"] = item.get("sender_name") or item.get("sender_id") or ""
        item["received_at"] = fmt_time(item.get("received_at"))
        item["published_at"] = fmt_time(item.get("published_at"))
        items.append(item)
    return {"day": day, "count": len(items), "total": total, "official": items}


# ---------------------------------------------------------------- 消息聚合时间线

FEED_KIND_LABELS = {
    "message": "文本消息",
    "link:card": "分享卡片",
    "link:text": "文本网址",
    "link:official": "公众号文章",
    "official:push": "公众号推送",
    "official:shared": "群内分享",
    "official:text": "公众号文字",
    "image": "图片消息",
    "image:photo": "图片消息",
}
FEED_SOURCES = ("group", "contact", "official", "helper", "system")
FEED_KIND_GROUPS = {
    "message": {"文本消息"},
    "link": {"分享卡片", "文本网址", "公众号文章"},
    "official": {"公众号推送", "群内分享", "公众号文字"},
    "image": {"图片消息"},
}


def safe_feed_url(value):
    """只允许 http(s) 链接进入页面，避免 javascript: 之类的伪协议。"""
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""


def _feed_stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _merge_feed_duplicates(items):
    """同一条内容可能被采集多次，合并成一条并保留多重来源标签。

    1) 文本消息里粘贴的网址会同时进 links 表 -> 并回原消息，标「文本网址」；
    2) 群内分享的公众号文章会同时进 official 和 links 表 -> 保留信息更全的一条。
    """
    messages = [item for item in items if item["kind"] == "message"]
    drop = set()
    for link in items:
        if link["kind"] != "link" or link["subtype"] != "text" or not link["url"]:
            continue
        stamp = _feed_stamp(link["received_at_raw"])
        for msg in messages:
            if msg["chat_id"] != link["chat_id"]:
                continue
            if abs(_feed_stamp(msg["received_at_raw"]) - stamp) > 90:
                continue
            body = msg["body"].strip()
            if link["url"] in body or (body and body in link["url"]):
                labels = msg.setdefault("merged_labels", [])
                if "文本网址" not in labels:
                    labels.append("文本网址")
                msg["url"] = link["url"]
                drop.add(link["id"])
                break

    priority = {"official": 3, "link": 2, "message": 1}
    ordered = sorted((item for item in items if item["id"] not in drop),
                     key=lambda item: (item["received_at_raw"],
                                       priority.get(item["kind"], 0)), reverse=True)
    buckets, result = {}, []
    for item in ordered:
        url = item["url"].split("?")[0].rstrip("/") if item["url"] else ""
        title = item["title"].strip()
        key = ("article", url, title[:80]) if url and title else None
        stamp = _feed_stamp(item["received_at_raw"])
        merged = False
        if key:
            for prev_stamp, prev in buckets.get(key, []):
                if abs(prev_stamp - stamp) > 300:
                    continue
                labels = prev.setdefault("merged_labels", [])
                if item["kind_label"] not in labels:
                    labels.append(item["kind_label"])
                if not prev["publisher_name"]:
                    prev["publisher_name"] = item["publisher_name"]
                if not prev["cover_url"]:
                    prev["cover_url"] = item["cover_url"]
                merged = True
                break
            if not merged:
                buckets.setdefault(key, []).append((stamp, item))
        if not merged:
            result.append(item)
    result.sort(key=lambda item: (item["received_at_raw"], item["item_key"]),
                reverse=True)
    return result


def query_feed(day="", chat_id="", keyword="", kind="", source="",
               limit=50, offset=0):
    """把三类数据合成一条按时间倒序的消息流。

    kind   内容类型：message / link / official，或其细分 link:card、official:push …
    source 会话来源：group / contact / official / helper / system
    返回项同时带「内容类型」和「会话来源」两级来源标记 + 真实会话名与头像。
    """
    empty = {"day": day, "count": 0, "total": 0, "items": [], "counts": {}}
    conn = connect(DB_PATH)
    if conn is None:
        return empty
    day = str(day or "").strip() or date.today().isoformat()
    all_days = day == "all"
    needle = str(keyword or "").strip().casefold()

    def fetch(table, extra="", extra_args=(), fields="*"):
        where, args = [], []
        if not all_days:
            where.append("substr(received_at, 1, 10) = ?")
            args.append(day)
        if chat_id:
            where.append(extra or "chat_id = ?")
            args.extend(extra_args or (chat_id,))
        sql = "SELECT %s FROM %s" % (fields, table)
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            return [dict(row) for row in conn.execute(sql, tuple(args)).fetchall()]
        except sqlite3.OperationalError:
            return []

    def base(row, item_kind, subtype, fields):
        item = {
            "item_key": str(row.get(fields[0]) or ""),
            "kind": item_kind,
            "subtype": subtype,
            "chat_id": str(row.get("chat_id") or ""),
            "sender_id": str(row.get("sender_id") or ""),
            "sender_name": str(row.get("sender_name") or ""),
            "is_group": bool(row.get("is_group")),
            "title": "", "body": "", "url": "", "description": "",
            "publisher_id": "", "publisher_name": "", "cover_url": "",
            "published_at": "", "received_at_raw": str(row.get("received_at") or ""),
        }
        return item

    rows = []
    for row in fetch("messages"):
        item = base(row, "message", "", ("msg_key",))
        item["body"] = str(row.get("text") or "")
        rows.append(item)
    for row in fetch("links"):
        subtype = str(row.get("kind") or "card")
        item = base(row, "link", subtype, ("link_key",))
        item["title"] = str(row.get("title") or "")
        item["url"] = safe_feed_url(row.get("url"))
        item["description"] = str(row.get("description") or "")
        item["publisher_name"] = str(row.get("source") or "")
        rows.append(item)
    for row in fetch("official", "(chat_id = ? OR publisher_id = ?)",
                     (chat_id, chat_id)):
        subtype = str(row.get("scene") or "push")
        item = base(row, "official", subtype, ("official_key",))
        item["title"] = str(row.get("title") or "")
        item["url"] = safe_feed_url(row.get("url"))
        item["description"] = str(row.get("digest") or "")
        item["publisher_id"] = str(row.get("publisher_id") or "")
        item["publisher_name"] = str(row.get("publisher_name") or "")
        item["cover_url"] = safe_feed_url(row.get("cover_url"))
        item["published_at"] = fmt_time(row.get("published_at"))
        rows.append(item)
    # 聊天图片：helper 已解密落在本地，作为一类消息并入同一条时间线
    for rec in image_records():
        stamp = str(rec.get("received_at") or "")
        if not all_days and not stamp.startswith(day):
            continue
        if chat_id and rec.get("chat_id") != chat_id:
            continue
        item = base({"chat_id": rec.get("chat_id"),
                     "received_at": stamp,
                     "filename": rec.get("filename")},
                    "image", "photo", ("filename",))
        item["image_filename"] = rec.get("filename") or ""
        item["image_width"] = rec.get("width") or 0
        item["image_height"] = rec.get("height") or 0
        item["image_size"] = rec.get("size_label") or ""
        rows.append(item)
    conn.close()

    auto = load_auto_chats()
    meta = load_chat_meta()
    unified = []
    for item in rows:
        cid = item["chat_id"]
        item["id"] = "%s:%s" % (item["kind"], item["item_key"])
        item["kind_label"] = FEED_KIND_LABELS.get(
            "%s:%s" % (item["kind"], item["subtype"]),
            FEED_KIND_LABELS.get(item["kind"], "消息"))
        item["source_kind"] = kind_of(cid, auto)
        item["source_label"] = KIND_LABELS.get(item["source_kind"], "其他")
        item["chat_name"] = chat_display(cid, meta)
        item["avatar_url"] = avatar_for(cid, meta) or avatar_for(
            item["publisher_id"], meta)
        item["sender_label"] = item["sender_name"] or item["sender_id"] or ""
        if item["kind"] == "official" and not item["sender_label"]:
            item["sender_label"] = item["publisher_name"] or "公众号"
        if needle and needle not in " ".join((
                item["body"], item["title"], item["url"], item["description"],
                item["publisher_name"], item["sender_label"],
                item["chat_name"], cid,
                str(item.get("image_filename") or ""))).casefold():
            continue
        unified.append(item)

    unified = _merge_feed_duplicates(unified)
    counts = {"all": len(unified), "message": 0, "link": 0,
              "official": 0, "image": 0}
    for key in FEED_SOURCES:
        counts["source_" + key] = 0
    def labels_of(item):
        return [item["kind_label"]] + list(item.get("merged_labels") or [])

    for item in unified:
        labels = labels_of(item)
        for group, names in FEED_KIND_GROUPS.items():
            if any(label in names for label in labels):
                counts[group] += 1
        source_key = "source_" + item["source_kind"]
        counts[source_key] = counts.get(source_key, 0) + 1

    def matched(item):
        if kind and kind != "all":
            labels = labels_of(item)
            if ":" in kind:
                label = FEED_KIND_LABELS.get(kind, "")
                if label not in labels and "%s:%s" % (item["kind"], item["subtype"]) != kind:
                    return False
            elif not any(label in FEED_KIND_GROUPS.get(kind, ())
                         for label in labels):
                return False
        if source and source != "all" and item["source_kind"] != source:
            return False
        return True

    unified = [item for item in unified if matched(item)]
    unified.sort(key=lambda item: (item["received_at_raw"], item["item_key"]),
                 reverse=True)
    total = len(unified)
    page = unified[offset:offset + limit]
    for item in page:
        item["received_at"] = fmt_time(item["received_at_raw"])
    return {"day": day, "count": len(page), "total": total,
            "items": page, "counts": counts}


# 主号的自定义微信号：helper 上报的 sender_id 常是 Alias 而不是 wxid。
# 如需补充/覆盖，可在 admin/self_senders.json 里写字符串数组。
SELF_SENDER_FALLBACK = ("pigwan",)


def self_sender_ids(auto=None):
    """「我」的 sender 标识集合：登录账号、内存中 is_self 的会话及其别名。"""
    auto = load_auto_chats() if auto is None else auto
    ids = {str(x).strip().casefold() for x in SELF_SENDER_FALLBACK if str(x).strip()}
    try:
        extra = read_json(ROOT / "self_senders.json")
    except (OSError, ValueError, TypeError):
        extra = None
    if isinstance(extra, list):
        ids |= {str(x).strip().casefold() for x in extra if str(x).strip()}
    account = str((wechat_health() or {}).get("account") or "").strip()
    if account:
        ids.add(account.casefold())

    def _names(row):
        out = set()
        if isinstance(row, dict):
            for key in ("alias", "wechat_name", "name"):
                value = str(row.get(key) or "").strip()
                if value:
                    out.add(value.casefold())
        return out

    for cid, row in auto.items():
        if isinstance(row, dict) and row.get("is_self"):
            ids.add(str(cid).casefold())
            ids |= _names(row)
    if account:
        ids |= _names(auto.get(account))
    return sorted(ids)


def query_conversations(day="all", keyword="", source="", limit=200):
    """按会话聚合全部采集内容，供「对话」视图的左侧会话列表使用。

    与消息流共用同一批数据源（文本 / 链接 / 公众号 / 图片），只做分组统计，
    不改动任何存储；返回 self_ids 让前端把「我」发出的消息排到右侧气泡。
    """
    conn = connect(DB_PATH)
    empty = {"day": day, "items": [], "total": 0, "self_ids": []}
    if conn is None:
        return empty
    day = str(day or "all").strip() or "all"
    all_days = day == "all"
    needle = str(keyword or "").strip().casefold()
    auto = load_auto_chats()
    meta = load_chat_meta()
    groups = {}

    def add(cid, stamp, preview, sender, item_kind, searchable):
        cid, stamp = str(cid or "").strip(), str(stamp or "")
        if not cid or (not all_days and stamp[:10] != day):
            return
        name = chat_display(cid, meta)
        if needle and needle not in " ".join(
                (str(searchable), cid, name)).casefold():
            return
        group = groups.setdefault(cid, {
            "chat_id": cid, "count": 0, "last_stamp": "", "last_preview": "",
            "last_sender": "", "last_kind": "",
            "kinds": {"message": 0, "link": 0, "official": 0, "image": 0}})
        group["count"] += 1
        group["kinds"][item_kind] = group["kinds"].get(item_kind, 0) + 1
        if stamp >= group["last_stamp"]:
            group["last_stamp"] = stamp
            group["last_preview"] = str(preview or "").replace("\n", " ").strip()[:120]
            group["last_sender"] = str(sender or "").strip()
            group["last_kind"] = item_kind

    def rows_of(sql):
        try:
            return [dict(row) for row in conn.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            return []

    for row in rows_of("SELECT chat_id, sender_id, sender_name, text, received_at "
                       "FROM messages"):
        add(row["chat_id"], row["received_at"], row["text"] or "（无文本内容）",
            row["sender_name"] or row["sender_id"], "message",
            " ".join((str(row["text"] or ""), str(row["sender_name"] or ""))))
    for row in rows_of("SELECT chat_id, sender_id, sender_name, title, url, "
                       "description, source, received_at FROM links"):
        preview = row["title"] or row["url"] or "链接消息"
        add(row["chat_id"], row["received_at"], preview,
            row["sender_name"] or row["sender_id"] or row["source"], "link",
            " ".join((str(row["title"] or ""), str(row["url"] or ""),
                      str(row["description"] or ""))))
    for row in rows_of("SELECT chat_id, sender_id, sender_name, publisher_name, "
                       "title, digest, url, received_at FROM official"):
        add(row["chat_id"], row["received_at"],
            row["title"] or row["digest"] or "公众号文章",
            row["publisher_name"] or row["sender_name"] or "公众号", "official",
            " ".join((str(row["title"] or ""), str(row["digest"] or ""),
                      str(row["publisher_name"] or ""))))
    for rec in image_records():
        add(rec.get("chat_id"), rec.get("received_at"), "[图片]",
            "", "image", str(rec.get("filename") or ""))
    conn.close()

    items = []
    for group in groups.values():
        cid = group["chat_id"]
        source_kind = kind_of(cid, auto)
        if source and source != "all" and source_kind != source:
            continue
        items.append({
            "chat_id": cid,
            "name": chat_display(cid, meta),
            "avatar_url": avatar_for(cid, meta),
            "source_kind": source_kind,
            "source_label": KIND_LABELS.get(source_kind, "其他"),
            "message_count": group["count"],
            "kinds": group["kinds"],
            "last_at": fmt_time(group["last_stamp"]),
            "last_stamp": group["last_stamp"],
            "last_preview": group["last_preview"] or "（无文本内容）",
            "last_sender": group["last_sender"],
            "last_kind": group["last_kind"],
        })
    items.sort(key=lambda x: (x["last_stamp"], x["message_count"]), reverse=True)
    total = len(items)
    limit = max(1, min(int(limit), 500))
    return {"day": day, "items": items[:limit], "total": total,
            "self_ids": self_sender_ids(auto)}


def query_publishers(keyword="", limit=400):
    """已关注的公众号目录（来自微信内存只读同步），即使还没收到推送也能筛选。"""
    try:
        auto = load_auto_chats()
    except (OSError, ValueError, TypeError):
        auto = {}
    meta = load_chat_meta()
    needle = str(keyword or "").strip().casefold()
    rows = []
    for chat_id, raw in auto.items():
        chat_id = str(chat_id)
        if not (chat_id.startswith("gh_") or
                (isinstance(raw, dict) and raw.get("kind") == "official")):
            continue
        row = raw if isinstance(raw, dict) else {}
        name = chat_display(chat_id, meta)
        if needle and needle not in " ".join((
                chat_id, name, str(row.get("alias", "")),
                str(row.get("wechat_name", "")))).casefold():
            continue
        rows.append({"chat_id": chat_id, "name": name,
                     "alias": str(row.get("alias", "")),
                     "avatar_url": avatar_for(chat_id, meta)})
    rows.sort(key=lambda item: item["name"])
    return rows[:max(1, min(int(limit), 1000))]


def _probe_media(path):
    """只读图片头，拿到真实格式与像素尺寸；Pillow 缺失时退化为文件头解析。"""
    info = {"format": "", "width": 0, "height": 0}
    try:
        from PIL import Image
        with Image.open(path) as im:
            info.update(format=(im.format or "").upper(),
                        width=im.size[0], height=im.size[1])
        return info
    except Exception:
        pass
    try:
        head = path.open("rb").read(32)
    except OSError:
        return info
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        info.update(format="PNG",
                    width=int.from_bytes(head[16:20], "big"),
                    height=int.from_bytes(head[20:24], "big"))
    elif head[:3] == b"GIF8" and len(head) >= 10:
        info.update(format="GIF",
                    width=int.from_bytes(head[6:8], "little"),
                    height=int.from_bytes(head[8:10], "little"))
    elif head[:2] == b"\xff\xd8":
        # 无 Pillow 时扫描 JPEG SOF 标记读取宽高
        try:
            data = path.open("rb").read(65536)
            pos = 2
            sof = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                   0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
                    pos += 2
                    continue
                length = int.from_bytes(data[pos + 2:pos + 4], "big")
                if marker in sof:
                    info.update(format="JPEG",
                                height=int.from_bytes(data[pos + 5:pos + 7], "big"),
                                width=int.from_bytes(data[pos + 7:pos + 9], "big"))
                    break
                if length <= 0:
                    break
                pos += 2 + length
        except OSError:
            pass
    return info


def _media_log_index():
    """bot.log 的「图片接收」行给出会话 ID、接收时间与下载状态。"""
    index = {}
    if not LOG_FILE.is_file():
        return index
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return index
    for line in text.splitlines():
        match = MEDIA_LOG_RE.match(line.strip())
        if not match:
            continue
        row = match.groupdict()
        name = Path(str(row.get("file") or "").strip()).name
        if name:
            index[name] = {"time": row.get("time") or "",
                           "chat_id": str(row.get("chat_id") or "").strip(),
                           "status": str(row.get("status") or "").strip()}
    return index


def image_records():
    """扫描解密图片，关联会话、群名、头像与尺寸；10 秒缓存。"""
    now = time.time()
    with _image_lock:
        if now - _image_cache["at"] < 10 and _image_cache["records"]:
            return _image_cache["records"]
    meta = load_chat_meta()
    auto = load_auto_chats()
    log = _media_log_index()
    records = []
    if IMAGE_DIR.is_dir():
        for folder in sorted(IMAGE_DIR.iterdir()):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if not path.is_file() or not MEDIA_NAME_RE.fullmatch(path.name):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                hit = log.get(path.name, {})
                chat_id = hit.get("chat_id", "")
                stamp = hit.get("time") or datetime.fromtimestamp(
                    st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                probe = _probe_media(path)
                records.append({
                    "filename": path.name,
                    "chat_id": chat_id,
                    "chat_name": chat_display(chat_id, meta) if chat_id else "未知会话",
                    "chat_kind": kind_of(chat_id, auto) if chat_id else "system",
                    "avatar_url": avatar_for(chat_id, meta) if chat_id else "",
                    "received_at": stamp,
                    "status": hit.get("status") or "saved",
                    "format": probe["format"],
                    "width": probe["width"],
                    "height": probe["height"],
                    "size_bytes": st.st_size,
                    "size_label": "%.0f KB" % (st.st_size / 1024.0),
                    "url": "/media/" + quote(path.name),
                })
    records.sort(key=lambda r: (r["received_at"], r["filename"]), reverse=True)
    with _image_lock:
        _image_cache["at"] = now
        _image_cache["records"] = records
    return records


def query_images(day="all", chat_id="", keyword="", limit=24, offset=0):
    """图片墙数据：按日期 / 会话 / 关键词过滤，附统计与会话下拉。"""
    records = image_records()
    today = date.today().isoformat()
    kw = str(keyword or "").strip().lower()

    def matched(row):
        if day and day != "all" and not str(row["received_at"]).startswith(day):
            return False
        if chat_id and row["chat_id"] != chat_id:
            return False
        if kw:
            haystack = " ".join((row["chat_name"], row["chat_id"],
                                 row["filename"], row["received_at"])).lower()
            if kw not in haystack:
                return False
        return True

    picked = [row for row in records if matched(row)]
    chat_map = {}
    for row in records:
        if not row["chat_id"]:
            continue
        item = chat_map.setdefault(row["chat_id"], {
            "chat_id": row["chat_id"], "chat_name": row["chat_name"],
            "count": 0})
        item["count"] += 1
    chats = sorted(chat_map.values(),
                   key=lambda x: (-x["count"], x["chat_name"]))
    return {
        "items": picked[offset:offset + limit],
        "total": len(picked),
        "counts": {
            "all": len(records),
            "today": sum(1 for r in records
                         if str(r["received_at"]).startswith(today)),
            "chats": len(chats),
        },
        "chats": chats,
    }


def resolve_media(name):
    """只允许严格格式的文件名，并在固定目录内查找，杜绝路径穿越。"""
    if not MEDIA_NAME_RE.fullmatch(name) or not IMAGE_DIR.is_dir():
        return None
    for folder in IMAGE_DIR.iterdir():
        if not folder.is_dir():
            continue
        target = folder / name
        if target.is_file():
            return target
    return None


def media_payload(name, width=0):
    """返回 (bytes, content-type)；width 命中白名单时返回居中裁切的缩略图。"""
    target = resolve_media(name)
    if target is None:
        return None, None
    ctype = MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    try:
        if not width:
            return target.read_bytes(), ctype
        thumb = THUMB_DIR / (target.stem + "-w%d.jpg" % width)
        if (not thumb.is_file()
                or thumb.stat().st_mtime < target.stat().st_mtime):
            from PIL import Image
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 3)
            height = int(width * 0.75)
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            with Image.open(target) as im:
                ratio = max(width / float(im.width), height / float(im.height))
                nw = max(1, int(im.width * ratio))
                nh = max(1, int(im.height * ratio))
                scaled = im.convert("RGBA").resize((nw, nh), resample)
                left, top = max(0, (nw - width) // 2), max(0, (nh - height) // 2)
                cropped = scaled.crop((left, top, left + width, top + height))
                flat = Image.new("RGB", (width, height), (255, 255, 255))
                flat.paste(cropped, mask=cropped.split()[-1])
                flat.save(thumb, "JPEG", quality=82, optimize=True)
        return thumb.read_bytes(), "image/jpeg"
    except Exception:
        # 缩略图生成失败时回退原图，保证图片始终可见
        try:
            return target.read_bytes(), ctype
        except OSError:
            return None, None


def query_chats():
    conn = connect(DB_PATH)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT chat_id, COUNT(*) AS n, MAX(received_at) AS last_at "
            "FROM messages GROUP BY chat_id ORDER BY n DESC").fetchall()
        meta = load_chat_meta()
        result = []
        for row in rows:
            cid = row["chat_id"]
            result.append({"chat_id": cid, "n": row["n"],
                           "last_at": fmt_time(row["last_at"]),
                           "name": chat_display(cid, meta),
                           "avatar_url": avatar_for(cid, meta),
                           "is_group": str(cid).endswith("@chatroom")})
        return result
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def ensure_outbox_columns(conn):
    """老库可能没有 at_names 列（插件升级时也会补），写入前统一补齐。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    if cols and "at_names" not in cols:
        conn.execute("ALTER TABLE outbox ADD COLUMN at_names TEXT NOT NULL DEFAULT ''")
        conn.commit()


def reminders_conn():
    """打开共享队列库，并确保 outbox / reminders 两张表可用。"""
    OUTBOX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OUTBOX_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(OUTBOX_SCHEMA)
    conn.execute(REMINDERS_SCHEMA)
    ensure_outbox_columns(conn)
    conn.commit()
    return conn


def enqueue(chat_id, text, at_list="", at_names=""):
    OUTBOX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OUTBOX_DB), timeout=5)
    try:
        conn.execute(OUTBOX_SCHEMA)
        ensure_outbox_columns(conn)
        conn.commit()
        cur = conn.execute(
            "INSERT INTO outbox (chat_id, text, at_list, at_names, status, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (chat_id, text, at_list, at_names,
             datetime.now().astimezone().isoformat(timespec="seconds")))
        rid = cur.lastrowid
        conn.commit()
        return rid
    finally:
        conn.close()


def outbox_status(rid):
    conn = connect(OUTBOX_DB)
    if conn is None:
        return {"status": 0, "error": ""}
    try:
        row = conn.execute("SELECT status, error FROM outbox WHERE id = ?", (rid,)).fetchone()
        return {"status": row[0], "error": row[1] or ""} if row else {"status": -1, "error": ""}
    except sqlite3.OperationalError:
        return {"status": 0, "error": ""}
    finally:
        conn.close()


def outbox_recent(limit=20):
    conn = connect(OUTBOX_DB)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, chat_id, substr(text, 1, 60) AS preview, status, error, "
            "created_at, finished_at FROM outbox ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        meta = load_chat_meta()
        items = []
        for row in rows:
            item = dict(row)
            item["created_at"] = fmt_time(item["created_at"])
            item["finished_at"] = fmt_time(item["finished_at"])
            item["chat_name"] = chat_display(item["chat_id"], meta)
            item["avatar_url"] = avatar_for(item["chat_id"], meta)
            items.append(item)
        return items
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------- 定时提醒（应用页）
REMINDER_STATUS_LABELS = {"active": "进行中", "paused": "已停止", "done": "已完成",
                          "error": "异常"}
REMINDER_KIND_LABELS = {"once": "一次性", "interval": "间隔循环",
                        "daily": "每天", "weekly": "每周"}
REMINDER_ACTIONS = ("stop", "resume", "send", "delete")


def ts_text(value):
    """epoch 秒 -> 本地时间字符串，0/空值返回空串。"""
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def reminder_heartbeat():
    """插件心跳里是否声明已加载提醒引擎（旧插件没有这个字段）。"""
    try:
        return bool(read_json(WORKER_HEARTBEAT).get("reminders"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def reminders_payload(status="", q="", kind="", delivery="", limit=500, offset=0):
    """读取共享库里的全部提醒任务，附带筛选计数与格式化字段。"""
    meta = load_chat_meta()
    conn = reminders_conn()
    try:
        # 进行中的排最前且按「最快到期」升序，其余按创建时间倒序
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM reminders ORDER BY "
            "CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 "
            "WHEN 'done' THEN 2 ELSE 3 END, "
            "CASE WHEN status = 'active' THEN next_ts ELSE -id END ASC, "
            "id DESC").fetchall()]
        items = []
        counts = {"all": len(rows), "active": 0, "paused": 0, "done": 0, "error": 0}
        kind_counts = {"all": len(rows)}
        delivery_counts = {"all": len(rows)}
        needle = str(q or "").strip().lower()
        now = time.time()
        for r in rows:
            st = str(r.get("status") or "active")
            kd = str(r.get("kind") or "once")
            dv = str(r.get("delivery") or "group")
            counts[st] = counts.get(st, 0) + 1
            kind_counts[kd] = kind_counts.get(kd, 0) + 1
            delivery_counts[dv] = delivery_counts.get(dv, 0) + 1
            if status and st != status:
                continue
            if kind and kd != kind:
                continue
            if delivery and dv != delivery:
                continue
            if needle:
                hay = " ".join(str(r.get(k) or "") for k in (
                    "content", "raw_text", "chat_name", "chat_id", "creator_name",
                    "creator_id", "target_names", "targets", "rule_text", "dm_to"))
                if needle not in hay.lower():
                    continue
            r["status_label"] = REMINDER_STATUS_LABELS.get(st, st)
            r["kind_label"] = REMINDER_KIND_LABELS.get(kd, kd)
            r["delivery_label"] = "私信回复（不@）" if dv == "dm" else "群里 @ 回复"
            r["chat_display"] = chat_display(r.get("chat_id") or "", meta)
            r["next_at"] = ts_text(r.get("next_ts"))
            r["last_fire_at"] = ts_text(r.get("last_fire_ts"))
            r["created_at_fmt"] = fmt_time(r.get("created_at")) or ts_text(r.get("created_ts"))
            r["updated_at_fmt"] = ts_text(r.get("updated_ts"))
            r["countdown_sec"] = max(0, int((float(r.get("next_ts") or 0)) - now))
            r["overdue"] = bool(st == "active" and float(r.get("next_ts") or 0) <= now)
            r["rule_desc"] = reminder_desc(r.get("rule_json"), dv)
            r["targets_arr"] = [x for x in str(r.get("targets") or "").split(",") if x]
            r["target_names_arr"] = [x for x in str(r.get("target_names") or "").split(",") if x]
            items.append(r)
        total = len(items)
        items = items[max(0, int(offset)):max(0, int(offset)) + max(1, min(int(limit), 1000))]
        worker_ok, worker_time = worker_alive()
        hint = ""
        if not reminder_heartbeat():
            hint = "提醒引擎尚未加载：请在 start.bat 黑窗口输入 reload report_bridge（管理台改动也需重启 admin/app.py）"
        elif not worker_ok:
            hint = "发送模块心跳已超时，到期提醒不会自动发出，请检查桥接进程"
        return {"items": items, "total": total, "counts": counts,
                "kind_counts": kind_counts, "delivery_counts": delivery_counts,
                "worker": worker_ok, "worker_time": worker_time,
                "engine": reminder_heartbeat(), "core": reminder_core() is not None,
                "db": str(OUTBOX_DB), "hint": hint}
    finally:
        conn.close()


def reminder_action(action, rid):
    """停止 / 恢复 / 立即发送 / 删除一条提醒任务。"""
    if action not in REMINDER_ACTIONS:
        raise ValueError("不支持的操作：%s" % action)
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        raise ValueError("任务 ID 无效")
    if rid <= 0:
        raise ValueError("任务 ID 无效")
    conn = reminders_conn()
    try:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return {"ok": False, "error": "任务不存在或已被删除"}
        item = dict(row)
        now = time.time()
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        if action == "delete":
            conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
            conn.commit()
            return {"ok": True, "action": action, "id": rid, "message": "已删除任务 #%d" % rid}
        if action == "stop":
            conn.execute(
                "UPDATE reminders SET status = 'paused', updated_ts = ? WHERE id = ?",
                (now, rid))
            conn.commit()
            return {"ok": True, "action": action, "id": rid,
                    "message": "已停止 #%d，不会再自动触发" % rid}
        if action == "resume":
            nxt = float(item.get("next_ts") or 0)
            if nxt <= now:
                core = reminder_core()
                fn = getattr(core, "next_fire", None) if core else None
                computed = None
                if str(item.get("kind") or "once") != "once" and callable(fn):
                    try:
                        computed = fn(item.get("rule_json"), now)
                    except Exception:
                        computed = None
                nxt = float(computed) if computed else now + 1
            conn.execute(
                "UPDATE reminders SET status = 'active', next_ts = ?, updated_ts = ?, "
                "last_error = '' WHERE id = ?", (nxt, now, rid))
            conn.commit()
            return {"ok": True, "action": action, "id": rid, "next_at": ts_text(nxt),
                    "message": "已恢复 #%d，下次触发 %s" % (rid, ts_text(nxt))}
        # send：与插件到期投递走同一套格式化逻辑，只入队、不改排程
        sent = reminder_delivery(item)
        if not sent.get("chat_id") or not sent.get("text"):
            return {"ok": False, "error": "投递信息不完整（缺少会话或正文）"}
        outbox_id = enqueue(sent["chat_id"], sent["text"],
                            sent.get("at_list", ""), sent.get("at_names", ""))
        conn.execute(
            "UPDATE reminders SET fire_count = fire_count + 1, last_fire_ts = ?, "
            "updated_ts = ? WHERE id = ?", (now, now, rid))
        conn.commit()
        result = {"ok": True, "action": "send", "id": rid, "outbox_id": outbox_id,
                  "chat_id": sent["chat_id"], "message": "已加入发送队列 #%d" % outbox_id}
        if not sender_ready():
            result["warning"] = ("消息已入队，但桥接尚未加载发送模块："
                                 "请在 start.bat 黑窗口输入 reload report_bridge，加载后会自动补发。")
        return result
    finally:
        conn.close()


def thread_stamp(value):
    """把 ISO 8601 / 日志时间统一成可字典序比较的 YYYY-MM-DD HH:MM:SS。"""
    text = str(value or "").strip().replace("T", " ")
    return text[:19] if len(text) >= 19 else text


def chat_thread_payload(raw_chat, limit=120):
    """聊天窗口的只读聚合视图：接收消息 + 己方已发消息合成一条时间线。

    插件配置为 include_self=false，/api/send 发出的消息只落在 send_outbox.sqlite3，
    不会回流到 messages.sqlite3，因此必须单独并入才有「我」的绿色气泡。
    不修改任何表结构，也不触碰 helper 插件。
    """
    chat_id, resolved = resolve_chat_id(raw_chat)
    if not chat_id:
        return {"error": str(resolved), "chat_id": str(raw_chat or ""),
                "items": [], "total": 0, "has_more": False}
    try:
        limit = min(max(int(limit), 1), 200)
    except (TypeError, ValueError):
        limit = 120

    auto = load_auto_chats()
    meta = load_chat_meta()
    source_kind = kind_of(chat_id, auto)
    chat_avatar = avatar_for(chat_id, meta)
    self_ids = {str(x).casefold() for x in self_sender_ids(auto) if str(x).strip()}
    avatars = social_avatar_index(social_data())
    self_avatar = ""
    for cid, row in auto.items():
        if isinstance(row, dict) and row.get("is_self"):
            self_avatar = avatar_for(cid, meta)
            if self_avatar:
                break

    items = []
    feed = query_feed("all", chat_id, "", "", "", limit, 0)
    feed_total = int(feed.get("total") or 0)
    for row in feed.get("items") or []:
        sid = str(row.get("sender_id") or "").casefold()
        sname = str(row.get("sender_name") or "").casefold()
        is_self = bool((sid and sid in self_ids) or (sname and sname in self_ids))
        wxid = str(row.get("sender_id") or "")
        fallback = self_avatar if is_self else (
            "" if source_kind == "group" else chat_avatar)
        items.append({
            "key": "feed:" + str(row.get("id") or ""),
            "ts": thread_stamp(row.get("received_at_raw")),
            "time": fmt_time(row.get("received_at_raw")),
            "self": is_self,
            "sender": row.get("sender_label") or row.get("chat_name") or "",
            "sender_id": wxid,
            "avatar_url": avatars.get(wxid) or fallback,
            "kind": row.get("kind") or "message",
            "kind_label": row.get("kind_label") or "",
            "body": str(row.get("body") or ""),
            "title": str(row.get("title") or ""),
            "url": str(row.get("url") or ""),
            "description": str(row.get("description") or ""),
            "cover_url": str(row.get("cover_url") or ""),
            "image_filename": str(row.get("image_filename") or ""),
            "image_width": row.get("image_width") or 0,
            "image_height": row.get("image_height") or 0,
            "status": None,
        })

    sent_total = 0
    conn = connect(OUTBOX_DB)
    if conn is not None:
        try:
            sent_total = int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0] or 0)
            rows = conn.execute(
                "SELECT id, text, status, error, created_at FROM outbox "
                "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit)).fetchall()
            for row in rows:
                item = dict(row)
                stamp = str(item.get("created_at") or "")
                items.append({
                    "key": "out:%d" % int(item.get("id") or 0),
                    "ts": thread_stamp(stamp),
                    "time": fmt_time(stamp),
                    "self": True,
                    "sender": "我",
                    "sender_id": "",
                    "avatar_url": self_avatar,
                    "kind": "text",
                    "kind_label": "已发消息",
                    "body": str(item.get("text") or ""),
                    "title": "", "url": "", "description": "", "cover_url": "",
                    "image_filename": "", "image_width": 0, "image_height": 0,
                    "status": int(item.get("status") or 0),
                    "error": str(item.get("error") or ""),
                    "rid": int(item.get("id") or 0),
                })
        except (sqlite3.OperationalError, IndexError, TypeError, ValueError):
            pass
        finally:
            conn.close()

    items.sort(key=lambda x: (x["ts"], x["key"]))
    total = feed_total + sent_total
    page = items[-limit:]
    return {
        "chat_id": chat_id,
        "chat_name": chat_display(chat_id, meta),
        "chat_avatar": chat_avatar,
        "source_kind": source_kind,
        "resolver": resolved,
        "count": len(page), "total": total, "has_more": total > len(page),
        "limit": limit, "items": page,
        "self_ids": sorted(self_ids), "self_avatar": self_avatar,
        "send_enabled": not READONLY_MODE,
        "sender_ready": sender_ready(),
        "image_send_enabled": False,
        "image_send_note": "图片发送需新增上传接口与 helper 插件分派，本期未接入；历史收到的图片仍可查看",
        "note": "己方气泡来自发送队列；采集插件 include_self=false、only_groups=true，好友/公众号的历史可能为空",
    }


def config_payload():
    cfg = read_json(BOT_CONFIG)
    plugin = cfg.get("plugins", {}).get("report_bridge", {})
    local = read_json(LOCAL_CONFIG)
    return {"chat_allowlist": cfg.get("chat_allowlist", []),
            "only_groups": plugin.get("only_groups", True),
            "include_self": plugin.get("include_self", False),
            "collect_official": plugin.get("collect_official", True),
            "group_allowlist": plugin.get("group_allowlist", []),
            "batch_size": plugin.get("batch_size", 50),
            "flush_seconds": plugin.get("flush_seconds", 30),
            "http_timeout_seconds": plugin.get("http_timeout_seconds", 5),
            "report_keywords": plugin.get("report_keywords", ["/报告"]),
            "report_top_n": plugin.get("report_top_n", 5),
            "endpoint": local.get("endpoint", ""),
            "token_hint": str(local.get("token", ""))[:6] + "…"}


def save_config(payload):
    if not isinstance(payload, dict):
        raise ValueError("配置格式无效")
    cfg = read_json(BOT_CONFIG)
    plugin = cfg.setdefault("plugins", {}).setdefault("report_bridge", {})
    changed = []
    for key, value in payload.items():
        if key in EDITABLE_NUMBERS:
            if type(value) is not int:
                raise ValueError("%s 必须是整数" % key)
            low, high = EDITABLE_NUMBERS[key]
            if not low <= value <= high:
                raise ValueError("%s 超出范围 %d-%d" % (key, low, high))
            plugin[key] = value
        elif key in EDITABLE_BOOLS:
            if type(value) is not bool:
                raise ValueError("%s 必须是布尔值" % key)
            plugin[key] = value
        elif key in EDITABLE_LISTS:
            if not isinstance(value, list):
                raise ValueError("%s 必须是列表" % key)
            items = [str(x).strip() for x in value if str(x).strip()][:100]
            if key == "chat_allowlist":
                cfg[key] = items
            else:
                plugin[key] = items
        else:
            continue
        changed.append(key)
    BOT_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return changed


class Handler(BaseHTTPRequestHandler):
    server_version = "BridgeAdmin/1.0"

    def _raw(self, status, body, content_type, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._raw(status, json.dumps(payload, ensure_ascii=False),
                  "application/json; charset=utf-8")

    def _bearer(self):
        header = self.headers.get("Authorization", "")
        parts = header.split(" ", 1)
        supplied = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
        # 公网模式禁止 URL 传令牌，避免它落进浏览器历史、反代日志和 Referer。
        if not supplied and not PUBLIC_MODE:
            supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return supplied

    def _authorized(self):
        """两种凭据都接受：长期访问令牌（程序调用）与账号密码签发的短期会话。"""
        expected = token()
        if not expected or expected == "CHANGE_ME":
            return False
        supplied = self._bearer()
        if hmac.compare_digest(supplied, expected):
            return True
        return session_valid(supplied)

    def _handle_login(self):
        if auth_record() is None:
            return self._json(404, {"error": "未启用账号密码登录，请先运行 "
                                            "python admin/app.py --set-login 账号 密码 后重启"})
        retry = login_gate()
        if retry:
            return self._json(429, {"error": "尝试过于频繁，请 %d 秒后再试" % retry})
        payload = self._body()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not verify_login(username, password):
            login_fail()
            return self._json(401, {"error": "账号或密码错误"})
        login_ok()
        return self._json(200, {"token": new_session(),
                                "expires_in": SESSION_TTL,
                                "mode": "session"})

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1024 * 1024:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, ValueError):
            return {}

    def _media(self, path, params):
        """聊天图片：与 /api/* 同样鉴权，避免公网直接读取聊天内容。"""
        if not self._authorized():
            return self._raw(401, "unauthorized", "text/plain; charset=utf-8")
        name = Path(path[len("/media/"):]).name
        try:
            raw_width = int((params.get("w") or ["0"])[0])
        except (TypeError, ValueError):
            raw_width = 0
        width = raw_width if raw_width in MEDIA_THUMB_WIDTHS else 0
        body, ctype = media_payload(name, width)
        if body is None:
            return self._raw(404, "not found", "text/plain; charset=utf-8")
        return self._raw(200, body, ctype, cache="private, max-age=3600")

    def _avatar(self, path):
        name = Path(path[len("/avatars/"):]).name
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,120}", name):
            return self._raw(404, "not found", "text/plain; charset=utf-8")
        ctype = AVATAR_TYPES.get(Path(name).suffix.lower())
        target = AVATAR_DIR / name
        if not ctype or not target.is_file():
            return self._raw(404, "not found", "text/plain; charset=utf-8")
        try:
            return self._raw(200, target.read_bytes(), ctype,
                             cache="public, max-age=300")
        except OSError:
            return self._raw(404, "not found", "text/plain; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path, params = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
        if path in STATIC:
            name, content_type = STATIC[path]
            try:
                self._raw(200, (ROOT / name).read_bytes(), content_type)
            except OSError:
                self._raw(404, "not found", "text/plain; charset=utf-8")
            return
        if path.startswith("/avatars/"):
            return self._avatar(path)
        if path.startswith("/media/"):
            return self._media(path, params)
        if not path.startswith("/api/"):
            self._raw(404, "not found", "text/plain; charset=utf-8")
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            self._api_get(path, params)
        except (ValueError, OSError, sqlite3.Error) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            self._json(500, {"error": "internal: %s" % error})

    def _api_get(self, path, p):
        def one(key, default=""):
            return (p.get(key) or [default])[0]

        if path == "/api/state":
            return self._json(200, state_payload())
        if path == "/api/feed":
            try:
                limit = min(max(int(one("limit", "50")), 1), 200)
                offset = max(int(one("offset", "0")), 0)
            except ValueError:
                limit, offset = 50, 0
            return self._json(200, query_feed(
                one("day", date.today().isoformat()), one("chat_id"), one("q"),
                one("kind"), one("source"), limit, offset))
        if path == "/api/conversations":
            try:
                conv_limit = min(max(int(one("limit", "200")), 1), 500)
            except ValueError:
                conv_limit = 200
            return self._json(200, query_conversations(
                one("day", "all"), one("q"), one("source"), conv_limit))
        if path == "/api/messages":
            try:
                limit = min(max(int(one("limit", "50")), 1), 200)
                offset = max(int(one("offset", "0")), 0)
            except ValueError:
                limit, offset = 50, 0
            return self._json(200, query_messages(
                one("day", date.today().isoformat()), one("chat_id"),
                one("q"), limit, offset))
        if path == "/api/links":
            try:
                limit = min(max(int(one("limit", "50")), 1), 200)
                offset = max(int(one("offset", "0")), 0)
            except ValueError:
                limit, offset = 50, 0
            return self._json(200, query_links(
                one("day", date.today().isoformat()), one("chat_id"),
                one("q"), limit, offset, one("kind")))
        if path == "/api/official":
            try:
                limit = min(max(int(one("limit", "50")), 1), 200)
                offset = max(int(one("offset", "0")), 0)
            except ValueError:
                limit, offset = 50, 0
            return self._json(200, query_official(
                one("day", date.today().isoformat()), one("chat_id"),
                one("q"), one("scene"), limit, offset))
        if path == "/api/images":
            try:
                image_limit = min(max(int(one("limit", "24")), 1), 100)
                image_offset = max(int(one("offset", "0")), 0)
            except ValueError:
                image_limit, image_offset = 24, 0
            return self._json(200, query_images(
                one("day", "all"), one("chat_id"), one("q"),
                image_limit, image_offset))
        if path == "/api/publishers":
            try:
                publisher_limit = int(one("limit", "400"))
            except ValueError:
                publisher_limit = 400
            publishers = query_publishers(one("q"), publisher_limit)
            return self._json(200, {"publishers": publishers,
                                    "total": len(publishers)})
        if path == "/api/chats":
            return self._json(200, {"chats": query_chats()})
        if path == "/api/contacts":
            try:
                contact_limit = int(one("limit", "400"))
            except ValueError:
                contact_limit = 400
            kinds = [x for x in one("kind").split(",") if x in KIND_ORDER]
            contacts = query_contacts(one("q"), one("system") == "1",
                                      contact_limit, kinds or None)
            everyone = query_contacts("", True, 1000)
            counts = {"all": len(everyone)}
            for key in KIND_ORDER:
                counts[key] = sum(1 for row in everyone if row["kind"] == key)
            return self._json(200, {"contacts": contacts,
                                    "total": len(contacts),
                                    "counts": counts})
        if path == "/api/chat-meta":
            meta = load_chat_meta()
            auto = read_json(CHAT_AUTO_FILE)
            manual = read_json(CHAT_META_FILE)
            auto = auto if isinstance(auto, dict) else {}
            manual = manual if isinstance(manual, dict) else {}

            def names_of(source):
                return {str(key): (value.get("name", "") if isinstance(value, dict)
                                   else str(value))
                        for key, value in source.items()}

            return self._json(200, {
                "names": {key: value.get("name", "") for key, value in meta.items()},
                "manual_names": names_of(manual),
                "auto_names": names_of(auto),
                "avatar_dir": str(AVATAR_DIR),
                "avatars": {key: avatar_for(key, meta) for key in meta},
                "sync": sync_status()})
        if path == "/api/report":
            try:
                top = min(max(int(one("top", "5")), 1), 50)
            except ValueError:
                top = 5
            day = one("day", date.today().isoformat())
            return self._json(200, decorate_stats(get_stats(day, one("chat_id") or None, top)))
        if path == "/api/report.md":
            day = one("day", date.today().isoformat())
            stats = decorate_stats(get_stats(day, one("chat_id") or None, 5))
            return self._raw(200, to_markdown(stats), "text/markdown; charset=utf-8")
        if path == "/api/send":
            try:
                rid = int(one("id", "0"))
            except ValueError:
                rid = 0
            return self._json(200, outbox_status(rid))
        if path == "/api/outbox":
            return self._json(200, {"items": outbox_recent()})
        if path == "/api/reminders":
            try:
                r_limit = min(max(int(one("limit", "500")), 1), 1000)
                r_offset = max(int(one("offset", "0")), 0)
            except ValueError:
                r_limit, r_offset = 500, 0
            return self._json(200, reminders_payload(
                one("status"), one("q"), one("kind"), one("delivery"),
                r_limit, r_offset))
        if path == "/api/thread":
            chat = one("chat_id")
            if not chat or not CHAT_ID_RE.match(chat):
                return self._json(400, {"error": "缺少或无效的 chat_id"})
            try:
                thread_limit = min(max(int(one("limit", "120")), 1), 200)
            except ValueError:
                thread_limit = 120
            payload = chat_thread_payload(chat, thread_limit)
            if payload.get("error"):
                return self._json(404, payload)
            return self._json(200, payload)
        if path == "/api/config":
            return self._json(200, config_payload())
        if path == "/api/logs":
            try:
                lines = min(max(int(one("lines", "60")), 1), 300)
            except ValueError:
                lines = 60
            return self._json(200, {"lines": log_tail(lines)})
        if path == "/api/social/state":
            return self._json(200, social_state_payload())
        if path == "/api/social/groups":
            return self._json(200, social_groups_payload())
        if path == "/api/social/group":
            room = one("room")
            if not room.endswith("@chatroom") or not CHAT_ID_RE.match(room):
                return self._json(400, {"error": "群 ID 无效"})
            return self._json(200, social_group_payload(room))
        if path == "/api/social/friends":
            try:
                friend_limit = min(max(int(one("limit", "1000")), 1), 2000)
            except ValueError:
                friend_limit = 1000
            return self._json(200, social_friends_payload(one("q"), friend_limit))
        if path == "/api/social/moments":
            return self._json(200, social_moments_payload())
        if path == "/api/social/announcements":
            return self._json(200, {"announcements": load_announcements(),
                                    "note": SOCIAL_CAPABILITIES["announcement"]})
        if path == "/api/social/notices":
            room = one("room")
            if not room.endswith("@chatroom") or not CHAT_ID_RE.match(room):
                return self._json(400, {"error": "群 ID 无效"})
            return self._json(200, {"notices": group_notices(room)})
        if path == "/api/social/status":
            return self._json(200, social_status())
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        if path == "/api/login":
            return self._handle_login()
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if path == "/api/logout":
            drop_session(self._bearer())
            return self._json(200, {"ok": True})
        if READONLY_MODE and path in ("/api/send", "/api/report/send",
                                      "/api/config", "/api/chat-meta",
                                      "/api/chat-meta/sync",
                                      "/api/reminders/action",
                                      "/api/social/sync", "/api/social/announcement"):
            return self._json(403, {"error": "管理台处于只读模式，写操作已禁用"})
        if path == "/api/chat-meta/sync":
            if not SYNC_SCRIPT.is_file():
                return self._json(500, {"error": "同步脚本不存在"})
            if _sync_lock.locked():
                return self._json(200, {"started": False,
                                        "hint": "已有同步任务在进行中，请稍后刷新查看结果"})
            threading.Thread(target=run_sync_once, daemon=True).start()
            return self._json(200, {"started": True,
                                    "hint": "已开始从微信读取群名与头像，约需数秒到一分钟"})
        payload = self._body()
        try:
            if path == "/api/reminders/action":
                action = str(payload.get("action") or "").strip()
                result = reminder_action(action, payload.get("id"))
                return self._json(200 if result.get("ok") else 400, result)
            if path == "/api/social/sync":
                if not SOCIAL_SCRIPT.is_file():
                    return self._json(500, {"error": "社交资料同步脚本不存在"})
                if _social_lock.locked():
                    return self._json(200, {"started": False,
                                            "hint": "已有同步任务在进行中，请稍后刷新查看结果"})
                only = str(payload.get("only") or "auto") if isinstance(payload, dict) else "auto"
                if only not in ("auto", "all", "groups", "friends", "moments"):
                    only = "auto"
                threading.Thread(target=run_social_once, args=(only,), daemon=True).start()
                hint = ("已开始只读读取朋友圈缓存，请稍后刷新" if only == "moments"
                        else "已开始从微信只读同步群成员与好友资料，约需数秒到一分钟")
                return self._json(200, {"started": True, "only": only, "hint": hint})
            if path == "/api/social/announcement":
                return self._json(200, save_announcement(payload))
            if path in ("/api/send", "/api/report/send"):
                raw_chat = str(payload.get("chat_id") or "").strip()
                chat_id, resolved = resolve_chat_id(raw_chat)
                if not chat_id:
                    return self._json(400, {"error": resolved})
                if path == "/api/send":
                    text = str(payload.get("text") or "").strip()
                    at_list = str(payload.get("at_list") or "").strip()
                    if not text:
                        return self._json(400, {"error": "正文不能为空"})
                    if "\0" in text or len(text) > 30000:
                        return self._json(400, {"error": "正文含非法字符或过长"})
                    rid = enqueue(chat_id, text, at_list)
                else:
                    day = str(payload.get("day") or date.today().isoformat())
                    stats = get_stats(day, None, 5)
                    rid = enqueue(chat_id, to_markdown(stats))
                meta = load_chat_meta()
                result = {"id": rid, "chat_id": chat_id,
                          "chat_name": chat_display(chat_id, meta),
                          "resolver": resolved}
                if not sender_ready():
                    result["warning"] = (
                        "消息已入队（#%d），但桥接尚未加载发送模块："
                        "请在 start.bat 黑窗口输入 reload report_bridge，"
                        "加载后会自动补发。" % rid)
                return self._json(200, result)
            if path == "/api/chat-meta":
                count = save_chat_names(payload.get("names") or {})
                return self._json(200, {"saved": count,
                                        "hint": "群名已保存，刷新页面即可看到"})
            if path == "/api/config":
                changed = save_config(payload)
                return self._json(200, {"changed": changed,
                                        "hint": "已保存；在桥接控制台输入 reload report_bridge 生效"})
            self._json(404, {"error": "not found"})
        except (ValueError, OSError, sqlite3.Error) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            self._json(500, {"error": "internal: %s" % error})

    def log_message(self, fmt, *args):
        return


def main():
    global PUBLIC_MODE
    parser = argparse.ArgumentParser(description="本地微信消息管理台")
    parser.add_argument("--host", default=os.environ.get("ADMIN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("ADMIN_PORT", "8088")))
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--set-login", nargs=2, metavar=("USER", "PASSWORD"),
                        help="生成/更新 Web 登录账号密码（只保存 PBKDF2 散列），然后退出")
    args = parser.parse_args()

    if args.set_login:
        username = args.set_login[0].strip()
        write_auth(username, args.set_login[1])
        print("已写入 %s" % AUTH_FILE)
        print("账号：%s ；密码以 PBKDF2-HMAC-SHA256 散列保存，明文不落盘。" % username)
        return 0

    if not token() or token() == "CHANGE_ME":
        print("config.local.json 缺少有效 token，拒绝启动。", file=sys.stderr)
        return 1
    # 配置了账号密码就统一进入登录模式，避免 admin.bat 用带令牌的网址绕过登录。
    if auth_record() is not None and not PUBLIC_MODE:
        PUBLIC_MODE = True
        print("检测到账号密码配置：已自动启用登录模式，网址不再携带令牌。")
    if PUBLIC_MODE:
        url = "http://%s:%d/" % (args.host, args.port)
        print("公网加固模式：已禁用 URL 传令牌。")
        print("ADMIN_TOKEN = %s" % token())
        if auth_record():
            print("账号密码登录已启用：页面输入账号密码即可，会话有效期 %d 小时。" %
                  (SESSION_TTL // 3600))
        else:
            print("警告：尚未配置账号密码，网页将无法登录。请运行 "
                  "python admin/app.py --set-login 账号 密码 后重启。")
        if READONLY_MODE:
            print("只读模式：发消息 / 发日报 / 改配置已禁用。")
    else:
        url = "http://%s:%d/?token=%s" % (args.host, args.port, quote(token()))
    print("管理台已启动：%s" % url)
    print("仅监听 %s；外网访问请走 frp 隧道 + TLS，不要改成 0.0.0.0。" % args.host)
    if args.open:
        webbrowser.open(url)
    if SYNC_SCRIPT.is_file():
        threading.Thread(target=auto_sync_loop, name="wechat-meta-sync",
                         daemon=True).start()
        print("真实群名/头像：每 %d 分钟自动从微信只读同步一次。" % (SYNC_INTERVAL // 60))
    if SOCIAL_SCRIPT.is_file():
        threading.Thread(target=social_loop, name="wechat-social-sync",
                         daemon=True).start()
        print("群成员/好友资料：每 %d 分钟自动从微信只读同步一次。" % (SOCIAL_INTERVAL // 60))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
