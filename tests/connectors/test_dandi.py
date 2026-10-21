"""DANDI Archive connector tests."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.dandi import parse_dandiset_id
from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://api.dandiarchive.org/api"
VERSION = "0.260218.2052"


def test_parse_dandiset_id_variants():
    assert parse_dandiset_id("000003") == ("000003", None)
    assert parse_dandiset_id("DANDI:000003") == ("000003", None)
    assert parse_dandiset_id("3") == ("000003", None)
    assert parse_dandiset_id("https://dandiarchive.org/dandiset/000003/0.260218.2052") == ("000003", VERSION)
    assert parse_dandiset_id("10.48324/dandi.000003/0.260218.2052") == ("000003", VERSION)
    assert parse_dandiset_id("not an id") == (None, None)


@respx.mock
async def test_search_dandisets_normalizes_and_enriches(make_registry):
    search = respx.get(f"{API}/dandisets/").mock(
        return_value=httpx.Response(200, json=load_fixture("dandi", "search.json"))
    )
    respx.get(f"{API}/dandisets/000003/versions/{VERSION}/").mock(
        return_value=httpx.Response(200, json=load_fixture("dandi", "version_000003.json"))
    )
    respx.get(f"{API}/dandisets/000030/versions/draft/").mock(return_value=httpx.Response(404, json={}))
    registry = make_registry("dandi")

    result = await registry.call("dandi", "search_dandisets", {"query": "hippocampus", "limit": 2})

    sent = search.calls.last.request.url.params
    assert sent["search"] == "hippocampus"
    assert sent["page_size"] == "2"
    assert sent["page"] == "1"
    assert sent["draft"] == "true" and sent["empty"] == "false" and sent["embargoed"] == "false"
    assert result.total == 67
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "000003"
    assert first.type == "dataset"
    assert first.identifiers == {"dandi": "000003", "doi": f"10.48324/dandi.000003/{VERSION}"}
    assert first.title.startswith("Physiological Properties and Behavioral Correlates")
    assert first.date == "2026-02-18"
    assert first.url == f"https://dandiarchive.org/dandiset/000003/{VERSION}"
    assert first.authors[0] == "Senzai, Yuta"
    assert "theta maze" in first.summary
    assert first.attributes["species"] == ["House mouse"]
    assert first.attributes["subjects"] == 16
    assert first.attributes["related_dois"] == ["10.1016/j.neuron.2016.12.011"]
    # draft-only dandiset whose metadata 404s still yields a record from the listing
    second = result.records[1]
    assert second.id == "000030" and second.title == "Allen Brain Observatory Neuropixels recording"
    assert "doi" not in second.identifiers
    assert any("unavailable" in n for n in result.notes)


@respx.mock
async def test_search_without_details_and_cursor(make_registry):
    route = respx.get(f"{API}/dandisets/").mock(
        return_value=httpx.Response(200, json={"count": 3, "next": None, "results": []})
    )
    registry = make_registry("dandi")
    result = await registry.call(
        "dandi", "search_dandisets", {"query": "species:mouse", "cursor": "3", "details": False, "ordering": "-stars"}
    )
    params = route.calls.last.request.url.params
    assert params["page"] == "3" and params["ordering"] == "-stars"
    assert result.records == [] and result.next_cursor is None


async def test_search_rejects_bad_ordering(make_registry):
    registry = make_registry("dandi")
    with pytest.raises(SourceError, match="invalid ordering"):
        await registry.call("dandi", "search_dandisets", {"query": "x", "ordering": "random"})


@respx.mock
async def test_get_dandiset_full_metadata(make_registry):
    respx.get(f"{API}/dandisets/000003/").mock(
        return_value=httpx.Response(200, json=load_fixture("dandi", "dandiset_000003.json"))
    )
    respx.get(f"{API}/dandisets/000003/versions/{VERSION}/").mock(
        return_value=httpx.Response(200, json=load_fixture("dandi", "version_000003.json"))
    )
    registry = make_registry("dandi")
    result = await registry.call("dandi", "get_dandiset", {"dandiset_id": "DANDI:000003"})
    record = result.records[0]
    assert record.identifiers["doi"] == f"10.48324/dandi.000003/{VERSION}"
    attrs = record.attributes
    assert attrs["license"] == ["CC-BY-4.0"]
    assert attrs["citation"].startswith("Senzai, Yuta")
    assert attrs["contributors"][0]["orcid"] == "0000-0002-2990-9889"
    assert any(f.startswith("National Institutes of Health") for f in attrs["funders"])
    assert attrs["related_resources"][0]["relation"] == "IsDescribedBy"
    assert "spike sorting technique" in attrs["measurement_techniques"]
    assert attrs["asset_count"] == 101


@respx.mock
async def test_get_dandiset_not_found(make_registry):
    respx.get(f"{API}/dandisets/999999/").mock(return_value=httpx.Response(404, json={"detail": "Not found."}))
    registry = make_registry("dandi")
    result = await registry.call("dandi", "get_dandiset", {"dandiset_id": "999999"})
    assert result.records == []
    assert "not found" in result.notes[0]


@respx.mock
async def test_list_assets(make_registry):
    respx.get(f"{API}/dandisets/000003/").mock(
        return_value=httpx.Response(200, json=load_fixture("dandi", "dandiset_000003.json"))
    )
    route = respx.get(f"{API}/dandisets/000003/versions/{VERSION}/assets/").mock(
        return_value=httpx.Response(200, json=load_fixture("dandi", "assets.json"))
    )
    registry = make_registry("dandi")
    result = await registry.call("dandi", "list_assets", {"dandiset_id": "000003", "limit": 2, "glob": "*ecephys*"})
    params = route.calls.last.request.url.params
    assert params["glob"] == "*ecephys*" and params["page_size"] == "2"
    assert result.total == 101 and result.next_cursor == "2"
    asset = result.records[0]
    assert asset.title.endswith(".nwb")
    assert asset.url.endswith("/download/")
    assert asset.attributes["subject"].startswith("sub-")
    assert asset.identifiers == {}


@pytest.mark.live
async def test_live_dandi_all_tools(live_registry):
    registry = live_registry("dandi")
    result = await registry.call("dandi", "search_dandisets", {"query": "hippocampus", "limit": 3})
    assert result.records and result.total
    assert any(r.identifiers.get("doi") for r in result.records)
    detail = await registry.call("dandi", "get_dandiset", {"dandiset_id": "000003"})
    assert detail.records[0].identifiers["dandi"] == "000003"
    assert detail.records[0].identifiers["doi"].startswith("10.48324/dandi.000003/")
    assets = await registry.call("dandi", "list_assets", {"dandiset_id": "000003", "limit": 2})
    assert len(assets.records) == 2 and assets.total
