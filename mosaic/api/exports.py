"""Locating and describing cross-database extraction exports.

Two kinds of export live in ``exports_dir``:

- single tables: ``<export_id>.csv`` + ``<export_id>.json`` (``save_table``)
- data packages: a folder ``<name-slug>__<export_id>/`` with one CSV per group,
  ``mosaic_data.xlsx``, ``README.txt``, ``manifest.json`` and ``mosaic_data.zip``
  (``mosaic.datapackage``)

``export_id`` is 32 lowercase hex characters. Only names matching the patterns
below are ever turned into paths, and resolved paths are re-checked to stay
inside ``exports_dir``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..audit import AuditLog

EXPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
EXPORT_FILE_RE = re.compile(r"^(?P<export_id>[0-9a-f]{32})\.(?P<ext>csv|json)$")
_JSON_META_MAX_BYTES = 8 * 1024 * 1024
PACKAGE_FILE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}\.(?P<ext>csv|xlsx|zip|txt|json)$")
PACKAGE_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json",
}
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


def find_package_folder(exports_dir: Path, export_id: str) -> Path | None:
    """The package folder ``*__<export_id>`` directly inside ``exports_dir``, if any."""
    if not EXPORT_ID_RE.fullmatch(export_id or "") or not exports_dir.exists():
        return None
    base = exports_dir.resolve()
    for path in exports_dir.iterdir():
        if path.is_dir() and path.name.endswith(f"__{export_id}") and path.resolve().parent == base:
            return path
    return None


def resolve_package_file(exports_dir: Path, export_id: str, filename: str) -> tuple[Path, str] | None:
    """Validate a package file name and return ``(path, ext)``; ``None`` for anything else."""
    match = PACKAGE_FILE_RE.fullmatch(filename or "")
    folder = find_package_folder(exports_dir, export_id)
    if match is None or folder is None:
        return None
    path = (folder / filename).resolve()
    if path.parent != folder.resolve() or not path.is_file():
        return None
    return path, match["ext"]


def read_manifest(folder: Path) -> dict[str, Any]:
    path = folder / "manifest.json"
    try:
        if path.stat().st_size > _JSON_META_MAX_BYTES:
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def preview_csv(path: Path, limit: int = 200) -> dict[str, Any]:
    """First ``limit`` rows of a package CSV as ``{columns, rows, truncated}``."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    truncated = False
    for i, row in enumerate(reader):
        if i >= limit:
            truncated = True
            break
        rows.append(row)
    return {"columns": reader.fieldnames or [], "rows": rows, "truncated": truncated}


def package_download_name(manifest: dict[str, Any], export_id: str, filename: str) -> str:
    stem, _, ext = filename.rpartition(".")
    base = _SLUG.sub("_", str(manifest.get("name") or "")).strip("._-")[:60] or f"mosaic-{export_id[:8]}"
    if stem in ("mosaic_data", "README", "manifest"):
        return f"{base}.{ext}" if stem == "mosaic_data" else f"{base}_{stem}.{ext}"
    return f"{base}_{stem}.{ext}"


def _package_entries(exports_dir: Path) -> list[dict[str, Any]]:
    entries = []
    if not exports_dir.exists():
        return entries
    for folder in exports_dir.iterdir():
        name = folder.name
        if not folder.is_dir() or "__" not in name:
            continue
        export_id = name.rsplit("__", 1)[1]
        if not EXPORT_ID_RE.fullmatch(export_id):
            continue
        manifest = read_manifest(folder)
        files = [f for f in manifest.get("files") or [] if isinstance(f, dict) and PACKAGE_FILE_RE.fullmatch(str(f.get("file") or ""))]
        base = f"/api/exports/{export_id}"
        mtime = folder.stat().st_mtime
        entries.append({
            "export_id": export_id,
            "kind": "package",
            "name": manifest.get("name") or name.rsplit("__", 1)[0],
            "description": manifest.get("description"),
            "query": manifest.get("query"),
            "group_by": manifest.get("group_by"),
            "n_rows": sum(int(f.get("rows") or 0) for f in files),
            "columns": [],
            "created_at": _iso(manifest.get("created_at")) or _iso(mtime),
            "session_id": manifest.get("session_id"),
            "formats": ["csv", "xlsx", "zip"],
            "files": [{**{k: f.get(k) for k in ("file", "group", "title", "rows")}, "url": f"{base}/{f['file']}",
                       "preview_url": f"{base}/{f['file']}/preview", "n_columns": len(f.get("columns") or [])} for f in files],
            "xlsx_url": f"{base}/mosaic_data.xlsx",
            "zip_url": f"{base}/mosaic_data.zip",
            "readme_url": f"{base}/README.txt",
            "folder": str(folder),
            "size_bytes": sum(p.stat().st_size for p in folder.iterdir() if p.is_file()),
            "_sort": mtime,
        })
    return entries


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
        entry["kind"] = "table"
        entries.append(entry)
    entries.extend(_package_entries(exports_dir))
    entries.sort(key=lambda e: e["_sort"], reverse=True)
    for entry in entries:
        entry.pop("_sort", None)
    return entries[:limit]
