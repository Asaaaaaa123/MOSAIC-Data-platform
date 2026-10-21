"""Dryad connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://datadryad.org/api/v2"
ENC = "doi%3A10.5061%2Fdryad.4qrfj6qk2"


@respx.mock
async def test_search_params_and_normalization(make_registry):
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("dryad", "search.json"))
    )
    registry = make_registry("dryad")
    result = await registry.call("dryad", "search", {"query": "glioblastoma", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["q"] == "glioblastoma" and sent["per_page"] == "2" and sent["page"] == "1"
    assert result.total == 21 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "10.5061/dryad.4qrfj6qk2"
    assert rec.identifiers == {"doi": "10.5061/dryad.4qrfj6qk2", "dryad": "10.5061/dryad.4qrfj6qk2"}
    assert rec.type == "dataset"
    assert rec.date == "2024-10-23"
    assert rec.authors[0] == "Robert Rifkin"
    assert rec.url == "https://datadryad.org/dataset/doi:10.5061/dryad.4qrfj6qk2"
    assert rec.attributes["license"] == "CC0-1.0"
    assert {"relation": "primary_article", "scheme": "doi", "id": "10.1016/j.neuropharm.2024.110259"} in rec.attributes["related"]
    assert rec.attributes["funders"][0].startswith("National Institute of Neurological Disorders and Stroke (")
    assert "<p>" not in rec.summary


@respx.mock
async def test_search_last_page_has_no_cursor(make_registry):
    payload = load_fixture("dryad", "search.json")
    respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json=payload))
    registry = make_registry("dryad")
    result = await registry.call("dryad", "search", {"query": "glioblastoma", "limit": 10, "cursor": "3"})
    assert result.next_cursor is None  # 3 * 10 >= 21


@respx.mock
async def test_get_dataset_with_versions_and_files(make_registry):
    ds = respx.get(f"{API}/datasets/{ENC}").mock(
        return_value=httpx.Response(200, json=load_fixture("dryad", "dataset.json"))
    )
    vs = respx.get(f"{API}/datasets/{ENC}/versions").mock(
        return_value=httpx.Response(200, json=load_fixture("dryad", "versions.json"))
    )
    fs = respx.get(f"{API}/versions/323737/files").mock(
        return_value=httpx.Response(200, json=load_fixture("dryad", "files.json"))
    )
    registry = make_registry("dryad")
    result = await registry.call(
        "dryad", "get_dataset", {"doi": "https://datadryad.org/dataset/doi:10.5061/dryad.4qrfj6qk2", "files_limit": 2}
    )
    assert ds.called and vs.called and fs.called
    assert fs.calls.last.request.url.params["per_page"] == "2"
    rec = result.records[0]
    assert rec.attributes["versions"][0]["version"] == 7
    assert rec.attributes["file_count"] == 35
    f0 = rec.attributes["files"][0]
    assert f0["name"].endswith(".ns5") and f0["size"] > 0
    assert f0["digest"].startswith("sha-256:")
    assert f0["link"] == "https://datadryad.org/api/v2/files/3564587/download"
    assert "35 files" in result.notes[0]


@respx.mock
async def test_get_dataset_not_found(make_registry):
    respx.get(f"{API}/datasets/doi%3A10.5061%2Fdryad.nope").mock(
        return_value=httpx.Response(404, json={"error": "not-found"})
    )
    registry = make_registry("dryad")
    result = await registry.call("dryad", "get_dataset", {"doi": "10.5061/dryad.nope"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_datasets_for_article_keeps_only_linked(make_registry):
    payload = load_fixture("dryad", "for_article.json")
    payload["_embedded"]["stash:datasets"][2]["relatedWorks"] = []  # simulate a text-only hit
    route = respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json=payload))
    registry = make_registry("dryad")
    result = await registry.call("dryad", "datasets_for_article", {"doi": "doi:10.1016/J.NEUROPHARM.2024.110259"})
    assert route.calls.last.request.url.params["q"] == '"10.1016/j.neuropharm.2024.110259"'
    assert [r.id for r in result.records] == ["10.5061/dryad.4qrfj6qk2", "10.5061/dryad.m0cfxppd0"]
    assert "dropped" in result.notes[0]


@pytest.mark.live
async def test_live_dryad_all_tools(live_registry):
    registry = live_registry("dryad")
    search = await registry.call("dryad", "search", {"query": "microbiome", "limit": 3})
    assert search.records and search.total
    doi = search.records[0].identifiers["doi"]
    detail = await registry.call("dryad", "get_dataset", {"doi": doi, "files_limit": 3})
    assert detail.records[0].identifiers["doi"] == doi
    linked = await registry.call("dryad", "datasets_for_article", {"doi": "10.1016/j.neuropharm.2024.110259"})
    assert linked.records
