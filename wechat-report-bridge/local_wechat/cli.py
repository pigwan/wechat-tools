"""本地能力中心命令行（零第三方依赖、纯本机）。

    local_wechat.bat list                    列出本机已实现的能力
    local_wechat.bat coverage                134 项需求逐项覆盖报告
    local_wechat.bat info message.postText   查看参数与实现说明
    local_wechat.bat call message.postText toWxid=xxx content=你好
    local_wechat.bat status                  队列状态
    local_wechat.bat enable                  安装并启用 helper 插件
    local_wechat.bat selftest                离线自检

call 会把动作写入本地 SQLite 队列，由 helper 框架内的 local_api 插件执行；
没有运行机器人时任务会留在队列里，启动后自动补做（与 send.py 同一机制）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from local_wechat import capabilities as caps  # noqa: E402
from local_wechat import queue as q  # noqa: E402


def _framework_root() -> Path:
    here = _ROOT
    for parent in here.parents:
        if (parent / "native" / "text_service.py").is_file():
            return parent
        candidate = parent / "vendor" / "helper-main"
        if (candidate / "native" / "text_service.py").is_file():
            return candidate
    return here.parent


def cmd_list(args):
    rows = caps.list_actions(args.module, args.keyword)
    print("本机已实现能力：%d 个%s" % (len(rows), "（筛选后）" if (args.module or args.keyword) else ""))
    print("-" * 104)
    print("%-38s %-10s %-10s %s" % ("动作", "状态", "模块", "名称"))
    print("-" * 104)
    for a in rows:
        print("%-38s %-10s %-10s %s" % (a.key, a.status, a.module, a.name))
    return 0


def cmd_coverage(args):
    report = caps.coverage()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("功能需求对照：共 %d 项，本地已实现 %d 项，底层未开放 %d 项，本地架构不适用 %d 项"
          % (report["total"], report["covered"], report["uncovered"],
             report["not_applicable"]))
    print("本机动作数：%d" % report["actions"])
    print("\n按模块：")
    print("%-16s %8s %8s %8s %8s" % ("模块", "需求数", "已实现", "未开放", "不适用"))
    for mod, stat in report["by_module"].items():
        print("%-16s %8d %8d %8d %8d" % (mod, stat["total"], stat["covered"],
                                         stat["unsupported"], stat["not_applicable"]))
    print("\n已实现（动作 → 需求）：")
    for item in report["covered_items"]:
        print("  %-36s ← %s" % (item["action"], item["name"]))
    if args.detail:
        print("\n底层未开放（含原因）：")
        for item in report["uncovered_items"]:
            print("  %-24s %s" % (item["name"], item["reason"]))
    else:
        print("\n（加 --detail 可查看每一项的未开放原因）")
    return 0


def cmd_info(args):
    a = caps.ACTIONS.get(args.key)
    if a is None:
        hits = [x.key for x in caps.list_actions(keyword=args.key)]
        print("未知动作：%s%s" % (args.key, "，可能是：" + ", ".join(hits[:8]) if hits else ""),
              file=sys.stderr)
        return 2
    print("%s  [%s / %s]" % (a.name, a.module, a.status))
    print("handler：%s" % a.handler)
    print("参数：%s" % (", ".join(a.params) or "无"))
    if a.aliases:
        print("对应需求：%s" % ", ".join(a.aliases))
    if a.note:
        print("说明：%s" % a.note)
    return 0


def _parse_value(raw):
    text = raw.strip()
    if text.startswith(("{", "[", '"')):
        try:
            return json.loads(text)
        except ValueError:
            pass
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    return text


def cmd_call(args):
    if args.key not in caps.ACTIONS:
        print("未知动作：%s" % args.key, file=sys.stderr)
        return 2
    params = {}
    for pair in args.params:
        if "=" not in pair:
            print("参数格式应为 key=value：%s" % pair, file=sys.stderr)
            return 2
        key, _, val = pair.partition("=")
        params[key.strip()] = _parse_value(val)
    queue = q.TaskQueue()
    task_id = queue.submit(args.key, params)
    print("已提交本地任务 #%d（%s），等待 helper 插件执行 ..." % (task_id, args.key))
    row = queue.wait(task_id, timeout=args.wait)
    if row is None or row["status"] not in (2, 3):
        print("超时未执行；任务仍在队列中，启动机器人后会自动补做（不会丢）。")
        return 2
    if row["status"] == 3:
        print("执行失败：%s" % row.get("error"), file=sys.stderr)
        return 1
    print(json.dumps(row.get("result"), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_status(args):
    queue = q.TaskQueue()
    print("队列文件：%s" % queue.path)
    print("状态统计：%s" % json.dumps(queue.stats(), ensure_ascii=False))
    print("\n最近任务：")
    for row in queue.recent(args.limit):
        print("  #%-5d %-12s %-34s %s" % (
            row["id"], q.STATUS_TEXT.get(row["status"], "?"), row["action"],
            (row["error"] or row["created_at"])[:60]))
    return 0


def cmd_enable(args):
    """安装插件副本并在 bot_config.json 中启用（会先备份配置）。"""
    framework = _framework_root()
    target = framework / "plugins" / "local_api"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "plugin.json"):
        shutil.copy2(_ROOT / "plugin" / name, target / name)
    print("插件副本已安装：%s" % target)

    cfg_path = framework / "bot_config.json"
    if not cfg_path.is_file():
        print("未找到 %s，请手动在 plugins 中加入 local_api" % cfg_path, file=sys.stderr)
        return 1
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    plugins = data.setdefault("plugins", {})
    if plugins.get("local_api", {}).get("enabled"):
        print("bot_config.json 中 local_api 已启用，无需修改。")
        return 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = cfg_path.with_name("bot_config.json.bak-localapi-" + stamp)
    shutil.copy2(cfg_path, backup)
    plugins["local_api"] = {"enabled": True}
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已备份配置：%s" % backup)
    print("已启用 local_api。重启机器人（或在黑窗口 reload）后生效。")
    return 0


def cmd_selftest(args):
    sys.path.insert(0, str(_ROOT))
    from local_wechat.tools import selftest
    return selftest.run()


def build_parser():
    p = argparse.ArgumentParser(prog="local_wechat",
                                description="本地能力中心（纯本机，不联网、不需要 Token）")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("list", help="列出本机已实现的能力")
    sp.add_argument("module", nargs="?", help="模块过滤")
    sp.add_argument("-k", "--keyword", help="关键字过滤")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("coverage", help="功能需求逐项覆盖报告")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("--detail", action="store_true", help="列出未开放原因")
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser("info", help="查看某动作的参数与说明")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("call", help="提交一个本地动作并等待结果")
    sp.add_argument("key")
    sp.add_argument("params", nargs="*", help="key=value")
    sp.add_argument("--wait", type=float, default=20.0, help="等待秒数，默认 20")
    sp.set_defaults(func=cmd_call)

    sp = sub.add_parser("status", help="任务队列状态")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_status)

    sub.add_parser("enable", help="安装并启用 helper 插件（备份配置后）").set_defaults(func=cmd_enable)
    sub.add_parser("selftest", help="离线自检").set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
