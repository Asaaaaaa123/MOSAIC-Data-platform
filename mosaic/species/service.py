"""Run a cross-species comparison end to end (shared by the API, the agent and the CLI).

``run_comparison`` = engine (``compare.compare_species``) + optional Claude extraction of dose /
effect from abstracts + a data package split by species (one CSV per species, Excel, ZIP) + saving
the comparison JSON for the dashboard.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models import Record
from .models import Paper, PaperExtractor, SpeciesComparison
from .store import save_comparison

_EXTRACTION_FIELDS = [
    ("model", "string", "Experimental system: e.g. 'NCI-H358 KRAS G12C xenograft in nude mice', 'healthy volunteers', "
                        "'phase 3 NSCLC patients'. Empty if not stated."),
    ("dose", "string", "Dose and schedule exactly as reported, e.g. '100 mg/kg p.o. daily', '960 mg once daily'."),
    ("dose_mg_kg", "number", "The dose in mg per kg body weight when it is reported that way (number only), else empty."),
    ("effect", "string", "The main quantitative result as reported, e.g. 'tumor growth inhibition 86%', "
                         "'objective response rate 37.1%', 'oral bioavailability 47%'."),
    ("effect_value", "number", "The number in the main result (e.g. 86 for 86%), else empty."),
    ("effect_unit", "string", "Unit of effect_value, e.g. '%', 'months', 'ng/mL'."),
    ("evidence", "string", "One of: clinical, in_vivo, in_vitro, computational, review."),
    ("finding", "string", "One sentence: what the study found about the compound in this species."),
]
_EVIDENCE = {"clinical", "in_vivo", "in_vitro", "computational"}


def claude_paper_extractor(settings: Settings, audit: Any = None, session_id: str | None = None) -> PaperExtractor:
    """A PaperExtractor that asks Claude (structured outputs) for model / dose / effect per abstract."""
    import anthropic

    from ..agent.extraction import FieldSpec, SourceRecord, StructuredExtractor

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else anthropic.AsyncAnthropic()
    extractor = StructuredExtractor.from_settings(
        settings, lambda: client, fallbacks=(settings.mosaic_fallbacks or "default") != "off", audit=audit
    )
    fields = [FieldSpec(name=n, type=t, description=d) for n, t, d in _EXTRACTION_FIELDS]

    async def extract(items: list[tuple[Paper, Record]]) -> list[Paper]:
        usable = [(p, r) for p, r in items if r.summary]
        if not usable:
            return [p for p, _ in items]
        table = await extractor.extract(
            [SourceRecord(result_id="species", record=r) for _, r in usable],
            fields,
            "Each record is a paper abstract about a drug or chemical compound studied in the species named in the "
            "record attributes. Extract only what the abstract states; leave fields empty rather than guessing.",
            session_id=session_id,
        )
        by_id = {row.get("record_id"): row for row in table.rows}
        out: list[Paper] = []
        for paper, record in items:
            row = by_id.get(record.id)
            if not row:
                out.append(paper)
                continue
            update = {k: row.get(k) for k, _, _ in _EXTRACTION_FIELDS if row.get(k) not in (None, "")}
            evidence = str(update.pop("evidence", "") or "").strip().lower()
            if evidence in _EVIDENCE:
                update["evidence"] = evidence
            for key in ("dose_mg_kg", "effect_value"):
                if key in update:
                    try:
                        update[key] = float(update[key])
                    except (TypeError, ValueError):
                        update.pop(key)
            out.append(paper.model_copy(update=update))
        return out

    return extract


async def run_comparison(
    registry: Any,
    settings: Settings,
    query: str,
    *,
    species: list[str] | None = None,
    max_papers: int = 5,
    extract_with_claude: bool = False,
    session_id: str | None = None,
) -> SpeciesComparison:
    from ..datapackage import build_package, write_package
    from .compare import compare_species

    extractor = claude_paper_extractor(settings, registry.audit, session_id) if extract_with_claude else None
    comparison = await compare_species(
        registry, query, species=species, max_papers=max_papers, extractor=extractor, session_id=session_id
    )
    if comparison.records:
        name = f"Cross-species {comparison.compound.name or query}"
        package = build_package(comparison.records, "species")
        saved = write_package(
            settings.exports_dir, package, name=name, query=query, session_id=session_id,
            description=f"Evidence and ortholog data for {comparison.compound.name or query}, one file per species",
        )
        registry.audit.record_export(
            name=name, path=str(saved.folder), n_rows=saved.n_rows,
            columns=[f["file"] for f in saved.files], session_id=session_id,
        )
        base = f"/api/exports/{saved.export_id}"
        comparison.package = {
            "kind": "package",
            "export_id": saved.export_id,
            "name": saved.name,
            "group_by": "species",
            "n_rows": saved.n_rows,
            "folder": str(saved.folder),
            "files": [{**{k: f[k] for k in ("file", "group", "title", "rows")}, "url": f"{base}/{f['file']}",
                       "preview_url": f"{base}/{f['file']}/preview"} for f in saved.files],
            "xlsx_url": f"{base}/mosaic_data.xlsx",
            "zip_url": f"{base}/mosaic_data.zip",
        }
    save_comparison(settings, comparison)
    return comparison


def event_summary(comparison: SpeciesComparison) -> dict[str, Any]:
    """Small payload for the chat card (SpeciesComparisonReady.summary)."""
    return {
        "compound": comparison.compound.name or comparison.query,
        "kind": comparison.compound.kind,
        "max_phase": comparison.compound.max_phase,
        "targets": [t.gene_symbol for t in comparison.targets],
        "species": [
            {k: getattr(s, k) for k in ("species", "label", "label_zh", "group", "mean_identity_pct", "n_papers",
                                        "n_measurements", "evidence_level")}
            for s in comparison.species
        ],
        "package": comparison.package,
    }
