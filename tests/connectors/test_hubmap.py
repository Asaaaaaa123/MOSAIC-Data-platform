"""HuBMAP connector tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import load_fixture

SEARCH = "https://search.api.hubmapconsortium.org/v3/portal/search"
ENTITY = "https://entity.api.hubmapconsortium.org/entities"


def _body(call) -> dict:
    return json.loads(call.request.content)


@respx.mock
async def test_search_datasets(make_registry):
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=load_fixture("hubmap", "search.json")))
    registry = make_registry("hubmap")

    result = await registry.call(
        "hubmap", "search_datasets", {"query": "kidney", "limit": 2, "processing": "Processed", "organ": "Kidney"}
    )

    body = _body(route.calls.last)
    assert body["size"] == 2 and body["from"] == 0
    must = body["query"]["bool"]["must"]
    assert must[0]["simple_query_string"]["query"] == "kidney"
    assert must[0]["simple_query_string"]["default_operator"] == "and"
    assert must[1]["match"]["origin_samples_unique_mapped_organs"]["query"] == "Kidney"
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"entity_type.keyword": "Dataset"}} in filters
    assert {"term": {"processing.keyword": "processed"}} in filters
    assert "_source" in body and "files" not in body["_source"]
    assert result.total == 312
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "HBM292.XZZJ.243"
    assert first.identifiers == {"hubmap": "HBM292.XZZJ.243", "doi": "10.35079/hbm292.xzzj.243"}
    assert first.date == "2022-01-27"
    assert first.url.startswith("https://portal.hubmapconsortium.org/browse/dataset/")
    assert first.attributes["organs"] == ["Kidney (Right)"]
    assert first.attributes["access"] in ("public", "protected")
    assert "age" in first.attributes["donor"]


@respx.mock
async def test_search_cursor_offset(make_registry):
    route = respx.post(SEARCH).mock(
        return_value=httpx.Response(200, json={"hits": {"total": {"value": 12}, "hits": []}})
    )
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "search_datasets", {"query": "*", "cursor": "10"})
    body = _body(route.calls.last)
    assert body["from"] == 10
    assert body["query"]["bool"]["must"][0] == {"match_all": {}}
    assert result.records == [] and result.next_cursor is None


@respx.mock
async def test_get_dataset_with_publications(make_registry):
    route = respx.post(SEARCH).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("hubmap", "dataset.json")),
            httpx.Response(200, json=load_fixture("hubmap", "dataset_publications.json")),
        ]
    )
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "get_dataset", {"dataset_id": "hbm329.crsf.278"})

    first_body = _body(route.calls[0])
    assert first_body["query"]["bool"]["filter"] == [{"term": {"hubmap_id.keyword": "HBM329.CRSF.278"}}]
    pub_body = _body(route.calls[1])
    assert {"term": {"ancestor_ids": "0775dc5b1d61c5fe35f8ad57c9f1574d"}} in pub_body["query"]["bool"]["filter"]
    dataset, publication = result.records
    assert dataset.identifiers["doi"] == "10.35079/hbm329.crsf.278"
    assert dataset.attributes["publications"][0]["doi"] == "10.1038/s41467-025-60435-8"
    assert publication.type == "article"
    assert publication.identifiers["doi"] == "10.1038/s41467-025-60435-8"
    assert any("publication" in n for n in result.notes)


@respx.mock
async def test_get_dataset_not_found_and_bad_id(make_registry):
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}}))
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "get_dataset", {"dataset_id": "HBM000.AAAA.000"})
    assert result.records == [] and "not found" in result.notes[0]
    bad = await registry.call("hubmap", "get_dataset", {"dataset_id": "kidney"})
    assert bad.records == [] and "not a HuBMAP id" in bad.notes[0]


@respx.mock
async def test_search_publications(make_registry):
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=load_fixture("hubmap", "publications.json")))
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "search_publications", {"query": "intestine", "limit": 2})
    body = _body(route.calls.last)
    assert {"term": {"entity_type.keyword": "Publication"}} in body["query"]["bool"]["filter"]
    assert result.total == 2 and result.next_cursor is None
    paper = result.records[0]
    assert paper.type == "article"
    assert paper.identifiers["doi"] == "10.1038/s41586-023-05915-x"
    assert paper.date == "2023-07-20"
    assert paper.attributes["venue"] == "Nature"


@respx.mock
async def test_get_entity_donor(make_registry):
    route = respx.get(f"{ENTITY}/HBM765.TRHD.452").mock(
        return_value=httpx.Response(200, json=load_fixture("hubmap", "entity_donor.json"))
    )
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "get_entity", {"entity_id": "HBM765.TRHD.452"})
    assert route.called
    donor = result.records[0]
    assert donor.attributes["entity_type"] == "Donor"
    clinical = donor.attributes["clinical"]
    assert clinical["age"] == "24 years"
    assert clinical["sex"] == "Female"
    assert isinstance(clinical["social_history"], list)


@respx.mock
async def test_get_entity_not_found(make_registry):
    respx.get(f"{ENTITY}/0000000000000000000000000000abcd").mock(
        return_value=httpx.Response(404, json={"error": "404 Not Found"})
    )
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "get_entity", {"entity_id": "0000000000000000000000000000abcd"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_dataset_facets(make_registry):
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=load_fixture("hubmap", "facets.json")))
    registry = make_registry("hubmap")
    result = await registry.call("hubmap", "dataset_facets", {"query": "kidney", "size": 5})
    body = _body(route.calls.last)
    assert body["size"] == 0 and body["aggs"]["organ"]["terms"]["size"] == 5
    assert result.data["processing"]["raw"] > 0
    assert "Kidney (Left)" in result.data["organ"]
    assert result.total


@pytest.mark.live
async def test_live_hubmap_all_tools(live_registry):
    registry = live_registry("hubmap")
    result = await registry.call("hubmap", "search_datasets", {"query": "kidney", "limit": 3})
    assert result.records and result.total
    hid = result.records[0].identifiers["hubmap"]
    detail = await registry.call("hubmap", "get_dataset", {"dataset_id": hid})
    assert detail.records[0].identifiers["hubmap"] == hid
    pubs = await registry.call("hubmap", "search_publications", {"query": "kidney", "limit": 2})
    assert pubs.records and pubs.records[0].identifiers.get("doi")
    entity = await registry.call("hubmap", "get_entity", {"entity_id": "HBM765.TRHD.452"})
    assert entity.records[0].attributes["entity_type"] == "Donor"
    facets = await registry.call("hubmap", "dataset_facets", {"query": "heart"})
    assert facets.data["organ"]
