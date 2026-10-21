"""Synapse connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://repo-prod.prod.sagebase.org/repo/v1"


@respx.mock
async def test_search_body_and_normalization(make_registry):
    route = respx.post(f"{API}/search").mock(
        return_value=httpx.Response(201, json=load_fixture("synapse", "search.json"))
    )
    registry = make_registry("synapse")
    result = await registry.call("synapse", "search", {"query": "ROSMAP proteomics", "limit": 2})

    body = json.loads(route.calls.last.request.content)
    assert body["queryTerm"] == ["ROSMAP", "proteomics"]
    assert body["start"] == 0 and body["size"] == 2
    assert "booleanQuery" not in body
    assert "authorization" not in route.calls.last.request.headers
    assert result.total == 778 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "syn64957327"
    assert rec.identifiers == {"synapse": "syn64957327"}
    assert rec.type == "dataset"
    assert rec.url == "https://www.synapse.org/Synapse:syn64957327"
    assert rec.date == "2025-03-05"
    assert rec.attributes["project"] == "AD Knowledge Portal - backend"
    assert rec.attributes["entity_type"] == "folder"
    assert "**" not in (rec.summary or "")


@respx.mock
async def test_search_node_type_filter_and_token(make_registry, monkeypatch):
    monkeypatch.setenv("SYNAPSE_AUTH_TOKEN", "syn-test-token")
    route = respx.post(f"{API}/search").mock(return_value=httpx.Response(201, json={"found": 0, "hits": []}))
    registry = make_registry("synapse")
    result = await registry.call("synapse", "search", {"query": "tau", "node_type": "dataset", "cursor": "20"})
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["booleanQuery"] == [{"key": "node_type", "value": "dataset"}]
    assert body["start"] == 20
    assert request.headers["authorization"] == "Bearer syn-test-token"
    assert result.records == [] and result.next_cursor is None


@respx.mock
async def test_get_entity_bundle_access_and_wiki(make_registry):
    bundle = respx.post(f"{API}/entity/syn3388564/bundle2").mock(
        return_value=httpx.Response(200, json=load_fixture("synapse", "bundle.json"))
    )
    ar = respx.get(f"{API}/entity/syn3388564/accessRequirement").mock(
        return_value=httpx.Response(200, json=load_fixture("synapse", "access_requirements.json"))
    )
    wiki = respx.get(f"{API}/entity/syn3388564/wiki").mock(
        return_value=httpx.Response(200, json=load_fixture("synapse", "wiki.json"))
    )
    registry = make_registry("synapse")
    result = await registry.call("synapse", "get_entity", {"syn_id": "https://www.synapse.org/Synapse:SYN3388564"})
    assert bundle.called and ar.called and wiki.called
    sent = json.loads(bundle.calls.last.request.content)
    assert sent["includeRestrictionInformation"] and sent["includeDOIAssociation"]
    rec = result.records[0]
    assert rec.identifiers == {"synapse": "syn3388564", "doi": "10.7303/syn3388564"}
    assert rec.title == "Gene Expression (RNA seq - bulk brain)"
    assert rec.attributes["restriction_level"] == "CONTROLLED_BY_ACT"
    assert rec.attributes["controlled_access"] is True
    assert {r["type"] for r in rec.attributes["access_requirements"]} == {
        "TermsOfUseAccessRequirement",
        "ManagedACTAccessRequirement",
    }
    assert rec.attributes["annotations"]["study"] == "ROSMAP"
    assert rec.summary  # from the wiki
    assert "CONTROLLED_BY_ACT" in result.notes[0]


async def test_get_entity_rejects_bad_id(make_registry):
    registry = make_registry("synapse")
    bad = await registry.call("synapse", "get_entity", {"syn_id": "not-an-id"})
    assert bad.records == [] and "not a Synapse id" in bad.notes[0]


@respx.mock
async def test_get_entity_not_found_note(make_registry):
    respx.post(f"{API}/entity/syn12345/bundle2").mock(return_value=httpx.Response(404, json={"reason": "missing"}))
    registry = make_registry("synapse")
    result = await registry.call("synapse", "get_entity", {"syn_id": "syn12345"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_list_children_paging(make_registry):
    route = respx.post(f"{API}/entity/children").mock(
        return_value=httpx.Response(200, json=load_fixture("synapse", "children.json"))
    )
    registry = make_registry("synapse")
    result = await registry.call("synapse", "list_children", {"syn_id": "syn2580853", "cursor": "abc"})
    body = json.loads(route.calls.last.request.content)
    assert body["parentId"] == "syn2580853" and body["nextPageToken"] == "abc"
    assert "dataset" in body["includeTypes"]
    assert result.total == 148 and result.next_cursor == "50a50"
    assert result.records[0].id == "syn5550376"
    assert result.records[0].attributes["parent_id"] == "syn2580853"


@pytest.mark.live
async def test_live_synapse_all_tools(live_registry):
    registry = live_registry("synapse")
    search = await registry.call("synapse", "search", {"query": "alzheimer", "limit": 3})
    assert search.records and search.total
    entity = await registry.call("synapse", "get_entity", {"syn_id": "syn3388564"})
    assert entity.records[0].attributes["restriction_level"]
    children = await registry.call("synapse", "list_children", {"syn_id": "syn2580853"})
    assert children.records
