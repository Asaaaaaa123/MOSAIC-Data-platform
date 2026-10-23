"""OLS4 connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.ols import identifiers_from_curies, map_curie
from mosaic.curation import curate
from tests.conftest import load_fixture

API = "https://www.ebi.ac.uk/ols4/api"
ASTHMA_IRI_2X = "http%253A%252F%252Fpurl.obolibrary.org%252Fobo%252FMONDO_0004979"


def test_map_curie_canonical_keys():
    assert map_curie("MESH:D001249") == ("mesh", "D001249")
    assert map_curie("OMIM:154700") == ("omim", "154700")
    assert map_curie("EFO:0000270") == ("efo", "EFO_0000270")
    assert map_curie("http://www.ebi.ac.uk/efo/EFO_0000270") == ("efo", "EFO_0000270")
    assert map_curie("ORDO:558") == ("orphanet", "Orphanet:558")
    assert map_curie("http://www.orpha.net/ORDO/Orphanet_558") == ("orphanet", "Orphanet:558")
    assert map_curie("http://purl.obolibrary.org/obo/MONDO_0004979") == ("mondo", "MONDO:0004979")
    assert map_curie("http://identifiers.org/mesh/D001249") == ("mesh", "D001249")
    assert map_curie("UMLS:C0004096") == ("umls", "C0004096")
    assert map_curie("NCBIGene:2200") == ("entrez_gene", "2200")
    assert map_curie("FOO:1") is None
    ids, multi = identifiers_from_curies(["Orphanet:284963", "Orphanet:558"])
    assert ids["orphanet"] == "Orphanet:284963" and multi["orphanet"] == ["Orphanet:284963", "Orphanet:558"]


@respx.mock
async def test_search_terms_params_and_records(make_registry):
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "search.json"))
    )
    registry = make_registry("ols")
    result = await registry.call("ols", "search_terms", {"query": "asthma", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["q"] == "asthma"
    assert sent["rows"] == "2"
    assert sent["start"] == "0"
    assert sent["local"] == "true"
    assert sent["ontology"].startswith("mondo,efo,doid,hp")
    assert "exact" not in sent
    assert result.total == 122
    assert result.next_cursor == "2"
    hp, mondo = result.records
    assert hp.id == "HP:0002099" and hp.type == "phenotype" and hp.identifiers == {"hp": "HP:0002099"}
    assert mondo.id == "MONDO:0004979" and mondo.type == "disease"
    assert mondo.identifiers["mondo"] == "MONDO:0004979"
    assert mondo.title == "asthma"
    assert mondo.summary.startswith("A bronchial disease")
    assert "ols4/ontologies/mondo/classes/" in mondo.url

    await registry.call("ols", "search_terms", {"query": "asthma", "exact": True, "ontology": "hp", "cursor": "20"})
    sent = route.calls.last.request.url.params
    assert sent["exact"] == "true" and sent["ontology"] == "hp" and sent["start"] == "20"


@respx.mock
async def test_get_term_maps_xrefs(make_registry):
    route = respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "term.json"))
    )
    registry = make_registry("ols")
    result = await registry.call("ols", "get_term", {"term_id": "MONDO_0004979"})
    assert route.calls.last.request.url.params["obo_id"] == "MONDO:0004979"
    rec = result.records[0]
    assert rec.id == "MONDO:0004979"
    assert rec.title == "asthma"
    ids = rec.identifiers
    assert ids["mondo"] == "MONDO:0004979"
    assert ids["doid"] == "DOID:2841"
    assert ids["hp"] == "HP:0002099"
    assert ids["mesh"] == "D001249"
    assert ids["umls"] == "C0004096"
    assert ids["icd10"] == "J45"
    assert ids["ncit"] == "C28397"
    assert "DOID:2841" in rec.attributes["xrefs"]


@respx.mock
async def test_get_term_orphanet_and_not_found(make_registry):
    defining = respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(
        return_value=httpx.Response(200, json={"_embedded": {"terms": []}, "page": {"totalElements": 0}})
    )
    anywhere = respx.get(f"{API}/terms").mock(
        return_value=httpx.Response(200, json={"page": {"totalElements": 0}})
    )
    registry = make_registry("ols")
    result = await registry.call("ols", "get_term", {"term_id": "Orphanet:99999999"})
    assert defining.calls.last.request.url.params["obo_id"] == "ORDO:99999999"
    assert anywhere.called
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_term_with_ontology_404(make_registry):
    respx.get(f"{API}/ontologies/mondo/terms").mock(return_value=httpx.Response(404, json={"status": 404}))
    registry = make_registry("ols")
    result = await registry.call("ols", "get_term", {"term_id": "MONDO:9999999", "ontology": "mondo"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_term_hierarchy_children_paging(make_registry):
    respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "term.json"))
    )
    route = respx.get(f"{API}/ontologies/mondo/terms/{ASTHMA_IRI_2X}/hierarchicalChildren").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "children.json"))
    )
    registry = make_registry("ols")
    result = await registry.call(
        "ols", "term_hierarchy", {"term_id": "MONDO:0004979", "direction": "children", "limit": 3}
    )
    sent = route.calls.last.request.url.params
    assert sent["size"] == "3" and sent["page"] == "0"
    assert result.total == 8
    assert result.next_cursor == "1"
    child = result.records[0]
    assert child.id == "MONDO:0001491"
    assert child.title == "cough variant asthma"
    assert child.attributes["relative_of"] == "MONDO:0004979"
    assert result.data["relation"] == "hierarchicalChildren"


@respx.mock
async def test_map_term_uses_v2_linked_entities(make_registry):
    respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "term.json"))
    )
    route = respx.get(f"{API}/v2/ontologies/mondo/classes/{ASTHMA_IRI_2X}").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "v2_class.json"))
    )
    registry = make_registry("ols")
    result = await registry.call("ols", "map_term", {"term_id": "MONDO:0004979"})
    assert route.called
    source = result.records[0]
    assert source.id == "MONDO:0004979"
    assert source.identifiers["doid"] == "DOID:2841"
    assert source.identifiers["mesh"] == "D001249"
    by_id = {r.id: r for r in result.records[1:]}
    doid = by_id["DOID:2841"]
    assert doid.title == "asthma"
    assert doid.identifiers == {"doid": "DOID:2841"}
    assert doid.attributes["relation"] == "xref"
    assert doid.attributes["mapped_from"] == "MONDO:0004979"
    assert any(m["relation"] == "exactMatch" for m in result.data["mappings"])
    assert result.total == len(result.data["mappings"])


@respx.mock
async def test_close_narrow_and_broad_matches_are_not_identifiers(make_registry):
    term = load_fixture("ols", "term.json")
    v2 = load_fixture("ols", "v2_class.json")
    v2["http://www.w3.org/2004/02/skos/core#closeMatch"] = ["OMIM:600807"]
    v2["http://www.w3.org/2004/02/skos/core#narrowMatch"] = [{"value": "Orphanet:1234", "axioms": []}]
    v2["http://www.w3.org/2004/02/skos/core#broadMatch"] = "MONDO:0005087"
    respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(return_value=httpx.Response(200, json=term))
    respx.get(f"{API}/v2/ontologies/mondo/classes/{ASTHMA_IRI_2X}").mock(return_value=httpx.Response(200, json=v2))
    registry = make_registry("ols")
    result = await registry.call("ols", "map_term", {"term_id": "MONDO:0004979"})
    source = result.records[0]
    assert source.identifiers["doid"] == "DOID:2841"  # cross-references and exact matches still link
    assert "omim" not in source.identifiers and "orphanet" not in source.identifiers
    assert source.attributes["loose_matches"] == [
        "OMIM:600807 (closeMatch)", "Orphanet:1234 (narrowMatch)", "MONDO:0005087 (broadMatch)"]
    omim = next(r for r in result.records if r.id == "OMIM:600807")
    assert omim.identifiers == {"omim": "600807"} and omim.attributes["relation"] == "closeMatch"
    entity = next(e for e in curate(result.records).entities if any(s["id"] == "MONDO:0004979" for s in e.sources))
    assert {s["id"] for s in entity.sources}.isdisjoint({"OMIM:600807", "Orphanet:1234", "MONDO:0005087"})


@respx.mock
async def test_map_term_identifiers_are_normalized(make_registry):
    v2 = load_fixture("ols", "v2_class.json")
    v2["http://www.w3.org/2004/02/skos/core#exactMatch"].append("ORDO:558")  # map_curie gives 'Orphanet:558'
    respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(
        return_value=httpx.Response(200, json=load_fixture("ols", "term.json")))
    respx.get(f"{API}/v2/ontologies/mondo/classes/{ASTHMA_IRI_2X}").mock(return_value=httpx.Response(200, json=v2))
    registry = make_registry("ols")
    result = await registry.call("ols", "map_term", {"term_id": "MONDO:0004979"})
    orphanet = next(r for r in result.records if r.id == "ORDO:558")
    assert result.records[0].identifiers["orphanet"] == orphanet.identifiers["orphanet"] == "558"  # so they link


@respx.mock
async def test_get_term_close_match_annotation_is_not_an_identifier(make_registry):
    term = load_fixture("ols", "term.json")
    found = term["_embedded"]["terms"][0] if "_embedded" in term else term
    found.setdefault("annotation", {})["closeMatch"] = ["http://identifiers.org/omim/600807"]
    respx.get(f"{API}/terms/findByIdAndIsDefiningOntology").mock(return_value=httpx.Response(200, json=term))
    registry = make_registry("ols")
    rec = (await registry.call("ols", "get_term", {"term_id": "MONDO:0004979"})).records[0]
    assert "omim" not in rec.identifiers and rec.identifiers["doid"] == "DOID:2841"
    assert rec.attributes["loose_matches"] == ["http://identifiers.org/omim/600807 (closeMatch)"]


@pytest.mark.live
async def test_live_ols_all_tools(live_registry):
    registry = live_registry("ols")
    search = await registry.call("ols", "search_terms", {"query": "type 2 diabetes", "ontology": "mondo", "limit": 3})
    assert any(r.id == "MONDO:0005148" for r in search.records)
    term = await registry.call("ols", "get_term", {"term_id": "MONDO:0005148"})
    ids = term.records[0].identifiers
    assert ids["mesh"] == "D003924" and ids["doid"] == "DOID:9352"
    orpha = await registry.call("ols", "get_term", {"term_id": "Orphanet:558"})
    assert orpha.records and orpha.records[0].identifiers["orphanet"] == "558"  # canonical form: bare number
    kids = await registry.call("ols", "term_hierarchy", {"term_id": "MONDO:0004979", "direction": "children"})
    assert kids.records and kids.total >= 5
    mapped = await registry.call("ols", "map_term", {"term_id": "MONDO:0007947"})
    assert mapped.records[0].identifiers["omim"] == "154700"
    assert any(r.id == "DOID:14323" for r in mapped.records)
