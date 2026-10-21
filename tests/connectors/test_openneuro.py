"""OpenNeuro connector tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.connectors.openneuro import parse_dataset_id
from tests.conftest import load_fixture

GRAPHQL = "https://openneuro.org/crn/graphql"


def _body(route) -> dict:
    return json.loads(route.calls.last.request.content)


def test_parse_dataset_id():
    assert parse_dataset_id("ds000117") == ("ds000117", None)
    assert parse_dataset_id("117") == ("ds000117", None)
    assert parse_dataset_id("https://openneuro.org/datasets/ds000117/versions/1.0.5") == ("ds000117", "1.0.5")
    assert parse_dataset_id("doi:10.18112/openneuro.ds000117.v1.0.5") == ("ds000117", "1.0.5")
    assert parse_dataset_id("nonsense") == (None, None)


@respx.mock
async def test_search_datasets(make_registry):
    route = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=load_fixture("openneuro", "search.json")))
    registry = make_registry("openneuro")

    result = await registry.call(
        "openneuro", "search_datasets", {"query": "visual working memory", "limit": 3, "modality": "EEG"}
    )

    body = _body(route)
    assert "advancedSearch" in body["query"]
    assert body["variables"]["query"] == {"keywords": ["visual working memory"], "sortBy": "relevance", "modality": "eeg"}
    assert body["variables"]["first"] == 3
    assert result.total == 217
    assert result.next_cursor == "WzcyLjYzMTI5LCJkczAwNTA4NiJd"
    assert len(result.records) == 2  # third edge is null (private dataset)
    assert any("omitted" in n for n in result.notes)
    first = result.records[0]
    assert first.id == "ds003519"
    assert first.identifiers == {"openneuro": "ds003519", "doi": "10.18112/openneuro.ds003519.v1.1.0"}
    assert first.title == "EEG: Visual Working Memory + Cabergoline Challenge"
    assert first.date == "2021-02-17"
    assert first.url == "https://openneuro.org/datasets/ds003519/versions/1.1.0"
    assert first.authors[0] == "James F Cavanagh"
    assert first.attributes["modalities"] == ["eeg"]
    assert first.attributes["subject_count"] > 0
    assert first.summary.startswith("Visual Working Memory")


@respx.mock
async def test_search_passes_cursor(make_registry):
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(
            200, json={"data": {"advancedSearch": {"pageInfo": {"hasNextPage": False, "count": 0}, "edges": []}}}
        )
    )
    registry = make_registry("openneuro")
    result = await registry.call("openneuro", "search_datasets", {"query": "x", "cursor": "abc", "sort": "newest"})
    assert _body(route)["variables"]["after"] == "abc"
    assert _body(route)["variables"]["query"]["sortBy"] == "newest"
    assert result.records == [] and result.next_cursor is None


@respx.mock
async def test_get_dataset(make_registry):
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json=load_fixture("openneuro", "dataset_ds000117.json"))
    )
    registry = make_registry("openneuro")
    result = await registry.call("openneuro", "get_dataset", {"dataset_id": "ds000117"})
    assert _body(route)["variables"] == {"id": "ds000117"}
    record = result.records[0]
    assert record.identifiers == {"openneuro": "ds000117", "doi": "10.18112/openneuro.ds000117.v2.0.0"}
    assert record.title == "Multisubject, multimodal face processing"
    assert record.attributes["license"] == "CC0"
    assert record.attributes["paper_pmids"] == ["25977808"]
    assert "2.0.0" in record.attributes["versions"]
    assert record.attributes["subject_count"] == 16  # 'emptyroom' pseudo-subject excluded
    assert "face processing" in record.summary
    assert "|" not in record.summary


@respx.mock
async def test_get_dataset_specific_tag_uses_snapshot_query(make_registry):
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Not Found"}], "data": {"snapshot": None}})
    )
    registry = make_registry("openneuro")
    result = await registry.call("openneuro", "get_dataset", {"dataset_id": "ds000117", "tag": "9.9.9"})
    assert _body(route)["variables"] == {"id": "ds000117", "tag": "9.9.9"}
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_dataset_not_found(make_registry):
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=load_fixture("openneuro", "dataset_missing.json")))
    registry = make_registry("openneuro")
    result = await registry.call("openneuro", "get_dataset", {"dataset_id": "ds999999"})
    assert result.records == []
    assert "not found" in result.notes[0]


@respx.mock
async def test_list_files(make_registry):
    route = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json=load_fixture("openneuro", "files_ds000117.json"))
    )
    registry = make_registry("openneuro")
    result = await registry.call("openneuro", "list_files", {"dataset_id": "ds000117", "tag": "2.0.0", "limit": 4})
    assert _body(route)["variables"] == {"id": "ds000117", "tag": "2.0.0", "tree": None}
    assert result.total == 6 and len(result.records) == 4
    readme = next(r for r in result.records if r.title == "README")
    assert readme.url.startswith("https://s3.amazonaws.com/openneuro.org/ds000117/README")
    assert any("more entries" in n for n in result.notes)


@pytest.mark.live
async def test_live_openneuro_all_tools(live_registry):
    registry = live_registry("openneuro")
    result = await registry.call("openneuro", "search_datasets", {"query": "working memory", "limit": 3})
    assert result.records and result.total
    assert all(r.identifiers["openneuro"].startswith("ds") for r in result.records)
    detail = await registry.call("openneuro", "get_dataset", {"dataset_id": "ds000117"})
    assert detail.records[0].identifiers["doi"].startswith("10.18112/openneuro.ds000117")
    files = await registry.call("openneuro", "list_files", {"dataset_id": "ds000117", "limit": 5})
    assert files.records and files.total
