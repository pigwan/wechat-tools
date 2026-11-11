"""本地能力中心离线自检：不联网、不连微信、不写项目数据库。

验证：
  1. 需求对照表与能力注册表完整性（134 项逐项有状态；每个动作都有 handler）
  2. 全部 handler 用 FakeContext 可正常分派执行
  3. 任务队列 submit / claim / complete / wait / 幂等
  4. 未知动作、缺参数、未实现动作的错误处理

运行： local_wechat.bat selftest
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from local_wechat import capabilities as caps  # noqa: E402
from local_wechat import queue as q  # noqa: E402
from local_wechat import plugin as local_plugin  # noqa: E402

_PASS = 0
_FAIL = 0


def check(label, condition, detail=""):
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print("  [OK]   %s" % label)
    else:
        _FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


class _Raw:
    self_wxid = "wxid_selftest"
    wechat_version = "4.1.15.56"


class _Backend:
    session_id = "a" * 32
    raw = _Raw()


class FakeService:
    def __init__(self):
        self.calls = []
        self.connected = True
        self._backend = _Backend()

    def _log(self, name, *args):
        self.calls.append((name, args))
        return {"ok": True, "call": name}

    def send_text(self, *a): return self._log("send_text", *a)
    def send_at_text(self, *a): return self._log("send_at_text", *a)
    def send_image(self, *a): return self._log("send_image", *a)
    def send_file(self, *a): return self._log("send_file", *a)
    def send_voice(self, *a): return self._log("send_voice", *a)
    def send_emoticon(self, *a): return self._log("send_emoticon", *a)
    def accept_friend(self, *a): return self._log("accept_friend", *a)
    def group_members(self, *a):
        self._log("group_members", *a)
        return {"members": [{"wxid": "wxid_a", "name": "群友甲"}]}
    def group_member_name(self, *a): return "群友甲"
    def group_member_role(self, *a): return 0
    def export_emoticons(self, **kw): return {"files": []}
    def contacts(self, **kw):
        self._log("contacts")
        return {"contacts": [
            {"wxid": "wxid_a", "name": "张三", "remark": "小张", "labels": ["同事", "重要"]},
            {"wxid": "wxid_b", "name": "李四", "labels": ["同学"]},
        ]}
    def friend_requests(self, **kw): return {"rows": []}
    def create_group(self, *a, **kw): return self._log("create_group", *a)
    def invite_group_members(self, *a, **kw): return self._log("invite_group_members", *a)
    def moments_capabilities(self, **kw):
        return {"supported": True, "inspect_supported": True,
                "interaction_supported": True, "publish_supported": True}
    def publish_moments(self, payload, *, expected_session):
        self._log("publish_moments", payload.get("content"), len(payload.get("images", [])))
        return {"status": "done", "sns_id": "12345"}
    def interact_moments(self, payload, *, expected_session):
        self._log("interact_moments", payload.get("action"))
        return {"status": "done"}
    def read_moments(self, limit, *, expected_session):
        self._log("read_moments", limit)
        return {"rows": [{"sns_id": "12345", "content": "测试"}],
                "loaded_count": 1, "truncated": False}


class FakeContext:
    def __init__(self, service, root):
        self._service = service
        self.capabilities = frozenset({"text.receive", "text.send"})
        self.project_root = str(root)
        self.logger = logging.getLogger("local_api_selftest")
        self.logger.addHandler(logging.NullHandler())

    def __getattr__(self, name):
        return getattr(self._service, name)


def _params_for(action, tmp_files):
    values = {
        "toWxid": "wxid_test", "content": "本地自检", "path": str(tmp_files["image"]),
        "room": "12345@chatroom", "ats": "wxid_a,wxid_b", "wxids": "wxid_a,wxid_b",
        "wxid": "wxid_a", "keyword": "张", "requestId": "req_selftest_1",
        "kind": "image", "limit": 5, "snsId": "12345", "action": "like",
        "privacy": "false", "images": [str(tmp_files["image"])],
        "convert": "true", "identifier": "x", "publisher": "",
    }
    return {name: values.get(name, "v") for name in action.params}


def test_catalog():
    print("\n1) 需求对照与能力注册表")
    report = caps.coverage()
    check("需求清单 134 项", report["total"] == 134, str(report["total"]))
    check("本地已实现项 > 0", report["covered"] > 0, str(report["covered"]))
    check("每项都有结论（已实现+未开放+不适用=总数）",
          report["covered"] + report["uncovered"] + report["not_applicable"]
          == report["total"])
    check("未开放项都有原因",
          all(i.get("reason") for i in report["uncovered_items"]))
    check("动作数 >= 40", len(caps.ACTIONS) >= 40, str(len(caps.ACTIONS)))
    bad = [a.key for a in caps.ACTIONS.values()
           if a.handler not in local_plugin.HANDLERS]
    check("每个动作的 handler 都已实现", not bad, str(bad))
    empty = [a.key for a in caps.ACTIONS.values() if not a.status or not a.module]
    check("动作字段完整", not empty, str(empty))
    return report


def test_handlers(tmp):
    print("\n2) 全部 handler 离线分派")
    service = FakeService()
    ctx = FakeContext(service, tmp)
    tmp_files = {"image": tmp / "sample.png"}
    tmp_files["image"].write_bytes(b"\x89PNG\r\n\x1a\n")

    failures = []
    for key, action in caps.ACTIONS.items():
        try:
            result = local_plugin.dispatch(ctx, key, _params_for(action, tmp_files))
            if result is None:
                failures.append("%s -> None" % key)
        except Exception as exc:
            failures.append("%s -> %s: %s" % (key, type(exc).__name__, exc))
    check("全部 %d 个动作可正常执行" % len(caps.ACTIONS),
          not failures, "; ".join(failures[:6]))

    # 关键行为细查
    labels = local_plugin.dispatch(ctx, "label.list", {})
    check("标签聚合出 3 个标签", labels.get("count") == 3, str(labels))
    search = local_plugin.dispatch(ctx, "contacts.search", {"keyword": "张"})
    check("本地通讯录搜索命中张三", search.get("count") == 1, str(search))
    online = local_plugin.dispatch(ctx, "account.checkOnline", {})
    check("在线检测返回 connected", online.get("connected") is True, str(online))
    media = local_plugin.dispatch(ctx, "message.downloadImage", {"kind": "image", "limit": 3})
    check("媒体检索返回结构化结果", "files" in media, str(media)[:120])

    try:
        local_plugin.dispatch(ctx, "not.exist", {})
        check("未知动作应报错", False)
    except Exception:
        check("未知动作应报错", True)
    try:
        local_plugin.dispatch(ctx, "message.postText", {"toWxid": "x"})
        check("缺参数应报错", False)
    except ValueError:
        check("缺参数应报错", True)


def test_queue(tmp):
    print("\n3) 本地任务队列")
    db = tmp / "tasks.sqlite3"
    os.environ["LOCAL_API_DB"] = str(db)
    queue = None
    try:
        queue = q.TaskQueue()
        tid = queue.submit("message.postText", {"toWxid": "x", "content": "hi"})
        check("提交任务返回 ID", tid > 0, str(tid))
        claimed = queue.claim()
        check("领取到任务", claimed is not None and claimed["id"] == tid, str(claimed))
        check("领取后参数正确解析", claimed and claimed["params"]["content"] == "hi")
        check("重复领取不会拿到同一条", queue.claim() is None)
        queue.complete(tid, result={"ok": True})
        row = queue.wait(tid, timeout=1)
        check("结果写回且状态为 done", row and row["status"] == 2, str(row))
        check("wait 返回结果内容", row and row["result"] == {"ok": True}, str(row))
        tid2 = queue.submit("message.postText", {"toWxid": "x", "content": "bad"})
        claimed2 = queue.claim()
        queue.complete(claimed2["id"], error="模拟失败")
        row2 = queue.get(tid2)
        check("失败状态与错误信息", row2 and row2["status"] == 3
              and row2["error"] == "模拟失败", str(row2))
        check("stats 统计正确", queue.stats()["total"] == 2, str(queue.stats()))
    finally:
        if queue is not None:
            queue.close()
        os.environ.pop("LOCAL_API_DB", None)


def run() -> int:
    print("=" * 74)
    print("本地能力中心离线自检（纯本机：不联网 / 不连微信 / 不写项目数据库）")
    print("=" * 74)
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            test_catalog()
            test_handlers(tmp)
            test_queue(tmp)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    print("\n" + "=" * 74)
    print("结果： %d 项通过， %d 项失败" % (_PASS, _FAIL))
    print("=" * 74)
    return 0 if _FAIL == 0 else 1
