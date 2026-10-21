"""Write extraction tables to ``<exports_dir>/<export_id>.csv`` and ``.json``.

The API serves exactly ``/api/exports/<export_id>.csv`` and ``.json``
(``export_id`` = ``uuid4().hex``). The JSON file has the shape::

    {"export_id", "name", "description", "columns", "rows", "created_at",
     "session_id", "provenance"}

with ``rows`` as a list of objects keyed by column. CSV cells that a
spreadsheet would evaluate as a formula are neutralized; the JSON keeps the
original values.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Leading characters that make Excel / LibreOffice / Sheets treat a cell as a
# formula (OWASP "CSV injection"); tab and carriage return are included because
# some importers strip them before evaluating.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def csv_cell(value: Any) -> str:
    """Render one value for CSV, neutralizing formula injection.

    Strings that start with ``= + - @`` (or tab / CR) get a leading apostrophe
    unless the whole cell is a plain number such as ``-5`` or ``+1.2e3``.
    Lists and dicts are written as compact JSON.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(value)
    if text.startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.fullmatch(text):
        return "'" + text
    return text


@dataclass(frozen=True)
class SavedExport:
    export_id: str
    name: str
    csv_path: Path
    json_path: Path
    n_rows: int
    columns: list[str]

    @property
    def csv_url(self) -> str:
        return f"/api/exports/{self.export_id}.csv"

    @property
    def json_url(self) -> str:
        return f"/api/exports/{self.export_id}.json"


def _atomic_write(path: Path, data: str, encoding: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="") as handle:
        handle.write(data)
    os.replace(tmp, path)


def write_export(
    exports_dir: Path,
    *,
    name: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    description: str | None = None,
    session_id: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> SavedExport:
    export_id = uuid.uuid4().hex
    exports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = exports_dir / f"{export_id}.csv"
    json_path = exports_dir / f"{export_id}.json"

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([csv_cell(c) for c in columns])
    for row in rows:
        writer.writerow([csv_cell(row.get(c)) for c in columns])
    # utf-8-sig: Excel only detects UTF-8 in a CSV that starts with a BOM.
    _atomic_write(csv_path, buffer.getvalue(), "utf-8-sig")

    document = {
        "export_id": export_id,
        "name": name,
        "description": description,
        "columns": columns,
        "rows": [{c: row.get(c) for c in columns} for row in rows],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "provenance": provenance or {},
    }
    _atomic_write(json_path, json.dumps(document, ensure_ascii=False, indent=2, default=str), "utf-8")
    return SavedExport(export_id, name, csv_path, json_path, len(rows), list(columns))
