"""Cluster records from different databases that describe the same entity.

Two records are linked when they share any normalized identifier (same DOI,
PMID, NCT number, InChIKey, ChEMBL ID, UniProt accession, ...). Linking is
transitive (union-find), so a PubMed article (pmid) and an OpenAlex work
(doi + pmid) and a Crossref work (doi) collapse into one cluster.

Relationship records (bioactivities, associations) are not linked through
identifiers: connectors give them the identifiers of the counterpart entity,
which many rows of one result share (every ChEMBL activity of imatinib against
ABL1 carries ABL1's ChEMBL ID, every GWAS association of a variant its rsID).
Each stays its own cluster; only a repeat of the same source record merges.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..models import Record

# Identifier keys strong enough to assert identity on their own.
LINKING_KEYS = (
    "doi", "pmid", "pmcid", "arxiv", "s2", "openalex", "scopus_eid", "wos_uid",
    "nct", "euctr",
    "inchikey", "chembl", "pubchem_cid", "unii", "drugcentral", "rxcui", "drugbank",
    "uniprot", "ensembl_gene", "entrez_gene", "hgnc", "pdb",
    "rsid", "clinvar", "gnomad_variant", "gwas_study", "pgs",
    "mondo", "efo", "doid", "hp", "mesh", "omim", "orphanet", "reactome", "go",
    "geo", "sra", "bioproject", "biosample", "dbgap", "gdc_project", "cbioportal",
    "chebi", "kegg_compound", "kegg_drug", "pharmgkb", "ncit", "umls",
    "mgi", "rgd", "zfin", "xenbase", "flybase", "wormbase", "sgd",
    "zenodo", "figshare", "osf", "synapse", "dandi", "openneuro",
)

# Record types that state a relation between two entities rather than describe one entity.
RELATIONSHIP_TYPES = frozenset({"activity", "association"})


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_records(records: Iterable[Record], keys: Iterable[str] = LINKING_KEYS) -> list[list[Record]]:
    """Group records into clusters of the same real-world entity."""
    items = list(records)
    uf = _UnionFind(len(items))
    seen: dict[tuple[str, str], int] = {}
    key_set = set(keys)
    for idx, record in enumerate(items):
        if record.type in RELATIONSHIP_TYPES:
            # "" is never an identifier key, so this token only matches the same source record
            tokens = [("", f"{record.source}:{record.id}")] if record.id else []
        else:
            tokens = [(key, value) for key, value in record.identifiers.items() if key in key_set and value]
        for token in tokens:
            if token in seen:
                uf.union(seen[token], idx)
            else:
                seen[token] = idx
    groups: dict[int, list[Record]] = {}
    for idx, record in enumerate(items):
        groups.setdefault(uf.find(idx), []).append(record)
    return sorted(groups.values(), key=lambda g: (-len({r.source for r in g}), -len(g)))


def merge_cluster(cluster: list[Record]) -> dict[str, Any]:
    """Summarize a cluster: union of identifiers, best title, sources and per-source ids."""
    identifiers: dict[str, str] = {}
    for record in cluster:
        for key, value in record.identifiers.items():
            identifiers.setdefault(key, value)
    titled = [r for r in cluster if r.title]
    title = max(titled, key=lambda r: len(r.title or "")).title if titled else None
    dates = sorted(r.date for r in cluster if r.date)
    return {
        "type": cluster[0].type,
        "title": title,
        "date": dates[0] if dates else None,
        "identifiers": identifiers,
        "sources": sorted({r.source for r in cluster}),
        "records": [{"source": r.source, "id": r.id, "url": r.url} for r in cluster],
    }
