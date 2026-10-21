"""The actual data of datasets: file inventory, plus download and preview of small open tables.

Repository connectors (Zenodo, Figshare, Dryad, Dataverse, OSF ...) return each dataset's files as
``attributes["files"] = [{name, size, link, checksum/md5/digest, mimetype/content_type, restricted}]``.
``collect_data_files`` turns those into rows of ``data_files.csv`` and downloads the files that are
safe and small enough to include in the package:

- https links on known repository hosts only (the link comes from the repository API, never from a
  user), redirects to the repository's storage are followed
- open files only (``restricted`` false), tabular / text formats only (csv, tsv, tab, txt, json, xlsx,
  xls), at most ``MAX_FILE_BYTES`` each and ``MAX_TOTAL_BYTES`` per package
- files are stored as data (never executed or imported); spreadsheets are read with ``data_only``

Each downloaded table also gets a preview (first ``PREVIEW_ROWS`` rows) for the Excel workbook.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .models import Record

ALLOWED_HOSTS = {
    "zenodo.org", "sandbox.zenodo.org", "ndownloader.figshare.com", "figshare.com", "api.figshare.com",
    "datadryad.org", "dataverse.harvard.edu", "borealisdata.ca", "dataverse.no", "osf.io", "files.osf.io",
    "files.de-1.osf.io", "files.us.osf.io",
}
TABULAR_EXTENSIONS = (".csv", ".tsv", ".tab", ".txt", ".json", ".xlsx", ".xls")
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 25
PREVIEW_ROWS = 200
FILE_COLUMNS = [
    "dataset", "dataset_source", "dataset_id", "dataset_doi", "file_name", "format", "size_bytes",
    "checksum", "open_access", "included_in_package", "package_path", "preview_sheet", "download_url", "note",
]
_SAFE = re.compile(r"[^\w.\-]+")


@dataclass
class DataFilesResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)  # "files/<folder>/<name>" -> bytes
    previews: list[tuple[str, list[str], list[list[Any]]]] = field(default_factory=list)  # (sheet title, header, rows)
    notes: list[str] = field(default_factory=list)


def _ext(name: str) -> str:
    low = name.lower()
    return next((e for e in TABULAR_EXTENSIONS if low.endswith(e)), "")


def _allowed(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "").lower() in ALLOWED_HOSTS


def inventory(records: list[Record]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        files = record.attributes.get("files")
        if not isinstance(files, list):
            continue
        for item in files:
            if not isinstance(item, dict) or not (item.get("name") or item.get("key")):
                continue
            name = str(item.get("name") or item.get("key"))
            link = item.get("link") or item.get("url") or item.get("download_url")
            key = f"{record.source}:{record.id}:{name}"
            if key in seen:
                continue
            seen.add(key)
            restricted = bool(item.get("restricted")) or bool(item.get("embargoed"))
            rows.append({
                "dataset": record.title,
                "dataset_source": record.source,
                "dataset_id": record.id,
                "dataset_doi": record.identifiers.get("doi"),
                "file_name": name,
                "format": _ext(name).lstrip(".") or item.get("mimetype") or item.get("content_type") or item.get("type"),
                "size_bytes": item.get("size"),
                "checksum": item.get("checksum") or item.get("md5") or item.get("digest"),
                "open_access": not restricted,
                "included_in_package": False,
                "package_path": None,
                "preview_sheet": None,
                "download_url": link,
                "note": None,
            })
    return rows


def _package_path(source: str, dataset_id: str, name: str, taken: set[str]) -> str:
    """Short, safe relative path: Windows paths must stay under 260 characters in total."""
    folder = _SAFE.sub("_", f"{source}_{dataset_id}").strip("_")[:40]
    ext = _ext(name) or ""
    stem = _SAFE.sub("_", name[: len(name) - len(ext)] if ext else name).strip("_")[:50] or "file"
    path, n = f"files/{folder}/{stem}{ext}", 2
    while path in taken:
        path, n = f"files/{folder}/{stem}_{n}{ext}", n + 1
    return path


def _preview(name: str, data: bytes) -> tuple[list[str], list[list[Any]]] | None:
    ext = _ext(name)
    try:
        if ext in (".csv", ".tsv", ".tab", ".txt"):
            text = data.decode("utf-8-sig", errors="replace")
            sample = text[:20_000]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            except csv.Error:
                dialect = csv.excel_tab if ext in (".tsv", ".tab") else csv.excel
            reader = csv.reader(io.StringIO(text), dialect)
            rows = [row for _, row in zip(range(PREVIEW_ROWS + 1), reader)]
            if len(rows) < 2 or max(len(r) for r in rows) < 2:
                return None
            return [str(c) for c in rows[0]], rows[1:]
        if ext == ".xlsx":
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = [list(r) for _, r in zip(range(PREVIEW_ROWS + 1), ws.iter_rows(values_only=True))]
            wb.close()
            if len(rows) < 2:
                return None
            return [str(c) if c is not None else "" for c in rows[0]], rows[1:]
        if ext == ".json":
            parsed = json.loads(data.decode("utf-8", errors="replace"))
            if isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed[:50]):
                header = list(dict.fromkeys(k for x in parsed[:PREVIEW_ROWS] for k in x))[:60]
                return header, [[x.get(k) for k in header] for x in parsed[:PREVIEW_ROWS]]
    except Exception:  # noqa: BLE001 - a malformed file only loses its preview
        return None
    return None


async def collect_data_files(http: Any, records: list[Record], *, download: bool = True) -> DataFilesResult:
    """Inventory every dataset file; download and preview the small open tables."""
    result = DataFilesResult(rows=inventory(records))
    if not result.rows:
        return result
    if not download:
        result.notes.append(f"{len(result.rows)} dataset files listed (download disabled)")
        return result
    total = 0
    downloaded = 0
    for row in result.rows:
        name, url, size = row["file_name"], row["download_url"], row["size_bytes"]
        reason = None
        if not row["open_access"]:
            reason = "restricted: request access from the repository"
        elif not _ext(name):
            reason = "not a tabular/text file: download it from download_url"
        elif not url or not _allowed(url):
            reason = "link is not on a known repository host"
        elif isinstance(size, (int, float)) and size > MAX_FILE_BYTES:
            reason = f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB: download it from download_url"
        elif downloaded >= MAX_FILES or (isinstance(size, (int, float)) and total + size > MAX_TOTAL_BYTES):
            reason = "package size limit reached: download it from download_url"
        if reason:
            row["note"] = reason
            continue
        try:
            response = await http.request("datafiles", "GET", url, cache_ttl=0, timeout=90, retries=1,
                                          headers={"Accept": "*/*"})
        except Exception as exc:  # noqa: BLE001 - one failed file must not break the package
            row["note"] = f"download failed: {str(exc)[:120]}"
            continue
        data = response.content
        if len(data) > MAX_FILE_BYTES:
            row["note"] = "larger than the size limit once downloaded: not included"
            continue
        path = _package_path(row["dataset_source"], row["dataset_id"], name, set(result.blobs))
        result.blobs[path] = data
        total += len(data)
        downloaded += 1
        row.update(included_in_package=True, package_path=path, size_bytes=row["size_bytes"] or len(data))
        preview = _preview(name, data)
        if preview is not None:
            sheet = f"data_{len(result.previews) + 1}"
            row["preview_sheet"] = sheet
            result.previews.append((sheet, *preview))
    result.notes.append(
        f"{len(result.rows)} dataset files listed; {downloaded} small open tables included in the package "
        f"({total / (1024 * 1024):.1f} MB), {len(result.previews)} previewed in the workbook"
    )
    return result
