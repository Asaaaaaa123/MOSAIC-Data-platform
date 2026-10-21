"""NDEx connector tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://www.ndexbio.org/v2"
PID = "640e2cef-795d-11e8-a4bf-0ac135e8bacf"


def fx(name):
    return httpx.Response(200, json=load_fixture("ndex", name))


@respx.mock
async def test_search_networks(make_registry):
    route = respx.post(f"{API}/search/network").mock(return_value=fx("search.json"))
    registry = make_registry("ndex")
    result = await registry.call("ndex", "search_networks", {"query": "TP53 apoptosis", "limit": 2, "cursor": "4"})
    request = route.calls.last.request
    assert json.loads(request.content) == {"searchString": "TP53 apoptosis"}
    assert request.url.params["start"] == "2" and request.url.params["size"] == "2"
    assert result.total == 1379 and result.next_cursor == "6"
    rec = result.records[0]
    assert rec.id == "26914970-5ec4-11ed-ae36-0ac135e8bacf"
    assert rec.title == "NeoPPI - TP53" and rec.type == "pathway"
    assert rec.identifiers["ndex"] == rec.id
    # the cited paper (shared by every NeoPPI network) is a related entity, not the network's own DOI
    assert rec.identifiers == {"ndex": rec.id}
    assert rec.attributes["reference_doi"] == "10.1016/j.cell.2022.04.014"  # parsed from the reference property
    assert rec.url == f"https://www.ndexbio.org/viewer/networks/{rec.id}"
    assert rec.date == "2022-11-09"
    assert rec.attributes["node_count"] == 228
    assert "<" not in (rec.summary or "")


@respx.mock
async def test_network_summary(make_registry):
    respx.get(f"{API}/network/{PID}/summary").mock(return_value=fx("summary.json"))
    registry = make_registry("ndex")
    result = await registry.call("ndex", "network_summary", {"uuid": PID.upper()})
    rec = result.records[0]
    assert rec.title == "NCI PID - Complete Interactions"
    assert rec.attributes["organism"] == "Homo sapiens (human)"
    assert rec.attributes["edge_count"] > 20000
    assert "organism" in result.data["properties"]
    assert "@context" not in result.data["properties"]


@respx.mock
async def test_network_summary_not_found(make_registry):
    uuid = "00000000-0000-0000-0000-000000000000"
    respx.get(f"{API}/network/{uuid}/summary").mock(return_value=httpx.Response(404, json={"errorCode": "NDEx_Object_Not_Found_Exception"}))
    registry = make_registry("ndex")
    result = await registry.call("ndex", "network_summary", {"uuid": uuid})
    assert result.records == [] and "not found" in result.notes[0]


async def test_network_summary_rejects_bad_uuid(make_registry):
    registry = make_registry("ndex")
    result = await registry.call("ndex", "network_summary", {"uuid": "../../admin"})
    assert result.records == [] and "not an NDEx network UUID" in result.notes[0]


@respx.mock
async def test_query_network_interconnect(make_registry):
    route = respx.post(f"{API}/search/network/{PID}/interconnectquery").mock(return_value=fx("interconnect.json"))
    registry = make_registry("ndex")
    result = await registry.call(
        "ndex", "query_network",
        {"uuid": PID, "genes": ["TP53", "MDM2", "ATM", "CHEK2"], "mode": "interconnect", "max_edges": 3},
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"searchString": "TP53 MDM2 ATM CHEK2", "edgeLimit": 500}
    assert result.data["node_count"] == 4 and result.data["edge_count"] == 6
    assert len(result.data["edges"]) == 3
    edge = result.data["edges"][0]
    assert {"source", "interaction", "target"} <= set(edge)
    assert edge["citations"][0].startswith("pubmed:")
    by_symbol = {r.identifiers["gene_symbol"]: r for r in result.records}
    assert set(by_symbol) == {"TP53", "MDM2", "ATM", "CHEK2"}
    assert "P04637" in by_symbol["TP53"].attributes["uniprot_accessions"]
    # represents + alias lists hold secondary accessions: no single UniProt id is asserted.
    assert "uniprot" not in by_symbol["MDM2"].identifiers
    assert "Q00987" in by_symbol["MDM2"].attributes["uniprot_accessions"]
    assert by_symbol["TP53"].attributes["query_gene"] is True


@respx.mock
async def test_query_network_neighborhood_body(make_registry):
    route = respx.post(f"{API}/search/network/{PID}/query").mock(return_value=fx("interconnect.json"))
    registry = make_registry("ndex")
    result = await registry.call("ndex", "query_network", {"uuid": PID, "genes": ["TP53"], "depth": 2, "edge_limit": 10})
    assert json.loads(route.calls.last.request.content) == {"searchString": "TP53", "edgeLimit": 10, "searchDepth": 2}
    assert result.data["mode"] == "neighborhood"


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("ndex")
    search = await registry.call("ndex", "search_networks", {"query": "NCI PID", "limit": 3})
    assert search.records and search.total
    summary = await registry.call("ndex", "network_summary", {"uuid": PID})
    assert summary.records[0].attributes["node_count"] > 1000
    sub = await registry.call("ndex", "query_network", {"uuid": PID, "genes": ["TP53", "MDM2"], "edge_limit": 200, "max_edges": 5})
    assert sub.data["edge_count"] > 0 and sub.records
