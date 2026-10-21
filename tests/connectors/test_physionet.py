"""PhysioNet connector tests."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.physionet import parse_project_ref
from tests.conftest import load_fixture

API = "https://physionet.org/api/v1/project/published"
SEARCH = f"{API}/search/"
DATACITE = "https://api.datacite.org/dois"


def _fixture_response(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("physionet", name))


def test_parse_project_ref():
    assert parse_project_ref("mimiciv") == ("mimiciv", None, None)
    assert parse_project_ref("mimiciv/3.1") == ("mimiciv", "3.1", None)
    assert parse_project_ref("https://physionet.org/content/ptb-xl/1.0.3/") == ("ptb-xl", "1.0.3", None)
    assert parse_project_ref("https://physionet.org/content/mimiciv/") == ("mimiciv", None, None)
    assert parse_project_ref("https://doi.org/10.13026/KPB9-MT58") == (None, None, "10.13026/kpb9-mt58")
    assert parse_project_ref("not a slug!") == (None, None, None)


@respx.mock
async def test_search_sends_term_and_normalizes(make_registry):
    route = respx.get(SEARCH).mock(return_value=_fixture_response("search_af.json"))
    registry = make_registry("physionet")
    result = await registry.call("physionet", "search_projects", {"query": "atrial fibrillation ecg", "limit": 3})
    sent = route.calls.last.request.url.params
    assert sent["search_term"] == "atrial fibrillation ecg"
    assert "resource_type" not in sent
    assert result.total == 6
    assert result.next_cursor == "3"
    assert len(result.records) == 3
    afdb = next(r for r in result.records if r.id == "afdb/1.0.0")
    assert afdb.type == "dataset"
    assert afdb.title == "MIT-BIH Atrial Fibrillation Database"
    assert afdb.identifiers == {"doi": "10.13026/c2mw2d", "physionet": "afdb"}
    assert afdb.url == "https://physionet.org/content/afdb/1.0.0/"
    assert afdb.date == "2000-11-04"
    assert afdb.attributes["access_policy"] == "Open"
    assert afdb.attributes["requires_credentialing"] is False
    assert "atrial fibrillation" in afdb.attributes["topics"]
    assert afdb.summary and "<p>" not in afdb.summary
    page2 = await registry.call(
        "physionet", "search_projects", {"query": "atrial fibrillation ecg", "limit": 3, "cursor": "3"}
    )
    assert len(page2.records) == 3 and page2.next_cursor is None
    assert not {r.id for r in page2.records} & {r.id for r in result.records}


@respx.mock
async def test_search_reranks_canonical_project_first(make_registry):
    respx.get(SEARCH).mock(return_value=_fixture_response("search_mimic.json"))
    registry = make_registry("physionet")
    result = await registry.call("physionet", "search_projects", {"query": "mimic", "limit": 3})
    first = result.records[0]
    # the server puts MIMIC-CXR-Ext-... first; the exact-slug project must win after re-ranking
    assert first.id == "mimiciv/3.1"
    assert first.attributes["access_policy"] == "Credentialed"
    assert first.attributes["requires_credentialing"] is True
    assert first.attributes["core_doi"] == "10.13026/07hj-2a80"
    assert first.identifiers["doi"] == "10.13026/kpb9-mt58"
    assert any("not open access" in n for n in result.notes)
    assert result.total == 8


@respx.mock
async def test_search_access_and_type_filters(make_registry):
    route = respx.get(SEARCH).mock(return_value=_fixture_response("search_mimic.json"))
    registry = make_registry("physionet")
    open_only = await registry.call("physionet", "search_projects", {"query": "mimic", "access_policy": "open"})
    assert {r.id.split("/")[0] for r in open_only.records} == {"mimicdb", "mimic-iv-demo"}
    assert all(r.attributes["access_policy"] == "Open" for r in open_only.records)
    assert not any("not open access" in n for n in open_only.notes)

    route.mock(return_value=_fixture_response("browse.json"))
    review = await registry.call("physionet", "search_projects", {"query": "*", "access_policy": "contributor_review"})
    assert "search_term" not in route.calls.last.request.url.params
    assert [r.id for r in review.records] == ["hirid/1.1.1"]
    assert review.records[0].attributes["requires_credentialing"] is True

    software = await registry.call("physionet", "search_projects", {"query": "*", "resource_type": "software"})
    assert route.calls.last.request.url.params["resource_type"] == "software"
    assert [r.attributes["resource_type"] for r in software.records] == ["Software"]
    assert software.records[0].type == "other"

    newest = await registry.call("physionet", "search_projects", {"query": "*", "limit": 2})
    dates = [r.date for r in newest.records]
    assert dates == sorted(dates, reverse=True)

    route.mock(return_value=httpx.Response(200, json=[]))
    none = await registry.call("physionet", "search_projects", {"query": "zzzqqq"})
    assert none.records == [] and none.total == 0 and none.next_cursor is None


@respx.mock
async def test_get_project_by_url(make_registry):
    versions = respx.get(f"{API}/mimiciv/").mock(return_value=_fixture_response("versions_mimiciv.json"))
    detail = respx.get(f"{API}/mimiciv/3.1/").mock(return_value=_fixture_response("project_mimiciv_3.1.json"))
    registry = make_registry("physionet")
    result = await registry.call("physionet", "get_project", {"project": "https://physionet.org/content/mimiciv/"})
    assert versions.called and detail.called
    record = result.records[0]
    assert record.id == "mimiciv/3.1"
    assert record.identifiers == {"doi": "10.13026/kpb9-mt58", "physionet": "mimiciv"}
    assert record.date == "2024-10-11"
    assert record.authors[:2] == ["Johnson A", "Bulgarelli L"] and record.authors[-1] == "Mark R"
    attrs = record.attributes
    assert attrs["access_policy"] == "Credentialed"
    assert "CITI" in attrs["access_requirements"]
    assert attrs["license"] == "PhysioNet Credentialed Health Data License 1.5.0"
    assert attrs["versions"][-1] == "3.1" and "0.3" in attrs["versions"]
    assert attrs["is_latest_version"] is True
    assert attrs["files_url"] == "https://physionet.org/files/mimiciv/3.1/"
    assert attrs["project_home_page"] == "https://mimic.mit.edu"
    assert attrs["citation"].startswith("Johnson A")
    assert "download_hint" not in attrs
    assert result.notes[0].startswith("ACCESS: Credentialed")
    assert result.request_url == f"{API}/mimiciv/3.1/"


@respx.mock
async def test_get_project_by_doi_via_datacite(make_registry):
    dc = respx.get(f"{DATACITE}/10.13026/kpb9-mt58").mock(return_value=_fixture_response("datacite_mimiciv_3.1.json"))
    respx.get(f"{API}/mimiciv/").mock(return_value=_fixture_response("versions_mimiciv.json"))
    respx.get(f"{API}/mimiciv/3.1/").mock(return_value=_fixture_response("project_mimiciv_3.1.json"))
    registry = make_registry("physionet")
    result = await registry.call("physionet", "get_project", {"project": "doi:10.13026/KPB9-MT58"})
    assert dc.called
    assert result.records[0].id == "mimiciv/3.1"


@respx.mock
async def test_get_project_old_version_and_not_found(make_registry):
    respx.get(f"{API}/mimiciv/").mock(return_value=_fixture_response("versions_mimiciv.json"))
    old = respx.get(f"{API}/mimiciv/3.0/").mock(return_value=_fixture_response("project_mimiciv_3.1.json"))
    respx.get(f"{API}/mimiciv/9.9/").mock(
        return_value=httpx.Response(404, json={"detail": "No PublishedProject matches the given query."})
    )
    respx.get(f"{API}/nosuchproject/").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{DATACITE}/10.13026/zzzz-0000").mock(return_value=httpx.Response(404, json={"errors": []}))
    registry = make_registry("physionet")

    older = await registry.call("physionet", "get_project", {"project": "mimiciv/3.0"})
    assert old.called
    assert any("not the latest" in n for n in older.notes)

    missing_version = await registry.call("physionet", "get_project", {"project": "mimiciv", "version": "9.9"})
    assert missing_version.records == []
    assert "not found" in missing_version.notes[0] and "3.1" in missing_version.notes[0]
    missing = await registry.call("physionet", "get_project", {"project": "nosuchproject"})
    assert missing.records == [] and "not found" in missing.notes[0]
    bad_doi = await registry.call("physionet", "get_project", {"project": "10.13026/zzzz-0000"})
    assert bad_doi.records == [] and "does not resolve" in bad_doi.notes[0]
    unparsable = await registry.call("physionet", "get_project", {"project": "???"})
    assert unparsable.records == [] and "could not parse" in unparsable.notes[0]


@pytest.mark.live
async def test_live_physionet_all_tools(live_registry):
    registry = live_registry("physionet")
    result = await registry.call("physionet", "search_projects", {"query": "atrial fibrillation ecg", "limit": 5})
    assert result.records and result.total
    assert all(r.identifiers.get("physionet") for r in result.records)
    assert any(r.identifiers.get("doi", "").startswith("10.13026/") for r in result.records)
    detail = await registry.call("physionet", "get_project", {"project": "mimiciv"})
    record = detail.records[0]
    assert record.identifiers["physionet"] == "mimiciv"
    assert record.identifiers["doi"].startswith("10.13026/")
    assert record.attributes["access_policy"] == "Credentialed"
    assert record.authors
    by_doi = await registry.call("physionet", "get_project", {"project": "10.13026/kpb9-mt58"})
    assert by_doi.records[0].id == "mimiciv/3.1"
