"""Monarch Initiative connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://api-v3.monarchinitiative.org/v3/api"


@respx.mock
async def test_search_sends_categories_and_maps_xrefs(make_registry):
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "search.json"))
    )
    registry = make_registry("monarch")
    result = await registry.call("monarch", "search", {"query": "marfan", "limit": 3})
    params = route.calls.last.request.url.params
    assert params["q"] == "marfan"
    assert params.get_list("category") == ["biolink:Disease", "biolink:Gene", "biolink:PhenotypicFeature"]
    assert params["limit"] == "3" and params["offset"] == "0"
    assert result.total == 29
    assert result.next_cursor == "3"
    mfs = next(r for r in result.records if r.id == "MONDO:0007947")
    assert mfs.type == "disease"
    assert mfs.title == "Marfan syndrome"
    ids = mfs.identifiers
    assert ids["mondo"] == "MONDO:0007947"
    assert ids["omim"] == "154700"
    assert ids["doid"] == "DOID:14323"
    assert ids["mesh"] == "D008382"
    assert ids["umls"] == "C0024796"
    assert ids["orphanet"].isdigit()  # canonical form drops the "Orphanet:" prefix
    assert mfs.url == "https://monarchinitiative.org/MONDO:0007947"

    await registry.call("monarch", "search", {"query": "fbn1", "category": "gene", "taxon": "NCBITaxon:9606"})
    params = route.calls.last.request.url.params
    assert params.get_list("category") == ["biolink:Gene"] and params["in_taxon"] == "NCBITaxon:9606"


@respx.mock
async def test_get_entity_disease_and_gene(make_registry):
    respx.get(f"{API}/entity/MONDO:0007947").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "entity_disease.json"))
    )
    respx.get(f"{API}/entity/HGNC:3603").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "entity_gene.json"))
    )
    registry = make_registry("monarch")
    disease = (await registry.call("monarch", "get_entity", {"entity_id": "MONDO:0007947"})).records[0]
    assert disease.attributes["inheritance"]["name"] == "Autosomal dominant inheritance"
    assert disease.attributes["causal_genes"][0] == {"id": "HGNC:3603", "symbol": "FBN1"}
    assert disease.attributes["association_counts"]["DiseaseToPhenotypicFeatureAssociation"] == 181
    assert disease.attributes["phenotype_ids_sample"][0].startswith("HP:")
    assert disease.identifiers["omim"] == "154700"

    gene = (await registry.call("monarch", "get_entity", {"entity_id": "HGNC:3603"})).records[0]
    assert gene.type == "gene"
    assert gene.identifiers == {
        "hgnc": "HGNC:3603",
        "ensembl_gene": "ENSG00000166147",
        "omim": "134797",
        "gene_symbol": "FBN1",
    }
    assert gene.attributes["taxon"] == "Homo sapiens"


@respx.mock
async def test_get_entity_resolves_xref_and_not_found(make_registry):
    respx.get(f"{API}/entity/OMIM:154700").mock(return_value=httpx.Response(404, json={"detail": "Entity not found"}))
    respx.get(f"{API}/entity/MONDO:0007947").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "entity_disease.json"))
    )
    search = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "search.json"))
    )
    registry = make_registry("monarch")
    result = await registry.call("monarch", "get_entity", {"entity_id": "OMIM:154700"})
    assert search.calls.last.request.url.params["q"] == "OMIM:154700"
    assert result.records[0].id == "MONDO:0007947"
    assert "resolved via cross-reference" in result.notes[0]

    respx.get(f"{API}/entity/MONDO:9999999").mock(return_value=httpx.Response(404, json={"detail": "Entity not found"}))
    missing = await registry.call("monarch", "get_entity", {"entity_id": "MONDO:9999999"})
    assert missing.records == [] and "not found" in missing.notes[0]


@respx.mock
async def test_associations_disease_phenotype(make_registry):
    route = respx.get(f"{API}/entity/MONDO:0007947/biolink:DiseaseToPhenotypicFeatureAssociation").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "assoc_disease_phenotype.json"))
    )
    registry = make_registry("monarch")
    result = await registry.call(
        "monarch", "associations", {"entity_id": "MONDO:0007947", "category": "disease_phenotype", "limit": 2}
    )
    params = route.calls.last.request.url.params
    assert params["direct"] == "true" and params["limit"] == "2" and params["offset"] == "0"
    assert result.total == 138
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.type == "association"
    assert rec.attributes["predicate"] == "has_phenotype"
    assert rec.attributes["subject"] == "MONDO:0007947"
    assert rec.identifiers["hp"] == rec.attributes["object"]
    assert rec.title.startswith("Marfan syndrome has phenotype")


@respx.mock
async def test_associations_gene_disease_and_unknown(make_registry):
    respx.get(f"{API}/entity/HGNC:3603/biolink:CausalGeneToDiseaseAssociation").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "assoc_gene_disease.json"))
    )
    registry = make_registry("monarch")
    result = await registry.call(
        "monarch", "associations", {"entity_id": "HGNC:3603", "category": "causal_gene_disease"}
    )
    assert result.total == 9
    rec = result.records[0]
    assert rec.identifiers["mondo"].startswith("MONDO:")  # the disease side
    assert rec.attributes["subject_label"] == "FBN1"
    assert rec.attributes["primary_source"] == "omim"

    respx.get(f"{API}/entity/FOO:1/biolink:CausalGeneToDiseaseAssociation").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    missing = await registry.call("monarch", "associations", {"entity_id": "FOO:1", "category": "causal_gene_disease"})
    assert missing.records == [] and "no causal_gene_disease" in missing.notes[0]


@respx.mock
async def test_phenotype_similarity_search(make_registry):
    route = respx.get(f"{API}/semsim/search/HP:0001166,HP:0000545,HP:0002616/Human%20Diseases").mock(
        return_value=httpx.Response(200, json=load_fixture("monarch", "semsim.json"))
    )
    registry = make_registry("monarch")
    result = await registry.call(
        "monarch",
        "phenotype_similarity_search",
        {"phenotypes": ["hp:0001166", "HP:0000545", "HP:0002616", "HP:0001166"], "limit": 2},
    )
    assert route.calls.last.request.url.params["limit"] == "2"
    top = result.records[0]
    assert top.id == "MONDO:0859194"
    assert top.attributes["rank"] == 1 and top.attributes["score"] > 10
    assert top.identifiers["omim"] == "619543"


@pytest.mark.live
async def test_live_monarch_all_tools(live_registry):
    registry = live_registry("monarch")
    search = await registry.call("monarch", "search", {"query": "Marfan syndrome", "category": "disease", "limit": 3})
    assert any(r.id == "MONDO:0007947" for r in search.records)
    entity = await registry.call("monarch", "get_entity", {"entity_id": "OMIM:154700"})
    assert entity.records[0].id == "MONDO:0007947"
    assoc = await registry.call(
        "monarch", "associations", {"entity_id": "HGNC:3603", "category": "causal_gene_disease", "limit": 5}
    )
    assert assoc.records and assoc.records[0].identifiers.get("mondo")
    pheno = await registry.call(
        "monarch", "associations", {"entity_id": "MONDO:0007947", "category": "disease_phenotype", "limit": 5}
    )
    assert pheno.total and pheno.total > 50
    sim = await registry.call(
        "monarch", "phenotype_similarity_search", {"phenotypes": ["HP:0001166", "HP:0000545", "HP:0002616"], "limit": 5}
    )
    assert sim.records and sim.records[0].attributes["score"] > 0
