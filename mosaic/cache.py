"""SQLite-backed TTL cache for upstream HTTP responses."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CachedResponse:
    status: int
    headers: dict[str, str]
    content: bytes


class ResponseCache:
    def __init__(self, path: Path | str | None, default_ttl: int = 21600):
        self.default_ttl = default_ttl
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS http_cache ("
            " key TEXT PRIMARY KEY, source TEXT, status INTEGER, headers TEXT,"
            " content BLOB, expires_at REAL)"
        )
        self._conn.commit()
        self._writes = 0

    @property
    def enabled(self) -> bool:
        return self.default_ttl > 0

    def get(self, key: str) -> CachedResponse | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, headers, content, expires_at FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        status, headers, content, expires_at = row
        if expires_at < time.time():
            return None
        return CachedResponse(status=status, headers=json.loads(headers), content=content)

    def set(self, key: str, source: str, status: int, headers: dict[str, str], content: bytes, ttl: int) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO http_cache (key, source, status, headers, content, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, source, status, json.dumps(headers), content, time.time() + ttl),
            )
            self._writes += 1
            if self._writes % 500 == 0:
                self._conn.execute("DELETE FROM http_cache WHERE expires_at < ?", (time.time(),))
            self._conn.commit()

    def clear(self, source: str | None = None) -> int:
        with self._lock:
            if source:
                cur = self._conn.execute("DELETE FROM http_cache WHERE source = ?", (source,))
            else:
                cur = self._conn.execute("DELETE FROM http_cache")
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
