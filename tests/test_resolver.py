"""Entity resolver and identifier-normalization tests (offline, fake registry)."""

from __future__ import annotations

import json

import pytest

from mosaic.crosslink.resolver import EntityResolver, _probes
from mosaic.http import SourceError
from mosaic.ids import normalize_identifiers
from mosaic.models import Record, ToolResult


class _Connector:
    def __init__(self, name: str, tools: list[str], configured: bool = True):
        self.name = name
        self.configured = configured
        self._tool_defs = {t: object() for t in tools}


class FakeRegistry:
    """Just enough of Registry for the resolver: ``connectors`` and ``call``."""

    def __init__(self, responses: dict[tuple[str, str], list[Record] | Exception]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []
        tools: dict[str, list[str]] = {}
        for source, tool in responses:
            tools.setdefault(source, []).append(tool)
        self.connectors = {s: _Connector(s, t) for s, t in tools.items()}

    async def call(self, source, tool, params, *, session_id=None, timeout=None):
        self.calls.append((source, tool, params))
        outcome = self.responses[(source, tool)]
        if isinstance(outcome, Exception):
            raise outcome
        return ToolResult(source=source, tool=tool, records=outcome)


def _rec(source: str, rid: str, title: str, **ids) -> Record:
    return Record(source=source, id=rid, title=title, identifiers=ids, type="gene")


async def test_gene_consensus_and_conflicts():
    registry = FakeRegistry(
        {
            ("hgnc", "fetch_gene"): [_rec("hgnc", "HGNC:1097", "BRAF", gene_symbol="BRAF", hgnc="HGNC:1097", ensembl_gene="ENSG00000157764.15")],
            ("uniprot", "search_proteins"): [_rec("uniprot", "P15056", "BRAF_HUMAN", gene_symbol="BRAF", uniprot="P15056", ensembl_gene="ENSG00000157764")],
            ("ncbi", "entrez_search"): [_rec("ncbi", "673", "BRAF", gene_symbol="BRAF", entrez_gene="673", hgnc="1097")],
            ("opentargets", "search"): [_rec("opentargets", "ENSG00000999999", "BRAFP1", gene_symbol="BRAFP1", ensembl_gene="ENSG00000999999")],
        }
    )
    result = await EntityResolver(registry).resolve("gene", "BRAF")

    assert result.identifiers["gene_symbol"] == "BRAF"
    assert result.identifiers["hgnc"] == "HGNC:1097"  # "1097" normalized and agreed by two sources
    assert result.agreement["hgnc"].sources == ["hgnc", "ncbi"]
    assert result.identifiers["ensembl_gene"] == "ENSG00000157764"  # exact matches outvote the pseudogene hit
    assert [a.value for a in result.conflicts["ensembl_gene"]] == ["ENSG00000157764", "ENSG00000999999"]
    exact = {c.source: c.exact_match for c in result.candidates}
    assert exact == {"hgnc": True, "uniprot": True, "ncbi": True, "opentargets": False}


async def test_failures_and_missing_sources_are_reported():
    registry = FakeRegistry(
        {
            ("pubchem", "get_compound"): SourceError("pubchem", "HTTP 503 upstream down", status=503),
            ("chembl", "get_molecule"): [Record(source="chembl", id="CHEMBL941", title="IMATINIB", identifiers={"chembl": "chembl941"}, type="drug")],
        }
    )
    result = await EntityResolver(registry).resolve("drug", "Imatinib")
    assert result.identifiers == {"chembl": "CHEMBL941"}
    assert "503" in result.errors["pubchem"]
    assert any("rxnorm" in note and "drugcentral" in note for note in result.notes)


async def test_no_match_note():
    registry = FakeRegistry({("hgnc", "fetch_gene"): []})
    result = await EntityResolver(registry).resolve("gene", "NOTAGENE")
    assert result.identifiers == {}
    assert any("no source returned a match" in n for n in result.notes)


def test_article_probes_route_by_identifier_type():
    doi = {(p.source, p.tool) for p in _probes("article", "https://doi.org/10.1038/S41586-019-1711-4")}
    assert ("crossref", "get_work") in doi and ("unpaywall", "get_oa_status") in doi
    assert {p.params.get("doi") for p in _probes("article", "doi:10.1038/S41586-019-1711-4") if p.source == "crossref"} == {
        "10.1038/s41586-019-1711-4"
    }
    pmid = {(p.source, p.tool) for p in _probes("article", "PMID: 31634902")}
    assert ("ncbi", "pubmed_fetch") in pmid
    title = {(p.source, p.tool) for p in _probes("article", "Search-and-replace genome editing")}
    assert ("crossref", "search_works") in title


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("mondo", "MONDO_0009061", "MONDO:0009061"),
        ("mondo", "http://purl.obolibrary.org/obo/MONDO_0009061", "MONDO:0009061"),
        ("efo", "EFO:0000305", "EFO_0000305"),
        ("hp", "hp_0001250", "HP:0001250"),
        ("doid", "DOID_1485", "DOID:1485"),
        ("go", "GO_0008150", "GO:0008150"),
        ("orphanet", "Orphanet:586", "586"),
        ("orphanet", "ORPHA:586", "586"),
        ("omim", "MIM:219700", "219700"),
        ("mesh", "MESH:D003550", "D003550"),
        ("umls", "umls:c0010674", "C0010674"),
    ],
)
def test_ontology_identifier_normalization(key, raw, expected):
    assert normalize_identifiers({key: raw}) == {key: expected}


def test_to_llm_trims_large_data_and_stays_valid_json():
    rows = [{"row": i, "text": "x" * 200} for i in range(500)]
    text = ToolResult(source="s", tool="t", data=rows).to_llm(max_chars=5000)
    parsed = json.loads(text)
    assert len(text) <= 5000
    assert 0 < len(parsed["data"]) < 500
    assert any("data trimmed" in n for n in parsed["notes"])

    blob = {"huge": "y" * 50_000}
    parsed = json.loads(ToolResult(source="s", tool="t", data=blob).to_llm(max_chars=2000))
    assert "data" not in parsed and any("omitted" in n for n in parsed["notes"])
