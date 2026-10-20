"""Crossref connector tests (reference pattern for connector tests)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://api.crossref.org"


@respx.mock
async def test_search_works_normalizes_records(make_registry):
    route = respx.get(f"{API}/works").mock(
        return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json"))
    )
    registry = make_registry("crossref")

    result = await registry.call("crossref", "search_works", {"query": "CRISPR base editing", "limit": 2})

    assert route.called
    sent = route.calls.last.request.url.params
    assert sent["query"] == "CRISPR base editing"
    assert sent["rows"] == "2"
    assert sent["mailto"] == "tests@mosaic.invalid"
    assert result.total == 4321
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.identifiers["doi"] == "10.1038/s41586-019-1711-4"
    assert first.title.startswith("Search-and-replace genome editing")
    assert first.date == "2019-12-05"
    assert first.authors[0] == "Andrew V. Anzalone"
    assert first.attributes["journal"] == "Nature"
    assert "tests@mosaic.invalid" not in result.request_url and "mailto=***" in result.request_url


@respx.mock
async def test_search_works_filters(make_registry):
    route = respx.get(f"{API}/works").mock(
        return_value=httpx.Response(200, json={"status": "ok", "message": {"total-results": 0, "items": []}})
    )
    registry = make_registry("crossref")
    await registry.call(
        "crossref", "search_works", {"query": "x", "year_from": 2020, "type": "journal-article", "sort": "published"}
    )
    params = route.calls.last.request.url.params
    assert params["filter"] == "from-pub-date:2020,type:journal-article"
    assert params["sort"] == "published"
    assert params["order"] == "desc"


@respx.mock
async def test_get_work_not_found_is_a_note_not_an_error(make_registry):
    respx.get(f"{API}/works/10.9999/missing").mock(return_value=httpx.Response(404, text="Resource not found."))
    registry = make_registry("crossref")
    result = await registry.call("crossref", "get_work", {"doi": "https://doi.org/10.9999/MISSING"})
    assert result.records == []
    assert "not found" in result.notes[0]


async def test_invalid_params_raise_source_error(make_registry):
    registry = make_registry("crossref")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("crossref", "search_works", {"query": "x", "limit": 1000})


@pytest.mark.live
async def test_live_search_and_get(live_registry):
    registry = live_registry("crossref")
    result = await registry.call("crossref", "search_works", {"query": "prime editing", "limit": 3})
    assert result.records and result.total
    doi = result.records[0].identifiers["doi"]
    detail = await registry.call("crossref", "get_work", {"doi": doi})
    assert detail.records[0].identifiers["doi"] == doi
