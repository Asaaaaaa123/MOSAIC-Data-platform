"""GWAS Catalog connector tests (REST API v2)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://www.ebi.ac.uk/gwas/rest/api/v2"
EMPTY = {"page": {"size": 20, "totalElements": 0, "totalPages": 0, "number": 0}}


def fx(name):
    return httpx.Response(200, json=load_fixture("gwas_catalog", name))


@respx.mock
async def test_search_studies_by_trait(make_registry):
    route = respx.get(f"{API}/studies").mock(return_value=fx("studies.json"))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "search", {"query": "asthma", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["efo_trait"] == "asthma"
    assert sent["size"] == "2" and sent["page"] == "0"
    assert result.total == 288
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.type == "study"
    assert first.id == "GCST91000572"
    assert first.identifiers["gwas_study"] == "GCST91000572"
    assert first.identifiers["pmid"] == "38750426"
    assert first.identifiers["mondo"] == "MONDO:0004979"
    assert first.url == "https://www.ebi.ac.uk/gwas/studies/GCST91000572"
    assert "Discovery sample" in first.summary


@respx.mock
async def test_search_falls_back_to_reported_trait(make_registry):
    route = respx.get(f"{API}/studies").mock(side_effect=[httpx.Response(200, json=EMPTY), fx("studies.json")])
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "search", {"query": "childhood asthma", "limit": 2})
    assert route.call_count == 2
    assert route.calls[1].request.url.params["disease_trait"] == "childhood asthma"
    assert result.records and "reported-trait" in result.notes[0]


@respx.mock
async def test_search_pagination_page_from_cursor(make_registry):
    route = respx.get(f"{API}/studies").mock(return_value=fx("studies.json"))
    registry = make_registry("gwas_catalog")
    await registry.call("gwas_catalog", "search", {"query": "GCST000392", "limit": 2, "cursor": "4"})
    sent = route.calls.last.request.url.params
    assert sent["accession_id"] == "GCST000392"
    assert sent["page"] == "2"


@respx.mock
async def test_search_traits_ranks_exact_first(make_registry):
    payload = load_fixture("gwas_catalog", "efo_traits.json")
    payload["_embedded"]["efo_traits"].append({"efo_trait": "asthma", "efo_id": "MONDO_0004979"})
    route = respx.get(f"{API}/efo-traits").mock(return_value=httpx.Response(200, json=payload))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "search", {"query": "asthma", "kind": "traits", "limit": 3})
    assert route.calls.last.request.url.params["size"] == "200"
    assert result.records[0].id == "MONDO_0004979"
    assert result.records[0].type == "ontology_term"
    assert result.records[0].identifiers == {"mondo": "MONDO:0004979"}
    assert result.records[1].title == "T2-high asthma"  # substring matches next, shortest label first
    assert result.next_cursor == "3"


@respx.mock
async def test_associations_for_variant(make_registry):
    assoc = respx.get(f"{API}/associations").mock(return_value=fx("associations_variant.json"))
    respx.get(f"{API}/single-nucleotide-polymorphisms/rs7903146").mock(return_value=fx("snp.json"))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "associations_for_variant", {"rsid": "RS7903146", "limit": 2})

    sent = assoc.calls.last.request.url.params
    assert sent["rs_id"] == "rs7903146"
    assert sent["sort"] == "p_value" and sent["direction"] == "asc"
    assert result.total == 353 and result.next_cursor == "2"
    top = result.records[0]
    assert top.type == "association"
    assert top.attributes["p_value"] == "3e-1315"  # mantissa/exponent, not the underflowed 0.0
    assert top.identifiers["rsid"] == "rs7903146"
    assert top.identifiers["gwas_study"] == "GCST90492734"
    assert top.identifiers["gene_symbol"] == "TCF7L2"
    assert top.identifiers["mondo"] == "MONDO:0005148"
    assert top.identifiers["pmid"] == "38374256"
    assert result.records[1].attributes["beta"] == "0.28 unit decrease"
    assert result.data["variant"]["functional_class"] == "intron_variant"
    assert result.data["variant"]["location_grch38"] == "10:112998590"


async def test_associations_for_variant_rejects_non_rsid(make_registry):
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "associations_for_variant", {"rsid": "chr1:12345"})
    assert result.records == [] and "not an rsID" in result.notes[0]


@respx.mock
async def test_associations_for_gene(make_registry):
    assoc = respx.get(f"{API}/associations").mock(return_value=fx("associations_study.json"))
    respx.get(f"{API}/genes/APOE").mock(return_value=fx("gene.json"))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "associations_for_gene", {"gene": "APOE", "extended_geneset": True})
    sent = assoc.calls.last.request.url.params
    assert sent["mapped_gene"] == "APOE" and sent["extended_geneset"] == "true"
    with_or = result.records[1]
    assert with_or.attributes["or_value"] == "1.09"
    assert with_or.attributes["ci"] == "[1.00-1.19]"
    assert "OR=1.09" in with_or.summary
    assert result.data["gene"]["ensembl_gene"] == ["ENSG00000130203"]


@respx.mock
async def test_associations_for_trait_by_id(make_registry):
    assoc = respx.get(f"{API}/associations").mock(return_value=fx("associations_study.json"))
    registry = make_registry("gwas_catalog")
    result = await registry.call(
        "gwas_catalog", "associations_for_trait", {"trait": "MONDO:0005147", "show_child_traits": False, "limit": 2}
    )
    sent = assoc.calls.last.request.url.params
    assert sent["efo_id"] == "MONDO_0005147"
    assert sent["show_child_traits"] == "false"
    assert result.total == 38 and result.next_cursor == "2"


@respx.mock
async def test_associations_for_trait_resolves_label(make_registry):
    assoc = respx.get(f"{API}/associations").mock(
        side_effect=[httpx.Response(200, json=EMPTY), fx("associations_study.json")]
    )
    respx.get(f"{API}/efo-traits").mock(return_value=fx("efo_traits.json"))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "associations_for_trait", {"trait": "allergic asthma"})
    assert assoc.calls[0].request.url.params["efo_trait"] == "allergic asthma"
    assert assoc.calls[1].request.url.params["efo_id"] == "MONDO_0004784"
    assert "resolved trait" in result.notes[0]


@respx.mock
async def test_get_study_with_publication_and_ancestry(make_registry):
    respx.get(f"{API}/studies/GCST000392").mock(return_value=fx("study.json"))
    respx.get(f"{API}/publications/19430480").mock(return_value=fx("publication.json"))
    respx.get(f"{API}/studies/GCST000392/ancestries").mock(return_value=fx("ancestries.json"))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "get_study", {"accession": "gcst000392"})
    rec = result.records[0]
    assert rec.id == "GCST000392" and rec.title == "Type 1 diabetes"
    assert rec.date == "2009-05-10"
    assert rec.authors[0] == "Barrett JC"
    assert rec.identifiers == {"gwas_study": "GCST000392", "pmid": "19430480", "mondo": "MONDO:0005147"}
    assert rec.attributes["full_summary_stats_available"] is True
    assert rec.attributes["journal"] == "Nat Genet"
    assert rec.attributes["ancestries"][0]["ancestral_groups"] == ["European"]


@respx.mock
async def test_get_study_not_found(make_registry):
    respx.get(f"{API}/studies/GCST999999999").mock(return_value=httpx.Response(404, json={"error": "Not Found"}))
    registry = make_registry("gwas_catalog")
    result = await registry.call("gwas_catalog", "get_study", {"accession": "GCST999999999"})
    assert result.records == [] and "not found" in result.notes[0]


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("gwas_catalog")
    search = await registry.call("gwas_catalog", "search", {"query": "asthma", "limit": 2})
    assert search.records and search.records[0].identifiers["gwas_study"].startswith("GCST")
    traits = await registry.call("gwas_catalog", "search", {"query": "asthma", "kind": "traits", "limit": 2})
    assert traits.records[0].id == "MONDO_0004979"
    var = await registry.call("gwas_catalog", "associations_for_variant", {"rsid": "rs7903146", "limit": 3})
    assert var.records and var.records[0].identifiers["rsid"] == "rs7903146"
    gene = await registry.call("gwas_catalog", "associations_for_gene", {"gene": "TCF7L2", "limit": 3})
    assert gene.records and gene.data["gene"]["symbol"] == "TCF7L2"
    trait = await registry.call("gwas_catalog", "associations_for_trait", {"trait": "MONDO_0005148", "limit": 3})
    assert trait.records and trait.total > 1000
    study = await registry.call("gwas_catalog", "get_study", {"accession": "GCST000392"})
    assert study.records[0].identifiers["pmid"] == "19430480"
