"""QuickGO connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://www.ebi.ac.uk/QuickGO/services"


@respx.mock
async def test_search_terms(make_registry):
    route = respx.get(f"{API}/ontology/go/search").mock(
        return_value=httpx.Response(200, json=load_fixture("quickgo", "search.json"))
    )
    registry = make_registry("quickgo")
    result = await registry.call("quickgo", "search_terms", {"query": "apoptosis", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["query"] == "apoptosis" and sent["limit"] == "2" and sent["page"] == "1"
    assert result.total == 352
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "GO:0097194"
    assert rec.identifiers == {"go": "GO:0097194"}
    assert rec.type == "ontology_term"
    assert rec.attributes["aspect"] == "biological_process"
    assert rec.url == "https://www.ebi.ac.uk/QuickGO/term/GO:0097194"


@respx.mock
async def test_get_terms_details_obsolete_and_missing(make_registry):
    route = respx.get(url__regex=rf"{API}/ontology/go/terms/.*/complete").mock(
        return_value=httpx.Response(200, json=load_fixture("quickgo", "terms.json"))
    )
    registry = make_registry("quickgo")
    result = await registry.call(
        "quickgo", "get_terms", {"go_ids": ["GO:0006915", "0000005", "GO:9999999", "junk"]}
    )
    assert route.calls.last.request.url.path.endswith("/GO:0006915,GO:0000005,GO:9999999/complete")
    by_id = {r.id: r for r in result.records}
    apoptosis = by_id["GO:0006915"]
    assert apoptosis.title == "apoptotic process"
    assert apoptosis.identifiers["reactome"] == "R-HSA-109581"
    assert "apoptosis" in apoptosis.attributes["synonyms"]
    assert apoptosis.attributes["secondary_ids"] == ["GO:0006917", "GO:0008632"]
    obsolete = by_id["GO:0000005"]
    assert obsolete.attributes["is_obsolete"] is True
    assert obsolete.attributes["consider"][0] == "GO:0042254"
    joined = " ".join(result.notes)
    assert "junk" in joined and "GO:9999999" in joined and "obsolete" in joined


@respx.mock
async def test_gene_annotations_aggregates_terms(make_registry):
    route = respx.get(f"{API}/annotation/search").mock(
        return_value=httpx.Response(200, json=load_fixture("quickgo", "annotations.json"))
    )
    registry = make_registry("quickgo")
    result = await registry.call(
        "quickgo", "gene_annotations", {"gene_product_id": "p04637", "exclude_iea": True, "limit": 3}
    )
    sent = route.calls[0].request.url.params
    assert sent["geneProductId"] == "UniProtKB:P04637"
    assert sent["evidenceCode"] == "ECO:0000352" and sent["evidenceCodeUsage"] == "descendants"
    assert sent["includeFields"] == "goName,name,synonyms"
    assert route.call_count == 3  # fixture claims many pages; capped at 3 pages
    assert result.total == 5
    assert len(result.records) == 3
    rec = result.records[0]
    assert rec.identifiers == {"go": rec.id}
    assert rec.attributes["gene_symbol"] == "TP53"
    assert rec.attributes["evidence_codes"]
    assert result.data["gene_product"]["uniprot"] == "P04637"
    assert "GO:0006974 DNA damage response [IDA]" in result.data["by_aspect"]["biological_process"]
    assert any("PMID" not in p for p in rec.attributes.get("pmids", []))


@respx.mock
async def test_gene_annotations_rejected_id(make_registry):
    respx.get(f"{API}/annotation/search").mock(
        return_value=httpx.Response(400, json={"messages": ["invalid values: TP53"]})
    )
    registry = make_registry("quickgo")
    result = await registry.call("quickgo", "gene_annotations", {"gene_product_id": "TP53"})
    assert result.records == [] and "UniProtKB accession" in result.notes[0]


@respx.mock
async def test_genes_for_term_dedupes_and_resumes(make_registry):
    route = respx.get(f"{API}/annotation/search").mock(
        return_value=httpx.Response(200, json=load_fixture("quickgo", "term_annotations.json"))
    )
    stats = respx.get(f"{API}/annotation/stats").mock(
        return_value=httpx.Response(200, json=load_fixture("quickgo", "stats.json"))
    )
    registry = make_registry("quickgo")
    result = await registry.call("quickgo", "genes_for_term", {"go_id": "0006915", "limit": 3})
    sent = route.calls.last.request.url.params
    assert sent["goId"] == "GO:0006915"
    assert sent["taxonId"] == "9606"
    assert sent["goUsage"] == "descendants"
    assert sent["geneProductSubset"] == "Swiss-Prot" and sent["geneProductType"] == "protein"
    assert stats.called
    assert result.total == 1558
    assert [r.identifiers["uniprot"] for r in result.records] == ["A0A0G2JMD5", "A0PJW8", "A1A4S6"]
    assert result.records[2].attributes["annotation_count"] == 2
    assert result.records[2].identifiers["gene_symbol"] == "ARHGAP10"
    assert result.next_cursor == "1.4"

    page2 = await registry.call("quickgo", "genes_for_term", {"go_id": "GO:0006915", "limit": 3, "cursor": "1.4"})
    assert [r.identifiers["uniprot"] for r in page2.records][:2] == ["A3QJZ6", "A3QJZ7"]
    assert stats.call_count == 1  # totals only fetched on the first page


@pytest.mark.live
async def test_live_quickgo_all_tools(live_registry):
    registry = live_registry("quickgo")
    search = await registry.call("quickgo", "search_terms", {"query": "mitochondrial fission", "limit": 3})
    assert any(r.id == "GO:0000266" for r in search.records)
    terms = await registry.call("quickgo", "get_terms", {"go_ids": ["GO:0006915", "GO:0005634"]})
    assert {r.id for r in terms.records} == {"GO:0006915", "GO:0005634"}
    ann = await registry.call("quickgo", "gene_annotations", {"gene_product_id": "P04637", "exclude_iea": True})
    assert ann.records and ann.data["gene_product"]["symbol"] == "TP53"
    genes = await registry.call("quickgo", "genes_for_term", {"go_id": "GO:0000266", "limit": 5, "exclude_iea": True})
    assert genes.records and genes.total and genes.records[0].identifiers.get("uniprot")
