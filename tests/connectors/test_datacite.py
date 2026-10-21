"""DataCite connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://api.datacite.org"


@respx.mock
async def test_search_datasets_params_and_normalization(make_registry):
    route = respx.get(f"{API}/dois").mock(
        return_value=httpx.Response(200, json=load_fixture("datacite", "search_datasets.json"))
    )
    registry = make_registry("datacite")
    result = await registry.call("datacite", "search_datasets", {"query": "glioblastoma single-cell", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["query"].startswith("(glioblastoma single-cell)")
    assert "NOT relatedIdentifiers.relationType:IsVersionOf" in sent["query"]
    assert sent["resource-type-id"] == "dataset"
    assert sent["page[size]"] == "2"
    assert sent["page[number]"] == "1"
    assert "xml" not in sent["fields[dois]"]
    assert "client-id" not in sent and "sort" not in sent

    assert result.total == 153
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "10.5281/zenodo.23408607"
    assert rec.type == "dataset"
    assert rec.identifiers == {"doi": "10.5281/zenodo.23408607", "zenodo": "23408607"}
    assert rec.date == "2026-10-17"
    assert rec.authors[0] == "Haodong Li"
    assert rec.attributes["license"] == "cc-by-4.0"
    assert rec.attributes["repository"] == "cern.zenodo"
    assert rec.title.startswith("Data and code for")
    assert result.records[1].identifiers["doi"] == "10.21227/eba5-td89"


@respx.mock
async def test_search_datasets_filters(make_registry):
    route = respx.get(f"{API}/dois").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"total": 0}, "links": {}})
    )
    registry = make_registry("datacite")
    result = await registry.call(
        "datacite",
        "search_datasets",
        {
            "query": "asthma",
            "repository": "vivli",
            "client_id": "bl.mendeley",
            "year_from": 2020,
            "resource_type": "any",
            "sort": "-published",
            "collapse_versions": False,
            "cursor": "3",
        },
    )
    sent = route.calls.last.request.url.params
    assert sent["query"] == "(asthma) AND publicationYear:[2020 TO *]"
    assert sent["client-id"] == "vivli.search,bl.mendeley"
    assert sent["sort"] == "-published"
    assert sent["page[number]"] == "3"
    assert "resource-type-id" not in sent
    assert result.records == [] and result.next_cursor is None


async def test_search_datasets_rejects_bad_client_id(make_registry):
    registry = make_registry("datacite")
    result = await registry.call("datacite", "search_datasets", {"query": "x", "client_id": "../evil host"})
    assert result.records == [] and "invalid client_id" in result.notes[0]


@respx.mock
async def test_get_doi_normalizes_related_and_ids(make_registry):
    route = respx.get(f"{API}/dois/10.5061/dryad.zs7h44jsj").mock(
        return_value=httpx.Response(200, json=load_fixture("datacite", "get_doi.json"))
    )
    registry = make_registry("datacite")
    result = await registry.call("datacite", "get_doi", {"doi": "https://doi.org/10.5061/DRYAD.ZS7H44JSJ"})
    assert route.called
    rec = result.records[0]
    assert rec.identifiers == {"doi": "10.5061/dryad.zs7h44jsj", "dryad": "10.5061/dryad.zs7h44jsj"}
    assert rec.authors[0] == "Esmerina Tili"
    assert rec.date == "2026-09-01"
    assert {"relation": "IsCitedBy", "scheme": "doi", "id": "10.1073/pnas.2618249123"} in rec.attributes["related"]
    assert rec.attributes["license"] == "cc0-1.0"
    assert rec.url == "https://datadryad.org/dataset/doi:10.5061/dryad.zs7h44jsj"
    assert "<" not in (rec.summary or "")


@respx.mock
async def test_get_doi_not_found(make_registry):
    respx.get(f"{API}/dois/10.9999/missing").mock(return_value=httpx.Response(404, json={"errors": []}))
    registry = make_registry("datacite")
    result = await registry.call("datacite", "get_doi", {"doi": "10.9999/missing"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_datasets_for_article_by_pmid(make_registry):
    route = respx.get(f"{API}/dois").mock(
        return_value=httpx.Response(200, json=load_fixture("datacite", "datasets_for_article.json"))
    )
    registry = make_registry("datacite")
    result = await registry.call("datacite", "datasets_for_article", {"pmid": "PMID:30824204"})
    sent = route.calls.last.request.url.params
    assert 'relatedIdentifiers.relatedIdentifier:"30824204"' in sent["query"]
    assert "relatedIdentifierType:PMID" in sent["query"]
    assert "resource-type-id" not in sent
    rec = result.records[0]
    assert rec.identifiers["dataverse"] == "doi:10.7910/DVN/CTJN63"
    assert {"relation": "IsSupplementTo", "scheme": "pmid", "id": "30824204"} in rec.attributes["related"]


@respx.mock
async def test_datasets_for_article_by_doi_query(make_registry):
    route = respx.get(f"{API}/dois").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"total": 0}, "links": {}})
    )
    registry = make_registry("datacite")
    result = await registry.call("datacite", "datasets_for_article", {"doi": "doi:10.1073/PNAS.2618249123"})
    query = route.calls.last.request.url.params["query"]
    assert '"10.1073/pnas.2618249123" OR "https://doi.org/10.1073/pnas.2618249123"' in query
    assert result.records == [] and result.notes


async def test_datasets_for_article_requires_an_id(make_registry):
    registry = make_registry("datacite")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("datacite", "datasets_for_article", {})


@respx.mock
async def test_repositories(make_registry):
    route = respx.get(f"{API}/clients").mock(
        return_value=httpx.Response(200, json=load_fixture("datacite", "repositories.json"))
    )
    registry = make_registry("datacite")
    result = await registry.call("datacite", "repositories", {"query": "vivli", "limit": 1})
    assert route.calls.last.request.url.params["query"] == "vivli"
    rec = result.records[0]
    assert rec.id == "vivli.search" and rec.type == "repository" and rec.title == "Vivli"
    assert rec.attributes["prefixes"] == ["10.25934"]
    assert result.data["repository_param_mapping"]["vivli"] == "vivli.search"


@pytest.mark.live
async def test_live_datacite_all_tools(live_registry):
    registry = live_registry("datacite")
    search = await registry.call("datacite", "search_datasets", {"query": "single-cell RNA-seq", "limit": 3})
    assert search.records and search.total and search.next_cursor == "2"
    doi = search.records[0].identifiers["doi"]
    detail = await registry.call("datacite", "get_doi", {"doi": doi})
    assert detail.records[0].identifiers["doi"] == doi
    linked = await registry.call("datacite", "datasets_for_article", {"pmid": "30824204"})
    assert any(r.identifiers.get("doi") == "10.7910/dvn/ctjn63" for r in linked.records)
    repos = await registry.call("datacite", "repositories", {"query": "zenodo"})
    assert any(r.id == "cern.zenodo" for r in repos.records)
    for repo in ("mendeley_data", "ieee_dataport", "vivli"):
        hits = await registry.call("datacite", "search_datasets", {"query": "cancer", "repository": repo, "limit": 1})
        assert hits.records, repo
