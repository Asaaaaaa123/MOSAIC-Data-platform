"""Curation tests: cleaning, evaluation flags/scores, consolidation, registry integration."""

from __future__ import annotations

import json

import httpx
import respx

from mosaic.curation import CleaningLog, clean_record, clean_text, curate, evaluate, record_flags
from mosaic.curation.clean import clean_date, clean_value
from mosaic.models import Record
from tests.conftest import load_fixture


def rec(source, rid, type="article", **kw) -> Record:
    return Record(source=source, id=rid, type=type, **kw)


# ---------------------------------------------------------------- cleaning
def test_clean_text_strips_markup_but_keeps_math():
    assert clean_text("<jats:p>Most <i>BRAF</i>&nbsp;variants</jats:p>") == "Most BRAF variants"
    assert clean_text("IC50<IC90 and EC50>2 nM") == "IC50<IC90 and EC50>2 nM"
    assert clean_text("p < 0.05") == "p < 0.05"
    assert clean_text("  N/A ") is None
    assert clean_text("Not available") is None


def test_clean_date_formats():
    cases = {
        "2024 Mar 5": "2024-03-05", "2024/3/5": "2024-03-05", "2024-03-05T10:00:00Z": "2024-03-05",
        "Mar 2024": "2024-03", "5 March 2024": "2024-03-05", "2019": "2019", "unknown": None,
    }
    for raw, expected in cases.items():
        assert clean_date(raw) == expected, raw


def test_clean_value_keeps_symbols_and_row_shape():
    log = CleaningLog()
    cleaned = clean_value(
        {"strand": "-", "a": "N/A", "tags": ["x", "X", "", "y"], "rows": [{"id": "1", "resolution": None}]}, log
    )
    assert cleaned == {"strand": "-", "tags": ["x", "y"], "rows": [{"id": "1", "resolution": None}]}
    assert log.counts["duplicate_removed"] == 1


def test_clean_record_dedupes_authors_and_counts_fixes():
    log = CleaningLog()
    out = clean_record(
        rec("x", "1", title=" <b>Title</b> ", authors=["Ann Lee", "Ann  Lee", "", "Bo Li"], date="2020 Jan 2",
            summary="unknown"),
        log,
    )
    assert out.title == "Title" and out.authors == ["Ann Lee", "Bo Li"] and out.date == "2020-01-02"
    assert out.summary is None
    assert any("authors" in s for s in log.summary())


def test_cleaning_keeps_an_unknown_trial_status_and_its_flag():
    # ClinicalTrials.gov's UNKNOWN: completion date passed and status not verified for 2+ years
    trial = rec("clinicaltrials", "NCT01234567", type="trial", title="A trial",
                identifiers={"nct": "NCT01234567"}, attributes={"status": " UNKNOWN ", "phase": "N/A", "why_stopped": "unknown"})
    out = clean_record(trial)
    assert out.attributes == {"status": "UNKNOWN"}  # other placeholders still go
    assert list(clean_record(rec("x", "1", attributes={"a": 1, "status": "Recruiting", "b": 2})).attributes) == ["a", "status", "b"]
    assert clean_record(rec("x", "2", attributes={"status": "  "})).attributes == {}
    assert "trial status unknown (registry not updated)" in curate([trial]).entities[0].evaluation.flags


# ---------------------------------------------------------------- evaluation
def test_flags():
    assert "retracted" in record_flags(rec("openalex", "W1", attributes={"is_retracted": True}))
    assert "preprint (not peer reviewed)" in record_flags(rec("europepmc", "PPR1", type="preprint"))
    assert "trial terminated" in record_flags(rec("clinicaltrials", "NCT1", type="trial", attributes={"status": "TERMINATED"}))
    assert "access restricted (authorization required)" in record_flags(
        rec("physionet", "p", type="dataset", attributes={"access_policy": "credentialed"}))
    assert record_flags(rec("clinicaltrials", "NCT2", type="trial", attributes={"access_criteria": "controlled by sponsor"})) == []
    assert any("FAERS" in f for f in record_flags(rec("openfda", "e", type="adverse_event")))
    assert "non-human (mus musculus)" in record_flags(rec("uniprot", "P1", type="protein", attributes={"organism": "Mus musculus"}))
    assert record_flags(rec("uniprot", "P2", type="protein", attributes={"organism": "Homo sapiens", "reviewed": True})) == []
    assert "unreviewed (TrEMBL) entry" in record_flags(rec("uniprot", "P3", type="protein", attributes={"reviewed": False}))


def test_evaluate_rewards_tier_corroboration_and_penalizes_conflicts():
    full = dict(title="T", url="https://x", summary="s", date="2020", identifiers={"doi": "10.1/x", "pmid": "1"},
                attributes={"a": 1, "b": 2, "c": 3}, authors=["A"])
    single = evaluate([rec("zenodo", "1", **full)])
    multi = evaluate([rec("ncbi", "1", **full), rec("crossref", "10.1/x", **full), rec("openalex", "W", **full)])
    assert multi.score > single.score
    assert multi.grade == "A" and multi.corroborated_by == ["crossref", "ncbi", "openalex"]
    conflicted = evaluate([rec("ncbi", "1", **full)], conflicts={"doi": [], "year": []})
    assert conflicted.score < evaluate([rec("ncbi", "1", **full)]).score
    retracted = evaluate([rec("openalex", "W", attributes={"is_retracted": True}, **{k: v for k, v in full.items() if k != "attributes"})])
    assert retracted.score <= 20 and retracted.grade == "D"
    assert all(isinstance(r, str) for r in multi.reasons)


# ---------------------------------------------------------------- consolidation
def test_curate_merges_by_identifier_with_provenance_and_conflicts():
    records = [
        rec("crossref", "10.1/abc", title="Base editing in vivo", date="2020-02-01",
            identifiers={"doi": "10.1/ABC"}, attributes={"journal": "Nature", "cited_by": 10}),
        rec("ncbi", "123", title="Base editing in vivo.", date="2020-01-15", authors=["Ann Lee"],
            identifiers={"doi": "10.1/abc", "pmid": "123"}, attributes={"journal": "Nature", "mesh": ["Gene Editing"]},
            summary="An abstract."),
        rec("openalex", "W9", title="Base editing in vivo", date="2020",
            identifiers={"pmid": "123", "openalex": "W9", "mag": "1"}, attributes={"cited_by": 12}),
        rec("zenodo", "55", type="dataset", title="Unrelated dataset", identifiers={"doi": "10.5281/zenodo.55"}),
    ]
    result = curate(records)
    assert result.stats["records_in"] == 4 and result.stats["entities_out"] == 2
    paper = result.entities[0]
    assert sorted(s["source"] for s in paper.sources) == ["crossref", "ncbi", "openalex"]
    assert paper.identifiers == {"doi": "10.1/abc", "pmid": "123", "openalex": "W9", "mag": "1"}
    assert paper.date == "2020-01-15"  # earliest year across sources, most precise date within it
    assert paper.title == "Base editing in vivo."  # NCBI is the reference source for articles
    assert paper.field_sources["journal"] == "ncbi" and paper.fields["cited_by"] == 10
    assert paper.summary == "An abstract." and paper.authors == ["Ann Lee"]
    assert paper.evaluation.grade in {"A", "B"} and len(paper.evaluation.corroborated_by) == 3
    assert not paper.conflicts


def test_curate_records_identifier_conflict_but_not_for_multi_valued_ids():
    result = curate([
        rec("chembl", "CHEMBL1", type="drug", title="imatinib",
            identifiers={"chembl": "CHEMBL1", "inchikey": "KTUFNOKKBVMGRW-UHFFFAOYSA-N", "rxcui": "282388"},
            attributes={"max_phase": 4, "match": "exact"}),
        rec("drugcentral", "1423", type="drug", title="imatinib",
            identifiers={"chembl": "CHEMBL1", "inchikey": "YLMAHDNUQAMNNX-UHFFFAOYSA-N", "rxcui": "282386"}),
    ])
    entity = result.entities[0]
    assert set(entity.conflicts) == {"inchikey"}  # ingredient vs brand RxCUI is not a conflict
    assert any("sources disagree on: inchikey" in f for f in entity.evaluation.flags)
    assert entity.identifiers["rxcui"] == "282388"  # ChEMBL is the reference source for drugs
    assert entity.fields == {"max_phase": 4}  # search bookkeeping ("match") is not data


def test_curate_merges_same_title_without_shared_ids_but_not_across_years():
    title = "A very specific dataset title about melanoma single-cell RNA-seq"
    result = curate([
        rec("figshare", "1", type="dataset", title=title, date="2021", identifiers={"figshare": "1"}),
        rec("zenodo", "2", type="dataset", title=title + ".", date="2022", identifiers={"zenodo": "2"}),
        rec("osf", "3", type="dataset", title=title, date="2015", identifiers={"osf": "3"}),
    ])
    assert result.stats["entities_out"] == 2
    assert sorted(s["source"] for s in result.entities[0].sources) == ["figshare", "zenodo"]


def test_curate_keeps_relationship_records_that_share_a_counterpart_apart():
    # relationship records carry the counterpart entity's IDs, which sibling rows of one result share
    activities = [
        rec("chembl", aid, type="activity", title=f"IMATINIB IC50 {value} nM vs ABL1",
            identifiers={"chembl": "CHEMBL1862"}, attributes={"standard_value": value})
        for aid, value in (("101", 25), ("102", 38), ("103", 600))
    ]
    associations = [  # GWAS association ids can be empty
        rec("gwas_catalog", "", type="association", title=f"rs334 - {trait}", identifiers={"rsid": "rs334"},
            attributes={"trait": trait})
        for trait in ("malaria", "sickle cell anaemia")
    ]
    target = rec("chembl", "CHEMBL1862", type="target", title="Tyrosine-protein kinase ABL1",
                 identifiers={"chembl": "CHEMBL1862", "uniprot": "P00519"})
    protein = rec("uniprot", "P00519", type="protein", title="ABL1", identifiers={"uniprot": "P00519"})
    by_target = rec("chembl", "101", type="activity", title="IMATINIB IC50 25 nM vs ABL1",  # activity 101 fetched by target
                    identifiers={"chembl": "CHEMBL941", "smiles": "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1"},
                    attributes={"standard_value": 25})
    same_id = rec("gwas_catalog", "101", type="association", title="rs7903146 - type 2 diabetes",
                  identifiers={"rsid": "rs7903146"}, attributes={"trait": "type 2 diabetes"})
    result = curate([*activities, *associations, target, protein, by_target, same_id])

    assert result.stats["records_in"] == 9 and result.stats["entities_out"] == 7
    acts = {e.sources[0]["id"]: e for e in result.entities if e.type == "activity"}
    assert [acts[a].fields["standard_value"] for a in ("101", "102", "103")] == [25, 38, 600]
    assert acts["102"].identifiers == {"chembl": "CHEMBL1862"}  # the counterpart's ID is kept as data
    # the same activity fetched from the other side merges, without a false identifier conflict
    assert [s["id"] for s in acts["101"].sources] == ["101", "101"] and not acts["101"].conflicts
    traits = sorted(e.fields["trait"] for e in result.entities if e.type == "association")
    assert traits == ["malaria", "sickle cell anaemia", "type 2 diabetes"]  # gwas_catalog:101 is not chembl:101
    gene = [e for e in result.entities if e.type in ("target", "protein")]
    assert len(gene) == 1 and sorted(s["source"] for s in gene[0].sources) == ["chembl", "uniprot"]


def test_curated_to_llm_is_valid_json_within_budget():
    records = [rec("crossref", f"10.1/{i}", title=f"Paper {i}", summary="x" * 3000, identifiers={"doi": f"10.1/{i}"})
               for i in range(80)]
    text = curate(records).to_llm(max_chars=8000)
    parsed = json.loads(text)
    assert len(text) <= 8000 and parsed["entities"] and parsed["stats"]["entities_out"] == 80


# ---------------------------------------------------------------- registry integration
@respx.mock
async def test_registry_call_cleans_and_grades(make_registry):
    respx.get("https://api.crossref.org/works").mock(
        return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json"))
    )
    registry = make_registry("crossref")
    result = await registry.call("crossref", "search_works", {"query": "q", "limit": 2})
    first = result.records[0]
    assert first.summary == "Most genetic variants that contribute to disease are challenging to correct efficiently."
    assert first.quality["grade"] in {"A", "B", "C", "D"} and "score" in first.quality
    assert "quality" in result.to_llm()
    federated = await registry.federated_search("q", sources=["crossref"])
    assert federated.curated is not None and federated.curated.stats["records_in"] == 2
    llm = json.loads(federated.to_llm())
    assert llm["curated"]["entities"][0]["quality"]["grade"]
