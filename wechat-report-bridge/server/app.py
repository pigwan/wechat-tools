"""群消息接收端：Bearer 鉴权 + 幂等落库 + 日报查询。

零第三方依赖，只用 Python 标准库，Python 3.8+ 可直接运行：

    BRIDGE_TOKEN=换成随机长串 python app.py

默认只监听 127.0.0.1:8080。若要放到另一台服务器对外提供服务，
请设置 BRIDGE_HOST=0.0.0.0 并在前面用 Nginx/Caddy 终止 HTTPS，
不要把本服务直接暴露到公网。

接口：
    POST /api/messages   {"messages": [...], "links": [...], "official": [...]}  批量写入，按 key 幂等
    GET  /api/messages?day=YYYY-MM-DD[&chat_id=...]  查询原始消息
    GET  /api/links?day=...[&chat_id=...][&q=...][&limit=...]  查询采集到的链接
    GET  /api/official?day=...[&chat_id=...][&q=...][&scene=...]  查询公众号消息
    GET  /api/report?day=...[&chat_id=...][&format=md]  结构化日报 / Markdown
    GET  /health
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from report import build_stats, to_markdown

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BRIDGE_DB", os.path.join(BASE_DIR, "messages.sqlite3"))
TOKEN = os.environ.get("BRIDGE_TOKEN", "CHANGE_ME")
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 2 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_key     TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    is_group    INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    link_key    TEXT PRIMARY KEY,
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
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


_OFFICIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS official (
    official_key   TEXT PRIMARY KEY,
    chat_id        TEXT NOT NULL,
    publisher_id   TEXT NOT NULL DEFAULT '',
    publisher_name TEXT NOT NULL DEFAULT '',
    sender_id      TEXT NOT NULL DEFAULT '',
    sender_name    TEXT NOT NULL DEFAULT '',
    is_group       INTEGER NOT NULL DEFAULT 0,
    scene          TEXT NOT NULL DEFAULT 'push',
    title          TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    digest         TEXT NOT NULL DEFAULT '',
    cover_url      TEXT NOT NULL DEFAULT '',
    published_at   TEXT NOT NULL DEFAULT '',
    received_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.execute(_LINKS_SCHEMA)
    conn.execute(_OFFICIAL_SCHEMA)
    return conn


def init_db():
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


def ingest(items, links=None, official=None):
    """写入一批消息/链接/公众号消息，返回各类新增与重复数。

    主键保证重复投递被忽略；公众号消息按 official_key 幂等。
    """
    accepted = duplicates = links_accepted = links_duplicates = 0
    official_accepted = official_duplicates = 0
    links = links or []
    official = official or []
    conn = connect()
    try:
        with conn:
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("msg_key") or "")
                if not key:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO messages "
                        "(msg_key, chat_id, sender_id, sender_name, is_group, text, received_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            key,
                            str(item.get("chat_id") or ""),
                            str(item.get("sender_id") or ""),
                            str(item.get("sender_name") or ""),
                            1 if item.get("is_group") else 0,
                            str(item.get("text") or ""),
                            str(item.get("received_at") or ""),
                        ),
                    )
                    accepted += 1
                except sqlite3.IntegrityError:
                    duplicates += 1
            for link in links:
                if not isinstance(link, dict):
                    continue
                key = str(link.get("link_key") or "")
                url = str(link.get("url") or "")
                if not key or not url or len(url) > 2048:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO links "
                        "(link_key, chat_id, sender_id, sender_name, is_group, kind, "
                        "app_type, title, url, description, source, received_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            key,
                            str(link.get("chat_id") or ""),
                            str(link.get("sender_id") or ""),
                            str(link.get("sender_name") or ""),
                            1 if link.get("is_group") else 0,
                            str(link.get("kind") or "card")[:16],
                            str(link.get("app_type") or "")[:16],
                            str(link.get("title") or "")[:512],
                            url,
                            str(link.get("description") or "")[:2048],
                            str(link.get("source") or "")[:256],
                            str(link.get("received_at") or ""),
                        ),
                    )
                    links_accepted += 1
                except sqlite3.IntegrityError:
                    links_duplicates += 1
            for entry in official:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("official_key") or "")
                url = str(entry.get("url") or "")
                if not key or len(url) > 2048 or not str(entry.get("chat_id") or ""):
                    continue
                try:
                    conn.execute(
                        "INSERT INTO official "
                        "(official_key, chat_id, publisher_id, publisher_name, sender_id, "
                        "sender_name, is_group, scene, title, url, digest, cover_url, "
                        "published_at, received_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            key,
                            str(entry.get("chat_id") or ""),
                            str(entry.get("publisher_id") or "")[:128],
                            str(entry.get("publisher_name") or "")[:128],
                            str(entry.get("sender_id") or "")[:128],
                            str(entry.get("sender_name") or "")[:128],
                            1 if entry.get("is_group") else 0,
                            str(entry.get("scene") or "push")[:16],
                            str(entry.get("title") or "")[:512],
                            url,
                            str(entry.get("digest") or "")[:2048],
                            str(entry.get("cover_url") or "")[:2048],
                            str(entry.get("published_at") or "")[:32],
                            str(entry.get("received_at") or ""),
                        ),
                    )
                    official_accepted += 1
                except sqlite3.IntegrityError:
                    official_duplicates += 1
    finally:
        conn.close()
    return {"accepted": accepted, "duplicates": duplicates,
            "links_accepted": links_accepted, "links_duplicates": links_duplicates,
            "official_accepted": official_accepted,
            "official_duplicates": official_duplicates}


def query_messages(day, chat_id=None, limit=500):
    sql = "SELECT * FROM messages WHERE substr(received_at, 1, 10) = ?"
    args = [day]
    if chat_id:
        sql += " AND chat_id = ?"
        args.append(chat_id)
    sql += " ORDER BY received_at, msg_key LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


def query_links(day, chat_id=None, keyword="", limit=200):
    sql = "SELECT * FROM links WHERE 1 = 1"
    args = []
    if day and day != "all":
        sql += " AND substr(received_at, 1, 10) = ?"
        args.append(day)
    if chat_id:
        sql += " AND chat_id = ?"
        args.append(chat_id)
    if keyword:
        sql += (" AND (title LIKE ? OR url LIKE ? OR description LIKE ? "
                "OR source LIKE ? OR chat_id LIKE ?)")
        args.extend(["%" + keyword + "%"] * 5)
    sql += " ORDER BY received_at DESC, link_key DESC LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


def query_official(day, chat_id=None, keyword="", scene="", limit=200):
    sql = "SELECT * FROM official WHERE 1 = 1"
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
        sql += (" AND (title LIKE ? OR url LIKE ? OR digest LIKE ? "
                "OR publisher_name LIKE ? OR chat_id LIKE ?)")
        args.extend(["%" + keyword + "%"] * 5)
    sql += " ORDER BY received_at DESC, official_key DESC LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "ReportBridge/1.0"

    def _send(self, status, payload, markdown=False):
        if markdown:
            body = payload.encode("utf-8")
            content_type = "text/markdown; charset=utf-8"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if TOKEN == "CHANGE_ME":
            return False
        header = self.headers.get("Authorization", "")
        parts = header.split(" ", 1)
        return (len(parts) == 2 and parts[0].lower() == "bearer"
                and hmac.compare_digest(parts[1], TOKEN))

    def _reject(self):
        self._send(401, {"error": "unauthorized"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/messages":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._reject()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"error": "invalid body length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return
        items = payload.get("messages") if isinstance(payload, dict) else None
        links = payload.get("links") if isinstance(payload, dict) else None
        official = payload.get("official") if isinstance(payload, dict) else None
        links = links if links is not None else []
        official = official if official is not None else []
        if (not isinstance(items, list) or len(items) > 500
                or not isinstance(links, list) or len(links) > 500
                or not isinstance(official, list) or len(official) > 500):
            self._send(400, {"error": "invalid payload"})
            return
        self._send(200, ingest(items, links, official))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query, keep_blank_values=True)
        if path == "/health":
            self._send(200, {"status": "ok"})
            return
        if not self._authorized():
            self._reject()
            return
        day = (params.get("day") or [date.today().isoformat()])[0]
        chat_id = (params.get("chat_id") or [None])[0] or None
        if path == "/api/messages":
            try:
                limit = min(max(int((params.get("limit") or ["500"])[0]), 1), 5000)
            except ValueError:
                limit = 500
            rows = query_messages(day, chat_id, limit)
            self._send(200, {"day": day, "count": len(rows), "messages": rows})
            return
        if path == "/api/links":
            try:
                limit = min(max(int((params.get("limit") or ["200"])[0]), 1), 1000)
            except ValueError:
                limit = 200
            keyword = (params.get("q") or [""])[0]
            rows = query_links(day if (params.get("day") or [""])[0] != "all" else "all",
                               chat_id, keyword, limit)
            self._send(200, {"day": day, "count": len(rows), "links": rows})
            return
        if path == "/api/official":
            try:
                limit = min(max(int((params.get("limit") or ["200"])[0]), 1), 1000)
            except ValueError:
                limit = 200
            keyword = (params.get("q") or [""])[0]
            scene = (params.get("scene") or [""])[0]
            rows = query_official(day if (params.get("day") or [""])[0] != "all" else "all",
                                  chat_id, keyword, scene, limit)
            self._send(200, {"day": day, "count": len(rows), "official": rows})
            return
        if path == "/api/report":
            try:
                top_n = min(max(int((params.get("top") or ["5"])[0]), 1), 50)
            except ValueError:
                top_n = 5
            conn = connect()
            try:
                stats = build_stats(conn, day, chat_id, top_n)
            finally:
                conn.close()
            if (params.get("format") or [""])[0].lower() == "md":
                self._send(200, to_markdown(stats), markdown=True)
            else:
                self._send(200, stats)
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        # 安静运行，避免在高消息量下把 4 GB 机器拖进日志 I/O
        return


def main():
    if TOKEN == "CHANGE_ME":
        print("请先设置 BRIDGE_TOKEN 环境变量（随机长串），拒绝以默认口令启动。", file=sys.stderr)
        return 1
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print("ReportBridge 接收端已启动：http://%s:%d  数据库：%s" % (HOST, PORT, DB_PATH))
    if HOST == "127.0.0.1":
        print("仅监听本机。对远端提供服务请配置 BRIDGE_HOST 并用反向代理启用 HTTPS。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
