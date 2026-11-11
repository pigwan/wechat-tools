"""掉线复盘：把取证快照 + 结构化事件流整理成一份可读诊断报告。

下次再掉线时：
    python diagnose_offline.py                # 分析最近一次事件，生成 Markdown 报告
    python diagnose_offline.py --list         # 列出所有事件快照
    python diagnose_offline.py --incident INC-20261109-033012
    python diagnose_offline.py --events 50    # 只看结构化事件时间线
    python diagnose_offline.py --no-write     # 只在控制台打印，不落文件

输出目录：reports/掉线诊断_<incident_id>.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import guard_diagnostics as diag                       # noqa: E402

REPORT_DIR = ROOT / "reports"


def list_incidents() -> list[Path]:
    try:
        return sorted(diag.INCIDENT_DIR.glob("INC-*.json"))
    except OSError:
        return []


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fmt(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)[:300]
    return str(value)


def evidence_lines(payload: dict) -> tuple[list[str], list[str]]:
    """返回 (证据表行, 自动排除结论)。判定只依据快照里可验证的事实。"""
    snap = payload.get("snapshot") or {}
    wechat = snap.get("wechat") or {}
    procs = wechat.get("processes") or []
    core = wechat.get("core_module") or {}
    windows = wechat.get("windows") or {}
    mem = snap.get("memory") or {}
    net = snap.get("network") or {}
    system = snap.get("system_events") or {}
    cls = payload.get("classification") or {}
    signals = cls.get("signals") or []
    rows: list[str] = []
    exclusions: list[str] = []

    rows.append("| 项 | 值 |")
    rows.append("|---|---|")
    rows.append("| 触发时间 | %s |" % fmt(payload.get("created_at")))
    rows.append("| 探测结果 | online=%s / %s |" % (
        fmt((payload.get("probe") or {}).get("online")),
        fmt((payload.get("probe") or {}).get("detail"))))
    rows.append("| 微信进程数 | %d（主 PID：%s） |" % (
        len(procs), fmt(wechat.get("main_pids"))))
    if procs:
        rows.append("| 进程启动时间 | %s |" %
                    "、".join(fmt(p.get("started_at")) for p in procs[:5]))
        rows.append("| 主进程工作集 | %s |" %
                    "、".join(fmt(p.get("working_set_mb")) + " MB" for p in procs[:3]))
    rows.append("| Weixin.dll | 大小=%s / 版本目录=%s / 指纹匹配=%s |" % (
        fmt(core.get("size")), fmt(core.get("version_hint")),
        fmt(core.get("profile_match"))))
    if core.get("matched_versions"):
        rows.append("| 匹配到的 profile | %s |" % fmt(core.get("matched_versions")))
    if core.get("sha256"):
        rows.append("| DLL SHA256 | `%s` |" % core.get("sha256"))
    titles = [w.get("title") for w in (windows.get("windows") or []) if w.get("title")]
    rows.append("| 微信窗口标题 | %s |" % (fmt("、".join(titles[:8])) if titles else "—"))
    rows.append("| 疑似登录/扫码窗口 | %s |" %
                fmt(windows.get("login_window_suspected")))
    rows.append("| 可用物理内存 | %s MB（占用 %s%%） |" % (
        fmt(mem.get("available_mb")), fmt(mem.get("load_percent"))))
    rows.append("| frpc 进程 | %s |" % fmt(net.get("frpc_running")))
    rows.append("| 本机端口 | %s |" % fmt(net.get("ports")))
    rows.append("| 公网出口 IP | %s |" % fmt(net.get("public_ip")))
    rows.append("| 最近系统事件 | %s |" % fmt(system.get("note")))
    rows.append("| 需关注的系统事件 | %s 条 |" %
                len(system.get("relevant_events") or []))
    rows.append("| 辅助信号 | %s |" % ("、".join(signals) if signals else "无"))

    # 自动排除结论
    if core.get("profile_match") is True:
        exclusions.append("✅ **排除客户端升级 / 内存偏移失配**：Weixin.dll 大小与 SHA256 均在已验证指纹表中。")
    elif core.get("profile_match") is False:
        exclusions.append("⚠️ **高度怀疑客户端升级**：DLL 指纹不匹配，应更新 native_profiles 而不是按掉线处理。")
    relevant_events = system.get("relevant_events") or []
    if not relevant_events:
        exclusions.append(
            "✅ **排除系统休眠 / 重启 / 自动维护**：最近 %s 分钟内只有 %d 条常规信息事件，"
            "无电源 / 网络 / 计划任务类关键事件。"
            % (fmt(system.get("window_minutes") or 15), len(system.get("events") or [])))
    else:
        exclusions.append(
            "⚠️ 掉线前后有 %d 条需关注的系统事件（%s），需逐条核对是否触发网络或会话中断。"
            % (len(relevant_events),
               "、".join("%s#%s" % (e.get("provider"), e.get("id"))
                        for e in relevant_events[:6])))
    ports = net.get("ports") or {}
    if net.get("frpc_running") and ports and all("可连接" in str(v) for v in ports.values()):
        exclusions.append("✅ **排除本机服务与内网穿透故障**：frpc 存活，8080/8088 均可连接。")
    elif not net.get("frpc_running") or any("不可连接" in str(v) for v in ports.values()):
        exclusions.append("⚠️ 本机服务或 frp 隧道存在异常，需单独排查（不是微信掉线的典型表现）。")
    # 只看 Weixin.exe 主进程，且取「最老」的那个：登录时拉起的子进程不算主进程重启。
    main_pids = set(wechat.get("main_pids") or [])
    main_procs = [p for p in procs if p.get("pid") in main_pids] or procs
    started = [p.get("started_at") for p in main_procs if p.get("started_at")]
    if started:
        try:
            delta = datetime.now() - datetime.fromisoformat(min(started))
            if delta.total_seconds() > 1800:
                exclusions.append(
                    "✅ **排除微信主进程崩溃/重启**：最老的主进程已运行 %.1f 小时，掉线前后未重启。"
                    % (delta.total_seconds() / 3600))
            else:
                exclusions.append("⚠️ 微信主进程在最近 30 分钟内启动过：可能是重启/更新，需结合 DLL 指纹判断。")
        except ValueError:
            pass
    if "low_memory" in signals:
        exclusions.append("⚠️ 可用内存偏低，可能叠加工作集换页；但 11-09 排查已确认它不是连续三天凌晨掉线的主因。")
    if windows.get("login_window_suspected"):
        exclusions.append("🔎 检测到登录/扫码窗口：**支持「客户端被踢回登录界面」这一判定**。")
    if "wechat_child_process_spawned" in signals:
        exclusions.append("🔎 掉线前后有微信子进程新启动，通常是重新登录时拉起登录组件，"
                          "不代表主进程重启。")
    return rows, exclusions


def log_highlights(payload: dict) -> list[str]:
    keywords = ("微信已退出登录", "等待绑定的机器人账号登录", "微信已连接",
                "原生接收服务停止", "微信核心对象链未就绪", "ERROR", "Traceback")
    lines = (payload.get("snapshot") or {}).get("logs", {}).get("bot_log_tail") or []
    hits = [line for line in lines if any(k in line for k in keywords)]
    return ["    %s" % line for line in hits[-12:]] or ["    （本次快照未捕获到异常行）"]


def render(payload: dict, events: list[dict]) -> str:
    cls = payload.get("classification") or {}
    rows, exclusions = evidence_lines(payload)
    incident_id = payload.get("incident_id") or "未知"
    out = [
        "# 微信状态诊断 · %s" % incident_id,
        "",
        "- 生成时间：%s" % diag.now_iso(),
        "- 触发方式：%s" % fmt(payload.get("trigger")),
        "- 快照文件：`%s`" % fmt(payload.get("_file")),
        "",
        "## 一、结论",
        "",
        "**主分类：`%s`** — %s" % (cls.get("reason"), diag.REASON_TEXT.get(
            cls.get("reason"), "未分类")),
        "",
        "> %s" % fmt(cls.get("summary")),
        "",
    ]
    if cls.get("reason") == "ok":
        out += ["> 本次为**在线基线快照**（手动取证），不是掉线事件；"
                "下次状态翻转时守护会自动生成同类文件，可直接对比。", ""]
    out += [
        "**建议处置**：%s" % (cls.get("suggested_action") or
                            diag.REASON_ACTION.get(cls.get("reason"), "见分类说明")),
        "",
        "### 自动排除项",
        "",
    ]
    out += ["- " + line for line in exclusions]
    out += [
        "",
        "## 二、现场证据",
        "",
    ] + rows + [
        "",
        "## 三、bot.log 关键行（快照时刻回看）",
        "",
        "```text",
    ] + log_highlights(payload) + [
        "```",
        "",
        "## 四、事件时间线（最近 %d 条结构化事件）" % len(events),
        "",
        "| 时间 | 级别 | 事件 | 说明 |",
        "|---|---|---|---|",
    ]
    for ev in events[-25:]:
        detail = ev.get("detail") or ev.get("note") or ev.get("reason") or ""
        out.append("| %s | %s | %s | %s |" % (
            fmt(ev.get("ts")), fmt(ev.get("level")), fmt(ev.get("event")),
            fmt(detail)[:120].replace("|", "/")))
    out += [
        "",
        "## 五、判定口径（避免再次误判）",
        "",
        "1. **进程未重启 + DLL 指纹不变 + 服务/frp 全正常 + 出现登录窗口** → 微信服务端使 PC 会话失效，"
        "提示「为了您的账号安全，请重新登陆」，必须人工重新登录；窗口唤醒无效。",
        "2. **DLL 指纹不匹配** → 客户端升级，需更新 `vendor/helper-main/wxbot_framework/native_profiles.py`。",
        "3. **系统事件有休眠/重启、frpc 断连、端口不通** → 属于本机/网络原因，按对应项修复。",
        "4. **可用内存 < 600MB** → 只是可能的叠加因素，不要单独归因（2026-11-09 排查已证伪其为连续掉线主因）。",
        "5. 需要确认服务端动机时，看手机微信「登录设备管理 / 微信团队通知」，"
        "或在快照里开启 `WX_BRIDGE_PUBLIC_IP=1` 记录出口 IP 变化。",
        "",
        "## 六、复盘入口",
        "",
        "- 全部快照：`wechat-report-bridge/diagnostics/incidents/`",
        "- 结构化事件流：`wechat-report-bridge/diagnostics/events/events-YYYY-MM-DD.jsonl`",
        "- 人类可读日志：`wechat-report-bridge/wechat_guard.log`（2MB 轮转）",
        "- 历史基线：`wechat-report-bridge/reports/2026-11-09_微信掉线排查报告.md`",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="微信掉线取证复盘")
    ap.add_argument("--list", action="store_true", help="列出所有事件快照")
    ap.add_argument("--incident", help="指定事件 ID，默认分析最新一次")
    ap.add_argument("--events", type=int, default=0, help="只打印最近 N 条结构化事件")
    ap.add_argument("--no-write", action="store_true", help="不写 Markdown，只打印摘要")
    args = ap.parse_args()

    incidents = list_incidents()
    if args.list:
        if not incidents:
            print("暂无事件快照。可先执行 python wechat_guard.py --snapshot 手动抓一份。")
            return 0
        for path in incidents:
            try:
                data = load(path)
                cls = data.get("classification") or {}
                print("%s  %-20s  %s" % (path.stem, cls.get("reason"),
                                        data.get("trigger")))
            except (OSError, ValueError):
                print("%s  <读取失败>" % path.stem)
        return 0

    if args.events:
        rows = diag.event_tail(args.events)
        for row in rows:
            print("%s  %-8s %-18s %s" % (row.get("ts"), row.get("level"),
                                         row.get("event"),
                                         row.get("detail") or row.get("note")
                                         or row.get("reason") or ""))
        return 0

    if not incidents:
        print("暂无事件快照。可先执行 python wechat_guard.py --snapshot 手动抓一份，"
              "或等待下次掉线由守护自动生成。")
        return 0

    path = None
    if args.incident:
        path = diag.INCIDENT_DIR / (args.incident + ".json")
        if not path.is_file():
            print("找不到事件：%s" % args.incident)
            return 1
    else:
        path = incidents[-1]
    try:
        payload = load(path)
    except (OSError, ValueError) as exc:
        print("读取快照失败：%s" % exc)
        return 1
    payload["_file"] = str(path)

    events = diag.event_tail(40)
    report = render(payload, events)
    cls = payload.get("classification") or {}
    print("事件 %s | 主分类 %s | 辅助信号 %s" %
          (payload.get("incident_id"), cls.get("reason"),
           "、".join(cls.get("signals") or []) or "无"))
    print("结论：%s" % cls.get("summary"))
    print("建议：%s" % cls.get("suggested_action"))
    for line in evidence_lines(payload)[1]:
        print(line)

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / ("掉线诊断_%s.md" % payload.get("incident_id"))
        out.write_text(report, encoding="utf-8")
        print("诊断报告已写入：%s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
