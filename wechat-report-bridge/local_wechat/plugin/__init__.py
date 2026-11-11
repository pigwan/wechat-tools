"""local_api 插件：本地能力中心的执行端（运行在 helper 框架进程内）。

职责：从本地 SQLite 任务队列领取动作，调用本机微信桥接（ctx._service）执行，
把结果写回队列。不联网、不开端口、不需要任何 Token，也不改动框架或现有插件。

生命周期：on_load 启动后台线程；on_unload 停止；on_message 为空（框架要求实现）。
任务分派依据 local_wechat/capabilities.py 里的 handler 名，单一数据源。
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
import uuid
from pathlib import Path


def _mount_local_package():
    """让插件副本也能 import 同一份 local_wechat 源码，避免代码分叉。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        bridge = parent / "wechat-report-bridge"
        if (bridge / "local_wechat" / "capabilities.py").is_file():
            if str(bridge) not in sys.path:
                sys.path.insert(0, str(bridge))
            import local_wechat.capabilities as caps
            import local_wechat.queue as queue_mod
            return caps, queue_mod
    raise RuntimeError("找不到 wechat-report-bridge/local_wechat，无法加载本地能力中心")


caps, queue_mod = _mount_local_package()


# --------------------------------------------------------------------------- 工具
def _svc(ctx):
    svc = getattr(ctx, "_service", None) or getattr(ctx, "service", None)
    if svc is None:
        raise RuntimeError("插件上下文未持有微信服务（微信可能未连接）")
    return svc


def _session(ctx):
    backend = getattr(_svc(ctx), "_backend", None)
    return getattr(backend, "session_id", None) if backend is not None else None


def _framework_root(ctx):
    root = getattr(ctx, "project_root", None)
    if root:
        return Path(root)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "native" / "text_service.py").is_file():
            return parent
    return here.parent


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return [v.strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _require_file(raw):
    path = Path(str(raw or "")).expanduser()
    if not path.is_absolute():
        path = _framework_root_path() / path
    if not path.is_file():
        raise ValueError("本机文件不存在：%s" % path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("文件超过 100 MiB：%s" % path)
    return path


_FRAMEWORK_ROOT = None


def _framework_root_path():
    global _FRAMEWORK_ROOT
    if _FRAMEWORK_ROOT is None:
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "native" / "text_service.py").is_file():
                _FRAMEWORK_ROOT = parent
                break
        else:
            _FRAMEWORK_ROOT = here.parent
    return _FRAMEWORK_ROOT


def _rows_from(snapshot):
    """contacts() 返回结构在不同版本略有差异，宽松取出其中的记录列表。"""
    if isinstance(snapshot, list):
        return snapshot
    if isinstance(snapshot, dict):
        for key in ("contacts", "rows", "items", "friends", "list", "data"):
            value = snapshot.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _rows_from(value)
                if nested:
                    return nested
        if all(not isinstance(v, (dict, list)) for v in snapshot.values()):
            return [snapshot]
    return []


def _contact_rows(ctx):
    return _rows_from(_svc(ctx).contacts())


def _field(row, *names):
    for name in names:
        if isinstance(row, dict) and row.get(name):
            return str(row[name])
    return ""


# --------------------------------------------------------------------------- handlers
def h_send_text(ctx, p):
    return ctx.send_text(p["toWxid"], p["content"])


def h_send_at_text(ctx, p):
    return ctx.send_at_text(p["room"], p["content"], _as_list(p.get("ats")))


def h_send_image(ctx, p):
    return ctx.send_image(p["toWxid"], str(_require_file(p["path"])))


def h_send_file(ctx, p):
    return ctx.send_file(p["toWxid"], str(_require_file(p["path"])))


def h_send_voice(ctx, p):
    return ctx.send_voice(p["toWxid"], str(_require_file(p["path"])))


def h_send_emoticon(ctx, p):
    return ctx.send_emoticon(p["toWxid"], str(_require_file(p["path"])))


def h_export_emoticons(ctx, p):
    return _svc(ctx).export_emoticons(convert=bool(p.get("convert", True)))


def h_local_media(ctx, p):
    kind = str(p.get("kind") or "image")
    limit = min(max(int(p.get("limit") or 20), 1), 200)
    sub = {"image": "images", "file": "files", "voice": "voices",
           "video": "files", "emoji": "emoticons"}.get(kind, kind)
    directory = _framework_root(ctx) / ".runtime" / sub
    if not directory.is_dir():
        return {"kind": kind, "directory": str(directory), "files": [],
                "note": "本地媒体目录尚不存在（收到对应消息后会自动创建）"}
    files = [f for f in directory.rglob("*") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return {"kind": kind, "directory": str(directory),
            "files": [{"path": str(f), "size": f.stat().st_size,
                       "mtime": int(f.stat().st_mtime)} for f in files[:limit]]}


def h_contacts(ctx, p):
    return _svc(ctx).contacts()


def h_contact_info(ctx, p):
    needle = str(p.get("wxid") or "")
    for row in _contact_rows(ctx):
        if needle and needle in json.dumps(row, ensure_ascii=False, default=str):
            return row
    return {"found": False, "wxid": needle}


def h_search_contacts(ctx, p):
    keyword = str(p.get("keyword") or "").casefold()
    hits = []
    for row in _contact_rows(ctx):
        blob = " ".join(_field(row, "name", "nickname", "remark", "alias", "wxid",
                               "userName", "wechat_name", "display_name")).casefold()
        if keyword and keyword in blob:
            hits.append(row)
    return {"keyword": p.get("keyword"), "count": len(hits), "rows": hits[:200]}


def h_accept_friend(ctx, p):
    return ctx.accept_friend(str(p["requestId"]))


def h_friend_requests(ctx, p):
    return _svc(ctx).friend_requests()


def h_create_group(ctx, p):
    return _svc(ctx).create_group(_as_list(p["wxids"]))


def h_invite_group_members(ctx, p):
    return _svc(ctx).invite_group_members(p["room"], _as_list(p["wxids"]))


def h_group_members(ctx, p):
    return ctx.group_members(p["room"])


def h_group_info(ctx, p):
    room = p["room"]
    members = ctx.group_members(room)
    rows = _rows_from(members) if not isinstance(members, dict) else _rows_from(members)
    names = _contact_rows(ctx)
    group_name = ""
    for row in names:
        if _field(row, "wxid", "userName", "chat_id") == room:
            group_name = _field(row, "name", "nickname", "remark")
            break
    return {"room": room, "name": group_name,
            "member_count": len(rows), "members": members}


def h_group_member_name(ctx, p):
    return {"name": _svc(ctx).group_member_name(p["room"], p["wxid"])}


def h_group_member_role(ctx, p):
    return {"role": _svc(ctx).group_member_role(p["room"], p["wxid"])}


def h_moments_capabilities(ctx, p):
    return _svc(ctx).moments_capabilities()


def _moments_images(raw):
    """把本机图片路径或 data_url 转成原生发布需要的字节列表。"""
    out = []
    for item in _as_list(raw) if isinstance(raw, str) else list(raw or []):
        if isinstance(item, dict) and item.get("data_url"):
            match = item["data_url"].split(",", 1)
            data = base64.b64decode(match[-1])
            name = str(item.get("name") or ("image_%d.png" % len(out)))
            ext = ".png" if "png" in match[0] else ".jpg"
        else:
            path = _require_file(item)
            data = path.read_bytes()
            name = path.name
            ext = path.suffix.lower() if path.suffix.lower() in (".png", ".jpg", ".jpeg") else ".jpg"
            if ext == ".jpeg":
                ext = ".jpg"
        out.append({"name": name, "bytes": data, "extension": ext})
    return out


def h_publish_moments(ctx, p):
    svc = _svc(ctx)
    session = _session(ctx)
    if not session:
        raise RuntimeError("微信尚未连接，无法发布朋友圈")
    ability = svc.moments_capabilities()
    if not ability.get("supported"):
        raise RuntimeError("当前微信版本未开放朋友圈发布：%s"
                           % (ability.get("reason") or "不支持"))
    payload = {
        "content": str(p.get("content") or "")[:1000],
        "visibility": "private" if str(p.get("privacy", "")).lower()
                        in ("1", "true", "private", "yes") else "public",
        "images": _moments_images(p.get("images")),
    }
    if not payload["content"].strip() and not payload["images"]:
        raise ValueError("朋友圈正文与图片不能同时为空")
    return svc.publish_moments(payload, expected_session=session)


def h_interact_moments(ctx, p):
    svc = _svc(ctx)
    session = _session(ctx)
    if not session:
        raise RuntimeError("微信尚未连接，无法操作朋友圈")
    ability = svc.moments_capabilities()
    if not ability.get("interaction_supported"):
        raise RuntimeError("当前微信版本未开放朋友圈点赞/评论（仅 4.1.15.56 已实测）")
    action = str(p.get("action") or "like")
    if action not in ("like", "comment"):
        raise ValueError("action 只能是 like 或 comment")
    payload = {
        "request_id": uuid.uuid4().hex,
        "session_id": session,
        "sns_id": str(p["snsId"]),
        "action": action,
        "content": str(p.get("content") or "")[:1023] if action == "comment" else "",
    }
    return svc.interact_moments(payload, expected_session=session)


def h_read_moments(ctx, p):
    svc = _svc(ctx)
    session = _session(ctx)
    if not session:
        raise RuntimeError("微信尚未连接，无法读取朋友圈")
    limit = min(max(int(p.get("limit") or 20), 1), 50)
    result = svc.read_moments(limit, expected_session=session)
    sns_id = str(p.get("snsId") or "")
    if sns_id and isinstance(result, dict):
        result = dict(result)
        result["rows"] = [r for r in result.get("rows", [])
                          if str(r.get("sns_id")) == sns_id]
    return result


def h_labels(ctx, p):
    labels = {}
    for row in _contact_rows(ctx):
        raw = row.get("labels") if isinstance(row, dict) else None
        values = raw if isinstance(raw, list) else _as_list(raw)
        for value in values:
            key = str(value)
            labels.setdefault(key, 0)
            labels[key] += 1
    return {"count": len(labels),
            "labels": [{"name": k, "members": v} for k, v in sorted(labels.items())],
            "note": "由本地通讯录缓存的标签关联聚合，只读"}


def h_check_online(ctx, p):
    svc = _svc(ctx)
    return {"connected": bool(svc.connected),
            "session_id": _session(ctx),
            "capabilities": sorted(str(c) for c in getattr(ctx, "capabilities", ()))}


def h_profile(ctx, p):
    svc = _svc(ctx)
    backend = getattr(svc, "_backend", None)
    raw = getattr(backend, "raw", None)
    return {
        "self_wxid": str(getattr(raw, "self_wxid", "") or ""),
        "wechat_version": str(getattr(raw, "wechat_version", "") or ""),
        "connected": bool(svc.connected),
        "session_id": _session(ctx),
        "note": "来自本机微信进程，非服务端资料",
    }


HANDLERS = {
    "send_text": h_send_text, "send_at_text": h_send_at_text,
    "send_image": h_send_image, "send_file": h_send_file,
    "send_voice": h_send_voice, "send_emoticon": h_send_emoticon,
    "export_emoticons": h_export_emoticons, "local_media": h_local_media,
    "contacts": h_contacts, "contact_info": h_contact_info,
    "search_contacts": h_search_contacts, "accept_friend": h_accept_friend,
    "friend_requests": h_friend_requests, "create_group": h_create_group,
    "invite_group_members": h_invite_group_members, "group_members": h_group_members,
    "group_info": h_group_info, "group_member_name": h_group_member_name,
    "group_member_role": h_group_member_role,
    "moments_capabilities": h_moments_capabilities,
    "publish_moments": h_publish_moments, "interact_moments": h_interact_moments,
    "read_moments": h_read_moments, "labels": h_labels,
    "check_online": h_check_online, "profile": h_profile,
}


class Unsupported(RuntimeError):
    pass


def dispatch(ctx, action_key: str, params: dict):
    action = caps.ACTIONS.get(action_key)
    if action is None:
        raise Unsupported("未知动作 %s；用 local_wechat.bat list 查看可用动作" % action_key)
    if not action.available:
        raise Unsupported("动作 %s 本地底层不支持" % action_key)
    handler = HANDLERS.get(action.handler)
    if handler is None:
        raise Unsupported("动作 %s 的 handler(%s) 未实现" % (action_key, action.handler))
    missing = [name for name in action.params if not params.get(name)]
    if missing:
        raise ValueError("缺少参数：%s" % ", ".join(missing))
    return handler(ctx, params)


class Plugin:
    """本地能力中心执行端。"""

    def on_load(self, ctx):
        self.ctx = ctx
        self.queue = queue_mod.TaskQueue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="local-api-worker")
        self._thread.start()
        ctx.logger.info("本地能力中心已启动，队列：%s，可用动作 %d 个"
                        % (self.queue.path, len(caps.ACTIONS)))

    def _worker(self):
        while not self._stop.is_set():
            try:
                task = self.queue.claim()
            except Exception as exc:
                self.ctx.logger.warning("领取本地任务失败：%s", exc)
                task = None
            if task is None:
                self._stop.wait(0.3)
                continue
            try:
                result = dispatch(self.ctx, task["action"], task["params"])
                self.queue.complete(task["id"], result=result)
            except Exception as exc:
                self.queue.complete(task["id"], error="%s: %s"
                                    % (type(exc).__name__, exc))

    def on_message(self, message, ctx):
        # 本插件只处理本地任务队列，不参与聊天消息分发。
        return None

    def on_unload(self, ctx):
        self._stop.set()
        if getattr(self, "_thread", None) and self._thread.is_alive():
            self._thread.join(timeout=2)
        ctx.logger.info("本地能力中心已停止")
