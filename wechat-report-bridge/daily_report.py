"""一键生成指定日期的群消息日报，输出为 Markdown。

用法：
    python daily_report.py                 # 今天
    python daily_report.py 2026-11-05     # 指定日期
    python daily_report.py 2026-11-05 --chat-id 58507398167@chatroom
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
from report import build_stats, to_markdown  # noqa: E402


def main():
    args = sys.argv[1:]
    day = date.today().isoformat()
    chat_id = None
    if args and not args[0].startswith("--"):
        day = args.pop(0)
    if "--chat-id" in args:
        idx = args.index("--chat-id")
        if idx + 1 < len(args):
            chat_id = args[idx + 1]

    db_path = Path(os.environ.get("BRIDGE_DB", ROOT / "server" / "messages.sqlite3"))
    if not db_path.is_file():
        print("还没有消息数据库，请先双击 start.bat 启动并收发几条群消息。")
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        stats = build_stats(conn, day, chat_id)
    except sqlite3.OperationalError:
        print("数据库里还没有消息表，请先启动接收端。")
        return 1
    finally:
        conn.close()

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    suffix = ("-" + chat_id.split("@")[0]) if chat_id else ""
    out = out_dir / ("%s%s.md" % (day, suffix))
    out.write_text(to_markdown(stats), encoding="utf-8")

    print("日报已生成：%s" % out)
    print("共 %d 条消息，%d 人发言，涉及 %d 个群。"
          % (stats["total"], stats["participants"], stats["groups"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
