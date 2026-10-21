"""GTEx connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.gtex import gtex_to_gnomad
from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://gtexportal.org/api/v2"


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("gtex", name))


@respx.mock
async def test_search_genes(make_registry):
    route = respx.get(f"{API}/reference/geneSearch").mock(return_value=_ok("gene_search_braf.json"))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "search_genes", {"query": "BRAF", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["geneId"] == "BRAF" and sent["gencodeVersion"] == "v39" and sent["page"] == "0"
    assert sent["itemsPerPage"] == "2"
    braf = result.records[0]
    assert braf.id == "ENSG00000157764.14"
    assert braf.type == "gene"
    assert braf.title == "BRAF: B-Raf proto-oncogene, serine/threonine kinase"
    assert braf.identifiers == {
        "ensembl_gene": "ENSG00000157764",
        "gene_symbol": "BRAF",
        "entrez_gene": "673",
        "hgnc": "HGNC:1097",
    }
    assert braf.url == "https://gtexportal.org/home/gene/BRAF"
    assert braf.attributes["gencode_id"] == "ENSG00000157764.14"
    assert result.total == 2 and result.next_cursor is None


@respx.mock
async def test_search_genes_reduces_free_text_to_gene_tokens(make_registry):
    route = respx.get(f"{API}/reference/geneSearch").mock(return_value=_ok("gene_search_braf.json"))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "search_genes", {"query": "BRAF V600E melanoma"})
    searched = [c.request.url.params["geneId"] for c in route.calls]
    assert searched == ["BRAF", "V600E"]
    assert "searched tokens" in result.notes[0]
    assert result.records[0].identifiers["gene_symbol"] == "BRAF"


@respx.mock
async def test_median_expression_sorted(make_registry):
    gene = respx.get(f"{API}/reference/gene").mock(return_value=_ok("gene_pcsk9.json"))
    median = respx.get(f"{API}/expression/medianGeneExpression").mock(return_value=_ok("median_pcsk9.json"))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "median_expression", {"gene": "PCSK9"})
    assert gene.calls.last.request.url.params["geneId"] == "PCSK9"
    sent = median.calls.last.request.url.params
    assert sent["gencodeId"] == "ENSG00000169174.11" and sent["datasetId"] == "gtex_v10"
    assert "tissueSiteDetailId" not in sent
    record = result.records[0]
    assert record.identifiers["ensembl_gene"] == "ENSG00000169174"
    assert record.identifiers["gene_symbol"] == "PCSK9"
    tissues = result.data["tissues"]
    tpms = [t["median_tpm"] for t in tissues]
    assert tpms == sorted(tpms, reverse=True)
    assert tissues[0]["tissue"] == "Adipose_Visceral_Omentum" and tissues[0]["uberon"].startswith("UBERON:")
    assert record.attributes["top_tissues"][0].startswith("Adipose_Visceral_Omentum")
    assert result.total == 6


@respx.mock
async def test_median_expression_gene_not_found(make_registry):
    respx.get(f"{API}/reference/gene").mock(return_value=httpx.Response(200, json={"data": [], "paging_info": {}}))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "median_expression", {"gene": "NOTAGENE1"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_single_tissue_eqtls_by_rsid_with_lenient_tissue(make_registry):
    variant = respx.get(f"{API}/dataset/variant").mock(return_value=_ok("variant_rs38719.json"))
    respx.get(f"{API}/dataset/tissueSiteDetail").mock(return_value=_ok("tissues.json"))
    eqtl = respx.get(f"{API}/association/singleTissueEqtl").mock(return_value=_ok("eqtl_variant.json"))
    registry = make_registry("gtex")
    result = await registry.call(
        "gtex", "single_tissue_eqtls", {"variant": "rs38719", "tissue": "nerve - tibial", "limit": 5}
    )
    assert variant.calls.last.request.url.params["snpId"] == "rs38719"
    sent = eqtl.calls.last.request.url.params
    assert sent["variantId"] == "chr7_140994094_G_T_b38"
    assert sent["tissueSiteDetailId"] == "Nerve_Tibial"
    assert sent["itemsPerPage"] == "10000"
    record = result.records[0]
    assert record.type == "association"
    assert record.identifiers == {"rsid": "rs38719", "gnomad_variant": "7-140994094-G-T", "gene_symbol": "MRPS33"}
    assert "ensembl_gene" not in record.identifiers  # the gene lives in attributes
    assert record.attributes["ensembl_gene"] == "ENSG00000090263"
    assert record.attributes["nes"] < 0 and record.attributes["p_value"] < 1e-10
    assert record.title.startswith("rs38719 eQTL for MRPS33 in Nerve_Tibial")
    assert result.data["by_tissue"][0]["tissue"] == "Nerve_Tibial"


@respx.mock
async def test_single_tissue_eqtls_by_gene_sorted_and_paged(make_registry):
    respx.get(f"{API}/reference/gene").mock(return_value=_ok("gene_pcsk9.json"))
    respx.get(f"{API}/dataset/tissueSiteDetail").mock(return_value=_ok("tissues.json"))
    eqtl = respx.get(f"{API}/association/singleTissueEqtl").mock(return_value=_ok("eqtl_gene.json"))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "single_tissue_eqtls", {"gene": "PCSK9", "tissue": "liver", "limit": 2})
    sent = eqtl.calls.last.request.url.params
    assert sent["gencodeId"] == "ENSG00000169174.11" and sent["tissueSiteDetailId"] == "Liver"
    pvals = [r.attributes["p_value"] for r in result.records]
    assert pvals == sorted(pvals) and result.records[0].identifiers["rsid"] == "rs1165287"
    assert result.total == 4 and result.next_cursor == "2"
    page2 = await registry.call(
        "gtex", "single_tissue_eqtls", {"gene": "PCSK9", "tissue": "liver", "limit": 2, "cursor": "2"}
    )
    assert len(page2.records) == 2 and page2.next_cursor is None
    assert page2.records[0].attributes["p_value"] >= pvals[-1]


@respx.mock
async def test_single_tissue_eqtls_input_validation(make_registry):
    respx.get(f"{API}/dataset/tissueSiteDetail").mock(return_value=_ok("tissues.json"))
    respx.get(f"{API}/association/singleTissueEqtl").mock(
        return_value=httpx.Response(200, json={"data": [], "paging_info": {"totalNumberOfItems": 0}})
    )
    registry = make_registry("gtex")
    with pytest.raises(SourceError, match="gene and/or variant"):
        await registry.call("gtex", "single_tissue_eqtls", {})
    with pytest.raises(SourceError, match="ambiguous GTEx tissue"):
        await registry.call("gtex", "single_tissue_eqtls", {"variant": "7-140994094-G-T", "tissue": "brain"})
    empty = await registry.call("gtex", "single_tissue_eqtls", {"variant": "chr7:140994094:G:T"})
    assert empty.records == [] and "no significant eQTLs" in empty.notes[-1]


@respx.mock
async def test_tissues(make_registry):
    route = respx.get(f"{API}/dataset/tissueSiteDetail").mock(return_value=_ok("tissues.json"))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "tissues", {"dataset": "gtex_v10"})
    assert route.calls.last.request.url.params["datasetId"] == "gtex_v10"
    liver = next(t for t in result.data["tissues"] if t["tissue"] == "Liver")
    assert liver["uberon"] == "UBERON:0001114" and liver["rnaseq_samples"] > 100
    assert result.total == len(result.data["tissues"])


@respx.mock
async def test_top_expressed_genes(make_registry):
    respx.get(f"{API}/dataset/tissueSiteDetail").mock(return_value=_ok("tissues.json"))
    route = respx.get(f"{API}/expression/topExpressedGene").mock(return_value=_ok("top_liver.json"))
    registry = make_registry("gtex")
    result = await registry.call("gtex", "top_expressed_genes", {"tissue": "LIVER", "limit": 3})
    sent = route.calls.last.request.url.params
    assert sent["tissueSiteDetailId"] == "Liver" and sent["filterMtGene"] == "true"
    assert [r.identifiers["gene_symbol"] for r in result.records] == ["ALB", "HP", "ORM1"]
    assert result.records[0].attributes["rank"] == 1 and result.records[0].identifiers["ensembl_gene"] == "ENSG00000163631"
    assert result.total == 58996 and result.next_cursor == "1"


def test_gtex_to_gnomad_variant_ids():
    assert gtex_to_gnomad("chr1_55055322_C_CG_b38") == "1-55055322-C-CG"
    assert gtex_to_gnomad("chrX_100_A_T_b38") == "X-100-A-T"
    assert gtex_to_gnomad("rs123") is None


@respx.mock
async def test_unknown_dataset_rejected_upstream_is_source_error(make_registry):
    respx.get(f"{API}/reference/geneSearch").mock(
        return_value=httpx.Response(422, json={"detail": [{"loc": ["query", "geneId"], "msg": "bad input"}]})
    )
    registry = make_registry("gtex")
    with pytest.raises(SourceError, match="GTEx rejected"):
        await registry.call("gtex", "search_genes", {"query": "BRAF"})


@pytest.mark.live
async def test_live_gtex_all_tools(live_registry):
    registry = live_registry("gtex")
    search = await registry.call("gtex", "search_genes", {"query": "PCSK9", "limit": 3})
    assert search.records[0].identifiers["ensembl_gene"] == "ENSG00000169174"
    median = await registry.call("gtex", "median_expression", {"gene": "PCSK9"})
    assert median.data["tissues"][0]["tissue"] == "Liver"
    eqtls = await registry.call("gtex", "single_tissue_eqtls", {"variant": "rs38719", "limit": 3})
    assert eqtls.records and eqtls.records[0].identifiers["rsid"] == "rs38719"
    tissues = await registry.call("gtex", "tissues", {})
    assert tissues.total >= 50
    top = await registry.call("gtex", "top_expressed_genes", {"tissue": "Liver", "limit": 3})
    assert top.records[0].identifiers["gene_symbol"] == "ALB"
