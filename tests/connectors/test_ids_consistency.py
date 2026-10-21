"""Cross-connector identifier consistency: the same real-world id must normalize to one form."""

from __future__ import annotations

import pytest

from mosaic.connectors.datacite import repository_ids
from mosaic.crosslink.merge import cluster_records
from mosaic.ids import normalize_identifier
from mosaic.models import Record


@pytest.mark.parametrize(
    ("key", "variants", "canonical"),
    [
        # Zenodo alternate ids are "arXiv:<id>"; Semantic Scholar reports the bare id
        ("arxiv", ["arXiv:2204.01289", "2204.01289v2", "https://arxiv.org/abs/2204.01289v1", "2204.01289"], "2204.01289"),
        ("arxiv", ["arXiv:hep-th/9901001v3", "hep-th/9901001"], "hep-th/9901001"),
        # dbGaP accessions are cited with and without the version suffix
        ("dbgap", ["phs000178.v11.p8", "PHS000178", "phs000178.v11"], "phs000178"),
        ("doi", ["https://www.doi.org/10.1038/X", "doi.org/10.1038/x", "doi:10.1038/X", "10.1038/x"], "10.1038/x"),
        # Alliance / Monarch emit prefixed model-organism CURIEs; bare ids get the same prefix
        ("zfin", ["ZFIN:ZDB-GENE-040805-1", "zfin:ZDB-GENE-040805-1", "ZDB-GENE-040805-1"], "ZFIN:ZDB-GENE-040805-1"),
        ("mgi", ["MGI:88388", "mgi:88388", "88388"], "MGI:88388"),
        ("rgd", ["RGD:619908", "619908"], "RGD:619908"),
        ("flybase", ["FB:FBgn0003731", "FBgn0003731"], "FB:FBgn0003731"),
        ("wormbase", ["WB:WBGene00000898", "WBGene00000898"], "WB:WBGene00000898"),
        ("sgd", ["SGD:S000001855", "S000001855"], "SGD:S000001855"),
        ("xenbase", ["Xenbase:XB-GENE-865046", "XENBASE:XB-GENE-865046", "XB-GENE-865046"], "Xenbase:XB-GENE-865046"),
    ],
)
def test_variants_normalize_to_one_form(key, variants, canonical):
    assert {normalize_identifier(key, v) for v in variants} == {canonical}


def test_zenodo_and_semantic_scholar_arxiv_link():
    zenodo = Record(source="zenodo", id="1", identifiers={"zenodo": "1", "arxiv": "arXiv:1706.03762"})
    s2 = Record(source="semantic_scholar", id="x", identifiers={"s2": "x", "arxiv": "1706.03762"})
    assert len(cluster_records([zenodo, s2])) == 1


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.18112/openneuro.ds003404.v1.0.8", {"openneuro": "ds003404"}),
        ("10.18112/openneuro.ds003404.v1.0.7", {"openneuro": "ds003404"}),
        ("10.48324/dandi.000235/0.230316.1600", {"dandi": "000235"}),
        ("10.48324/dandi.000235", {"dandi": "000235"}),
        ("10.6084/m9.figshare.34737582.v2", {"figshare": "34737582"}),
    ],
)
def test_datacite_version_dois_map_to_native_ids(doi, expected):
    # OpenNeuro / DANDI mint one DOI per version, so the native id is what links DataCite to them
    assert repository_ids(doi) == expected


def test_ensembl_prefix_version_and_type():
    # Reactome search hits report Ensembl references as "ENSEMBL:ENSG..."
    assert normalize_identifier("ensembl_gene", "ENSEMBL:ENSG00000207702") == "ENSG00000207702"
    assert normalize_identifier("ensembl_gene", "ENSG00000157764.14") == "ENSG00000157764"
    assert normalize_identifier("ensembl_gene", "ENSMUSG00000002413") == "ENSMUSG00000002413"
    # a transcript / protein under ensembl_gene (or vice versa) is dropped, never linked as a gene
    assert normalize_identifier("ensembl_gene", "ENST00000646891") is None
    assert normalize_identifier("ensembl_gene", "ENSP00000496776") is None
    assert normalize_identifier("ensembl_transcript", "ENST00000646891.2") == "ENST00000646891"
    assert normalize_identifier("ensembl_transcript", "ENSG00000157764") is None


def test_reactome_ensembl_hit_links_with_hgnc_gene():
    from mosaic.connectors.reactome import _search_record

    hit = _search_record({"stId": "R-HSA-1", "type": "RNA Sequence", "name": "MIR21 gene",
                          "databaseName": "ENSEMBL", "referenceIdentifier": "ENSEMBL:ENSG00000284190"})
    assert hit.identifiers["ensembl_gene"] == "ENSG00000284190"
    hgnc = Record(source="hgnc", id="HGNC:31586", identifiers={"hgnc": "HGNC:31586", "ensembl_gene": "ENSG00000284190"})
    assert len(cluster_records([hit, hgnc])) == 1
