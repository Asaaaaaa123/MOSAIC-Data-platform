"""PGS Catalog connector tests."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://www.pgscatalog.org/rest"


def fx(name):
    return httpx.Response(200, json=load_fixture("pgs_catalog", name))


@respx.mock
async def test_search_traits(make_registry):
    route = respx.get(f"{API}/trait/search").mock(return_value=fx("trait_search.json"))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "search", {"query": "breast cancer", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["term"] == "breast cancer" and sent["limit"] == "2" and sent["offset"] == "0"
    assert result.total == 13 and result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "MONDO_0007254" and first.title == "breast cancer"
    assert first.type == "phenotype"
    assert first.identifiers == {"mondo": "MONDO:0007254"}
    assert first.url == "https://www.pgscatalog.org/trait/MONDO_0007254"
    assert first.attributes["pgs_count"] == 3


@respx.mock
async def test_search_scores_kind_uses_trait_pgs_ids(make_registry):
    respx.get(f"{API}/trait/search").mock(return_value=fx("trait_search.json"))
    scores = respx.get(f"{API}/score/search").mock(return_value=fx("score_search.json"))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "search", {"query": "breast cancer", "kind": "scores", "limit": 3})
    ids = scores.calls.last.request.url.params["pgs_ids"].split(",")
    assert len(ids) == 3 and ids == sorted(ids)
    assert result.total == 7  # 3 direct + 4 child ids in the trimmed fixture
    assert result.next_cursor == "3"
    assert "breast cancer (MONDO_0007254)" in result.notes[0]


@respx.mock
async def test_search_with_pgs_id_returns_score(make_registry):
    respx.get(f"{API}/score/PGS000004").mock(return_value=fx("score.json"))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "search", {"query": "PGS000004"})
    assert result.tool == "search" and result.records[0].id == "PGS000004"


@respx.mock
async def test_get_score_normalizes(make_registry):
    respx.get(f"{API}/score/PGS000004").mock(return_value=fx("score.json"))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "get_score", {"pgs_id": "pgs000004"})
    rec = result.records[0]
    assert rec.identifiers["pgs"] == "PGS000004"
    assert rec.identifiers["pmid"] == "30554720"
    assert rec.identifiers["doi"] == "10.1016/j.ajhg.2018.11.002"
    assert rec.identifiers["mondo"] == "MONDO:0004989"
    assert rec.date == "2018-12-13"
    assert rec.attributes["variants_number"] == 313
    assert rec.attributes["method_name"] == "Hard-Thresholding Stepwise Forward Regression"
    assert rec.attributes["ancestry_distribution"]["gwas"] == {"EUR": 100}
    assert rec.attributes["gwas_samples"][0]["n"] == 158648
    assert rec.attributes["scoring_file"].endswith("PGS000004.txt.gz")
    assert "GRCh38" in rec.attributes["harmonized_scoring_files"]


@respx.mock
async def test_get_score_not_found_empty_object(make_registry):
    respx.get(f"{API}/score/PGS999999").mock(return_value=httpx.Response(200, json={}))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "get_score", {"pgs_id": "PGS999999"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_scores_for_trait_paginates_ids(make_registry):
    respx.get(f"{API}/trait/MONDO_0005010").mock(return_value=fx("trait.json"))
    scores = respx.get(f"{API}/score/search").mock(return_value=fx("score_search.json"))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "scores_for_trait", {"trait_id": "MONDO:0005010", "limit": 2})
    assert scores.calls.last.request.url.params["pgs_ids"] == "PGS000010,PGS000011"
    assert result.total == 3 and result.next_cursor == "2"
    assert result.records[0].identifiers["pgs"] == "PGS000010"
    assert result.records[0].identifiers["doi"] == "10.1016/s0140-6736(14)61730-x"
    assert result.data["trait"]["id"] == "MONDO_0005010"

    direct = await registry.call(
        "pgs_catalog", "scores_for_trait", {"trait_id": "MONDO_0005010", "include_children": False}
    )
    assert direct.total == 2


@respx.mock
async def test_scores_for_unknown_trait(make_registry):
    respx.get(f"{API}/trait/EFO_0001645").mock(return_value=httpx.Response(200, json={}))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "scores_for_trait", {"trait_id": "EFO_0001645"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_publication_by_pmid(make_registry):
    route = respx.get(f"{API}/publication/search").mock(return_value=fx("publication_search.json"))
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "get_publication", {"publication": "30554720"})
    assert route.calls.last.request.url.params["pmid"] == "30554720"
    rec = result.records[0]
    assert rec.id == "PGP000002" and rec.type == "article"
    assert rec.identifiers == {"pmid": "30554720", "doi": "10.1016/j.ajhg.2018.11.002"}
    assert rec.authors[0] == "Mavaddat N"
    assert "PGS000004" in rec.attributes["pgs_developed"]


async def test_get_publication_rejects_doi(make_registry):
    registry = make_registry("pgs_catalog")
    result = await registry.call("pgs_catalog", "get_publication", {"publication": "10.1016/j.ajhg.2018.11.002"})
    assert result.records == [] and "DOI" in result.notes[0]


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("pgs_catalog")
    traits = await registry.call("pgs_catalog", "search", {"query": "breast cancer", "limit": 2})
    assert traits.records and traits.total
    scores = await registry.call("pgs_catalog", "search", {"query": "breast cancer", "kind": "scores", "limit": 2})
    assert scores.records and scores.records[0].identifiers["pgs"].startswith("PGS")
    score = await registry.call("pgs_catalog", "get_score", {"pgs_id": "PGS000004"})
    assert score.records[0].identifiers["pmid"] == "30554720"
    by_trait = await registry.call("pgs_catalog", "scores_for_trait", {"trait_id": "MONDO_0005010", "limit": 2})
    assert by_trait.records and by_trait.total > 10
    pub = await registry.call("pgs_catalog", "get_publication", {"publication": "PGP000002"})
    assert pub.records[0].identifiers["doi"] == "10.1016/j.ajhg.2018.11.002"
