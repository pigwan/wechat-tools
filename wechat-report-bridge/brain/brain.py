"""拟人大脑：独立旁路应用，和飞书告警同一层级。

只读 server/messages.sqlite3 的消息，经拟人策略决策后，
把回复写进 vendor/helper-main/.runtime/send_outbox.sqlite3。
不开端口、不碰 hook/插件、不需要令牌，关掉进程即恢复现状。

用法：
    python brain.py                 # 常驻轮询
    python brain.py --selftest      # 自检：只检查配置/库/LLM 连通，不发消息
    python brain.py --once          # 扫一轮新消息，打印决策，dry_run 不写队列
    python brain.py --status        # 打印当前状态与决策统计
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memory as memory_mod
CONFIG = HERE / "config.local.json"
GROUPS_LOCAL = HERE / "groups.local.json"   # 网页勾选的群白名单（是否启用的唯一来源）
RUNTIME_LOCAL = HERE / "runtime.local.json"  # 网页运行开关（enabled / dry_run）
PERSONA_LOCAL = HERE / "persona.local.json"  # 网页端人设（默认 + 按群覆盖）
TUNING_LOCAL = HERE / "tuning.local.json"    # 网页端节奏参数（概率/冷却/上限），全局覆盖
_MUTEX_HANDLE = None

# 网页勾选的群若在 config.groups 里没有专属参数，用这套默认值
DEFAULT_GROUP_TEMPLATE = {
    "persona": "你是群里一个爱接话、说话简短口语化的成员。不要自称 AI、机器人或助手。回复控制在 1-3 句，偶尔用一两个语气词。",
    "personality": "normal",
    "reply_probability": 0.28,
    "context_limit": 10,
    "quiet_hours": [[0, 8]],
    "cooldown_seconds": 90,
    "max_replies_per_10min": 6,
    # 跟进窗口：接上一句之后，这段时间内算「还在同一段对话里」，
    # 用更高的概率和更短的冷却继续跟，接满 follow_max_turns 句就交回给人。
    # 没有这一段的话，每句话都按开场概率重掷，聊到一半突然不说话，很假。
    "follow_window_seconds": 180,
    "follow_probability": 0.85,
    "follow_max_turns": 4,
    "follow_cooldown_seconds": 6,
    "energy": {"base": 0.7, "drain_per_reply": 0.06, "recover_per_minute": 0.01, "min": 0.15},
    "at_reply": True,          # 被@默认必回；群配置或网页开关可关掉
    "trigger_words": [],
}

# 决策原因常量，写进决策日志便于复盘
DECISION_AT = "at_reply"
DECISION_TRIGGER = "trigger"
DECISION_PROB = "probability"
DECISION_QUIET = "quiet_hours"
DECISION_COOLDOWN = "cooldown"
DECISION_RATE = "rate_limit"
DECISION_SKIP = "skip"
DECISION_DRY = "dry_run"


LOG_FILE = HERE / "brain.log"
_LOG_MAX_BYTES = 2 * 1024 * 1024


def _rotate_log() -> None:
    """超过 2MB 就滚动一份，只保留上一份，避免长期跑把盘占满。"""
    try:
        backup = LOG_FILE.with_name("brain.log.1")
        if backup.is_file():
            backup.unlink()
        LOG_FILE.rename(backup)
    except OSError:
        pass


def log(msg: str) -> None:
    """同时输出到控制台和 brain.log —— 网页端靠这个看运行日志（静默启动时也有记录）。"""
    line = "[brain %s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        if LOG_FILE.is_file() and LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
            _rotate_log()
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # 日志写盘失败绝不能影响主流程


# --------------------------------------------------------------------------- 配置
def load_config(path: Path = CONFIG) -> dict:
    if not path.is_file():
        raise SystemExit("缺少配置文件：%s（请从 config.example.json 复制）" % path)
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))

    def resolve(value: str) -> Path:
        p = Path(value)
        return (HERE / p).resolve() if not p.is_absolute() else p

    paths = cfg.get("paths", {})
    cfg["_resolved"] = {k: resolve(v) for k, v in paths.items() if isinstance(v, str)}
    _apply_runtime_switches(cfg)
    _apply_group_whitelist(cfg)
    _apply_persona(cfg)
    _apply_tuning(cfg)
    return cfg


def load_persona_file() -> dict:
    """读网页端保存的人设文件。读不到就返回空字典，绝不因此启动失败。"""
    if not PERSONA_LOCAL.is_file():
        return {}
    try:
        data = json.loads(PERSONA_LOCAL.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _apply_persona(cfg: dict) -> None:
    """网页人设 > config.local.json 里的 persona。

    优先级：persona.local.json 的 groups[群] > persona.local.json 的 default
    > config 里该群的 persona / 模板默认。
    白名单已经把模板 persona 填进每个群，所以网页默认人设必须**压过**它，
    否则网页怎么填都不会生效。想恢复某群自己的设定，用「该群改用默认」清掉覆盖。
    """
    data = load_persona_file()
    default = data.get("default")
    if isinstance(default, str) and default.strip():
        default = default.strip()
        cfg["global_persona"] = default
        for gcfg in cfg.get("groups", {}).values():
            if isinstance(gcfg, dict):
                gcfg["persona"] = default
    else:
        cfg["global_persona"] = str(cfg.get("global_persona") or "").strip()
    cfg["_persona_source"] = "web" if data else "config"
    overrides = data.get("groups")
    if not isinstance(overrides, dict):
        return
    for chat_id, text in overrides.items():
        gcfg = cfg.get("groups", {}).get(chat_id)
        if isinstance(gcfg, dict) and isinstance(text, str) and text.strip():
            gcfg["persona"] = text.strip()


TUNING_KEYS = ("reply_probability", "cooldown_seconds", "max_replies_per_10min",
               "follow_window_seconds", "follow_probability", "follow_max_turns",
               "follow_cooldown_seconds")


def load_tuning_file() -> dict:
    """读网页端保存的节奏参数。读不到就返回空字典，绝不因此启动失败。"""
    if not TUNING_LOCAL.is_file():
        return {}
    try:
        data = json.loads(TUNING_LOCAL.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _apply_tuning(cfg: dict) -> None:
    """网页节奏参数覆盖所有群（概率/冷却/10 分钟上限）。

    这几项是「整体性格」层面的旋钮，逐群微调仍可写在 config.groups 里，
    但网页一旦保存就以网页为准——否则用户改完发现没反应。
    """
    data = load_tuning_file()
    applied = {k: data[k] for k in TUNING_KEYS if isinstance(data.get(k), (int, float))
               and not isinstance(data[k], bool)}
    cfg["_tuning_source"] = "web" if applied else "config"
    if not applied:
        return
    for gcfg in cfg.get("groups", {}).values():
        if isinstance(gcfg, dict):
            gcfg.update(applied)


def _apply_runtime_switches(cfg: dict) -> None:
    """网页运行开关优先：runtime.local.json 存在时用它的 enabled/dry_run 覆盖 config；
    未设置过时回退 config.local.json 的值（向后兼容）。"""
    cfg["_runtime_source"] = "config"
    if not RUNTIME_LOCAL.is_file():
        return
    try:
        data = json.loads(RUNTIME_LOCAL.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if "enabled" in data and isinstance(data["enabled"], bool):
        cfg["enabled"] = data["enabled"]
    if "dry_run" in data and isinstance(data["dry_run"], bool):
        cfg["dry_run"] = data["dry_run"]
    if "dry_run_llm" in data and isinstance(data["dry_run_llm"], bool):
        cfg["dry_run_llm"] = data["dry_run_llm"]
    if "at_reply" in data and isinstance(data["at_reply"], bool):
        cfg["at_reply"] = data["at_reply"]
    cfg["_runtime_source"] = "web"


def load_group_whitelist() -> dict | None:
    """读网页勾选的群白名单。返回 {"enabled":[chat_id...], "names":{...}}；
    文件不存在或格式损坏时返回 None——表示"网页未设置过"，此时回退到 config.groups 的 enabled。"""
    if not GROUPS_LOCAL.is_file():
        return None
    try:
        data = json.loads(GROUPS_LOCAL.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    enabled = [str(x) for x in data.get("enabled", []) if str(x).strip()]
    names = data.get("names", {})
    if not isinstance(names, dict):
        names = {}
    return {"enabled": list(dict.fromkeys(enabled)), "names": names}


def _apply_group_whitelist(cfg: dict) -> None:
    """把网页白名单合成进 cfg['groups']：
    - 网页设置过 → 只有勾选的群生效，参数取 config.groups 专属配置（无则用默认模板）；
    - 网页没设置 → 保持 config.local.json 里 enabled=true 的旧行为（向后兼容）。"""
    wl = load_group_whitelist()
    cfg["_group_source"] = "web" if wl is not None else "config"
    if wl is not None:
        templates = cfg.get("groups", {})
        merged: dict[str, dict] = {}
        for chat_id in wl["enabled"]:
            base = dict(DEFAULT_GROUP_TEMPLATE)
            override = templates.get(chat_id)
            if isinstance(override, dict):
                base.update(override)
            base["enabled"] = True   # 白名单里的群强制启用，忽略模板里的 enabled
            if chat_id in wl["names"]:
                base["name"] = str(wl["names"][chat_id])
            merged[chat_id] = base
        cfg["groups"] = merged
    # 网页的「被@必回」是全局开关：显式设置过就覆盖所有群（含 config 里逐群写的值）
    global_at = cfg.get("at_reply")
    if isinstance(global_at, bool):
        for gcfg in cfg.get("groups", {}).values():
            if isinstance(gcfg, dict):
                gcfg["at_reply"] = global_at


# --------------------------------------------------------------------------- 互斥体
def acquire_mutex() -> bool:
    """Windows 命名互斥体，防止计划任务和手动双击起两个大脑进程。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, True, "Local\\WxBridgeBrain")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError) as exc:
        log("互斥体创建失败，按单实例继续：%s" % exc)
        return True


# --------------------------------------------------------------------------- 状态库
def init_state(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS group_state (
        chat_id TEXT PRIMARY KEY,
        last_reply_ts REAL NOT NULL DEFAULT 0,
        energy REAL NOT NULL DEFAULT 0.7,
        energy_ts REAL NOT NULL DEFAULT 0,
        recent_replies TEXT NOT NULL DEFAULT '[]',
        session_turns INTEGER NOT NULL DEFAULT 0,
        session_ts REAL NOT NULL DEFAULT 0)""")
    # 老库补列：session_* 是「跟进窗口」用的，缺了不会报错，只是当 0 处理
    have = {row[1] for row in conn.execute("PRAGMA table_info(group_state)")}
    for col, ddl in (("session_turns", "INTEGER NOT NULL DEFAULT 0"),
                     ("session_ts", "REAL NOT NULL DEFAULT 0")):
        if col not in have:
            conn.execute("ALTER TABLE group_state ADD COLUMN %s %s" % (col, ddl))
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        chat_id TEXT NOT NULL,
        msg_key TEXT NOT NULL,
        sender_name TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        reply TEXT NOT NULL DEFAULT '')""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts)")
    conn.commit()
    # 记忆表与状态库同一个文件，但由 memory 模块独立管理 schema
    memory_mod.init_memory(str(path))
    return conn


def get_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='cursor_rowid'").fetchone()
    return int(row[0]) if row else 0


def set_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES('cursor_rowid',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
    conn.commit()


def skip_backlog(state_conn: sqlite3.Connection, msg_conn: sqlite3.Connection,
                 reason: str) -> int:
    """把游标直接推到消息库最新，跳过大脑停机/待机期间积压的旧消息。

    重新开启大脑时用户要的是「从此刻开始看」，不是把关着那几个小时的群聊一次性补回来；
    这些消息早就过去了，补读既刷屏又容易对已经聊完的话题插嘴。返回跳过的条数。
    """
    latest = int(msg_conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0] or 0)
    cursor = get_cursor(state_conn)
    if latest <= cursor:
        return 0
    set_cursor(state_conn, latest)
    log("%s：跳过 %d 条旧消息（游标 %d → %d），只处理此后的新消息" % (
        reason, latest - cursor, cursor, latest))
    return latest - cursor


def get_group(conn: sqlite3.Connection, chat_id: str, base: float) -> dict:
    row = conn.execute("SELECT * FROM group_state WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO group_state(chat_id,energy,energy_ts) VALUES(?,?,?)",
                     (chat_id, base, time.time()))
        conn.commit()
        return {"last_reply_ts": 0.0, "energy": base, "energy_ts": time.time(),
                "recent_replies": [], "session_turns": 0, "session_ts": 0.0}
    return {
        "last_reply_ts": float(row["last_reply_ts"]),
        "energy": float(row["energy"]),
        "energy_ts": float(row["energy_ts"]),
        "recent_replies": json.loads(row["recent_replies"] or "[]"),
        "session_turns": int(row["session_turns"] or 0),
        "session_ts": float(row["session_ts"] or 0.0),
    }


def save_group(conn: sqlite3.Connection, chat_id: str, state: dict) -> None:
    conn.execute("""INSERT INTO group_state(chat_id,last_reply_ts,energy,energy_ts,recent_replies,
        session_turns,session_ts) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
        last_reply_ts=excluded.last_reply_ts, energy=excluded.energy,
        energy_ts=excluded.energy_ts, recent_replies=excluded.recent_replies,
        session_turns=excluded.session_turns, session_ts=excluded.session_ts""",
        (chat_id, state["last_reply_ts"], state["energy"], state["energy_ts"],
         json.dumps(state["recent_replies"], ensure_ascii=False),
         int(state.get("session_turns", 0) or 0), float(state.get("session_ts", 0.0) or 0.0)))
    conn.commit()


def record_decision(conn: sqlite3.Connection, chat_id: str, msg, decision: str,
                    reason: str = "", reply: str = "") -> None:
    conn.execute("""INSERT INTO decisions(ts,chat_id,msg_key,sender_name,text,decision,reason,reply)
        VALUES(?,?,?,?,?,?,?,?)""",
        (time.time(), chat_id, msg["msg_key"], msg["sender_name"] or "",
         (msg["text"] or "")[:500], decision, reason, (reply or "")[:2000]))
    conn.commit()


# --------------------------------------------------------------------------- LLM 调用
LLM_LOCAL = HERE / "llm.local.json"
LLM_FALLBACK = HERE.parent.parent / "vendor" / "helper-main" / "llm_config.json"


def llm_config_dict(cfg: dict | None = None) -> dict:
    """配置优先级：网页设置的 llm.local.json > config 里指定的文件 > 现有插件 llm_config.json。

    网页端（管理台 /api/brain/llm）写入的是 llm.local.json，这里只读它。
    """
    # 1) 网页设置写入的私有配置
    if LLM_LOCAL.is_file():
        try:
            data = json.loads(LLM_LOCAL.read_text(encoding="utf-8-sig"))
            if str(data.get("api_key", "")).strip():
                return data
        except (OSError, ValueError):
            pass
    # 2) config.local.json 里显式指定的路径（兼容旧用法）
    if cfg and cfg.get("_resolved", {}).get("llm_config"):
        p = cfg["_resolved"]["llm_config"]
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
    # 3) 现有插件配置（api/key 通常为空）
    if LLM_FALLBACK.is_file():
        try:
            return json.loads(LLM_FALLBACK.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            pass
    return {}


class BrainLLM:
    """走 OpenAI 兼容接口拿纯文本回复；配置来自网页设置或本地文件。"""

    def __init__(self, data: dict):
        base = str(data.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self.api_key = str(data.get("api_key", ""))
        self.model = str(data.get("model", "deepseek-flash"))
        self.timeout = float(data.get("timeout_seconds", 20))
        self.max_tokens = int(data.get("max_tokens", 512))
        if not self.api_key or "CHANGE" in self.api_key.upper():
            raise ValueError("api_key 未配置（请在网页设置里填写）")

    def reply(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.model in ("deepseek-chat", "deepseek-flash", "deepseek-v4-pro"):
            payload["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            result = json.loads(resp.read(1024 * 1024 + 1))
        content = result["choices"][0]["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("大模型返回空正文")
        return content.strip()


class MockLLM:
    """dry-run 且明确关掉"演练也调模型"时的本地模拟回复，不调用任何接口。"""

    def reply(self, system: str, user: str) -> str:
        return "[演练回复] 收到，我先看看。（未调用大模型，可在网页勾选“演练也调模型”）"


def build_llm(cfg: dict, force_reply: bool = False) -> tuple[object, str]:
    """按开关决定用真模型还是本地模拟文本，返回 (llm, 说明)。

    - 实发模式：必须真模型，没配 key 就让异常抛出去（绝不能静默降级成假回复）
    - 演练 + dry_run_llm=true（默认）：同样调真模型，只是结果只写日志、不入队
    - 演练 + dry_run_llm=false：走本地模拟文本，省 token
    """
    dry_run = bool(cfg.get("dry_run", True))
    want_llm = (not dry_run) or force_reply or bool(cfg.get("dry_run_llm", True))
    if not want_llm:
        return MockLLM(), "dry-run：用本地模拟文本（未调用大模型；网页勾选“演练也调模型”可看真实回复）"
    try:
        llm = BrainLLM(llm_config_dict(cfg))
    except ValueError as exc:
        if dry_run and not force_reply:
            return MockLLM(), "dry-run：无法调用大模型（%s），回退本地模拟文本" % exc
        raise
    return llm, ("dry-run：真模型生成，仅记录不回发" if dry_run
                 else "实发：真模型生成并发送")


def build_context(messages_conn: sqlite3.Connection, chat_id: str, limit: int) -> str:
    rows = messages_conn.execute(
        "SELECT sender_name,text FROM messages WHERE chat_id=? "
        "AND text IS NOT NULL AND trim(text)<>'' ORDER BY rowid DESC LIMIT ?",
        (chat_id, limit)).fetchall()
    lines = ["%s：%s" % (r[0] or "某人", r[1]) for r in reversed(rows)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- 拟人决策
def in_quiet_hours(quiet_hours: list, now: datetime) -> bool:
    hour = now.hour
    for window in quiet_hours:
        if isinstance(window, (list, tuple)) and len(window) == 2:
            start, end = int(window[0]), int(window[1])
            if start <= end:
                if start <= hour < end:
                    return True
            elif hour >= start or hour < end:  # 跨午夜
                return True
    return False


def update_energy(state: dict, gcfg: dict, now_ts: float) -> None:
    ecfg = gcfg.get("energy", {})
    recover = float(ecfg.get("recover_per_minute", 0.01))
    elapsed_min = max(0.0, (now_ts - state["energy_ts"]) / 60.0)
    state["energy"] = min(1.0, state["energy"] + recover * elapsed_min)
    state["energy_ts"] = now_ts


def decide(msg: dict, gcfg: dict, state: dict, now_ts: float, now: datetime,
           self_wxid: str) -> tuple[str, str]:
    """返回 (decision, reason)。decision ∈ reply / skip。"""
    text = (msg["text"] or "").strip()

    # 跳过自己发的
    if self_wxid and msg["sender_id"] == self_wxid:
        return DECISION_SKIP, "self"
    # 机器人自己生成的消息，避免自问自答
    if not text:
        return DECISION_SKIP, "empty"

    at_names = gcfg.get("_at_names", [])
    is_at = any(("@" + name) in text for name in at_names if name) if at_names else False

    # 被@：必定回复（若开启），且不受冷却/概率限制
    if is_at and gcfg.get("at_reply", False):
        return DECISION_AT, "at_mentioned"

    # 关键词触发
    for word in gcfg.get("trigger_words", []):
        if word and word in text:
            return DECISION_TRIGGER, "word:" + word

    # 安静时段：不主动插话（被@已在上文放行）
    if in_quiet_hours(gcfg.get("quiet_hours", []), now):
        return DECISION_QUIET, "quiet"

    # 是不是还在刚才那一段对话里：刚说过话 + 窗口没超时 + 没连说太多次
    window = float(gcfg.get("follow_window_seconds", 180))
    turns = int(state.get("session_turns", 0) or 0)
    since_self = now_ts - float(state.get("session_ts", 0.0) or 0.0)
    in_session = (turns > 0 and since_self < window
                  and turns < int(gcfg.get("follow_max_turns", 4)))

    # 冷却：跟随时用短冷却，开场用长冷却
    cooldown = float(gcfg.get("follow_cooldown_seconds", 6) if in_session
                     else gcfg.get("cooldown_seconds", 90))
    if now_ts - state["last_reply_ts"] < cooldown:
        return DECISION_COOLDOWN, ("cooldown_follow" if in_session else "cooldown")

    # 频率上限：recent_replies 存的是最近主动回复的时间戳
    recent = [t for t in state["recent_replies"] if now_ts - t < 600]
    if len(recent) >= int(gcfg.get("max_replies_per_10min", 6)):
        return DECISION_RATE, "rate_limit"

    # 概率 × 精力；跟随时用更高的 follow_probability，才接得上话
    base = float(gcfg.get("follow_probability", 0.85) if in_session
                 else gcfg.get("reply_probability", 0.28))
    prob = base * float(state["energy"])
    if random.random() < prob:
        return DECISION_PROB, ("follow p=%.2f turn=%d" % (prob, turns + 1)
                               if in_session else "p=%.2f" % prob)
    return DECISION_SKIP, "probability_miss"


# --------------------------------------------------------------------------- 发送
def enqueue_reply(outbox: Path, chat_id: str, text: str) -> int:
    conn = sqlite3.connect(str(outbox), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        text TEXT NOT NULL,
        at_list TEXT NOT NULL DEFAULT '',
        status INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT '')""")
    cur = conn.execute(
        "INSERT INTO outbox(chat_id,text,at_list,status,created_at) VALUES(?,?,?,0,?)",
        (chat_id, text, "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    rid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return rid


# 待发队列：(到期时间戳, chat_id, 文本)。拟人延迟靠它实现——
# 主循环每轮只把到期的写进发送队列，绝不 sleep 阻塞扫描，
# 这样群里连来五条时不会一条条串行排队（旧实现第一条就卡 2-7 秒）。
_PENDING_SENDS: list[tuple[float, str, str]] = []


def split_reply(cfg: dict, reply: str) -> list[str]:
    """长回复按句末标点拆条，避免一大段砸过去。"""
    rate = cfg.get("rate", {})
    chunks = [reply]
    if rate.get("split_long_reply", True) and len(reply) > int(rate.get("split_threshold", 120)):
        # 优先按句末标点切，切不动就整段发
        parts, buf = [], ""
        for ch in reply:
            buf += ch
            if ch in "。！？!?~～" and len(buf) >= 12:
                parts.append(buf.strip())
                buf = ""
        if buf.strip():
            parts.append(buf.strip())
        chunks = [p for p in parts if p] or [reply]
    return chunks


def reply_delay(cfg: dict, is_at: bool) -> float:
    """被@时用更短的"拿手机"延迟；主动插话可以慢一点，像路过瞄一眼。"""
    rate = cfg.get("rate", {})
    if is_at:
        lo = float(rate.get("at_min_delay_seconds", 1.0))
        hi = float(rate.get("at_max_delay_seconds", 2.5))
    else:
        lo = float(rate.get("min_reply_delay_seconds", 2))
        hi = float(rate.get("max_reply_delay_seconds", 7))
    return random.uniform(min(lo, hi), max(lo, hi))


def deliver_reply(cfg: dict, chat_id: str, reply: str, logger, is_at: bool = False) -> int:
    """拆句并按拟人延迟排进内存待发队列，立即返回排程条数（不睡、不阻塞）。

    dry_run 只打印预览；实发由主循环的 flush_pending() 到期写进发送队列。
    """
    chunks = split_reply(cfg, reply)
    delay = reply_delay(cfg, is_at)
    if cfg.get("dry_run", True):
        # 演练：只报"本来会怎么发"
        logger("dry-run 预览：拟延迟 %.1fs 后分 %d 条发出 → %s" % (
            delay, len(chunks), " | ".join(c[:30] for c in chunks[:4])))
        return 0
    gap = float(cfg.get("rate", {}).get("split_delay_seconds", 3))
    base = time.time() + delay
    for i, chunk in enumerate(chunks):
        _PENDING_SENDS.append((base + i * gap, chat_id, chunk))
    logger("已排程 %d 条：首条 %.1fs 后发出%s" % (
        len(chunks), delay, "（@回复）" if is_at else ""))
    return len(chunks)


def flush_pending(cfg: dict, logger=log) -> int:
    """把到期的待发消息真正写进发送队列（每轮主循环调用）。"""
    if not _PENDING_SENDS:
        return 0
    outbox = cfg["_resolved"]["outbox_db"]
    now = time.time()
    sent = 0
    for item in sorted(_PENDING_SENDS, key=lambda x: x[0]):
        if item[0] > now:
            break
        _PENDING_SENDS.remove(item)
        _, chat_id, chunk = item
        try:
            rid = enqueue_reply(outbox, chat_id, chunk)
            logger("已入队 id=%d：%s" % (rid, chunk[:40]))
            sent += 1
        except (OSError, sqlite3.Error) as exc:
            # 发送队列写不进去不能拖死主循环，单条丢弃并记日志
            logger("入队失败已丢弃：%s（%s）" % (chunk[:24], exc))
    return sent


# --------------------------------------------------------------------------- 取新消息
def fetch_new(messages_conn: sqlite3.Connection, cursor: int, limit: int = 50) -> list[dict]:
    rows = messages_conn.execute(
        "SELECT rowid,* FROM messages WHERE rowid>? ORDER BY rowid ASC LIMIT ?",
        (cursor, limit)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["_rowid"] = d.pop("rowid")
        result.append(d)
    return result


# --------------------------------------------------------------------------- 自检
def cmd_selftest(cfg: dict) -> int:
    log("===== 自检开始 =====")
    ok = True
    # 配置
    rsrc = cfg.get("_runtime_source", "config")
    log("运行开关：enabled=%s, dry_run=%s（来源：%s）" % (
        cfg.get("enabled"), cfg.get("dry_run"),
        "网页设置" if rsrc == "web" else "config.local.json（网页尚未设置）"))
    # 库
    for name, key in (("消息库", "messages_db"), ("发送队列", "outbox_db")):
        p = cfg["_resolved"].get(key)
        exists = bool(p and p.is_file())
        log("%s：%s (%s)" % (name, "OK" if exists else "缺失", p))
        ok = ok and exists
    # 状态库可写
    try:
        conn = init_state(cfg["_resolved"]["state_db"])
        conn.close()
        log("状态库：OK 可写")
    except Exception as exc:
        log("状态库：失败 %s" % exc)
        ok = False
    # LLM 连通（读网页设置或本地文件，发一句极简请求，不算群消息）
    raw = llm_config_dict(cfg)
    key = str(raw.get("api_key", ""))
    log("LLM 配置来源：%s（模型 %s）" % (
        "网页设置" if LLM_LOCAL.is_file() and str(llm_config_dict(cfg).get("api_key", "")).strip()
        else "本地文件/未配置", raw.get("model", "—")))
    if not key or "CHANGE" in key.upper():
        log("LLM 连通：跳过 —— api_key 未配置（在网页设置里填 base_url/api_key/model）")
    else:
        try:
            llm = BrainLLM(raw)
            answer = llm.reply("只回复两个字：收到", "测试连通")
            log("LLM 连通：OK（模型 %s，返回 %r）" % (llm.model, answer[:20]))
        except Exception as exc:
            log("LLM 连通：失败 %s" % exc)
            ok = False
    # 群配置
    groups = cfg.get("groups", {})
    enabled = [k for k, v in groups.items() if isinstance(v, dict) and v.get("enabled")]
    source = cfg.get("_group_source", "config")
    log("群白名单来源：%s" % ("网页勾选（groups.local.json）" if source == "web"
                             else "config.local.json（网页尚未设置，可在管理台→应用→拟人大脑里勾选）"))
    log("启用群：%d 个 %s" % (len(enabled), enabled[:3]))
    log("===== 自检%s =====" % ("通过" if ok else "未通过"))
    return 0 if ok else 1


def cmd_status(cfg: dict) -> int:
    conn = init_state(cfg["_resolved"]["state_db"])
    cursor = get_cursor(conn)
    total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    log("游标 rowid=%d，决策记录=%d 条" % (cursor, total))
    for row in conn.execute(
            "SELECT decision,COUNT(*) c FROM decisions GROUP BY decision ORDER BY c DESC"):
        log("  %-14s %d" % (row[0], row[1]))
    log("最近 5 条决策：")
    for row in conn.execute(
            "SELECT chat_id,decision,reason,substr(text,1,24) FROM decisions "
            "ORDER BY id DESC LIMIT 5"):
        # row 是 sqlite3.Row，不能直接拿去 % 展开，必须按下标取
        log("  %-18s | %-12s | %-22s | %s" % (
            str(row[0])[:18], row[1], str(row[2])[:22], str(row[3] or "")[:24]))
    conn.close()
    return 0


def cmd_memory(cfg: dict, args) -> int:
    conn = init_state(cfg["_resolved"]["state_db"])
    chat_id = args.chat_id or None

    if args.memory_stats:
        st = memory_mod.stats(conn)
        log("记忆统计：活跃 %d / 归档 %d / 铁律 %d / 平均保持率 %.3f" % (
            st["active"], st["archived"], st["iron"], st["avg_retention"]))
        log("  分类：" + (", ".join("%s %d" % (k, v) for k, v in st["by_kind"].items()) or "无"))
    elif args.memories:
        rows = memory_mod.list_memories(conn, chat_id, limit=100)
        if not rows:
            log("暂无记忆。")
        for m in rows:
            log("  #%s [%s] 重要%.2f 保持%.2f 复习%d %s | %s" % (
                m["id"], m["kind"], m["importance"], m["retention"], m["review_count"],
                m["created"], m["content"][:60]))
    elif args.extract:
        llm = BrainLLM(llm_config_dict(cfg))
        mdb = sqlite3.connect(str(cfg["_resolved"]["messages_db"]), timeout=10)
        mdb.row_factory = sqlite3.Row
        try:
            for cid, gcfg in cfg.get("groups", {}).items():
                if isinstance(gcfg, dict) and gcfg.get("enabled"):
                    log("提炼群 %s ..." % cid)
                    extract_for_group(cfg, conn, mdb, llm, cid, force=True)
        finally:
            mdb.close()
    elif args.memory_clear:
        if getattr(args, "yes", False):
            n = memory_mod.clear_memories(conn, chat_id)
            log("已清空 %d 条记忆。" % n)
        else:
            log("清空是不可逆操作。请加 --yes 确认（可配合 --chat-id 只清某群）。")
    conn.close()
    return 0


# --------------------------------------------------------------------------- 记忆提炼
def extract_for_group(cfg: dict, state_conn: sqlite3.Connection, msg_conn: sqlite3.Connection,
                      llm, chat_id: str, logger=log, force: bool = False) -> dict:
    """从该群最近消息提炼长期记忆。默认需要距上次≥间隔分钟 且 新增≥阈值条。"""
    mcfg = cfg.get("memory", {})
    if not mcfg.get("enabled", True):
        return {"skipped": "disabled"}
    min_minutes = float(mcfg.get("extract_min_minutes", 10))
    min_new = int(mcfg.get("extract_min_messages", 8))

    row = state_conn.execute(
        "SELECT value FROM meta WHERE key=?", ("last_extract_%s" % chat_id,)).fetchone()
    last = float(row[0]) if row else 0.0
    now = time.time()
    recent = msg_conn.execute(
        "SELECT sender_name,text FROM messages WHERE chat_id=? AND text IS NOT NULL "
        "AND trim(text)<>'' ORDER BY rowid DESC LIMIT ?",
        (chat_id, mcfg.get("extract_window", 60))).fetchall()
    new_since = sum(1 for r in recent if True)  # 用窗口条数做粗判据
    if not force and (now - last < min_minutes * 60 or len(recent) < min_new):
        return {"skipped": "conditions", "minutes": round((now - last) / 60, 1),
                "window": len(recent)}

    msgs = [{"sender_name": r[0], "text": r[1]} for r in reversed(recent)]
    user = memory_mod.build_extract_user(msgs)
    try:
        raw = llm.reply(memory_mod.EXTRACT_SYSTEM, user)
    except Exception as exc:
        logger("  记忆提炼失败：%s" % exc)
        return {"error": str(exc)[:100]}
    added, skipped = memory_mod.ingest_extraction(
        state_conn, chat_id, raw, source_key="extract:%d" % int(now))
    state_conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("last_extract_%s" % chat_id, str(now)))
    state_conn.commit()
    logger("  记忆提炼：新增 %d 条，重复 %d 条" % (added, skipped))
    return {"added": added, "skipped_dup": skipped}


# --------------------------------------------------------------------------- 主循环
def process_once(cfg: dict, state_conn: sqlite3.Connection, msg_conn: sqlite3.Connection,
                 llm: BrainLLM | None, logger=log, force_reply: bool = False) -> int:
    cursor = get_cursor(state_conn)
    if cursor <= 0:
        # 首次运行（或状态库被重置）：把游标直接定位到最新，绝不回扫历史。
        # 否则一启动就会把整个消息库当新消息处理，非演练模式下可能在群里刷屏。
        latest = msg_conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
        if latest:
            set_cursor(state_conn, int(latest))
            logger("首次运行：游标定位到最新 rowid=%d，只处理此后的新消息" % latest)
            return 0
    new = fetch_new(msg_conn, cursor)
    if not new:
        return 0

    self_wxid = str(cfg.get("self_wxid", "") or load_self_wxid())
    groups = cfg.get("groups", {})
    # 机器人昵称只读一次，避免每条消息都去读 chats.auto.json
    at_names = load_bot_names()
    for gcfg in groups.values():
        if isinstance(gcfg, dict) and "_at_names" not in gcfg:
            gcfg["_at_names"] = at_names

    def mentions_us(msg: dict) -> bool:
        text = msg["text"] or ""
        return bool(at_names) and any(("@" + n) in text for n in at_names if n)

    def handle(msg: dict) -> int:
        """处理单条消息，返回 1 表示走过决策（不代表一定回复）。异常都在内部消化。"""
        chat_id = msg["chat_id"]
        gcfg = groups.get(chat_id)
        if not isinstance(gcfg, dict) or not gcfg.get("enabled", False):
            return 0

        now_ts = time.time()
        now = datetime.now()
        state = get_group(state_conn, chat_id,
                          float(gcfg.get("energy", {}).get("base", 0.7)))
        update_energy(state, gcfg, now_ts)

        decision, reason = decide(msg, gcfg, state, now_ts, now, self_wxid)
        if force_reply:
            decision, reason = DECISION_PROB, "forced"

        logger("msg[%s] %s：%s → %s(%s)" % (
            msg["_rowid"], chat_id[:16], (msg["text"] or "")[:24], decision, reason))

        if decision in (DECISION_SKIP, DECISION_QUIET, DECISION_COOLDOWN, DECISION_RATE):
            record_decision(state_conn, chat_id, msg, decision, reason)
            return 1

        # 需要生成回复
        if llm is None:
            logger("  跳过生成：LLM 未初始化")
            record_decision(state_conn, chat_id, msg, DECISION_SKIP, "no_llm")
            return 1
        try:
            context = build_context(msg_conn, chat_id, int(gcfg.get("context_limit", 10)))
            system = gcfg.get("persona") or cfg.get("global_persona") or "你是群里一个正常聊天的成员。"
            # 让模型知道自己是谁、在哪个群、现在几点——否则写出来的话像凭空插嘴的陌生人
            hints = []
            gname = str(gcfg.get("name") or "").strip()
            bot_name = (at_names[0] if at_names else "").strip()
            if gname:
                hints.append("你在的群叫「%s」" % gname)
            if bot_name:
                hints.append("群里的人叫你「%s」" % bot_name)
            if hints:
                system += "\n" + "；".join(hints) + "。"
            system += "\n现在是 %s。" % datetime.now().strftime("%m月%d日 %H:%M")
            # 长期记忆：按相关性/保持率/重要度检索，注入并复习
            mem_text = ""
            if cfg.get("memory", {}).get("enabled", True):
                found = memory_mod.retrieve(
                    state_conn, chat_id, (msg["text"] or ""),
                    top_k=int(cfg.get("memory", {}).get("top_k", 12)),
                    # 演练只读：不去强化记忆权重，免得反复试跑把库洗乱
                    reinforce=not cfg.get("dry_run", True))
                mem_text = memory_mod.format_for_prompt(found)
                if found:
                    logger("  注入记忆 %d 条：%s" % (
                        len(found), "、".join(x["content"][:14] for x in found[:3])))
            user = "以下是本群最近的聊天记录：\n%s\n\n%s：%s\n\n请以上述身份自然接话回复最后一句。" % (
                context, msg["sender_name"] or "某人", msg["text"])
            if mem_text:
                user = mem_text + "\n\n" + user
            reply = llm.reply(system, user)
        except Exception as exc:
            logger("  生成失败：%s" % exc)
            record_decision(state_conn, chat_id, msg, DECISION_SKIP, "llm_error:" + str(exc)[:60])
            return 1

        is_at = (decision == DECISION_AT)
        if decision == DECISION_DRY or cfg.get("dry_run", True):
            record_decision(state_conn, chat_id, msg, DECISION_DRY, reason, reply)
            logger("  dry-run 回复%s：%s" % (
                "" if not isinstance(llm, MockLLM) else "（模拟）", reply[:80]))
            deliver_reply(cfg, chat_id, reply, logger, is_at=is_at)   # 演练只输出发送预览
        else:
            record_decision(state_conn, chat_id, msg, decision, reason, reply)
            deliver_reply(cfg, chat_id, reply, logger, is_at=is_at)
            drain = float(gcfg.get("energy", {}).get("drain_per_reply", 0.06))
            state["energy"] = max(float(gcfg.get("energy", {}).get("min", 0.15)),
                                  state["energy"] - drain)
            state["last_reply_ts"] = now_ts
            state["recent_replies"] = [
                t for t in state["recent_replies"] if now_ts - t < 600] + [now_ts]
            # 跟进窗口记账：还在同一段对话里就累加，断档了就从这一句重新起算
            win = float(gcfg.get("follow_window_seconds", 180))
            prev_turns = int(state.get("session_turns", 0) or 0)
            same_session = (now_ts - float(state.get("session_ts", 0.0) or 0.0)) < win
            state["session_turns"] = prev_turns + 1 if (same_session and prev_turns > 0) else 1
            state["session_ts"] = now_ts
            save_group(state_conn, chat_id, state)
        return 1

    handled = 0
    # 第一趟：只处理被@的消息。群里连刷十条闲聊，也不该让"叫它名字"那条排在最后
    # 等一堆生成完。这一趟不推进游标，交给第二趟统一按 rowid 顺序推进，
    # 避免"先处理大 rowid 导致中间消息被游标跳过"。
    early: set[int] = set()
    for msg in sorted(new, key=lambda m: m["_rowid"]):
        if not mentions_us(msg):
            continue
        handled += handle(msg)
        early.add(msg["_rowid"])
    # 第二趟：按 rowid 顺序处理剩余，同时把游标一路推上去
    for msg in sorted(new, key=lambda m: m["_rowid"]):
        if msg["_rowid"] not in early:
            handled += handle(msg)
        set_cursor(state_conn, msg["_rowid"])
    return handled


def load_self_wxid() -> str:
    """优先从 bot_config.json 的 wechat_account 读机器人自己的 wxid。"""
    path = HERE.parent.parent / "vendor" / "helper-main" / "bot_config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return str(data.get("wechat_account", "") or "")
    except (OSError, ValueError):
        return ""


def load_bot_names() -> list[str]:
    """从 chats.auto.json 里找 is_self 条目的昵称，用于识别真@。"""
    path = HERE.parent / "admin" / "chats.auto.json"
    names: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    if isinstance(data, dict):
        for row in data.values():
            if isinstance(row, dict) and row.get("is_self"):
                for key in ("name", "alias", "wechat_name"):
                    if row.get(key):
                        names.append(str(row[key]))
    return list(dict.fromkeys(names))


def _config_signature() -> tuple:
    """所有会影响行为的私有配置文件的 (mtime, 大小) 快照，用来判断是否需要热重载。"""
    parts = []
    for path in (CONFIG, RUNTIME_LOCAL, GROUPS_LOCAL, PERSONA_LOCAL, TUNING_LOCAL,
                 LLM_LOCAL, LLM_FALLBACK):
        try:
            st = path.stat()
            parts.append((path.name, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((path.name, 0, 0))
    return tuple(parts)


def _at_reply_all(cfg: dict) -> bool:
    """所有启用群是否都开了「被@必回」，仅用于日志展示。"""
    vals = [g.get("at_reply") for g in cfg.get("groups", {}).values() if isinstance(g, dict)]
    return bool(vals) and all(bool(v) for v in vals)


def _write_heartbeat(conn: sqlite3.Connection, cfg: dict, llm) -> None:
    """心跳：网页端据此判断进程是否还活着，以及实际在用什么模式。"""
    mode = "mock" if isinstance(llm, MockLLM) else ("none" if llm is None else "real")
    try:
        for key, value in (("last_tick", str(time.time())),
                           ("last_dry_run", "1" if cfg.get("dry_run", True) else "0"),
                           ("last_llm_mode", mode)):
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
    except sqlite3.Error:
        pass


def reload_config_if_changed(cfg: dict, llm, force_reply: bool = False):
    """网页保存开关后不重启就生效：检测到私有配置变化就重载并重建模型。

    返回 (cfg, llm, changed)。llm 可能变成 None（实发模式配置有误），
    这时主循环会暂停扫描（不推进游标，消息不会丢），补好配置后自动恢复。
    """
    sig = _config_signature()
    if sig == cfg.get("_signature"):
        return cfg, llm, False
    try:
        fresh = load_config()
    except SystemExit as exc:
        log("配置热更新失败，继续用旧配置：%s" % exc)
        cfg["_signature"] = sig
        return cfg, llm, False

    cfg.update(fresh)                  # 开关、群白名单、记忆参数整份换新
    cfg["_signature"] = sig
    try:
        llm, note = build_llm(cfg, force_reply)
    except ValueError as exc:
        log("热更新：真模型不可用（%s），暂停生成（不会乱发消息）" % exc)
        llm, note = None, ""
    g0 = next((v for v in cfg.get("groups", {}).values() if isinstance(v, dict)), {})
    log("配置已热更新：总开关=%s，演练=%s，模型=%s，被@必回=%s，群=%d，轮询=%ss，"
        "主动概率=%.2f，冷却=%ss，跟进=%.2f/%ss/%s句" % (
            "开" if cfg.get("enabled") else "关",
            "是" if cfg.get("dry_run") else "否",
            "模拟文本" if isinstance(llm, MockLLM) else ("未就绪" if llm is None else "真模型"),
            "是" if _at_reply_all(cfg) else "否",
            len([v for v in cfg.get("groups", {}).values() if v.get("enabled")]),
            cfg.get("poll_seconds", 2),
            float(g0.get("reply_probability", 0.28)),
            g0.get("cooldown_seconds", 90),
            float(g0.get("follow_probability", 0.85)),
            g0.get("follow_window_seconds", 180),
            g0.get("follow_max_turns", 4)))
    if note:
        log(note)
    return cfg, llm, True


def run(cfg: dict, once: bool = False, force_reply: bool = False) -> int:
    resolved = cfg["_resolved"]
    state_conn = init_state(resolved["state_db"])
    msg_conn = sqlite3.connect(str(resolved["messages_db"]), timeout=10)
    msg_conn.row_factory = sqlite3.Row
    cfg["_signature"] = _config_signature()

    # 进程重启但开关仍是开启：上次心跳距今太久，说明中间停过（或刚被统一控制台接管），
    # 停机期间的群消息属于旧消息，一并跳过。--once / --force-reply 是手工调试入口，
    # 这里不跳过，否则用户「强制回复下一条」flag 的那条会被吃掉。
    if cfg.get("enabled", False) and not once and not force_reply:
        try:
            row = state_conn.execute(
                "SELECT value FROM meta WHERE key='last_tick'").fetchone()
            gap = time.time() - float(row[0]) if row and row[0] else None
        except (TypeError, ValueError):
            gap = None
        if gap is None or gap > 60:
            skip_backlog(state_conn, msg_conn,
                         "重启后检测到大脑中断%s" % (
                             "（无历史心跳）" if gap is None else " %.0fs" % gap))

    llm = None
    idle = None
    missing_logged = False
    try:
        if cfg.get("enabled", False):
            try:
                llm, llm_note = build_llm(cfg, force_reply)
                log(llm_note)
            except ValueError as exc:
                log("真模型不可用（%s）：暂停扫描，补好配置后自动恢复，消息不会丢" % exc)
            log("启动：poll=%ss, dry_run=%s, 启用群=%d" % (
                cfg.get("poll_seconds", 2), cfg.get("dry_run", True),
                len([v for v in cfg.get("groups", {}).values() if v.get("enabled")])))
        else:
            log("总开关 enabled=false：进程待机中，网页打开总开关后自动开始工作（无需重启）")
            if once:
                return 0

        while True:
            cfg, llm, _changed = reload_config_if_changed(cfg, llm, force_reply)
            enabled = bool(cfg.get("enabled", False))
            processed = 0
            # 待机状态和开关不一致时翻转一次并打日志（避免每轮刷屏）
            if idle is None:
                idle = not enabled
            elif idle and enabled:
                log("总开关已打开：恢复工作")
                # 待机期间攒下的消息已经过时，跳过；只从打开开关这一刻往后看。
                # 必须在 process_once 之前推进游标，否则会把积压当新消息全读一遍。
                skip_backlog(state_conn, msg_conn, "总开关打开")
                idle = False
            elif not idle and not enabled:
                log("总开关已关闭：转入待机，不再读消息、不再回复")
                idle = True

            if enabled and llm is None:
                if not missing_logged:
                    log("模型未就绪：本轮只心跳不消费消息（游标不动，修好后自动恢复）")
                    missing_logged = True
            elif enabled:
                missing_logged = False
                try:
                    processed = process_once(cfg, state_conn, msg_conn, llm,
                                             force_reply=force_reply)
                    # 记忆提炼：有新消息的群才检查条件，避免空转。演练不写库（会污染长期记忆）
                    if (processed and cfg.get("memory", {}).get("enabled", True)
                            and not isinstance(llm, MockLLM) and not cfg.get("dry_run", True)):
                        for cid, gcfg in cfg.get("groups", {}).items():
                            if isinstance(gcfg, dict) and gcfg.get("enabled"):
                                extract_for_group(cfg, state_conn, msg_conn, llm, cid)
                    # 每日遗忘巡检（每天一次）
                    today = datetime.now().strftime("%Y-%m-%d")
                    crow = state_conn.execute(
                        "SELECT value FROM meta WHERE key='last_consolidate'").fetchone()
                    if not crow or crow[0] != today:
                        result = memory_mod.consolidate(state_conn)
                        state_conn.execute(
                            "INSERT INTO meta(key,value) VALUES('last_consolidate',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
                        state_conn.commit()
                        if result["archived"] or result["deleted"]:
                            log("遗忘巡检：归档 %d，删除 %d" % (result["archived"], result["deleted"]))
                except Exception as exc:
                    log("本轮异常（不退出）：%s" % exc)

            _write_heartbeat(state_conn, cfg, llm)
            if once:
                break
            # 拟人延迟靠这里的 flush 落地：每 0.25 秒看一次到期队列，
            # 有新消息就用 0.3 秒短间隔连扫，把积压一口气消化掉，不再干等满一个轮询周期。
            wait = 0.3 if processed else float(cfg.get("poll_seconds", 2))
            deadline = time.time() + wait
            while time.time() < deadline:
                flush_pending(cfg, log)
                left = deadline - time.time()
                if left > 0:
                    time.sleep(min(0.25, left))
    finally:
        msg_conn.close()
        state_conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="拟人大脑（独立旁路应用）")
    ap.add_argument("--selftest", action="store_true", help="自检配置/库/LLM 连通，不发消息")
    ap.add_argument("--once", action="store_true", help="只扫一轮新消息")
    ap.add_argument("--status", action="store_true", help="打印状态与决策统计")
    ap.add_argument("--force-reply", action="store_true", help="强制回复下一条（灰度验证用，仍遵守 dry_run）")
    ap.add_argument("--memories", action="store_true", help="列出长期记忆")
    ap.add_argument("--memory-stats", action="store_true", help="记忆系统统计")
    ap.add_argument("--extract", action="store_true", help="立即对所有启用群提炼一次记忆")
    ap.add_argument("--memory-clear", action="store_true", help="清空所有长期记忆（不可逆，需 --yes 确认）")
    ap.add_argument("--yes", action="store_true", help="确认执行清空操作")
    ap.add_argument("--chat-id", default="", help="配合记忆子命令指定群")
    args = ap.parse_args()

    cfg = load_config()
    if args.selftest:
        return cmd_selftest(cfg)
    if args.status:
        return cmd_status(cfg)
    if args.memories or args.memory_stats or args.extract or args.memory_clear:
        return cmd_memory(cfg, args)
    if not acquire_mutex():
        log("已有大脑实例在运行，退出。")
        return 0
    return run(cfg, once=args.once, force_reply=args.force_reply)


if __name__ == "__main__":
    sys.exit(main())
