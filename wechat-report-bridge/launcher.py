"""统一启动器：让 .bat 不必处理中文路径，由 Python 负责读配置、设环境变量。

用法：
    python launcher.py both     默认：同时启动接收端 + helper 桥接，退出时一起关闭
    python launcher.py server   只启动本机接收端
    python launcher.py bridge   只启动 helper（接收端在别的机器上时用）
    python launcher.py admin    只启动本地 Web 管理台（127.0.0.1:8088，可加 --open）
    python launcher.py alert    只拉起飞书告警监控（无窗口、独立常驻）
    python launcher.py check    只做配置与插件语法检查，不连接微信

飞书告警现在是主项目的旁路组件：both / server / bridge / admin 启动时都会自动带起，
但以独立进程运行，主项目退出或崩溃后它仍存活，才能把故障推到飞书群。
临时关闭：设置环境变量 WXBRIDGE_NO_ALERT=1；
长期关闭：把 alert.local.json 的 auto_start_with_launcher 设为 false。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
PYTHON = WORKSPACE / "runtime" / "python313" / "python.exe"
PYTHONW = WORKSPACE / "runtime" / "python313" / "pythonw.exe"
HELPER = WORKSPACE / "vendor" / "helper-main"
SERVER = ROOT / "server" / "app.py"
ADMIN = ROOT / "admin" / "app.py"
ALERT = ROOT / "health_monitor.py"
ALERT_CONFIG = ROOT / "alert.local.json"
# 必须与 health_monitor.py::acquire_single_instance 中的名字保持一致
ALERT_MUTEX = "Local\\WxBridgeFeishuAlertMonitor"
ALERT_HEARTBEAT = ROOT / "alert_heartbeat.json"
ALERT_STATE = ROOT / "alert_state.json"
ALERT_LOG = ROOT / "alert.log"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
ALERT_CREATION_FLAGS = 0x00000008 | 0x00000200 | 0x01000000
ALERT_CREATION_FLAGS_FALLBACK = 0x00000008 | 0x00000200

# pythonw 无控制台时 sys.stdout / sys.stderr 为 None，print 会抛异常
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def build_env():
    local = json.loads((ROOT / "config.local.json").read_text(encoding="utf-8-sig"))
    env = os.environ.copy()
    # 便携 Python 目录里有 VC++ 运行库 DLL，桥接 DLL 加载时依赖它们
    env["PATH"] = str(PYTHON.parent) + os.pathsep + env.get("PATH", "")
    # embed 版的 sys.path 不含脚本目录，显式加入 helper 框架与接收端模块目录
    module_paths = [str(HELPER), str(ROOT / "server")]
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(module_paths + ([previous] if previous else []))
    env["BRIDGE_TOKEN"] = str(local["token"])
    env["BRIDGE_HOST"] = str(local.get("server_host", "127.0.0.1"))
    env["PORT"] = str(local.get("server_port", 8080))
    env["BRIDGE_DB"] = str(ROOT / "server" / "messages.sqlite3")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def port_in_use(host, port):
    target = "127.0.0.1" if host in ("", "0.0.0.0") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((target, port)) == 0


def alert_is_running() -> bool:
    """用命名互斥体判断告警监控是否已在跑，避免重复实例。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenMutexW(0x00100000, False, ALERT_MUTEX)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        # 探测失败时交回 health_monitor 自己的互斥体兜底，不影响主项目
        return False


def alert_heartbeat_age() -> float | None:
    """巡检进程心跳年龄（秒）。优先读独立心跳文件；旧版回退到状态文件与日志时间。"""
    try:
        data = json.loads(ALERT_HEARTBEAT.read_text(encoding="utf-8-sig"))
        return max(0.0, time.time() - float(data["ts"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    ages = []
    try:
        data = json.loads(ALERT_STATE.read_text(encoding="utf-8-sig"))
        text = str(data.get("updated") or "")
        if text:
            ages.append(time.time() - datetime.strptime(
                text, "%Y-%m-%d %H:%M:%S %z").timestamp())
    except (OSError, ValueError, TypeError):
        pass
    try:
        ages.append(time.time() - ALERT_LOG.stat().st_mtime)
    except OSError:
        pass
    return max(0.0, min(ages)) if ages else None


def kill_stale_alert_processes() -> int:
    """心跳陈旧时，只清理命令行确实指向本项目 health_monitor.py 的残留进程。"""
    try:
        import psutil
    except ImportError:
        return 0
    targets = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == os.getpid():
                continue
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            if "health_monitor.py" in cmd and "wechat-report-bridge" in cmd:
                targets.append(proc)
        except (psutil.Error, OSError):
            continue
    for proc in targets:
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            pass
    if targets:
        _, alive = psutil.wait_procs(targets, timeout=4)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.Error, OSError):
                pass
        time.sleep(0.5)
    return len(targets)


def ensure_alert_monitor(env: dict, max_age: float = 180) -> str:
    """按心跳确认巡检真的活着；失联则清残留并强制重启。

    返回 running / started / restarted / disabled / failed。
    11-10 凌晨的教训：只在启动时探测一次互斥体是不够的，进程后来死掉没人补。
    """
    age = alert_heartbeat_age()
    if age is not None and age <= max_age:
        return "running"
    if age is None:
        return "started" if start_alert_monitor(env) else "disabled"
    kill_stale_alert_processes()
    return "restarted" if start_alert_monitor(env, force=True) else "failed"


def start_alert_monitor(env: dict, force: bool = False) -> bool:
    """把飞书告警作为独立无窗口进程拉起；任何失败都只提示，绝不阻断主项目。

    force=True 用于心跳陈旧后的强制重启：此时互斥体可能仍被僵死进程持有。
    """
    if os.environ.get("WXBRIDGE_NO_ALERT") == "1":
        print("飞书告警：已被 WXBRIDGE_NO_ALERT=1 禁用，跳过。")
        return False
    if not ALERT.is_file() or not ALERT_CONFIG.is_file():
        print("飞书告警：未部署（health_monitor.py / alert.local.json 缺失），跳过。")
        return False
    try:
        cfg = json.loads(ALERT_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print("飞书告警：配置读取失败，跳过（%s）。" % exc)
        return False
    if not cfg.get("enabled", True) or not cfg.get("auto_start_with_launcher", True):
        print("飞书告警：配置中已关闭，跳过。")
        return False
    webhook = str(cfg.get("webhook") or "").strip()
    if not webhook.startswith("https://") or "CHANGE_ME" in webhook:
        print("飞书告警：webhook 未配置，跳过。")
        return False
    if not force and alert_is_running():
        print("飞书告警监控已在运行，直接复用。")
        return True

    interpreter = PYTHONW if PYTHONW.is_file() else PYTHON
    cmd = [str(interpreter), "-X", "utf8", str(ALERT), "--quiet"]
    kwargs = {
        "cwd": str(ROOT), "env": env,
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "close_fds": True,
    }
    # 11-09 19:42 统一入口接管后，旧会话/窗口关闭把同一作业对象里的 pythonw
    # 一起带走，凌晨 03:29 掉线时巡检已死且无人补拉。先尝试脱离作业对象，
    # 若作业不允许 breakaway（ERROR_ACCESS_DENIED）就退回普通 detached。
    flag_options = ([ALERT_CREATION_FLAGS, ALERT_CREATION_FLAGS_FALLBACK]
                    if os.name == "nt" else [None])
    proc, last_error = None, None
    for flags in flag_options:
        if flags is not None:
            kwargs["creationflags"] = flags
        try:
            proc = subprocess.Popen(cmd, **kwargs)
            break
        except OSError as exc:
            last_error = exc
    if proc is None:
        print("飞书告警：启动失败（不影响主项目）：%s" % last_error)
        return False
    print("飞书告警监控已随主项目启动（PID %d，独立常驻）。" % proc.pid)
    return True


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()
    if not PYTHON.is_file():
        raise SystemExit("未找到便携 Python：%s" % PYTHON)
    env = build_env()

    server_cmd = [str(PYTHON), str(SERVER)]
    # 额外参数透传给 main.py，例如 bridge --run-seconds 45
    bridge_cmd = [str(PYTHON), str(HELPER / "main.py")] + sys.argv[2:]
    if mode == "check":
        return subprocess.call(bridge_cmd + ["--check"], cwd=str(HELPER), env=env)
    if mode == "alert":
        # 供 alert_monitor_silent.bat 与开机自启项使用：只拉告警，不启动任何服务
        return 0 if start_alert_monitor(env) else 1

    # 告警是旁路；所有运行模式都自动带起，重复启动由命名互斥体挡住
    start_alert_monitor(env)

    if mode == "server":
        return subprocess.call(server_cmd, cwd=str(ROOT), env=env)
    if mode == "admin":
        return subprocess.call([str(PYTHON), str(ADMIN)] + sys.argv[2:],
                               cwd=str(ROOT), env=env)
    if mode == "bridge":
        return subprocess.call(bridge_cmd, cwd=str(HELPER), env=env)
    if mode != "both":
        raise SystemExit("未知模式：%s" % mode)

    host, port = env["BRIDGE_HOST"], int(env["PORT"])
    server = None
    if port_in_use(host, port):
        print("接收端已在 %s:%d 运行，直接复用。" % (host, port))
    else:
        print("启动本机接收端 …")
        server = subprocess.Popen(server_cmd, cwd=str(ROOT), env=env)
    try:
        print("启动 helper 桥接（控制台输入 quit 可正常退出）…")
        return subprocess.call(bridge_cmd, cwd=str(HELPER), env=env)
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            print("接收端已关闭。")


if __name__ == "__main__":
    raise SystemExit(main())
