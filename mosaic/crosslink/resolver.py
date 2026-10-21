"""Entity resolution: map a gene, drug, disease, article or variant to identifiers across databases.

Cross-database work starts by pinning an entity to identifiers (gene symbol ->
HGNC / Ensembl / UniProt / Entrez; drug name -> ChEMBL / PubChem / InChIKey /
RxCUI / UNII / DrugCentral; disease -> MONDO / EFO / DOID / MeSH; article ->
DOI / PMID / PMCID / OpenAlex / S2; variant -> rsID / ClinVar / gnomAD). The
resolver asks the authoritative sources for each kind in parallel, takes each
source's best match, and reports the identifiers the sources agree on plus any
conflicts, so Claude (or a user) can query every other database with the right
IDs instead of free text.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..http import SourceError
from ..ids import normalize_doi
from ..models import Record

EntityKind = Literal["gene", "drug", "disease", "article", "variant"]

_DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
_PMID = re.compile(r"^(?:pmid:?\s*)?(\d{1,9})$", re.IGNORECASE)
_PMCID = re.compile(r"^(?:pmcid:?\s*)?(pmc\d+)$", re.IGNORECASE)
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$", re.IGNORECASE)
_RSID = re.compile(r"^rs\d+$", re.IGNORECASE)
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-.]{0,19}$")

# Keys that identify the entity itself (not related entities) for each kind.
_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "gene": ("gene_symbol", "hgnc", "entrez_gene", "ensembl_gene", "uniprot", "omim"),
    "drug": ("chembl", "pubchem_cid", "inchikey", "unii", "rxcui", "drugcentral", "drugbank", "cas", "atc", "mesh"),
    "disease": ("mondo", "efo", "doid", "mesh", "omim", "orphanet", "umls", "hp", "icd10"),
    "article": ("doi", "pmid", "pmcid", "openalex", "s2", "arxiv", "mag", "scopus_eid", "wos_uid"),
    "variant": ("rsid", "clinvar", "gnomad_variant", "hgvs"),
}


@dataclass(frozen=True)
class _Probe:
    source: str
    tool: str
    params: dict[str, Any]


class Candidate(BaseModel):
    source: str
    tool: str
    id: str
    title: str | None = None
    url: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    exact_match: bool = False


class Agreement(BaseModel):
    value: str
    sources: list[str]


class ResolutionResult(BaseModel):
    kind: EntityKind
    query: str
    identifiers: dict[str, str] = Field(
        default_factory=dict, description="Best identifier per key (majority across sources, exact matches first)"
    )
    agreement: dict[str, Agreement] = Field(default_factory=dict)
    conflicts: dict[str, list[Agreement]] = Field(default_factory=dict)
    candidates: list[Candidate] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    records: list[Record] = Field(default_factory=list, exclude=True)


def _article_probes(query: str) -> list[_Probe]:
    text = query.strip()
    if m := _DOI.match(text):
        doi = normalize_doi(m.group(1))
        return [
            _Probe("crossref", "get_work", {"doi": doi}),
            _Probe("openalex", "get_work", {"id": f"doi:{doi}"}),
            _Probe("semantic_scholar", "get_paper", {"paper_id": f"DOI:{doi}"}),
            _Probe("europepmc", "get_article", {"identifier": doi}),
            _Probe("unpaywall", "get_oa_status", {"doi": doi}),
        ]
    if m := _PMID.match(text):
        pmid = m.group(1)
        return [
            _Probe("ncbi", "pubmed_fetch", {"pmids": [pmid]}),
            _Probe("europepmc", "get_article", {"identifier": pmid}),
            _Probe("openalex", "get_work", {"id": f"pmid:{pmid}"}),
            _Probe("semantic_scholar", "get_paper", {"paper_id": f"PMID:{pmid}"}),
        ]
    if m := _PMCID.match(text):
        pmcid = m.group(1).upper()
        return [
            _Probe("europepmc", "get_article", {"identifier": pmcid}),
            _Probe("openalex", "get_work", {"id": f"pmcid:{pmcid}"}),
            _Probe("semantic_scholar", "get_paper", {"paper_id": f"PMCID:{pmcid}"}),
        ]
    return [
        _Probe("crossref", "search_works", {"query": text, "limit": 3}),
        _Probe("openalex", "search_works", {"query": text, "limit": 3}),
        _Probe("europepmc", "search", {"query": f'TITLE:"{text}"' if len(text) > 25 else text, "limit": 3}),
    ]


def _probes(kind: EntityKind, query: str) -> list[_Probe]:
    q = query.strip()
    if kind == "gene":
        symbolish = bool(_SYMBOL.match(q))
        return [
            _Probe("hgnc", "fetch_gene", {"value": q, "field": "auto"}),
            _Probe(
                "uniprot",
                "search_proteins",
                {"query": f"gene_exact:{q}" if symbolish else q, "organism_id": 9606, "reviewed": True, "limit": 3},
            ),
            _Probe("ncbi", "entrez_search", {"db": "gene", "query": f"{q}[sym] AND human[orgn]" if symbolish else f"{q} AND human[orgn]", "limit": 3}),
            _Probe("opentargets", "search", {"query": q, "entities": ["target"], "limit": 3}),
        ]
    if kind == "drug":
        if _INCHIKEY.match(q):
            return [
                _Probe("pubchem", "get_compound", {"inchikey": q.upper()}),
                _Probe("chembl", "get_molecule", {"identifier": q.upper()}),
            ]
        return [
            _Probe("pubchem", "get_compound", {"name": q}),
            _Probe("chembl", "get_molecule", {"identifier": q}),
            _Probe("rxnorm", "search", {"query": q, "limit": 3}),
            _Probe("drugcentral", "search_drugs", {"query": q, "limit": 3}),
            _Probe("opentargets", "search", {"query": q, "entities": ["drug"], "limit": 3}),
        ]
    if kind == "disease":
        return [
            _Probe("ols", "search_terms", {"query": q, "ontology": "mondo", "limit": 3}),
            _Probe("ols", "search_terms", {"query": q, "ontology": "efo", "limit": 3}),
            _Probe("ols", "search_terms", {"query": q, "ontology": "doid", "limit": 3}),
            _Probe("monarch", "search", {"query": q, "category": "disease", "limit": 3}),
            _Probe("opentargets", "search", {"query": q, "entities": ["disease"], "limit": 3}),
            _Probe("ncbi", "entrez_search", {"db": "mesh", "query": q, "limit": 3}),
        ]
    if kind == "article":
        return _article_probes(q)
    if kind == "variant":
        probes = [_Probe("gnomad", "get_variant", {"variant": q})]
        if _RSID.match(q):
            probes.append(_Probe("ncbi", "entrez_search", {"db": "snp", "query": q.lower(), "limit": 1}))
        probes.append(_Probe("ncbi", "entrez_search", {"db": "clinvar", "query": q, "limit": 3}))
        return probes
    raise ValueError(f"unknown entity kind {kind!r}")


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _is_exact(kind: EntityKind, query: str, record: Record) -> bool:
    q = _norm(query)
    if not q:
        return False
    if kind == "gene" and _norm(record.identifiers.get("gene_symbol")) == q:
        return True
    if kind == "article":
        ids = record.identifiers
        return any(_norm(ids.get(k)) == q for k in ("doi", "pmid", "pmcid")) or _norm(record.title) == q
    if kind == "variant":
        return any(_norm(record.identifiers.get(k)) == q for k in ("rsid", "gnomad_variant", "clinvar", "hgvs"))
    names = [record.title] + [str(s) for s in (record.attributes.get("synonyms") or [])[:50]]
    return any(_norm(n) == q for n in names if n)


class EntityResolver:
    def __init__(self, registry: Any, *, per_source_timeout: float = 30.0):
        self.registry = registry
        self.per_source_timeout = per_source_timeout

    async def resolve(self, kind: EntityKind, query: str, *, session_id: str | None = None) -> ResolutionResult:
        outcome = ResolutionResult(kind=kind, query=query)
        probes = [p for p in _probes(kind, query) if self._available(p)]
        skipped = {p.source for p in _probes(kind, query)} - {p.source for p in probes}
        if skipped:
            outcome.notes.append(f"not available or not configured: {', '.join(sorted(skipped))}")

        async def run(probe: _Probe) -> tuple[_Probe, list[Record], str | None]:
            try:
                result = await self.registry.call(
                    probe.source, probe.tool, probe.params, session_id=session_id, timeout=self.per_source_timeout
                )
            except SourceError as exc:
                return probe, [], str(exc)
            return probe, list(result.records), None

        repeated = {p.source for p in probes if sum(q.source == p.source for q in probes) > 1}
        for probe, records, error in await asyncio.gather(*(run(p) for p in probes)):
            label = probe.source
            if probe.source in repeated:
                label += ":" + str(probe.params.get("ontology") or probe.params.get("db") or probe.tool)
            if error:
                outcome.errors[label] = error[:300]
                continue
            if not records:
                continue
            exact = [r for r in records if _is_exact(kind, query, r)]
            best = exact[0] if exact else records[0]
            outcome.records.append(best)
            outcome.candidates.append(
                Candidate(
                    source=label,
                    tool=probe.tool,
                    id=best.id,
                    title=best.title,
                    url=best.url,
                    identifiers=best.identifiers,
                    exact_match=bool(exact),
                )
            )
        self._consensus(outcome)
        if not outcome.candidates:
            outcome.notes.append("no source returned a match; check spelling or try a synonym / identifier")
        elif not any(c.exact_match for c in outcome.candidates):
            outcome.notes.append("no exact name match: identifiers come from each source's top hit; verify them")
        return outcome

    def _available(self, probe: _Probe) -> bool:
        connector = self.registry.connectors.get(probe.source)
        return bool(connector and connector.configured and probe.tool in connector._tool_defs)

    @staticmethod
    def _consensus(outcome: ResolutionResult) -> None:
        keys = _IDENTITY_KEYS[outcome.kind]
        votes: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        weights: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for cand in outcome.candidates:
            for key in keys:
                value = cand.identifiers.get(key)
                if not value:
                    continue
                canonical = value.upper() if key in ("gene_symbol", "inchikey", "chembl", "unii") else value
                votes[key][canonical].append(cand.source)
                weights[key][canonical] += 2 if cand.exact_match else 1
        for key, values in votes.items():
            ranked = sorted(values, key=lambda v: (-weights[key][v], -len(values[v]), v))
            best = ranked[0]
            outcome.identifiers[key] = best
            outcome.agreement[key] = Agreement(value=best, sources=sorted(set(values[best])))
            if len(ranked) > 1:
                outcome.conflicts[key] = [Agreement(value=v, sources=sorted(set(values[v]))) for v in ranked]
