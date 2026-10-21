"""Open Targets Platform connector tests (offline GraphQL mocks + live checks)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.connectors.opentargets import disease_identifiers
from tests.conftest import load_fixture

API = "https://api.platform.opentargets.org/api/v4/graphql"


def fx(name: str):
    return load_fixture("opentargets", name)


def router(mapping: dict[str, str | list[str]]):
    """respx side effect: pick a fixture by GraphQL operation name (lists are consumed in order)."""
    queues = {k: list(v) if isinstance(v, list) else [v] for k, v in mapping.items()}
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        op = body["query"].split("query ", 1)[1].split("(", 1)[0].strip()
        queue = queues[op]
        name = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json=fx(name))

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def test_disease_identifier_mapping():
    ids = disease_identifiers("MONDO_0005233", ["DOID:3908", "EFO:0003060", "MESH:D002289", "OMIM:211980", "Orphanet:488201", "UMLS:C0007131", "MESH:130014"])
    assert ids == {
        "mondo": "MONDO:0005233",
        "doid": "DOID:3908",
        "efo": "EFO_0003060",
        "mesh": "D002289",
        "omim": "211980",
        "orphanet": "488201",
        "umls": "C0007131",
    }
    assert disease_identifiers("EFO_0000311") == {"efo": "EFO_0000311"}
    assert disease_identifiers("Orphanet_558") == {"orphanet": "558"}


@respx.mock
async def test_search_mixed_entities(make_registry):
    handler = router({"Search": "search.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "search", {"query": "EGFR", "limit": 3})
    variables = handler.seen[0]["variables"]
    assert variables == {"q": "EGFR", "entities": ["target", "disease", "drug"], "page": {"index": 0, "size": 3}}
    assert result.total == 2731 and result.next_cursor == "3"
    target, disease = result.records[0], result.records[1]
    assert target.type == "target" and target.identifiers == {
        "ensembl_gene": "ENSG00000146648", "gene_symbol": "EGFR", "uniprot": "P00533"
    }
    assert disease.type == "disease" and disease.identifiers["efo"] == "EFO_0022194"
    assert result.records[2].identifiers["mondo"] == "MONDO:0014481"


@respx.mock
async def test_search_pagination_uses_page_index(make_registry):
    handler = router({"Search": "search.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "search", {"query": "EGFR", "limit": 3, "cursor": "6", "entities": ["target"]})
    assert handler.seen[0]["variables"]["page"] == {"index": 2, "size": 3}
    assert handler.seen[0]["variables"]["entities"] == ["target"]
    assert result.next_cursor == "9"


@respx.mock
async def test_get_target_resolves_symbol(make_registry):
    handler = router({"Search": "search_target_symbol.json", "Target": "target.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "get_target", {"target": "egfr", "max_drugs": 3})
    assert handler.seen[0]["variables"]["entities"] == ["target"]
    assert handler.seen[1]["variables"] == {"id": "ENSG00000146648"}
    assert result.notes == []  # exact symbol match, no resolution caveat
    rec = result.records[0]
    assert rec.identifiers == {
        "ensembl_gene": "ENSG00000146648", "gene_symbol": "EGFR", "uniprot": "P00533",
        "hgnc": "HGNC:3236", "chembl": "CHEMBL203",
    }
    attrs = rec.attributes
    assert attrs["n_drugs_and_candidates"] == 82
    assert [d["max_stage"] for d in attrs["drugs_and_candidates"]] == ["APPROVAL", "APPROVAL", "PHASE_2"]
    assert "SM" in attrs["tractability"] and attrs["pathways"][0]["id"].startswith("R-HSA-")
    assert "EGFR" not in attrs.get("synonyms", [])


@respx.mock
async def test_get_target_not_found(make_registry):
    respx.post(API).mock(side_effect=router({"Target": "target_missing.json"}))
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "get_target", {"target": "ENSG00000000000"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_disease_accepts_curie(make_registry):
    handler = router({"Disease": "disease.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "get_disease", {"disease": "MONDO:0005233"})
    assert handler.seen[0]["variables"] == {"id": "MONDO_0005233"}
    rec = result.records[0]
    assert rec.title == "non-small cell lung carcinoma"
    assert rec.identifiers["mondo"] == "MONDO:0005233" and rec.identifiers["mesh"] == "D002289"
    assert rec.identifiers["efo"] == "EFO_0003060"
    assert rec.attributes["drugs_and_candidates"][0]["max_stage"] == "APPROVAL"
    assert "NSCLC" in rec.attributes["synonyms"]


@respx.mock
async def test_disease_associated_targets(make_registry):
    handler = router({"DiseaseTargets": "disease_targets.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "disease_associated_targets", {"disease": "MONDO_0005233", "limit": 2, "cursor": "2"})
    assert handler.seen[0]["variables"]["page"] == {"index": 1, "size": 2}
    assert result.total == 13555 and result.next_cursor == "4"
    rec = result.records[0]
    assert rec.type == "target" and rec.identifiers["gene_symbol"] == "ALK"
    assert rec.identifiers["ensembl_gene"] == "ENSG00000171094"
    assert rec.attributes["rank"] == 3 and 0 < rec.attributes["overall_score"] <= 1
    assert "clinical" in rec.attributes["datatype_scores"]


@respx.mock
async def test_target_associated_diseases(make_registry):
    handler = router({"TargetDiseases": "target_diseases.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "target_associated_diseases", {"target": "ENSG00000146648", "limit": 2})
    assert handler.seen[0]["variables"] == {"id": "ENSG00000146648", "page": {"index": 0, "size": 2}}
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.type == "disease" and rec.identifiers["mondo"] == "MONDO:0005233"
    assert rec.attributes["target_symbol"] == "EGFR"


@respx.mock
async def test_get_drug(make_registry):
    handler = router({"Drug": "drug.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call("opentargets", "get_drug", {"chembl_id": "chembl941"})
    assert handler.seen[0]["variables"] == {"id": "CHEMBL941"}
    rec = result.records[0]
    assert rec.identifiers == {"chembl": "CHEMBL941", "drugbank": "DB00619"}
    attrs = rec.attributes
    assert attrs["max_clinical_stage"] == "APPROVAL"
    assert "ABL1" in attrs["linked_targets"]
    assert attrs["indications"][0]["max_stage"] == "APPROVAL" and attrs["indications"][-1]["max_stage"] == "PHASE_2"
    assert attrs["adverse_events"][0]["event"]


@respx.mock
async def test_association_evidence_clinical(make_registry):
    handler = router({"Evidence": "evidence.json"})
    respx.post(API).mock(side_effect=handler)
    registry = make_registry("opentargets")
    result = await registry.call(
        "opentargets",
        "association_evidence",
        {"target": "ENSG00000146648", "disease": "MONDO_0005233", "datasources": ["clinical_precedence"], "limit": 2},
    )
    variables = handler.seen[0]["variables"]
    assert variables["ds"] == ["clinical_precedence"] and variables["size"] == 2 and variables["cursor"] is None
    assert result.total and result.next_cursor
    assert result.data["datasource_scores"]["clinical_precedence"] > 0.9
    first, second = result.records
    assert first.identifiers == {"pmid": "35343187"}
    assert first.attributes["drug_name"] == "OSIMERTINIB" and first.attributes["clinical_stage"] == "PHASE_4"
    assert second.identifiers == {}  # EMA report id, no PMID/NCT
    assert second.attributes["clinical_stage"] == "APPROVAL"


@respx.mock
async def test_association_evidence_literature_sentence(make_registry):
    respx.post(API).mock(side_effect=router({"Evidence": "evidence_literature.json"}))
    registry = make_registry("opentargets")
    result = await registry.call(
        "opentargets", "association_evidence",
        {"target": "ENSG00000146648", "disease": "MONDO_0005233", "datasources": ["europepmc"], "limit": 1},
    )
    rec = result.records[0]
    assert rec.identifiers == {"pmid": "21645456", "pmcid": "PMC5999884"}
    assert rec.date == "2011"
    assert rec.summary.startswith("The aim of this study")  # non-English sentences skipped


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_search_target_disease(live_registry):
    registry = live_registry("opentargets")
    found = await registry.call("opentargets", "search", {"query": "EGFR", "limit": 3})
    assert found.records[0].identifiers["ensembl_gene"] == "ENSG00000146648"
    target = await registry.call("opentargets", "get_target", {"target": "ENSG00000146648", "max_drugs": 5})
    assert target.records[0].identifiers["uniprot"] == "P00533"
    disease = await registry.call("opentargets", "get_disease", {"disease": "MONDO_0005233"})
    assert disease.records[0].identifiers["mesh"] == "D002289"


@pytest.mark.live
async def test_live_associations_drug_evidence(live_registry):
    registry = live_registry("opentargets")
    targets = await registry.call("opentargets", "disease_associated_targets", {"disease": "MONDO_0005233", "limit": 5})
    assert any(r.identifiers.get("gene_symbol") == "EGFR" for r in targets.records)
    diseases = await registry.call("opentargets", "target_associated_diseases", {"target": "EGFR", "limit": 5})
    assert diseases.records and diseases.total
    drug = await registry.call("opentargets", "get_drug", {"chembl_id": "CHEMBL941"})
    assert "ABL1" in drug.records[0].attributes["linked_targets"]
    ev = await registry.call(
        "opentargets", "association_evidence",
        {"target": "EGFR", "disease": "MONDO_0005233", "datasources": ["clinical_precedence"], "limit": 3},
    )
    assert ev.records and ev.data["overall_score"] > 0.5
