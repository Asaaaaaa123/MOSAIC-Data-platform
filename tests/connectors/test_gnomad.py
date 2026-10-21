"""gnomAD connector tests (offline respx mocks + live)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.connectors import gnomad as gnomad_module
from mosaic.connectors.gnomad import normalize_variant_id
from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://gnomad.broadinstitute.org/api"


def _registry(make_registry):
    registry = make_registry("gnomad")
    registry.http.set_rate("gnomad", 1000.0)  # the real pacing (~9/min) would make offline tests slow
    return registry


def _query(call) -> tuple[str, dict]:
    body = json.loads(call.request.content)
    return body["query"], body.get("variables") or {}


def test_normalize_variant_id():
    assert normalize_variant_id("1-55051215-G-GA") == "1-55051215-G-GA"
    assert normalize_variant_id("chr19:44908684 T>C") == "19-44908684-T-C"
    assert normalize_variant_id("x_100_a_t") == "X-100-A-T"
    assert normalize_variant_id("rs429358") is None
    assert normalize_variant_id("PCSK9") is None


@respx.mock
async def test_search_gene_symbol_returns_constraint_records(make_registry):
    route = respx.post(API).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("gnomad", "gene_search.json")),
            httpx.Response(200, json=load_fixture("gnomad", "gene_batch.json")),
        ]
    )
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "search", {"query": "PCSK9 loss of function", "limit": 3})

    first_query, variables = _query(route.calls[0])
    assert "gene_search(query: $q, reference_genome: GRCh38)" in first_query and variables == {"q": "PCSK9"}
    assert "variant_search" not in first_query  # would be an HTTP 500 for non-variant text
    batch_query, _ = _query(route.calls[1])
    assert 'g0: gene(gene_id: "ENSG00000169174", reference_genome: GRCh38)' in batch_query
    record = result.records[0]
    assert record.id == "ENSG00000169174" and record.type == "gene"
    assert record.identifiers == {
        "ensembl_gene": "ENSG00000169174",
        "gene_symbol": "PCSK9",
        "hgnc": "HGNC:20001",
        "entrez_gene": "255738",
        "omim": "607786",
        "ensembl_transcript": "ENST00000302118",
        "refseq": "NM_174936",
    }
    assert record.attributes["loeuf"] > 1 and record.attributes["pLI"] < 0.01
    assert "LOEUF=" in record.summary
    assert "searched gene token(s): PCSK9" in result.notes[0]


@respx.mock
async def test_search_rsid_routes_to_variant_search(make_registry):
    route = respx.post(API).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("gnomad", "variant_search.json")),
            httpx.Response(200, json=load_fixture("gnomad", "variant_batch.json")),
        ]
    )
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "search", {"query": "RS429358"})
    query, variables = _query(route.calls[0])
    assert "variant_search(query: $q, dataset: gnomad_r4)" in query and variables == {"q": "rs429358"}
    details, _ = _query(route.calls[1])
    assert 'v0: variant(variantId: "19-44908684-T-C", dataset: gnomad_r4)' in details
    assert 'c0: clinvar_variant(variant_id: "19-44908684-T-C", reference_genome: GRCh38)' in details
    assert result.records[0].identifiers["rsid"] == "rs429358"


@respx.mock
async def test_get_variant_by_id(make_registry):
    route = respx.post(API).mock(return_value=httpx.Response(200, json=load_fixture("gnomad", "variant_batch.json")))
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "get_variant", {"variant": "chr19:44908684 T>C"})
    assert route.call_count == 1  # variant IDs skip variant_search
    record = result.records[0]
    assert record.id == "19-44908684-T-C" and record.type == "variant"
    assert record.url == "https://gnomad.broadinstitute.org/variant/19-44908684-T-C?dataset=gnomad_r4"
    assert record.title == "19-44908684-T-C (APOE p.Cys130Arg, missense_variant)"
    ids = record.identifiers
    assert ids["gnomad_variant"] == "19-44908684-T-C"
    assert ids["rsid"] == "rs429358"
    assert ids["clinvar"] == "17864"
    assert ids["gene_symbol"] == "APOE"
    assert ids["hgvs"] == "ENST00000252486.9:c.388T>C"
    assert "ensembl_gene" not in ids  # the gene is an attribute of a variant, not its identity
    attrs = record.attributes
    assert 0.1 < attrs["af"] < 0.2 and attrs["homozygotes"] > 1000
    assert attrs["gene_id"] == "ENSG00000130203"
    assert attrs["consequence"] == "missense_variant" and attrs["hgvsp"] == "p.Cys130Arg"
    pops = attrs["top_populations"]
    assert pops and all("_" not in p["population"] for p in pops)  # no sex-split or sub-cohort rows
    assert all(p["population"] for p in pops) and pops == sorted(pops, key=lambda p: -p["af"])
    assert attrs["clinvar_significance"].startswith("Conflicting")
    assert "cadd" in attrs["in_silico"]


@respx.mock
async def test_get_variant_not_found(make_registry):
    respx.post(API).mock(
        return_value=httpx.Response(
            200, json={"errors": [{"message": "Variant not found"}], "data": {"v0": None, "c0": None}}
        )
    )
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "get_variant", {"variant": "1-12345-A-T"})
    assert result.records == [] and "not found" in result.notes[0]
    with pytest.raises(SourceError, match="not a variant ID"):
        await registry.call("gnomad", "get_variant", {"variant": "APOE e4"})


@respx.mock
async def test_gene_constraint(make_registry):
    route = respx.post(API).mock(return_value=httpx.Response(200, json=load_fixture("gnomad", "constraint.json")))
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "gene_constraint", {"gene": "ensg00000169174.11"})
    query, variables = _query(route.calls.last)
    assert "gene(gene_id: $v, reference_genome: GRCh38)" in query and variables == {"v": "ENSG00000169174"}
    record = result.records[0]
    assert record.identifiers["gene_symbol"] == "PCSK9"
    attrs = record.attributes
    assert attrs["constraint_version"] == "v4"
    assert attrs["obs_lof"] == 57 and attrs["exp_lof"] == 62.12
    assert attrs["oe_lof_ci"][1] == attrs["loeuf"]
    assert "Not LoF-intolerant" in record.summary


@respx.mock
async def test_gene_constraint_not_found(make_registry):
    respx.post(API).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Gene not found"}], "data": {"gene": None}})
    )
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "gene_constraint", {"gene": "NOTAGENE1"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_gene_variants_plof_filter_sort_and_counts(make_registry):
    route = respx.post(API).mock(
        return_value=httpx.Response(200, json=load_fixture("gnomad", "gene_variants_pcsk9.json"))
    )
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "gene_variants", {"gene": "PCSK9", "limit": 3})
    query, variables = _query(route.calls.last)
    assert "gene(gene_symbol: $v, reference_genome: GRCh38)" in query and "variants(dataset: gnomad_r4)" in query
    assert variables == {"v": "PCSK9"}
    # 7 pLoF variants in the fixture, one has AC=0 and is dropped
    assert result.total == 6 and result.next_cursor == "3"
    acs = [r.attributes["ac"] for r in result.records]
    assert acs == sorted(acs, reverse=True) and acs[0] == 5
    top = result.records[0]
    assert top.id == "1-55039902-T-TG" and top.attributes["consequence"] == "frameshift_variant"
    assert top.attributes["lof"] == "HC"
    assert top.identifiers["gnomad_variant"] == "1-55039902-T-TG"
    assert all(r.attributes["consequence"] in {"stop_gained", "frameshift_variant", "splice_donor_variant"} for r in result.records)
    counts = result.data["counts"]
    assert counts["observed"] == 12 and counts["plof"] == 6 and counts["missense"] == 3 and counts["synonymous"] == 2


@respx.mock
async def test_gene_variants_clinvar_and_max_af(make_registry):
    payload = load_fixture("gnomad", "gene_variants_pcsk9.json")
    payload["data"]["gene"]["clinvar_variants"][0]["clinical_significance"] = "Pathogenic/Likely pathogenic"
    respx.post(API).mock(return_value=httpx.Response(200, json=payload))
    registry = _registry(make_registry)
    result = await registry.call("gnomad", "gene_variants", {"gene": "PCSK9", "consequence": "clinvar_pathogenic"})
    assert [r.id for r in result.records] == ["1-55039842-G-A"]
    assert result.records[0].identifiers["clinvar"]
    assert result.records[0].attributes["clinvar_significance"] == "Pathogenic/Likely pathogenic"
    rare = await registry.call(
        "gnomad", "gene_variants", {"gene": "PCSK9", "consequence": "missense", "max_af": 0.000005, "sort": "position"}
    )
    assert [r.id for r in rare.records] == ["1-55039842-G-T", "1-55039842-G-A"]


@respx.mock
async def test_rate_limit_backs_off_once_then_raises(make_registry, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gnomad_module.asyncio, "sleep", fake_sleep)
    route = respx.post(API).mock(return_value=httpx.Response(429, text="<title>429</title>429 Too Many Requests"))
    registry = _registry(make_registry)
    with pytest.raises(SourceError, match="rate limit"):
        await registry.call("gnomad", "gene_constraint", {"gene": "PCSK9"})
    assert route.call_count == 2 and 20 in slept


@respx.mock
async def test_graphql_error_with_null_data_is_not_retried(make_registry):
    route = respx.post(API).mock(
        return_value=httpx.Response(500, json={"errors": [{"message": "Unrecognized query."}], "data": None})
    )
    registry = _registry(make_registry)
    with pytest.raises(SourceError, match="Unrecognized query"):
        await registry.call("gnomad", "search", {"query": "rs1"})
    assert route.call_count == 1


@pytest.mark.live
async def test_live_gnomad_all_tools(live_registry):
    """~6 requests; the connector paces them at ~9/min so this takes ~40 s."""
    registry = live_registry("gnomad")
    search = await registry.call("gnomad", "search", {"query": "PCSK9", "limit": 2})
    assert search.records[0].identifiers["ensembl_gene"] == "ENSG00000169174"
    constraint = await registry.call("gnomad", "gene_constraint", {"gene": "PCSK9"})
    assert constraint.records[0].attributes["loeuf"] is not None
    variant = await registry.call("gnomad", "get_variant", {"variant": "19-44908684-T-C"})
    assert variant.records[0].identifiers["rsid"] == "rs429358"
    variants = await registry.call("gnomad", "gene_variants", {"gene": "PCSK9", "limit": 5})
    assert variants.records and variants.data["counts"]["plof"] > 50
