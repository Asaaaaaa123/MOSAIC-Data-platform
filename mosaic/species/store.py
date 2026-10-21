"""Persist cross-species comparisons as ``<data_dir>/species/<comparison_id>.json``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import Settings
from .models import SpeciesComparison

COMPARISON_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _dir(settings: Settings) -> Path:
    path = settings.data_dir / "species"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_comparison(settings: Settings, comparison: SpeciesComparison) -> Path:
    if not COMPARISON_ID_RE.fullmatch(comparison.comparison_id):
        raise ValueError("comparison_id must be 32 lowercase hex characters")
    path = _dir(settings) / f"{comparison.comparison_id}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(comparison.model_dump_json(exclude={"records"}, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def load_comparison(settings: Settings, comparison_id: str) -> SpeciesComparison | None:
    if not COMPARISON_ID_RE.fullmatch(comparison_id or ""):
        return None
    path = _dir(settings) / f"{comparison_id}.json"
    if not path.is_file():
        return None
    try:
        return SpeciesComparison.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def list_comparisons(settings: Settings, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first: ``[{comparison_id, query, compound, created_at, targets, n_species}]``."""
    items = []
    for path in sorted(_dir(settings).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        if not COMPARISON_ID_RE.fullmatch(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append({
            "comparison_id": path.stem,
            "query": data.get("query"),
            "compound": (data.get("compound") or {}).get("name") or data.get("query"),
            "created_at": data.get("created_at"),
            "targets": [t.get("gene_symbol") for t in data.get("targets") or []],
            "n_species": len(data.get("species") or []),
        })
    return items
