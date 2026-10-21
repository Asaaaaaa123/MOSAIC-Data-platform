"""Dataverse connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

HARVARD = "https://dataverse.harvard.edu/api"


@respx.mock
async def test_search_params_and_normalization(make_registry):
    route = respx.get(f"{HARVARD}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("dataverse", "search.json"))
    )
    registry = make_registry("dataverse")
    result = await registry.call("dataverse", "search", {"query": "glioblastoma", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["q"] == "glioblastoma" and sent["type"] == "dataset"
    assert sent["per_page"] == "2" and sent["start"] == "0"
    assert "sort" not in sent
    assert result.total == 39 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "doi:10.7910/DVN/QRYIQZ"
    assert rec.identifiers == {"dataverse": "doi:10.7910/DVN/QRYIQZ", "doi": "10.7910/dvn/qryiqz"}
    assert rec.authors == ["Emel Sokullu"]
    assert rec.date == "2026-08-07"
    assert rec.attributes["file_count"] == 2
    assert rec.attributes["installation"] == "harvard"


@respx.mock
async def test_search_other_installation_and_sort(make_registry):
    route = respx.get("https://borealisdata.ca/api/search").mock(
        return_value=httpx.Response(200, json={"status": "OK", "data": {"total_count": 0, "items": []}})
    )
    registry = make_registry("dataverse")
    result = await registry.call(
        "dataverse", "search", {"query": "x", "installation": "borealis", "sort": "date", "cursor": "20"}
    )
    sent = route.calls.last.request.url.params
    assert sent["sort"] == "date" and sent["order"] == "desc" and sent["start"] == "20"
    assert result.records == [] and result.next_cursor is None


async def test_search_rejects_unknown_installation(make_registry):
    registry = make_registry("dataverse")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("dataverse", "search", {"query": "x", "installation": "evil.example.com"})


@respx.mock
async def test_get_dataset_native_metadata(make_registry):
    route = respx.get(f"{HARVARD}/datasets/:persistentId/").mock(
        return_value=httpx.Response(200, json=load_fixture("dataverse", "dataset.json"))
    )
    registry = make_registry("dataverse")
    result = await registry.call("dataverse", "get_dataset", {"persistent_id": "https://doi.org/10.7910/dvn/ctjn63"})
    assert route.calls.last.request.url.params["persistentId"] == "doi:10.7910/DVN/CTJN63"
    rec = result.records[0]
    assert rec.id == "doi:10.7910/DVN/CTJN63"
    assert rec.title == "Simulation Results: Global Childhood Cancer Incidence"
    assert rec.authors[0] == "Zachary Ward"
    assert rec.attributes["license"] == "CC0-1.0"
    assert rec.attributes["related"][0]["scheme"] == "pmid" and rec.attributes["related"][0]["id"] == "30824204"
    assert rec.attributes["files"][0]["link"].startswith("https://dataverse.harvard.edu/api/access/datafile/")
    assert rec.url == "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/CTJN63"
    assert rec.date and rec.date.startswith("2019")


@respx.mock
async def test_get_dataset_not_found(make_registry):
    respx.get(f"{HARVARD}/datasets/:persistentId/").mock(
        return_value=httpx.Response(404, json={"status": "ERROR", "message": "not found"})
    )
    registry = make_registry("dataverse")
    result = await registry.call("dataverse", "get_dataset", {"persistent_id": "doi:10.7910/DVN/NOPE00"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_datasets_for_article_queries(make_registry):
    route = respx.get(f"{HARVARD}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("dataverse", "for_article.json"))
    )
    registry = make_registry("dataverse")
    result = await registry.call("dataverse", "datasets_for_article", {"pmid": "30824204", "doi": "10.1016/X.1"})
    q = route.calls.last.request.url.params["q"]
    assert q.startswith('publicationIDNumber:"30824204" OR publicationIDNumber:"10.1016/x.1"')
    assert 'publicationURL:"https://doi.org/10.1016/x.1"' in q
    assert result.records[0].id == "doi:10.7910/DVN/CTJN63"


@pytest.mark.live
async def test_live_dataverse_all_tools(live_registry):
    registry = live_registry("dataverse")
    search = await registry.call("dataverse", "search", {"query": "cancer", "limit": 3})
    assert search.records and search.total
    pid = search.records[0].id
    detail = await registry.call("dataverse", "get_dataset", {"persistent_id": pid, "files_limit": 2})
    assert detail.records[0].identifiers["dataverse"] == pid
    linked = await registry.call("dataverse", "datasets_for_article", {"pmid": "30824204"})
    assert any(r.id == "doi:10.7910/DVN/CTJN63" for r in linked.records)
    other = await registry.call("dataverse", "search", {"query": "cancer", "installation": "borealis", "limit": 1})
    assert other.records
