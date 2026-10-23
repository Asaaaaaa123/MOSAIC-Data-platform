"""Body map: medical words and GTEx tissues -> organs, evidence levels, checks, figures, package and API."""

from __future__ import annotations

import csv
import io
import json

import pytest

from mosaic.api import create_app
from mosaic.body.anatomy import ORGANS, ORGANS_BY_KEY, gtex_organ, layout_json, organs_for_text
from mosaic.body.engine import _checks, _drop_broad, _expression, _names_treated_disease, _organs, build_body_map
from mosaic.body.models import BodyMap, Evidence, KeyFact
from mosaic.body.package import write_body_package
from mosaic.models import ToolResult
from mosaic.visuals.figures import load_engine, reset_engine

from .test_api import _client  # noqa: F401


def organs(text: str) -> set[str]:
    return {m.organ for m in organs_for_text(text)}


# ---------------------------------------------------------------- words -> organs
@pytest.mark.parametrize("text,expected", [
    ("Carcinoma, Non-Small-Cell Lung", {"lung"}),
    ("colorectal cancer", {"colon"}),
    ("HEPATOTOXICITY", {"liver"}),
    ("Alanine aminotransferase increased", {"liver"}),
    ("DIARRHOEA", {"small_intestine", "colon"}),
    ("Haematuria", {"kidney"}),                       # not blood
    ("Interstitial Lung Disease (ILD)/Pneumonitis", {"lung"}),
    ("Type II diabetes mellitus", {"pancreas"}),
    ("gastrointestinal stromal tumor", {"stomach", "small_intestine", "colon"}),
    ("dermatofibrosarcoma protuberans", {"skin"}),
    ("ovarian carcinoma", {"ovary"}),
    ("Retinopathy", {"eye"}),
    ("Hypertension", {"blood"}),
    ("cardiovascular disease", {"heart", "blood"}),
    ("Neoplasms", set()),
    ("Fatigue", set()),
    ("computer", set()),                              # 'uter' inside a word is not the uterus
    ("isotretinoin", set()),                          # nor is 'retin' the retina
    ("Road traffic crash", set()),                    # nor 'rash' the skin
])
def test_medical_words_map_to_organs(text, expected):
    assert organs(text) == expected


def test_matched_words_are_whole_words():
    match = {m.organ: m.words for m in organs_for_text("cardiovascular disease")}
    assert match["heart"] == "cardiovascular"


def test_every_gtex_tissue_and_organ_is_known():
    for tissue in ("Liver", "Lung", "Brain_Cortex", "Whole_Blood", "Colon_Transverse", "Skin_Sun_Exposed_Lower_leg"):
        assert gtex_organ(tissue) in ORGANS_BY_KEY
    assert gtex_organ("Cells_Cultured_fibroblasts") is None  # cell lines are not organs
    layout = layout_json()
    assert {o["key"] for o in layout["organs"]} == {o.key for o in ORGANS}
    assert len(layout["outline"]) > 50 and layout["head"]


# ---------------------------------------------------------------- evidence -> organs
def ev(kind, organ, text, matched, weight=1.0, value=None, **kw):
    return Evidence(kind=kind, organ=organ, text=text, matched=matched, weight=weight, value=value, source="test",
                    record_id="X1", **kw)


def test_levels_follow_the_strongest_evidence_and_weak_faers_is_minor():
    items = [
        ev("treats", "lung", "non-small cell lung carcinoma (approved)", "lung", 5.0, 4.0),
        ev("adverse_event", "lung", "Pneumonitis: 30 of 1000 reports (3.0%)", "Pneumonitis", 3.0, 30.0),
        ev("adverse_event", "liver", "Hepatotoxicity: 40 of 1000 reports (4.0%)", "Hepatotoxicity", 4.0, 40.0),
        ev("adverse_event", "skin", "Rash: 5 of 1000 reports (0.5%)", "Rash", 0.5, 5.0),
        ev("studied", "colon", "colorectal cancer (phase 3)", "colorectal", 3.0, 3.0),
        ev("expression", "nerve", "KRAS (target) 24.5 TPM in Nerve Tibial, 2.4x its median", "Nerve_Tibial", 1.0, 24.5),
    ]
    findings, minor = _organs(items)
    level = {f.key: f.level for f in findings}
    assert level == {"lung": "primary", "liver": "safety", "colon": "investigational", "nerve": "expression"}
    assert [f.key for f in findings][0] == "lung"
    assert [e.organ for e in minor] == ["skin"]  # 0.5% of reports, nothing else: not highlighted
    checks = _checks(items, findings, {"faers_total": 1000, "gtex": {("X1", "Nerve_Tibial"): 24.5}})
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_checks_catch_an_organ_the_words_do_not_name():
    items = [ev("treats", "heart", "non-small cell lung carcinoma (approved)", "lung", 5.0, 4.0)]
    findings, _ = _organs(items)
    bad = next(c for c in _checks(items, findings, {}) if "own words" in c.name)
    assert not bad.ok and "heart" in bad.detail


def test_checks_catch_a_wrong_faers_share():
    items = [ev("adverse_event", "liver", "Hepatotoxicity: 40 of 1000 reports (4.0%)", "Hepatotoxicity", 9.0, 40.0)]
    findings, _ = _organs(items)
    check = next(c for c in _checks(items, findings, {"faers_total": 1000}) if c.name.startswith("FAERS"))
    assert not check.ok


@pytest.mark.parametrize("term,treated,expected", [
    ("Lymphoma", ["kidney transplant rejection"], False),  # treats no cancer: a second cancer is an adverse event
    ("Lymphoma", [], False),
    ("Death", [], True),
    ("Disease progression", [], True),
    ("Chronic myeloid leukaemia", ["chronic myelogenous leukemia"], True),
    ("Breast cancer", ["chronic myelogenous leukemia"], False),  # not the cancer it treats
    ("Neoplasm malignant", ["chronic myelogenous leukemia"], True),  # names no organ
    ("Hepatocellular carcinoma", ["solid tumour"], True),  # the indication names no organ
    ("Nephrotoxicity", ["renal cell carcinoma"], False),  # not a cancer term
])
def test_faers_terms_naming_the_treated_cancer(term, treated, expected):
    assert _names_treated_disease(term, treated) is expected


async def test_faers_cancer_reports_count_unless_the_drug_treats_that_cancer():
    from mosaic.models import Record

    from .test_species_engine import FakeRegistry

    def registry(indication, mesh=None):
        drug = Record(source="chembl", id="CHEMBL1", type="drug", title="TESTDRUG", identifiers={"chembl": "CHEMBL1"})
        rows = [("Lymphoma", 80), ("Chronic myeloid leukaemia", 60), ("Lung neoplasm malignant", 40), ("Death", 20),
                ("Nephrotoxicity", 50)]
        return FakeRegistry({
            ("chembl", "get_molecule"): lambda p: ToolResult(source="chembl", tool="get_molecule", records=[drug]),
            ("chembl", "get_mechanisms"): lambda p: ToolResult(source="chembl", tool="get_mechanisms"),
            ("chembl", "get_drug_indications"): lambda p: ToolResult(source="chembl", tool="get_drug_indications", records=[
                Record(source="chembl", id="i1", type="disease", title=indication,
                       attributes={"efo_term": indication, "mesh_heading": mesh, "max_phase_for_indication": 4})]),
            ("openfda", "adverse_event_counts"): lambda p: ToolResult(
                source="openfda", tool="adverse_event_counts",
                data={"total_reports": 1000, "rows": [{"term": t, "count": c} for t, c in rows]}),
        })

    def faers_terms(body: BodyMap) -> set[str]:
        items = [e for o in body.organs for e in o.evidence] + body.minor
        return {e.record_id.removeprefix("FAERS:") for e in items if e.kind == "adverse_event"}

    every_term = {"Lymphoma", "Chronic myeloid leukaemia", "Lung neoplasm malignant", "Nephrotoxicity"}
    immunosuppressant = await build_body_map(registry("kidney transplant rejection"), "CHEMBL1", kind="drug")
    assert faers_terms(immunosuppressant) == every_term
    leukaemia_drug = await build_body_map(registry("chronic myelogenous leukemia"), "CHEMBL1", kind="drug")
    assert faers_terms(leukaemia_drug) == {"Lung neoplasm malignant", "Nephrotoxicity"}  # blood cancers: the one it treats
    assert any(n.startswith("3 FAERS terms naming the treated disease") for n in leukaemia_drug.notes)
    # the indication's MeSH heading names the cancer when its EFO term does not
    mesothelioma_drug = await build_body_map(registry("pleural mesothelioma", "Mesothelioma, Malignant"), "CHEMBL1", kind="drug")
    assert faers_terms(mesothelioma_drug) == every_term - {"Lung neoplasm malignant"}


def test_whole_system_terms_give_way_to_a_named_organ():
    items = [
        ev("disease", "pancreas", "Disease: type 2 diabetes mellitus", "diabetes", 5.0),
        ev("disease", "stomach", "Parent term: digestive system disorder (MONDO:0004335)", "digestive", 3.0),
        ev("disease", "colon", "Parent term: digestive system disorder (MONDO:0004335)", "digestive", 3.0),
    ]
    kept, dropped = _drop_broad(items)
    assert [e.organ for e in kept] == ["pancreas"] and dropped == 2
    alone, dropped = _drop_broad(items[1:])  # nothing more specific: the system stays
    assert len(alone) == 2 and dropped == 0


def test_expression_uses_tissues_enriched_over_the_median():
    def gtex(values):
        tissues = sorted(({"tissue": t, "median_tpm": v} for t, v in values.items()), key=lambda r: -r["median_tpm"])
        return ToolResult(source="gtex", tool="median_expression", data={"gencode_id": "ENSG1", "tissues": tissues})

    raw: dict = {}
    specific, broad = _expression("PPARG", gtex({"Adipose_Subcutaneous": 60.0, "Breast_Mammary_Tissue": 40.0, "Liver": 3.0,
                                                 "Lung": 4.0, "Heart_Left_Ventricle": 2.0, "Muscle_Skeletal": 1.0}), raw, "gene")
    assert [e.organ for e in specific] == ["adipose", "breast"] and not broad
    assert raw["gtex"][("ENSG1", "Adipose_Subcutaneous")] == 60.0
    everywhere, broad = _expression("MT-ND6", gtex({"Liver": 900.0, "Lung": 850.0, "Heart_Left_Ventricle": 880.0}), {}, "target")
    assert everywhere == [] and broad  # expressed everywhere: says nothing about where


# ---------------------------------------------------------------- figure, package, API
def body_map() -> BodyMap:
    items = [
        ev("treats", "lung", "non-small cell lung carcinoma (approved)", "lung", 5.0, 4.0, url="https://example.org/x"),
        ev("adverse_event", "liver", "Hepatotoxicity: 40 of 1000 reports (4.0%)", "Hepatotoxicity", 4.0, 40.0),
        ev("expression", "nerve", "KRAS (target) 24.5 TPM in Nerve Tibial, 2.4x its median", "Nerve_Tibial", 1.0, 24.5),
    ]
    findings, minor = _organs(items)
    checks = _checks(items, findings, {"faers_total": 1000, "gtex": {("X1", "Nerve_Tibial"): 24.5}})
    return BodyMap(map_id="a" * 32, query="sotorasib", kind="drug", name="SOTORASIB", identifiers={"chembl": "CHEMBL4535757"},
                   generated_at="2026-10-22T00:00:00+00:00", organs=findings, minor=minor, checks=checks,
                   facts=[KeyFact(label="Highest development phase", label_zh="最高研发阶段", value="approved (phase 4)", source="chembl")],
                   sources_used=["chembl", "openfda", "gtex"], notes=["FAERS reports are spontaneous"])


def test_body_package_files(tmp_path):
    saved = write_body_package(tmp_path, body_map(), {"figures/body_map.svg": b"<svg/>", "figures/body_map_legend.txt": b"x"})
    names = {p.name for p in saved.folder.iterdir()}
    assert {"organs.csv", "organ_evidence.csv", "key_facts.csv", "body_map.json", "README.txt", "mosaic_data.xlsx",
            "manifest.json", "mosaic_data.zip"} <= names
    rows = list(csv.DictReader(io.StringIO((saved.folder / "organ_evidence.csv").read_text(encoding="utf-8-sig"))))
    assert {r["organ_key"] for r in rows} == {"lung", "liver", "nerve"} and all(r["matched"] for r in rows)
    manifest = json.loads((saved.folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["figures"][0]["name"] == "body_map" and all(c["ok"] for c in manifest["checks"])
    assert "Data checks: 5 of 5 passed." in (saved.folder / "README.txt").read_text(encoding="utf-8-sig")


def test_figure_studio_draws_the_body_map():
    reset_engine()
    engine, error = load_engine(None)
    if engine is None:
        pytest.skip(f"Figure Studio not installed next to Mosaic: {error}")
    from figuregen import glyphs

    if "human_body" not in glyphs.GLYPHS:
        pytest.skip("this Figure Studio has no anatomy illustrations")
    from mosaic.body.figure import body_scene, figure_names, render

    bm = body_map()
    for name in figure_names(bm):
        assert b"<svg" in render(engine, bm, name, "svg")
    assert render(engine, bm, "body_map", "png")[:4] == b"\x89PNG"
    scene = body_scene(engine, bm)  # labels are drawn with a halo, so they are outlines in the SVG: check the spec
    assert {"Lungs", "Liver", "Peripheral nerves"} <= {t.text for t in scene.texts}
    colours = {it.id: it.color_key for it in scene.items}
    assert colours["lung"] == "vermilion" and colours["liver"] == "gold" and colours["heart"] == ""


@pytest.fixture
async def body_client(offline_settings, make_registry, tmp_path, monkeypatch):
    offline_settings.mosaic_figuregen_path = str(tmp_path / "no-figure-studio")
    reset_engine()

    async def fake_build(registry, query, kind="auto", **_):
        return body_map().model_copy(update={"query": query})

    monkeypatch.setattr("mosaic.body.build_body_map", fake_build)
    app = create_app(offline_settings, registry=make_registry("crossref"), agent=None, load_agent_on_startup=False)
    async with _client(app) as c:
        yield c
    reset_engine()


async def test_body_api_map_figures_and_export(body_client):
    res = await body_client.post("/api/body", json={"query": "sotorasib", "kind": "auto"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["kind"] == "drug" and data["organs"][0]["key"] == "lung" and data["layout"]["organs"]
    assert data["figures"]["available"] is False and data["figures"]["names"] == ["body_map", "organ_evidence"]
    fig = await body_client.get(f"/api/body/{data['map_id']}/figures/body_map.svg")
    assert fig.status_code == 503 and fig.json()["kind"] == "figures_unavailable"
    exp = await body_client.post(f"/api/body/{data['map_id']}/export")
    assert exp.status_code == 200, exp.text
    entry = exp.json()
    assert entry["kind"] == "package" and entry["checks"]["passed"] == entry["checks"]["total"] > 0
    assert {f["file"] for f in entry["files"]} == {"organs.csv", "organ_evidence.csv", "key_facts.csv"}


@pytest.mark.parametrize("path", [
    "/api/body/" + "0" * 32 + "/figures/body_map.svg",
    "/api/body/not-an-id/figures/body_map.svg",
    "/api/body/" + "a" * 32 + "/figures/../secret.svg",
])
async def test_body_paths_are_validated(body_client, path):
    await body_client.post("/api/body", json={"query": "sotorasib"})
    assert (await body_client.get(path)).status_code in (400, 404)


def test_layout_has_whole_body_systems():
    organs = {o["key"]: o for o in layout_json()["organs"]}
    for key, glyph in (("bone", "skeleton"), ("blood", "vascular_system"), ("nerve", "nervous_system")):
        assert organs[key]["glyph"] == glyph and organs[key]["system"] and organs[key]["anchor"]
    assert not organs["liver"]["system"] and organs["kidney"]["mirror"]


def test_single_illustrations_render_in_every_tone():
    reset_engine()
    engine, error = load_engine(None)
    if engine is None or "human_body" not in getattr(engine.glyphs, "GLYPHS", {}):
        pytest.skip(f"Figure Studio with anatomy not installed next to Mosaic: {error}")
    from mosaic.body.art import ART_NAMES, TONE_NAMES, art_info, render_art

    info = art_info(engine)
    assert set(info["names"]) == set(ART_NAMES) and info["pad"] > 0
    for tone in TONE_NAMES:
        assert b"<svg" in render_art(engine, "stomach", tone)
    natural, highlighted = render_art(engine, "vascular_system", "natural"), render_art(engine, "vascular_system", "primary")
    assert natural != highlighted
    with pytest.raises(KeyError):
        render_art(engine, "pill", "natural")  # not an anatomy picture: never served


async def test_illustration_endpoint_validates_and_reports_missing_figure_studio(body_client):
    assert (await body_client.get("/api/body/art/stomach.svg?tone=primary")).status_code == 503
    assert (await body_client.get("/api/body/art/stomach.svg?tone=hot-pink")).status_code == 404
    assert (await body_client.get("/api/body/art/pill.svg")).status_code == 404
    assert (await body_client.get("/api/body/art/stomach.png")).status_code == 404
    data = (await body_client.post("/api/body", json={"query": "sotorasib"})).json()
    assert data["layout"]["art"] is None  # the page falls back to plain shapes


async def test_body_request_validation(body_client):
    assert (await body_client.post("/api/body", json={"query": "  "})).status_code == 422
    assert (await body_client.post("/api/body", json={"query": "x", "kind": "planet"})).status_code == 422
