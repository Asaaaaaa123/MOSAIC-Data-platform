"""Consolidation: turn raw multi-source records into cleaned, de-duplicated, evaluated entities.

``curate(records)``
1. cleans every record (``clean.py``)
2. merges records describing the same entity: shared normalized identifiers
   (``crosslink.merge``), plus identical titles of the same kind within a year
   when no identifier was shared
3. builds one ``CuratedEntity`` per group: the best value of each field taken from
   the most reliable source (field-level provenance kept), identifiers unioned,
   disagreements between sources recorded as conflicts
4. evaluates each entity (``evaluate.py``) and orders them: entities confirmed by
   several databases first, then by score
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from ..crosslink.merge import LINKING_KEYS, cluster_records
from ..models import Record
from .clean import CleaningLog, clean_record
from .evaluate import Evaluation, completeness, evaluate, profile

_TITLE_MERGE_TYPES = {"article", "preprint", "dataset", "book"}

# Identifier kinds where one entity legitimately has several valid values (ingredient vs branded
# RxCUI, many NDCs/ATC codes, print vs electronic ISSN, MeSH descriptor vs supplementary concept...):
# differing values are alternatives, not conflicts.
MULTI_VALUED_KEYS = {
    "rxcui", "ndc", "spl_set_id", "fda_application", "atc", "issn", "isbn", "refseq", "mesh", "umls",
    "pubchem_sid", "ensembl_transcript", "hgvs", "cas", "kegg_drug", "kegg_compound", "smiles",
}
# Search-engine bookkeeping, not data about the entity.
NOISE_FIELDS = {"match", "match_type", "matched", "matched_on", "search_score", "relevance", "rank", "highlight", "highlights", "_score"}


class CuratedEntity(BaseModel):
    entity_id: str
    type: str
    title: str | None = None
    date: str | None = None
    authors: list[str] = Field(default_factory=list)
    summary: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, Any] = Field(default_factory=dict, description="Cleaned structured data, best source first")
    field_sources: dict[str, str] = Field(default_factory=dict, description="Which source supplied each field")
    conflicts: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list, description="Provenance: source, id, url")
    evaluation: Evaluation

    def compact(self, summary_chars: int = 500, max_fields: int = 25) -> dict[str, Any]:
        """The entity as data for Claude / the UI: values first, provenance last."""
        out: dict[str, Any] = {"entity": self.entity_id, "type": self.type}
        for key in ("title", "date"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.authors:
            out["authors"] = self.authors[:6] + ([f"+{len(self.authors) - 6} more"] if len(self.authors) > 6 else [])
        if self.identifiers:
            out["identifiers"] = self.identifiers
        if self.fields:
            out["data"] = {k: _shorten(v) for k, v in list(self.fields.items())[:max_fields]}
        if self.summary:
            out["summary"] = self.summary if len(self.summary) <= summary_chars else self.summary[:summary_chars].rstrip() + " ..."
        out["quality"] = self.evaluation.compact()
        if self.conflicts:
            out["conflicts"] = self.conflicts
        out["sources"] = [f"{s['source']}:{s['id']}" for s in self.sources]
        return out


class CuratedResult(BaseModel):
    entities: list[CuratedEntity] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    def to_llm(self, max_chars: int = 30_000, limit: int = 40) -> str:
        entities = [e.compact() for e in self.entities[:limit]]
        payload = {"stats": self.stats, "entities": entities}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        summary_chars = 300
        while len(text) > max_chars and entities:
            if summary_chars > 0:
                summary_chars = 120 if summary_chars == 300 else 0
                entities = [e.compact(summary_chars=summary_chars or 1, max_fields=12) for e in self.entities[: len(entities)]]
                if summary_chars == 0:
                    for e in entities:
                        e.pop("summary", None)
            else:
                entities = entities[: max(1, len(entities) * 3 // 4)] if len(entities) > 1 else []
            payload = {"stats": self.stats, "entities": entities}
            if len(entities) < len(self.entities[:limit]):
                payload["note"] = f"showing {len(entities)} of {len(self.entities)} entities to fit the context budget"
            text = json.dumps(payload, ensure_ascii=False, default=str)
        return text


def _shorten(value: Any, max_str: int = 300, max_items: int = 12) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str].rstrip() + " ..."
    if isinstance(value, list):
        items = [_shorten(v, max_str, max_items) for v in value[:max_items]]
        return items + ([f"... +{len(value) - max_items} more"] if len(value) > max_items else [])
    if isinstance(value, dict):
        return {k: _shorten(v, max_str, max_items) for k, v in list(value.items())[:max_items]}
    return value


def _norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _title_similar(a: str, b: str) -> bool:
    ta, tb = set(_norm_title(a).split()), set(_norm_title(b).split())
    if not ta or not tb:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5 or _norm_title(a) in _norm_title(b) or _norm_title(b) in _norm_title(a)


def _merge_by_title(groups: list[list[Record]]) -> list[list[Record]]:
    """Join groups that share no identifier but have the same title, kind and year (within 1)."""
    merged: list[list[Record]] = []
    index: dict[tuple[str, str], int] = {}
    for group in groups:
        lead = group[0]
        title = _norm_title(lead.title)
        key = (lead.type, title)
        if lead.type in _TITLE_MERGE_TYPES and len(title) >= 25 and key in index:
            target = merged[index[key]]
            y1, y2 = _year(lead.date), _year(target[0].date)
            if y1 is None or y2 is None or abs(y1 - y2) <= 1:
                target.extend(group)
                continue
        if lead.type in _TITLE_MERGE_TYPES and len(title) >= 25:
            index.setdefault(key, len(merged))
        merged.append(list(group))
    return merged


# Which source is the reference for each kind of entity (its fields and title win).
TYPE_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "drug": ("chembl", "drugcentral", "rxnorm", "openfda", "clinpgx", "pubchem", "opentargets"),
    "compound": ("chembl", "pubchem", "drugcentral", "bindingdb"),
    "gene": ("hgnc", "ncbi", "uniprot", "opentargets", "alliance", "gnomad", "gtex"),
    "protein": ("uniprot", "hgnc", "rcsb_pdb", "opentargets"),
    "target": ("opentargets", "chembl", "uniprot"),
    "disease": ("ols", "monarch", "opentargets", "ncbi"),
    "phenotype": ("ols", "monarch"),
    "article": ("ncbi", "europepmc", "crossref", "scopus", "clarivate", "openalex", "semantic_scholar", "springer"),
    "preprint": ("europepmc", "crossref", "openalex", "semantic_scholar"),
    "trial": ("clinicaltrials",),
    "dataset": ("datacite", "dryad", "zenodo", "figshare", "dataverse", "osf"),
    "variant": ("ncbi", "gnomad", "civic"),
    "pathway": ("reactome",),
}


def _rank(record: Record, kind: str | None = None) -> tuple[int, int, float]:
    priority = TYPE_SOURCE_PRIORITY.get(kind or record.type, ())
    position = priority.index(record.source) if record.source in priority else len(priority)
    return (position, profile(record.source).tier, -completeness(record))


def _consolidate(group: list[Record], entity_id: str) -> CuratedEntity:
    kind = Counter(r.type for r in group).most_common(1)[0][0]
    ordered = sorted(group, key=lambda r: _rank(r, kind))
    best = ordered[0]
    titled = [r for r in ordered if r.title]
    dated = [r for r in ordered if r.date]
    if kind in {"article", "preprint", "book"} and dated:
        # first publication: the earliest year, then the most precise date within it
        first_year = min((r.date or "9999")[:4] for r in dated)
        same_year = [r.date for r in dated if r.date and r.date.startswith(first_year)]
        date = min(same_year, key=lambda d: (-len(d), d))
    else:
        date = dated[0].date if dated else None

    identifiers: dict[str, str] = {}
    votes: dict[str, dict[str, set[str]]] = {}
    for record in ordered:
        for key, value in record.identifiers.items():
            identifiers.setdefault(key, value)
            votes.setdefault(key, {}).setdefault(value, set()).add(record.source)
    conflicts: dict[str, list[dict[str, Any]]] = {}
    for key, values in votes.items():
        if len(values) > 1 and key in LINKING_KEYS and key not in MULTI_VALUED_KEYS:
            conflicts[key] = [{"value": v, "sources": sorted(s)} for v, s in sorted(values.items(), key=lambda kv: -len(kv[1]))]
    if len(titled) > 1 and not all(_title_similar(titled[0].title or "", r.title or "") for r in titled[1:]):
        conflicts["title"] = [{"value": r.title, "sources": [r.source]} for r in titled[:4]]
    years = {y for y in (_year(r.date) for r in dated) if y}
    if kind in {"article", "preprint", "book", "trial", "dataset"} and years and max(years) - min(years) > 1:
        conflicts["year"] = [{"value": y, "sources": sorted({r.source for r in dated if _year(r.date) == y})} for y in sorted(years)]

    fields: dict[str, Any] = {}
    field_sources: dict[str, str] = {}
    for record in ordered:
        for key, value in record.attributes.items():
            if key not in fields and key not in NOISE_FIELDS:
                fields[key] = value
                field_sources[key] = record.source

    summaries = [r for r in ordered if r.summary]
    top_tier = profile(best.source).tier
    good = [r for r in summaries if profile(r.source).tier <= top_tier + 1] or summaries
    summary = max(good, key=lambda r: len(r.summary or "")).summary if good else None
    authors = next((r.authors for r in ordered if r.authors), [])

    return CuratedEntity(
        entity_id=entity_id,
        type=kind,
        title=(titled[0].title if titled else None),
        date=date,
        authors=authors,
        summary=summary,
        identifiers=identifiers,
        fields=fields,
        field_sources=field_sources,
        conflicts=conflicts,
        sources=[{"source": r.source, "id": r.id, "url": r.url, "title": r.title} for r in ordered],
        evaluation=evaluate(ordered, conflicts=conflicts),
    )


def curate(records: list[Record]) -> CuratedResult:
    log = CleaningLog()
    cleaned = [clean_record(r, log) for r in records]
    groups = _merge_by_title(cluster_records(cleaned))
    entities = [_consolidate(g, f"e{i + 1}") for i, g in enumerate(groups)]
    entities.sort(key=lambda e: (-(len(e.evaluation.corroborated_by) > 1), -e.evaluation.score))
    for i, entity in enumerate(entities, start=1):
        entity.entity_id = f"e{i}"
    grades = Counter(e.evaluation.grade for e in entities)
    flagged = Counter(flag.split(":")[0] for e in entities for flag in e.evaluation.flags)
    return CuratedResult(
        entities=entities,
        stats={
            "records_in": len(records),
            "entities_out": len(entities),
            "duplicates_merged": len(records) - len(entities),
            "multi_source_entities": sum(1 for e in entities if len(e.evaluation.corroborated_by) > 1),
            "grades": dict(sorted(grades.items())),
            "flags": dict(flagged.most_common()),
            "cleaning": log.summary(),
        },
    )
