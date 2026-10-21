"""Springer Nature connector tests (respx offline + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.config import Settings
from mosaic.http import NotConfiguredError, SourceError
from tests.conftest import load_fixture

META = "https://api.springernature.com/meta/v2/json"
OA_JSON = "https://api.springernature.com/openaccess/json"
OA_JATS = "https://api.springernature.com/openaccess/jats"


@respx.mock
async def test_search_meta_normalizes_and_paginates(make_registry):
    route = respx.get(META).mock(return_value=httpx.Response(200, json=load_fixture("springer", "meta_search.json")))
    registry = make_registry("springer")
    result = await registry.call(
        "springer", "search",
        {"query": "keyword:crispr", "limit": 2, "year": 2026, "content_type": "Journal", "cursor": "0"},
    )
    sent = route.calls.last.request.url.params
    assert sent["q"] == "keyword:crispr datefrom:2026-01-01 dateto:2026-12-31 type:Journal"
    assert sent["s"] == "1" and sent["p"] == "2"
    assert sent["api_key"] == "test-key"
    assert "test-key" not in result.request_url and "api_key=***" in result.request_url

    assert result.total == 4509
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "10.1007/s12010-026-05916-2"
    assert first.type == "article"
    assert first.identifiers == {"doi": "10.1007/s12010-026-05916-2", "issn": "0273-2289"}
    assert first.url == "https://doi.org/10.1007/s12010-026-05916-2"
    assert first.date == "2026-11-01"
    assert first.authors[0] == "Hao Wang"
    assert first.title.startswith("Rapid Differential Detection of PRRSV")
    assert first.summary.startswith("Innovative")
    assert first.attributes["journal"] == "Applied Biochemistry and Biotechnology"
    assert first.attributes["open_access"] is False
    assert first.attributes["eissn"] == "1559-0291"
    assert "CRISPR" in first.attributes["keywords"]


@respx.mock
async def test_search_open_access_flag_routes_to_oa_api(make_registry):
    meta = respx.get(META).mock(return_value=httpx.Response(200, json={}))
    oa = respx.get(OA_JSON).mock(return_value=httpx.Response(200, json=load_fixture("springer", "oa_search.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "search", {"query": "base editing", "open_access": True})
    assert oa.called and not meta.called
    assert result.records[0].attributes["open_access"] is True


@respx.mock
async def test_search_no_hits_404_is_empty(make_registry):
    respx.get(META).mock(return_value=httpx.Response(404, json=load_fixture("springer", "not_found.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "search", {"query": "zzzxqv"})
    assert result.records == [] and result.total == 0 and result.notes


@respx.mock
async def test_premium_feature_error_is_explained(make_registry):
    respx.get(META).mock(return_value=httpx.Response(403, json=load_fixture("springer", "premium_403.json")))
    registry = make_registry("springer")
    with pytest.raises(SourceError) as info:
        await registry.call("springer", "search", {"query": "title:crispr"})
    assert info.value.status == 403 and "premium feature" in info.value.message
    assert "test-key" not in str(info.value)


async def test_limit_capped_at_25(make_registry):
    registry = make_registry("springer")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("springer", "search", {"query": "x", "limit": 50})


@respx.mock
async def test_get_by_doi(make_registry):
    route = respx.get(META).mock(return_value=httpx.Response(200, json=load_fixture("springer", "meta_doi.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "get_by_doi", {"doi": "https://doi.org/10.1038/S41586-019-1711-4"})
    assert route.calls.last.request.url.params["q"] == 'doi:"10.1038/s41586-019-1711-4"'
    record = result.records[0]
    assert record.identifiers["doi"] == "10.1038/s41586-019-1711-4"
    assert record.attributes["journal"] == "Nature"
    assert record.authors[0] == "Andrew V. Anzalone"
    assert len(record.attributes["subjects"]) == len(set(record.attributes["subjects"]))
    assert result.next_cursor is None


@respx.mock
async def test_get_by_doi_not_found(make_registry):
    respx.get(META).mock(return_value=httpx.Response(404, json=load_fixture("springer", "not_found.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "get_by_doi", {"doi": "10.9999/none"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_search_open_access(make_registry):
    route = respx.get(OA_JSON).mock(return_value=httpx.Response(200, json=load_fixture("springer", "oa_search.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "search_open_access", {"query": 'keyword:"base editing"', "limit": 2})
    assert route.calls.last.request.url.params["q"] == 'keyword:"base editing"'
    assert result.total == 7 and result.next_cursor == "2"
    first, second = result.records
    assert first.identifiers == {"doi": "10.1186/s13059-023-03097-3", "issn": "1474-760X"}
    assert first.summary.startswith("We introduce DEQSeq")
    assert second.summary.startswith("Background Gene editing")  # structured {"h1", "p": [...]} abstract
    assert first.attributes["journal"] == "Genome Biology"


@respx.mock
async def test_get_open_access_full_text_parses_jats(make_registry):
    route = respx.get(OA_JATS).mock(return_value=httpx.Response(200, text=load_fixture("springer", "full_text.xml")))
    registry = make_registry("springer")
    result = await registry.call(
        "springer", "get_open_access_full_text", {"doi": "10.1186/s13059-016-1012-2", "max_chars": 3000}
    )
    assert route.calls.last.request.url.params["q"] == 'doi:"10.1186/s13059-016-1012-2"'
    record = result.records[0]
    assert record.id == "10.1186/s13059-016-1012-2"
    assert record.title.startswith("Evaluation of off-target and on-target scoring algorithms")
    assert record.identifiers["issn"] == "1474-760X"
    assert record.date == "2016-07-05"
    assert record.authors[0] == "Maximilian Haeussler"
    assert record.attributes["journal"] == "Genome Biology"
    sections = result.data["sections"]
    assert sections[0]["title"] == "Abstract" and sections[0]["text"].startswith("Background The success")
    assert sections[1]["title"] == "Background"
    assert record.attributes["section_titles"][:5] == ["Abstract", "Background", "Results and discussion", "Acknowledgements", "Funding"]
    assert "Availability of data and materials" in record.attributes["section_titles"]
    assert sum(len(s["text"]) for s in sections) <= 3000
    assert result.data["truncated"] is True and "truncated" in result.notes[0]


@respx.mock
async def test_get_open_access_full_text_not_found(make_registry):
    respx.get(OA_JATS).mock(return_value=httpx.Response(404, json=load_fixture("springer", "not_found.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "get_open_access_full_text", {"doi": "10.1038/s41586-019-1711-4"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_count_facets(make_registry):
    route = respx.get(META).mock(return_value=httpx.Response(200, json=load_fixture("springer", "meta_search.json")))
    registry = make_registry("springer")
    result = await registry.call("springer", "count_facets", {"query": "keyword:crispr", "facets": ["subject"], "top": 2})
    assert route.calls.last.request.url.params["p"] == "1"
    assert result.total == 4509
    assert list(result.data["facets"]) == ["subject"]
    assert result.data["facets"]["subject"][0] == {"value": "Life Sciences", "count": 2481}
    assert len(result.data["facets"]["subject"]) == 2


async def test_oa_tool_without_oa_key_is_not_configured(tmp_path, make_registry, monkeypatch):
    monkeypatch.setattr("mosaic.config._dotenv", lambda: {})
    monkeypatch.delenv("SPRINGER_OPENACCESS_API_KEY", raising=False)
    settings = Settings(
        _env_file=None,
        springer_meta_api_key="test-key",
        springer_openaccess_api_key=None,
        biopha_data_dir=tmp_path / "d",
        biopha_audit_db=tmp_path / "d" / "a.db",
        biopha_cache_db=tmp_path / "d" / "c.db",
        biopha_cache_ttl=0,
    )
    registry = make_registry("springer", settings=settings)
    with pytest.raises(NotConfiguredError, match="SPRINGER_OPENACCESS_API_KEY"):
        await registry.call("springer", "search_open_access", {"query": "x"})


# ---------------------------------------------------------------- live
@pytest.mark.live
async def test_live_meta_tools(live_registry):
    registry = live_registry("springer")
    result = await registry.call("springer", "search", {"query": 'keyword:"prime editing"', "limit": 3, "year": 2024})
    assert result.records and result.total
    doi = result.records[0].identifiers["doi"]
    detail = await registry.call("springer", "get_by_doi", {"doi": doi})
    assert detail.records[0].identifiers["doi"] == doi
    facets = await registry.call("springer", "count_facets", {"query": 'keyword:"CAR-T"', "facets": ["pub", "year"]})
    assert facets.data["facets"]["pub"] and facets.total


@pytest.mark.live
async def test_live_open_access_tools(live_registry):
    registry = live_registry("springer")
    result = await registry.call("springer", "search_open_access", {"query": "keyword:CRISPR type:Journal", "limit": 2})
    assert result.records and all(r.attributes.get("open_access") for r in result.records)
    full = await registry.call(
        "springer", "get_open_access_full_text", {"doi": "10.1186/s13059-016-1012-2", "max_chars": 5000}
    )
    assert full.records[0].attributes["journal"] == "Genome Biology"
    assert len(full.data["sections"]) >= 2 and full.data["chars"] > 10000
