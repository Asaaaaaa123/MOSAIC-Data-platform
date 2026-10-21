"""Chat sessions (SQLite): exact API history, display transcript and result handles.

Each session stores:

* ``prefix`` - the ``system`` blocks and ``tools`` array frozen when the session
  was created. Every later request in the session reuses them byte-for-byte, so
  prompt caching keeps hitting and Claude's preserved thinking blocks (bound to
  the conversation prefix) stay valid even if the configured sources change
  between server restarts.
* ``messages`` - the exact Messages API history, one row per message, with
  content blocks as the API returned them (thinking signatures, server-tool and
  tool-search results, fallback markers ...). History is append-only; the two
  exceptions are dropping the unanswered tail of a refused run and the
  documented one-time thinking-strip recovery.
* ``transcript`` - what the UI shows (role, text, tool calls, tables).
* ``results`` - the full payload behind each session-scoped result handle
  (``r1``, ``r2`` ...), so extraction and export work across restarts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    prefix TEXT NOT NULL,
    next_result INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_sessions_updated ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS transcript (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    entry TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS results (
    session_id TEXT NOT NULL,
    result_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    tool TEXT NOT NULL,
    source TEXT,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, result_id)
);
"""

TITLE_CHARS = 80


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def make_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


class SessionStore:
    """Thread-safe SQLite store. ``path=None`` keeps everything in memory (tests)."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def from_settings(cls, settings: Any) -> "SessionStore":
        return cls(settings.data_dir / "sessions.db")

    # ---- sessions ----
    def exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row is not None

    def create(self, session_id: str, *, title: str, model: str, prefix: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, title, created_at, updated_at, turns, model, prefix)"
                " VALUES (?, ?, ?, ?, 0, ?, ?)",
                (session_id, make_title(title), now, now, model, _dumps(prefix)),
            )
            self._conn.commit()

    def info(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, title, created_at, updated_at, turns, model FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_row(row) if row else None

    def prefix(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT prefix FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return json.loads(row["prefix"])

    def record_turn(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET turns = turns + 1, updated_at = ? WHERE session_id = ?",
                (time.time(), session_id),
            )
            self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, title, created_at, updated_at, turns, model FROM sessions"
                " ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._session_row(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            for table in ("messages", "transcript", "results"):
                self._conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _session_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "title": row["title"],
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
            "turns": row["turns"],
            "model": row["model"],
        }

    # ---- API message history ----
    def messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Append messages in one transaction (an assistant tool round and its results land together)."""
        if not messages:
            return
        with self._lock:
            start = self._next_seq("messages", session_id)
            self._conn.executemany(
                "INSERT INTO messages (session_id, seq, role, content) VALUES (?, ?, ?, ?)",
                [(session_id, start + i, m["role"], _dumps(m["content"])) for i, m in enumerate(messages)],
            )
            self._touch(session_id)
            self._conn.commit()

    def truncate_messages(self, session_id: str, keep: int) -> None:
        """Drop every message after the first ``keep`` (the unanswered tail of a refused run).

        Sequence numbers are always ``0..n-1`` (appends continue from the max,
        replacements renumber), so position ``keep`` is ``seq = keep``.
        """
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE session_id = ? AND seq >= ?", (session_id, keep))
            self._conn.commit()

    def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._conn.executemany(
                "INSERT INTO messages (session_id, seq, role, content) VALUES (?, ?, ?, ?)",
                [(session_id, i, m["role"], _dumps(m["content"])) for i, m in enumerate(messages)],
            )
            self._conn.commit()

    # ---- display transcript ----
    def append_transcript(self, session_id: str, entry: dict[str, Any]) -> None:
        with self._lock:
            seq = self._next_seq("transcript", session_id)
            self._conn.execute(
                "INSERT INTO transcript (session_id, seq, entry) VALUES (?, ?, ?)", (session_id, seq, _dumps(entry))
            )
            self._touch(session_id)
            self._conn.commit()

    def get_transcript(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry FROM transcript WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [json.loads(r["entry"]) for r in rows]

    # ---- result handles ----
    def allocate_result_id(self, session_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT next_result FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            number = row["next_result"] if row else 1
            self._conn.execute(
                "UPDATE sessions SET next_result = ? WHERE session_id = ?", (number + 1, session_id)
            )
            self._conn.commit()
        return f"r{number}"

    def save_result(self, session_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO results (session_id, result_id, kind, tool, source, created_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    record["result_id"],
                    record["kind"],
                    record["tool"],
                    record.get("source"),
                    record["created_at"],
                    _dumps(record["payload"]),
                ),
            )
            self._conn.commit()

    def load_result(self, session_id: str, result_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_id, kind, tool, source, created_at, payload FROM results"
                " WHERE session_id = ? AND result_id = ?",
                (session_id, result_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "result_id": row["result_id"],
            "kind": row["kind"],
            "tool": row["tool"],
            "source": row["source"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload"]),
        }

    def result_ids(self, session_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result_id FROM results WHERE session_id = ? ORDER BY created_at", (session_id,)
            ).fetchall()
        return [r["result_id"] for r in rows]

    # ---- internals (call with the lock held) ----
    def _next_seq(self, table: str, session_id: str) -> int:
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(seq) + 1, 0) AS n FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"])

    def _touch(self, session_id: str) -> None:
        self._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (time.time(), session_id))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
