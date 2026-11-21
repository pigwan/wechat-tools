"""微信工具全链路故障监控 -> 飞书告警（独立进程，纯标准库）。

不改动现有任何运行链路：只读取已有的状态文件、日志、数据库并探测端口/进程，
因此守护、helper、接收端、管理台、frpc 都不需要重启。

覆盖的故障：
  微信类  微信连接断开（账号对象链失效）/ 恢复、wechat_guard 守护进程失联
  服务类  8080 接收端、8088 管理台、frpc 隧道（可选公网侧探测）
  采集类  helper 插件 worker 心跳消失、bot.log 长时间无更新、消息库采集停滞、
          群名头像同步失败、运行日志出现 ERROR/Traceback 等异常关键字
  资源类  可用物理内存过低、磁盘剩余空间过低

告警语义：
  每个故障是一个「事件」，首次发生立刻推，未恢复按 10/30/60/120/240 分钟递增提醒，
  恢复时推一条绿色通知；同一故障不会每 60 秒刷屏，也不会掉线 12 小时无人知晓。
  除微信掉线即时告警外，其余故障要连续 2 轮巡检都异常才确认，过滤心跳抖动等瞬时误报。
  夜间安静导致的「无新消息」不算故障（active_hours 之外的时段跳过该类判定）。

用法：
    python health_monitor.py              # 常驻监控（默认 60s 一轮）
    python health_monitor.py --once       # 只体检一次，打印 JSON，不发任何消息
    python health_monitor.py --test       # 发一条飞书测试卡片
    python health_monitor.py --interval 30 --quiet
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# 便携版 Python（embed）默认不把脚本目录放进 sys.path，显式补上以便同目录导入。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from feishu_notifier import Notifier, get_logger, human_duration, ROOT  # noqa: E402

BRIDGE = ROOT
WORKSPACE = BRIDGE.parent
HELPER = WORKSPACE / "vendor" / "helper-main"
ADMIN = BRIDGE / "admin"
SERVER = BRIDGE / "server"

HEALTH_FILE = ADMIN / "wechat_health.json"
SYNC_STATUS_FILE = ADMIN / "sync_status.json"
BOT_LOG = HELPER / "logs" / "bot.log"
GUARD_LOG = BRIDGE / "wechat_guard.log"
WORKER_HEARTBEAT = HELPER / ".runtime" / "outbox_worker.json"
MESSAGES_DB = SERVER / "messages.sqlite3"

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
log = get_logger("Monitor")
_MUTEX_HANDLE = None  # 必须保持引用，否则互斥体会被释放
# 本进程常以 pythonw + DETACHED_PROCESS 无窗口运行。Windows 下这种父进程起子进程时，
# 系统会临时新建一个控制台窗口——每轮巡检的 tasklist 就会在桌面闪一个黑框。
# 显式给子进程 CREATE_NO_WINDOW，任务照跑，窗口不再闪（父进程自己的窗口不受影响）。
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# 独立心跳文件：只由巡检进程写。不能拿 alert_state.json 的 updated 判断存活，
# 因为守护直报也会刷新它，会把「巡检已死」误判成「巡检还活着」。
HEARTBEAT_FILE = ROOT / "alert_heartbeat.json"


def write_heartbeat() -> None:
    """每轮巡检刷新一次，供 launcher / 守护判断本进程是否真的还活着。"""
    try:
        tmp = HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "pid": os.getpid(),
                                   "at": stamp()}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HEARTBEAT_FILE)
    except OSError:
        pass


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def to_epoch(value) -> float | None:
    """兼容 ISO8601 / 'YYYY-MM-DD HH:MM:SS' / unix 时间戳三种写法。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return dt.astimezone().timestamp() if dt.tzinfo is None else dt.timestamp()
    except (ValueError, TypeError):
        return None


def age_of(ts) -> float | None:
    """以「读到该时间戳的当下」计算年龄，并把负值夹到 0。

    不能沿用 collect() 开头取的 now：中间要读健康文件、探测 8080/8088、
    调 tasklist，耗时几百毫秒到一秒多；而 worker 心跳每 2 秒刷新一次，
    于是 now - hb.ts 经常是负数，被 `0 <= age` 判成「心跳不存在」，
    造成 2026-11-09 反复出现 critical/recovered 刷屏（实为误报）。
    系统时钟被 NTP 微调时时间戳也可能略超前，夹到 0 即可。
    """
    if ts is None:
        return None
    try:
        return max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return None


def age_text(seconds: float | None) -> str:
    return human_duration(seconds) if seconds is not None else "未知"


def http_get(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "WxBridge-Alert/1.0"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.status, resp.read(2048).decode("utf-8", "replace")


def tcp_ok(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "%s:%d 可连接" % (host, port)
    except OSError as exc:
        return False, "%s:%d 连接失败（%s）" % (host, port, type(exc).__name__)


def process_running(image: str) -> bool:
    """用 tasklist 判断进程是否存在；中文系统输出乱码也不影响 ASCII 进程名匹配。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % image, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=_CREATE_NO_WINDOW).stdout or ""
        return image.lower() in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def memory_free_mb() -> float | None:
    if os.name != "nt":
        return None

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullAvailPhys / 1024 / 1024
    except (AttributeError, OSError):
        pass
    return None


def disk_free_mb() -> float:
    try:
        return shutil.disk_usage(str(WORKSPACE)).free / 1024 / 1024
    except OSError:
        return -1


def in_active_hours(cfg: dict) -> bool:
    hours = cfg.get("active_hours") or [8, 23]
    try:
        start, end = int(hours[0]), int(hours[1])
    except (ValueError, IndexError, TypeError):
        return True
    hour = datetime.now().hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


# --------------------------------------------------------------------------
# 各项体检：统一返回 {"ok": bool, "skipped": bool, "detail": str, ...}
# --------------------------------------------------------------------------
def collect(cfg: dict) -> dict:
    active = in_active_hours(cfg)
    out = {"time": stamp(), "active_hours": active}
    # 注意：各项年龄一律用 age_of() 在「实际读取时」现算，不要在函数开头缓存 now。

    # 1) 微信守护状态文件（掉线判定的唯一数据源）
    health = read_json(HEALTH_FILE)
    last_check = to_epoch(health.get("last_check"))
    guard_age = age_of(last_check)
    stale_limit = float(cfg.get("guard_stale_seconds", 180))
    fresh = guard_age is not None and guard_age <= stale_limit
    offline_since = to_epoch(health.get("offline_since"))
    out["guard"] = {
        "ok": fresh,
        "fresh": fresh,
        "age": guard_age,
        "online": bool(health.get("online")),
        "account": health.get("account"),
        "detail": health.get("detail") or "无状态",
        "offline_seconds": age_of(offline_since),
        "wake_attempts": health.get("wake_attempts", 0),
        "recoveries": health.get("recoveries", 0),
        "last_check": health.get("last_check"),
        "last_online": health.get("last_online"),
        # v2 守护写入的取证字段；旧版守护没有这些键，这里安全降级为空值
        "reason": health.get("reason") or "unknown",
        "signals": health.get("signals") or [],
        "last_incident": health.get("last_incident"),
        "incident_file": health.get("incident_file"),
        "guard_version": health.get("guard_version", "1.x"),
    }
    if not fresh:
        out["guard"]["detail"] = "健康文件超过 %s 未更新" % age_text(guard_age)

    # 2) 8080 接收端
    try:
        code, text = http_get("http://127.0.0.1:8080/health", 3)
        ok = code == 200 and "ok" in text.lower()
        detail = "GET /health -> HTTP %d %s" % (code, text.strip()[:60])
    except (urllib.error.URLError, OSError) as exc:
        ok, detail = False, "GET /health 失败：%s: %s" % (type(exc).__name__, exc)
    out["receiver"] = {"ok": ok, "detail": detail}

    # 3) 8088 管理台
    try:
        code, _ = http_get("http://127.0.0.1:8088/", 3)
        ok, detail = code < 500, "GET / -> HTTP %d" % code
    except (urllib.error.URLError, OSError) as exc:
        ok, detail = False, "GET / 失败：%s: %s" % (type(exc).__name__, exc)
    out["admin"] = {"ok": ok, "detail": detail}

    # 4) frpc 隧道进程 + 可选公网侧探测
    running = process_running("frpc.exe")
    out["frpc"] = {"ok": running,
                   "detail": "frpc.exe %s" % ("运行中" if running else "未运行")}
    remote = cfg.get("remote_probe") or {}
    if isinstance(remote, dict) and remote.get("enabled"):
        ok, detail = tcp_ok(str(remote.get("host")), int(remote.get("port", 5980)),
                            float(remote.get("timeout_seconds", 5)))
        out["remote"] = {"ok": ok, "detail": detail}
    else:
        out["remote"] = {"ok": True, "skipped": True, "detail": "未启用"}

    # 5) helper 插件 worker 心跳（进程死了 / 插件没加载都能发现）
    hb = read_json(WORKER_HEARTBEAT)
    hb_age = age_of(to_epoch(hb.get("ts")))
    ttl = float(cfg.get("worker_ttl_seconds", 90))
    worker_ok = bool(hb.get("running")) and hb_age is not None and hb_age <= ttl
    if not worker_ok:
        # 心跳每 2 秒以 tmp->replace 写入，读取恰好撞上写入窗口会拿到空/旧内容。
        # 稍等重读一次再下结论，避免 critical 一分钟后紧跟 recovered 的刷屏。
        time.sleep(0.3)
        retry = read_json(WORKER_HEARTBEAT)
        retry_age = age_of(to_epoch(retry.get("ts")))
        if bool(retry.get("running")) and retry_age is not None and retry_age <= ttl:
            hb, hb_age, worker_ok = retry, retry_age, True
    out["worker"] = {
        "ok": worker_ok,
        "age": hb_age,
        "detail": "心跳 %s 前，running=%s，PID=%s（阈值 %ss）" % (
            age_text(hb_age), hb.get("running"), hb.get("pid"), int(ttl)),
        "hooks": "link=%s / official=%s" % (hb.get("link_hook"), hb.get("official_hook")),
    }

    # 6) bot.log 新鲜度（夜间不判）
    try:
        log_age = age_of(BOT_LOG.stat().st_mtime)
        log_ok = log_age <= float(cfg.get("bot_idle_minutes", 120)) * 60
        log_detail = "bot.log 最后写入 %s 前" % age_text(log_age)
    except OSError:
        log_age, log_ok, log_detail = None, False, "bot.log 不存在或无法读取"
    out["bot_log"] = {"ok": log_ok or not active, "skipped": not active,
                      "age": log_age, "detail": log_detail}

    # 7) 消息库采集停滞（夜间不判）
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % MESSAGES_DB.as_posix(),
                               uri=True, timeout=2)
        try:
            row = conn.execute("SELECT MAX(received_at), COUNT(*) FROM messages").fetchone()
        finally:
            conn.close()
        last_msg = to_epoch(row[0] if row else None)
        lag = age_of(last_msg)
        data_ok = lag is not None and lag <= float(cfg.get("data_stall_minutes", 90)) * 60
        data_detail = "最新消息 %s（共 %s 条）" % (
            row[0] if row and row[0] else "无", row[1] if row else 0)
        if lag is not None:
            data_detail += "，距今 %s" % age_text(lag)
    except (OSError, sqlite3.Error) as exc:
        data_ok, data_detail = False, "读取消息库失败：%s: %s" % (type(exc).__name__, exc)
    out["data"] = {"ok": data_ok or not active, "skipped": not active,
                   "detail": data_detail}

    # 8) 群名/头像同步失败
    sync = read_json(SYNC_STATUS_FILE)
    sync_err = str(sync.get("error") or "").strip()
    sync_ts = to_epoch(sync.get("last_sync"))
    sync_age = age_of(sync_ts)
    window = float(cfg.get("sync_error_minutes", 20)) * 60
    sync_bad = bool(sync_err) and sync_age is not None and sync_age <= window
    out["sync"] = {"ok": not sync_bad,
                   "detail": ("最近同步 %s 前报错：%s" % (age_text(sync_age), sync_err[:120])
                              if sync_bad else "无近期同步错误")}

    # 9) 资源
    free_mem = memory_free_mb()
    mem_limit = float(cfg.get("low_memory_mb", 600))
    out["memory"] = {"ok": mem_limit <= 0 or free_mem is None or free_mem >= mem_limit,
                     "skipped": mem_limit <= 0 or free_mem is None,
                     "free_mb": free_mem,
                     "detail": "可用物理内存 %s MB（阈值 %s MB）"
                               % (("%.0f" % free_mem) if free_mem is not None else "未知",
                                  int(mem_limit))}
    free_disk = disk_free_mb()
    disk_limit = float(cfg.get("low_disk_mb", 1024))
    out["disk"] = {"ok": disk_limit <= 0 or free_disk < 0 or free_disk >= disk_limit,
                   "skipped": disk_limit <= 0 or free_disk < 0,
                   "free_mb": free_disk,
                   "detail": "磁盘剩余 %s MB（阈值 %s MB）"
                             % (("%.0f" % free_disk) if free_disk >= 0 else "未知", int(disk_limit))}
    return out


def make_body(detail: str, impact: str, action: str, extra: str = "") -> str:
    lines = ["**检测时间**：%s" % stamp(), "**判定依据**：%s" % detail]
    if extra:
        lines.append("**补充**：%s" % extra)
    lines += ["**影响**：%s" % impact, "", "**建议处置**：%s" % action]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 事件判定：raise_event 触发/续报，clear_event 恢复
# --------------------------------------------------------------------------
def evaluate(n: Notifier, cfg: dict, s: dict, pending: dict | None = None) -> list[str]:
    """把体检结果转成告警事件。

    pending 记录每个检查项「连续失败了几轮」：除微信掉线外，都要连续 2 轮
    （默认 60s 一轮，即约 1 分钟）才确认故障，避免心跳文件写入瞬间的抖动误报。
    """
    fired: list[str] = []
    pending = pending if pending is not None else {}
    g = s["guard"]
    wechat_broken = (g["fresh"] and not g["online"])
    confirm_cycles = max(1, int(cfg.get("confirm_cycles", 2)))

    def fire(key, level, title, detail, impact, action, extra=""):
        if n.raise_event(key, level, title, make_body(detail, impact, action, extra)):
            fired.append(key)

    def recover(key, title, detail=""):
        if n.clear_event(key, title, detail):
            fired.append(key + ":recovered")

    def verdict(key, ok_now, needed=None):
        """-> 'fire' | 'recover' | 'skip'（未达确认门槛时既不告警也不误报恢复）"""
        if ok_now:
            pending[key] = 0
            return "recover"
        pending[key] = int(pending.get(key, 0)) + 1
        return "fire" if pending[key] >= (needed or confirm_cycles) else "skip"

    def handle(key, check, level, title, recovered, detail, impact, action, extra="",
               needed=None, recovery_detail=""):
        if check.get("skipped"):
            pending[key] = 0
            return
        v = verdict(key, bool(check["ok"]), needed)
        if v == "fire":
            fire(key, level, title, detail, impact, action, extra)
        elif v == "recover":
            recover(key, recovered, recovery_detail)

    # 微信守护失联
    handle("guard", {"ok": g["fresh"]}, "warning", "⚠️ 微信守护进程失联",
           "✅ 微信守护已恢复", g["detail"] + "（wechat_health.json）",
           "无法判断微信是否在线，掉线自愈也已停止，可能再次出现长时间断连。",
           "检查 wechat_guard.bat 的窗口是否还在；重新双击启动 wechat_guard.bat。",
           recovery_detail="守护重新开始按配置周期更新健康状态（v2 心跳 60s / 内存探测 300s）。")

    # 微信掉线 / 恢复：守护自己已经做了内存探测，这里无需再等第二轮，立刻告警
    if g["fresh"]:
        if not g["online"]:
            extra = ("根因分类：`%s`；辅助信号：%s。\n"
                     "取证快照：%s（运行 python diagnose_offline.py 生成复盘报告）。\n"
                     "注意：11-09 排查确认连续三天凌晨掉线的真实原因是微信服务端使 PC 登录会话失效，"
                     "需人工重新登录；v2 守护不会对强制下线做无效唤醒。\n"
                     "账号对象链失效期间的消息无法回补。"
                     % (g.get("reason") or "unknown",
                        "、".join(g.get("signals") or []) or "无",
                        g.get("incident_file") or "尚未生成"))
            if g["offline_seconds"]:
                extra = "已掉线 %s；" % age_text(g["offline_seconds"]) + extra
            if verdict("wechat", False, 1) == "fire":
                fire("wechat", "critical", "🚨 微信连接断开（多半需要重新登录）", g["detail"],
                     "群消息实时采集与自动回复全部中断，且断线期间的消息无法补采；"
                     "近三次均需人工重新登录才恢复。",
                     "① 先看微信窗口是否提示「为了您的账号安全，请重新登陆」，用手机确认重新登录；"
                     "② 登录成功后桥接会自动恢复，无需重启服务；③ 别等窗口唤醒自愈（对强制下线无效）；"
                     "④ 运行 python diagnose_offline.py 查看掉线瞬间的取证快照与自动排除项；"
                     "⑤ 基线见 reports/2026-11-09_微信掉线排查报告.md。", extra)
        elif verdict("wechat", True, 1) == "recover":
            recover("wechat", "✅ 微信连接已恢复",
                    "当前账号：%s\n守护累计自愈 %s 次。" % (g["account"], g["recoveries"]))
    else:
        pending["wechat"] = 0        # 守护失联期间不误判「微信已恢复」

    # 服务类
    handle("receiver", s["receiver"], "critical", "🚨 消息接收端 8080 故障",
           "✅ 消息接收端 8080 已恢复", s["receiver"]["detail"],
           "插件上报的消息会写入本地缓冲但无法入库，网页看不到新消息。",
           "重启 start.bat 或 launcher.py server；确认 BRIDGE_TOKEN 一致。")
    handle("admin", s["admin"], "warning", "⚠️ 管理台 8088 故障",
           "✅ 管理台 8088 已恢复", s["admin"]["detail"],
           "网页无法打开，但采集链路仍在运行；公网经 frp 访问也会 502。",
           "重启 launcher.py admin 或 admin.bat。")
    handle("frpc", s["frpc"], "warning", "⚠️ frpc 隧道进程未运行",
           "✅ frpc 隧道进程已恢复", s["frpc"]["detail"],
           "公网域名无法访问本机 8080/8088，本机使用不受影响。",
           "双击 frp/start_frp.bat 重新启动隧道；检查 frps 15443 端口连通性。")
    handle("remote", s["remote"], "warning", "⚠️ 公网隧道端口不可达",
           "✅ 公网隧道端口已恢复", s["remote"]["detail"],
           "公网侧无法穿透到本机，可能 frps 未运行或防火墙变更。",
           "检查服务端 frps / 安全组规则；确认后再决定是否关闭该探测。")
    # worker 心跳每 2 秒刷新，历史上出现过「刚报失联下一分钟就恢复」的抖动，
    # 所以比其它项多确认一轮，宁可晚 1 分钟也不要 critical/recovered 来回刷屏。
    handle("worker", s["worker"], "critical", "🚨 微信桥接插件 worker 失联",
           "✅ 微信桥接插件 worker 已恢复", s["worker"]["detail"],
           "helper 主进程可能已退出，或 report_bridge 插件未加载；群消息 hook 与自动回复停止。",
           "确认 start.bat 黑窗口仍在；在窗口内输入 reload report_bridge；必要时重启 helper。",
           extra=s["worker"]["hooks"],
           needed=int(cfg.get("worker_confirm_cycles", 3)))

    # 这三类是微信掉线的下游表现，掉线期间不重复告警；夜间安静也跳过
    for key, check, title, recovered, impact, action in (
        ("bot_idle", s["bot_log"], "⚠️ 桥接日志长时间无更新", "✅ 桥接日志已恢复写入",
         "微信消息 hook 可能已停止工作，但守护仍显示在线。",
         "查看 vendor/helper-main/logs/bot.log 末尾；确认微信窗口与 helper 进程。"),
        ("stall", s["data"], "⚠️ 消息采集停滞", "✅ 消息采集已恢复",
         "数据库长时间没有新消息，群消息可能没被采集。",
         "在微信里发一条测试消息，检查缓冲库 report_buffer.sqlite3 与 8080 接收端。"),
        ("sync", s["sync"], "⚠️ 群名/头像同步失败", "✅ 群名/头像同步已恢复",
         "管理台显示的群名与头像可能过期，不影响消息采集。",
         "查看 admin/sync_status.json；微信恢复在线后会自动重试。"),
    ):
        if wechat_broken:
            pending[key] = 0
            continue
        handle(key, check, "warning", title, recovered, check["detail"], impact, action)

    # 资源类
    for key, title, recovered, impact, action in (
        ("memory", "⚠️ 可用内存过低", "✅ 可用内存已回到安全水位",
         "内存过低会加剧微信工作集换页，可能让账号对象链短时不可读；"
         "但 11-09 排查已确认它不是连续三天凌晨掉线的主因，需结合取证快照判断。",
         "关掉不用的常驻程序；长期建议加内存条（8GB 以上）。"),
        ("disk", "⚠️ 磁盘剩余空间过低", "✅ 磁盘空间已回到安全水位",
         "SQLite 写入、图片落盘与日志轮转可能失败。",
         "清理 vendor/helper-main/.runtime/images 等历史图片或转移数据。"),
    ):
        handle(key, s[key], "warning", title, recovered, s[key]["detail"], impact, action)

    return fired


def scan_logs(n: Notifier, cfg: dict) -> None:
    """扫描 bot.log / wechat_guard.log 的新增行，命中异常关键字就提醒（按文件限频）。"""
    patterns = [p for p in (cfg.get("error_patterns") or []) if p]
    if not patterns:
        return
    offsets = n.state.setdefault("log_offsets", {})
    throttle = float(cfg.get("log_error_throttle_minutes", 10)) * 60
    now = time.time()
    for label, path in (("bot.log", BOT_LOG), ("wechat_guard.log", GUARD_LOG)):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            rec = offsets.get(label)
            if not isinstance(rec, dict):
                offsets[label] = {"offset": size, "last_sent": 0}
                continue                              # 首次运行不回溯历史日志
            prev = int(rec.get("offset") or 0)
            if size < prev:
                prev = 0                              # 日志被轮转
            if size == prev:
                continue
            with path.open("rb") as handle:
                handle.seek(prev)
                chunk = handle.read(min(size - prev, 256 * 1024))
            rec["offset"] = size
            hits = [line.strip() for line in chunk.decode("utf-8", "replace").splitlines()
                    if any(p in line for p in patterns)]
            if not hits or now - float(rec.get("last_sent") or 0) < throttle:
                continue
            excerpt = "\n".join("`%s`" % hit[-180:] for hit in hits[-3:])
            body = make_body("%s 新增 %d 条异常日志" % (label, len(hits)),
                             "可能出现未纳入上述专项检查的运行故障。",
                             "打开日志查看详情，必要时重启对应进程。",
                             "最近命中：\n" + excerpt)
            if n.send("warning", "⚠️ 运行日志出现异常（%s）" % label, body):
                rec["last_sent"] = now
        except (OSError, ValueError) as exc:
            log.warning("扫描日志 %s 失败：%s", label, exc)
    n.save()


def maybe_digest(n: Notifier, cfg: dict, s: dict) -> None:
    if not cfg.get("daily_digest"):
        return
    today = datetime.now().date().isoformat()
    runtime = n.state.setdefault("runtime", {})
    if runtime.get("last_digest") == today or datetime.now().hour < int(cfg.get("daily_digest_hour", 9)):
        return
    bad = [k for k in ("guard", "receiver", "admin", "frpc", "worker", "bot_log", "data",
                       "sync", "memory", "disk") if not s[k].get("ok") and not s[k].get("skipped")]
    body = make_body("完成一轮全链路体检（%d 项）" % len(s),
                     "无" if not bad else "仍有异常项：" + "、".join(bad),
                     "无需处理" if not bad else "按前述告警逐项排查。")
    if n.send("recovered" if not bad else "warning",
              "📋 微信工具巡检摘要 · %s" % today, body):
        runtime["last_digest"] = today
        n.save()


def acquire_single_instance() -> bool:
    """Windows 命名互斥体，防止计划任务与手动双击产生两个监控进程。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True,
                                              "Local\\WxBridgeFeishuAlertMonitor")
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description="微信工具故障监控 -> 飞书告警")
    ap.add_argument("--once", action="store_true", help="只体检一次，打印 JSON，不发消息")
    ap.add_argument("--test", action="store_true", help="发送一条飞书测试卡片")
    ap.add_argument("--interval", type=int, default=0, help="巡检间隔秒数，默认读配置")
    ap.add_argument("--quiet", action="store_true", help="不往控制台打印逐轮日志")
    args = ap.parse_args()

    n = Notifier()
    if args.test:
        ok = n.send("info", "✅ 飞书告警通道测试",
                    make_body("由 health_monitor.py --test 手动触发",
                              "验证 webhook 与卡片渲染是否正常。", "无需处理。"))
        print("测试消息发送%s" % ("成功" if ok else "失败，详见 alert.log"))
        return 0 if ok else 1

    if args.once:
        print(json.dumps(collect(n.cfg), ensure_ascii=False, indent=2, default=str))
        return 0

    if not acquire_single_instance():
        print("已有飞书告警监控在运行，本次启动退出。")
        return 0

    if not args.quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                          "%Y-%m-%d %H:%M:%S"))
        logging.getLogger("WxAlert").addHandler(sh)

    interval = max(20, int(args.interval or n.cfg.get("check_interval_seconds", 60)))
    pending: dict = {}          # 每个检查项连续失败轮数，跨巡检周期保留
    log.info("飞书告警监控启动：间隔 %ds / PID %d / 配置 %s",
             interval, os.getpid(), n.cfg.get("_source"))
    write_heartbeat()

    # 启动提示：既是链路自检，也让人知道「告警本身活着」。
    # 这一段必须独立兜异常，否则无窗口的 pythonw 会静默退出。
    if n.cfg.get("notify_on_start", True):
        try:
            first = collect(n.cfg)
            bad = [k for k, v in first.items() if isinstance(v, dict) and not v.get("ok")
                   and not v.get("skipped")]
            n.send("info", "🟢 飞书告警监控已启动",
                   make_body("开始每 %d 秒巡检一次微信连接与全链路组件" % interval,
                             "本机微信工具的掉线与故障现在会推送到本群。",
                             "无需处理；把本条消息当作告警通道的连通性确认。",
                             "启动体检异常项：%s" % ("、".join(bad) if bad else "无")))
        except Exception as exc:
            log.exception("启动提示失败（不影响后续巡检）：%s", exc)

    while True:
        try:
            write_heartbeat()
            status = collect(n.cfg)
            fired = evaluate(n, n.cfg, status, pending)
            scan_logs(n, n.cfg)
            maybe_digest(n, n.cfg, status)
            ok_count = sum(1 for v in status.values()
                           if isinstance(v, dict) and v.get("ok") and not v.get("skipped"))
            log.info("巡检完成：正常 %d 项；微信=%s；触发告警=%s",
                     ok_count, "在线" if status["guard"]["online"] else "掉线/未知",
                     ",".join(fired) if fired else "无")
        except KeyboardInterrupt:
            log.info("飞书告警监控退出")
            return 0
        except Exception as exc:
            log.exception("巡检循环异常（继续运行）：%s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
