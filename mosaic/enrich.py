"""Full records: replace search hits with each source's complete record before exporting.

Search tools return hits (a title, a few fields, often a truncated or missing abstract). Exports
and dashboards should carry the actual data, so ``enrich_records`` calls each source's detail
tool (``get_study``, ``pubmed_fetch``, ``get_record`` ...) for every hit it can, merges the full
record over the hit (identifiers and attributes unioned), and reports what it could not fetch.
PubMed and Semantic Scholar are fetched in batches; everything else concurrently with a cap.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .http import SourceError
from .models import Record

DetailCall = tuple[str, dict[str, Any]]


def _doi(r: Record) -> str | None:
    return r.identifiers.get("doi") or (r.id if r.id.startswith("10.") else None)


# (source, record types or None for any) -> builds the detail call from the hit
_DETAIL: dict[str, tuple[set[str] | None, Callable[[Record], DetailCall | None]]] = {
    "crossref": ({"article", "preprint", "book", "dataset"}, lambda r: ("get_work", {"doi": _doi(r)}) if _doi(r) else None),
    "europepmc": ({"article", "preprint", "book"}, lambda r: ("get_article", {"identifier": r.id})),
    "openalex": ({"article", "preprint", "book", "dataset"}, lambda r: ("get_work", {"id": r.id})),
    "scopus": ({"article", "book"}, lambda r: ("get_abstract", {"identifier": r.identifiers.get("scopus_eid") or r.id})),
    "clarivate": ({"article", "book"}, lambda r: ("get_document", {"identifier": r.id})),
    "springer": ({"article", "book"}, lambda r: ("get_by_doi", {"doi": _doi(r)}) if _doi(r) else None),
    "clinicaltrials": ({"trial"}, lambda r: ("get_study", {"nct": r.identifiers.get("nct") or r.id})),
    "chembl": ({"drug", "compound"}, lambda r: ("get_molecule", {"identifier": r.id})),
    "pubchem": ({"compound", "drug", "substance"},
                lambda r: ("get_compound", {"cid": int(r.id)}) if str(r.id).isdigit() else None),
    "drugcentral": ({"drug", "compound"}, lambda r: ("get_drug", {"id": r.id})),
    "rxnorm": ({"drug"}, lambda r: ("get_concept", {"rxcui": r.identifiers.get("rxcui") or r.id})),
    "openfda": ({"drug_label"}, lambda r: ("get_drug_label", {"value": r.identifiers.get("spl_set_id") or r.id})),
    "uniprot": ({"protein", "gene", "target"}, lambda r: ("get_protein", {"accession": r.identifiers.get("uniprot") or r.id})),
    "hgnc": ({"gene"}, lambda r: ("fetch_gene", {"value": r.identifiers.get("hgnc") or r.id})),
    "rcsb_pdb": ({"structure"}, lambda r: ("get_entry", {"pdb_id": r.identifiers.get("pdb") or r.id})),
    "reactome": ({"pathway"}, lambda r: ("get_pathway", {"pathway_id": r.id}) if r.id.startswith("R-") else None),
    "ols": ({"disease", "phenotype", "ontology_term"}, lambda r: ("get_term", {"term_id": r.id})),
    "monarch": ({"disease", "gene", "phenotype"}, lambda r: ("get_entity", {"entity_id": r.id})),
    "gwas_catalog": ({"study"}, lambda r: ("get_study", {"accession": r.identifiers.get("gwas_study") or r.id})),
    "pgs_catalog": (None, lambda r: ("get_score", {"pgs_id": r.id}) if r.id.upper().startswith("PGS") else None),
    "nci_gdc": ({"study"}, lambda r: ("get_project", {"project_id": r.identifiers.get("gdc_project") or r.id})),
    "cbioportal": ({"study"}, lambda r: ("get_study", {"study_id": r.id})),
    "alliance": ({"gene"}, lambda r: ("get_gene", {"gene_id": r.id})),
    "datacite": ({"dataset", "software", "other"}, lambda r: ("get_doi", {"doi": _doi(r)}) if _doi(r) else None),
    "zenodo": (None, lambda r: ("get_record", {"record": r.identifiers.get("zenodo") or r.id})),
    "figshare": (None, lambda r: ("get_article", {"article_id": r.identifiers.get("figshare") or r.id})),
    "dryad": (None, lambda r: ("get_dataset", {"doi": _doi(r) or r.id})),
    "dataverse": (None, lambda r: ("get_dataset", {"persistent_id": r.id})),
    "osf": (None, lambda r: ("get_node", {"guid": r.identifiers.get("osf") or r.id})),
    "synapse": (None, lambda r: ("get_entity", {"syn_id": r.identifiers.get("synapse") or r.id})),
    "dandi": (None, lambda r: ("get_dandiset", {"dandiset_id": r.identifiers.get("dandi") or r.id})),
    "openneuro": (None, lambda r: ("get_dataset", {"dataset_id": r.identifiers.get("openneuro") or r.id})),
    "hubmap": (None, lambda r: ("get_dataset", {"dataset_id": r.id})),
    "physionet": (None, lambda r: ("get_project", {"project": r.id.split("/")[0]})),
    "tcia": (None, lambda r: ("get_collection", {"collection": r.id})),
    "metabolomics_workbench": ({"study", "dataset"},
                               lambda r: ("get_study", {"study_id": r.id}) if re.match(r"^ST\d+$", r.id) else None),
}


@dataclass
class EnrichStats:
    attempted: int = 0
    enriched: int = 0
    skipped_no_detail_tool: int = 0
    skipped_over_cap: int = 0
    failed: dict[str, int] = field(default_factory=dict)

    def notes(self) -> list[str]:
        out = [f"full records fetched for {self.enriched} of {self.attempted} search hits"]
        if self.skipped_over_cap:
            out.append(f"{self.skipped_over_cap} hits beyond the cap kept as search results (not re-fetched)")
        if self.failed:
            out.append("could not fetch full records from: " + ", ".join(f"{s} ({n})" for s, n in sorted(self.failed.items())))
        return out


def merge(hit: Record, full: Record) -> Record:
    """The full record, keeping anything the hit had that the full record lacks."""
    return full.model_copy(update={
        "identifiers": {**hit.identifiers, **full.identifiers},
        "attributes": {**hit.attributes, **full.attributes},
        "summary": full.summary or hit.summary,
        "title": full.title or hit.title,
        "date": full.date or hit.date,
        "authors": full.authors or hit.authors,
        "url": full.url or hit.url,
    })


def _pick(hit: Record, records: list[Record]) -> Record | None:
    if not records:
        return None
    for record in records:
        if record.id == hit.id or (record.identifiers and set(record.identifiers.items()) & set(hit.identifiers.items())):
            return record
    return records[0] if len(records) == 1 else None


async def enrich_records(
    registry: Any,
    records: list[Record],
    *,
    max_records: int = 120,
    concurrency: int = 8,
    timeout: float = 45.0,
    session_id: str | None = None,
) -> tuple[list[Record], EnrichStats]:
    stats = EnrichStats()
    out = list(records)
    available = set(getattr(registry, "connectors", {}) or {})

    # Batched sources first.
    pubmed = [i for i, r in enumerate(out) if r.source == "ncbi" and r.type in ("article", "preprint") and r.id.isdigit()]
    s2 = [i for i, r in enumerate(out) if r.source == "semantic_scholar" and r.type in ("article", "preprint")]
    single = [i for i, r in enumerate(out) if i not in set(pubmed) | set(s2)]

    async def batch(indices: list[int], source: str, tool: str, key: str, size: int) -> None:
        if source not in available or not indices:
            return
        for start in range(0, len(indices), size):
            chunk = indices[start:start + size]
            stats.attempted += len(chunk)
            try:
                result = await registry.call(source, tool, {key: [out[i].id for i in chunk]}, session_id=session_id, timeout=timeout)
            except SourceError:
                stats.failed[source] = stats.failed.get(source, 0) + len(chunk)
                continue
            by_id = {r.id: r for r in result.records}
            for i in chunk:
                full = by_id.get(out[i].id)
                if full is not None:
                    out[i] = merge(out[i], full)
                    stats.enriched += 1

    await batch(pubmed[:200], "ncbi", "pubmed_fetch", "pmids", 200)
    await batch(s2[:500], "semantic_scholar", "get_papers_batch", "ids", 500)

    calls: list[tuple[int, str, DetailCall]] = []
    for i in single:
        hit = out[i]
        spec = _DETAIL.get(hit.source)
        if spec is None or hit.source not in available:
            stats.skipped_no_detail_tool += 1
            continue
        types, build = spec
        if types is not None and hit.type not in types:
            stats.skipped_no_detail_tool += 1
            continue
        try:
            call = build(hit)
        except (KeyError, ValueError, TypeError):
            call = None
        if call is None:
            stats.skipped_no_detail_tool += 1
            continue
        calls.append((i, hit.source, call))
    if len(calls) > max_records:
        stats.skipped_over_cap = len(calls) - max_records
        calls = calls[:max_records]

    semaphore = asyncio.Semaphore(concurrency)

    async def one(i: int, source: str, call: DetailCall) -> None:
        tool, params = call
        async with semaphore:
            stats.attempted += 1
            try:
                result = await registry.call(source, tool, params, session_id=session_id, timeout=timeout)
            except SourceError:
                stats.failed[source] = stats.failed.get(source, 0) + 1
                return
        full = _pick(out[i], result.records)
        if full is not None:
            out[i] = merge(out[i], full)
            stats.enriched += 1

    await asyncio.gather(*(one(i, s, c) for i, s, c in calls))
    return out, stats
