"""微信工具统一启动台：一个控制台启动全部常驻组件，并汇总展示它们的日志。

用法（一般由桌面的「微信工具-启动.bat」调用）：
    python run_all.py                # 接管模式：重启已运行的旧组件，全部日志归本窗口
    python run_all.py --reuse        # 复用模式：已跑的组件不动，只补启动缺失的
    python run_all.py --dry-run      # 只打印将要执行的动作，不启动也不停止任何进程
    python run_all.py --no-alert     # 本次不拉起飞书告警
    python run_all.py --no-brain     # 本次不拉拟人大脑（网页总开关关闭时也会自动跳过）

设计要点：
- 每个子进程 stdout/stderr 用管道逐行读取，加组件标签后打印到同一控制台。
- 命名互斥体保证只开一个统一控制台；Ctrl+C 会关闭「本次启动」的组件。
- 飞书告警仍是独立静默进程（主项目挂掉时它才能报故障），不在本窗口内。
- 拟人大脑作为普通组件纳入本窗口；它自己另有 WxBridgeBrain 命名互斥体，
  网页把 enabled 关掉时本入口不会再拉它（避免起了就退、反复刷日志）。
- 组件启动参数与项目里各 .bat 声明保持一致，环境变量复用 launcher.build_env()。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - 便携 Python 里已内置 psutil
    psutil = None

ROOT = Path(__file__).resolve().parent                 # wechat-report-bridge
WORKSPACE = ROOT.parent
PY = WORKSPACE / "runtime" / "python313" / "python.exe"
HELPER = WORKSPACE / "vendor" / "helper-main"
FRP = WORKSPACE / "frp"
SERVER = ROOT / "server" / "app.py"
ADMIN = ROOT / "admin" / "app.py"
GUARD = ROOT / "wechat_guard.py"
BRAIN = ROOT / "brain" / "brain.py"
BRAIN_RUNTIME = ROOT / "brain" / "runtime.local.json"
BRAIN_CONFIG = ROOT / "brain" / "config.local.json"
FRPC = FRP / "frp_0.72.0_windows_amd64" / "frpc.exe"
ALERT_LOG = ROOT / "alert.log"
MUTEX_NAME = "Local\\WxBridgeUnifiedConsole"

# 接管时要先停外部隧道和大脑，再停服务（brain 读消息库，必须早于 receiver 停）
STOP_ORDER = ["brain", "frpc", "guard", "bridge", "admin", "receiver"]

# 与 wechat_guard.bat 保持一致的 v2 参数；改参数时两处一起改
GUARD_ARGS = ["--interval", "60", "--probe-every", "300", "--nudge-minutes", "15"]

sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402  复用统一的环境构造与飞书告警启动逻辑

_MUTEX_HANDLE = None
_PRINT_LOCK = threading.Lock()


def acquire_single_instance() -> bool:
    """同一时刻只允许一个统一控制台（重复双击会直接退出）。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True


def set_console_title(title: str) -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def say(label: str, text: str) -> None:
    with _PRINT_LOCK:
        print("%s [%s] %s" % (stamp(), label.ljust(6), text), flush=True)


def norm(value: str) -> str:
    return (value or "").replace("\\", "/").lower()


def short_cmd(comp: Comp) -> str:
    """计划输出里省略解释器路径，但非 Python 组件（frpc）要完整显示。"""
    parts = comp.cmd[3:] if str(comp.cmd[0]).lower().endswith("python.exe") else comp.cmd
    return " ".join(parts)[:100]


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.25)
    return False


@dataclass
class Comp:
    key: str
    label: str
    cmd: list
    cwd: Path
    needle: str                       # 进程命令行识别关键字
    env_extra: dict = field(default_factory=dict)
    wait_port: int = 0
    settle: float = 0.4                # 启动后留给它的缓冲时间


def build_components(base_env: dict) -> list[Comp]:
    # 8088 的鉴权参数必须与现有实例一致，否则公网登录方式会变。
    # admin_token 取自 frp/settings.local.json，不在本文件里硬编码密钥。
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    settings = FRP / "settings.local.json"
    if settings.is_file():
        try:
            admin_token = str(json.loads(settings.read_text(encoding="utf-8-sig"))
                              .get("admin_token") or admin_token)
        except (OSError, ValueError):
            pass
    admin_env = {
        "ADMIN_TOKEN": admin_token,
        "ADMIN_PUBLIC": "1",
        "ADMIN_READONLY": "0",
    }
    py = [str(PY), "-X", "utf8", "-u"]
    return [
        Comp("receiver", "8080", py + [str(SERVER)], WORKSPACE,
             "wechat-report-bridge/server/app.py", wait_port=8080, settle=0.2),
        Comp("bridge", "helper", py + [str(HELPER / "main.py")], HELPER,
             "helper-main/main.py", settle=1.2),
        Comp("admin", "8088", py + [str(ADMIN), "--port", "8088"], WORKSPACE,
             "wechat-report-bridge/admin/app.py", env_extra=admin_env, settle=0.5),
        Comp("guard", "guard", py + [str(GUARD)] + GUARD_ARGS, WORKSPACE,
             "wechat_guard.py", settle=0.5),
        Comp("frpc", "frpc", [str(FRPC), "-c", "frpc.toml"], FRP,
             "frpc.toml", settle=0.5),
        # 大脑排在最后：它只读消息库，等 receiver 把库/表准备好再起来，
        # 否则空库首轮会刷“no such table”异常（不致命但很吵）。
        Comp("brain", "brain", py + [str(BRAIN)], ROOT,
             "brain/brain.py", settle=0.6),
    ]


def brain_switch() -> dict:
    """网页端开关状态：先取 config.local.json 兜底，再用 runtime.local.json 覆盖
    （与 brain.py 里的 _apply_runtime_switches 保持同一优先级）。
    只做判断，不改任何文件；两个文件都没有就按“关闭”处理，宁可不启动也不乱发消息。"""
    state = {"enabled": False, "dry_run": True, "source": "default"}
    for path, source in ((BRAIN_CONFIG, "config"), (BRAIN_RUNTIME, "web")):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data.get("enabled"), bool):
            state["enabled"] = data["enabled"]
        if isinstance(data.get("dry_run"), bool):
            state["dry_run"] = data["dry_run"]
        state["source"] = source
    return state


def matches(comp: Comp, proc) -> bool:
    cmd = norm(" ".join(proc.info.get("cmdline") or []))
    if not cmd:
        return False
    if comp.key == "bridge":
        # helper 的命令行只有 "main.py"，靠工作目录区分，并排除它的 watchdog 子进程
        if "bridge_watchdog" in cmd or "main.py" not in cmd:
            return False
        try:
            cwd = norm(str(proc.cwd()))
        except (psutil.AccessDenied, OSError):
            cwd = ""
        return norm(str(HELPER)) in cwd or norm(str(HELPER)) in cmd
    if comp.key == "frpc":
        exe = norm(proc.info.get("exe") or "")
        return ("frpc.exe" in norm(proc.info.get("name") or "")
                and "frpc.toml" in cmd and norm(str(FRP)) in exe)
    return comp.needle in cmd


def scan_existing(comps: list[Comp]) -> dict:
    found = {c.key: [] for c in comps}
    if psutil is None:
        return found
    ancestors = set()
    try:
        cur = psutil.Process(os.getpid())
        while cur is not None:
            ancestors.add(cur.pid)
            cur = cur.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
        try:
            if proc.pid == os.getpid() or proc.pid in ancestors:
                continue
            for comp in comps:
                if matches(comp, proc):
                    found[comp.key].append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return found


def stop_existing(found: dict, comps: list[Comp]) -> None:
    """只终止本项目可识别的常驻进程；顺序为先停大脑和外部隧道，再停服务。"""
    targets = []
    for key in STOP_ORDER:
        for proc in found.get(key, []):
            targets.append((key, proc))
    if not targets:
        say("boot", "没有发现旧的组件进程，直接全新启动。")
        return
    for key, proc in targets:
        say("boot", "接管：停止 %s (PID %d)" % (key, proc.pid))
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs = [p for _, p in targets]
    _, alive = psutil.wait_procs(procs, timeout=6)
    for proc in alive:
        say("boot", "进程 %d 未及时退出，强制结束。" % proc.pid)
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # 等 helper 的 watchdog 完成 DLL/注入清理，避免新旧实例抢锁
    time.sleep(1.5)


class Runner:
    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.exited: set[str] = set()

    def _pump(self, label: str, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text.strip():
                    say(label, text)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def start(self, comp: Comp, base_env: dict) -> None:
        env = dict(base_env)
        env.update(comp.env_extra)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            comp.cmd, cwd=str(comp.cwd), env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.procs[comp.key] = proc
        for stream in (proc.stdout, proc.stderr):
            threading.Thread(target=self._pump, args=(comp.label, stream), daemon=True).start()
        say("boot", "已启动 %-7s (PID %d)" % (comp.label, proc.pid))
        if comp.wait_port:
            if wait_port(comp.wait_port):
                say("boot", "%s 端口 %d 已就绪" % (comp.label, comp.wait_port))
            else:
                say("boot", "⚠️ 端口 %d 在 15 秒内未就绪，请留意上方日志" % comp.wait_port)
        time.sleep(comp.settle)

    def check_exits(self) -> bool:
        """返回是否还有活动组件；某个组件意外退出时只提示，不自动重启。"""
        for key, proc in list(self.procs.items()):
            rc = proc.poll()
            if rc is not None and key not in self.exited:
                self.exited.add(key)
                hint = ("；原因见 brain\\brain.log 末几行"
                        if key == "brain" else "")
                say("sys", "组件 %s 已退出（退出码 %s）%s；其它组件继续运行。按 Ctrl+C 关闭本窗口。"
                    % (key, rc, hint))
        return any(p.poll() is None for p in self.procs.values())

    def shutdown(self) -> None:
        for key, proc in list(self.procs.items()):
            if proc.poll() is None:
                say("sys", "正在关闭 %s (PID %d)" % (key, proc.pid))
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.time() + 5
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        say("sys", "本次启动的组件已全部关闭。飞书告警为独立进程，仍在后台监控。")


def follow_log(path: Path, label: str, stop: threading.Event) -> None:
    """把只写文件的日志（飞书告警 / frpc）也接到控制台，仅显示启动后的新增行。"""
    offset = path.stat().st_size if path.is_file() else 0
    while not stop.is_set():
        stop.wait(1.0)
        try:
            size = path.stat().st_size if path.is_file() else 0
            if size < offset:               # 日志轮转或被清空
                offset = 0
            if size <= offset:
                continue
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
            offset = size
            for raw in chunk.decode("utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line:
                    say(label, line)
        except OSError:
            pass


def check_alert_watchdog(base_env: dict) -> None:
    """统一控制台常驻时按心跳补拉飞书巡检，并把监控空窗显式报到飞书。"""
    try:
        result = launcher.ensure_alert_monitor(base_env, max_age=180)
    except Exception as exc:
        say("alert", "告警保活检查异常（忽略）：%s" % exc)
        return
    if result != "restarted":
        return
    say("alert", "⚠️ 飞书告警监控曾失联，已重新拉起；空窗期间的告警可能缺失")
    try:
        from feishu_notifier import Notifier
        Notifier().send(
            "warning", "⚠️ 飞书告警监控失联后已自愈",
            "**说明**：统一控制台按心跳发现巡检进程长时间没有刷新，已强制重新拉起。\n"
            "**影响**：失联期间的全链路巡检告警可能缺失；微信掉线仍由守护进程直报。\n"
            "**建议**：查看 alert.log 与飞书群里对应时段是否有掉线消息。")
    except Exception as exc:
        say("alert", "空窗提示发送失败（忽略）：%s" % exc)


def main() -> int:
    ap = argparse.ArgumentParser(description="微信工具统一启动台（一个控制台看全部日志）")
    ap.add_argument("--reuse", action="store_true", help="复用已运行的组件，只补启动缺失的")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不启动/停止任何进程")
    ap.add_argument("--no-alert", action="store_true", help="本次不拉起飞书告警")
    ap.add_argument("--no-brain", action="store_true", help="本次不拉起拟人大脑")
    args = ap.parse_args()

    if not args.dry_run and not acquire_single_instance():
        print("已有一个统一控制台在运行。请回到原窗口查看日志；本入口只允许开一个。")
        return 1

    set_console_title("微信工具 · 统一控制台")
    say("sys", "=".ljust(58, "="))
    say("sys", "微信工具统一启动台 · 模式：%s%s" %
        ("复用已有" if args.reuse else "接管重启", "（DRY-RUN）" if args.dry_run else ""))
    say("sys", "=".ljust(58, "="))

    if psutil is None:
        say("sys", "缺少 psutil，无法识别已有进程；请改用便携 Python 运行。")
        return 2

    base_env = launcher.build_env()
    comps = build_components(base_env)
    found = scan_existing(comps)
    running = {k: [p.pid for p in v] for k, v in found.items() if v}
    say("sys", "当前已运行：%s" % (", ".join("%s=%s" % (k, v) for k, v in running.items())
                                  or "无"))

    brain = brain_switch()
    run_brain = not args.no_brain
    skip_note = "（跳过：--no-brain）"
    if args.no_brain:
        say("sys", "拟人大脑：本次按 --no-brain 跳过。")
    elif not brain["enabled"]:
        say("sys", "拟人大脑：总开关当前是关闭的（来源 %s），进程会以待机方式启动；"
                   "网页打开开关后自动开始工作，不用重启。" % brain["source"])
    else:
        say("sys", "拟人大脑：总开关已开启（来源 %s，%s）。" %
            (brain["source"], "演练模式 dry_run=true，不会真发消息"
             if brain["dry_run"] else "实发模式 dry_run=false"))

    if args.dry_run:
        if args.reuse:
            for comp in comps:
                if comp.key == "brain" and not run_brain:
                    state = skip_note
                else:
                    state = "复用 PID %s" % running[comp.key] if comp.key in running else "将启动"
                say("plan", "%-7s %s | %s" % (comp.label, state, short_cmd(comp)))
        else:
            for key in STOP_ORDER:
                if key in running:
                    say("plan", "将停止 %s PID %s" % (key, running[key]))
            for comp in comps:
                state = "" if (comp.key != "brain" or run_brain) else skip_note
                say("plan", "将启动 %-7s | %s%s" % (comp.label, short_cmd(comp), state))
        say("plan", "飞书告警：%s" % ("跳过" if args.no_alert else "确保运行（静默独立进程）"))
        say("plan", "DRY-RUN 结束，未启动或停止任何进程。")
        return 0

    if not args.reuse:
        stop_existing(found, comps)
    else:
        for key, pids in running.items():
            say("boot", "复用 %-7s (PID %s)；它的日志不在本窗口" % (key, pids))

    runner = Runner()
    stop_event = threading.Event()
    if not args.no_alert:
        threading.Thread(target=follow_log, args=(ALERT_LOG, "alert", stop_event),
                         daemon=True).start()
    # frpc 按配置写文件日志，stdout 内容很少，单独跟随文件才能看到隧道状态
    threading.Thread(target=follow_log, args=(FRP / "frpc.log", "frpc", stop_event),
                     daemon=True).start()

    try:
        for comp in comps:
            if args.reuse and comp.key in running:
                continue
            if comp.key == "brain" and not run_brain:
                continue
            runner.start(comp, base_env)

        if not args.no_alert:
            os.environ.pop("WXBRIDGE_NO_ALERT", None)
            ok = launcher.start_alert_monitor(base_env)
            say("alert", "飞书告警：%s" % ("已就绪（独立静默进程，日志见下方 [alert]）" if ok
                                          else "未启用，见上方提示"))
        else:
            os.environ["WXBRIDGE_NO_ALERT"] = "1"

        say("sys", "-".ljust(58, "-"))
        say("sys", "全部就绪 · 管理台 http://127.0.0.1:8088 · 接收端 http://127.0.0.1:8080/health")
        if run_brain and "brain" in runner.procs:
            say("sys", "拟人大脑已随本窗口启动（%s）；决策日志看管理台「应用 → 拟人大脑」，"
                       "文件日志 brain\\brain.log。" %
                ("演练 dry_run" if brain["dry_run"] else "实发模式"))
        say("sys", "下面开始实时显示各组件日志；按 Ctrl+C 停止本次启动的全部组件。")
        say("sys", "-".ljust(58, "-"))

        if not runner.procs:
            say("sys", "本次没有新启动的组件（全部为复用）。按 Ctrl+C 退出。")
        last_alert_check = time.time() - 25        # 启动 5 秒后做第一次保活检查
        while True:
            runner.check_exits()
            if not args.no_alert and time.time() - last_alert_check >= 30:
                last_alert_check = time.time()
                check_alert_watchdog(base_env)
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("sys", "收到 Ctrl+C，开始收尾 …")
    finally:
        stop_event.set()
        runner.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
