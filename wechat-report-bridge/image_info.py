"""只读汇总 helper 已接收的微信图片信息（不改采集、不重启微信、不上传图片）。

helper 4.2 的 native_text 后端收到允许会话的普通图片时，会解析消息里的
msg/img XML，用 CDN filekey + aeskey 调原生下载任务把图片解密保存到：

    vendor/helper-main/.runtime/images/account-<hash>/image-<序号>-<md5>.<ext>

同时在 logs/bot.log 写一行「图片接收 | 会话ID=… | 状态=… | 文件=…」。
本脚本把这两处信息合并，再用 Pillow 读取图片真实格式/尺寸，并关联
admin/chats.auto.json 里的真实群名。

用法：
    python image_info.py                     表格输出（按时间倒序）
    python image_info.py --json              JSON
    python image_info.py --csv out.csv       导出 CSV
    python image_info.py --chat 58507398167@chatroom   只看某个会话
    python image_info.py --limit 10          只看最近 10 条
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
WORKSPACE = BRIDGE.parent
HELPER = WORKSPACE / "vendor" / "helper-main"
IMAGE_DIR = HELPER / ".runtime" / "images"
LOG_FILE = HELPER / "logs" / "bot.log"
CHAT_AUTO = BRIDGE / "admin" / "chats.auto.json"

LOG_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+"
    r"INFO\s+\[NativeText\]\s+图片接收\s+\|\s+"
    r"会话ID=(?P<chat_id>.*?)\s+\|\s+状态=(?P<status>.*?)\s+\|\s+"
    r"文件=(?P<file>.*)$")
NAME_RE = re.compile(r"^image-(?P<seq>\d+)-(?P<md5>[0-9a-fA-F]+)\.(?P<ext>\w+)$")


def read_chat_names():
    try:
        data = json.loads(CHAT_AUTO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str((value or {}).get("name", ""))
            for key, value in data.items() if isinstance(value, dict)}


def probe_image(path):
    """Pillow 可用时读取真实格式与尺寸；不可用时退化为文件头解析。"""
    info = {"format": "", "width": 0, "height": 0}
    try:
        from PIL import Image
        with Image.open(path) as image:
            info["format"] = (image.format or "").upper()
            info["width"], info["height"] = image.size
        return info
    except Exception:
        pass
    try:
        header = path.open("rb").read(32)
    except OSError:
        return info
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
        info.update(format="PNG",
                    width=int.from_bytes(header[16:20], "big"),
                    height=int.from_bytes(header[20:24], "big"))
    elif header[:3] in (b"GIF8",):
        info.update(format="GIF",
                    width=int.from_bytes(header[6:8], "little"),
                    height=int.from_bytes(header[8:10], "little"))
    return info


def collect():
    names = read_chat_names()
    records = []
    if not LOG_FILE.is_file():
        return records
    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LOG_RE.match(line.strip())
        if not match:
            continue
        row = match.groupdict()
        path = Path(row["file"].strip()) if row["file"].strip() else None
        filename = path.name if path else ""
        named = NAME_RE.match(filename) if filename else None
        item = {
            "time": row["time"],
            "status": row["status"].strip(),
            "chat_id": row["chat_id"].strip(),
            "chat_name": names.get(row["chat_id"].strip(), "") or row["chat_id"].strip(),
            "sequence": int(named.group("seq")) if named else 0,
            "md5": named.group("md5") if named else "",
            "filename": filename,
            "format": "", "width": 0, "height": 0,
            "size_bytes": 0,
            "path": str(path) if path else "",
        }
        if path and path.is_file():
            item["size_bytes"] = path.stat().st_size
            item.update(probe_image(path))
        records.append(item)
    records.sort(key=lambda row: (row["time"], row["sequence"]), reverse=True)
    return records


def human_size(value):
    return "%.1f KB" % (value / 1024) if value < 1024 * 1024 else "%.2f MB" % (value / 1048576)


def print_table(records):
    if not records:
        print("没有找到图片接收记录（helper 只保存启动后实时到达的普通图片）。")
        return
    print("共 %d 条图片记录，成功 %d 张，失败 %d 条\n" % (
        len(records), sum(1 for r in records if r["status"] == "ready"),
        sum(1 for r in records if r["status"] != "ready")))
    print("%-19s %-26s %-8s %-14s %-10s %s" %
          ("时间", "会话", "状态", "格式/尺寸", "大小", "文件"))
    print("-" * 108)
    for row in records:
        dimension = ("%s %dx%d" % (row["format"], row["width"], row["height"])
                     if row["width"] else "-")
        size = human_size(row["size_bytes"]) if row["size_bytes"] else "-"
        print("%-19s %-26s %-8s %-14s %-10s %s" % (
            row["time"], row["chat_name"][:24], row["status"], dimension,
            size, row["filename"] or "(下载失败，无文件)"))
    print("\n图片目录：%s" % IMAGE_DIR)


def main():
    parser = argparse.ArgumentParser(description="查看 helper 已接收的微信图片信息（只读）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--csv", default="", help="导出到 CSV 文件")
    parser.add_argument("--chat", default="", help="只显示指定会话 ID")
    parser.add_argument("--limit", type=int, default=0, help="只显示最近 N 条")
    args = parser.parse_args()
    records = collect()
    if args.chat:
        records = [r for r in records if r["chat_id"] == args.chat]
    if args.limit > 0:
        records = records[:args.limit]
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        print_table(records)
    if args.csv:
        target = Path(args.csv)
        if not target.is_absolute():
            target = BRIDGE / target
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()) if records else
                                    ["time", "status", "chat_id", "chat_name"])
            writer.writeheader()
            writer.writerows(records)
        print("已导出 CSV：%s" % target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
