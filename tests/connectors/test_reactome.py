"""Reactome connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

CONTENT = "https://reactome.org/ContentService"
ANALYSIS = "https://reactome.org/AnalysisService"


@respx.mock
async def test_search_normalizes_and_paginates(make_registry):
    route = respx.get(f"{CONTENT}/search/query/paginated").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "search.json"))
    )
    registry = make_registry("reactome")
    result = await registry.call("reactome", "search", {"query": "apoptosis", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["query"] == "apoptosis"
    assert sent["species"] == "Homo sapiens"
    assert sent["types"] == "Pathway"
    assert sent["rowCount"] == "2"
    assert sent["page"] == "1"
    assert result.total == 192
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "R-HSA-109581"
    assert first.type == "pathway"
    assert first.title == "Apoptosis"  # highlight spans stripped
    assert first.identifiers["reactome"] == "R-HSA-109581"
    assert first.url == "https://reactome.org/content/detail/R-HSA-109581"
    assert "<span" not in (first.summary or "")
    assert result.records[1].title == "Regulation of Apoptosis"

    await registry.call("reactome", "search", {"query": "apoptosis", "limit": 2, "cursor": "3"})
    assert route.calls.last.request.url.params["page"] == "3"


@respx.mock
async def test_search_no_hits_is_a_note(make_registry):
    respx.get(f"{CONTENT}/search/query/paginated").mock(
        return_value=httpx.Response(404, json={"code": 404, "messages": ["No entries found for query: zzqq"]})
    )
    registry = make_registry("reactome")
    result = await registry.call("reactome", "search", {"query": "zzqq"})
    assert result.records == [] and result.total == 0
    assert "no Reactome entries" in result.notes[0]


@respx.mock
async def test_get_pathway_with_literature_and_participants(make_registry):
    route = respx.get(f"{CONTENT}/data/query/enhanced/R-HSA-109581").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "pathway.json"))
    )
    parts = respx.get(f"{CONTENT}/data/participants/R-HSA-109581/referenceEntities").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "participants.json"))
    )
    registry = make_registry("reactome")
    result = await registry.call(
        "reactome", "get_pathway", {"pathway_id": "r-hsa-109581.6", "include_participants": True}
    )
    assert route.called and parts.called
    pathway = result.records[0]
    assert pathway.id == "R-HSA-109581"
    assert pathway.title == "Apoptosis"
    assert pathway.date == "2004-09-20"
    assert pathway.identifiers == {"reactome": "R-HSA-109581", "go": "GO:0006915"}
    assert pathway.summary.startswith("Apoptosis is a distinct form of cell death")
    assert pathway.attributes["species"] == "Homo sapiens"
    assert pathway.attributes["pmids"][0] == "12189384"
    assert pathway.attributes["parent_pathways"][0]["id"] == "R-HSA-5357801"
    assert pathway.attributes["sub_event_count"] == 4
    assert pathway.attributes["participants_count"] == 4
    assert pathway.attributes["participant_uniprot"][0] == "Q12933"
    assert "TRAF2" in pathway.attributes["participant_genes"]
    lit = result.records[1]
    assert lit.type == "article"
    assert lit.identifiers["pmid"] == "12189384"
    assert lit.date == "2002"
    assert lit.attributes["journal"] == "Nat Rev Cancer"


@respx.mock
async def test_get_pathway_not_found(make_registry):
    respx.get(f"{CONTENT}/data/query/enhanced/R-HSA-0").mock(
        return_value=httpx.Response(404, json={"code": 404, "messages": ["not found"]})
    )
    registry = make_registry("reactome")
    result = await registry.call("reactome", "get_pathway", {"pathway_id": "R-HSA-0"})
    assert result.records == []
    assert "not found" in result.notes[0]


@respx.mock
async def test_pathways_for_uniprot_uses_mapping(make_registry):
    route = respx.get(f"{CONTENT}/data/mapping/UniProt/P04637/pathways").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "mapping.json"))
    )
    registry = make_registry("reactome")
    result = await registry.call("reactome", "pathways_for_entity", {"identifier": "p04637", "limit": 2})
    assert route.calls.last.request.url.params["species"] == "9606"
    assert result.total == 3
    assert len(result.records) == 2
    rec = result.records[0]
    assert rec.id == "R-HSA-111448"
    assert rec.type == "pathway"
    assert rec.identifiers["reactome"] == "R-HSA-111448"
    assert rec.attributes["lowest_level_pathway"] is True


@respx.mock
async def test_pathways_for_chebi_and_ncbi_and_ensembl(make_registry):
    chebi = respx.get(f"{CONTENT}/data/mapping/ChEBI/15377/pathways").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "mapping.json"))
    )
    ncbi = respx.get(f"{CONTENT}/data/mapping/NCBI%20Gene/7157/pathways").mock(
        return_value=httpx.Response(404, json={"code": 404, "messages": ["No pathways found"]})
    )
    ens = respx.get(f"{CONTENT}/data/mapping/ENSEMBL/ENSG00000141510/pathways").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "mapping.json"))
    )
    registry = make_registry("reactome")
    await registry.call("reactome", "pathways_for_entity", {"identifier": "CHEBI:15377"})
    assert chebi.called
    missing = await registry.call("reactome", "pathways_for_entity", {"identifier": "7157"})
    assert ncbi.called and missing.records == [] and "no Reactome pathways" in missing.notes[0]
    await registry.call("reactome", "pathways_for_entity", {"identifier": "ENSG00000141510.18"})
    assert ens.called


@respx.mock
async def test_pathways_for_gene_symbol_uses_analysis(make_registry):
    route = respx.get(f"{ANALYSIS}/identifier/TP53/projection").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "identifier.json"))
    )
    registry = make_registry("reactome")
    result = await registry.call("reactome", "pathways_for_entity", {"identifier": "TP53", "limit": 2})
    assert route.calls.last.request.url.params["pageSize"] == "2"
    assert result.total == 129
    rec = result.records[0]
    assert rec.id == "R-HSA-6804754"
    assert rec.attributes["p_value"] < 1e-6
    assert rec.attributes["entities_found"] == 2


@respx.mock
async def test_enrichment_analysis_posts_list(make_registry):
    payload = load_fixture("reactome", "enrichment.json")
    token = payload["summary"]["token"]
    route = respx.post(f"{ANALYSIS}/identifiers/projection").mock(return_value=httpx.Response(200, json=payload))
    nf = respx.get(f"{ANALYSIS}/token/{token}/notFound").mock(
        return_value=httpx.Response(200, json=load_fixture("reactome", "notfound.json"))
    )
    registry = make_registry("reactome")
    result = await registry.call(
        "reactome",
        "enrichment_analysis",
        {"identifiers": ["TP53", "BRCA1", "ATM", "CHEK2", "FOOBARX"], "limit": 3},
    )
    request = route.calls.last.request
    assert request.content.decode() == "TP53\nBRCA1\nATM\nCHEK2\nFOOBARX"
    assert request.headers["content-type"].startswith("text/plain")
    assert request.url.params["pageSize"] == "3"
    assert request.url.params["sortBy"] == "ENTITIES_PVALUE"
    assert nf.called
    assert result.total == payload["pathwaysFound"]
    assert result.next_cursor == "2"
    top = result.records[0]
    assert top.type == "pathway" and top.identifiers["reactome"].startswith("R-HSA-")
    assert top.attributes["fdr"] is not None
    assert result.data["rows"][0]["pathway"] == top.id
    assert result.data["identifiers_not_found"] == ["FOOBARX"]
    assert "not found" in result.notes[0]


@pytest.mark.live
async def test_live_reactome_all_tools(live_registry):
    registry = live_registry("reactome")
    search = await registry.call("reactome", "search", {"query": "apoptosis", "limit": 3})
    assert search.records and search.records[0].identifiers["reactome"].startswith("R-HSA-")
    detail = await registry.call("reactome", "get_pathway", {"pathway_id": "R-HSA-109581"})
    assert detail.records[0].title == "Apoptosis"
    assert detail.records[0].attributes.get("pmids")
    mapped = await registry.call("reactome", "pathways_for_entity", {"identifier": "P04637", "limit": 5})
    assert mapped.records and mapped.total >= 5
    symbol = await registry.call("reactome", "pathways_for_entity", {"identifier": "BRCA1", "limit": 5})
    assert symbol.records
    enrich = await registry.call(
        "reactome", "enrichment_analysis", {"identifiers": ["TP53", "BRCA1", "ATM", "CHEK2"], "limit": 5}
    )
    assert enrich.records and enrich.records[0].attributes["p_value"] < 0.01
