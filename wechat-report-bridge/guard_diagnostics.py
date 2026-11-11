"""微信掉线取证：结构化事件日志 + 掉线瞬间全量快照。

背景（2026-11-09 排查）：连续三天凌晨掉线时，`wechat_guard.log` 只有一句
「账号对象链未就绪（内存页可能已被裁剪）」，无法回答「到底是微信被踢下线、
客户端升级、内存换页，还是进程/网络问题」，只能事后翻 500KB bot.log 反推。

本模块解决的就是这个问题：
  1. 状态翻转那一秒，抓取进程 / Weixin.dll 指纹 / 窗口 / 内存 / 网络 /
     服务端口 / 系统事件 / bot.log 上下文，落成 diagnostics/incidents/INC-*.json；
  2. 所有探测与动作写结构化事件流 diagnostics/events/events-YYYY-MM-DD.jsonl；
  3. 给出统一的根因分类 reason + 辅助信号 signals，供日志、health.json、
     飞书告警和 diagnose_offline.py 复用，避免再出现「一个假设走到底」的误判。

设计约束：
  * 零第三方依赖（psutil 可用时增强，缺失自动降级），不修改 helper 源码；
  * 快照采集失败一律降级为 note，绝不影响守护主循环；
  * 只在「掉线瞬间 / 手动 --snapshot」采集重信息，常规探测不产生额外开销；
  * 默认不向外网发请求；需要记录公网出口 IP 时显式设置环境变量
    WX_BRIDGE_PUBLIC_IP=1（用于验证「IP 变化触发微信安全校验」这一诱因）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections import deque
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
HELPER = WORKSPACE / "vendor" / "helper-main"
BOT_LOG = HELPER / "logs" / "bot.log"
GUARD_LOG = ROOT / "wechat_guard.log"
DIAG_DIR = ROOT / "diagnostics"
INCIDENT_DIR = DIAG_DIR / "incidents"
EVENT_DIR = DIAG_DIR / "events"

# 让 CoreProfile 指纹可被导入（守护脚本也会把 HELPER 加进 sys.path）。
for _entry in (str(HELPER), str(HELPER / "native")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

try:
    import psutil                                  # 可选增强：进程/网卡信息更准
except ImportError:                                # pragma: no cover - 便携环境可能没有
    psutil = None

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 与「掉线」可能相关的系统事件；Kernel-Power 566 之类的常规信息事件不算。
RELEVANT_SYSTEM_IDS = {1, 16, 41, 42, 107, 109, 1014, 1074, 1076,
                       4004, 5002, 6005, 6006, 6008, 6009, 20001}
RELEVANT_SYSTEM_PROVIDERS = ("kernel-power", "kernel-general", "tcpip", "wlan",
                             "netwtw", "user32", "dhcp", "taskscheduler")

# 主分类：只描述「技术上发生了什么」，不臆测服务端动机。
REASON_TEXT = {
    "ok": "在线，账号对象链可读",
    "awaiting_login": "微信进程与 DLL 正常，但读不到绑定账号：客户端多半回到登录界面（需人工重新登录）",
    "process_gone": "未找到微信主进程：进程退出或尚未启动",
    "core_dll_missing": "微信进程里没有已加载的 Weixin.dll：可能正在启动/更新，或换了版本",
    "version_mismatch": "Weixin.dll 指纹与 native_profiles 不匹配：客户端很可能刚升级，硬编码偏移已失效",
    "probe_error": "探测代码自身异常（导入失败 / 内存读取报错）",
    "memory_pressure": "可用内存过低且账号链短时不可读：疑似工作集换页（11-09 排查确认它不是连续掉线主因）",
    "unknown": "信息不足，无法分类，请查看事件快照",
}

REASON_ACTION = {
    "ok": "当前在线，无需处置；这份快照可作为正常状态的比对基线。",
    "awaiting_login": "查看微信窗口是否提示「为了您的账号安全，请重新登陆」，用手机确认后重新登录；窗口唤醒对此无效。",
    "process_gone": "确认微信是否被关闭或重启，启动后等待桥接自动重连。",
    "core_dll_missing": "等待微信完全启动；若持续存在，检查客户端是否被更新。",
    "version_mismatch": "先更新 vendor/helper-main 的内存 profile，再重启桥接；不要按掉线处理。",
    "probe_error": "查看事件快照里的异常堆栈，确认 helper 依赖是否完整。",
    "memory_pressure": "关闭占用内存的常驻程序；仅此一项通常不会让微信强制重新登录。",
    "unknown": "运行 python diagnose_offline.py 查看最新事件快照。",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
    EVENT_DIR.mkdir(parents=True, exist_ok=True)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 结构化事件流
# --------------------------------------------------------------------------
def append_event(event: str, *, level: str = "INFO", guard_pid: int | None = None,
                 **fields) -> dict:
    """追加一行 JSON 事件。任何写入失败都只返回记录，不抛给调用方。"""
    record = {"ts": now_iso(), "level": level, "event": event,
              "guard_pid": guard_pid if guard_pid is not None else os.getpid(), **fields}
    try:
        ensure_dirs()
        path = EVENT_DIR / ("events-%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    return record


def event_tail(limit: int = 40) -> list[dict]:
    """读取最近若干条事件（跨天文件），按时间顺序返回。"""
    try:
        ensure_dirs()
        rows: list[dict] = []
        for path in sorted(EVENT_DIR.glob("events-*.jsonl")):
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        return rows[-limit:]
    except OSError:
        return []


def prune(max_event_days: int = 14, max_incidents: int = 40) -> None:
    """只清理本模块自己生成的过期文件，保留最近证据，避免无限膨胀。"""
    try:
        ensure_dirs()
        cutoff = (datetime.now() - timedelta(days=max_event_days)).strftime("%Y-%m-%d")
        for path in EVENT_DIR.glob("events-*.jsonl"):
            if path.stem.replace("events-", "") < cutoff:
                path.unlink(missing_ok=True)
        files = sorted(INCIDENT_DIR.glob("INC-*.json"))
        for path in files[:-max_incidents] if len(files) > max_incidents else []:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def read_tail(path: Path, lines: int = 60) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\r\n") for line in deque(fh, maxlen=lines)]
    except OSError:
        return []


# --------------------------------------------------------------------------
# 单块取证能力（每一块都必须能独立失败而不影响其它块）
# --------------------------------------------------------------------------
def memory_status() -> dict:
    """系统物理内存；纯 ctypes，不依赖 psutil。"""
    if os.name != "nt":
        return {"available_mb": None, "note": "非 Windows 环境"}
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return {"total_mb": round(stat.ullTotalPhys / 1048576, 1),
                    "available_mb": round(stat.ullAvailPhys / 1048576, 1),
                    "load_percent": stat.dwMemoryLoad}
    except (AttributeError, OSError):
        pass
    return {"available_mb": None, "note": "读取失败"}


def wechat_processes() -> list[dict]:
    """所有微信相关进程；用于判断「进程是否重启过 / 是否拉起了登录组件」。"""
    rows: list[dict] = []
    if psutil is None:
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, errors="replace", timeout=10,
                                 creationflags=_CREATE_NO_WINDOW).stdout or ""
            for line in out.splitlines():
                if "weixin" in line.lower() or "wechat" in line.lower():
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 2 and parts[1].isdigit():
                        rows.append({"pid": int(parts[1]), "name": parts[0]})
        except (OSError, subprocess.SubprocessError):
            pass
        return rows
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time", "memory_info"]):
        try:
            name = proc.info.get("name") or ""
            if not ("weixin" in name.lower() or "wechat" in name.lower()):
                continue
            info = proc.info
            started = info.get("create_time")
            rows.append({
                "pid": info["pid"], "name": name, "exe": info.get("exe"),
                "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds")
                              if started else None,
                "working_set_mb": round(info["memory_info"].rss / 1048576, 1)
                                  if info.get("memory_info") else None,
            })
        except (psutil.Error, OSError, ValueError):
            continue
    return sorted(rows, key=lambda r: r["pid"])


def core_module_info(procs: list[dict]) -> dict:
    """Weixin.dll 指纹与 native_profiles 比对：把「客户端升级」从掉线里摘出来。"""
    result: dict = {"found": False, "profile_match": None,
                    "known_profiles": [], "note": ""}
    try:
        from wxbot_framework.native_profiles import CORE_PROFILES
        result["known_profiles"] = [
            {"version": p.version, "size": p.size} for p in CORE_PROFILES]
    except Exception as exc:                        # helper 不可用时不能拖垮取证
        result["note"] = "无法读取 native_profiles: %s: %s" % (type(exc).__name__, exc)
        return result

    dll_path = None
    if psutil is not None:
        for row in procs:
            try:
                for mmap in psutil.Process(row["pid"]).memory_maps():
                    if Path(mmap.path).name.lower() == "weixin.dll":
                        dll_path = Path(mmap.path)
                        result["weixin_pid"] = row["pid"]
                        break
            except (psutil.Error, OSError):
                continue
            if dll_path:
                break
    if dll_path is None:                            # 降级：在微信安装目录的版本子目录里找
        for row in procs:
            exe = row.get("exe")
            if not exe:
                continue
            for candidate in sorted(Path(exe).parent.glob("*/Weixin.dll")):
                if candidate.is_file():
                    dll_path = candidate
                    break
            if dll_path:
                break
    if dll_path is None or not dll_path.is_file():
        result["note"] = "未定位到 Weixin.dll（进程可能正在启动）"
        return result

    stat = dll_path.stat()
    result.update({"found": True, "path": str(dll_path), "size": stat.st_size,
                   "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                   "version_hint": dll_path.parent.name})
    matches = [p for p in CORE_PROFILES if p.size == stat.st_size]
    if matches:
        # 大小一致时再算 SHA256 确认（200MB 约 1~2s，只在掉线/手动快照时做）
        digest = hashlib.sha256()
        try:
            with dll_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            result["sha256"] = digest.hexdigest()
            result["profile_match"] = any(p.sha256 == result["sha256"] for p in matches)
            result["matched_versions"] = [p.version for p in matches
                                          if p.sha256 == result["sha256"]]
        except OSError as exc:
            result["profile_match"] = True          # 大小已匹配，哈希读失败不判失配
            result["note"] = "SHA256 读取失败，仅按大小匹配：%s" % exc
    else:
        result["profile_match"] = False
        result["note"] = "DLL 大小不在已验证指纹表中，客户端可能已升级"
    return result


def wechat_windows(procs: list[dict]) -> dict:
    """枚举微信进程窗口；登录窗口/二维码窗口是「被踢下线」的直接旁证。"""
    if os.name != "nt":
        return {"windows": [], "login_window_suspected": False}
    pids = {int(r["pid"]) for r in procs}
    found: list[dict] = []
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def cb(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(128)
        owner = wintypes.DWORD()
        user32.GetWindowTextW(hwnd, title, 256)
        user32.GetClassNameW(hwnd, cls, 128)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids:
            found.append({"hwnd": int(hwnd), "pid": owner.value, "title": title.value,
                          "class": cls.value, "visible": bool(user32.IsWindowVisible(hwnd))})
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except (OSError, AttributeError):
        pass
    keywords = ("登录", "登陆", "扫码", "二维码", "login", "qrcode")
    suspected = any(any(k in (w["title"] or "").lower() for k in keywords)
                    for w in found)
    return {"windows": found[:30], "login_window_suspected": suspected,
            "note": "窗口标题含登录/扫码关键词时，基本可确认客户端已回到登录界面"}


def network_state() -> dict:
    """本机网卡、端口、frpc 进程；出口 IP 变化是微信安全校验的候选诱因。"""
    out: dict = {"local_interfaces": [], "ports": {}, "frpc_running": False,
                 "public_ip": None, "public_ip_note": "默认不采集（避免外部请求）"}
    if psutil is not None:
        try:
            for name, addrs in psutil.net_if_addrs().items():
                ipv4 = [a.address for a in addrs if a.family == socket.AF_INET]
                if ipv4:
                    out["local_interfaces"].append({"name": name, "ipv4": ipv4})
        except (psutil.Error, OSError):
            pass
        try:
            out["frpc_running"] = any(
                (p.info.get("name") or "").lower() == "frpc.exe"
                for p in psutil.process_iter(["name"]))
        except (psutil.Error, OSError):
            pass
    for port in (8080, 8088):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                out["ports"][str(port)] = "可连接"
        except OSError as exc:
            out["ports"][str(port)] = "不可连接（%s）" % type(exc).__name__
    if os.environ.get("WX_BRIDGE_PUBLIC_IP") == "1":
        try:
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(urllib.request.Request(
                    "https://api.ipify.org", headers={"User-Agent": "WxBridge-Diag/1.0"}),
                    timeout=3) as resp:
                out["public_ip"] = resp.read(64).decode("ascii", "replace").strip()
                out["public_ip_note"] = "显式开启采集"
        except Exception as exc:
            out["public_ip_note"] = "采集失败：%s: %s" % (type(exc).__name__, exc)
    return out


def recent_system_events(minutes: int = 15) -> dict:
    """最近系统事件：排除休眠/重启/维护/网络断开等「其他原因」。"""
    if os.name != "nt":
        return {"events": [], "note": "非 Windows 环境"}
    start = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    cmd = ("$ErrorActionPreference='SilentlyContinue'; "
           "Get-WinEvent -FilterHashtable @{LogName='System';StartTime='%s'} "
           "-MaxEvents 40 | Select-Object -First 40 "
           "TimeCreated,Id,ProviderName,LevelDisplayName | ConvertTo-Json -Compress" % start)
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd],
                              capture_output=True, text=True, errors="replace", timeout=15,
                              creationflags=_CREATE_NO_WINDOW)
        raw = (proc.stdout or "").strip()
        if not raw:
            return {"events": [], "note": "最近 %d 分钟无系统事件（可排除休眠/重启/维护）" % minutes}
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        rows: list[dict] = []
        relevant: list[dict] = []
        for item in data[:40]:
            if not isinstance(item, dict):
                continue
            created = str(item.get("TimeCreated") or "")
            # PowerShell ConvertTo-Json 把 DateTime 序列化成 /Date(epoch_ms)/，转成可读时间
            match = re.match(r"/Date\((\d+)\)", created)
            if match:
                created = datetime.fromtimestamp(
                    int(match.group(1)) / 1000).isoformat(timespec="seconds")
            provider = str(item.get("ProviderName") or "")
            row = {"time": created, "id": item.get("Id"), "provider": provider}
            rows.append(row)
            if (row["id"] in RELEVANT_SYSTEM_IDS and
                    any(key in provider.lower() for key in RELEVANT_SYSTEM_PROVIDERS)):
                relevant.append(row)
        return {"events": rows, "relevant_events": relevant,
                "window_minutes": minutes,
                "regular_count": len(rows) - len(relevant),
                "note": ("最近 %d 分钟共 %d 条系统事件，其中需关注 %d 条"
                         % (minutes, len(rows), len(relevant)))}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"events": [], "relevant_events": [],
                "note": "系统事件读取失败：%s: %s" % (type(exc).__name__, exc)}


# --------------------------------------------------------------------------
# 分类与快照
# --------------------------------------------------------------------------
def classify(online: bool, detail: str, snapshot: dict) -> tuple[str, list[str], str]:
    """返回 (主分类, 辅助信号, 人话说明)。

    主分类只依据可验证的技术事实；「服务端为什么踢下线」属于推测，
    统一放进 signals / 诊断报告，不再像旧版那样把单一假设写进日志。
    """
    if online:
        return "ok", [], REASON_TEXT["ok"]
    text = detail or ""
    core = snapshot.get("wechat", {}).get("core_module") or {}
    procs = snapshot.get("wechat", {}).get("processes") or []
    mem = snapshot.get("memory") or {}
    net = snapshot.get("network") or {}
    signals: list[str] = []

    main_pids = snapshot.get("wechat", {}).get("main_pids") or []
    # 只剩 WeChatAppEx 小程序组件、没有 Weixin.exe 主进程，同样算「进程不在」
    if not procs or not main_pids:
        reason = "process_gone"
    elif "Weixin.dll" in text and "没有" in text:
        reason = "core_dll_missing"
    elif core.get("profile_match") is False:
        reason = "version_mismatch"
    elif text.startswith("探测异常"):
        reason = "probe_error"
    else:
        reason = "awaiting_login"

    avail = mem.get("available_mb")
    if isinstance(avail, (int, float)) and avail < 600:
        signals.append("low_memory")
    if snapshot.get("wechat", {}).get("windows", {}).get("login_window_suspected"):
        signals.append("login_window")
    if net.get("frpc_running") is False:
        signals.append("frpc_down")
    for port, state in (net.get("ports") or {}).items():
        if "不可连接" in str(state):
            signals.append("port_%s_down" % port)
    system = snapshot.get("system_events") or {}
    if system.get("relevant_events"):
        signals.append("relevant_system_events")
    if core.get("found") and core.get("mtime"):
        # DLL 文件在最近 24h 内被改动 = 客户端刚更新过
        try:
            mtime = datetime.fromisoformat(core["mtime"])
            if datetime.now() - mtime < timedelta(hours=24):
                signals.append("dll_recently_updated")
        except (ValueError, TypeError):
            pass
    proc_starts = [p.get("started_at") for p in procs if p.get("started_at")]
    if proc_starts:
        try:
            newest = max(datetime.fromisoformat(s) for s in proc_starts)
            if datetime.now() - newest < timedelta(minutes=10):
                # 主进程没换，但登录时会拉起 Weixin.exe / WeChatAppEx 子进程，
                # 这是「重新登录」的旁证，不是进程重启。
                signals.append("wechat_child_process_spawned")
        except (ValueError, TypeError):
            pass

    summary = REASON_TEXT.get(reason, REASON_TEXT["unknown"])
    if signals:
        summary += "；辅助信号：" + "、".join(signals)
    return reason, signals, summary


def build_snapshot(*, include_system: bool = True) -> dict:
    """采集一份完整现场快照（重操作，只在掉线瞬间或手动执行）。"""
    procs = wechat_processes()
    windows = wechat_windows(procs)
    return {
        "captured_at": now_iso(),
        "wechat": {
            "processes": procs,
            "main_pids": [p["pid"] for p in procs
                          if (p.get("name") or "").lower() == "weixin.exe"],
            "core_module": core_module_info(procs),
            "windows": windows,
        },
        "memory": memory_status(),
        "network": network_state(),
        "system_events": recent_system_events() if include_system else
                         {"events": [], "note": "本次快照跳过系统事件采集"},
        "logs": {
            "bot_log_tail": read_tail(BOT_LOG, 60),
            "guard_log_tail": read_tail(GUARD_LOG, 25),
            "bot_log_size": BOT_LOG.stat().st_size if BOT_LOG.is_file() else None,
        },
        "hint": "对照 2026-11-09 排查报告：连续三天凌晨掉线的主因是微信服务端使 PC 会话失效，"
                "典型特征是进程不重启、DLL 指纹不变、窗口回到登录界面、本机服务与 frp 全正常。",
    }


def quick_core_fingerprint() -> dict:
    """轻量 DLL 指纹：只比文件大小，不读 200MB 内容。

    每轮低频探测都调一次，用于提前发现「微信客户端升级导致偏移失配」，
    避免版本问题被误判成掉线。完整 SHA256 校验只在掉线取证时做。
    """
    out: dict = {"match": None, "size": None, "path": None, "note": ""}
    try:
        from wxbot_framework.native_profiles import CORE_PROFILES
    except Exception as exc:
        out["note"] = "无法读取 native_profiles: %s: %s" % (type(exc).__name__, exc)
        return out
    dll_path = None
    for row in wechat_processes():
        exe = row.get("exe")
        if not exe:
            continue
        for candidate in sorted(Path(exe).parent.glob("*/Weixin.dll")):
            if candidate.is_file():
                dll_path = candidate
                break
        if dll_path:
            break
    if dll_path is None:
        out["note"] = "未定位到 Weixin.dll，无法校验版本"
        return out
    try:
        size = dll_path.stat().st_size
    except OSError as exc:
        out["note"] = "读取 DLL 大小失败：%s" % exc
        return out
    matches = [p.version for p in CORE_PROFILES if p.size == size]
    out.update({"size": size, "path": str(dll_path), "known_versions": matches,
                "match": bool(matches)})
    if not matches:
        out["note"] = "DLL 大小不在已验证指纹表中：客户端很可能已升级"
    return out


def capture_incident(trigger: str, probe_result: dict, guard_state: dict,
                     *, include_system: bool = True) -> tuple[str, Path, dict]:
    """掉线瞬间取证：写 incidents/INC-*.json + 一条 incident 事件，返回快照。"""
    ensure_dirs()
    incident_id = "INC-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = build_snapshot(include_system=include_system)
    reason, signals, summary = classify(bool(probe_result.get("online")),
                                        str(probe_result.get("detail") or ""), snapshot)
    payload = {
        "incident_id": incident_id, "trigger": trigger, "created_at": now_iso(),
        "probe": probe_result, "classification": {
            "reason": reason, "signals": signals, "summary": summary,
            "suggested_action": REASON_ACTION.get(reason, REASON_ACTION["unknown"]),
            "reason_catalog": REASON_TEXT,
        },
        "guard_state_at_trigger": guard_state,
        "snapshot": snapshot,
    }
    path = INCIDENT_DIR / (incident_id + ".json")
    atomic_json(path, payload)
    append_event("incident", level="INFO" if probe_result.get("online") else "WARNING",
                 incident_id=incident_id,
                 trigger=trigger, reason=reason, signals=signals,
                 detail=probe_result.get("detail"), incident_file=str(path))
    prune()
    return incident_id, path, payload
