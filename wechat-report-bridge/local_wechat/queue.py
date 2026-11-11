"""本地任务队列（SQLite，零第三方依赖）。

沿用项目现有的 send_outbox.sqlite3 思路：CLI/管理台把要执行的动作写入队列，
helper 框架里的 local_api 插件领取执行并把结果写回，全程只走本机文件，
不开端口、不联网、不需要任何 Token。

表状态：0 待处理 / 1 执行中 / 2 成功 / 3 失败。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    status      INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
)
"""

STATUS_TEXT = {0: "pending", 1: "running", 2: "done", 3: "failed"}


def _default_db() -> Path:
    """从本文件位置向上找 wechat-report-bridge，保证 CLI 与插件副本指向同一个库。"""
    override = os.environ.get("LOCAL_API_DB")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        target = parent / "wechat-report-bridge"
        if target.is_dir():
            return target / ".runtime" / "local_api_tasks.sqlite3"
    return here.parent / ".runtime" / "local_api_tasks.sqlite3"


def default_db() -> Path:
    return _default_db()


class TaskQueue:
    def __init__(self, path=None):
        self.path = Path(path or _default_db())
        self._local = threading.local()
        self.init()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
            self._local.conn = conn
        return conn

    def init(self):
        self._conn()

    def submit(self, action: str, params: dict | None = None) -> int:
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "INSERT INTO local_tasks (action, params, status, created_at) "
                "VALUES (?, ?, 0, ?)",
                (str(action), json.dumps(params or {}, ensure_ascii=False),
                 datetime.now().astimezone().isoformat(timespec="seconds")),
            )
        return int(cur.lastrowid)

    def claim(self):
        """原子领取一条待处理任务；没有则返回 None。"""
        conn = self._conn()
        with conn:
            row = conn.execute(
                "SELECT * FROM local_tasks WHERE status = 0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                "UPDATE local_tasks SET status = 1 WHERE id = ? AND status = 0",
                (row["id"],),
            ).rowcount
        if not changed:
            return None
        task = dict(row)
        task["status"] = 1
        try:
            task["params"] = json.loads(task["params"] or "{}")
        except ValueError:
            task["params"] = {}
        return task

    def complete(self, task_id: int, result=None, error: str = ""):
        conn = self._conn()
        status = 3 if error else 2
        with conn:
            conn.execute(
                "UPDATE local_tasks SET status = ?, result = ?, error = ?, finished_at = ? "
                "WHERE id = ?",
                (status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else "",
                 str(error)[:2000],
                 datetime.now().astimezone().isoformat(timespec="seconds"), task_id),
            )

    def get(self, task_id: int):
        row = self._conn().execute(
            "SELECT * FROM local_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["status_text"] = STATUS_TEXT.get(out["status"], "unknown")
        for field in ("params", "result"):
            try:
                out[field] = json.loads(out[field] or ("{}" if field == "params" else ""))
            except ValueError:
                pass
        return out

    def wait(self, task_id: int, timeout: float = 20.0, poll: float = 0.2):
        deadline = time.time() + max(0.5, timeout)
        while time.time() < deadline:
            row = self.get(task_id)
            if row and row["status"] in (2, 3):
                return row
            time.sleep(poll)
        return self.get(task_id)

    def stats(self) -> dict:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM local_tasks GROUP BY status"
        ).fetchall()
        counts = {STATUS_TEXT.get(r["status"], str(r["status"])): r["n"] for r in rows}
        for key in STATUS_TEXT.values():
            counts.setdefault(key, 0)
        counts["total"] = sum(counts.values())
        return counts

    def recent(self, limit: int = 20):
        rows = self._conn().execute(
            "SELECT * FROM local_tasks ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)
        ).fetchall()
        return [dict(r) for r in rows]

    def purge(self, days: int = 7) -> int:
        from datetime import timedelta
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM local_tasks WHERE status IN (2, 3) AND created_at < ?", (cutoff,)
            )
        return cur.rowcount
