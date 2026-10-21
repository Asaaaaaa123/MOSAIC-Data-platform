"""HGNC connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

EMPTY = {"responseHeader": {"status": 0}, "response": {"numFound": 0, "start": 0, "docs": []}}


def _router(routes: dict[str, object]):
    """Route by raw request path (HGNC puts the query in the path)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        seen.append(path)
        assert request.headers["accept"] == "application/json"
        if path in routes:
            payload = routes[path]
            if isinstance(payload, httpx.Response):
                return payload
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=EMPTY)

    return handler, seen


@respx.mock
async def test_search_genes_boosts_exact_symbols_and_fetches_details(make_registry):
    handler, seen = _router(
        {
            "/search/EGFR%20lung%20cancer": load_fixture("hgnc", "search_free.json"),
            '/search/symbol:%22EGFR%22%20OR%20alias_symbol:%22EGFR%22%20OR%20prev_symbol:%22EGFR%22': load_fixture(
                "hgnc", "search_boost.json"
            ),
            "/fetch/hgnc_id/HGNC:3236+OR+HGNC:58132": load_fixture("hgnc", "fetch_ids.json"),
        }
    )
    respx.route(host="rest.genenames.org").mock(side_effect=handler)
    registry = make_registry("hgnc")
    result = await registry.call("hgnc", "search_genes", {"query": "EGFR lung cancer", "limit": 2})

    assert seen[0] == "/search/EGFR%20lung%20cancer"
    assert seen[2] == "/fetch/hgnc_id/HGNC:3236+OR+HGNC:58132"  # one batched detail fetch
    egfr = result.records[0]
    assert egfr.id == "HGNC:3236"  # upstream ranks lncRNAs first; the exact symbol is boosted
    assert egfr.type == "gene"
    assert egfr.title == "EGFR: epidermal growth factor receptor"
    assert egfr.url == "https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/HGNC:3236"
    assert egfr.identifiers["gene_symbol"] == "EGFR"
    assert egfr.identifiers["entrez_gene"] == "1956"
    assert egfr.identifiers["ensembl_gene"] == "ENSG00000146648"
    assert egfr.identifiers["uniprot"] == "P00533"
    assert egfr.identifiers["omim"] == "131550"
    assert egfr.identifiers["refseq"] == "NM_005228"  # versionless
    assert egfr.identifiers["ensembl_transcript"].startswith("ENST")
    assert "ERBB1" in egfr.attributes["alias_symbols"]
    assert egfr.attributes["locus_group"] == "protein-coding gene"
    assert egfr.date and len(egfr.date) == 10
    assert result.records[1].id == "HGNC:58132"
    assert result.total >= 486 and result.next_cursor == "2"


@respx.mock
async def test_search_genes_field_syntax_is_passed_through(make_registry):
    handler, seen = _router({})
    respx.route(host="rest.genenames.org").mock(side_effect=handler)
    registry = make_registry("hgnc")
    result = await registry.call("hgnc", "search_genes", {"query": "symbol:BRAF*"})
    assert seen == ["/search/symbol:BRAF*"]  # no boosting query for field syntax, nothing to fetch
    assert result.records == [] and "no HGNC genes" in result.notes[0]


@respx.mock
async def test_fetch_gene_alias_fallback(make_registry):
    handler, seen = _router(
        {
            "/fetch/symbol/ERBB1": load_fixture("hgnc", "fetch_empty.json"),
            "/fetch/alias_symbol/ERBB1": load_fixture("hgnc", "fetch_alias_erbb1.json"),
        }
    )
    respx.route(host="rest.genenames.org").mock(side_effect=handler)
    registry = make_registry("hgnc")
    result = await registry.call("hgnc", "fetch_gene", {"value": "ERBB1"})
    assert seen == ["/fetch/symbol/ERBB1", "/fetch/alias_symbol/ERBB1"]
    assert result.records[0].identifiers["gene_symbol"] == "EGFR"
    assert result.notes == ["ERBB1 is an alias of EGFR"]


@respx.mock
async def test_fetch_gene_infers_identifier_fields(make_registry):
    handler, seen = _router({"/fetch/entrez_id/673": load_fixture("hgnc", "fetch_entrez_673.json")})
    respx.route(host="rest.genenames.org").mock(side_effect=handler)
    registry = make_registry("hgnc")
    braf = await registry.call("hgnc", "fetch_gene", {"value": "673"})
    record = braf.records[0]
    assert record.id == "HGNC:1097"
    assert record.identifiers["uniprot"] == "P15056"
    assert {"id": 1157, "name": "RAF family"} in record.attributes["gene_groups"]
    assert record.attributes["mgd_ids"] == ["MGI:88190"]

    await registry.call("hgnc", "fetch_gene", {"value": "ENSG00000157764.14"})
    await registry.call("hgnc", "fetch_gene", {"value": "hgnc:1097"})
    await registry.call("hgnc", "fetch_gene", {"value": "NM_004333.6"})
    await registry.call("hgnc", "fetch_gene", {"value": "164757", "field": "omim_id"})
    missing = await registry.call("hgnc", "fetch_gene", {"value": "P15056"})
    assert seen[1:5] == [
        "/fetch/ensembl_gene_id/ENSG00000157764",
        "/fetch/hgnc_id/HGNC:1097",
        "/fetch/refseq_accession/NM_004333",
        "/fetch/omim_id/164757",
    ]
    assert seen[5] == "/fetch/uniprot_ids/P15056"
    assert missing.records == [] and "no HGNC gene found" in missing.notes[0]


@respx.mock
async def test_gene_group_members(make_registry):
    handler, seen = _router({"/fetch/gene_group_id/1157": load_fixture("hgnc", "gene_group_1157.json")})
    respx.route(host="rest.genenames.org").mock(side_effect=handler)
    registry = make_registry("hgnc")
    result = await registry.call("hgnc", "gene_group", {"group_id": 1157, "limit": 2})
    assert seen == ["/fetch/gene_group_id/1157"]
    assert result.total == 3 and result.next_cursor == "2"
    symbols = [r.identifiers["gene_symbol"] for r in result.records]
    assert symbols == sorted(symbols) and symbols[0] == "ARAF"  # members sorted by symbol
    assert result.data["groups"][0]["name"] == "RAF family"
    assert result.data["groups"][0]["member_count"] == 3

    empty = await registry.call("hgnc", "gene_group", {"group_id": 999999})
    assert empty.records == [] and "not found" in empty.notes[0]
    with pytest.raises(SourceError, match="group_id or symbol"):
        await registry.call("hgnc", "gene_group", {})


@respx.mock
async def test_gene_group_by_symbol(make_registry):
    handler, seen = _router(
        {
            "/fetch/symbol/BRAF": load_fixture("hgnc", "fetch_entrez_673.json"),
            "/fetch/gene_group_id/1157": load_fixture("hgnc", "gene_group_1157.json"),
        }
    )
    respx.route(host="rest.genenames.org").mock(side_effect=handler)
    registry = make_registry("hgnc")
    result = await registry.call("hgnc", "gene_group", {"symbol": "BRAF"})
    assert seen == ["/fetch/symbol/BRAF", "/fetch/gene_group_id/654", "/fetch/gene_group_id/1157"]
    names = {g["id"]: g["name"] for g in result.data["groups"]}
    assert names[654] == "Mitogen-activated protein kinase kinase kinases" and names[1157] == "RAF family"
    assert "not found" in result.notes[0]  # group 654 returned no members in this mock


@pytest.mark.live
async def test_live_hgnc_all_tools(live_registry):
    registry = live_registry("hgnc")
    search = await registry.call("hgnc", "search_genes", {"query": "BRAF V600E melanoma", "limit": 3})
    assert search.records[0].identifiers["gene_symbol"] == "BRAF"
    fetched = await registry.call("hgnc", "fetch_gene", {"value": "MLL"})
    assert fetched.records[0].identifiers["gene_symbol"] == "KMT2A" and "previous symbol" in fetched.notes[0]
    by_uniprot = await registry.call("hgnc", "fetch_gene", {"value": "P04637"})
    assert by_uniprot.records[0].identifiers["hgnc"] == "HGNC:11998"
    group = await registry.call("hgnc", "gene_group", {"group_id": 1157})
    assert {r.identifiers["gene_symbol"] for r in group.records} >= {"ARAF", "BRAF", "RAF1"}
