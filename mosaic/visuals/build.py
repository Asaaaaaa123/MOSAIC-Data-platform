"""Visual summary of a search: a mind map, comparison charts and data checks.

Everything here is counted directly from the records the databases returned and the curated
entities built from them (``mosaic.curation``); nothing is estimated or written by a model.
Each chart keeps, for every plotted value, the entity numbers (``e12``) or records
(``source:id``) it counted, so a bar can be traced back to rows of the data package, and
``checks`` re-adds the numbers to confirm they match the data.

``build_visuals(runs, entities)``:

- ``runs``: one ``QueryRun`` per query (one for a plain search, 2-4 to compare queries)
- ``entities``: the curated entities of all runs together (``curate(all records)``)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from ..curation import CuratedEntity
from ..models import Record

# Short labels for kinds of data (charts, mind map, web page).
GROUP_LABELS: dict[str, str] = {
    "papers": "Papers",
    "clinical_trials": "Clinical trials",
    "projects_studies": "Projects & studies",
    "datasets_repositories": "Datasets",
    "drugs_compounds": "Drugs & compounds",
    "safety_regulatory": "Safety & regulatory",
    "genes_proteins": "Genes & proteins",
    "variants_associations": "Variants & associations",
    "diseases_phenotypes": "Diseases & phenotypes",
    "pathways_ontology": "Pathways & ontology",
    "bioactivity_assays": "Bioactivity & assays",
    "people_institutions": "People & institutions",
    "guidelines": "Guidelines",
    "other": "Other",
}
GRADES = ("A", "B", "C", "D")
TOP_ENTITIES = 5  # entity leaves per branch in the mind map
MAX_SOURCE_BARS = 16
MAX_HEATMAP_ROWS = 12
TIMELINE_YEARS = 12

# Key facts per group for mind-map leaves: (field names to try, label). The first found wins.
_FACTS: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "papers": [(("journal", "journal_abbrev", "venue", "source_title"), "Journal"), (("cited_by", "citation_count", "citations"), "Cited by"),
               (("publication_types", "work_type", "document_type"), "Type"), (("mesh_terms",), "MeSH")],
    "clinical_trials": [(("phases", "phase"), "Phase"), (("status", "overall_status"), "Status"), (("enrollment",), "Enrollment"),
                        (("conditions",), "Conditions"), (("interventions",), "Interventions"), (("sponsor", "lead_sponsor"), "Sponsor")],
    "drugs_compounds": [(("max_phase",), "Max phase"), (("first_approval",), "First approval"), (("molecule_type",), "Molecule type"),
                        (("formula", "molecular_formula"), "Formula"), (("mw", "molecular_weight", "full_mwt"), "Mol. weight")],
    "genes_proteins": [(("symbol", "gene_symbol", "gene_name"), "Symbol"), (("location", "chromosome", "map_location"), "Location"),
                       (("organism", "species"), "Organism"), (("protein_name", "full_name", "name"), "Name"), (("length",), "Length (aa)")],
    "datasets_repositories": [(("repository", "publisher", "portal"), "Repository"), (("file_count",), "Files"),
                              (("total_size_bytes",), "Size (bytes)"), (("license",), "License"), (("downloads",), "Downloads")],
    "diseases_phenotypes": [(("ontology", "obo_id"), "Ontology"), (("synonyms",), "Synonyms"), (("definition", "description"), "Definition")],
    "pathways_ontology": [(("species", "organism"), "Species"), (("ontology", "obo_id"), "Ontology"), (("category",), "Category")],
}
_SKIP_FACT_FIELDS = {"url", "urls", "iri", "oa_url", "pdf_url", "links", "xrefs", "related", "match_rank", "match_score",
                     "entity", "has_data", "has_pdf", "has_full_text", "in_pmc", "is_open_access"}
# Short chart labels for connectors whose titles are long
SHORT_SOURCE_NAMES = {
    "alliance": "Alliance (AGR)", "ols": "OLS", "opentargets": "Open Targets", "ncbi": "NCBI (PubMed ...)",
    "metabolomics_workbench": "Metabolomics WB", "nci_gdc": "NCI GDC", "rcsb_pdb": "RCSB PDB", "osf": "OSF",
    "hubmap": "HuBMAP", "gtex": "GTEx", "clinicaltrials": "ClinicalTrials.gov", "semantic_scholar": "Semantic Scholar",
    "gwas_catalog": "GWAS Catalog", "pgs_catalog": "PGS Catalog", "dandi": "DANDI", "ndex": "NDEx", "ctd": "CTD",
}
_NUMERIC = re.compile(r"^[<>~]?\s*-?[\d.,]+(\s*%)?$")
_PHASE_ORDER = ["Early phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4",
                "Not applicable", "Not stated"]
_YEAR = re.compile(r"^\s*(\d{4})")


# ---------------------------------------------------------------- models
class ChartSeries(BaseModel):
    name: str
    values: list[float]


class ChartData(BaseModel):
    """One chart: the numbers, how they were counted, and which data each number counted."""

    id: str
    title: str
    kind: Literal["hbar", "bar", "stacked", "heatmap"]
    categories: list[str]
    series: list[ChartSeries]
    x_label: str = ""
    y_label: str = ""
    value_label: str = ""
    definition: str = Field(description="Exactly how every value was counted")
    members: dict[str, list[str]] = Field(
        default_factory=dict,
        description="'category|series' -> entity numbers (e12) or records (source:id) counted in that value",
    )

    def rows(self) -> list[dict[str, Any]]:
        """One row per plotted value (for figure_data.csv)."""
        out = []
        for s in self.series:
            for cat, value in zip(self.categories, s.values):
                members = self.members.get(f"{cat}|{s.name}", [])
                out.append({"chart": self.id, "chart_title": self.title, "category": cat, "series": s.name,
                            "value": value, "counted": "; ".join(members), "definition": self.definition})
        return out


class MindNode(BaseModel):
    id: str
    label: str
    kind: Literal["root", "query", "group", "entity", "shared"]
    detail: str = ""
    count: int | None = None
    group: str = ""
    entity: str | None = None
    type: str | None = None
    grade: str | None = None
    score: int | None = None
    date: str | None = None
    sources: list[str] = Field(default_factory=list)
    facts: list[list[str]] = Field(default_factory=list, description="[label, value] pairs from the entity's data")
    queries: list[str] = Field(default_factory=list)
    children: list["MindNode"] = Field(default_factory=list)


class DataCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class Visuals(BaseModel):
    queries: list[str]
    compare: bool
    generated_at: str
    n_entities: int
    n_records: int
    mindmap: MindNode
    charts: list[ChartData]
    checks: list[DataCheck]
    entity_queries: dict[str, list[str]] = Field(default_factory=dict, description="entity number -> queries that found it")

    @property
    def checks_passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def figure_rows(self) -> list[dict[str, Any]]:
        return [row for chart in self.charts for row in chart.rows()]


@dataclass
class QueryRun:
    """What one query returned: its records and per-source statuses (``SourceSearchResult``)."""

    query: str
    records: list[Record]
    sources: list[Any] = field(default_factory=list)
    limit_per_source: int | None = None


# ---------------------------------------------------------------- helpers
def type_group(entity_type: str | None) -> str:
    from ..datapackage import _TYPE_TO_GROUP  # lazy: datapackage imports curation too

    return _TYPE_TO_GROUP.get(entity_type or "", "other")


def group_label(group: str) -> str:
    return GROUP_LABELS.get(group, group.replace("_", " ").capitalize())


def short_source_name(title: str | None, name: str) -> str:
    """'Comparative Toxicogenomics Database (CTD)' -> 'CTD'; 'UniProt (UniProtKB)' -> 'UniProt'."""
    if name in SHORT_SOURCE_NAMES:
        return SHORT_SOURCE_NAMES[name]
    if not title:
        return name
    base, _, rest = title.partition(" (")
    paren = rest.rstrip(")").strip()
    base = base.split(" - ")[0].strip()
    if len(base) > 24 and paren and len(paren) <= 14:
        return paren
    return base or name


def _record_key(source: str, record_id: str) -> tuple[str, str]:
    return source, str(record_id).strip().lower()


def entity_queries(runs: list[QueryRun], entities: list[CuratedEntity]) -> dict[str, list[str]]:
    """Which queries found each entity (through the records merged into it)."""
    by_record: dict[tuple[str, str], list[str]] = {}
    for run in runs:
        for r in run.records:
            found = by_record.setdefault(_record_key(r.source, r.id), [])
            if run.query not in found:
                found.append(run.query)
    order = {run.query: i for i, run in enumerate(runs)}
    out: dict[str, list[str]] = {}
    for e in entities:
        qs: set[str] = set()
        for s in e.sources:
            qs.update(by_record.get(_record_key(str(s.get("source")), str(s.get("id"))), []))
        out[e.entity_id] = sorted(qs, key=lambda q: order.get(q, 99))
    return out


def year_of(date: str | None) -> int | None:
    m = _YEAR.match(date or "")
    if not m:
        return None
    year = int(m.group(1))
    return year if 1800 <= year <= datetime.now(timezone.utc).year + 10 else None


def _text(value: Any, limit: int = 90) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float):
        text = f"{value:g}"
    elif isinstance(value, (list, tuple)):
        parts = [_text(v, limit) for v in value[:6]]
        text = "; ".join(p for p in parts if p) + ("; ..." if len(value) > 6 else "")
    elif isinstance(value, dict):
        text = ", ".join(f"{k}: {_text(v, 40)}" for k, v in list(value.items())[:4] if _text(v, 40))
    else:
        text = str(value)
    return clip_words(text, limit)


def clip_words(text: str, limit: int) -> str:
    """Shorten at a word boundary: 'Dose-Dependent Intracranial Response to…'."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-·") + "…"


def phase_label(value: Any) -> str:
    """ClinicalTrials.gov phases (['PHASE2', 'PHASE3'], 'PHASE1', 'NA') -> 'Phase 2/3' ..."""
    items = value if isinstance(value, (list, tuple)) else [value] if value not in (None, "") else []
    nums: list[str] = []
    early = False
    for item in items:
        text = str(item).upper().replace(" ", "").replace("_", "")
        if text in ("NA", "N/A", "NOTAPPLICABLE"):
            return "Not applicable"
        if text.startswith("EARLYPHASE"):
            early = True
            continue
        m = re.search(r"(\d)", text)
        if m and m.group(1) not in nums:
            nums.append(m.group(1))
    if early and not nums:
        return "Early phase 1"
    if not nums:
        return "Not stated"
    return "Phase " + "/".join(sorted(nums))


def status_label(value: Any) -> str:
    text = _text(value[0] if isinstance(value, (list, tuple)) and value else value, 60)
    return text.replace("_", " ").capitalize() if text else "Not stated"


def entity_facts(e: CuratedEntity, limit: int = 4) -> list[list[str]]:
    group = type_group(e.type)
    facts: list[list[str]] = []
    used: set[str] = set()
    for names, label in _FACTS.get(group, []):
        for name in names:
            value = e.fields.get(name)
            if name in ("phases", "phase") and value not in (None, "", []):
                text = phase_label(value)
            elif name in ("status", "overall_status") and group == "clinical_trials":
                text = status_label(value) if value not in (None, "", []) else ""
            else:
                text = _text(value)
            if text:
                facts.append([label, text])
                used.add(name)
                break
        if len(facts) >= limit:
            return facts
    for key, value in e.fields.items():  # fill up with the entity's other short scalar fields
        if len(facts) >= limit:
            break
        if key in used or key in _SKIP_FACT_FIELDS or isinstance(value, (dict, list)):
            continue
        text = _text(value, 60)
        if text and not text.startswith("http"):
            facts.append([key.replace("_", " ").capitalize(), text])
    return facts


def _rank(e: CuratedEntity) -> tuple:
    return (-e.evaluation.score, -len(e.evaluation.corroborated_by), -(year_of(e.date) or 0), e.entity_id)


def _entity_node(e: CuratedEntity, queries: list[str], prefix: str) -> MindNode:
    facts = entity_facts(e)
    year = year_of(e.date)
    detail_bits = [str(year)] if year else []
    for label, value in facts:  # numbers keep their label ("Cited by 139"), words stand alone
        if len(detail_bits) >= 3 or value == str(year):
            continue
        detail_bits.append(f"{label} {value}" if _NUMERIC.match(value) else clip_words(value, 28))
    return MindNode(
        id=f"{prefix}{e.entity_id}", label=_text(e.title or e.entity_id, 140), kind="entity",
        detail=" · ".join(detail_bits), group=type_group(e.type), entity=e.entity_id, type=e.type,
        grade=e.evaluation.grade, score=e.evaluation.score, date=e.date,
        sources=[f"{s.get('source')}:{s.get('id')}" for s in e.sources], facts=facts, queries=queries,
    )


def _group_branches(entities: list[CuratedEntity], eq: dict[str, list[str]], prefix: str) -> list[MindNode]:
    by_group: dict[str, list[CuratedEntity]] = {}
    for e in entities:
        by_group.setdefault(type_group(e.type), []).append(e)
    branches = []
    for group, members in sorted(by_group.items(), key=lambda kv: (-len(kv[1]), list(GROUP_LABELS).index(kv[0]) if kv[0] in GROUP_LABELS else 99)):
        top = sorted(members, key=_rank)[:TOP_ENTITIES]
        branches.append(MindNode(
            id=f"{prefix}g:{group}", label=group_label(group), kind="group", group=group, count=len(members),
            detail=f"{len(members)} entit{'y' if len(members) == 1 else 'ies'}",
            children=[_entity_node(e, eq.get(e.entity_id, []), prefix) for e in top],
        ))
    return branches


# ---------------------------------------------------------------- charts
def _members_key(category: str, series: str) -> str:
    return f"{category}|{series}"


def _records_by_source(runs: list[QueryRun], titles: dict[str, str]) -> ChartData | None:
    totals: Counter[str] = Counter()
    per_run: list[dict[str, list[str]]] = []
    for run in runs:
        found: dict[str, list[str]] = {}
        for r in run.records:
            found.setdefault(r.source, []).append(f"{r.source}:{r.id}")
            totals[r.source] += 1
        per_run.append(found)
    if not totals:
        return None
    ranked = [s for s, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]
    shown, rest = ranked[:MAX_SOURCE_BARS], ranked[MAX_SOURCE_BARS:]
    labels = {s: short_source_name(titles.get(s), s) for s in shown}
    categories = [labels[s] for s in shown]
    if rest:
        categories.append(f"Other ({len(rest)} databases)")
    chart = ChartData(
        id="records_by_source", title="Records returned by each database",
        kind="hbar", categories=categories, series=[], x_label="Records (n)",
        definition=("Number of records each database returned for the query"
                    + (f" (at most {runs[0].limit_per_source} per database)" if runs[0].limit_per_source else "")
                    + ", counted before duplicates across databases were merged."),
    )
    for run, found in zip(runs, per_run):
        values = []
        for s in shown:
            ids = found.get(s, [])
            values.append(float(len(ids)))
            chart.members[_members_key(labels[s], run.query)] = ids
        if rest:
            ids = [i for s in rest for i in found.get(s, [])]
            values.append(float(len(ids)))
            chart.members[_members_key(categories[-1], run.query)] = ids
        chart.series.append(ChartSeries(name=run.query, values=values))
    return chart


def _entities_by_type(runs: list[QueryRun], entities: list[CuratedEntity], eq: dict[str, list[str]]) -> ChartData | None:
    if not entities:
        return None
    counts: Counter[str] = Counter(type_group(e.type) for e in entities)
    groups = [g for g, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    chart = ChartData(
        id="entities_by_type", title="Data found, by kind",
        kind="hbar", categories=[group_label(g) for g in groups], series=[], x_label="Entities (n)",
        definition=("Number of distinct entities of each kind after records describing the same thing in "
                    "different databases were merged (one paper, trial, drug ... counted once)."),
    )
    for run in runs:
        values = []
        for g in groups:
            ids = [e.entity_id for e in entities if type_group(e.type) == g and run.query in eq.get(e.entity_id, [])]
            values.append(float(len(ids)))
            chart.members[_members_key(group_label(g), run.query)] = ids
        chart.series.append(ChartSeries(name=run.query, values=values))
    return chart


def _grades(runs: list[QueryRun], entities: list[CuratedEntity], eq: dict[str, list[str]]) -> ChartData | None:
    if not entities:
        return None
    if len(runs) == 1:
        counts: Counter[str] = Counter(type_group(e.type) for e in entities)
        groups = [g for g, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        cats = [group_label(g) for g in groups]
        pick = lambda cat_index, e: type_group(e.type) == groups[cat_index]  # noqa: E731
        x_label = ""
    else:
        cats = [run.query for run in runs]
        pick = lambda cat_index, e: runs[cat_index].query in eq.get(e.entity_id, [])  # noqa: E731
        x_label = "Query"
    chart = ChartData(
        id="quality_grades", title="Quality grade of the data",
        kind="stacked", categories=cats, series=[], y_label="Entities (n)", x_label=x_label,
        definition=("Mosaic quality grade of each entity: A (score >= 80), B (>= 60), C (>= 40), D, from the source's "
                    "reliability tier, agreement across databases, completeness and identifier coverage, minus conflicts."),
    )
    for grade in GRADES:
        values = []
        for i, cat in enumerate(cats):
            ids = [e.entity_id for e in entities if e.evaluation.grade == grade and pick(i, e)]
            values.append(float(len(ids)))
            chart.members[_members_key(cat, f"Grade {grade}")] = ids
        if any(values):
            chart.series.append(ChartSeries(name=f"Grade {grade}", values=values))
    return chart


def _timeline(runs: list[QueryRun], entities: list[CuratedEntity], eq: dict[str, list[str]]) -> ChartData | None:
    dated = [(e, year_of(e.date)) for e in entities]
    dated = [(e, y) for e, y in dated if y is not None]
    if len(dated) < 3 or len({y for _, y in dated}) < 2:
        return None
    last = max(y for _, y in dated)
    first = max(min(y for _, y in dated), last - TIMELINE_YEARS + 1)
    earlier = any(y < first for _, y in dated)
    years = list(range(first, last + 1))
    cats = ([f"Before {first}"] if earlier else []) + [str(y) for y in years]

    def cat_of(year: int) -> str:
        return f"Before {first}" if year < first else str(year)

    if len(runs) == 1:
        counts: Counter[str] = Counter(type_group(e.type) for e, _ in dated)
        main = [g for g, _ in counts.most_common(4)]
        series_keys = [(group_label(g), lambda e, g=g: type_group(e.type) == g) for g in main]
        if len(counts) > 4:
            series_keys.append(("Other", lambda e, main=tuple(main): type_group(e.type) not in main))
        kind: Literal["stacked", "bar"] = "stacked"
    else:
        series_keys = [(run.query, lambda e, q=run.query: q in eq.get(e.entity_id, [])) for run in runs]
        kind = "bar"
    chart = ChartData(
        id="timeline", title="Data by year", kind=kind, categories=cats, series=[],
        y_label="Entities (n)", x_label="Year",
        definition=("Entities by the year of their date field (publication date for papers, start date for trials, "
                    f"release date for datasets ...). {len(entities) - len(dated)} entities without a date are not shown."),
    )
    for name, match in series_keys:
        values = []
        for cat in cats:
            ids = [e.entity_id for e, y in dated if cat_of(y) == cat and match(e)]
            values.append(float(len(ids)))
            chart.members[_members_key(cat, name)] = ids
        if any(values):
            chart.series.append(ChartSeries(name=name, values=values))
    return chart if chart.series else None


def _trials(runs: list[QueryRun], entities: list[CuratedEntity], eq: dict[str, list[str]]) -> list[ChartData]:
    trials = [e for e in entities if e.type == "trial"]
    if len(trials) < 2:
        return []
    phase = {e.entity_id: phase_label(e.fields.get("phases") or e.fields.get("phase")) for e in trials}
    status = {e.entity_id: status_label(e.fields.get("status") or e.fields.get("overall_status")) for e in trials}
    out = []
    for cid, title, labels, order, definition in (
        ("trial_phases", "Clinical trials by phase", phase, _PHASE_ORDER,
         "Clinical trials by the phase registered at ClinicalTrials.gov (Phase 2/3 = a combined-phase trial)."),
        ("trial_status", "Clinical trials by status", status, None,
         "Clinical trials by their overall recruitment status as registered."),
    ):
        present = Counter(labels.values())
        cats = [c for c in order if c in present] if order else [c for c, _ in present.most_common()]
        cats += [c for c in present if c not in cats]
        chart = ChartData(id=cid, title=title, kind="bar" if len(runs) > 1 else "hbar",
                          categories=cats, series=[], x_label="Trials (n)" if len(runs) == 1 else "",
                          y_label="Trials (n)" if len(runs) > 1 else "", definition=definition)
        for run in runs:
            values = []
            for cat in cats:
                ids = [e.entity_id for e in trials if labels[e.entity_id] == cat and run.query in eq.get(e.entity_id, [])]
                values.append(float(len(ids)))
                chart.members[_members_key(cat, run.query)] = ids
            chart.series.append(ChartSeries(name=run.query, values=values))
        out.append(chart)
    return out


def _source_kind_matrix(entities: list[CuratedEntity], titles: dict[str, str]) -> ChartData | None:
    cells: dict[tuple[str, str], list[str]] = {}
    for e in entities:
        g = type_group(e.type)
        for s in {str(s.get("source")) for s in e.sources}:
            cells.setdefault((s, g), []).append(e.entity_id)
    sources = Counter()
    groups = Counter()
    for (s, g), ids in cells.items():
        sources[s] += len(ids)
        groups[g] += len(ids)
    if len(sources) < 3 or len(groups) < 2:
        return None
    rows = [s for s, _ in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))][:MAX_HEATMAP_ROWS]
    cols = [g for g, _ in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))][:8]
    chart = ChartData(
        id="sources_by_kind", title="Which database supplied which data",
        kind="heatmap", categories=[group_label(g) for g in cols], series=[], value_label="Entities (n)",
        definition=("Number of entities of each kind that each database contributed a record to (an entity confirmed "
                    f"by several databases counts once for each). Top {MAX_HEATMAP_ROWS} databases."),
    )
    for s in rows:
        name = short_source_name(titles.get(s), s)
        values = []
        for g in cols:
            ids = cells.get((s, g), [])
            values.append(float(len(ids)))
            chart.members[_members_key(group_label(g), name)] = ids
        chart.series.append(ChartSeries(name=name, values=values))
    return chart


def _overlap(runs: list[QueryRun], entities: list[CuratedEntity], eq: dict[str, list[str]]) -> ChartData:
    cats = [f"Only {run.query}" for run in runs] + ["Found by 2+ queries"]
    members: dict[str, list[str]] = {c: [] for c in cats}
    for e in entities:
        qs = eq.get(e.entity_id, [])
        if len(qs) >= 2:
            members["Found by 2+ queries"].append(e.entity_id)
        elif len(qs) == 1:
            members[f"Only {qs[0]}"].append(e.entity_id)
    chart = ChartData(
        id="overlap", title="Shared and unique data", kind="hbar", categories=cats,
        series=[ChartSeries(name="Entities", values=[float(len(members[c])) for c in cats])], x_label="Entities (n)",
        definition=("Entities found by only one query, or by two or more (the same paper, trial, drug ... returned "
                    "for several queries and merged by shared identifiers)."),
    )
    chart.members = {_members_key(c, "Entities"): ids for c, ids in members.items()}
    return chart


# ---------------------------------------------------------------- checks
def _sum(chart: ChartData | None, series: str | None = None) -> float:
    if chart is None:
        return 0.0
    return sum(sum(s.values) for s in chart.series if series is None or s.name == series)


def _checks(runs: list[QueryRun], entities: list[CuratedEntity], eq: dict[str, list[str]],
            charts: dict[str, ChartData], mindmap: MindNode) -> list[DataCheck]:
    checks: list[DataCheck] = []
    by_type = charts.get("entities_by_type")
    by_source = charts.get("records_by_source")
    for run in runs:
        n_entities = sum(1 for e in entities if run.query in eq.get(e.entity_id, []))
        got = _sum(by_type, run.query)
        checks.append(DataCheck(name=f"Entities by kind add up ({run.query})", ok=got == n_entities,
                                detail=f"chart total {got:g}, entities found {n_entities}"))
        got = _sum(by_source, run.query)
        checks.append(DataCheck(name=f"Records by database add up ({run.query})", ok=got == len(run.records),
                                detail=f"chart total {got:g}, records returned {len(run.records)}"))
        reported = {s.source: s.returned for s in run.sources if getattr(s, "ok", False)}
        if reported:
            counted = Counter(r.source for r in run.records)
            bad = [f"{s}: status {n}, records {counted.get(s, 0)}" for s, n in reported.items() if counted.get(s, 0) != n]
            checks.append(DataCheck(name=f"Every database's records are in the data ({run.query})", ok=not bad,
                                    detail="; ".join(bad) or f"{len(reported)} databases, counts match"))
    grades = charts.get("quality_grades")
    if grades is not None:
        expected = len(entities) if len(runs) == 1 else sum(len(eq.get(e.entity_id, [])) for e in entities)
        got = _sum(grades)
        checks.append(DataCheck(name="Quality grades add up", ok=got == expected, detail=f"chart total {got:g}, expected {expected}"))
    trials = [e for e in entities if e.type == "trial"]
    for cid in ("trial_phases", "trial_status"):
        chart = charts.get(cid)
        if chart is not None:
            expected = sum(len(eq.get(e.entity_id, [])) for e in trials)
            got = _sum(chart)
            checks.append(DataCheck(name=f"{chart.title} add up", ok=got == expected, detail=f"chart total {got:g}, trials {expected}"))
    timeline = charts.get("timeline")
    if timeline is not None:
        dated = [e for e in entities if year_of(e.date) is not None]
        expected = len(dated) if len(runs) == 1 else sum(len(eq.get(e.entity_id, [])) for e in dated)
        got = _sum(timeline)
        checks.append(DataCheck(name="Data by year adds up", ok=got == expected, detail=f"chart total {got:g}, dated entities {expected}"))
    known = {e.entity_id for e in entities}

    def leaves(node: MindNode) -> Iterable[MindNode]:
        if node.entity:
            yield node
        for child in node.children:
            yield from leaves(child)

    shown = list(leaves(mindmap))
    missing = [n.entity for n in shown if n.entity not in known]
    checks.append(DataCheck(name="Every mind-map item is in the data", ok=not missing,
                            detail=f"{len(shown)} items" + (f"; missing {', '.join(map(str, missing))}" if missing else "")))
    return checks


def table_checks(tables: list[Any], group_by: str, visuals: Visuals) -> list[DataCheck]:
    """Recount the written package tables (``GroupTable``s) and compare them with the charts."""
    known = set(visuals.entity_queries)
    checks: list[DataCheck] = []
    bad_rows = [f"{t.key}: {r.get('entity')}" for t in tables for r in t.rows if r.get("entity") not in known]
    dupes = [t.key for t in tables if len({r.get("entity") for r in t.rows}) != len(t.rows)]
    checks.append(DataCheck(name="Every table row is one entity of the data", ok=not bad_rows and not dupes,
                            detail="; ".join(bad_rows[:5] + [f"{k}: duplicate rows" for k in dupes]) or
                            f"{sum(len(t.rows) for t in tables)} rows in {len(tables)} tables"))
    chart = next((c for c in visuals.charts if c.id == "entities_by_type"), None)
    if group_by == "type" and chart is not None:
        wrong = []
        for t in tables:
            for q in visuals.queries:
                if visuals.compare:
                    n = sum(1 for r in t.rows if q in [x.strip() for x in str(r.get("matched_queries") or "").split(";")])
                else:
                    n = len(t.rows)
                series = next((s for s in chart.series if s.name == q), None)
                label = group_label(t.key)
                got = series.values[chart.categories.index(label)] if series and label in chart.categories else 0.0
                if n != got:
                    wrong.append(f"{t.key} ({q}): table {n}, chart {got:g}")
        checks.append(DataCheck(name="Tables match the chart of data by kind", ok=not wrong,
                                detail="; ".join(wrong[:6]) or f"{len(tables)} tables x {len(visuals.queries)} queries"))
    return checks


# ---------------------------------------------------------------- entry point
def build_visuals(runs: list[QueryRun], entities: list[CuratedEntity], *,
                  source_titles: dict[str, str] | None = None) -> Visuals:
    if not runs:
        raise ValueError("build_visuals needs at least one query run")
    titles = source_titles or {}
    compare = len(runs) > 1
    # one query: every entity was built from its records, whatever cleaning did to the record ids
    eq = entity_queries(runs, entities) if compare else {e.entity_id: [runs[0].query] for e in entities}
    n_records = sum(len(r.records) for r in runs)
    databases = {r.source for run in runs for r in run.records}

    if compare:
        children = []
        for i, run in enumerate(runs):
            mine = [e for e in entities if run.query in eq.get(e.entity_id, [])]
            children.append(MindNode(
                id=f"q{i}", label=run.query, kind="query", count=len(mine), queries=[run.query],
                detail=f"{len(mine)} entities · {len({r.source for r in run.records})} databases",
                children=_group_branches(mine, eq, f"q{i}:"),
            ))
        shared = sorted((e for e in entities if len(eq.get(e.entity_id, [])) >= 2), key=_rank)
        if shared:
            children.append(MindNode(
                id="shared", label="Found by 2+ queries", kind="shared", count=len(shared),
                detail=f"{len(shared)} shared entit{'y' if len(shared) == 1 else 'ies'}",
                children=[_entity_node(e, eq.get(e.entity_id, []), "s:") for e in shared[:TOP_ENTITIES * 2]],
            ))
        root = MindNode(id="root", label=" vs ".join(run.query for run in runs), kind="root", count=len(entities),
                        detail=f"{len(entities)} entities · {len(databases)} databases", children=children)
    else:
        root = MindNode(id="root", label=runs[0].query, kind="root", count=len(entities), queries=[runs[0].query],
                        detail=f"{len(entities)} entities · {len(databases)} databases",
                        children=_group_branches(entities, eq, ""))

    built = [
        _entities_by_type(runs, entities, eq),
        _records_by_source(runs, titles),
        _overlap(runs, entities, eq) if compare else None,
        _grades(runs, entities, eq),
        _timeline(runs, entities, eq),
        *_trials(runs, entities, eq),
        None if compare else _source_kind_matrix(entities, titles),
    ]
    charts = [c for c in built if c is not None and c.series and c.categories]
    by_id = {c.id: c for c in charts}
    return Visuals(
        queries=[run.query for run in runs], compare=compare,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        n_entities=len(entities), n_records=n_records, mindmap=root, charts=charts,
        checks=_checks(runs, entities, eq, by_id, root), entity_queries=eq,
    )
