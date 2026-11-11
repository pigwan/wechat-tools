"""向微信主动发一条文本消息（通过 helper 插件的本地发送队列）。

群、好友、公众号、文件传输助手都能发。目标可以填真实会话 ID，也可以填
自定义微信号 / 唯一匹配的昵称，会自动解析成 admin/chats.auto.json 里的 ID。

用法：
    python send.py 58507398167@chatroom "今天的日报已生成"
    python send.py wxid_xxxxxxxx "请查收" --at wxid_a,wxid_b
    python send.py pigwan "发给我自己"
    python send.py 婷婷 "晚上好"
    python send.py 58507398167@chatroom --file reports/2026-11-06.md

原理：把消息写进 send_outbox.sqlite3，由 helper 内运行的 report_bridge
插件领取并调用 ctx.send_text 发送。不新开端口、不重启微信。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTBOX = ROOT.parent / "vendor" / "helper-main" / ".runtime" / "send_outbox.sqlite3"
OUTBOX = Path(os.environ.get("BRIDGE_OUTBOX", str(DEFAULT_OUTBOX)))
CHAT_AUTO = ROOT / "admin" / "chats.auto.json"


def resolve_target(raw):
    """把 ID / 微信号 / 昵称解析成真实会话 ID，返回 (ID, 显示名)。"""
    try:
        data = json.loads(CHAT_AUTO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return raw, ""
    if raw in data:
        row = data[raw] if isinstance(data[raw], dict) else {}
        return raw, str(row.get("name", "") or "")
    needle = raw.casefold()
    hits = [key for key, row in data.items()
            if isinstance(row, dict) and needle in (
                str(row.get("alias", "")).casefold(),
                str(row.get("name", "")).casefold(),
                str(row.get("wechat_name", "")).casefold())]
    if len(hits) == 1:
        return hits[0], str(data[hits[0]].get("name", "") or "")
    if len(hits) > 1:
        print("「%s」匹配到多个会话，请改用精确 ID：" % raw)
        for key in hits[:10]:
            print("  %s  %s" % (key, (data[key] or {}).get("name", "")))
        raise SystemExit(2)
    if any(ord(char) > 127 for char in raw):
        print("未在微信联系人缓存中找到「%s」，中文昵称/名字必须唯一匹配才能发送。" % raw)
        print("请在管理台「发消息」页搜索并复制真实会话 ID。")
        raise SystemExit(2)
    return raw, ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    at_list     TEXT NOT NULL DEFAULT '',
    status      INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
)
"""


def main():
    parser = argparse.ArgumentParser(
        description="向微信群 / 好友发送一条文本消息")
    parser.add_argument("chat_id", help="会话 ID（群 wxid@chatroom / 好友 wxid_xxx / "
                                       "自定义微信号如 pigwan / 唯一昵称 / filehelper）")
    parser.add_argument("text", nargs="?", default=None,
                        help="消息正文；含空格请用引号包住")
    parser.add_argument("--file", default=None,
                        help="从文件读取正文（例如刚生成的日报 Markdown）")
    parser.add_argument("--at", default="",
                        help="艾特成员的 wxid，逗号分隔")
    parser.add_argument("--wait", type=float, default=20.0,
                        help="等待发送结果的最长秒数，默认 20")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            print("找不到正文文件：%s" % path)
            return 1
        text = path.read_text(encoding="utf-8-sig")
    else:
        text = args.text or ""
    text = text.strip()
    if not text or "\0" in text:
        print("正文为空或含非法字符。")
        return 1
    if len(text) > 30000:
        print("正文过长（%d 字符），请拆成多条发送。" % len(text))
        return 1

    chat_id, display_name = resolve_target(args.chat_id.strip())
    print("目标：%s%s" % (chat_id, "（%s）" % display_name if display_name else ""))

    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OUTBOX), timeout=5)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
        created = datetime.now().astimezone().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO outbox (chat_id, text, at_list, status, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (chat_id, text, args.at, created))
        rid = cur.lastrowid
        conn.commit()
        print("已入队 #%d，等待桥接发送 …" % rid)

        deadline = time.time() + max(1.0, args.wait)
        while time.time() < deadline:
            row = conn.execute(
                "SELECT status, error FROM outbox WHERE id = ?", (rid,)).fetchone()
            if row and row[0] == 2:
                print("发送成功。")
                return 0
            if row and row[0] == 3:
                print("发送失败：%s" % (row[1] or "未知错误"))
                return 1
            time.sleep(0.4)
    finally:
        conn.close()

    print("超时未发送；消息已留在队列中，桥接加载后会自动补发，请勿重复提交。")
    print("首次使用请在 start.bat 的黑窗口输入：reload report_bridge")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
