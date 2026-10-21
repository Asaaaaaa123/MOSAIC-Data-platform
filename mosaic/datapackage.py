"""Data packages: the real, cleaned data behind a search or conversation, split into several tables.

``build_package(records, group_by=...)`` curates the records (clean, merge across databases,
grade; see ``mosaic.curation``) and splits the entities into one table per group:

- ``group_by="type"`` (default): papers, clinical_trials, projects_studies, datasets_repositories,
  drugs_compounds, safety_regulatory, genes_proteins, variants_associations, diseases_phenotypes,
  pathways_ontology, bioactivity_assays, other
- ``group_by="species"``: human, mouse, rat, zebrafish, fly, worm, yeast, frog, bird, plant,
  primate, other_animal, microbe, not_species_specific, species_unknown (an entity studied in
  several species appears in each species table)
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
SPECIES_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("human", ("homo sapiens", "human", "humans", "taxon:9606", "ncbitaxon:9606", "9606", "patient", "patients")),
    ("mouse", ("mus musculus", "mouse", "mice", "murine", "10090")),
    ("rat", ("rattus", "rat", "rats", "10116")),
    ("zebrafish", ("danio rerio", "zebrafish", "7955")),
    ("fly", ("drosophila", "fruit fly", "7227")),
    ("worm", ("caenorhabditis", "c. elegans", "nematode", "6239")),
    ("yeast", ("saccharomyces", "schizosaccharomyces", "yeast", "4932", "559292")),
    ("frog", ("xenopus", "frog", "8355", "8364")),
    ("bird", ("gallus", "chicken", "chickens", "taeniopygia", "zebra finch", "aves", "birds", "bird", "9031")),
    ("plant", ("arabidopsis", "oryza", "rice", "zea mays", "maize", "nicotiana", "tobacco", "solanum", "tomato",
               "glycine max", "soybean", "triticum", "wheat", "plants", "plant", "viridiplantae", "3702")),
    ("primate", ("macaca", "rhesus", "macaque", "pan troglodytes", "chimpanzee", "marmoset", "callithrix", "primates")),
    ("other_animal", ("sus scrofa", "pig", "swine", "canis", "dog", "dogs", "bos taurus", "cattle", "ovis", "sheep",
                      "felis", "cat", "rabbit", "oryctolagus", "hamster", "guinea pig", "ferret", "monkey")),
    ("microbe", ("bacteria", "escherichia", "e. coli", "mycobacterium", "staphylococcus", "virus", "viruses", "sars-cov",
                 "influenza", "hiv", "plasmodium", "fungi", "candida")),
]
SPECIES_TITLES = {
    "human": "Human", "mouse": "Mouse", "rat": "Rat", "zebrafish": "Zebrafish", "fly": "Fruit fly (Drosophila)",
    "worm": "Worm (C. elegans)", "yeast": "Yeast", "frog": "Frog (Xenopus)", "bird": "Bird (chicken, zebra finch ...)",
    "plant": "Plant", "primate": "Non-human primate", "other_animal": "Other animal", "microbe": "Microbe / virus",
    "not_species_specific": "Not species-specific (drugs, compounds ...)", "species_unknown": "Species not stated",
}
_SPECIES_FIELDS = ("organism", "organisms", "species", "taxon", "taxonomy", "organism_name", "scientific_name",
                   "source_organism", "host", "host_organism", "model_organism", "organism_scientific_name")
# Only MeSH (whose check tags "Humans", "Mice" ... are curated), not free keywords like "plant extract".
_MESH_FIELDS = ("mesh", "mesh_terms", "mesh_headings")
# Free text (title / abstract) is noisier than organism fields: skip buckets and words that mislead
# there ("plant-derived", "virus-like", "cat" ...); Latin names and lab-animal words are reliable.
_TEXT_BUCKETS_SKIP = {"microbe", "other_animal"}
_TEXT_WORDS_SKIP = {"plant", "plants", "rice", "tobacco", "tomato", "wheat", "soybean", "bird", "birds", "aves",
                    "monkey", "primates", "9606", "10090", "10116", "7955", "7227", "6239", "4932", "559292", "8355",
                    "8364", "9031", "3702"}
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
    slug = _FILE_SAFE.sub("_", name).strip("_")[:60] or "mosaic_export"
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


def _xlsx_bytes(package: DataPackage, name: str, readme_rows: list[list[str]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    info = wb.active
    info.title = "README"
    for row in readme_rows:
        info.append([_xlsx_value(v) for v in row])
    info.column_dimensions["A"].width = 28
    info.column_dimensions["B"].width = 110
    info["A1"].font = Font(bold=True, size=13)
    header_fill = PatternFill("solid", fgColor="DCE6F8")
    used: set[str] = {"README"}
    for table in package.tables:
        sheet_name = re.sub(r"[\[\]:*?/\\]", "_", table.key)[:31]
        base, n = sheet_name, 2
        while sheet_name in used:
            sheet_name = f"{base[:28]}_{n}"
            n += 1
        used.add(sheet_name)
        ws = wb.create_sheet(sheet_name)
        ws.append(table.columns)
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        for row in table.rows:
            ws.append([_xlsx_value(row.get(c)) for c in table.columns])
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.data_type = "s"  # text, never a formula
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = ws.dimensions
        for idx, column in enumerate(table.columns, start=1):
            longest = max([len(str(column))] + [len(str(r.get(column) or "")) for r in table.rows[:200]])
            ws.column_dimensions[get_column_letter(idx)].width = max(10, min(60, longest + 2))
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _readme(package: DataPackage, *, name: str, description: str | None, query: str | None,
            created_at: str, files: list[dict[str, Any]]) -> tuple[str, list[list[str]]]:
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
        "",
        "Files:",
    ]
    for f in files:
        lines.append(f"  {f['file']:<34} {f['rows']:>5} rows  {f['title']}")
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
        "  other columns          cleaned data fields; the first source listed supplied them",
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
             ["Grades", ", ".join(f"{g}: {n}" for g, n in (st.get("grades") or {}).items())], [""], ["Sheet", "Contents"]]
    rows += [[f["group"][:31], f"{f['title']} ({f['rows']} rows)"] for f in files]
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
) -> SavedPackage:
    export_id = uuid.uuid4().hex
    folder = exports_dir / package_folder_name(name, export_id)
    folder.mkdir(parents=True, exist_ok=False)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    contents: dict[str, bytes] = {}
    files = []
    for table in package.tables:
        contents[table.filename] = _csv_bytes(table)
        files.append({"file": table.filename, "group": table.key, "title": table.title,
                      "rows": len(table.rows), "columns": table.columns})
    readme_text, readme_rows = _readme(package, name=name, description=description, query=query,
                                       created_at=created_at, files=files)
    contents["README.txt"] = readme_text.encode("utf-8-sig")
    contents["mosaic_data.xlsx"] = _xlsx_bytes(package, name, readme_rows)
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
        "files": files,
    }
    contents["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    for filename, data in contents.items():
        (folder / filename).write_bytes(data)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in contents.items():
            archive.writestr(filename, data)
    (folder / "mosaic_data.zip").write_bytes(zip_buffer.getvalue())
    return SavedPackage(
        export_id=export_id,
        name=name,
        folder=folder,
        files=files,
        n_rows=sum(f["rows"] for f in files),
    )
