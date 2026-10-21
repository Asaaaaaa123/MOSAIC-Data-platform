"""Zenodo connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://zenodo.org/api/records"


@respx.mock
async def test_search_params_and_normalization(make_registry):
    route = respx.get(API).mock(return_value=httpx.Response(200, json=load_fixture("zenodo", "search.json")))
    registry = make_registry("zenodo")
    result = await registry.call("zenodo", "search", {"query": "glioblastoma single-cell", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["q"] == "glioblastoma single-cell"
    assert sent["type"] == "dataset"
    assert sent["size"] == "2" and sent["page"] == "1"
    assert sent["sort"] == "bestmatch"
    assert "authorization" not in route.calls.last.request.headers
    assert result.total == 16007
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "13984673"
    assert rec.identifiers == {"doi": "10.5281/zenodo.13984673", "zenodo": "13984673"}
    assert rec.type == "dataset"
    assert rec.authors == ["Timothy Olsen"]
    assert rec.date == "2024-10-25"
    assert rec.url == "https://zenodo.org/records/13984673"
    assert rec.attributes["concept_doi"] == "10.5281/zenodo.13984672"
    assert rec.attributes["file_count"] == 2
    assert {"relation": "isSupplementTo", "scheme": "doi", "id": "10.1101/2023.02.09.527940", "resource_type": "dataset"} in rec.attributes["related"]
    assert "<" not in rec.summary


@respx.mock
async def test_search_caps_anonymous_page_size_and_filters(make_registry):
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"hits": {"hits": [], "total": 0}, "links": {}}))
    registry = make_registry("zenodo")
    result = await registry.call(
        "zenodo",
        "search",
        {"query": "covid", "limit": 60, "type": "any", "communities": "covid-19", "sort": "mostrecent", "cursor": "3"},
    )
    sent = route.calls.last.request.url.params
    assert sent["size"] == "25" and sent["page"] == "3"
    assert sent["communities"] == "covid-19" and sent["sort"] == "mostrecent"
    assert "type" not in sent
    assert any("25" in n for n in result.notes)
    assert result.next_cursor is None


@respx.mock
async def test_token_sent_as_bearer_header(make_registry, offline_settings, monkeypatch):
    monkeypatch.setenv("ZENODO_ACCESS_TOKEN", "zen-test-token")
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"hits": {"hits": [], "total": 0}, "links": {}}))
    registry = make_registry("zenodo")
    await registry.call("zenodo", "search", {"query": "x", "limit": 60})
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer zen-test-token"
    assert request.url.params["size"] == "60"
    assert "zen-test-token" not in str(request.url)


@respx.mock
async def test_get_record_by_doi_with_files_and_versions(make_registry):
    rec_route = respx.get(f"{API}/6962483").mock(
        return_value=httpx.Response(200, json=load_fixture("zenodo", "record.json"))
    )
    ver_route = respx.get(f"{API}/6962483/versions").mock(
        return_value=httpx.Response(200, json=load_fixture("zenodo", "versions.json"))
    )
    registry = make_registry("zenodo")
    result = await registry.call("zenodo", "get_record", {"record": "https://doi.org/10.5281/zenodo.6962483"})
    assert rec_route.called and ver_route.called
    rec = result.records[0]
    assert rec.identifiers["zenodo"] == "6962483"
    files = rec.attributes["files"]
    assert files[0]["name"] == "Lenaerts_et_al_ATAC_LFS_v2.tar.gz"
    assert files[0]["checksum"].startswith("md5:") and files[0]["size"] > 0
    versions = rec.attributes["versions"]
    assert {v["id"] for v in versions} == {"6962483", "5720286"}
    assert any(v["latest"] for v in versions)


@respx.mock
async def test_get_record_not_found(make_registry):
    respx.get(f"{API}/123").mock(return_value=httpx.Response(404, json={"status": 404}))
    registry = make_registry("zenodo")
    result = await registry.call("zenodo", "get_record", {"record": "123"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_record_by_foreign_doi_searches_first(make_registry):
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"hits": {"hits": [], "total": 0}, "links": {}}))
    registry = make_registry("zenodo")
    result = await registry.call("zenodo", "get_record", {"record": "10.1234/abc"})
    assert route.calls.last.request.url.params["q"] == 'doi:"10.1234/abc"'
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_records_for_article(make_registry):
    route = respx.get(API).mock(
        return_value=httpx.Response(200, json=load_fixture("zenodo", "records_for_article.json"))
    )
    registry = make_registry("zenodo")
    result = await registry.call("zenodo", "records_for_article", {"doi": "10.1038/S41586-024-07493-Y", "limit": 1})
    q = route.calls.last.request.url.params["q"]
    assert q == 'metadata.related_identifiers.identifier:("10.1038/s41586-024-07493-y" OR "https://doi.org/10.1038/s41586-024-07493-y")'
    assert result.total == 11 and result.next_cursor == "2"
    assert result.records[0].id == "10060876"
    assert any(r["id"] == "10.1038/s41586-024-07493-y" for r in result.records[0].attributes["related"])


@pytest.mark.live
async def test_live_zenodo_all_tools(live_registry):
    registry = live_registry("zenodo")
    search = await registry.call("zenodo", "search", {"query": "single-cell RNA-seq", "limit": 3})
    assert search.records and search.total
    rid = search.records[0].id
    detail = await registry.call("zenodo", "get_record", {"record": rid})
    assert detail.records[0].identifiers["zenodo"] == rid
    linked = await registry.call("zenodo", "records_for_article", {"doi": "10.1038/s41586-024-07493-y"})
    assert linked.records
