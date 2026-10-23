"""Visual summary: mind map, charts counted from the data, data checks, Figure Studio figures and the API."""

from __future__ import annotations

import csv
import io

import httpx
import pytest

from mosaic.api import create_app
from mosaic.curation import curate
from mosaic.datapackage import build_package, write_package
from mosaic.models import Record
from mosaic.registry import SourceSearchResult
from mosaic.visuals import QueryRun, build_visuals, short_source_name
from mosaic.visuals.build import clip_words, phase_label, status_label, table_checks
from mosaic.visuals.figures import figure_names, load_engine, reset_engine, safe_text

from .conftest import load_fixture
from .test_api import CROSSREF_WORKS, _client  # noqa: F401


def rec(source, rid, type, **kw) -> Record:
    return Record(source=source, id=rid, type=type, **kw)


A = [
    rec("ncbi", "1", "article", title="Sotorasib in KRAS G12C NSCLC", date="2021-06-24",
        identifiers={"pmid": "34096690", "doi": "10.1056/NEJMoa2103695"}, attributes={"journal": "N Engl J Med", "cited_by": 139}),
    rec("crossref", "10.1056/nejmoa2103695", "article", title="Sotorasib for Lung Cancers with KRAS p.G12C Mutation",
        date="2021", identifiers={"doi": "10.1056/NEJMoa2103695"}, attributes={"journal": "New England Journal of Medicine"}),
    rec("clinicaltrials", "NCT04303780", "trial", title="CodeBreaK 200", date="2020-06-04", identifiers={"nct": "NCT04303780"},
        attributes={"status": "COMPLETED", "phases": ["PHASE3"], "enrollment": 345}),
    rec("clinicaltrials", "NCT03600883", "trial", title="CodeBreaK 100", date="2018-08-27", identifiers={"nct": "NCT03600883"},
        attributes={"status": "ACTIVE_NOT_RECRUITING", "phases": ["PHASE1", "PHASE2"], "enrollment": 713}),
    rec("chembl", "CHEMBL4535757", "drug", title="SOTORASIB", date="2021", identifiers={"chembl": "CHEMBL4535757"},
        attributes={"max_phase": 4, "first_approval": 2021}),
    rec("opentargets", "MONDO_0005233", "disease", title="non-small cell lung carcinoma", identifiers={"mondo": "MONDO:0005233"},
        attributes={"entity": "disease", "title": "NSCLC (source title field)"}),
]
B = [
    rec("clinicaltrials", "NCT03600883", "trial", title="CodeBreaK 100", date="2018-08-27", identifiers={"nct": "NCT03600883"},
        attributes={"status": "ACTIVE_NOT_RECRUITING", "phases": ["PHASE1", "PHASE2"], "enrollment": 713}),
    rec("chembl", "CHEMBL4594350", "drug", title="ADAGRASIB", date="2022", identifiers={"chembl": "CHEMBL4594350"},
        attributes={"max_phase": 4, "first_approval": 2022}),
    rec("europepmc", "PMC1", "article", title="Adagrasib in KRAS G12C NSCLC", date="2022-07-14", identifiers={"pmcid": "PMC1"}),
]


def statuses(records):
    counts = {}
    for r in records:
        counts[r.source] = counts.get(r.source, 0) + 1
    return [SourceSearchResult(source=s, ok=True, returned=n) for s, n in counts.items()]


def single():
    entities = curate(A).entities
    return entities, build_visuals([QueryRun("sotorasib", A, statuses(A), 5)], entities)


def compared():
    union = A + [r for r in B if (r.source, r.id) not in {(x.source, x.id) for x in A}]
    entities = curate(union).entities
    runs = [QueryRun("sotorasib", A, statuses(A), 5), QueryRun("adagrasib", B, statuses(B), 5)]
    return entities, build_visuals(runs, entities)


def chart(visuals, cid):
    return next(c for c in visuals.charts if c.id == cid)


def value(c, category, series):
    return c.series[[s.name for s in c.series].index(series)].values[c.categories.index(category)]


# ---------------------------------------------------------------- counting
def test_single_query_charts_count_the_data_and_checks_pass():
    entities, v = single()
    assert v.checks and all(c.ok for c in v.checks), [c for c in v.checks if not c.ok]
    by_type = chart(v, "entities_by_type")
    assert sum(by_type.series[0].values) == len(entities) == 5  # the two DOI-sharing papers are one entity
    assert value(by_type, "Papers", "sotorasib") == 1 and value(by_type, "Clinical trials", "sotorasib") == 2
    papers = by_type.members["Papers|sotorasib"]
    assert len(papers) == 1 and papers[0] in {e.entity_id for e in entities if e.type == "article"}
    records = chart(v, "records_by_source")
    assert sum(records.series[0].values) == len(A)
    assert sorted(records.members["ClinicalTrials.gov|sotorasib"]) == ["clinicaltrials:NCT03600883", "clinicaltrials:NCT04303780"]
    phases = chart(v, "trial_phases")
    assert value(phases, "Phase 3", "sotorasib") == 1 and value(phases, "Phase 1/2", "sotorasib") == 1
    grades = chart(v, "quality_grades")
    assert sum(sum(s.values) for s in grades.series) == len(entities)
    rows = v.figure_rows()
    assert rows and all(r["definition"] for r in rows)


def test_mind_map_branches_hold_the_best_entities_with_facts():
    entities, v = single()
    root = v.mindmap
    assert root.kind == "root" and root.count == len(entities)
    groups = {g.group: g for g in root.children}
    trials = groups["clinical_trials"]
    assert trials.count == 2 and all(leaf.kind == "entity" for leaf in trials.children)
    facts = {leaf.label: dict(leaf.facts) for leaf in trials.children}
    assert facts["CodeBreaK 200"]["Phase"] == "Phase 3" and facts["CodeBreaK 200"]["Enrollment"] == "345"
    assert next(leaf.detail for leaf in trials.children if leaf.label == "CodeBreaK 100") == "2018 · Phase 1/2 · Active not recruiting"


def test_compare_two_queries_overlap_and_per_query_counts():
    entities, v = compared()
    assert v.compare and v.queries == ["sotorasib", "adagrasib"]
    assert all(c.ok for c in v.checks), [c for c in v.checks if not c.ok]
    codebreak = next(e.entity_id for e in entities if e.title == "CodeBreaK 100")
    assert v.entity_queries[codebreak] == ["sotorasib", "adagrasib"]
    overlap = chart(v, "overlap")
    assert value(overlap, "Found by 2+ queries", "Entities") == 1
    assert value(overlap, "Only adagrasib", "Entities") == 2
    by_type = chart(v, "entities_by_type")
    assert value(by_type, "Clinical trials", "adagrasib") == 1 and value(by_type, "Clinical trials", "sotorasib") == 2
    kinds = [c.kind for c in v.mindmap.children]
    assert kinds == ["query", "query", "shared"]
    assert v.mindmap.children[2].children[0].entity == codebreak


def test_checks_catch_numbers_that_do_not_add_up():
    entities, v = single()
    by_type = chart(v, "entities_by_type")
    by_type.series[0].values[0] += 1  # tamper with a plotted value
    from mosaic.visuals.build import _checks

    checks = _checks([QueryRun("sotorasib", A, statuses(A), 5)], entities, v.entity_queries,
                     {c.id: c for c in v.charts}, v.mindmap)
    assert not next(c for c in checks if c.name.startswith("Entities by kind")).ok


def test_labels():
    assert phase_label(["PHASE2", "PHASE3"]) == "Phase 2/3"
    assert phase_label("EARLY_PHASE1") == "Early phase 1"
    assert phase_label(["NA"]) == "Not applicable" and phase_label(None) == "Not stated"
    assert status_label("ACTIVE_NOT_RECRUITING") == "Active not recruiting"
    assert short_source_name("Comparative Toxicogenomics Database (CTD)", "x") == "CTD"
    assert short_source_name("UniProt (UniProtKB)", "uniprot") == "UniProt"
    assert short_source_name(None, "alliance") == "Alliance (AGR)"
    assert clip_words("Dose-Dependent Intracranial Response to Sotorasib", 30) == "Dose-Dependent Intracranial…"
    assert safe_text("Cost $5 ^2") == r"Cost \$5 ˆ2"  # never mathtext in figuregen


# ---------------------------------------------------------------- packages
def test_source_field_named_like_a_mosaic_column_does_not_overwrite_it(tmp_path):
    """Open Targets returns a field called 'entity': it must not replace the entity number."""
    entities, v = single()
    package = build_package(A, "type")
    disease = next(t for t in package.tables if t.key == "diseases_phenotypes")
    assert disease.columns.count("entity") == 1 and "field_entity" in disease.columns and "field_title" in disease.columns
    row = disease.rows[0]
    assert row["entity"].startswith("e") and row["field_entity"] == "disease"
    assert row["title"] == "non-small cell lung carcinoma"
    v2 = build_visuals([QueryRun("sotorasib", A, statuses(A), 5)], package.entities)
    assert all(c.ok for c in table_checks(package.tables, "type", v2))


def test_package_has_figure_data_mind_map_and_checks(tmp_path):
    package = build_package(A, "type")
    v = build_visuals([QueryRun("sotorasib", A, statuses(A), 5)], package.entities)
    v.checks += table_checks(package.tables, "type", v)
    fake_figures = {"figures/overview.svg": b"<svg/>", "figures/overview_legend.txt": b"legend"}
    saved = write_package(tmp_path, package, name="vis", query="sotorasib", visuals=v, figure_files=fake_figures)
    names = {f["file"] for f in saved.files}
    assert "figure_data.csv" in names and (saved.folder / "mindmap.json").is_file()
    assert (saved.folder / "figures" / "overview.svg").read_bytes() == b"<svg/>"
    rows = list(csv.DictReader(io.StringIO((saved.folder / "figure_data.csv").read_text(encoding="utf-8-sig"))))
    assert {r["chart"] for r in rows} >= {"entities_by_type", "records_by_source"}
    readme = (saved.folder / "README.txt").read_text(encoding="utf-8-sig")
    assert f"Data checks: {len(v.checks)} of {len(v.checks)} passed" in readme and "figures/" in readme
    assert saved.n_rows == len(package.entities)  # figure_data is not data rows


# ---------------------------------------------------------------- Figure Studio
def test_figure_studio_missing_is_reported_not_fatal(tmp_path):
    reset_engine()
    engine, error = load_engine(str(tmp_path / "nowhere"))
    assert engine is None and "MOSAIC_FIGUREGEN_PATH" in error
    reset_engine()


def test_figure_studio_draws_every_figure():
    reset_engine()
    engine, error = load_engine(None)
    if engine is None:
        pytest.skip(f"Figure Studio not installed next to Mosaic: {error}")
    from mosaic.visuals.figures import render

    _, v = compared()
    for name in figure_names(v):
        svg = render(engine, v, name, "svg")
        assert svg.lstrip().startswith(b"<?xml") and b"<svg" in svg, name
    assert render(engine, v, "mindmap", "png")[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- API
@pytest.fixture
async def vis_client(offline_settings, make_registry, tmp_path):
    offline_settings.mosaic_figuregen_path = str(tmp_path / "no-figure-studio")
    reset_engine()
    registry = make_registry("crossref")
    app = create_app(offline_settings, registry=registry, agent=None, load_agent_on_startup=False)
    async with _client(app) as c:
        yield c
    reset_engine()


async def test_search_returns_visuals_and_compare(vis_client, respx_mock):
    respx_mock.get(CROSSREF_WORKS).mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
    res = await vis_client.post("/api/search", json={"query": "prime editing", "compare": ["base editing", "Prime Editing", " "]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["queries"] == ["prime editing", "base editing"]  # blanks and case-insensitive repeats dropped
    assert {s["query"] for s in body["sources"]} == {"prime editing", "base editing"}
    vis = body["visuals"]
    assert vis["compare"] and all(c["ok"] for c in vis["checks"])
    assert body["figures"]["available"] is False and "MOSAIC_FIGUREGEN_PATH" in body["figures"]["error"]
    sid = body["search_id"]
    data = await vis_client.get(f"/api/search/{sid}/figure_data.csv")
    assert data.status_code == 200 and data.text.lstrip("﻿").startswith("chart,chart_title,category")
    assert (await vis_client.get(f"/api/search/{sid}/mindmap.json")).json()["queries"] == body["queries"]
    figure = await vis_client.get(f"/api/search/{sid}/figures/overview.svg")
    assert figure.status_code == 503 and figure.json()["kind"] == "figures_unavailable"


@pytest.mark.parametrize("path", [
    "/api/search/{sid}/figures/../../secret.svg",
    "/api/search/{sid}/figures/nope.svg",
    "/api/search/{sid}/figures/overview.exe",
    "/api/search/" + "0" * 32 + "/figures/overview.svg",
    "/api/search/not-an-id/figure_data.csv",
])
async def test_search_figure_paths_are_validated(vis_client, respx_mock, path):
    respx_mock.get(CROSSREF_WORKS).mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
    sid = (await vis_client.post("/api/search", json={"query": "prime editing"})).json()["search_id"]
    assert (await vis_client.get(path.format(sid=sid))).status_code in (400, 404)
