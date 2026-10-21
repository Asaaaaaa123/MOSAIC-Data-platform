"""OSF connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

SHARE = "https://share.osf.io/api/v3/index-card-search"
API = "https://api.osf.io/v2"


@respx.mock
async def test_search_params_and_normalization(make_registry):
    route = respx.get(SHARE).mock(return_value=httpx.Response(200, json=load_fixture("osf", "search.json")))
    registry = make_registry("osf")
    result = await registry.call("osf", "search", {"query": "alzheimer EEG", "limit": 2})

    request = route.calls.last.request
    sent = request.url.params
    assert sent["cardSearchText"] == "alzheimer EEG"
    assert sent["cardSearchFilter[accessService]"] == "https://osf.io/"
    assert sent["cardSearchFilter[resourceType]"] == "Project,ProjectComponent,Registration,RegistrationComponent"
    assert sent["page[size]"] == "2"
    assert "page[cursor]" not in sent
    assert request.headers["accept"] == "application/vnd.api+json"

    assert result.total == 6
    assert result.next_cursor  # opaque SHARE cursor taken from links.next
    ids = [r.id for r in result.records]
    assert ids == ["5sfa4", "5xnwj"]  # searchResultPage order (not the included order)
    rec = result.records[0]
    assert rec.title == "Resting-state EEG Gamma in Alzheimer"
    assert rec.identifiers["osf"] == "5sfa4"
    assert rec.type == "dataset"
    assert rec.url == "https://osf.io/5sfa4/"
    assert rec.date == "2022-02-17"
    assert rec.authors and rec.authors[0] == "Furkan Erdal"
    assert result.records[1].attributes["parent"] == "gq73d"


@respx.mock
async def test_search_cursor_and_all_types(make_registry):
    route = respx.get(SHARE).mock(return_value=httpx.Response(200, json={"data": {"attributes": {}}, "included": []}))
    registry = make_registry("osf")
    result = await registry.call("osf", "search", {"query": "x", "resource_type": "all", "cursor": "WyJPQyJd"})
    sent = route.calls.last.request.url.params
    assert sent["page[cursor]"] == "WyJPQyJd"
    assert "cardSearchFilter[resourceType]" not in sent
    assert result.records == [] and result.next_cursor is None


@respx.mock
async def test_get_node_with_files(make_registry):
    node = respx.get(f"{API}/nodes/gknwy/").mock(return_value=httpx.Response(200, json=load_fixture("osf", "node.json")))
    files = respx.get(f"{API}/nodes/gknwy/files/osfstorage/").mock(
        return_value=httpx.Response(200, json=load_fixture("osf", "node_files.json"))
    )
    registry = make_registry("osf")
    result = await registry.call("osf", "get_node", {"guid": "https://osf.io/gknwy/", "files_limit": 2})
    assert node.called and files.called
    embeds = node.calls.last.request.url.params.get_list("embed")
    assert set(embeds) == {"contributors", "license", "identifiers", "affiliated_institutions"}
    rec = result.records[0]
    assert rec.id == "gknwy" and rec.type == "dataset"
    assert rec.title == "Derivative Data"
    assert rec.authors[0] == "Laura Hainke"
    assert rec.attributes["license"] == "CC-By Attribution 4.0 International"
    assert rec.attributes["category"] == "data"
    assert rec.attributes["root"] == "gq73d" and rec.attributes["parent"] == "gq73d"
    assert rec.attributes["files"][0]["folder_id"] == "676535d1a457ed879faf1244"
    assert rec.attributes["top_level_entries"] == 32
    assert "32 top-level entries" in result.notes[0]


@respx.mock
async def test_get_node_falls_back_to_registration(make_registry):
    respx.get(f"{API}/nodes/8nefz/").mock(return_value=httpx.Response(404, json={"errors": [{"detail": "Not found."}]}))
    reg = respx.get(f"{API}/registrations/8nefz/").mock(
        return_value=httpx.Response(200, json=load_fixture("osf", "registration.json"))
    )
    registry = make_registry("osf")
    result = await registry.call("osf", "get_node", {"guid": "https://doi.org/10.17605/OSF.IO/8NEFZ", "files_limit": 0})
    assert reg.called
    rec = result.records[0]
    assert rec.type == "study"
    assert rec.identifiers == {"osf": "8nefz", "doi": "10.17605/osf.io/8nefz"}
    assert rec.date == "2022-03-09"
    assert rec.attributes["kind"] == "registration"
    assert rec.attributes["has_data"] is False


@respx.mock
async def test_get_node_not_found(make_registry):
    respx.get(f"{API}/nodes/zzzzz/").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{API}/registrations/zzzzz/").mock(return_value=httpx.Response(404, json={}))
    registry = make_registry("osf")
    result = await registry.call("osf", "get_node", {"guid": "zzzzz"})
    assert result.records == [] and "not found" in result.notes[0]
    bad = await registry.call("osf", "get_node", {"guid": "../../etc"})
    assert bad.records == [] and "not an OSF GUID" in bad.notes[0]


@respx.mock
async def test_list_files_folder_paging(make_registry):
    route = respx.get(f"{API}/nodes/gknwy/files/osfstorage/676535d1a457ed879faf1244/").mock(
        return_value=httpx.Response(200, json=load_fixture("osf", "folder_files.json"))
    )
    registry = make_registry("osf")
    result = await registry.call(
        "osf", "list_files", {"guid": "gknwy", "folder_id": "676535d1a457ed879faf1244", "limit": 2}
    )
    sent = route.calls.last.request.url.params
    assert sent["page[size]"] == "2" and sent["page"] == "1"
    assert result.total == 13 and result.next_cursor == "2"
    assert result.data[0]["kind"] == "file" and result.data[0]["link"].startswith("https://osf.io/download/")
    bad = await registry.call("osf", "list_files", {"guid": "gknwy", "folder_id": "../x"})
    assert "24 hex" in bad.notes[0]


@pytest.mark.live
async def test_live_osf_all_tools(live_registry):
    registry = live_registry("osf")
    search = await registry.call("osf", "search", {"query": "alzheimer EEG", "limit": 3})
    assert search.records and search.total
    node = await registry.call("osf", "get_node", {"guid": "gknwy", "files_limit": 2})
    assert node.records[0].id == "gknwy"
    reg = await registry.call("osf", "get_node", {"guid": "8nefz", "files_limit": 0})
    assert reg.records[0].type == "study"
    files = await registry.call("osf", "list_files", {"guid": "gknwy", "limit": 2})
    assert files.data
