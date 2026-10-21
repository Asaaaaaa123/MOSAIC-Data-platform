"""Locating and describing cross-database extraction exports.

The agent writes ``<exports_dir>/<export_id>.csv`` and ``<export_id>.json``
(``export_id`` = 32 lowercase hex characters) and records the export in the
audit log. Only names matching that pattern are ever turned into paths.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..audit import AuditLog

EXPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
EXPORT_FILE_RE = re.compile(r"^(?P<export_id>[0-9a-f]{32})\.(?P<ext>csv|json)$")
_JSON_META_MAX_BYTES = 8 * 1024 * 1024
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def export_urls(export_id: str) -> dict[str, str]:
    return {"csv_url": f"/api/exports/{export_id}.csv", "json_url": f"/api/exports/{export_id}.json"}


def resolve_export_file(exports_dir: Path, filename: str) -> tuple[str, str, Path] | None:
    """Validate ``<32 hex>.(csv|json)`` and return ``(export_id, ext, path)``.

    Returns ``None`` for anything else (traversal attempts, other extensions,
    uppercase hex, absolute paths ...). The resolved path is re-checked to sit
    directly inside ``exports_dir``.
    """
    match = EXPORT_FILE_RE.fullmatch(filename or "")
    if match is None:
        return None
    base = exports_dir.resolve()
    path = (base / f"{match['export_id']}.{match['ext']}").resolve()
    if path.parent != base:
        return None
    return match["export_id"], match["ext"], path


def download_name(name: str | None, export_id: str, ext: str) -> str:
    slug = _SLUG.sub("_", (name or "").strip()).strip("._-")[:80]
    return f"{slug or 'mosaic-export-' + export_id[:8]}.{ext}"


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    return str(ts)


def _audit_rows_by_export(audit: AuditLog | None, limit: int = 5000) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if audit is None:
        return rows
    for row in audit.recent_exports(limit=limit):
        stem = Path(str(row.get("path") or "")).stem
        export_id = stem if EXPORT_ID_RE.fullmatch(stem) else row.get("id")
        if export_id and EXPORT_ID_RE.fullmatch(str(export_id)) and export_id not in rows:
            rows[str(export_id)] = row
    return rows


def _columns(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(c) for c in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return [str(c) for c in parsed] if isinstance(parsed, list) else []
    return []


def audit_name(audit: AuditLog | None, export_id: str) -> str | None:
    row = _audit_rows_by_export(audit).get(export_id)
    return row.get("name") if row else None


def list_exports(exports_dir: Path, audit: AuditLog | None, limit: int = 200) -> list[dict[str, Any]]:
    """Exports present on disk, newest first, merged with their audit rows."""
    files: dict[str, dict[str, Path]] = {}
    if exports_dir.exists():
        for path in exports_dir.iterdir():
            match = EXPORT_FILE_RE.fullmatch(path.name)
            if match and path.is_file():
                files.setdefault(match["export_id"], {})[match["ext"]] = path
    audit_rows = _audit_rows_by_export(audit)
    entries: list[dict[str, Any]] = []
    for export_id, by_ext in files.items():
        row = audit_rows.get(export_id) or {}
        meta: dict[str, Any] = {}
        json_path = by_ext.get("json")
        if json_path is not None and json_path.stat().st_size <= _JSON_META_MAX_BYTES:
            try:
                loaded = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
            except (OSError, ValueError):
                meta = {}
        rows = meta.get("rows")
        newest = max(p.stat().st_mtime for p in by_ext.values())
        entry = {
            "export_id": export_id,
            "name": meta.get("name") or row.get("name") or f"export {export_id[:8]}",
            "description": meta.get("description"),
            "n_rows": len(rows) if isinstance(rows, list) else row.get("n_rows"),
            "columns": _columns(meta.get("columns")) or _columns(row.get("columns")),
            "created_at": _iso(meta.get("created_at")) or _iso(row.get("ts")) or _iso(newest),
            "session_id": meta.get("session_id") or row.get("session_id"),
            "formats": sorted(by_ext),
            "size_bytes": sum(p.stat().st_size for p in by_ext.values()),
            "_sort": row.get("ts") or newest,
        }
        if "csv" in by_ext:
            entry["csv_url"] = export_urls(export_id)["csv_url"]
        if "json" in by_ext:
            entry["json_url"] = export_urls(export_id)["json_url"]
        entries.append(entry)
    entries.sort(key=lambda e: e["_sort"], reverse=True)
    for entry in entries:
        entry.pop("_sort", None)
    return entries[:limit]
