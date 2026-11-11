"""端到端自测：不需要微信、不需要注入，模拟 helper 的 ctx/message 跑完整链路。

运行（用任意 Python 3.8+）：

    python selftest.py

覆盖：群消息过滤、本地缓冲、批量上报、Bearer 鉴权、远端幂等去重、
      /报告 艾特回复、日报统计与 Markdown 导出。所有数据写入临时目录，结束即清理。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
import app as receiver  # noqa: E402

TOKEN = "selftest-" + "a" * 40
TMP = Path(tempfile.mkdtemp(prefix="report-bridge-test-"))
receiver.DB_PATH = str(TMP / "remote.sqlite3")
receiver.TOKEN = TOKEN
receiver.init_db()


def load_plugin():
    path = ROOT / "plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("report_bridge_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Plugin()


def make_message(text, chat="group_a@chatroom", sender="wxid_alice", *,
                 is_group=True, is_self=False, msg_type=1):
    return types.SimpleNamespace(
        type=msg_type, text=text, chat_id=chat, sender_id=sender,
        is_group=is_group, is_self=is_self, session_id="session-1", data={},
    )


class FakeCtx:
    def __init__(self, config, project_root):
        self.config = config
        self.project_root = str(project_root)
        self.members = None
        self.logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
        self.replies = []

    def reply(self, message, text):
        self.replies.append(("reply", text))

    def reply_at(self, message, text):
        self.replies.append(("reply_at", text))

    def send_text(self, chat_id, text):
        self.replies.append(("send_text", text))
        return True


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError("FAIL %s %s" % (label, detail))
    print("  OK  %s %s" % (label, detail))


def main():
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    server = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = "http://127.0.0.1:%d/api/messages" % port
    base = "http://127.0.0.1:%d" % port

    plugin = load_plugin()
    ctx = FakeCtx({
        "endpoint": endpoint,
        "token": TOKEN,
        "batch_size": 4,
        "flush_seconds": 30,
        "only_groups": True,
        "include_self": False,
        "group_allowlist": [],
        "report_keywords": ["/报告"],
        "report_top_n": 3,
        "http_timeout_seconds": 3,
    }, TMP / "helper_root")
    plugin.on_load(ctx)

    print("\n[1] 模拟消息分发与本地缓冲")
    group_texts = [
        "早上好，今天同步一下进度", "昨日结论已经整理到文档", "待办：补齐单元测试",
        "上线时间定在周五", "这个 bug 复现了", "报告我先发群里", "问题定位到缓存",
        "结论是不需要改接口", "待办：更新部署说明", "收到，我这边跟进",
        "关键词命中测试 上线", "最后一条群消息",
    ]
    for index, text in enumerate(group_texts):
        chat = ("group_a@chatroom", "group_b@chatroom")[index % 2]
        sender = ("wxid_alice", "wxid_bob", "wxid_carol")[index % 3]
        plugin.on_message(make_message(text, chat, sender), ctx)
    plugin.on_message(make_message("私聊不该被采集", chat="wxid_friend", is_group=False), ctx)
    plugin.on_message(make_message("自己发的也跳过", is_self=True), ctx)
    plugin.on_message(make_message("图片消息", msg_type=3), ctx)
    plugin.on_message(make_message("/报告"), ctx)
    plugin.on_unload(ctx)  # 卸载时把剩余缓冲冲出去并关闭本地库

    local_db = TMP / "helper_root" / ".runtime" / "report_buffer.sqlite3"
    conn = sqlite3.connect(local_db)
    local_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    unsynced = conn.execute("SELECT COUNT(*) FROM messages WHERE synced = 0").fetchone()[0]
    conn.close()
    check("本地只落库 13 条群文本（12 条 + /报告）", local_count == 13, "实际 %d" % local_count)
    check("全部已上报（synced=0 剩余 0）", unsynced == 0, "剩余 %d" % unsynced)
    check("/报告 触发真实艾特回复",
          any(kind == "reply_at" and "群消息简报" in text for kind, text in ctx.replies))

    print("\n[2] 接收端鉴权与幂等落库")
    def post(url, payload, token=TOKEN):
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": "Bearer " + token}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    conn = sqlite3.connect(receiver.DB_PATH)
    remote_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    check("远端收到 13 条", remote_count == 13, "实际 %d" % remote_count)

    duplicate = {"messages": [{
        "msg_key": "manual-duplicate-key", "chat_id": "group_a@chatroom",
        "sender_id": "wxid_alice", "sender_name": "wxid_alice", "is_group": True,
        "text": "重复投递测试", "received_at": date.today().isoformat() + "T10:00:00+08:00",
    }]}
    _, first = post(endpoint, duplicate)
    _, second = post(endpoint, duplicate)
    check("相同 msg_key 第二次被去重", first["accepted"] == 1 and second["duplicates"] == 1,
          "first=%s second=%s" % (first, second))

    try:
        post(endpoint, {"messages": []}, token="wrong-token")
        check("错误 token 被拒绝", False)
    except urllib.error.HTTPError as error:
        check("错误 token 被拒绝", error.code == 401)

    print("\n[3] 日报接口与 Markdown 导出")
    def get(path, token=TOKEN):
        req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return resp.read().decode("utf-8")

    stats = json.loads(get("/api/report?day=" + date.today().isoformat()))
    check("日报统计消息数 >= 13", stats["total"] >= 13, "total=%d" % stats["total"])
    check("日报统计出 3 位发言者", stats["participants"] == 3,
          "participants=%d" % stats["participants"])
    markdown = get("/api/report?day=" + date.today().isoformat() + "&format=md")
    check("Markdown 日报含标题与活跃成员",
          "# 群消息日报" in markdown and "## 活跃成员" in markdown)
    print("\n----- 日报预览（前 16 行）-----")
    print("\n".join(markdown.splitlines()[:16]))
    print("------------------------------\n")

    server.shutdown()
    server.server_close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("端到端自测全部通过，临时数据已清理。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        shutil.rmtree(TMP, ignore_errors=True)
        raise SystemExit(1)
