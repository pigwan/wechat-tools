"""只读同步微信社交资料：群成员（含头像）、好友资料、朋友圈缓存。

与 sync_wechat_meta.py 一样，复用 helper 已核对的内存指纹与只读读取器：
只做 OpenProcess(PROCESS_VM_READ) + ReadProcessMemory，不注入、不调用微信函数、
不触发网络同步、不修改微信任何数据。头像图片从微信 CDN 下载到 admin/avatars/。

数据来源与边界（都会在输出里显式标注，不推测、不伪造）：
  * 群成员  : ChatroomCache 快照（read_group_members）。必须先在微信打开过该群，
              否则该群标记为 cache_not_ready 并保留上一次成功的成员列表。
  * 头像    : ContactCache 记录 +0x158 的 BigHeadImgUrl，按 wxid 与成员求交集；
              没进联系人缓存的群成员（陌生人）不会凭空编造头像。
              已有会话头像直接复用文件，其余按wxid下载为 avatars/member_<id>.jpg。
  * 好友    : ContactCache 里 flags & 1 的记录，含昵称/备注/微信号/个性描述/头像。
              性别、地区、签名等 ContactExtData 只在 4.1.15.56 核对过布局，
              当前 4.2.x 不适配，因此统一留空并在字段里标注原因，绝不猜测偏移。
  * 朋友圈  : read_loaded_moments，仅读电脑微信「已经加载」的列表（最多 50 条），
              必须在电脑微信先打开朋友圈；不含图片内容，只有 media_count。
              因需要扫描进程内存，只按需手动触发，不放进定时同步。

输出（原子写）：
    admin/social_data.json     群 / 好友 / 朋友圈数据
    admin/social_status.json   最近一次同步状态

用法：
    python admin/sync_wechat_social.py                    # 群成员 + 好友（定时同步用）
    python admin/sync_wechat_social.py --only moments     # 只刷新朋友圈（手动触发）
    python admin/sync_wechat_social.py --only all         # 全部
    python admin/sync_wechat_social.py --no-avatars --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

# 作为独立子进程启动时，嵌入式解释器可能不把脚本目录放进 sys.path。
_ADMIN_DIR = Path(__file__).resolve().parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import sync_wechat_meta as meta  # noqa: E402

# 项目自带解释器（runtime/python313）里带 pefile；若用系统 Python 直接启动，
# 补上随项目分发的 site-packages，避免进程枚举与朋友圈模块因缺依赖整体失败。
try:
    import pefile  # noqa: F401
except ImportError:
    _bundled = meta.WORKSPACE / "runtime" / "python313" / "Lib" / "site-packages"
    if _bundled.is_dir() and str(_bundled) not in sys.path:
        sys.path.append(str(_bundled))

ROOT = meta.ROOT
SOCIAL_FILE = ROOT / "social_data.json"
SOCIAL_STATUS_FILE = ROOT / "social_status.json"
AUTO_FILE = meta.AUTO_FILE
AVATAR_DIR = meta.AVATAR_DIR

MAX_GROUPS = 300
MAX_MEMBERS_TOTAL = 20000
MAX_NEW_AVATARS = 60          # 单次同步最多新下载多少张，避免卡住后台线程
MOMENTS_LIMIT = 50

OFF_ALIAS = meta.OFF_ALIAS          # 0x28  自定义微信号
OFF_REMARK = meta.OFF_REMARK        # 0x78  备注
OFF_NICKNAME = meta.OFF_NICKNAME    # 0xD8  微信昵称 / 真实群名
OFF_HEAD_URL = meta.OFF_HEAD_URL    # 0x158 大头像 URL
OFF_DESCRIPTION = 0x1A0            # 4.1.15.56 已核对的个性描述
OFF_DELETED = meta.OFF_DELETED      # 0x68  deleted(uint32) + flags(uint32)

EXT_NOTE = "性别 / 地区 / 签名等扩展资料未在本链路核对布局，不读取"


def clean(value, limit=512):
    text = meta.CONTROL.sub("", str(value or "")).strip()
    return text[:limit]


def now_text():
    return meta.now_text()


def empty_data():
    return {
        "updated_at": "", "account": "", "version": "", "source": "wechat_memory",
        "groups": {}, "friends": [],
        "moments": {"updated_at": "", "status": "empty", "error": "",
                    "note": "请先在电脑微信打开朋友圈并等待列表加载，然后点「刷新朋友圈」",
                    "source": "", "rows": [], "loaded_count": 0, "truncated": False},
        "stats": {},
    }


def load_data():
    data = meta.read_json(SOCIAL_FILE)
    base = empty_data()
    if not isinstance(data, dict):
        return base
    for key in ("updated_at", "account", "version"):
        if isinstance(data.get(key), str):
            base[key] = data[key]
    if isinstance(data.get("groups"), dict):
        base["groups"] = data["groups"]
    if isinstance(data.get("friends"), list):
        base["friends"] = data["friends"]
    if isinstance(data.get("moments"), dict):
        base["moments"] = dict(base["moments"], **data["moments"])
    if isinstance(data.get("stats"), dict):
        base["stats"] = data["stats"]
    return base


def local_avatar(chat_id, auto):
    """已有会话头像直接复用，不再重复下载。"""
    row = auto.get(str(chat_id))
    if not isinstance(row, dict):
        return ""
    name = Path(str(row.get("avatar") or "")).name
    if name and (AVATAR_DIR / name).is_file():
        return name
    return ""


class WechatSession:
    """持有一次只读进程句柄；关闭它不会影响运行中的微信或桥接。"""

    def __init__(self):
        (self.api, _Memory, self.read_current_account,
         self.profile_for_fingerprint, self.select_memory_profile,
         self.DirectoryReader, self.directory_cache,
         self.directory_records, _StableReader) = meta.import_helper()
        self._Memory = _Memory
        self.memory = None
        self.pid = 0
        self.base = 0
        self.account = ""
        self.version = ""
        self.profile = None
        self._open()

    def _open(self):
        pids = self.api.wechat_candidates()
        if not pids:
            raise RuntimeError("未找到运行中的微信进程（Weixin.exe + Weixin.dll）")
        last = None
        for pid in pids[:3]:
            try:
                core = self.api.modules(pid).get("weixin.dll")
                if not core:
                    continue
                base, dll_path = core[0], Path(core[1])
                digest = hashlib.sha256(dll_path.read_bytes()).hexdigest()
                profile = self.profile_for_fingerprint(dll_path.stat().st_size, digest)
                if profile is None:
                    last = "Weixin.dll 指纹不匹配（微信版本已变化）"
                    continue
                memory = self._Memory(pid)
                read = memory.read
                if self.select_memory_profile(read, base) is None:
                    memory.close()
                    last = "微信核心对象链尚未就绪"
                    continue
                account = self.read_current_account(read, base)
                if not account:
                    memory.close()
                    last = "当前微信账号尚未就绪"
                    continue
                self.memory, self.pid, self.base = memory, pid, base
                self.account, self.version, self.profile = str(account), profile.version, profile
                return
            except (OSError, ValueError, struct.error, RuntimeError) as error:
                last = "%s: %s" % (type(error).__name__, error)
                continue
        raise RuntimeError(last or "微信进程只读连接失败")

    @property
    def read(self):
        return self.memory.read

    def close(self):
        if self.memory is not None:
            try:
                self.memory.close()
            finally:
                self.memory = None


def collect_directory(session):
    """只读枚举 ContactCache，返回 {chat_id: 资料}。失败返回空字典 + 错误文本。"""
    try:
        directory = session.DirectoryReader(session.read)
        _context, cache = session.directory_cache(directory, session.base,
                                                  session.profile.rva)
        fields = session.DirectoryReader(session.read)   # 独立预算，避免与容器遍历互相挤占
        result = {}
        for key, record in session.directory_records(directory, cache):
            if len(result) >= meta.MAX_RECORDS:
                break
            chat_id = str(key)
            if not meta.CHAT_KEY_RE.match(chat_id):
                continue
            try:
                deleted, flags = struct.unpack("<II",
                                               fields.read(record + OFF_DELETED, 8))
            except (OSError, ValueError, struct.error):
                continue
            if deleted:
                continue
            nickname = clean(meta.string_at(fields, record + OFF_NICKNAME, 256), 128)
            remark = clean(meta.string_at(fields, record + OFF_REMARK, 1024), 128)
            alias = clean(meta.string_at(fields, record + OFF_ALIAS, 128), 128)
            description = clean(meta.string_at(fields, record + OFF_DESCRIPTION, 2048), 1024)
            head = meta.safe_avatar_url(meta.string_at(fields, record + OFF_HEAD_URL, 512))
            name = remark or nickname
            if not name and not head:
                continue
            result[chat_id] = {
                "wxid": chat_id,
                "name": name,
                "wechat_name": nickname,
                "remark": remark,
                "alias": alias,
                "description": description,
                "kind": meta.kind_of(chat_id),
                "is_friend": bool(flags & 1),
                "is_self": chat_id == session.account,
                "avatar_url": head,
                "sex": "", "signature": "", "region": "",
                "ext_note": EXT_NOTE,
            }
        return result, ""
    except (OSError, ValueError, struct.error, RuntimeError) as error:
        return {}, "%s: %s" % (type(error).__name__, error)


def collect_groups(session, directory, auto, old_groups, download):
    """逐个群读取 ChatroomCache 成员快照，并按 wxid 补齐头像。"""
    from wxbot_framework.group_members import read_group_members

    rooms = [key for key in auto if str(key).endswith("@chatroom")][:MAX_GROUPS]
    if not rooms:
        rooms = [key for key in directory if str(key).endswith("@chatroom")][:MAX_GROUPS]

    groups, total, avatar_hits, downloaded = {}, 0, 0, 0
    ready, not_ready = 0, 0
    for room in rooms:
        room = str(room)
        meta_row = auto.get(room) if isinstance(auto.get(room), dict) else {}
        group = {
            "room": room,
            "name": str(meta_row.get("name") or (directory.get(room) or {}).get("name") or room),
            "avatar": local_avatar(room, auto),
            "avatar_url": str(meta_row.get("avatar_url") or ""),
            "member_count": 0,
            "status": "cache_not_ready",
            "note": "",
            "members": [],
            "updated_at": "",
        }
        try:
            rows = read_group_members(session.read, session.base, room,
                                      expected_account=session.account)
        except (OSError, ValueError, struct.error, RuntimeError) as error:
            rows = None
            group["note"] = "读取失败：%s" % type(error).__name__
        if rows is None:
            not_ready += 1
            previous = old_groups.get(room) if isinstance(old_groups.get(room), dict) else {}
            if isinstance(previous.get("members"), list) and previous["members"]:
                group["members"] = previous["members"]
                group["member_count"] = len(previous["members"])
                group["updated_at"] = str(previous.get("updated_at") or "")
                group["note"] = "微信群缓存未就绪，显示上一次成功读取的成员（%s）" % (
                    group["updated_at"] or "时间未知")
            else:
                group["note"] = "群成员缓存尚未就绪：请先在电脑微信打开该群，再点「立即同步」"
            groups[room] = group
            continue

        ready += 1
        group["status"] = "ok"
        group["updated_at"] = now_text()
        previous = old_groups.get(room) if isinstance(old_groups.get(room), dict) else {}
        previous_members = previous.get("members") if isinstance(previous.get("members"), list) else []
        old_by_wxid = {str(m.get("wxid") or ""): m for m in previous_members
                       if isinstance(m, dict)}
        members = []
        for row in rows:
            if total >= MAX_MEMBERS_TOTAL:
                break
            wxid = str(row.get("wxid") or "")
            if not wxid:
                continue
            contact = directory.get(wxid) or {}
            if not isinstance(contact, dict):
                contact = {}
            avatar = local_avatar(wxid, auto)
            url = str(contact.get("avatar_url") or "")
            if not avatar and url and download and downloaded < MAX_NEW_AVATARS:
                old_member = old_by_wxid.get(wxid) or {}
                try:
                    filename, changed = meta.download_avatar(
                        url, "member_" + meta.safe_key(wxid),
                        str(old_member.get("avatar_url") or ""),
                        str(old_member.get("avatar") or ""))
                    if filename:
                        avatar = filename
                        if changed:
                            downloaded += 1
                except Exception:
                    avatar = str(old_member.get("avatar") or "")
            if avatar:
                avatar_hits += 1
            members.append({
                "wxid": wxid,
                "nickname": str(row.get("nickname") or ""),
                "group_nickname": str(row.get("group_nickname") or ""),
                "wechat_name": str(row.get("wechat_name") or ""),
                "role": str(row.get("role") or "unknown"),
                "is_owner": row.get("is_owner"),
                "is_admin": row.get("is_admin"),
                "avatar": avatar,
                "avatar_url": url,
                "in_directory": bool(contact),
                "remark": str(contact.get("remark") or ""),
                "alias": str(contact.get("alias") or ""),
                "description": str(contact.get("description") or ""),
                "sex": "", "signature": "", "region": "",
                "ext_note": EXT_NOTE if contact else "未进入联系人缓存，无法读取资料",
            })
            total += 1
        group["members"] = members
        group["member_count"] = len(members)
        groups[room] = group
    return groups, {
        "groups_ready": ready, "groups_not_ready": not_ready,
        "members": total, "avatar_hits": avatar_hits, "downloaded": downloaded,
    }


def collect_friends(directory, auto):
    """只保留 flags & 1 的好友/联系人，附上已同步的会话头像。"""
    rows = []
    for chat_id, row in directory.items():
        if not isinstance(row, dict) or not row.get("is_friend"):
            continue
        if row.get("is_self") or str(chat_id).endswith("@chatroom"):
            continue
        if str(chat_id).startswith("gh_") or meta.kind_of(chat_id) in ("system", "official"):
            continue
        meta_row = auto.get(chat_id) if isinstance(auto.get(chat_id), dict) else {}
        item = dict(row)
        item["avatar"] = local_avatar(chat_id, auto)
        item["avatar_url"] = str(row.get("avatar_url") or meta_row.get("avatar_url") or "")
        item["name"] = str(meta_row.get("name") or row.get("name") or chat_id)
        item["wechat_name"] = str(meta_row.get("wechat_name") or row.get("wechat_name") or "")
        item["remark"] = str(row.get("remark") or "")
        item["alias"] = str(row.get("alias") or meta_row.get("alias") or "")
        item["source"] = "wechat_memory"
        rows.append(item)
    rows.sort(key=lambda item: (item.get("name") or item.get("wechat_name") or item["wxid"]).casefold())
    return rows


def collect_moments(session):
    """读电脑微信已加载的朋友圈缓存；失败时返回明确原因，不伪造数据。"""
    from wxbot_framework.moments_reader import read_loaded_moments

    started = time.time()
    try:
        result = read_loaded_moments(session.pid, session.account, limit=MOMENTS_LIMIT)
        return {
            "updated_at": now_text(), "status": "ok", "error": "",
            "note": "仅当前已加载的缓存，最多 %d 条；不含图片内容" % MOMENTS_LIMIT,
            "source": result.get("source", ""),
            "rows": list(result.get("rows") or []),
            "loaded_count": int(result.get("loaded_count") or 0),
            "truncated": bool(result.get("truncated")),
            "seconds": round(time.time() - started, 2),
        }
    except Exception as error:
        return {
            "updated_at": now_text(), "status": "error",
            "error": "%s: %s" % (type(error).__name__, error),
            "note": "请先在电脑微信打开朋友圈并等待列表加载；读取不请求微信服务器",
            "source": "", "rows": [], "loaded_count": 0, "truncated": False,
            "seconds": round(time.time() - started, 2),
        }


def sync(parts=("groups", "friends"), download=True):
    started = time.time()
    data = load_data()
    old_groups = dict(data["groups"])
    auto = meta.read_json(AUTO_FILE)
    if not isinstance(auto, dict):
        auto = {}
    previous_stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    status = {
        "last_sync": now_text(), "mode": "+".join(parts),
        "error": "", "version": "", "account": "",
        # 单模式（例如只刷朋友圈）时，保留上一次的完整统计，避免状态里出现误导性的 0。
        "groups": int(previous_stats.get("groups") or 0),
        "members": int(previous_stats.get("members") or 0),
        "friends": int(previous_stats.get("friends") or 0),
        "moments": int(previous_stats.get("moments") or 0),
        "avatar_hits": 0, "downloaded": 0, "seconds": 0,
        "directory_error": "", "moments_error": "",
    }
    session = None
    try:
        session = WechatSession()
        status["version"], status["account"] = session.version, session.account
        data["version"], data["account"] = session.version, session.account

        directory = {}
        if "groups" in parts or "friends" in parts:
            directory, error = collect_directory(session)
            status["directory_error"] = error

        if "groups" in parts:
            groups, stats = collect_groups(session, directory, auto, old_groups, download)
            data["groups"] = groups
            status.update(groups=len(groups), members=stats["members"],
                          avatar_hits=stats["avatar_hits"],
                          downloaded=stats["downloaded"])
        if "friends" in parts:
            data["friends"] = collect_friends(directory, auto)
            status["friends"] = len(data["friends"])
        if "moments" in parts:
            data["moments"] = collect_moments(session)
            status["moments"] = len(data["moments"].get("rows") or [])
            status["moments_error"] = data["moments"].get("error", "")

        if "groups" in parts or "friends" in parts:
            data["updated_at"] = now_text()
        data["stats"] = {
            "groups": len(data["groups"]),
            "members": sum(int((g or {}).get("member_count") or 0)
                           for g in data["groups"].values() if isinstance(g, dict)),
            "friends": len(data["friends"]),
            "moments": len((data["moments"] or {}).get("rows") or []),
        }
        meta.atomic_json(SOCIAL_FILE, data)
    except Exception as error:
        status["error"] = "%s: %s" % (type(error).__name__, error)
    finally:
        if session is not None:
            session.close()
        status["seconds"] = round(time.time() - started, 2)
        try:
            meta.atomic_json(SOCIAL_STATUS_FILE, status)
        except OSError:
            pass
    return status


def main():
    parser = argparse.ArgumentParser(description="只读同步微信群成员 / 好友 / 朋友圈资料")
    parser.add_argument("--only", choices=("auto", "groups", "friends", "moments", "all"),
                        default="auto", help="同步范围；auto = 群成员 + 好友")
    parser.add_argument("--no-avatars", action="store_true", help="不下载新头像")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()
    parts = {
        "auto": ("groups", "friends"), "all": ("groups", "friends", "moments"),
        "groups": ("groups",), "friends": ("friends",), "moments": ("moments",),
    }[args.only]
    result = sync(parts=parts, download=not args.no_avatars)
    if args.json:
        print(json.dumps({"ok": not result["error"], **result}, ensure_ascii=False))
    else:
        if result["error"]:
            print("同步失败：%s" % result["error"], file=sys.stderr)
        else:
            print("微信 %s 账号 %s｜群 %d（成员 %d，有头像 %d，新下载 %d）"
                  "｜好友 %d｜朋友圈 %d 条｜耗时 %.2fs" %
                  (result["version"], result["account"], result["groups"],
                   result["members"], result["avatar_hits"], result["downloaded"],
                   result["friends"], result["moments"], result["seconds"]))
            if result["directory_error"]:
                print("联系人缓存：%s" % result["directory_error"], file=sys.stderr)
            if result["moments_error"]:
                print("朋友圈：%s" % result["moments_error"], file=sys.stderr)
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
