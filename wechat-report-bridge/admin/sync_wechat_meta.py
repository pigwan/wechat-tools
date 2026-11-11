"""从运行中的微信进程内存只读同步真实会话名（群 / 好友 / 公众号）与头像。

helper 4.2 的公开接口刻意不返回群名/头像（contacts() 拒绝 @chatroom，
文档也写明「页面不读取头像」）。但微信自己的 ContactCache 里两者都在：

    Contact record + 0x28  Alias（自定义微信号，例如 pigwan）
    Contact record + 0x78  Remark（备注 / 群备注）
    Contact record + 0xD8  NickName（真实群名 / 好友昵称）
    Contact record + 0x158 BigHeadImgUrl（wx.qlogo.cn/mmcrhead|mmhead/...）

目录的 key 就是可直接用于发消息的会话 ID：@chatroom 群、wxid_xxx 好友、
gh_xxx 公众号，以及「自定义微信号」本身（如 pigwan、huzi6044）。

本脚本复用 helper 已核对的内存指纹与只读读取器，不注入、不调用微信函数、
不触发网络同步、不修改微信任何数据；只做 OpenProcess(PROCESS_VM_READ) +
ReadProcessMemory。头像图片从微信 CDN 下载后缓存到 admin/avatars/。

输出：
    admin/chats.auto.json    自动同步的会话名/别名/头像（可被 chats.local.json 覆盖）
    admin/avatars/<id>.jpg   真实头像
    admin/sync_status.json   最近一次同步状态

用法：
    python admin/sync_wechat_meta.py                 # 同步名字 + 头像
    python admin/sync_wechat_meta.py --no-avatars   # 只同步名字
    python admin/sync_wechat_meta.py --json         # 机器可读结果
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent           # admin/
BRIDGE = ROOT.parent                             # wechat-report-bridge/
WORKSPACE = BRIDGE.parent                        # 微信工具/
HELPER = WORKSPACE / "vendor" / "helper-main"
AUTO_FILE = ROOT / "chats.auto.json"
AVATAR_DIR = ROOT / "avatars"
STATUS_FILE = ROOT / "sync_status.json"

MAX_RECORDS = 3000
MAX_NAME = 64
MAX_AVATAR_BYTES = 512 * 1024
AVATAR_TIMEOUT = 6
AVATAR_HOSTS = {"wx.qlogo.cn", "mmhead.qpic.cn"}
MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# ContactCache 记录中已核对的字段偏移（4.1.15.56 / 4.2.0.11 / 4.2.0.12）。
OFF_ALIAS = 0x28
OFF_REMARK = 0x78
OFF_NICKNAME = 0xD8
OFF_HEAD_URL = 0x158
OFF_DELETED = 0x68

CHAT_KEY_RE = re.compile(r"^[A-Za-z0-9_@.\-]{2,80}$")
# 微信内置功能账号：保留展示，但不作为发送候选。
SYSTEM_IDS = {"medianote", "floatbottle", "fmessage", "mphelper",
              "weixin", "qqmail", "tmessage", "qmessage", "newsapp"}


def kind_of(chat_id):
    text = str(chat_id)
    if text.endswith("@chatroom"):
        return "group"
    if text == "filehelper":
        return "helper"
    if text.startswith("gh_"):
        return "official"
    if text in SYSTEM_IDS:
        return "system"
    if text.endswith("@openim"):
        return "contact"
    return "contact"


def now_text():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def safe_key(chat_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(chat_id))[:80]


def clean_text(value):
    text = CONTROL.sub("", str(value or "")).strip()
    if not text or len(text) > MAX_NAME:
        return ""
    return text


def safe_avatar_url(value):
    text = str(value or "").strip()
    if not text or len(text) > 512:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme not in ("https", "http") or parsed.hostname not in AVATAR_HOSTS:
        return ""
    # 部分群（尤其是默认九宫格头像）缓存里是 http://，下载时统一升级到 HTTPS。
    if parsed.scheme == "http":
        return "https://" + text.split("://", 1)[1]
    return text


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def import_helper():
    for entry in (str(HELPER), str(HELPER / "native")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import run_selftest as api                                    # noqa: E402
    from wxbot_framework.moments_reader import _Memory            # noqa: E402
    from wxbot_framework.account import read_current_account      # noqa: E402
    from wxbot_framework.native_profiles import (                 # noqa: E402
        profile_for_fingerprint, select_memory_profile)
    from wxbot_framework.contacts import (                        # noqa: E402
        _DirectoryReader, _directory_cache, _directory_records)
    from wxbot_framework.group_members import _StableReader       # noqa: E402
    return (api, _Memory, read_current_account, profile_for_fingerprint,
            select_memory_profile, _DirectoryReader, _directory_cache,
            _directory_records, _StableReader)


def string_at(reader, address, limit=512):
    """宽松读取 MSVC std::string；字段不存在/正在变化时返回空串。"""
    try:
        header = reader.read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if not 1 <= length <= limit or capacity < length or not (
                capacity == 15 or 16 <= capacity <= 1048576):
            return ""
        start = address if capacity == 15 else struct.unpack_from("<Q", header)[0]
        if not start:
            return ""
        return reader.read(start, length).decode("utf-8", "replace")
    except (OSError, ValueError, struct.error, UnicodeError):
        return ""


def collect_from_wechat():
    """只读枚举 ContactCache，返回 {chat_id: {name, avatar_url}}。"""
    (api, _Memory, read_current_account, profile_for_fingerprint,
     select_memory_profile, _DirectoryReader, _directory_cache,
     _directory_records, _StableReader) = import_helper()

    pids = api.wechat_candidates()
    if not pids:
        raise RuntimeError("未找到运行中的微信进程（Weixin.exe + Weixin.dll）")
    last_error = None
    for pid in pids[:3]:
        try:
            core = api.modules(pid).get("weixin.dll")
            if not core:
                continue
            base, dll_path = core[0], Path(core[1])
            stat = dll_path.stat()
            digest = hashlib.sha256(dll_path.read_bytes()).hexdigest()
            profile = profile_for_fingerprint(stat.st_size, digest)
            if profile is None:
                last_error = "Weixin.dll 指纹不匹配（微信版本已变化），已跳过"
                continue
            memory = _Memory(pid)
            read = memory.read
            if select_memory_profile(read, base) is None:
                last_error = "微信核心对象链未就绪，已跳过"
                continue
            account = read_current_account(read, base)
            if not account:
                last_error = "当前微信账号尚未就绪，已跳过"
                continue

            directory = _DirectoryReader(read)
            _context, cache = _directory_cache(directory, base, profile.rva)
            fields = _StableReader(read)
            result = {}
            for key, record in _directory_records(directory, cache):
                if len(result) >= MAX_RECORDS:
                    break
                chat_id = str(key)
                if not CHAT_KEY_RE.match(chat_id):
                    continue
                try:
                    deleted = struct.unpack("<I", fields.read(record + OFF_DELETED, 4))[0]
                except (OSError, ValueError, struct.error):
                    continue
                if deleted:
                    continue
                nickname = clean_text(string_at(fields, record + OFF_NICKNAME, 256))
                remark = clean_text(string_at(fields, record + OFF_REMARK, 1024))
                alias = clean_text(string_at(fields, record + OFF_ALIAS, 128))
                head = safe_avatar_url(string_at(fields, record + OFF_HEAD_URL, 512))
                name = remark or nickname
                if not name and not head:
                    continue
                result[chat_id] = {
                    "name": name,
                    "wechat_name": nickname,
                    "alias": alias,
                    "kind": kind_of(chat_id),
                    "avatar_url": head,
                    "is_self": chat_id == str(account),
                }
            if read_current_account(read, base) != account:
                last_error = "同步期间微信账号发生变化，已跳过"
                continue
            return result, {"pid": pid, "version": profile.version,
                            "account": account, "dll_sha256": digest[:16]}
        except (OSError, ValueError, struct.error, RuntimeError) as error:
            last_error = "%s: %s" % (type(error).__name__, error)
            continue
    raise RuntimeError(last_error or "微信联系人缓存读取失败")


def extension_for(body):
    for magic, ext in MAGIC:
        if body.startswith(magic):
            return ext
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    return ""


def download_avatar(url, key, previous_url, previous_file):
    """URL 未变且文件已存在则复用；返回 (文件名, 本次是否真的下载)。"""
    if not url:
        return "", False
    if url == previous_url and previous_file:
        path = AVATAR_DIR / Path(str(previous_file)).name
        if path.is_file():
            return path.name, False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(request, timeout=AVATAR_TIMEOUT) as response:
        body = response.read(MAX_AVATAR_BYTES + 1)
    if len(body) > MAX_AVATAR_BYTES or len(body) < 64:
        raise ValueError("头像体积异常")
    ext = extension_for(body)
    if not ext:
        raise ValueError("头像格式不是常见图片")
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    target = AVATAR_DIR / (key + ext)
    tmp = AVATAR_DIR / (key + ext + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, target)
    return target.name, True


def sync(download=True):
    started = time.time()
    rows, info = collect_from_wechat()
    previous = read_json(AUTO_FILE)
    if not isinstance(previous, dict):
        previous = {}
    merged, downloaded, failed = {}, 0, 0
    for chat_id, row in rows.items():
        old = previous.get(chat_id) if isinstance(previous.get(chat_id), dict) else {}
        item = {
            "name": row["name"],
            "wechat_name": row.get("wechat_name", ""),
            "alias": row.get("alias", ""),
            "kind": row.get("kind", "contact"),
            "is_self": bool(row.get("is_self")),
            "avatar_url": row["avatar_url"],
            "source": "wechat_memory",
            "updated_at": now_text(),
        }
        if download and row["avatar_url"]:
            try:
                filename, changed = download_avatar(
                    row["avatar_url"], safe_key(chat_id),
                    old.get("avatar_url", ""), old.get("avatar", ""))
                if filename:
                    item["avatar"] = filename
                    if changed:
                        downloaded += 1
            except Exception:
                failed += 1
                if old.get("avatar"):
                    item["avatar"] = str(old["avatar"])
        elif old.get("avatar"):
            item["avatar"] = str(old["avatar"])
        merged[chat_id] = item
    atomic_json(AUTO_FILE, merged)
    status = {
        "last_sync": now_text(),
        "count": len(merged),
        "groups": sum(1 for key in merged if key.endswith("@chatroom")),
        "contacts": sum(1 for value in merged.values()
                        if value.get("kind") in ("contact", "helper")),
        "avatars": sum(1 for value in merged.values() if value.get("avatar")),
        "downloaded": downloaded,
        "failed": failed,
        "seconds": round(time.time() - started, 2),
        "error": "",
        "version": info.get("version", ""),
        "account": info.get("account", ""),
        "source": "wechat_memory",
    }
    atomic_json(STATUS_FILE, status)
    return status


def main():
    parser = argparse.ArgumentParser(description="同步微信真实会话名称/微信号/头像")
    parser.add_argument("--no-avatars", action="store_true", help="只同步名称，不下载头像")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()
    try:
        result = sync(download=not args.no_avatars)
    except Exception as error:
        status = {"last_sync": now_text(), "count": 0, "groups": 0,
                  "contacts": 0, "avatars": 0,
                  "downloaded": 0, "failed": 0, "seconds": 0, "version": "",
                  "account": "", "source": "wechat_memory",
                  "error": "%s: %s" % (type(error).__name__, error)}
        try:
            atomic_json(STATUS_FILE, status)
        except OSError:
            pass
        if args.json:
            print(json.dumps({"ok": False, **status}, ensure_ascii=False))
        else:
            print("同步失败：%s" % status["error"], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    else:
        print("微信 %s 账号 %s" % (result["version"], result["account"]))
        print("同步 %d 个会话（群 %d / 好友等 %d），头像 %d 个"
              "（新下载 %d，失败 %d），耗时 %.2fs" %
              (result["count"], result["groups"], result["contacts"],
               result["avatars"], result["downloaded"], result["failed"],
               result["seconds"]))
        print("已写入 %s" % AUTO_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
