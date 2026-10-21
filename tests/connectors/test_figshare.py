"""Figshare connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://api.figshare.com/v2"


@respx.mock
async def test_search_body_and_normalization(make_registry):
    route = respx.post(f"{API}/articles/search").mock(
        return_value=httpx.Response(200, json=load_fixture("figshare", "search.json"))
    )
    registry = make_registry("figshare")
    result = await registry.call("figshare", "search", {"query": "glioblastoma RNA-seq", "limit": 2})

    body = json.loads(route.calls.last.request.content)
    assert body == {"search_for": "glioblastoma RNA-seq", "item_type": 3, "page": 1, "page_size": 2}
    assert result.next_cursor == "2"  # full page -> maybe more
    rec = result.records[0]
    assert rec.id == "33717259"
    assert rec.type == "dataset"
    assert rec.identifiers == {"doi": "10.6084/m9.figshare.33717259.v1", "figshare": "33717259"}
    assert rec.date == "2026-09-14"
    assert rec.attributes["portal"] == "tandf.figshare.com"
    assert rec.attributes["related"][0]["relation"] == "IsSupplementTo"
    assert rec.attributes["related"][0]["id"] == "10.1080/15548627.2026.2732726"
    assert result.records[1].identifiers["doi"] == "10.1371/journal.pone.0355902.s008"


@respx.mock
async def test_search_options_and_last_page(make_registry):
    route = respx.post(f"{API}/articles/search").mock(return_value=httpx.Response(200, json=[]))
    registry = make_registry("figshare")
    result = await registry.call(
        "figshare",
        "search",
        {"query": "x", "item_type": "any", "order": "views", "published_since": "2024-01-01", "cursor": "4"},
    )
    body = json.loads(route.calls.last.request.content)
    assert "item_type" not in body
    assert body["order"] == "views" and body["order_direction"] == "desc"
    assert body["published_since"] == "2024-01-01" and body["page"] == 4
    assert result.records == [] and result.next_cursor is None


@respx.mock
async def test_get_article_full_record(make_registry):
    route = respx.get(f"{API}/articles/34700313").mock(
        return_value=httpx.Response(200, json=load_fixture("figshare", "article.json"))
    )
    registry = make_registry("figshare")
    result = await registry.call("figshare", "get_article", {"article_id": "10.6084/m9.figshare.34700313.v1"})
    assert route.called
    rec = result.records[0]
    assert rec.authors[0] == "Andrea Mancini"
    assert rec.identifiers["doi"] == "10.3389/fonc.2022.943064.s003"
    assert rec.attributes["license"] == "CC BY 4.0"
    assert rec.attributes["files"][0]["link"].startswith("https://ndownloader.figshare.com/files/")
    assert rec.attributes["files"][0]["md5"]
    assert "glioblastoma" in rec.attributes["keywords"]
    related = rec.attributes["related"]
    assert related[0] == {
        "relation": "IsSupplementTo",
        "scheme": "doi",
        "id": "10.3389/fonc.2022.943064",
        "title": related[0]["title"],
    }
    refs = [r for r in related if r["relation"] == "References"]
    assert 0 < len(refs) <= 10
    assert refs[0]["id"] == "10.1080/02688697.2019.1699903"  # "https://doi.org//10..." cleaned
    assert rec.summary and "<p>" not in rec.summary


@respx.mock
async def test_get_article_not_found_and_bad_id(make_registry):
    respx.get(f"{API}/articles/999").mock(return_value=httpx.Response(404, json={"code": "EntityNotFound"}))
    registry = make_registry("figshare")
    result = await registry.call("figshare", "get_article", {"article_id": "999"})
    assert result.records == [] and "not found" in result.notes[0]
    bad = await registry.call("figshare", "get_article", {"article_id": "10.1000/xyz"})
    assert bad.records == [] and "not a Figshare article id" in bad.notes[0]


@respx.mock
async def test_items_for_article(make_registry):
    route = respx.post(f"{API}/articles/search").mock(
        return_value=httpx.Response(200, json=load_fixture("figshare", "items_for_article.json"))
    )
    registry = make_registry("figshare")
    result = await registry.call(
        "figshare", "items_for_article", {"doi": "https://doi.org/10.3389/FONC.2022.943064", "limit": 5}
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {"resource_doi": "10.3389/fonc.2022.943064", "page": 1, "page_size": 5}
    assert [r.id for r in result.records] == ["34700316", "34700313"]
    assert result.next_cursor is None  # fewer than page_size


async def test_items_for_article_validates_doi(make_registry):
    registry = make_registry("figshare")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("figshare", "items_for_article", {"doi": "not-a-doi"})


@pytest.mark.live
async def test_live_figshare_all_tools(live_registry):
    registry = live_registry("figshare")
    search = await registry.call("figshare", "search", {"query": "RNA-seq", "limit": 3})
    assert search.records
    aid = search.records[0].id
    detail = await registry.call("figshare", "get_article", {"article_id": aid})
    assert detail.records[0].identifiers["figshare"] == aid
    items = await registry.call("figshare", "items_for_article", {"doi": "10.3389/fonc.2022.943064"})
    assert items.records
