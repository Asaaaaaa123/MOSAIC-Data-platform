"""CIViC connector tests (GraphQL)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://civicdb.org/api/graphql"


def fx(name):
    return httpx.Response(200, json=load_fixture("civic", name))


def body(call):
    return json.loads(call.request.content)


@respx.mock
async def test_search(make_registry):
    route = respx.post(API).mock(return_value=fx("search.json"))
    registry = make_registry("civic")
    result = await registry.call("civic", "search", {"query": "BRAF V600E", "limit": 3})
    sent = body(route.calls.last)
    assert sent["variables"] == {"q": "BRAF V600E"}
    first = result.records[0]
    assert first.id == "MPID12" and first.title == "BRAF V600E"  # exact-name molecular profile first
    assert first.identifiers == {"civic": "MPID12", "gene_symbol": "BRAF"}
    assert first.url == "https://civicdb.org/molecular-profiles/12"
    variant = result.records[1]
    assert variant.id == "VID12" and variant.identifiers["gene_symbol"] == "BRAF"


@respx.mock
async def test_search_types_filter(make_registry):
    route = respx.post(API).mock(return_value=httpx.Response(200, json={"data": {"search": []}}))
    registry = make_registry("civic")
    result = await registry.call("civic", "search", {"query": "vemurafenib", "types": ["therapies"]})
    assert body(route.calls.last)["variables"]["types"] == ["THERAPY"]
    assert result.records == [] and result.notes


@respx.mock
async def test_gene_evidence_filters_prefix_matches_and_pages(make_registry):
    last_page = {"data": {"evidenceItems": {"totalCount": 33, "pageInfo": {"endCursor": "Nw", "hasNextPage": False}, "nodes": []}}}
    route = respx.post(API).mock(side_effect=[fx("evidence_gene.json"), httpx.Response(200, json=last_page), fx("gene.json")])
    registry = make_registry("civic")
    result = await registry.call("civic", "gene_evidence", {"gene": "ar", "limit": 4, "evidence_type": "PREDICTIVE"})
    first_vars = body(route.calls[0])["variables"]
    assert first_vars["mp"] == "AR" and first_vars["first"] == 4
    assert first_vars["status"] == "ACCEPTED" and first_vars["type"] == "PREDICTIVE"
    second_vars = body(route.calls[1])["variables"]
    assert second_vars["after"] == "NA" and second_vars["first"] == 3  # only the remaining slots
    assert [r.id for r in result.records] == ["EID863"]  # AREG Expression items dropped
    rec = result.records[0]
    assert rec.type == "association"
    # gene, disease and paper are related entities: an evidence item must not merge with them
    assert rec.identifiers == {"civic": "EID863"}
    assert rec.attributes["genes"] == ["AR"] and rec.attributes["entrez_genes"] == ["367"]
    assert rec.attributes["disease_doid"] == "DOID:10283"
    assert rec.attributes["pmid"] == "25184630"
    assert rec.attributes["evidence_level"] and rec.attributes["significance"]
    assert result.total == 33 and result.next_cursor is None
    assert "skipped 3" in result.notes[0]
    assert result.data["gene"]["entrez_gene"] == 367


@respx.mock
async def test_evidence_for_therapy(make_registry):
    route = respx.post(API).mock(return_value=fx("evidence_therapy.json"))
    registry = make_registry("civic")
    result = await registry.call("civic", "evidence_for_therapy", {"therapy": "vemurafenib", "disease": "Melanoma", "limit": 2})
    sent = body(route.calls.last)["variables"]
    assert sent["therapy"] == "vemurafenib" and sent["disease"] == "Melanoma" and "mp" not in sent
    assert result.total == 124 and result.next_cursor == "Mg"
    assert result.records[0].id == "EID11238"
    assert "Vemurafenib" in result.records[0].attributes["therapies"]
    assert result.records[0].attributes["disease_doid"] == "DOID:1909"
    from mosaic.crosslink.merge import cluster_records

    assert len(cluster_records(result.records)) == 2  # two melanoma evidence items stay distinct


@respx.mock
async def test_molecular_profile_by_name(make_registry):
    route = respx.post(API).mock(side_effect=[fx("mp_by_name.json"), fx("mp.json")])
    registry = make_registry("civic")
    result = await registry.call("civic", "molecular_profile", {"profile": "BRAF V600E"})
    assert body(route.calls[1])["variables"] == {"id": 12}
    rec = result.records[0]
    assert rec.id == "MPID12" and rec.type == "variant"
    assert "gene_symbol" not in rec.identifiers and "entrez_gene" not in rec.identifiers  # the gene is related
    assert rec.attributes["genes"] == ["BRAF"] and rec.attributes["entrez_genes"] == ["673"]
    assert rec.identifiers["rsid"] == "rs113488022"
    assert rec.identifiers["clinvar"] == "13961"
    assert rec.identifiers["hgvs"] == "NM_004333.4:c.1799T>A"
    assert rec.attributes["evidence_counts"]["predictive"] > 100
    assert rec.attributes["variants"][0]["coordinates"].startswith("GRCH37 chr7:140453136")
    assert result.notes == []


@respx.mock
async def test_molecular_profile_by_id_not_found(make_registry):
    route = respx.post(API).mock(return_value=httpx.Response(200, json={"data": {"molecularProfile": None}}))
    registry = make_registry("civic")
    result = await registry.call("civic", "molecular_profile", {"profile": "MPID99999999"})
    assert body(route.calls.last)["variables"] == {"id": 99999999}
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_assertions(make_registry):
    route = respx.post(API).mock(return_value=fx("assertions.json"))
    registry = make_registry("civic")
    result = await registry.call("civic", "assertions", {"gene_or_profile": "BRAF", "disease": "Melanoma", "limit": 2})
    sent = body(route.calls.last)["variables"]
    assert sent["mp"] == "BRAF" and sent["disease"] == "Melanoma"
    rec = result.records[0]
    assert rec.id == "AID275"
    assert rec.identifiers == {"civic": "AID275"} and rec.attributes["disease_doid"] == "DOID:1909"
    assert rec.attributes["amp_level"] == "TIER_I_LEVEL_A"
    assert result.next_cursor == "Mg" and result.total == 11


async def test_assertions_requires_a_filter(make_registry):
    registry = make_registry("civic")
    result = await registry.call("civic", "assertions", {})
    assert result.records == [] and result.notes


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("civic")
    search = await registry.call("civic", "search", {"query": "EGFR L858R", "limit": 5})
    assert search.records
    gene = await registry.call("civic", "gene_evidence", {"gene": "EGFR", "limit": 3, "evidence_level": "A"})
    assert gene.records and all(r.attributes.get("genes") in (["EGFR"], None) for r in gene.records)
    therapy = await registry.call("civic", "evidence_for_therapy", {"therapy": "osimertinib", "limit": 3})
    assert therapy.records and therapy.records[0].attributes.get("disease_doid")
    mp = await registry.call("civic", "molecular_profile", {"profile": "12"})
    assert mp.records[0].identifiers["rsid"] == "rs113488022"
    assertions = await registry.call("civic", "assertions", {"gene_or_profile": "BRAF V600E", "limit": 3})
    assert assertions.records


def test_complex_profile_does_not_take_first_variant_ids():
    from mosaic.connectors.civic import profile_record

    variant = lambda vid, gene, rs, cv: {  # noqa: E731
        "id": vid, "name": "x", "feature": {"name": gene, "featureInstance": {"entrezId": 1}},
        "clinvarIds": [cv], "hgvsDescriptions": [f"NM_{vid}:c.1A>T"], "myVariantInfo": {"dbsnpRsid": rs},
    }
    mp = {"id": 5, "name": "BRAF V600E AND KRAS G12D", "variants": [variant(1, "BRAF", "rs1", "11"), variant(2, "KRAS", "rs2", "22")]}
    rec = profile_record(mp)
    assert rec.identifiers == {"civic": "MPID5"}  # not the same entity as BRAF V600E alone
    assert rec.attributes["genes"] == ["BRAF", "KRAS"]
