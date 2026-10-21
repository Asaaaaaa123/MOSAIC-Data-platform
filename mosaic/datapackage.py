"""Data packages: the real, cleaned data behind a search or conversation, split into several tables.

``build_package(records, group_by=...)`` curates the records (clean, merge across databases,
grade; see ``mosaic.curation``) and splits the entities into one table per group:

- ``group_by="type"`` (default): papers, clinical_trials, projects_studies, datasets_repositories,
  drugs_compounds, safety_regulatory, genes_proteins, variants_associations, diseases_phenotypes,
  pathways_ontology, bioactivity_assays, other
- ``group_by="species"``: human, macaque, mouse, rat, dog, pig, chicken, frog, zebrafish, fly, worm,
  yeast, plant (the 13 species of the cross-species comparison), plus other primates, other birds,
  other animals, microbes, not_species_specific and species_unknown (an entity studied in several
  species appears in each species table)
- ``group_by="source"``: one table per database

Each table has the columns that make sense for its rows: entity, title, date, quality, the
identifier types present, the cleaned data fields (most common first), authors/summary, sources.

``write_package(...)`` writes a folder ``<exports_dir>/<slug>__<export_id>/`` with one CSV per
group, ``mosaic_data.xlsx`` (one sheet per group), ``README.txt``, ``manifest.json`` and
``mosaic_data.zip``.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .curation import CuratedEntity, curate
from .models import Record

GroupBy = Literal["type", "species", "source"]

TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "papers": ("article", "preprint", "book", "journal"),
    "clinical_trials": ("trial",),
    "projects_studies": ("study", "sample", "grant"),
    "datasets_repositories": ("dataset", "repository", "software"),
    "drugs_compounds": ("drug", "compound", "substance"),
    "safety_regulatory": ("adverse_event", "drug_label", "recall"),
    "genes_proteins": ("gene", "protein", "target", "structure"),
    "variants_associations": ("variant", "association"),
    "diseases_phenotypes": ("disease", "phenotype"),
    "pathways_ontology": ("pathway", "ontology_term"),
    "bioactivity_assays": ("activity", "assay"),
    "people_institutions": ("author", "institution"),
    "guidelines": ("guideline",),
}
GROUP_TITLES = {
    "papers": "Papers (articles, preprints, books)",
    "clinical_trials": "Clinical trials",
    "projects_studies": "Projects and studies",
    "datasets_repositories": "Datasets and repository records",
    "drugs_compounds": "Drugs and compounds",
    "safety_regulatory": "Safety and regulatory (labels, adverse events, recalls)",
    "genes_proteins": "Genes, proteins, targets and structures",
    "variants_associations": "Variants and associations",
    "diseases_phenotypes": "Diseases and phenotypes",
    "pathways_ontology": "Pathways and ontology terms",
    "bioactivity_assays": "Bioactivity and assays",
    "people_institutions": "People and institutions",
    "guidelines": "Guidelines",
    "other": "Other",
}
_TYPE_TO_GROUP = {t: g for g, types in TYPE_GROUPS.items() for t in types}

# Species buckets: (bucket, patterns matched against organism names, MeSH check tags, taxon IDs).
# The first matching bucket wins, so specific species come before the general groups. The 13 species
# of the cross-species comparison (mosaic/species) each have their own bucket.
SPECIES_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("human", ("homo sapiens", "human", "humans", "taxon:9606", "ncbitaxon:9606", "9606", "patient", "patients")),
    ("macaque", ("macaca", "rhesus", "macaque", "macaques", "cynomolgus", "9544", "9541")),
    ("primate", ("pan troglodytes", "chimpanzee", "marmoset", "callithrix", "primates", "monkey", "monkeys")),
    ("mouse", ("mus musculus", "mouse", "mice", "murine", "10090")),
    ("rat", ("rattus", "rat", "rats", "10116")),
    ("dog", ("canis", "dog", "dogs", "beagle", "beagles", "canine", "9615")),
    # before "pig" so that "guinea pig" is not filed as a pig
    ("other_animal", ("guinea pig", "guinea pigs", "felis", "cat", "cats", "rabbit", "rabbits", "oryctolagus", "hamster",
                      "hamsters", "ferret", "ferrets", "bos taurus", "cattle", "ovis", "sheep")),
    ("pig", ("sus scrofa", "pig", "pigs", "swine", "porcine", "minipig", "minipigs", "9823")),
    ("chicken", ("gallus", "chicken", "chickens", "9031")),
    ("bird", ("taeniopygia", "zebra finch", "aves", "birds", "bird", "quail", "duck")),
    ("frog", ("xenopus", "frog", "frogs", "8355", "8364")),
    ("zebrafish", ("danio rerio", "zebrafish", "7955")),
    ("fly", ("drosophila", "fruit fly", "7227")),
    ("worm", ("caenorhabditis", "c. elegans", "nematode", "6239")),
    ("yeast", ("saccharomyces", "schizosaccharomyces", "yeast", "4932", "559292")),
    ("plant", ("arabidopsis", "oryza", "rice", "zea mays", "maize", "nicotiana", "tobacco", "solanum", "tomato",
               "glycine max", "soybean", "triticum", "wheat", "plants", "plant", "viridiplantae", "3702")),
    ("microbe", ("bacteria", "escherichia", "e. coli", "mycobacterium", "staphylococcus", "virus", "viruses", "sars-cov",
                 "influenza", "hiv", "plasmodium", "fungi", "candida")),
]
SPECIES_TITLES = {
    "human": "Human 人", "macaque": "Macaque 猕猴", "primate": "Other primates 其他灵长类", "mouse": "Mouse 小鼠",
    "rat": "Rat 大鼠", "dog": "Dog 狗", "pig": "Pig 猪", "chicken": "Chicken 鸡", "bird": "Other birds 其他鸟类",
    "frog": "Frog (Xenopus) 爪蟾", "zebrafish": "Zebrafish 斑马鱼", "fly": "Fruit fly 果蝇", "worm": "Worm (C. elegans) 线虫",
    "yeast": "Yeast 酵母", "plant": "Plant (Arabidopsis ...) 植物", "other_animal": "Other animals 其他动物",
    "microbe": "Microbe / virus 微生物", "not_species_specific": "Not species-specific (drugs, compounds) 非物种特异",
    "species_unknown": "Species not stated 未注明物种",
}
_SPECIES_FIELDS = ("organism", "organisms", "species", "taxon", "taxonomy", "organism_name", "scientific_name",
                   "source_organism", "host", "host_organism", "model_organism", "organism_scientific_name")
# Only MeSH (whose check tags "Humans", "Mice" ... are curated), not free keywords like "plant extract".
_MESH_FIELDS = ("mesh", "mesh_terms", "mesh_headings")
# Free text (title / abstract) is noisier than organism fields: skip buckets and words that mislead
# there ("plant-derived", "virus-like", "cat" ...); Latin names and lab-animal words are reliable.
_TEXT_BUCKETS_SKIP = {"microbe", "other_animal"}
_TEXT_WORDS_SKIP = {"plant", "plants", "rice", "tobacco", "tomato", "wheat", "soybean", "bird", "birds", "aves",
                    "duck", "monkey", "monkeys", "primates", "cat", "cats", "9606", "10090", "10116", "7955", "7227",
                    "6239", "4932", "559292", "8355", "8364", "9031", "3702", "9544", "9541", "9615", "9823"}
_TEXT_TYPES = {"article", "preprint", "book", "dataset", "study", "sample", "repository"}
_HUMAN_DEFAULT_TYPES = {"trial", "adverse_event", "drug_label", "recall", "guideline"}
_NOT_SPECIES_TYPES = {"drug", "compound", "substance"}

# Identifier columns per group, most useful first (other present IDs follow).
_ID_ORDER: dict[str, tuple[str, ...]] = {
    "papers": ("doi", "pmid", "pmcid", "openalex", "s2", "scopus_eid", "wos_uid", "arxiv", "issn"),
    "clinical_trials": ("nct", "euctr"),
    "drugs_compounds": ("chembl", "pubchem_cid", "inchikey", "unii", "rxcui", "drugbank", "drugcentral", "cas", "atc"),
    "safety_regulatory": ("rxcui", "unii", "spl_set_id", "fda_application", "ndc"),
    "genes_proteins": ("gene_symbol", "hgnc", "entrez_gene", "ensembl_gene", "uniprot", "pdb"),
    "variants_associations": ("rsid", "clinvar", "gnomad_variant", "hgvs", "gwas_study"),
    "diseases_phenotypes": ("mondo", "efo", "doid", "mesh", "omim", "orphanet", "hp"),
    "datasets_repositories": ("doi", "zenodo", "figshare", "dryad", "dataverse", "osf", "synapse", "dandi", "openneuro", "geo"),
}
MAX_FIELD_COLUMNS = 40
MAX_CELL_CHARS = 32_000  # Excel's per-cell limit is 32,767
_FILE_SAFE = re.compile(r"[^\w-]+")  # keeps letters of any script (e.g. Chinese names)


@dataclass
class GroupTable:
    key: str
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]

    @property
    def filename(self) -> str:
        return f"{self.key}.csv"


@dataclass
class DataPackage:
    group_by: str
    tables: list[GroupTable]
    stats: dict[str, Any]
    entities: list[CuratedEntity] = field(default_factory=list)


# ---------------------------------------------------------------- grouping
def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    return []


def _bucket(text: str) -> str | None:
    low = text.lower().strip()
    for bucket, patterns in SPECIES_BUCKETS:
        for pattern in patterns:
            if pattern.isdigit():
                if re.search(rf"(?<!\d){pattern}(?!\d)", low):
                    return bucket
            elif re.search(rf"(?<![a-z]){re.escape(pattern)}(?![a-z])", low):
                return bucket
    return None


def species_of(entity: CuratedEntity) -> list[str]:
    """Species buckets an entity belongs to (several when a paper studied several)."""
    found: list[str] = []
    for key, value in entity.fields.items():
        if key.lower() in _SPECIES_FIELDS or key.lower().endswith(("_organism", "_species")):
            for text in _strings(value):
                bucket = _bucket(text)
                if bucket and bucket not in found:
                    found.append(bucket)
    if not found and entity.type in {"article", "preprint", "book"}:
        for key in _MESH_FIELDS:
            for text in _strings(entity.fields.get(key)):
                bucket = _bucket(text)
                if bucket and bucket not in found:
                    found.append(bucket)
    if not found and entity.type in _TEXT_TYPES:
        text = " ".join(filter(None, [entity.title, (entity.summary or "")[:1500]])).lower()
        for bucket, patterns in SPECIES_BUCKETS:
            if bucket in _TEXT_BUCKETS_SKIP or bucket in found:
                continue
            if any(p not in _TEXT_WORDS_SKIP and re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", text) for p in patterns):
                found.append(bucket)
    if found:
        return found
    if entity.type in _HUMAN_DEFAULT_TYPES:
        return ["human"]
    if entity.type in _NOT_SPECIES_TYPES:
        return ["not_species_specific"]
    if entity.type in {"gene", "protein", "variant", "disease", "phenotype"} and (
        entity.identifiers.get("hgnc") or entity.identifiers.get("mondo") or entity.identifiers.get("hp")
    ):
        return ["human"]
    return ["species_unknown"]


def _group_keys(entity: CuratedEntity, group_by: str) -> list[str]:
    if group_by == "species":
        return species_of(entity)
    if group_by == "source":
        return sorted({s["source"] for s in entity.sources}) or ["unknown_source"]
    return [_TYPE_TO_GROUP.get(entity.type, "other")]


# ---------------------------------------------------------------- cells
def flatten(value: Any) -> Any:
    """A cell value: scalars kept, lists joined with '; ', objects as 'key: value'."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_CELL_CHARS else value[:MAX_CELL_CHARS] + " ..."
    if isinstance(value, dict):
        text = "; ".join(f"{k}: {flatten(v)}" for k, v in value.items() if v not in (None, "", [], {}))
        return flatten(text)
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(", ".join(f"{k}={flatten(v)}" for k, v in item.items() if v not in (None, "", [], {})))
            else:
                parts.append(str(flatten(item)))
        sep = " | " if any(isinstance(i, dict) for i in value) else "; "
        return flatten(sep.join(p for p in parts if p))
    return flatten(str(value))


def _table(key: str, title: str, entities: list[CuratedEntity], group_by: str) -> GroupTable:
    present_ids: dict[str, int] = {}
    field_counts: dict[str, int] = {}
    for entity in entities:
        for k in entity.identifiers:
            present_ids[k] = present_ids.get(k, 0) + 1
        for k, v in entity.fields.items():
            if v not in (None, "", [], {}):
                field_counts[k] = field_counts.get(k, 0) + 1
    order = _ID_ORDER.get(key, ())
    id_cols = [k for k in order if k in present_ids] + sorted(
        (k for k in present_ids if k not in order), key=lambda k: -present_ids[k]
    )
    field_cols = sorted(field_counts, key=lambda k: (-field_counts[k], k))[:MAX_FIELD_COLUMNS]
    has_authors = any(e.authors for e in entities)
    columns = ["entity", "title", "date"]
    if group_by != "type":
        columns.append("type")
    if group_by != "species":
        columns.append("species")
    columns += ["quality_grade", "quality_score", "quality_flags", "confirmed_by_databases"]
    columns += [f"id_{k}" for k in id_cols] + field_cols
    if has_authors:
        columns.append("authors")
    columns += ["summary", "conflicts", "sources", "source_urls"]

    rows = []
    for entity in entities:
        ev = entity.evaluation
        row: dict[str, Any] = {
            "entity": entity.entity_id,
            "title": entity.title,
            "date": entity.date,
            "type": entity.type,
            "species": "; ".join(species_of(entity)),
            "quality_grade": ev.grade,
            "quality_score": ev.score,
            "quality_flags": "; ".join(ev.flags) or None,
            "confirmed_by_databases": len(ev.corroborated_by),
            "authors": "; ".join(entity.authors) or None,
            "summary": flatten(entity.summary),
            "conflicts": "; ".join(
                f"{k}: " + " vs ".join(f"{o['value']} ({', '.join(o['sources'])})" for o in opts)
                for k, opts in entity.conflicts.items()
            ) or None,
            "sources": "; ".join(f"{s['source']}:{s['id']}" for s in entity.sources),
            "source_urls": "; ".join(s["url"] for s in entity.sources if s.get("url")) or None,
        }
        for k in id_cols:
            row[f"id_{k}"] = entity.identifiers.get(k)
        for k in field_cols:
            row[k] = flatten(entity.fields.get(k))
        rows.append(row)
    return GroupTable(key=key, title=title, columns=columns, rows=rows)


def build_package(records: list[Record], group_by: GroupBy = "type") -> DataPackage:
    curated = curate(records)
    buckets: dict[str, list[CuratedEntity]] = {}
    for entity in curated.entities:
        for key in _group_keys(entity, group_by):
            buckets.setdefault(key, []).append(entity)
    titles = SPECIES_TITLES if group_by == "species" else GROUP_TITLES if group_by == "type" else {}
    preferred = list(titles) if titles else sorted(buckets)
    keys = [k for k in preferred if k in buckets] + sorted(k for k in buckets if k not in preferred)
    tables = [_table(k, titles.get(k, k), buckets[k], group_by) for k in keys]
    stats = dict(curated.stats)
    stats["tables"] = {t.key: len(t.rows) for t in tables}
    return DataPackage(group_by=group_by, tables=tables, stats=stats, entities=curated.entities)


# ---------------------------------------------------------------- files
@dataclass(frozen=True)
class SavedPackage:
    export_id: str
    name: str
    folder: Path
    files: list[dict[str, Any]]  # [{"file", "group", "title", "rows", "columns"}]
    n_rows: int

    def url(self, filename: str) -> str:
        return f"/api/exports/{self.export_id}/{filename}"


def package_folder_name(name: str, export_id: str) -> str:
    slug = _FILE_SAFE.sub("_", name).strip("_")[:40] or "mosaic_export"
    return f"{slug}__{export_id}"


def _csv_bytes(table: GroupTable) -> bytes:
    import csv

    from .agent.exports import csv_cell  # formula-injection-safe cells (lazy: avoids an import cycle)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow([csv_cell(row.get(c)) for c in table.columns])
    return buffer.getvalue().encode("utf-8-sig")  # BOM: Excel detects UTF-8


def _xlsx_value(value: Any) -> Any:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return ILLEGAL_CHARACTERS_RE.sub("", str(value)) or None


def _styled_sheet(wb: Any, title: str, columns: list[str], rows: list[list[Any]], used: set[str]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    sheet_name = re.sub(r"[\[\]:*?/\\]", "_", title)[:31]
    base, n = sheet_name, 2
    while sheet_name in used:
        sheet_name = f"{base[:28]}_{n}"
        n += 1
    used.add(sheet_name)
    ws = wb.create_sheet(sheet_name)
    ws.append([_xlsx_value(c) for c in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DCE6F8")
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    for row in rows:
        ws.append([_xlsx_value(v) for v in row])
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"  # text, never a formula
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    for idx, column in enumerate(columns, start=1):
        longest = max([len(str(column))] + [len(str(r[idx - 1] if idx - 1 < len(r) else "") or "") for r in rows[:200]])
        ws.column_dimensions[get_column_letter(idx)].width = max(10, min(60, longest + 2))


def _xlsx_bytes(package: DataPackage, readme_rows: list[list[str]], data_files: Any = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    info = wb.active
    info.title = "README"
    for row in readme_rows:
        info.append([_xlsx_value(v) for v in row])
    info.column_dimensions["A"].width = 28
    info.column_dimensions["B"].width = 110
    info["A1"].font = Font(bold=True, size=13)
    used: set[str] = {"README"}
    for table in package.tables:
        _styled_sheet(wb, table.key, table.columns, [[row.get(c) for c in table.columns] for row in table.rows], used)
    if data_files is not None and data_files.rows:
        from .datafiles import FILE_COLUMNS

        _styled_sheet(wb, "data_files", FILE_COLUMNS, [[r.get(c) for c in FILE_COLUMNS] for r in data_files.rows], used)
        for sheet, header, rows in data_files.previews:
            _styled_sheet(wb, sheet, header, rows, used)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


_URL = re.compile(r"^https?://\S+$")


def content_metrics(package: DataPackage) -> dict[str, Any]:
    """How much of the package is actual data, versus identifiers and links (provenance)."""
    provenance_cols = {"entity", "sources", "source_urls", "quality_grade", "quality_score", "quality_flags",
                       "confirmed_by_databases", "conflicts", "species", "type"}
    data = ids = links = 0
    data_columns = 0
    for table in package.tables:
        cols = [c for c in table.columns if c not in provenance_cols]
        data_columns += sum(1 for c in cols if not c.startswith("id_"))
        for row in table.rows:
            for c in cols:
                v = row.get(c)
                if v in (None, ""):
                    continue
                if c.startswith("id_"):
                    ids += 1
                elif isinstance(v, str) and all(_URL.match(part.strip()) for part in v.split(";") if part.strip()):
                    links += 1
                else:
                    data += 1
    total = data + ids + links
    return {
        "data_cells": data,
        "identifier_cells": ids,
        "link_only_cells": links,
        "data_share_pct": round(100 * data / total, 1) if total else 0.0,
        "data_columns": data_columns,
    }


def _readme(package: DataPackage, *, name: str, description: str | None, query: str | None,
            created_at: str, files: list[dict[str, Any]], data_files: Any = None,
            metrics: dict[str, Any] | None = None, notes: list[str] | None = None) -> tuple[str, list[list[str]]]:
    st = package.stats
    lines = [
        f"Mosaic data package: {name}",
        "",
        f"Created: {created_at} (UTC)",
    ]
    if query:
        lines.append(f"Query: {query}")
    if description:
        lines.append(f"Description: {description}")
    lines += [
        f"Split by: {package.group_by}",
        "",
        f"{st.get('records_in', 0)} records from the databases were cleaned and merged into "
        f"{st.get('entities_out', 0)} entities ({st.get('duplicates_merged', 0)} duplicates merged, "
        f"{st.get('multi_source_entities', 0)} confirmed by 2+ databases).",
        "Grades: " + ", ".join(f"{g}: {n}" for g, n in (st.get("grades") or {}).items()),
    ]
    if metrics:
        lines.append(
            f"Content: {metrics['data_cells']} data values in {metrics['data_columns']} data columns, "
            f"{metrics['identifier_cells']} identifiers, {metrics['link_only_cells']} link-only cells "
            f"({metrics['data_share_pct']}% of filled cells are data)."
        )
    for note in notes or []:
        lines.append(f"Note: {note}")
    lines += ["", "Files:"]
    for f in files:
        lines.append(f"  {f['file']:<34} {f['rows']:>5} rows  {f['title']}")
    if data_files is not None and data_files.rows:
        included = sum(1 for r in data_files.rows if r.get("included_in_package"))
        lines.append(f"  {'data_files.csv':<34} {len(data_files.rows):>5} rows  every file of every dataset (name, size, format, download URL)")
        lines.append(f"  {'files/':<34} {included:>5} files actual data files downloaded from the repositories (small open tables)")
    lines += [
        "  mosaic_data.xlsx                   all tables as Excel sheets (opens directly in Excel)",
        "  manifest.json                      machine-readable description of this package",
        "",
        "Columns:",
        "  entity                 Mosaic entity number within this package",
        "  quality_grade / score  A (>=80) B (>=60) C (>=40) D: source reliability, agreement across",
        "                         databases, completeness, identifier coverage, minus conflicts",
        "  quality_flags          warnings: retracted, preprint, terminated trial, restricted access,",
        "                         FAERS (not causal), unreviewed entry, sources disagree ...",
        "  confirmed_by_databases number of databases reporting the same entity",
        "  id_*                   identifiers (DOI, PMID, NCT, ChEMBL, UniProt ...), normalized",
        "  other columns          cleaned data fields from the full records; the first source supplied them",
        "  sources / source_urls  provenance: database:record-id and links",
        "",
        "Cleaning applied: " + ("; ".join(st.get("cleaning") or []) or "none needed"),
    ]
    rows = [[lines[0], ""]] + [[""], ["Created (UTC)", created_at]]
    if query:
        rows.append(["Query", query])
    if description:
        rows.append(["Description", description])
    rows += [["Split by", package.group_by], ["Records in", str(st.get("records_in", 0))],
             ["Entities", str(st.get("entities_out", 0))], ["Duplicates merged", str(st.get("duplicates_merged", 0))],
             ["Grades", ", ".join(f"{g}: {n}" for g, n in (st.get("grades") or {}).items())]]
    if metrics:
        rows.append(["Content", f"{metrics['data_cells']} data values, {metrics['identifier_cells']} identifiers, "
                                f"{metrics['link_only_cells']} link-only cells ({metrics['data_share_pct']}% data)"])
    for note in notes or []:
        rows.append(["Note", note])
    rows += [[""], ["Sheet", "Contents"]]
    rows += [[f["group"][:31], f"{f['title']} ({f['rows']} rows)"] for f in files]
    if data_files is not None and data_files.rows:
        rows.append(["data_files", f"Every file of every dataset ({len(data_files.rows)} rows)"])
        rows += [[sheet, f"Preview of downloaded file: {next((r['file_name'] for r in data_files.rows if r.get('preview_sheet') == sheet), '')}"]
                 for sheet, _, _ in data_files.previews]
    rows += [[""], ["quality_grade", "A (>=80) B (>=60) C (>=40) D; see quality_flags for warnings"]]
    return "\r\n".join(lines) + "\r\n", rows


def write_package(
    exports_dir: Path,
    package: DataPackage,
    *,
    name: str,
    description: str | None = None,
    query: str | None = None,
    session_id: str | None = None,
    data_files: Any = None,
    notes: list[str] | None = None,
) -> SavedPackage:
    """Write the package folder. ``data_files`` is a ``mosaic.datafiles.DataFilesResult`` (optional)."""
    export_id = uuid.uuid4().hex
    folder = exports_dir / package_folder_name(name, export_id)
    folder.mkdir(parents=True, exist_ok=False)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    notes = list(notes or []) + list(getattr(data_files, "notes", None) or [])

    contents: dict[str, bytes] = {}
    files = []
    for table in package.tables:
        contents[table.filename] = _csv_bytes(table)
        files.append({"file": table.filename, "group": table.key, "title": table.title,
                      "rows": len(table.rows), "columns": table.columns})
    if data_files is not None and data_files.rows:
        from .datafiles import FILE_COLUMNS

        inventory = GroupTable(key="data_files", title="Dataset files (actual data: names, sizes, formats, downloads)",
                               columns=list(FILE_COLUMNS), rows=data_files.rows)
        contents[inventory.filename] = _csv_bytes(inventory)
        files.append({"file": inventory.filename, "group": "data_files", "title": inventory.title,
                      "rows": len(inventory.rows), "columns": inventory.columns})
    metrics = content_metrics(package)
    package.stats["content"] = metrics
    readme_text, readme_rows = _readme(package, name=name, description=description, query=query,
                                       created_at=created_at, files=files, data_files=data_files,
                                       metrics=metrics, notes=notes)
    contents["README.txt"] = readme_text.encode("utf-8-sig")
    contents["mosaic_data.xlsx"] = _xlsx_bytes(package, readme_rows, data_files)
    blobs = dict(getattr(data_files, "blobs", None) or {})
    manifest = {
        "export_id": export_id,
        "kind": "package",
        "name": name,
        "description": description,
        "query": query,
        "group_by": package.group_by,
        "created_at": created_at,
        "session_id": session_id,
        "stats": package.stats,
        "notes": notes,
        "files": files,
        "data_files_included": sorted(blobs),
    }
    contents["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    for filename, data in contents.items():
        (folder / filename).write_bytes(data)
    for rel, data in blobs.items():
        target = (folder / rel).resolve()
        if folder.resolve() not in target.parents:  # names are sanitized; this is defence in depth
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:  # e.g. a path still too long for the OS: the ZIP keeps the file
            notes.append(f"{rel} could not be written to the folder (it is in the ZIP)")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in contents.items():
            archive.writestr(filename, data)
        for rel, data in blobs.items():
            archive.writestr(rel, data)
    (folder / "mosaic_data.zip").write_bytes(zip_buffer.getvalue())
    return SavedPackage(
        export_id=export_id,
        name=name,
        folder=folder,
        files=files,
        n_rows=sum(f["rows"] for f in files if f["group"] != "data_files"),
    )
