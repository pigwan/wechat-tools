"""长期记忆系统 v2：艾宾浩斯遗忘曲线 + 情绪加权 + 检索增强。

被 brain.py 调用，状态存在自己的 SQLite（与消息库物理隔离）。

核心模型（参考大肥鱼记忆系统 v2 的思路，独立实现）：
  - 每条记忆是一个结构化 fact：kind / content / emotion / intensity / importance / self_relevant
  - 记忆强度 S（秒）：越大衰减越慢；保持率 R = exp(-elapsed / S)
  - 检索打分 score = R × 相关性 × importance × 情绪权重
  - 被检索进上下文 = 一次复习：S ← S × (1 + γ(1-R)(1-D))，然后刷新 last_review
  - 低权重（R × importance < 归档阈值）自动归档；归档满 N 天后真删
  - importance ≥ 铁律阈值的「铁律记忆」永不自动遗忘

纯标准库，不依赖第三方包。所有 LLM 交互由调用方注入（本模块只做存储与检索数学）。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

# --- 可调参数（对齐大肥鱼的默认值）-------------------------------------------
GAMMA = 0.35              # 复习带来的强度增益系数
ARCHIVE_R_IMPORTANCE = 0.06   # R×importance 低于此值 -> 归档
IRON_IMPORTANCE = 0.85       # ≥ 此值视为铁律记忆，永不自动忘
ARCHIVE_TTL_DAYS = 30        # 归档后多少天真删
DEFAULT_STRENGTH = 6 * 3600  # 新记忆初始强度（秒）
MIN_STRENGTH = 600           # 强度下限（10 分钟）
MAX_STRENGTH = 60 * 86400    # 强度上限（60 天）
RETRIEVE_TOP_K = 12          # 注入上下文的条数上限

EMOTION_WEIGHT = {
    "joy": 1.15, "surprise": 1.10, "anger": 1.08, "sad": 1.05,
    "fear": 1.05, "disgust": 1.0, "neutral": 1.0,
}
KIND_LABEL = {
    "fact": "事实", "pref": "偏好", "event": "事件", "meme": "梗",
    "person": "人物", "promise": "承诺", "rule": "铁律", "self": "自身",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'fact',
    content       TEXT NOT NULL,
    emotion       TEXT NOT NULL DEFAULT 'neutral',
    intensity     REAL NOT NULL DEFAULT 0.5,
    importance    REAL NOT NULL DEFAULT 0.5,
    self_relevant INTEGER NOT NULL DEFAULT 0,
    strength      REAL NOT NULL DEFAULT 21600,
    last_review   REAL NOT NULL,
    review_count  INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   REAL NOT NULL DEFAULT 0,
    pinned        INTEGER NOT NULL DEFAULT 0,
    source_key    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mem_chat ON memories(chat_id, archived);
CREATE INDEX IF NOT EXISTS idx_mem_archived ON memories(archived, archived_at);
"""


def init_memory(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# --- 写入 / 提炼 -------------------------------------------------------------
def add_memory(conn: sqlite3.Connection, chat_id: str, content: str, *,
               kind: str = "fact", emotion: str = "neutral", intensity: float = 0.5,
               importance: float = 0.5, self_relevant: bool = False,
               pinned: bool = False, source_key: str = "") -> int | None:
    content = (content or "").strip()
    if not content:
        return None
    importance = max(0.0, min(1.0, float(importance)))
    intensity = max(0.0, min(1.0, float(intensity)))
    if pinned or importance >= IRON_IMPORTANCE:
        strength = MAX_STRENGTH
    else:
        # 强度随重要度和情绪强度放大
        strength = DEFAULT_STRENGTH * (0.5 + importance + 0.5 * intensity)
    strength = max(MIN_STRENGTH, min(MAX_STRENGTH, strength))
    now = time.time()
    cur = conn.execute("""INSERT INTO memories
        (chat_id,kind,content,emotion,intensity,importance,self_relevant,strength,
         last_review,review_count,created_at,pinned,source_key)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (chat_id, kind, content, emotion, intensity, importance, 1 if self_relevant else 0,
         strength, now, 0, now, 1 if pinned else 0, source_key))
    conn.commit()
    return int(cur.lastrowid)


def parse_extract_json(raw: str) -> list[dict]:
    """把 LLM 返回的 JSON 解析成记忆条目列表，容错处理代码围栏。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        opening, _, body = text.partition("\n")
        if opening.strip().lower() in ("```", "```json"):
            text = body.rsplit("```", 1)[0].strip()
    data = json.loads(text)
    if isinstance(data, dict):
        items = data.get("memories") or data.get("facts") or data.get("items") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        content = str(it.get("content") or it.get("text") or "").strip()
        if not content:
            continue
        out.append({
            "content": content,
            "kind": str(it.get("kind") or "fact"),
            "emotion": str(it.get("emotion") or "neutral"),
            "intensity": float(it.get("intensity") or 0.5),
            "importance": float(it.get("importance") or 0.5),
            "self_relevant": bool(it.get("self_relevant") or it.get("selfRelevant") or False),
        })
    return out


def ingest_extraction(conn: sqlite3.Connection, chat_id: str, raw: str,
                      source_key: str = "") -> tuple[int, int]:
    """把一次提炼结果入库。返回 (新增数, 跳过数)。去重：同 chat 下 content 完全相同则跳过。"""
    try:
        items = parse_extract_json(raw)
    except (ValueError, TypeError):
        return 0, 0
    added = skipped = 0
    for it in items:
        exists = conn.execute(
            "SELECT 1 FROM memories WHERE chat_id=? AND content=? LIMIT 1",
            (chat_id, it["content"])).fetchone()
        if exists:
            skipped += 1
            continue
        add_memory(conn, chat_id, it["content"], kind=it["kind"], emotion=it["emotion"],
                   intensity=it["intensity"], importance=it["importance"],
                   self_relevant=it["self_relevant"], source_key=source_key)
        added += 1
    return added, skipped


# --- 保持率 / 检索 -----------------------------------------------------------
def _field(row, name: str, fallback_index: int):
    """同时兼容 Row（字符串索引）和普通 tuple（位置索引）。"""
    try:
        return row[name]
    except (TypeError, IndexError, KeyError):
        return row[fallback_index]


def retention(row: sqlite3.Row | tuple, now: float | None = None) -> float:
    """记忆保持率 R = exp(-elapsed / S)。"""
    now = now or time.time()
    last_review = float(_field(row, "last_review", 9))
    strength = max(MIN_STRENGTH, float(_field(row, "strength", 8)))
    elapsed = max(0.0, now - last_review)
    return math.exp(-elapsed / strength)


def _tokenize(text: str) -> set[str]:
    """中文按 2-gram + 英文单词做粗粒度相关性匹配。"""
    text = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(len(han) - 1):
        tokens.add(han[i:i + 2])
    return tokens


def relevance(query: str, content: str) -> float:
    q = _tokenize(query)
    c = _tokenize(content)
    if not q or not c:
        return 0.0
    overlap = len(q & c)
    return overlap / math.sqrt(len(q) * len(c))


def retrieve(conn: sqlite3.Connection, chat_id: str, query: str, *,
             top_k: int = RETRIEVE_TOP_K, now: float | None = None,
             reinforce: bool = True) -> list[dict]:
    """按 R×相关性×重要度×情绪权重 检索记忆；被检索到的条目顺带复习一次。"""
    now = now or time.time()
    rows = conn.execute(
        "SELECT * FROM memories WHERE chat_id=? AND archived=0", (chat_id,)).fetchall()
    scored = []
    for row in rows:
        r = retention(row, now)
        rel = relevance(query, row["content"])
        emotion = EMOTION_WEIGHT.get(row["emotion"], 1.0)
        score = r * (0.25 + rel) * (0.3 + float(row["importance"])) * emotion
        scored.append((score, r, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [x for x in scored if x[0] > 0][:top_k]

    results = []
    for score, r, row in picked:
        results.append({
            "id": row["id"], "content": row["content"], "kind": row["kind"],
            "kind_label": KIND_LABEL.get(row["kind"], "记忆"),
            "importance": row["importance"], "emotion": row["emotion"],
            "retention": round(r, 3), "score": round(score, 4),
            "review_count": row["review_count"],
        })
        if reinforce:
            reinforce_memory(conn, row, r, now)
    return results


def reinforce_memory(conn: sqlite3.Connection, row: sqlite3.Row | tuple,
                     r: float, now: float) -> None:
    """复习：S ← S×(1+γ(1-R)(1-D))，D 用 1-importance 近似难度；刷新时间。"""
    strength = max(MIN_STRENGTH, float(row["strength"]))
    importance = float(row["importance"])
    difficulty = 1.0 - importance
    new_strength = strength * (1 + GAMMA * (1 - r) * (1 - difficulty))
    if row["pinned"] or importance >= IRON_IMPORTANCE:
        new_strength = MAX_STRENGTH
    new_strength = max(MIN_STRENGTH, min(MAX_STRENGTH, new_strength))
    conn.execute("""UPDATE memories SET strength=?, last_review=?, review_count=review_count+1
        WHERE id=?""", (new_strength, now, row["id"]))
    conn.commit()


# --- 维护 -------------------------------------------------------------------
def consolidate(conn: sqlite3.Connection, now: float | None = None) -> dict:
    """遗忘巡检：低权重记忆归档、过期归档真删。返回处理结果统计。"""
    now = now or time.time()
    archived = deleted = kept_iron = 0
    rows = conn.execute("SELECT * FROM memories WHERE archived=0").fetchall()
    for row in rows:
        if row["pinned"] or float(row["importance"]) >= IRON_IMPORTANCE:
            kept_iron += 1
            continue
        r = retention(row, now)
        if r * float(row["importance"]) < ARCHIVE_R_IMPORTANCE:
            conn.execute("UPDATE memories SET archived=1, archived_at=? WHERE id=?",
                         (now, row["id"]))
            archived += 1
    cutoff = now - ARCHIVE_TTL_DAYS * 86400
    cur = conn.execute("DELETE FROM memories WHERE archived=1 AND archived_at>0 AND archived_at<?",
                       (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    return {"archived": archived, "deleted": deleted, "iron_kept": kept_iron}


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=0").fetchone()[0]
    archived = conn.execute("SELECT COUNT(*) FROM memories WHERE archived=1").fetchone()[0]
    iron = conn.execute("SELECT COUNT(*) FROM memories WHERE importance>=? AND archived=0",
                        (IRON_IMPORTANCE,)).fetchone()[0]
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind,COUNT(*) FROM memories WHERE archived=0 GROUP BY kind")}
    avg_ret = 0.0
    if total:
        avg_ret = sum(retention(r) for r in conn.execute(
            "SELECT * FROM memories WHERE archived=0")) / total
    return {"active": total, "archived": archived, "iron": iron,
            "by_kind": by_kind, "avg_retention": round(avg_ret, 3)}


def list_memories(conn: sqlite3.Connection, chat_id: str | None = None, *,
                  limit: int = 50, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM memories WHERE 1=1"
    args: list = []
    if chat_id:
        sql += " AND chat_id=?"
        args.append(chat_id)
    if not include_archived:
        sql += " AND archived=0"
    sql += " ORDER BY importance DESC, last_review DESC LIMIT ?"
    args.append(limit)
    out = []
    for row in conn.execute(sql, args):
        out.append({
            "id": row["id"], "chat_id": row["chat_id"], "kind": row["kind"],
            "content": row["content"], "emotion": row["emotion"],
            "importance": round(row["importance"], 2),
            "retention": round(retention(row), 3), "review_count": row["review_count"],
            "archived": bool(row["archived"]), "pinned": bool(row["pinned"]),
            "created": datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M"),
        })
    return out


def clear_memories(conn: sqlite3.Connection, chat_id: str | None = None) -> int:
    if chat_id:
        cur = conn.execute("DELETE FROM memories WHERE chat_id=?", (chat_id,))
    else:
        cur = conn.execute("DELETE FROM memories")
    conn.commit()
    return cur.rowcount


def pin_memory(conn: sqlite3.Connection, memory_id: int, pinned: bool = True) -> bool:
    cur = conn.execute("UPDATE memories SET pinned=?, strength=? WHERE id=?",
                       (1 if pinned else 0, MAX_STRENGTH if pinned else DEFAULT_STRENGTH, memory_id))
    conn.commit()
    return cur.rowcount > 0


# --- 提炼提示词 --------------------------------------------------------------
EXTRACT_SYSTEM = """你在为微信群聊机器人维护长期记忆。请从聊天记录中提炼值得长期记住的事实。

只提炼这几类：
- fact 事实：确定的信息（某人说什么、某个决定）
- pref 偏好：某人喜欢/讨厌什么
- person 人物：某人的身份、称呼、特征
- meme 梗：群里反复出现的梗、口头禅、玩笑
- promise 承诺：谁答应了做什么
- event 事件：发生的事
- self 自身：机器人自己该记住的设定

每条给出一个 JSON 对象字段：
  content（记忆正文，一句完整的话，含主体，不要代词指代不清）
  kind（上面的类别之一）
  emotion（joy/surprise/anger/sad/fear/disgust/neutral）
  intensity（0-1，情绪强度）
  importance（0-1，重要度；涉及身份/承诺/铁律给 0.85 以上）
  self_relevant（是否和机器人自身设定有关，true/false）

只返回 JSON 对象：{"memories": [ ... ]}。
没有值得记的就返回 {"memories": []}。
不要提炼无聊的寒暄、天气、纯粹的表情；不要记违反法律或有害的内容。"""


def build_extract_user(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        who = m.get("sender_name") or "某人"
        text = (m.get("text") or "").strip()
        if text:
            lines.append("%s：%s" % (who, text))
    return "聊天记录：\n" + "\n".join(lines[-40:]) + "\n\n请提炼长期记忆。"


def format_for_prompt(memories: list[dict]) -> str:
    """把检索到的记忆格式化成可注入 system/user 提示词的文本。"""
    if not memories:
        return ""
    lines = ["【你记得的关于这个群的事】"]
    for m in memories:
        tag = m["kind_label"]
        lines.append("- (%s) %s" % (tag, m["content"]))
    return "\n".join(lines)
