"""飞书自定义机器人告警通道（纯 Python 标准库，零第三方依赖）。

对外接口（health_monitor.py 使用）：
    from feishu_notifier import Notifier
    n = Notifier()
    n.raise_event("wechat", "critical", "微信连接断开", "……")   # 触发/持续提醒
    n.clear_event("wechat", "微信连接已恢复", "……")             # 恢复通知
    n.send("info", "标题", "正文")                              # 一次性消息
    n.event_active("wechat")                                   # 事件是否仍未恢复

命令行：
    python feishu_notifier.py --test
    python feishu_notifier.py --title "标题" --text "正文" --level warning
    python feishu_notifier.py --status

设计要点：
  * 「事件」有状态：首次发生立刻发，未恢复期间按递增间隔提醒，恢复时发一条绿色通知。
    这样既不会每 60s 刷屏，也不会像上次那样掉线 12 小时无人知晓。
  * 所有状态落在 alert_state.json，监控重启后仍能记得「上次告警还没恢复」。
  * 任何网络/文件异常都只记日志不抛给调用方，绝不能把监控主循环带崩。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "alert.local.json"
EXAMPLE_FILE = ROOT / "alert.example.json"
STATE_FILE = ROOT / "alert_state.json"
LOG_FILE = ROOT / "alert.log"

DEFAULT_CONFIG = {
    "webhook": "",
    "secret": "",
    "enabled": True,
    "timeout_seconds": 6,
    "retries": 3,
    "repeat_schedule_minutes": [10, 30, 60, 120, 240],
    "mention_all_levels": [],
    "_note": (
        "webhook 为飞书群自定义机器人地址；若开启签名校验，把密钥填到 secret。"
        "repeat_schedule_minutes 是同一故障未恢复时的递增提醒间隔（分钟）。"
        "其余巡检参数见 alert.example.json。"
    ),
}

# 飞书卡片头颜色：严重红 / 警告橙 / 提示蓝 / 恢复绿
HEADER_COLORS = {
    "critical": "red",
    "warning": "orange",
    "info": "blue",
    "recovered": "green",
}

_LOG_READY = False


def get_logger(name="Notifier"):
    """共用一个按天追加的 alert.log，避免多模块重复挂 handler。"""
    global _LOG_READY
    logger = logging.getLogger("WxAlert.%s" % name)
    if not _LOG_READY:
        logging.getLogger("WxAlert").setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s",
                                "%Y-%m-%d %H:%M:%S")
        try:
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(fmt)
            logging.getLogger("WxAlert").addHandler(fh)
        except OSError:
            pass
        _LOG_READY = True
    return logger


log = get_logger("Notifier")


def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    source = path or (CONFIG_FILE if CONFIG_FILE.is_file() else EXAMPLE_FILE)
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})
    except (OSError, ValueError) as exc:
        log.warning("读取告警配置失败（改用默认值）：%s", exc)
    cfg["_source"] = str(source)
    return cfg


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return "%d 分钟" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%d 小时 %d 分钟" % (hours, minutes) if minutes else "%d 小时" % hours
    days, hours = divmod(hours, 24)
    return "%d 天 %d 小时" % (days, hours) if hours else "%d 天" % days


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and isinstance(data.get("events"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"events": {}, "log_offsets": {}, "updated": ""}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Notifier:
    """发送 + 事件去重。send() 永不抛异常，失败返回 False。"""

    def __init__(self, config: dict | None = None):
        self.cfg = config or load_config()
        self.state = _load_state()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------- 底层发送 ----------
    def _build_payload(self, level: str, title: str, body: str) -> dict:
        mention = level in (self.cfg.get("mention_all_levels") or [])
        content = body
        if mention:
            content += "\n\n<at id=all></at>"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": HEADER_COLORS.get(level, "blue"),
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": "微信工具 · 故障告警 | %s | %s" % (_stamp(), os.environ.get("COMPUTERNAME", "PC"))}
                ]},
            ],
        }
        payload = {"msg_type": "interactive", "card": card}
        secret = str(self.cfg.get("secret") or "").strip()
        if secret:
            ts = str(int(time.time()))
            sign = base64.b64encode(
                hmac.new(("%s\n%s" % (ts, secret)).encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
            payload["timestamp"] = ts
            payload["sign"] = sign
        return payload

    def send(self, level: str, title: str, body: str) -> bool:
        webhook = str(self.cfg.get("webhook") or "").strip()
        if not self.cfg.get("enabled", True):
            log.info("告警已关闭，跳过发送：%s", title)
            return False
        if not webhook.startswith("https://"):
            log.error("未配置有效的飞书 webhook，无法发送：%s", title)
            return False

        payload = self._build_payload(level, title, body)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timeout = max(2, int(self.cfg.get("timeout_seconds", 6)))
        retries = max(1, int(self.cfg.get("retries", 3)))
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(
                    webhook, data=data,
                    headers={"Content-Type": "application/json; charset=utf-8",
                             "User-Agent": "WxBridge-Alert/1.0"},
                    method="POST")
                with self._opener.open(req, timeout=timeout) as resp:
                    text = resp.read(2048).decode("utf-8", "replace")
                try:
                    result = json.loads(text)
                except ValueError:
                    result = {}
                code = result.get("code", result.get("StatusCode", 0))
                if code in (0, None):
                    log.info("飞书告警已发送 [%s] %s", level, title)
                    return True
                log.error("飞书返回错误（第 %d 次）：%s", attempt, text[:300])
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.error("飞书发送失败（第 %d/%d 次）：%s: %s",
                          attempt, retries, type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(min(8, 1.5 * attempt))
        log.error("飞书告警最终失败：%s", title)
        return False

    # ---------- 事件级去重 / 恢复 ----------
    def event_active(self, key: str) -> bool:
        return bool(self.state["events"].get(key, {}).get("active"))

    def save(self) -> None:
        try:
            self.state["updated"] = _stamp()
            _atomic_write(STATE_FILE, self.state)
        except OSError as exc:
            log.warning("保存告警状态失败：%s", exc)

    def raise_event(self, key: str, level: str, title: str, body: str,
                    repeat_seconds: float | None = None) -> bool:
        """故障仍在持续：首次立刻发，之后按递增间隔提醒；发送失败下次循环会重试。"""
        try:
            # 守护直报与巡检进程共用同一个状态文件：每次事件操作前重新读盘，
            # 否则常驻巡检会拿着启动时的旧快照，把守护刚发过的告警重复发一遍。
            self.state = _load_state()
            now = time.time()
            ev = self.state["events"].get(key) or {}
            if ev.get("active"):
                count = int(ev.get("count") or 1)
                schedule = self.cfg.get("repeat_schedule_minutes") or [10, 30, 60, 120, 240]
                interval = (repeat_seconds if repeat_seconds is not None
                            else float(schedule[min(count - 1, len(schedule) - 1)]) * 60)
                if now - float(ev.get("last_sent") or 0) < interval:
                    return False
                title = "%s（仍未恢复 · 第 %d 次提醒）" % (title, count + 1)
                body = ("**首次发生**：%s\n**已持续**：%s\n\n%s"
                        % (ev.get("first_at", "未知"),
                           human_duration(now - float(ev.get("first_ts") or now)), body))
                first_at = ev.get("first_at") or _stamp()
                first_ts = float(ev.get("first_ts") or now)
                count += 1
                sent = self.send(level, title, body)
            else:
                first_at, first_ts, count, sent = _stamp(), now, 1, self.send(level, title, body)

            self.state["events"][key] = {
                "active": True,
                "first_at": first_at,
                "first_ts": first_ts,
                "last_sent": now if sent else 0,
                "count": count,
                "level": level,
                "last_title": title,
                "resolved_at": None,
            }
            self.save()
            return sent
        except Exception as exc:                       # 告警模块绝不能拖垮监控
            log.exception("raise_event 异常（忽略）：%s", exc)
            return False

    def clear_event(self, key: str, title: str, body: str = "") -> bool:
        """故障已恢复：只在之前确实告警过时才发一条绿色通知。"""
        try:
            self.state = _load_state()          # 同 raise_event：跨进程读取最新事件状态
            ev = self.state["events"].get(key) or {}
            if not ev.get("active"):
                return False
            duration = human_duration(time.time() - float(ev.get("first_ts") or time.time()))
            full = ("**故障已恢复**：%s\n**持续时长**：%s\n**告警次数**：%s\n\n%s"
                    % (ev.get("last_title", key), duration, ev.get("count", 1), body))
            sent = self.send("recovered", title, full)
            if sent:
                ev.update(active=False, resolved_at=_stamp(), last_sent=time.time())
                self.state["events"][key] = ev
                self.save()
            else:
                ev["last_sent"] = 0
                self.state["events"][key] = ev
                self.save()
            return sent
        except Exception as exc:
            log.exception("clear_event 异常（忽略）：%s", exc)
            return False


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书告警通道")
    ap.add_argument("--test", action="store_true", help="发送一条测试卡片")
    ap.add_argument("--title", default="", help="自定义消息标题")
    ap.add_argument("--text", default="", help="自定义消息正文（支持换行 \\n）")
    ap.add_argument("--level", default="info", choices=list(HEADER_COLORS))
    ap.add_argument("--status", action="store_true", help="查看当前未恢复的告警事件")
    args = ap.parse_args()

    n = Notifier()
    if args.status:
        events = {k: v for k, v in n.state["events"].items() if v.get("active")}
        print(json.dumps({"config": n.cfg["_source"], "active_events": events},
                         ensure_ascii=False, indent=2))
        return 0
    if args.test:
        body = ("**通道**：飞书自定义机器人（卡片消息）\n"
                "**配置**：%s\n**时间**：%s\n\n"
                "收到这条消息，说明微信工具现在可以把掉线与其它故障推到这个群了。"
                % (Path(n.cfg["_source"]).name, _stamp()))
        ok = n.send("info", "✅ 飞书告警通道测试", body)
        print("测试消息发送%s" % ("成功" if ok else "失败，详见 alert.log"))
        return 0 if ok else 1
    if args.title:
        ok = n.send(args.level, args.title, args.text.replace("\\n", "\n"))
        print("发送%s" % ("成功" if ok else "失败，详见 alert.log"))
        return 0 if ok else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
