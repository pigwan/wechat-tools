"""安全切换到新版微信守护（由 restart_guard.bat 调用，不会自动执行）。

为什么需要：旧守护是常驻进程，直接覆盖 .py 文件不会让新代码生效；
但直接 taskkill 又有误杀风险，因此这里先校验目标进程的命令行确实包含
wechat_guard.py，再停止，最后用新的控制台窗口拉起 v2。

回滚：把 wechat_guard.py.bak 复制回 wechat_guard.py，再双击本脚本即可。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEALTH_FILE = ROOT / "admin" / "wechat_health.json"
SCRIPT = ROOT / "wechat_guard.py"
PYTHON = ROOT.parent / "runtime" / "python313" / "python.exe"
PYTHONW = ROOT.parent / "runtime" / "python313" / "pythonw.exe"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：无窗口常驻，不继承控制台的 Ctrl+C
DETACHED_FLAGS = 0x00000008 | 0x00000200
GUARD_ARGS = ["--interval", "60", "--probe-every", "300", "--nudge-minutes", "15"]

try:
    import psutil
except ImportError:
    psutil = None


def old_guard_pid() -> int | None:
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8-sig"))
        pid = int(data.get("guard_pid") or 0)
        return pid or None
    except (OSError, ValueError, TypeError):
        return None


def is_guard_process(pid: int) -> bool:
    """必须确认命令行指向 wechat_guard.py，否则拒绝停止（防误杀）。"""
    if psutil is not None:
        try:
            cmd = " ".join(psutil.Process(pid).cmdline()).lower()
            return "wechat_guard.py" in cmd
        except (psutil.Error, OSError):
            return False
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine" % pid],
            capture_output=True, text=True, errors="replace", timeout=10).stdout or ""
        return "wechat_guard.py" in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def stop_old(pid: int) -> bool:
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            return True
        except (psutil.Error, OSError) as exc:
            print("停止旧守护失败：%s" % exc)
            return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print("停止旧守护失败：%s" % exc)
        return False


def main() -> int:
    if not SCRIPT.is_file() or not PYTHON.is_file():
        print("缺少 wechat_guard.py 或便携版 Python，已中止。")
        return 1

    pid = old_guard_pid()
    if pid and psutil is not None and not psutil.pid_exists(pid):
        # 旧守护已经不在了（例如被会话回收）：不要因为命令行校验失败就拒绝启动。
        print("旧守护 PID=%d 已不存在，跳过停止，直接启动新实例。" % pid)
    elif pid:
        if not is_guard_process(pid):
            print("PID %d 看起来不是微信守护（命令行校验失败），为避免误杀已中止。" % pid)
            print("请手动关闭旧的 wechat_guard.bat 窗口后再运行本脚本。")
            return 1
        print("正在停止旧守护 PID=%d …" % pid)
        if not stop_old(pid):
            return 1
        time.sleep(1.5)
    else:
        print("未发现旧守护 PID，直接启动新实例。")

    interpreter = PYTHONW if PYTHONW.is_file() else PYTHON
    proc = subprocess.Popen(
        [str(interpreter), "-X", "utf8", str(SCRIPT)] + GUARD_ARGS,
        cwd=str(ROOT.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=DETACHED_FLAGS, close_fds=True)
    time.sleep(2.0)
    alive = proc.poll() is None
    print("新守护已启动：PID=%d，存活=%s" % (proc.pid, alive))
    print("验证：python wechat_guard.py --once")
    print("复盘：python diagnose_offline.py --list")
    print("日志：wechat_guard.log / diagnostics/events/")
    if not alive:
        print("启动失败。回滚：copy wechat_guard.py.bak wechat_guard.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
