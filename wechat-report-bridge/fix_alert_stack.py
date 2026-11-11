"""一键恢复「微信掉线 -> 飞书告警」链路（由 fix_alert_stack.bat 双击调用）。

背景：2026-11-10 03:29 微信掉线时飞书零告警。根因不是 webhook，而是
飞书巡检进程在 11-09 19:42:07 之后就不在了，且没有任何组件负责把它拉回来。
本脚本只补拉「微信守护」和「飞书巡检」两个进程，不触碰 8080 / 8088 /
helper / frpc 等正在运行的服务，也不会重启微信。

为什么必须由用户双击：从 AI 会话命令行启动的后台进程会在命令结束时被环境
回收；由桌面会话启动的进程才能长期常驻（现有统一控制台已连续跑 12 小时）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import launcher                                        # noqa: E402
import guard_switch                                    # noqa: E402
import psutil                                          # noqa: E402

HEALTH = ROOT / "admin" / "wechat_health.json"
HEARTBEAT = ROOT / "alert_heartbeat.json"


def _version_tuple(value) -> tuple:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except (ValueError, TypeError):
        return ()


def guard_status():
    """返回 (pid, version, 是否确实是本项目守护进程)。"""
    try:
        h = json.loads(HEALTH.read_text(encoding="utf-8-sig"))
        pid = int(h.get("guard_pid") or 0)
        version = h.get("guard_version")
        if not pid or not psutil.pid_exists(pid):
            return None, version, False
        cmd = " ".join(psutil.Process(pid).cmdline()).lower()
        if "wechat_guard.py" not in cmd:
            return None, version, False
        return pid, version, True
    except (OSError, ValueError, TypeError, psutil.Error):
        return None, None, False


def main() -> int:
    print("=" * 62)
    print("微信工具 · 飞书告警链路一键恢复")
    print("=" * 62)

    pid, version, alive = guard_status()
    if alive and _version_tuple(version) >= (2, 1):
        print("1) 微信守护已在运行：PID=%s 版本=v%s，跳过重启" % (pid, version))
    else:
        print("1) 微信守护不可用（PID=%s 版本=%s），正在启动 v2.1 …" % (pid, version))
        if guard_switch.main() != 0:
            print("   守护启动失败，请查看上方提示。")
            return 1
        time.sleep(5)
        pid, version, alive = guard_status()
        print("   守护状态：PID=%s 版本=%s 存活=%s" % (pid, version, alive))

    print("2) 检查并拉起飞书巡检 …")
    try:
        result = launcher.ensure_alert_monitor(launcher.build_env(), max_age=180)
        print("   结果：%s" % result)
    except Exception as exc:
        print("   失败：%s: %s" % (type(exc).__name__, exc))
        return 1

    time.sleep(4)
    print("3) 复核：")
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8-sig"))
        age = time.time() - float(hb["ts"])
        print("   巡检心跳：%.1f 秒前（PID %s）%s" %
              (age, hb.get("pid"), "正常" if age <= 180 else "仍异常"))
    except (OSError, ValueError, KeyError, TypeError):
        print("   巡检心跳：未生成（异常）")
    try:
        h = json.loads(HEALTH.read_text(encoding="utf-8-sig"))
        print("   守护健康：v%s online=%s last_check=%s" %
              (h.get("guard_version"), h.get("online"), h.get("last_check")))
    except (OSError, ValueError):
        pass
    print("   飞书群收到「飞书告警监控已启动」即表示通道恢复。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
