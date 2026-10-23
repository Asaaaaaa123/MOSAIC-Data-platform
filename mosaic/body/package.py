"""A body map as a data package: organs, every evidence item, key facts, figures, Excel and ZIP.

Written to ``<exports_dir>/<name>__<export_id>/`` with the same manifest layout as search packages,
so it appears on the Exports page with its figures and checks.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..datapackage import GroupTable, SavedPackage, _csv_bytes, _figures_sheet, _styled_sheet, _xlsx_value, package_folder_name
from .models import KIND_LABELS, LEVEL_LABELS, LEVEL_OF, BodyMap

ORGAN_COLUMNS = ["organ_key", "organ", "level", "level_meaning", "score", "summary", "evidence_kinds", "n_evidence"]
EVIDENCE_COLUMNS = ["organ_key", "organ", "kind", "kind_meaning", "level", "text", "matched", "value", "unit", "weight",
                    "source", "record_id", "url"]
FACT_COLUMNS = ["label", "value", "source", "url"]


def tables(bm: BodyMap) -> list[GroupTable]:
    organs = GroupTable(key="organs", title="Organs on the body map, strongest evidence first", columns=ORGAN_COLUMNS, rows=[
        {"organ_key": f.key, "organ": f.en, "level": f.level, "level_meaning": LEVEL_LABELS[f.level],
         "score": f.score, "summary": f.summary, "evidence_kinds": "; ".join(f"{k}: {n}" for k, n in f.kinds.items()),
         "n_evidence": len(f.evidence)} for f in bm.organs])

    def ev_rows(items: list[Any]) -> list[dict[str, Any]]:
        return [{"organ_key": ev.organ, "organ": next((f.en for f in bm.organs if f.key == ev.organ), ev.organ),
                 "kind": ev.kind, "kind_meaning": KIND_LABELS[ev.kind], "level": LEVEL_OF[ev.kind], "text": ev.text,
                 "matched": ev.matched, "value": ev.value, "unit": ev.unit, "weight": ev.weight, "source": ev.source,
                 "record_id": ev.record_id, "url": ev.url} for ev in items]

    out = [organs,
           GroupTable(key="organ_evidence", title="Every evidence item: what the database says and the words that placed it",
                      columns=EVIDENCE_COLUMNS, rows=ev_rows([ev for f in bm.organs for ev in f.evidence])),
           GroupTable(key="key_facts", title="Key facts", columns=FACT_COLUMNS,
                      rows=[f.model_dump() for f in bm.facts])]
    if bm.minor:
        out.append(GroupTable(key="minor_evidence", title="Evidence below the threshold for highlighting an organ",
                              columns=EVIDENCE_COLUMNS, rows=ev_rows(bm.minor)))
    return out


def _readme(bm: BodyMap, files: list[dict[str, Any]], figure_names: list[str], created: str) -> tuple[str, list[list[str]]]:
    passed = sum(1 for c in bm.checks if c.ok)
    lines = [f"Mosaic body map: {bm.name} ({bm.kind})", "", f"Created: {created} (UTC)", f"Query: {bm.query}",
             "Identifiers: " + ", ".join(f"{k}: {v}" for k, v in bm.identifiers.items()), "",
             f"{len(bm.organs)} organs with evidence from {', '.join(bm.sources_used) or 'no source'}.",
             f"Data checks: {passed} of {len(bm.checks)} passed."]
    lines += [f"  [{'ok' if c.ok else 'FAILED'}] {c.name}: {c.detail}" for c in bm.checks]
    lines += ["", "Colours / levels:"] + [f"  {lvl:<16} {label}" for lvl, label in LEVEL_LABELS.items()]
    lines += ["", "Notes:"] + [f"  {n}" for n in bm.notes] + [""]
    if bm.errors:
        lines += ["Sources that failed (the map was built without them):"] + [f"  {k}: {v}" for k, v in bm.errors.items()] + [""]
    lines += ["Key facts:"] + [f"  {f.label}: {f.value}" for f in bm.facts] + ["", "Files:"]
    lines += [f"  {f['file']:<26} {f['rows']:>5} rows  {f['title']}" for f in files]
    lines += ["  body_map.json              the whole body map (organs, evidence, facts, checks)"]
    if figure_names:
        lines.append(f"  figures/                   Figure Studio figures (SVG, PNG 300 dpi, PDF + legend): {', '.join(figure_names)}")
    lines += ["  mosaic_data.xlsx           all tables as Excel sheets, with the figures", "",
              "Every organ comes from a database record: organ_evidence.csv gives the source, record id, the text and the",
              "exact words (or GTEx tissue) that placed it on that organ."]
    rows = [[lines[0], ""], [""], ["Created (UTC)", created], ["Query", bm.query], ["Kind", bm.kind],
            ["Data checks", f"{passed} of {len(bm.checks)} passed"]]
    rows += [[("ok: " if c.ok else "FAILED: ") + c.name, c.detail] for c in bm.checks]
    rows += [[""], ["Key facts", ""]] + [[f.label, f.value] for f in bm.facts]
    rows += [[""], ["Notes", ""]] + [["", n] for n in bm.notes]
    rows += [[""], ["Sheet", "Contents"]] + [[f["group"], f"{f['title']} ({f['rows']} rows)"] for f in files]
    return "\r\n".join(lines) + "\r\n", rows


def write_body_package(exports_dir: Path, bm: BodyMap, figure_files: dict[str, bytes] | None = None,
                       notes: list[str] | None = None) -> SavedPackage:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    export_id = uuid.uuid4().hex
    name = f"Body map {bm.name}"
    folder = exports_dir / package_folder_name(name, export_id)
    folder.mkdir(parents=True, exist_ok=False)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    figure_files = dict(figure_files or {})
    figure_names = sorted({k.split("/", 1)[1].rsplit(".", 1)[0] for k in figure_files if not k.endswith(".txt")})

    contents: dict[str, bytes] = {}
    files = []
    group_tables = tables(bm)
    for t in group_tables:
        contents[t.filename] = _csv_bytes(t)
        files.append({"file": t.filename, "group": t.key, "title": t.title, "rows": len(t.rows), "columns": t.columns})
    readme, readme_rows = _readme(bm, files, figure_names, created)
    contents["README.txt"] = readme.encode("utf-8-sig")
    contents["body_map.json"] = bm.model_dump_json(indent=2).encode("utf-8")

    wb = Workbook()
    info = wb.active
    info.title = "README"
    for row in readme_rows:
        info.append([_xlsx_value(v) for v in row])
    info.column_dimensions["A"].width = 34
    info.column_dimensions["B"].width = 110
    info["A1"].font = Font(bold=True, size=13)
    used = {"README"}
    if figure_files:
        _figures_sheet(wb, figure_files, {"body_map": "Body map", "organ_evidence": "Evidence per organ"})
        used.add("Figures")
    for t in group_tables:
        _styled_sheet(wb, t.key, t.columns, [[r.get(c) for c in t.columns] for r in t.rows], used)
    buffer = io.BytesIO()
    wb.save(buffer)
    contents["mosaic_data.xlsx"] = buffer.getvalue()

    manifest = {
        "export_id": export_id, "kind": "package", "name": name, "description": f"Body map of {bm.name} ({bm.kind})",
        "query": bm.query, "group_by": "organ", "created_at": created, "stats": {"organs": len(bm.organs)},
        "notes": list(bm.notes) + list(notes or []), "files": files, "data_files_included": [],
        "figures": [{"name": n, "title": {"body_map": "Body map", "organ_evidence": "Evidence per organ"}.get(n, n),
                     "files": sorted(k.split("/", 1)[1] for k in figure_files if k.split("/", 1)[1].startswith(n + "."))}
                    for n in figure_names],
        "checks": [c.model_dump() for c in bm.checks], "queries": [bm.query],
    }
    contents["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    for filename, data in contents.items():
        (folder / filename).write_bytes(data)
    for rel, data in figure_files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in {**contents, **figure_files}.items():
            archive.writestr(filename, data)
    (folder / "mosaic_data.zip").write_bytes(zbuf.getvalue())
    return SavedPackage(export_id=export_id, name=name, folder=folder, files=files, n_rows=sum(f["rows"] for f in files))
