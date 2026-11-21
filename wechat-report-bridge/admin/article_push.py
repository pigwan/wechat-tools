"""公众号文章链接推送（Webhook）—— 管理台的一个独立应用。

数据来源：只读 server/messages.sqlite3 的 official 表（scene=push 为公众号推送，
scene=shared 为群内分享的公众号文章）。采集端把一篇图文落一行，同一条推送的
多篇文章 received_at 相同，按 (chat_id, scene, 秒级时间) 合并成一次 POST。

对外只发文章链接，请求体固定为一个 JSON 对象、一个数组字段：

    POST <配置的 URL>
    Content-Type: application/json; charset=utf-8
    Authorization: Bearer <可选 token>

    {"urls": ["https://mp.weixin.qq.com/s/xxxx", ...]}

无论一条推送里有几篇文章，字段值永远是数组；只有一篇时也是长度为 1 的数组。
字段名、URL、Token、合并/逐篇模式等都可在管理台网页上配置。

状态（基线、待重试批次、日志）放在 admin/article_push.sqlite3，配置放在
admin/article_push.local.json。纯 Python 标准库，不改采集端与接收端。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "article_push.local.json"
STATE_DB = ROOT / "article_push.sqlite3"

DEFAULT_CONFIG = {
    "enabled": False,
    "url": "",
    "param_name": "urls",
    "token": "",
    "mode": "message",          # message=一条推送一次 POST；article=每篇一次 POST
    "include_shared": False,    # 是否连群里分享的公众号文章一起推
    "timeout_seconds": 10,
}

PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")
POLL_SECONDS = 1.5
SCAN_LIMIT = 200
FLUSH_LIMIT = 10
MAX_RETRIES = 6
RETRY_DELAYS = (5, 15, 60, 300, 900, 3600)
MAX_LOGS = 400
MAX_SEEN = 20000

_config_lock = threading.Lock()
_engine = {"started": 0.0, "last_tick": 0.0, "last_error": ""}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key    TEXT NOT NULL UNIQUE,
    urls_json    TEXT NOT NULL,
    max_rowid    INTEGER NOT NULL,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    next_retry   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    last_error   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    http_status INTEGER NOT NULL DEFAULT 0,
    url_count   INTEGER NOT NULL DEFAULT 0,
    urls_json   TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    ms          INTEGER NOT NULL DEFAULT 0,
    is_test     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (
    url TEXT PRIMARY KEY,
    at  TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _http_url(value):
    """校验必须是 http/https 且有主机名，返回去空格后的 URL 或空串。"""
    text = str(value or "").strip()
    if not text or len(text) > 2000:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


def _json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}


def state_conn():
    conn = sqlite3.connect(str(STATE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # 后台线程与网页请求会同时读写状态库，WAL 避免偶发 database is locked
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _meta(conn, key, default=""):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                 "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                 (key, str(value)))


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def read_config():
    """每次读盘，网页保存后无需重启即可生效。"""
    with _config_lock:
        raw = _json_file(CONFIG_FILE)
    cfg = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in raw:
            cfg[key] = raw[key]
    cfg["url"] = _http_url(cfg["url"])
    cfg["param_name"] = str(cfg["param_name"] or "").strip()
    if not PARAM_RE.match(cfg["param_name"]):
        cfg["param_name"] = DEFAULT_CONFIG["param_name"]
    cfg["token"] = str(cfg["token"] or "").strip()
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["include_shared"] = bool(cfg["include_shared"])
    cfg["mode"] = cfg["mode"] if cfg["mode"] in ("message", "article") else "message"
    try:
        cfg["timeout_seconds"] = min(60, max(1, int(cfg["timeout_seconds"])))
    except (TypeError, ValueError):
        cfg["timeout_seconds"] = DEFAULT_CONFIG["timeout_seconds"]
    return cfg


def save_config(payload):
    """接受网页表单，校验后原子写盘。返回 (配置, 变更字段列表)。"""
    if not isinstance(payload, dict):
        raise ValueError("配置格式无效")
    old = read_config()
    cfg = dict(old)
    changed = []

    if "url" in payload:
        url = _http_url(payload.get("url"))
        if not url:
            raise ValueError("推送 URL 必须是完整的 http:// 或 https:// 地址")
        cfg["url"] = url
        changed.append("url")
    if "param_name" in payload:
        name = str(payload.get("param_name") or "").strip()
        if not PARAM_RE.match(name):
            raise ValueError("参数名只能含字母、数字、下划线，且以字母或下划线开头")
        cfg["param_name"] = name
        changed.append("param_name")
    if "mode" in payload:
        mode = str(payload.get("mode") or "").strip()
        if mode not in ("message", "article"):
            raise ValueError("推送方式无效")
        cfg["mode"] = mode
        changed.append("mode")
    if "timeout_seconds" in payload:
        try:
            value = int(payload.get("timeout_seconds"))
        except (TypeError, ValueError):
            raise ValueError("超时必须是整数")
        if not 1 <= value <= 60:
            raise ValueError("超时范围 1-60 秒")
        cfg["timeout_seconds"] = value
        changed.append("timeout_seconds")
    for key in ("enabled", "include_shared"):
        if key in payload:
            if type(payload[key]) is not bool:
                raise ValueError("%s 必须是布尔值" % key)
            cfg[key] = payload[key]
            changed.append(key)
    if payload.get("clear_token"):
        cfg["token"] = ""
        changed.append("token")
    elif str(payload.get("token") or "").strip():
        cfg["token"] = str(payload["token"]).strip()
        changed.append("token")

    with _config_lock:
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    return cfg, changed


# --------------------------------------------------------------------------
# 状态库
# --------------------------------------------------------------------------

def _log(conn, *, ok, status=0, urls=(), target="", error="", ms=0, is_test=False):
    conn.execute(
        "INSERT INTO logs (at, ok, http_status, url_count, urls_json, target, "
        "error, ms, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_now(), 1 if ok else 0, int(status or 0), len(urls),
         json.dumps(list(urls), ensure_ascii=False), str(target or ""),
         str(error or "")[:500], int(ms), 1 if is_test else 0))
    if ok:
        _set_meta(conn, "stat:success", int(_meta(conn, "stat:success", "0")) + 1)
        _set_meta(conn, "stat:last_push_at", _now())
        _set_meta(conn, "stat:last_error", "")
    else:
        _set_meta(conn, "stat:failed", int(_meta(conn, "stat:failed", "0")) + 1)
        _set_meta(conn, "stat:last_error", str(error or "")[:500])


def _max_source_rowid(db_path):
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            return int(src.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM official").fetchone()[0])
        finally:
            src.close()
    except (OSError, ValueError, sqlite3.Error):
        return 0


def _ensure_baseline(conn, db_path):
    """首次运行以「当前已收到的最新一条」为基线，绝不把历史文章补推一遍。"""
    if _meta(conn, "baseline_set"):
        return
    baseline = _max_source_rowid(db_path)
    with conn:
        _set_meta(conn, "baseline", baseline)
        _set_meta(conn, "baseline_set", "1")
        _set_meta(conn, "baseline_at", _now())


def _source_rows(db_path, baseline, limit=SCAN_LIMIT):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return src.execute(
            "SELECT rowid, chat_id, scene, url, received_at FROM official "
            "WHERE rowid > ? AND url <> '' ORDER BY rowid LIMIT ?",
            (baseline, limit)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        src.close()


def _seen(conn, url):
    return conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,)).fetchone() is not None


def scan_new(conn, db_path, cfg):
    """扫描新增文章并入队；无论开关如何都推进游标（停用期间不补推）。"""
    baseline = int(_meta(conn, "baseline", "0") or 0)
    rows = _source_rows(db_path, baseline)
    if not rows:
        return 0
    allowed = {"push"}
    if cfg["include_shared"]:
        allowed.add("shared")
    active = bool(cfg["enabled"] and cfg["url"])

    groups = OrderedDict()
    max_rowid = baseline
    for row in rows:
        rid = int(row[0])
        max_rowid = max(max_rowid, rid)
        chat_id, scene, url = str(row[1] or ""), str(row[2] or ""), str(row[3] or "")
        received = str(row[4] or "")[:19]
        if scene not in allowed or not _http_url(url):
            continue
        if not active or _seen(conn, url):
            continue
        key = (chat_id, scene, received, rid if cfg["mode"] == "article" else "")
        groups.setdefault(key, []).append((rid, url))

    batches = 0
    now = time.time()
    with conn:
        for key, items in groups.items():
            unique = list(OrderedDict.fromkeys(url for _, url in items))
            max_rowid = max(rid for rid, _ in items)
            if not unique:
                continue
            digest = ("|".join(str(x) for x in key)).encode("utf-8", "replace")
            # 用内容哈希做幂等键，重复扫描不会产生两个相同批次
            batch_key = hashlib.sha256(
                digest + b"|" + json.dumps(unique, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO pending "
                "(batch_key, urls_json, max_rowid, retry_count, next_retry, status, "
                "created_at) VALUES (?, ?, ?, 0, ?, 'pending', ?)",
                (batch_key, json.dumps(unique, ensure_ascii=False), max_rowid,
                 now, _now()))
            batches += 1
        _set_meta(conn, "baseline", max_rowid)
    return batches


# --------------------------------------------------------------------------
# 真正发 POST
# --------------------------------------------------------------------------

def post_links(cfg, urls):
    """向目标 URL 发一次 POST。返回 (ok, http_status, error, ms)。"""
    urls = [str(u) for u in urls]
    body = json.dumps({cfg["param_name"]: urls}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"], data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    if cfg.get("token"):
        req.add_header("Authorization", "Bearer " + cfg["token"])
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as resp:
            status = resp.status
            resp.read(2048)
        ok = 200 <= status < 300
        return ok, status, "" if ok else "HTTP %d" % status, int((time.time() - started) * 1000)
    except urllib.error.HTTPError as error:
        return False, error.code, "HTTP %d %s" % (error.code, error.reason), \
            int((time.time() - started) * 1000)
    except Exception as error:
        return False, 0, "%s: %s" % (type(error).__name__, error), \
            int((time.time() - started) * 1000)


def flush_pending(conn, cfg):
    now = time.time()
    rows = conn.execute(
        "SELECT * FROM pending WHERE status = 'pending' AND next_retry <= ? "
        "ORDER BY id LIMIT ?", (now, FLUSH_LIMIT)).fetchall()
    for row in rows:
        try:
            urls = json.loads(row["urls_json"])
        except (ValueError, TypeError):
            urls = []
        if not urls:
            conn.execute("DELETE FROM pending WHERE id = ?", (row["id"],))
            continue
        ok, status, error, ms = post_links(cfg, urls)
        with conn:
            _log(conn, ok=ok, status=status, urls=urls, target=cfg["url"],
                 error=error, ms=ms)
            if ok:
                conn.execute("DELETE FROM pending WHERE id = ?", (row["id"],))
                for url in urls:
                    conn.execute("INSERT OR IGNORE INTO seen (url, at) VALUES (?, ?)",
                                 (url, _now()))
            else:
                retry = int(row["retry_count"]) + 1
                if retry >= MAX_RETRIES:
                    conn.execute("UPDATE pending SET status = 'failed', retry_count = ?, "
                                 "last_error = ? WHERE id = ?", (retry, error, row["id"]))
                else:
                    conn.execute("UPDATE pending SET retry_count = ?, next_retry = ?, "
                                 "last_error = ? WHERE id = ?",
                                 (retry, time.time() + RETRY_DELAYS[min(retry - 1, len(RETRY_DELAYS) - 1)],
                                  error, row["id"]))
    return len(rows)


def _prune_logs(conn):
    conn.execute("DELETE FROM logs WHERE id NOT IN "
                 "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (MAX_LOGS,))


def worker_loop(db_path):
    """管理台后台线程：扫描新文章 + 发送待推批次。"""
    _engine["started"] = time.time()
    while True:
        try:
            cfg = read_config()
            conn = state_conn()
            try:
                _ensure_baseline(conn, db_path)
                scan_new(conn, db_path, cfg)
                if cfg["enabled"] and cfg["url"]:
                    flush_pending(conn, cfg)
                _prune_logs(conn)
                conn.commit()
            finally:
                conn.close()
            _engine["last_tick"] = time.time()
            _engine["last_error"] = ""
        except Exception as error:
            _engine["last_error"] = "%s: %s" % (type(error).__name__, error)
            _engine["last_tick"] = time.time()
        time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------
# 对外：状态 / 测试 / 运维动作
# --------------------------------------------------------------------------

def status(db_path):
    cfg = read_config()
    conn = state_conn()
    try:
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT 20").fetchall()]
        pending = int(conn.execute(
            "SELECT COUNT(*) FROM pending WHERE status = 'pending'").fetchone()[0])
        failed = int(conn.execute(
            "SELECT COUNT(*) FROM pending WHERE status = 'failed'").fetchone()[0])
        stats = {
            "success": int(_meta(conn, "stat:success", "0")),
            "failed": int(_meta(conn, "stat:failed", "0")),
            "last_push_at": _meta(conn, "stat:last_push_at"),
            "last_error": _meta(conn, "stat:last_error"),
            "pending": pending,
            "failed_batches": failed,
            "baseline": int(_meta(conn, "baseline", "0") or 0),
            "baseline_at": _meta(conn, "baseline_at"),
            "source_max_rowid": _max_source_rowid(db_path),
        }
    finally:
        conn.close()
    public_cfg = {k: cfg[k] for k in DEFAULT_CONFIG if k != "token"}
    public_cfg["token_set"] = bool(cfg["token"])
    public_cfg["token_hint"] = (cfg["token"][:4] + "…") if cfg["token"] else ""
    return {
        "config": public_cfg,
        "stats": stats,
        "logs": logs,
        "engine": {
            "running": bool(_engine["last_tick"] and
                            time.time() - _engine["last_tick"] < 15),
            "started": _engine["started"],
            "last_tick": _engine["last_tick"],
            "last_error": _engine["last_error"],
            "poll_seconds": POLL_SECONDS,
        },
        "post": {
            "method": "POST",
            "content_type": "application/json; charset=utf-8",
            "body": {cfg["param_name"]: ["<文章链接>", "..."]},
            "auth_header": "Authorization: Bearer <token>" if cfg["token"] else "",
            "note": "只推文章链接；数组恒定存在，一篇也是长度 1 的数组",
        },
        "state_db": str(STATE_DB),
        "config_file": str(CONFIG_FILE),
        "source_db": str(db_path),
    }


def test_push(payload):
    """用表单里的临时配置发一条示例（不落盘、不改动正式队列）。"""
    cfg = read_config()
    for key in ("url", "param_name", "mode", "timeout_seconds"):
        if key in payload:
            cfg[key] = payload[key]
    if payload.get("clear_token"):
        cfg["token"] = ""
    elif str(payload.get("token") or "").strip():
        cfg["token"] = str(payload["token"]).strip()
    cfg["url"] = _http_url(cfg["url"])
    if not cfg["url"]:
        raise ValueError("请先填写有效的推送 URL")
    if not PARAM_RE.match(cfg["param_name"]):
        raise ValueError("参数名不合法")
    try:
        cfg["timeout_seconds"] = min(60, max(1, int(cfg["timeout_seconds"])))
    except (TypeError, ValueError):
        cfg["timeout_seconds"] = DEFAULT_CONFIG["timeout_seconds"]
    urls = ["https://mp.weixin.qq.com/s/TEST-EXAMPLE-ONLY"]
    ok, status, error, ms = post_links(cfg, urls)
    conn = state_conn()
    try:
        with conn:
            _log(conn, ok=ok, status=status, urls=urls, target=cfg["url"],
                 error=error, ms=ms, is_test=True)
    finally:
        conn.close()
    return {"ok": ok, "http_status": status, "error": error, "ms": ms,
            "url": cfg["url"], "body": {cfg["param_name"]: urls}}


def action(name, db_path):
    if name == "reset_baseline":
        conn = state_conn()
        try:
            with conn:
                _set_meta(conn, "baseline", _max_source_rowid(db_path))
                _set_meta(conn, "baseline_set", "1")
                _set_meta(conn, "baseline_at", _now())
            return {"ok": True, "message": "已把基线移到最新，积压文章不再补推"}
        finally:
            conn.close()
    if name == "retry_failed":
        conn = state_conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE pending SET status = 'pending', retry_count = 0, "
                    "next_retry = ?, last_error = '' WHERE status = 'failed'",
                    (time.time(),))
            return {"ok": True, "message": "已重新排队 %d 个失败批次" % cur.rowcount}
        finally:
            conn.close()
    if name == "clear_failed":
        conn = state_conn()
        try:
            with conn:
                cur = conn.execute("DELETE FROM pending WHERE status = 'failed'")
            return {"ok": True, "message": "已清除 %d 个失败批次" % cur.rowcount}
        finally:
            conn.close()
    if name == "clear_logs":
        conn = state_conn()
        try:
            with conn:
                conn.execute("DELETE FROM logs")
            return {"ok": True, "message": "推送日志已清空"}
        finally:
            conn.close()
    raise ValueError("不支持的操作：%s" % name)
