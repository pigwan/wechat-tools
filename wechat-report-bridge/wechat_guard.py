"""微信连接守护 v2：状态心跳 + 低频探测 + 掉线取证 + 有条件的自愈。

2026-11-09 排查结论修正了 v1 的核心假设：
  连续三天凌晨掉线并非「内存页被裁剪」，而是微信服务端使 PC 登录会话失效，
  客户端回到登录界面（提示「为了您的账号安全，请重新登陆」）。v1 为此唤醒窗口
  39 次全部无效，且日志只留下一句误导性的「内存页可能已被裁剪」。

v2 的变化：
  1. 取证：状态翻转那一秒生成 diagnostics/incidents/INC-*.json 全量快照，
     所有动作写 diagnostics/events/events-*.jsonl，根因按可验证事实分类；
  2. 减少自伤：账号对象链探测从 60s 降到 300s（降低高频读内存这一异常特征），
     保活从 5min 降到 15min 且不再抢焦点，唤醒只在确认内存压力时才允许；
  3. 兼容性：心跳仍每 60s 更新 admin/wechat_health.json 的 last_check，
     否则 health_monitor.py 的 guard_stale_seconds=180 会误报「守护失联」；
  4. 版本失配提前预警：微信升级后硬编码偏移会失效，这类问题不再被当成掉线；
  5. 单实例互斥锁；不修改 helper 源码、不重启微信、不改任何端口服务。

用法：
    python wechat_guard.py                    # 常驻守护（默认心跳 60s / 探测 300s）
    python wechat_guard.py --once             # 探测一次并给出根因分类
    python wechat_guard.py --snapshot         # 手动抓一份完整现场快照
    python wechat_guard.py --self-test        # 分类逻辑自测（不读微信内存）
    python wechat_guard.py --interval 45 --probe-every 300
    python diagnose_offline.py               # 复盘最近一次掉线
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
HELPER = WORKSPACE / "vendor" / "helper-main"
# 便携版 Python 不会自动把脚本目录放进 sys.path，显式补齐（同 health_monitor.py）。
for _entry in (str(ROOT), str(HELPER), str(HELPER / "native")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import guard_diagnostics as diag                       # noqa: E402

# 以 pythonw / 无控制台方式常驻时 sys.stdout、sys.stderr 为 None，
# setup_logging() 的 StreamHandler 需要真实流；兜底到 devnull，正式日志仍由
# RotatingFileHandler 写入 wechat_guard.log。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

HEALTH_FILE = ROOT / "admin" / "wechat_health.json"
LOG_FILE = ROOT / "wechat_guard.log"
GUARD_VERSION = "2.1"
ALERT_CONFIG_FILE = ROOT / "alert.local.json"
ALERT_WATCHDOG_INTERVAL = 300        # 每 5 分钟确认一次飞书巡检还活着
ALERT_HEARTBEAT_MAX_AGE = 180        # 巡检心跳超过该秒数视为失联
_notify_lock = threading.Lock()      # 同一时刻只允许一条飞书消息在发

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32 = ctypes.windll.user32
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

WM_NULL, WM_MOUSEMOVE = 0x0000, 0x0200
SW_RESTORE = 9

log = logging.getLogger("WechatGuard")
_MUTEX_HANDLE = None            # 必须保持引用，否则互斥体会被释放


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    # 轮转：上次断线 12 小时刷了 2912 条等待日志，避免日志无限膨胀。
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def acquire_single_instance() -> bool:
    """命名互斥体：防止手动双击与计划任务产生两个守护同时读微信内存。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, "Local\\WxBridgeWechatGuard")
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True


def find_wechat_window():
    """动态查找微信主窗口，避免硬编码句柄（重启微信后句柄会变）。"""
    found = []

    def cb(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(128)
        _user32.GetWindowTextW(hwnd, title, 256)
        _user32.GetClassNameW(hwnd, cls, 128)
        if title.value == "微信" and cls.value.startswith("Qt") and _user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    _user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def helper_api():
    """延迟导入 helper 的只读内存工具，避免守护启动时微信尚未就绪就报错。"""
    import run_selftest as api
    from wxbot_framework.moments_reader import _Memory
    from wxbot_framework.account import read_current_account
    from wxbot_framework.native_profiles import select_memory_profile
    return api, _Memory, read_current_account, select_memory_profile


def probe():
    """只读探测账号对象链：返回 (是否在线, 账号ID, 说明)。

    注意：这是「重」操作（读微信进程内存），v2 每 300s 才做一次，
    以降低长时间挂机时的异常行为特征；心跳不要依赖它。
    """
    try:
        api, Memory, read_account, select_profile = helper_api()
        pids = api.wechat_candidates()
        if not pids:
            return False, None, "未找到微信进程"
        for pid in pids[:3]:
            core = api.modules(pid).get("weixin.dll")
            if not core:
                continue
            base = core[0]
            mem = Memory(pid)
            if select_profile(mem.read, base) is None:
                return False, None, "账号对象链未就绪（读不到已验证的对象链）"
            account = read_account(mem.read, base)
            if not account:
                return False, None, "登录账号对象为空"
            return True, account, "正常"
        return False, None, "微信进程里没有已加载的 Weixin.dll"
    except Exception as exc:                       # 守护绝不能因为探测异常退出
        return False, None, "探测异常: %s: %s" % (type(exc).__name__, exc)


def nudge_window(hwnd, activate=False):
    """无害保活。activate=True 只在确认内存压力时才用，且会短暂抢焦点。"""
    if hwnd is None:
        return False
    try:
        _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        _user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, 0x000A000A)
        if activate:
            prev = _user32.GetForegroundWindow()
            _user32.ShowWindow(hwnd, SW_RESTORE)
            _user32.SetForegroundWindow(hwnd)
            time.sleep(1.0)
            if prev:
                _user32.SetForegroundWindow(prev)
        return True
    except Exception as exc:
        log.warning("窗口保活失败: %s", exc)
        return False


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 飞书直报 + 告警巡检保活（2026-11-10 凌晨告警空窗后新增）
#
# 教训：巡检进程在 11-09 19:42 之后就没了，03:29 微信掉线时没有任何进程
# 负责推飞书。守护是链路里最稳定、且第一时间知道掉线的进程，让它直接上报：
# 即使 health_monitor.py 完全没运行，掉线 / 递增提醒 / 恢复也都能到飞书群。
# 两边共用 alert_state.json 的 wechat 事件，由 Notifier 去重，不会重复刷屏。
# ---------------------------------------------------------------------------
def _feishu_async(job) -> None:
    """网络调用放后台线程：飞书超时/重试不能拖慢 60 秒心跳。"""
    if not _notify_lock.acquire(blocking=False):
        return

    def runner():
        try:
            job()
        except Exception as exc:
            log.warning("飞书直报异常（忽略，不影响守护）：%s: %s",
                        type(exc).__name__, exc)
        finally:
            _notify_lock.release()

    threading.Thread(target=runner, daemon=True, name="guard-feishu").start()


def feishu_report(online: bool, state: dict, reason: str = "", signals=None,
                  detail: str = "", incident_file: str = "") -> None:
    """掉线、持续离线、恢复都走这里；限频与去重交给 Notifier 的事件状态。"""
    try:
        cfg = json.loads(ALERT_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        if not cfg.get("enabled", True) or not cfg.get("guard_direct_notify", True):
            return
    except (OSError, ValueError):
        return
    snapshot = dict(state)

    def job():
        from feishu_notifier import Notifier, human_duration, _stamp
        n = Notifier()
        if online:
            n.clear_event("wechat", "✅ 微信连接已恢复",
                          "守护直报：账号对象链已恢复可读，消息收发恢复正常。\n"
                          "账号：%s｜守护累计自愈 %s 次。"
                          % (snapshot.get("account"), snapshot.get("recoveries")))
            return
        duration = ""
        if snapshot.get("offline_since"):
            try:
                seconds = (datetime.now() - datetime.fromisoformat(
                    snapshot["offline_since"])).total_seconds()
                duration = human_duration(seconds)
            except (ValueError, TypeError):
                pass
        body = (
            "**来源**：微信守护进程直报（不依赖飞书巡检进程是否存活）\n"
            "**时间**：%s\n"
            "**根因分类**：`%s`；辅助信号：%s\n"
            "**现场**：%s\n"
            "**取证快照**：%s\n"
            "**已掉线**：%s\n\n"
            "**影响**：群消息实时采集与自动回复中断，断线期间的消息无法补采。\n\n"
            "**建议处置**：① 查看微信窗口是否提示「为了您的账号安全，请重新登陆」，"
            "用手机确认后重新登录；② 登录成功后桥接自动恢复，无需重启服务；"
            "③ 别等窗口唤醒自愈（对服务端强制下线无效）；"
            "④ 运行 python diagnose_offline.py 复盘掉线瞬间快照。"
            % (_stamp(), reason or "unknown", "、".join(signals or []) or "无",
               detail, incident_file or "尚未生成", duration or "刚刚"))
        n.raise_event("wechat", "critical", "🚨 微信连接断开（守护直报）", body)

    _feishu_async(job)


def ensure_alert_monitor() -> None:
    """守护反过来盯着告警巡检：心跳陈旧就重新拉起，并留下事件记录。"""
    try:
        import launcher
        max_age = ALERT_HEARTBEAT_MAX_AGE
        try:
            cfg = json.loads(ALERT_CONFIG_FILE.read_text(encoding="utf-8-sig"))
            if not cfg.get("enabled", True) or not cfg.get("auto_start_with_launcher", True):
                return
            max_age = float(cfg.get("alert_heartbeat_max_age_seconds", max_age))
        except (OSError, ValueError, TypeError):
            pass
        result = launcher.ensure_alert_monitor(launcher.build_env(), max_age=max_age)
        if result in ("started", "restarted"):
            diag.append_event("alert_monitor_healed", result=result,
                              heartbeat_age_seconds=launcher.alert_heartbeat_age())
            log.warning("飞书告警监控%s，守护已自动补拉（%s）",
                        "曾失联" if result == "restarted" else "未运行", result)
    except Exception as exc:
        log.warning("检查飞书告警监控失败（忽略）：%s: %s",
                    type(exc).__name__, exc)


def self_test() -> int:
    """分类逻辑自测：不触碰微信内存，验证每种掉线都能落到正确的 reason。"""
    cases = [
        (False, "未找到微信进程", {"wechat": {"processes": [], "core_module": {},
                                          "windows": {}}}, "process_gone"),
        (False, "微信进程里没有已加载的 Weixin.dll",
         {"wechat": {"processes": [{"pid": 1}], "main_pids": [1], "core_module": {},
                     "windows": {}}}, "core_dll_missing"),
        (False, "账号对象链未就绪（读不到已验证的对象链）",
         {"wechat": {"processes": [{"pid": 1}], "main_pids": [1],
                     "core_module": {"profile_match": False}, "windows": {}}},
         "version_mismatch"),
        (False, "探测异常: ImportError: no module",
         {"wechat": {"processes": [{"pid": 1}], "main_pids": [1],
                     "core_module": {"profile_match": True}, "windows": {}}},
         "probe_error"),
        (False, "账号对象链未就绪（读不到已验证的对象链）",
         {"wechat": {"processes": [{"pid": 1}], "main_pids": [1],
                     "core_module": {"profile_match": True},
                     "windows": {"login_window_suspected": True}},
          "memory": {"available_mb": 1800}, "network": {"frpc_running": True, "ports": {}},
          "system_events": {"events": [], "relevant_events": []}}, "awaiting_login"),
        (True, "正常", {}, "ok"),
    ]
    failed = 0
    for online, detail, snap, expected in cases:
        reason, signals, _ = diag.classify(online, detail, snap)
        ok = reason == expected
        failed += 0 if ok else 1
        print("%s  %-20s -> %-20s signals=%s" %
              ("PASS" if ok else "FAIL", expected, reason, signals))
    print("自测结果：%d/%d 通过" % (len(cases) - failed, len(cases)))
    return 0 if failed == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="微信连接守护 v2（取证 + 低频探测）")
    ap.add_argument("--once", action="store_true", help="只探测一次，打印分类结果")
    ap.add_argument("--snapshot", action="store_true", help="手动抓一份完整现场快照")
    ap.add_argument("--self-test", action="store_true", help="根因分类逻辑自测")
    ap.add_argument("--interval", type=int, default=60,
                    help="心跳间隔秒数，默认 60；上限 60，保证飞书监控不误报守护失联")
    ap.add_argument("--probe-every", type=int, default=300,
                    help="账号对象链探测间隔秒数，默认 300（越大对微信内存读取越少）")
    ap.add_argument("--nudge-minutes", type=int, default=15,
                    help="无害窗口保活间隔分钟，默认 15")
    ap.add_argument("--skip-system-events", action="store_true",
                    help="取证时不采集 Windows 系统事件（可加快快照）")
    args = ap.parse_args()

    setup_logging()
    include_system = not args.skip_system_events

    if args.self_test:
        return self_test()

    online, account, detail = probe()
    probe_result = {"online": online, "account": account, "detail": detail}

    if args.once:
        snap = diag.build_snapshot(include_system=include_system)
        reason, signals, summary = diag.classify(online, detail, snap)
        print(json.dumps({"online": online, "account": account, "detail": detail,
                          "window": find_wechat_window(), "reason": reason,
                          "signals": signals, "summary": summary,
                          "core_dll": snap["wechat"]["core_module"].get("matched_versions"),
                          "available_memory_mb": snap["memory"].get("available_mb")},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    if args.snapshot:
        incident_id, path, payload = diag.capture_incident(
            "manual_snapshot", probe_result, {}, include_system=include_system)
        cls = payload["classification"]
        print(json.dumps({"incident_id": incident_id, "file": str(path),
                          "reason": cls["reason"], "signals": cls["signals"],
                          "summary": cls["summary"]}, ensure_ascii=False, indent=2))
        return 0

    if not acquire_single_instance():
        print("已有微信守护在运行，本次启动退出（避免两个进程同时读微信内存）。")
        return 0

    tick = max(20, min(args.interval, 60))         # 心跳上限 60s，兼容告警监控
    probe_every = max(60, args.probe_every)
    # 0 表示完全关闭窗口保活（减少对微信窗口的外部干预）
    nudge_every = args.nudge_minutes * 60 if args.nudge_minutes > 0 else 0
    log.info("微信守护 v%s 启动：心跳 %ds / 内存探测 %ds / 保活 %s / PID %d",
             GUARD_VERSION, tick, probe_every,
             ("%ds" % nudge_every) if nudge_every else "已关闭", os.getpid())
    diag.prune()
    diag.append_event("guard_start", guard_version=GUARD_VERSION, tick_seconds=tick,
                      probe_every_seconds=probe_every, nudge_seconds=nudge_every,
                      online=online, detail=detail)

    now_iso = datetime.now().isoformat(timespec="seconds")
    state = {
        # 以下字段被 health_monitor.py / admin/app.py 读取，全部保持兼容
        "online": online, "account": account, "detail": detail,
        "offline_since": None if online else now_iso,
        "last_online": now_iso if online else None,
        "last_check": now_iso,
        "recoveries": 0, "wake_attempts": 0, "guard_pid": os.getpid(),
        "bot_log": str(diag.BOT_LOG),
        # v2 新增字段（旧消费者忽略未知字段，不会破坏兼容）
        "guard_version": GUARD_VERSION, "reason": "ok" if online else "unknown",
        "signals": [], "last_probe": now_iso, "probe_every_seconds": probe_every,
        "last_incident": None, "incident_file": None,
        "diagnostics_dir": str(diag.INCIDENT_DIR),
    }
    atomic_json(HEALTH_FILE, state)

    last_probe = time.monotonic()                  # 启动时刚探测过
    last_nudge = last_probe
    last_alert_watchdog = last_probe               # 给巡检进程留足启动时间再开始盯
    last_wake = 0.0
    wake_count_hour = 0
    hour_mark = last_probe
    was_online = online
    current_reason = state["reason"]
    skipped_wake_logged = False

    while True:
        try:
            time.sleep(tick)
            now = time.monotonic()
            stamp = datetime.now().isoformat(timespec="seconds")
            if now - hour_mark >= 3600:
                hour_mark, wake_count_hour = now, 0

            # 守护顺带保活飞书巡检：11-10 凌晨就是巡检悄悄死掉导致零告警。
            if now - last_alert_watchdog >= ALERT_WATCHDOG_INTERVAL:
                last_alert_watchdog = now
                ensure_alert_monitor()

            hwnd = find_wechat_window()
            if nudge_every and now - last_nudge >= nudge_every:
                # 只发无害消息，不抢焦点、不激活窗口。
                nudge_window(hwnd, activate=False)
                last_nudge = now

            # 重探测（读微信内存）按低频执行；心跳不依赖它。
            if now - last_probe >= probe_every:
                online, account, detail = probe()
                probe_result = {"online": online, "account": account, "detail": detail}
                last_probe = now
                fingerprint = diag.quick_core_fingerprint()
                if fingerprint.get("match") is False:
                    log.warning("Weixin.dll 指纹不匹配（size=%s），客户端可能已升级，"
                                "请先更新 native_profiles，不要按掉线处理",
                                fingerprint.get("size"))
                    diag.append_event("version_mismatch", level="WARNING", **fingerprint)

                diag.append_event("probe", level="INFO" if online else "WARNING",
                                  online=online, account=account, detail=detail,
                                  core_dll_match=fingerprint.get("match"))
                state["last_probe"] = stamp

                if online:
                    if not was_online:
                        offline_since = state.get("offline_since")
                        minutes = 0.0
                        if offline_since:
                            try:
                                minutes = (datetime.now() -
                                           datetime.fromisoformat(offline_since)
                                           ).total_seconds() / 60
                            except ValueError:
                                pass
                        state["recoveries"] += 1
                        diag.append_event("recovery", offline_minutes=round(minutes, 1),
                                          account=account,
                                          note="微信会话恢复；近三次均需人工重新登录")
                        log.info("微信连接已恢复：账号=%s | 断线 %.1f 分钟 | 恢复方式=重新登录/会话恢复",
                                 account, minutes)
                        feishu_report(True, dict(state, account=account))
                    state.update(online=True, account=account, detail=detail,
                                 last_online=stamp, offline_since=None,
                                 reason="ok", signals=[])
                    was_online = True
                    current_reason = "ok"
                    skipped_wake_logged = False
                else:
                    if was_online:
                        # 关键：状态翻转这一秒抓取全量现场，供下次直接复盘。
                        incident_id, path, payload = diag.capture_incident(
                            "online_to_offline", probe_result, state,
                            include_system=include_system)
                        cls = payload["classification"]
                        current_reason = cls["reason"]
                        state.update(offline_since=stamp, reason=current_reason,
                                     signals=cls["signals"], last_incident=incident_id,
                                     incident_file=str(path))
                        log.warning("检测到微信连接断开：reason=%s | detail=%s | 取证快照=%s",
                                    current_reason, detail, path)
                        log.warning("分类说明：%s", cls["summary"])
                        log.warning("建议处置：%s", cls["suggested_action"])
                        skipped_wake_logged = False
                    state.update(online=False, account=None, detail=detail)
                    was_online = False
                    # 每次探测仍离线都上报，由 Notifier 按 10/30/60/120/240 分钟
                    # 递增提醒；巡检进程即使完全不在，用户也不会整夜收不到消息。
                    feishu_report(False, state, reason=current_reason,
                                  signals=state.get("signals"), detail=detail,
                                  incident_file=state.get("incident_file"))

                    offline_seconds = 0.0
                    if state.get("offline_since"):
                        try:
                            offline_seconds = (datetime.now() -
                                               datetime.fromisoformat(state["offline_since"])
                                               ).total_seconds()
                        except ValueError:
                            pass

                    # 只有确认是内存压力才允许唤醒；被强制下线时唤醒 100% 无效，
                    # 还会反复抢用户焦点（v1 的 39 次无效唤醒就是这么来的）。
                    if (current_reason == "memory_pressure" and offline_seconds >= 120
                            and now - last_wake >= 3600 and wake_count_hour < 1):
                        if nudge_window(hwnd, activate=True):
                            last_wake = now
                            wake_count_hour += 1
                            state["wake_attempts"] += 1
                            state["last_wake"] = stamp
                            diag.append_event("wake_attempt", reason=current_reason,
                                              offline_seconds=round(offline_seconds))
                            log.warning("疑似内存换页，已尝试唤醒微信窗口 1 次（本小时上限 1 次）")
                    elif not skipped_wake_logged:
                        skipped_wake_logged = True
                        diag.append_event("wake_skipped", reason=current_reason,
                                          note="非内存压力场景，窗口唤醒无效，跳过")
                        log.info("已跳过窗口唤醒：reason=%s（需人工重新登录，唤醒无效）",
                                 current_reason)

            # 每轮心跳都写：health_monitor.py 用 last_check 判断守护是否失联。
            state["last_check"] = stamp
            atomic_json(HEALTH_FILE, state)
        except KeyboardInterrupt:
            log.info("微信守护退出")
            diag.append_event("guard_stop", note="KeyboardInterrupt")
            atomic_json(HEALTH_FILE, dict(state, running=False,
                                          last_check=datetime.now().isoformat(timespec="seconds")))
            return 0
        except Exception as exc:
            log.exception("守护循环异常（继续运行）: %s", exc)
            diag.append_event("error", level="ERROR", error="%s: %s" %
                              (type(exc).__name__, exc))


if __name__ == "__main__":
    raise SystemExit(main())
