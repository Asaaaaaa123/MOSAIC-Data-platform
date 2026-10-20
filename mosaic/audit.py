"""Audit log (SQLite): every tool call, agent turn and export is recorded.

Parameters are stored after secret redaction; API keys never enter this file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .http import SECRET_PARAM_NAMES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT,
    source TEXT NOT NULL,
    tool TEXT NOT NULL,
    params TEXT,
    ok INTEGER NOT NULL,
    latency_ms REAL,
    n_records INTEGER,
    total INTEGER,
    error TEXT,
    request_url TEXT
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS ix_tool_calls_source ON tool_calls(source, ts);

CREATE TABLE IF NOT EXISTS agent_turns (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT,
    stop_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    tool_calls INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_turns_session ON agent_turns(session_id);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT,
    name TEXT,
    path TEXT,
    n_rows INTEGER,
    columns TEXT
);
"""


def redact_params(params: Any) -> Any:
    if isinstance(params, dict):
        return {
            k: ("***" if str(k).lower() in SECRET_PARAM_NAMES else redact_params(v)) for k, v in params.items()
        }
    if isinstance(params, list):
        return [redact_params(v) for v in params]
    return params


class AuditLog:
    def __init__(self, path: Path | str | None):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _insert(self, table: str, row: dict[str, Any]) -> str:
        row.setdefault("id", uuid.uuid4().hex)
        row.setdefault("ts", time.time())
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
            self._conn.commit()
        return row["id"]

    def record_tool_call(
        self,
        *,
        source: str,
        tool: str,
        params: dict[str, Any],
        ok: bool,
        latency_ms: float,
        session_id: str | None = None,
        n_records: int | None = None,
        total: int | None = None,
        error: str | None = None,
        request_url: str | None = None,
    ) -> str:
        return self._insert(
            "tool_calls",
            {
                "session_id": session_id,
                "source": source,
                "tool": tool,
                "params": json.dumps(redact_params(params), default=str),
                "ok": int(ok),
                "latency_ms": latency_ms,
                "n_records": n_records,
                "total": total,
                "error": error,
                "request_url": request_url,
            },
        )

    def record_agent_turn(
        self,
        *,
        session_id: str,
        model: str,
        stop_reason: str | None,
        usage: dict[str, int] | None = None,
        tool_calls: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> str:
        usage = usage or {}
        return self._insert(
            "agent_turns",
            {
                "session_id": session_id,
                "model": model,
                "stop_reason": stop_reason,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "tool_calls": tool_calls,
                "detail": json.dumps(detail or {}, default=str),
            },
        )

    def record_export(
        self, *, name: str, path: str, n_rows: int, columns: list[str], session_id: str | None = None
    ) -> str:
        return self._insert(
            "exports",
            {
                "session_id": session_id,
                "name": name,
                "path": path,
                "n_rows": n_rows,
                "columns": json.dumps(columns),
            },
        )

    def recent_tool_calls(
        self, limit: int = 100, session_id: str | None = None, source: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_calls"
        where, args = [], []
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        if source:
            where.append("source = ?")
            args.append(source)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def recent_agent_turns(self, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_turns"
        args: list[Any] = []
        if session_id:
            sql += " WHERE session_id = ?"
            args.append(session_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def recent_exports(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM exports ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def source_stats(self, since_seconds: float = 86400) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS calls, SUM(ok) AS ok_calls, AVG(latency_ms) AS avg_latency_ms,"
                " MAX(ts) AS last_call FROM tool_calls WHERE ts >= ? GROUP BY source ORDER BY calls DESC",
                (time.time() - since_seconds,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
