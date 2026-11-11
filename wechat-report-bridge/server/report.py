"""群消息日报统计与 Markdown 导出。

命令行：
  python report.py --day 2026-11-06 --out report.md
  python report.py --day 2026-11-06 --chat-id xxx@chatroom --top 10
"""

import argparse
import os
import re
import sqlite3
from collections import Counter
from datetime import date
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BRIDGE_DB", os.path.join(BASE_DIR, "messages.sqlite3"))


def build_stats(conn, day, chat_id=None, top_n=5):
    sql = ("SELECT sender_name, chat_id, text, received_at FROM messages "
           "WHERE substr(received_at, 1, 10) = ?")
    args = [day]
    if chat_id:
        sql += " AND chat_id = ?"
        args.append(chat_id)
    rows = conn.execute(sql, tuple(args)).fetchall()

    senders = Counter((row[0] or "未知") for row in rows)
    groups = Counter((row[1] or "未知群") for row in rows)
    hours = Counter(row[3][11:13] for row in rows if len(row[3]) >= 13)
    keywords = ("报告", "问题", "结论", "待办", "bug", "上线")

    # 公众号文章在 official 表里有专门段落，这里不重复列
    link_sql = ("SELECT title, url, description, source, kind, chat_id, "
                "sender_name, received_at FROM links "
                "WHERE substr(received_at, 1, 10) = ? AND kind <> 'official'")
    link_args = [day]
    if chat_id:
        link_sql += " AND chat_id = ?"
        link_args.append(chat_id)
    link_sql += " ORDER BY received_at DESC LIMIT 200"
    try:
        link_rows = conn.execute(link_sql, tuple(link_args)).fetchall()
    except sqlite3.Error:
        link_rows = []

    official_sql = ("SELECT title, url, digest, publisher_id, publisher_name, chat_id, "
                    "scene, published_at, received_at FROM official "
                    "WHERE substr(received_at, 1, 10) = ? AND scene <> 'text' "
                    "AND url <> ''")
    official_args = [day]
    if chat_id:
        official_sql += " AND chat_id = ?"
        official_args.append(chat_id)
    official_sql += " ORDER BY received_at DESC LIMIT 200"
    try:
        official_rows = conn.execute(official_sql, tuple(official_args)).fetchall()
    except sqlite3.Error:
        official_rows = []

    seen_urls = set()
    links = []
    domains = Counter()
    for row in link_rows:
        url = (row[1] or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        links.append({"title": row[0] or "", "url": url,
                      "source": row[3] or "", "kind": row[4] or "card",
                      "chat_id": row[5] or "", "sender_name": row[6] or "",
                      "received_at": row[7] or ""})
        try:
            host = (urlsplit(url).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                domains[host] += 1
        except ValueError:
            pass

    seen_official = set()
    official = []
    publishers = Counter()
    for row in official_rows:
        url = (row[1] or "").strip()
        if not url or url in seen_official:
            continue
        seen_official.add(url)
        name = (row[4] or row[3] or "").strip()
        official.append({"title": row[0] or "", "url": url,
                         "digest": row[2] or "", "publisher_id": row[3] or "",
                         "publisher_name": name, "chat_id": row[5] or "",
                         "scene": row[6] or "push",
                         "published_at": row[7] or "", "received_at": row[8] or ""})
        if name:
            publishers[name] += 1
    try:
        official_text_count = conn.execute(
            "SELECT COUNT(*) FROM official WHERE substr(received_at, 1, 10) = ? "
            "AND scene = 'text'", (day,)).fetchone()[0]
    except sqlite3.Error:
        official_text_count = 0

    return {
        "day": day,
        "chat_id": chat_id,
        "total": len(rows),
        "participants": len(senders),
        "groups": len(groups),
        "top_senders": [
            {"name": name, "count": count}
            for name, count in senders.most_common(top_n)
        ],
        "top_groups": [
            {"chat_id": gid, "count": count}
            for gid, count in groups.most_common(top_n)
        ],
        "hourly": [
            {"hour": hour, "count": hours.get("%02d" % hour, 0)}
            for hour in range(24)
        ],
        "keyword_hits": {
            word: sum(1 for row in rows if word.lower() in (row[2] or "").lower())
            for word in keywords
        },
        "samples": [row[2] for row in rows[:5]],
        "link_count": len(links),
        "links": links[:20],
        "link_domains": [{"domain": name, "count": count}
                         for name, count in domains.most_common(5)],
        "official_count": len(official),
        "official_text_count": official_text_count,
        "official": official[:20],
        "official_publishers": [{"name": name, "count": count}
                                for name, count in publishers.most_common(5)],
    }


def to_markdown(stats):
    lines = [
        "# 群消息日报 %s" % stats["day"],
        "",
        "共 **%d** 条消息，%d 人发言，涉及 %d 个群。" % (
            stats["total"], stats["participants"], stats["groups"]),
        "",
        "## 活跃成员",
        "",
    ]
    if stats["top_senders"]:
        for item in stats["top_senders"]:
            lines.append("- %s：%d 条" % (item["name"], item["count"]))
    else:
        lines.append("- 无记录")

    lines += ["", "## 群分布", ""]
    for item in stats["top_groups"]:
        lines.append("- `%s`：%d 条" % (item["chat_id"], item["count"]))

    peak = max(stats["hourly"], key=lambda x: x["count"])
    lines += [
        "",
        "## 时段",
        "",
        "高峰时段 %s 点（%d 条）。" % (peak["hour"], peak["count"]),
        "",
        "## 关键词命中",
        "",
    ]
    for word, count in stats["keyword_hits"].items():
        if count:
            lines.append("- %s：%d 次" % (word, count))

    links = stats.get("links") or []
    if links:
        lines += ["", "## 今日分享链接（%d 条）" % stats.get("link_count", len(links)), ""]
        for item in links:
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            title = title.replace("[", "【").replace("]", "】") or item.get("url", "")
            url = str(item.get("url") or "").replace(" ", "%20")
            meta = " · ".join(str(x) for x in (
                item.get("source", ""), item.get("sender_name", ""),
                item.get("chat_name", "")) if x)
            lines.append("- [%s](<%s>)%s" % (
                title[:120], url, (" — " + meta[:120]) if meta else ""))
        if stats.get("link_domains"):
            lines += ["", "链接来源：" + "、".join(
                "%s (%d)" % (d["domain"], d["count"])
                for d in stats["link_domains"])]

    articles = stats.get("official") or []
    if articles:
        lines += ["", "## 今日公众号文章（%d 篇）" % stats.get("official_count", len(articles)), ""]
        for item in articles:
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            title = title.replace("[", "【").replace("]", "】") or item.get("url", "")
            url = str(item.get("url") or "").replace(" ", "%20")
            meta = " · ".join(str(x) for x in (
                item.get("publisher_name", ""), item.get("chat_name", ""),
                item.get("published_at", "")) if x)
            lines.append("- [%s](<%s>)%s" % (
                title[:120], url, (" — " + meta[:120]) if meta else ""))
            digest = re.sub(r"\s+", " ", str(item.get("digest") or "")).strip()
            if digest:
                lines.append("  > %s" % digest[:160])
        if stats.get("official_publishers"):
            lines += ["", "公众号分布：" + "、".join(
                "%s (%d)" % (d["name"], d["count"])
                for d in stats["official_publishers"])]
        if stats.get("official_text_count"):
            lines += ["", "另有 %d 条公众号文字消息（管理台「公众号」页可查看）。"
                      % stats["official_text_count"]]

    lines += ["", "## 消息样本", ""]
    for text in stats["samples"]:
        lines.append("> %s" % text.replace("\n", " ")[:120])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="生成群消息日报")
    parser.add_argument("--day", default=date.today().isoformat())
    parser.add_argument("--chat-id", default=None)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    stats = build_stats(conn, args.day, args.chat_id, args.top)
    conn.close()
    content = to_markdown(stats)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(content)
        print("已写入 %s" % args.out)
    else:
        print(content)


if __name__ == "__main__":
    main()
