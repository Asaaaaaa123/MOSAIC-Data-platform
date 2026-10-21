"""openFDA connector: FDA drug labels (SPL), FAERS adverse events, Drugs@FDA approvals,
drug recalls (enforcement reports) and the NDC directory.

Docs: https://open.fda.gov/apis/  (query syntax: https://open.fda.gov/apis/query-syntax/)
Auth: optional key (``OPENFDA_API_KEY``) sent as the ``api_key`` query parameter, which the http
layer redacts. Without a key openFDA allows 240 requests/minute but only 1,000/day per IP; with a
key 240/minute and 120,000/day. We stay at 3.5 req/s.

Quirks:
- ``search=field:term AND field:"phrase"``; ranges ``field:[20200101 TO 20231231]``; ``count=field.exact``
  returns ``[{term, count}]`` (max 1000 buckets); ``limit`` <= 1000 and ``skip`` <= 25000.
- "No matches found" is an HTTP 404 with ``{"error": {"code": "NOT_FOUND"}}``: mapped to empty results.
  Malformed search expressions return HTTP 400; surfaced as a note so the caller can fix the query.
- The ``openfda`` harmonization block (rxcui, unii, spl_set_id, NDCs, application numbers,
  pharmacologic classes) is missing on many unapproved/OTC labels and on older FAERS drugs.
- Label sections are lists of plain-text strings that start with the section heading.

Identifiers: ``spl_set_id``, ``rxcui`` (first product-level RxCUI), ``unii`` (only for single-ingredient
products so combination products do not link to a single drug), ``ndc``, ``fda_application``.
FAERS case reports carry no linking identifiers (they are reports *about* drugs; drug ids are in
attributes).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator

from ..http import HttpResponse
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

API = "https://api.fda.gov"
DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid="
DRUGSFDA_URL = "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo="
MAX_SKIP = 25000

_DATE_RE = re.compile(r"^\d{4}-?\d{2}-?\d{2}$")
_FIELD_QUERY_RE = re.compile(r"[A-Za-z_.]+:")

_SEX = {"0": "unknown", "1": "male", "2": "female"}
_OUTCOME = {
    "1": "recovered/resolved",
    "2": "recovering/resolving",
    "3": "not recovered/not resolved",
    "4": "recovered with sequelae",
    "5": "fatal",
    "6": "unknown",
}
_SERIOUS = {"1": "serious", "2": "non-serious"}
_ROLE = {"1": "suspect", "2": "concomitant", "3": "interacting"}
_AGE_GROUP = {"1": "neonate", "2": "infant", "3": "child", "4": "adolescent", "5": "adult", "6": "elderly"}
_QUALIFICATION = {
    "1": "physician",
    "2": "pharmacist",
    "3": "other health professional",
    "4": "lawyer",
    "5": "consumer/non-health professional",
}
_AGE_UNIT = {"800": "decades", "801": "years", "802": "months", "803": "weeks", "804": "days", "805": "hours"}

_COUNT_FIELDS: dict[str, tuple[str, dict[str, str] | None]] = {
    "reaction": ("patient.reaction.reactionmeddrapt.exact", None),
    "outcome": ("patient.reaction.reactionoutcome", _OUTCOME),
    "serious": ("serious", _SERIOUS),
    "sex": ("patient.patientsex", _SEX),
    "age_group": ("patient.patientagegroup", _AGE_GROUP),
    "country": ("occurcountry.exact", None),
    "indication": ("patient.drug.drugindication.exact", None),
    "co_reported_drug": ("patient.drug.openfda.generic_name.exact", None),
    "reporter": ("primarysource.qualification", _QUALIFICATION),
    "year": ("receivedate", None),
}

_LABEL_SECTIONS = (
    ("boxed_warning", "boxed_warning"),
    ("indications_and_usage", "indications_and_usage"),
    ("dosage_and_administration", "dosage_and_administration"),
    ("dosage_forms_and_strengths", "dosage_forms_and_strengths"),
    ("contraindications", "contraindications"),
    ("warnings_and_cautions", "warnings_and_cautions"),
    ("warnings", "warnings"),
    ("adverse_reactions", "adverse_reactions"),
    ("drug_interactions", "drug_interactions"),
    ("use_in_specific_populations", "use_in_specific_populations"),
    ("pregnancy", "pregnancy"),
    ("mechanism_of_action", "mechanism_of_action"),
    ("pharmacodynamics", "pharmacodynamics"),
    ("pharmacokinetics", "pharmacokinetics"),
    ("clinical_studies", "clinical_studies"),
    ("overdosage", "overdosage"),
)


# ---------------------------------------------------------------- params
def _check_date(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not _DATE_RE.match(value):
        raise ValueError("use YYYY-MM-DD or YYYYMMDD")
    return value.replace("-", "")


class _DateRange(ToolParams):
    date_from: str | None = Field(None, description="Earliest date (YYYY-MM-DD)")
    date_to: str | None = Field(None, description="Latest date (YYYY-MM-DD)")

    @field_validator("date_from", "date_to")
    @classmethod
    def _v_dates(cls, value: str | None) -> str | None:
        return _check_date(value)


class SearchLabelsParams(SearchParams):
    query: str = Field(
        description=(
            "Drug name (brand, generic or substance, e.g. 'Keytruda', 'metformin') or topic words for full-text "
            "search (e.g. 'PD-1 melanoma'). Raw openFDA syntax is passed through when it contains field:value, "
            "e.g. 'openfda.pharm_class_epc:\"GLP-1 Receptor Agonist [EPC]\"' or 'boxed_warning:suicidal'"
        )
    )
    product_type: Literal["prescription", "otc", "any"] = Field(
        "any", description="Restrict to HUMAN PRESCRIPTION DRUG or HUMAN OTC DRUG labels"
    )


class GetLabelParams(ToolParams):
    id_type: Literal["set_id", "rxcui", "unii", "ndc", "application_number", "brand_name", "generic_name"] = Field(
        "set_id", description="Type of identifier given in `value`"
    )
    value: str = Field(
        description=(
            "Identifier value: SPL set id (UUID), RxCUI (digits), UNII (e.g. DPT0O3T46P), product NDC "
            "(e.g. 0006-3026), application number (e.g. BLA125514 or NDA209637), or a brand/generic name"
        )
    )
    section_chars: int = Field(1500, ge=200, le=8000, description="Maximum characters kept per label section")
    limit: int = Field(1, ge=1, le=5, description="Number of matching labels to return (newest first)")


class AdverseEventCountsParams(_DateRange):
    drug: str | None = Field(
        None, description="Drug (generic or brand name, e.g. 'semaglutide'); matched against harmonized and reported names"
    )
    reaction: str | None = Field(
        None, description="Restrict to reports with this MedDRA preferred term, e.g. 'pancreatitis'"
    )
    count_by: Literal[
        "reaction",
        "outcome",
        "serious",
        "sex",
        "age_group",
        "country",
        "indication",
        "co_reported_drug",
        "reporter",
        "year",
    ] = Field(
        "reaction",
        description=(
            "What to tally: reaction (MedDRA PT), outcome, serious, sex, age_group, country, indication, "
            "co_reported_drug (generic names of all drugs on the reports; with `reaction` set this ranks drugs "
            "for that reaction), reporter (qualification), year (reports received per year)"
        ),
    )
    serious_only: bool = Field(False, description="Only reports flagged serious")
    limit: int = Field(25, ge=1, le=1000, description="Number of top terms to return")


class SearchAdverseEventsParams(_DateRange):
    drug: str | None = Field(None, description="Drug generic or brand name, e.g. 'pembrolizumab'")
    reaction: str | None = Field(None, description="MedDRA preferred term, e.g. 'myocarditis'")
    serious_only: bool = Field(False, description="Only serious reports")
    fatal_only: bool = Field(False, description="Only reports with death as a seriousness criterion")
    query: str | None = Field(
        None, description="Extra raw openFDA expression ANDed in, e.g. 'occurcountry:\"JP\"' or 'patient.patientsex:2'"
    )
    limit: int = Field(10, ge=1, le=100, description="Number of case reports to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


class DrugApprovalsParams(ToolParams):
    drug: str | None = Field(None, description="Brand, generic or active-ingredient name, e.g. 'semaglutide'")
    application_number: str | None = Field(None, description="Application number, e.g. NDA209637, BLA125514, ANDA076543")
    sponsor: str | None = Field(None, description="Sponsor (applicant) name, e.g. 'NOVO'")
    max_submissions: int = Field(8, ge=0, le=100, description="Most recent submissions to list per application")
    limit: int = Field(10, ge=1, le=100, description="Number of applications to return")
    cursor: str | None = Field(None, description="Pagination cursor")


class DrugRecallsParams(_DateRange):
    drug: str | None = Field(None, description="Drug name matched in harmonized names and the product description")
    reason: str | None = Field(None, description="Words in the reason for recall, e.g. 'nitrosamine' or 'sterility'")
    firm: str | None = Field(None, description="Recalling firm name")
    classification: Literal["Class I", "Class II", "Class III"] | None = Field(
        None, description="Class I = most serious (reasonable probability of serious harm)"
    )
    status: Literal["Ongoing", "Completed", "Terminated", "Pending"] | None = Field(None, description="Recall status")
    limit: int = Field(10, ge=1, le=100, description="Number of recalls to return")
    cursor: str | None = Field(None, description="Pagination cursor")


class NdcLookupParams(ToolParams):
    ndc: str | None = Field(
        None,
        description="Product NDC (0169-4132), package NDC (0169-4132-12) or 10/11-digit NDC without dashes",
    )
    name: str | None = Field(None, description="Brand or generic name, e.g. 'Ozempic'")
    limit: int = Field(10, ge=1, le=100, description="Number of products to return")
    cursor: str | None = Field(None, description="Pagination cursor")


# ---------------------------------------------------------------- helpers
def _q(value: str) -> str:
    """Quote a user value as an openFDA phrase."""
    return '"' + value.replace('"', " ").replace("\\", " ").strip() + '"'


def _words(value: str) -> str:
    words = [w for w in re.split(r"[\s+]+", re.sub(r'[\\"()\[\]{}:]', " ", value)) if w]
    return "(" + " AND ".join(words) + ")" if len(words) > 1 else (words[0] if words else '""')


def _iso(value: Any) -> str | None:
    text = str(value or "").strip()
    if re.match(r"^\d{8}$", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.match(r"^\d{6}$", text):
        return f"{text[:4]}-{text[4:6]}"
    if re.match(r"^\d{4}$", text):
        return text
    return None


def _range(field: str, date_from: str | None, date_to: str | None) -> str | None:
    if not (date_from or date_to):
        return None
    return f"{field}:[{date_from or '19000101'} TO {date_to or '29991231'}]"


def _and(*parts: str | None) -> str:
    items = [p for p in parts if p]
    if len(items) == 1:
        return items[0]
    return " AND ".join(f"({p})" if " OR " in p else p for p in items)


_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\s*)?(?:INDICATIONS (?:AND|&) USAGE|DOSAGE AND ADMINISTRATION|DOSAGE FORMS AND STRENGTHS|"
    r"CONTRAINDICATIONS|WARNINGS AND PRECAUTIONS|WARNINGS|ADVERSE REACTIONS|DRUG INTERACTIONS|"
    r"USE IN SPECIFIC POPULATIONS|PREGNANCY|MECHANISM OF ACTION|PHARMACODYNAMICS|PHARMACOKINETICS|"
    r"CLINICAL STUDIES|OVERDOSAGE|PURPOSE|USES)\b[\s:.]*",
    re.IGNORECASE,
)


def _excerpt(value: Any, chars: int, strip_heading: bool = False) -> str | None:
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v)
    text = strip_html(value) if isinstance(value, str) else None
    if text and strip_heading:
        text = _HEADING_RE.sub("", text, count=1).strip()
    if not text:
        return None
    return text if len(text) <= chars else text[:chars].rstrip() + " ..."


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def _dedupe(items: Any) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _openfda_ids(block: dict[str, Any]) -> dict[str, Any]:
    substances = _list(block.get("substance_name"))
    uniis = _list(block.get("unii"))
    return {
        "rxcui": _list(block.get("rxcui")),
        "unii": uniis[0] if len(uniis) == 1 and len(substances) <= 1 else None,
        "spl_set_id": _list(block.get("spl_set_id")),
        "ndc": _list(block.get("product_ndc")),
        "fda_application": _list(block.get("application_number")),
    }


def _openfda_attrs(block: dict[str, Any]) -> dict[str, Any]:
    rxcuis = _list(block.get("rxcui"))
    ndcs = _list(block.get("product_ndc"))
    return {
        "rxcuis": rxcuis if len(rxcuis) > 1 else None,
        "uniis": _list(block.get("unii")) or None,
        "substances": _list(block.get("substance_name")) or None,
        "application_numbers": _list(block.get("application_number")) or None,
        "product_ndcs": (ndcs[:10] + [f"... (+{len(ndcs) - 10} more)"] if len(ndcs) > 10 else ndcs) or None,
        "routes": _list(block.get("route")) or None,
        "pharm_class_epc": _list(block.get("pharm_class_epc")) or None,
        "pharm_class_moa": _list(block.get("pharm_class_moa")) or None,
    }


def _label_record(item: dict[str, Any], section_chars: int = 600, full: bool = False) -> Record:
    block = item.get("openfda") or {}
    brand = ", ".join(dict.fromkeys(_list(block.get("brand_name"))))
    generic = ", ".join(dict.fromkeys(_list(block.get("generic_name"))))
    title = brand or generic
    if brand and generic and generic.lower() != brand.lower():
        title = f"{brand} ({generic.lower()})"
    if not title:
        title = _excerpt(item.get("spl_product_data_elements"), 120) or f"SPL {item.get('set_id')}"
    set_id = item.get("set_id") or item.get("id") or ""
    ids = _openfda_ids(block)
    ids["spl_set_id"] = set_id or ids["spl_set_id"]
    attributes: dict[str, Any] = {
        "manufacturer": ", ".join(dict.fromkeys(_list(block.get("manufacturer_name")))) or None,
        "product_type": ", ".join(_list(block.get("product_type"))) or None,
        "spl_version": item.get("version"),
        "spl_id": item.get("id"),
        **_openfda_attrs(block),
    }
    if full:
        for key, field in _LABEL_SECTIONS:
            attributes[key] = _excerpt(item.get(field), section_chars, strip_heading=key != "boxed_warning")
    else:
        attributes["boxed_warning"] = _excerpt(item.get("boxed_warning"), 400)
        attributes["warnings_excerpt"] = _excerpt(
            item.get("warnings_and_cautions") or item.get("warnings"), 400, strip_heading=True
        )
        attributes["has_openfda_block"] = bool(block) or None
    summary = _excerpt(
        item.get("indications_and_usage") or item.get("purpose"), section_chars if full else 1200, strip_heading=True
    )
    return Record(
        source="openfda",
        id=set_id,
        type="drug_label",
        title=title,
        url=f"{DAILYMED}{set_id}" if set_id else None,
        identifiers=ids,
        date=_iso(item.get("effective_time")),
        summary=summary,
        attributes=_compact(attributes),
    )


def _event_record(item: dict[str, Any]) -> Record:
    patient = item.get("patient") or {}
    drugs = patient.get("drug") or []
    reactions = patient.get("reaction") or []
    suspects = [d for d in drugs if str(d.get("drugcharacterization")) in ("1", "3")] or drugs[:1]

    def drug_name(d: dict[str, Any]) -> str:
        names = _list((d.get("openfda") or {}).get("generic_name"))
        return (names[0].lower() if names else None) or d.get("medicinalproduct") or "unknown drug"

    seriousness = [
        label
        for key, label in (
            ("seriousnessdeath", "death"),
            ("seriousnesslifethreatening", "life-threatening"),
            ("seriousnesshospitalization", "hospitalization"),
            ("seriousnessdisabling", "disability"),
            ("seriousnesscongenitalanomali", "congenital anomaly"),
            ("seriousnessother", "other serious"),
        )
        if str(item.get(key)) == "1"
    ]
    age = patient.get("patientonsetage")
    unit = _AGE_UNIT.get(str(patient.get("patientonsetageunit")), "")
    reaction_terms = [r.get("reactionmeddrapt") for r in reactions if r.get("reactionmeddrapt")]
    report_id = str(item.get("safetyreportid") or "")
    suspect_names = list(dict.fromkeys(drug_name(d) for d in suspects))
    title = f"{', '.join(suspect_names[:3])}: {', '.join(reaction_terms[:5])}"
    if len(reaction_terms) > 5:
        title += f" (+{len(reaction_terms) - 5} more)"
    suspect_block = (suspects[0].get("openfda") or {}) if suspects else {}
    return Record(
        source="openfda",
        id=report_id,
        type="adverse_event",
        title=title,
        url=None,
        date=_iso(item.get("receivedate")),
        attributes=_compact(
            {
                "report_id": report_id,
                "serious": _SERIOUS.get(str(item.get("serious"))),
                "seriousness": seriousness,
                "report_type": {"1": "spontaneous", "2": "study", "3": "other", "4": "not available"}.get(
                    str(item.get("reporttype"))
                ),
                "reporter": _QUALIFICATION.get(str((item.get("primarysource") or {}).get("qualification"))),
                "country": item.get("occurcountry") or (item.get("primarysource") or {}).get("reportercountry"),
                "sex": _SEX.get(str(patient.get("patientsex"))),
                "age": f"{age} {unit}".strip() if age else None,
                "age_group": _AGE_GROUP.get(str(patient.get("patientagegroup"))),
                "weight_kg": patient.get("patientweight"),
                "reactions": [
                    _compact({"term": r.get("reactionmeddrapt"), "outcome": _OUTCOME.get(str(r.get("reactionoutcome")))})
                    for r in reactions[:20]
                ],
                "drugs": [
                    _compact(
                        {
                            "name": drug_name(d),
                            "reported_as": d.get("medicinalproduct"),
                            "role": _ROLE.get(str(d.get("drugcharacterization"))),
                            "indication": d.get("drugindication"),
                            "dose": d.get("drugdosagetext"),
                            "route_code": d.get("drugadministrationroute"),
                        }
                    )
                    for d in sorted(drugs, key=lambda d: str(d.get("drugcharacterization") or "9"))[:15]
                ],
                "drug_count": len(drugs),
                "suspect_drug_rxcui": _list(suspect_block.get("rxcui"))[:1] or None,
                "suspect_drug_unii": _list(suspect_block.get("unii"))[:1] or None,
                "company_report_number": item.get("companynumb"),
            }
        ),
    )


def _approval_record(item: dict[str, Any], max_submissions: int) -> Record:
    appl = item.get("application_number") or ""
    block = item.get("openfda") or {}
    products = item.get("products") or []
    submissions = item.get("submissions") or []
    brands = list(dict.fromkeys(p.get("brand_name") for p in products if p.get("brand_name")))
    ingredients = list(
        dict.fromkeys(a.get("name") for p in products for a in p.get("active_ingredients") or [] if a.get("name"))
    )
    original = next(
        (
            s
            for s in submissions
            if s.get("submission_type") == "ORIG" and s.get("submission_status") == "AP"
        ),
        None,
    )
    ordered = sorted(submissions, key=lambda s: s.get("submission_status_date") or "", reverse=True)
    title = f"{appl} {' / '.join(brands[:3])}".strip()
    if ingredients:
        title += f" ({', '.join(i.lower() for i in ingredients[:4])})"
    ids = _openfda_ids(block)
    ids["fda_application"] = appl
    if not ids.get("unii") and len(ingredients) > 1:
        ids["unii"] = None
    digits = re.sub(r"\D", "", appl)
    return Record(
        source="openfda",
        id=appl,
        type="drug",
        title=title,
        url=f"{DRUGSFDA_URL}{digits}" if digits else None,
        identifiers=ids,
        date=_iso((original or {}).get("submission_status_date")),
        attributes=_compact(
            {
                "sponsor": item.get("sponsor_name"),
                "application_type": re.sub(r"\d", "", appl) or None,
                "original_approval_date": _iso((original or {}).get("submission_status_date")),
                "active_ingredients": ingredients,
                "products": _dedupe(
                    _compact(
                        {
                            "brand": p.get("brand_name"),
                            "strength": ", ".join(
                                f"{a.get('name')} {a.get('strength')}".strip() for a in p.get("active_ingredients") or []
                            ),
                            "form": p.get("dosage_form"),
                            "route": p.get("route"),
                            "marketing_status": p.get("marketing_status"),
                            "te_code": p.get("te_code"),
                            "reference_drug": p.get("reference_drug"),
                        }
                    )
                    for p in products
                )[:15],
                "submission_count": len(submissions),
                "recent_submissions": [
                    _compact(
                        {
                            "type": f"{s.get('submission_type')}-{s.get('submission_number')}",
                            "status": s.get("submission_status"),
                            "date": _iso(s.get("submission_status_date")),
                            "class": s.get("submission_class_code_description") or s.get("submission_class_code"),
                            "priority": s.get("review_priority"),
                            "documents": [d.get("url") for d in s.get("application_docs") or [] if d.get("url")][:3],
                        }
                    )
                    for s in ordered[:max_submissions]
                ],
                **_openfda_attrs(block),
            }
        ),
    )


def _recall_record(item: dict[str, Any]) -> Record:
    block = item.get("openfda") or {}
    ids = _openfda_ids(block)
    ids.pop("rxcui", None)  # a recall is an event about a product, not the product itself
    ids.pop("unii", None)
    number = item.get("recall_number") or item.get("event_id") or ""
    names = _list(block.get("brand_name")) or _list(block.get("generic_name"))
    title = f"{item.get('classification') or 'Recall'} {number}: " + (
        names[0] if names else (_excerpt(item.get("product_description"), 100) or "")
    )
    return Record(
        source="openfda",
        id=number,
        type="recall",
        title=title.strip(),
        url="https://www.accessdata.fda.gov/scripts/ires/index.cfm",
        identifiers=ids,
        date=_iso(item.get("recall_initiation_date")),
        summary=_excerpt(item.get("reason_for_recall"), 800),
        attributes=_compact(
            {
                "classification": item.get("classification"),
                "status": item.get("status"),
                "recalling_firm": item.get("recalling_firm"),
                "product_description": _excerpt(item.get("product_description"), 400),
                "product_quantity": item.get("product_quantity"),
                "distribution": _excerpt(item.get("distribution_pattern"), 200),
                "voluntary_mandated": item.get("voluntary_mandated"),
                "event_id": item.get("event_id"),
                "report_date": _iso(item.get("report_date")),
                "classification_date": _iso(item.get("center_classification_date")),
                "termination_date": _iso(item.get("termination_date")),
                "country": item.get("country"),
                "rxcuis": _list(block.get("rxcui"))[:5] or None,
                "uniis": _list(block.get("unii")) or None,
                "generic_names": _list(block.get("generic_name")) or None,
            }
        ),
    )


def _ndc_record(item: dict[str, Any]) -> Record:
    block = item.get("openfda") or {}
    ndc = item.get("product_ndc") or ""
    ingredients = item.get("active_ingredients") or []
    strength = ", ".join(f"{a.get('name')} {a.get('strength')}".strip() for a in ingredients)
    brand = item.get("brand_name")
    generic = item.get("generic_name")
    title = " ".join(x for x in (brand or generic, f"({generic.lower()})" if brand and generic else None) if x)
    title = f"{title} {item.get('dosage_form') or ''}".strip()
    ids = _openfda_ids(block)
    ids["ndc"] = ndc
    ids["spl_set_id"] = _list(block.get("spl_set_id")) or None
    ids["fda_application"] = item.get("application_number")
    ids["unii"] = ids["unii"] if len(ingredients) <= 1 else None
    set_id = _list(block.get("spl_set_id"))
    packages = item.get("packaging") or []
    return Record(
        source="openfda",
        id=ndc,
        type="drug",
        title=title or ndc,
        url=f"{DAILYMED}{set_id[0]}" if set_id else None,
        identifiers=ids,
        date=_iso(item.get("marketing_start_date")),
        summary=strength or None,
        attributes=_compact(
            {
                "labeler": item.get("labeler_name"),
                "active_ingredients": [f"{a.get('name')} {a.get('strength')}".strip() for a in ingredients],
                "dosage_form": item.get("dosage_form"),
                "route": item.get("route"),
                "product_type": item.get("product_type"),
                "marketing_category": item.get("marketing_category"),
                "marketing_start_date": _iso(item.get("marketing_start_date")),
                "marketing_end_date": _iso(item.get("marketing_end_date")),
                "dea_schedule": item.get("dea_schedule"),
                "pharm_class": item.get("pharm_class"),
                "packages": [
                    _compact({"package_ndc": p.get("package_ndc"), "description": p.get("description")})
                    for p in packages[:8]
                ],
                "rxcuis": _list(block.get("rxcui"))[:10] or None,
                "uniis": _list(block.get("unii")) or None,
            }
        ),
    )


def _ndc_candidates(raw: str) -> tuple[str, list[str]]:
    """Return (field, candidate dashed NDCs) for a user-supplied NDC."""
    value = raw.strip()
    if "-" in value:
        parts = value.split("-")
        return ("packaging.package_ndc" if len(parts) == 3 else "product_ndc"), [value]
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11:  # 5-4-2 with one zero-padded segment
        lab, prod, pkg = digits[:5], digits[5:9], digits[9:]
        cands = [f"{lab}-{prod}-{pkg}"]
        if lab.startswith("0"):
            cands.append(f"{lab[1:]}-{prod}-{pkg}")
        if prod.startswith("0"):
            cands.append(f"{lab}-{prod[1:]}-{pkg}")
        if pkg.startswith("0"):
            cands.append(f"{lab}-{prod}-{pkg[1:]}")
        return "packaging.package_ndc", cands
    if len(digits) == 10:
        return "packaging.package_ndc", [
            f"{digits[:4]}-{digits[4:8]}-{digits[8:]}",
            f"{digits[:5]}-{digits[5:8]}-{digits[8:]}",
            f"{digits[:5]}-{digits[5:9]}-{digits[9:]}",
        ]
    if len(digits) in (8, 9):
        return "product_ndc", [f"{digits[:4]}-{digits[4:]}", f"{digits[:5]}-{digits[5:]}"]
    return "product_ndc", [value]


def _parse_cursor(cursor: str | None) -> tuple[str | None, int]:
    if not cursor:
        return None, 0
    mode, _, num = cursor.rpartition(":")
    try:
        return (mode or None), max(0, int(num))
    except ValueError:
        return None, 0


# ---------------------------------------------------------------- connector
class OpenFDA(Connector):
    name = "openfda"
    title = "openFDA"
    category = "clinical"
    description = (
        "FDA public datasets: 250k+ structured product labels (indications, boxed warnings, dosing), 20M+ FAERS "
        "adverse event reports, Drugs@FDA approval histories, drug recall enforcement reports and the NDC "
        "directory. Best for US regulatory status, label text and post-marketing safety signals."
    )
    homepage = "https://open.fda.gov/"
    docs_url = "https://open.fda.gov/apis/"
    auth = "optional_api_key"
    rate_limit = 3.5
    health_query = "metformin"

    # ---- plumbing
    async def _get(
        self,
        endpoint: str,
        *,
        search: str | None,
        limit: int | None = None,
        skip: int = 0,
        count: str | None = None,
        sort: str | None = None,
    ) -> tuple[dict[str, Any], HttpResponse, str | None]:
        """GET an openFDA endpoint. Returns (payload, response, problem) where problem is a note."""
        params: dict[str, Any] = {
            "search": search or None,
            "count": count,
            "limit": limit,
            "skip": skip or None,
            "sort": sort,
        }
        key = self.setting("openfda_api_key")
        if key:
            params["api_key"] = key
        response = await self.request(
            "GET", f"{API}/{endpoint}.json", params=params, allow_status=(400, 404), secret_params=("api_key",)
        )
        if response.status == 404:
            return {}, response, None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status == 400:
            message = ((payload or {}).get("error") or {}).get("message") or response.text[:200]
            return {}, response, f"openFDA rejected the query: {message}"
        return (payload if isinstance(payload, dict) else {}), response, None

    @staticmethod
    def _total(payload: dict[str, Any]) -> int | None:
        total = ((payload.get("meta") or {}).get("results") or {}).get("total")
        return int(total) if isinstance(total, int) else None

    @staticmethod
    def _next(skip: int, returned: int, total: int | None, mode: str | None = None) -> str | None:
        nxt = skip + returned
        if not returned or (total is not None and nxt >= total) or nxt > MAX_SKIP:
            return None
        return f"{mode}:{nxt}" if mode else str(nxt)

    def _result(self, tool_name: str, response: HttpResponse, problem: str | None, **kwargs: Any) -> ToolResult:
        notes = list(kwargs.pop("notes", []) or [])
        if problem:
            notes.append(problem)
        return ToolResult(source=self.name, tool=tool_name, request_url=response.url, notes=notes, **kwargs)

    @staticmethod
    def _drug_clause(drug: str, prefix: str = "openfda.") -> str:
        q = _q(drug)
        return " OR ".join(f"{prefix}{f}:{q}" for f in ("generic_name", "brand_name", "substance_name"))

    # ---- tools
    @tool(
        description="""
        Search FDA drug labels (DailyMed SPL package inserts). One record per label: brand/generic
        name, manufacturer, label effective date, indications excerpt (summary), boxed warning and
        warnings excerpts, pharmacologic classes, and identifiers rxcui / unii / spl_set_id / ndc /
        fda_application. A drug name ('Keytruda', 'semaglutide') matches brand, generic and substance
        names; if nothing matches by name, the words are searched in the full label text (e.g.
        'PD-1 melanoma'). Raw openFDA syntax is accepted when the query contains field:value, e.g.
        'openfda.pharm_class_epc:"Kinase Inhibitor [EPC]" AND boxed_warning:hepatotoxicity'.
        Many generic labels exist per molecule; use get_drug_label for full sections of one label.
        """,
        federated=True,
    )
    async def search_drug_labels(self, params: SearchLabelsParams) -> ToolResult:
        mode, skip = _parse_cursor(params.cursor)
        query = params.query.strip()
        ptype = {
            "prescription": 'openfda.product_type:"HUMAN PRESCRIPTION DRUG"',
            "otc": 'openfda.product_type:"HUMAN OTC DRUG"',
        }.get(params.product_type)
        notes: list[str] = []
        if _FIELD_QUERY_RE.search(query):
            mode = "raw"
            search = _and(query, ptype)
        elif mode == "text":
            search = _and(_words(query), ptype)
        else:
            mode = "name"
            search = _and(self._drug_clause(query), ptype)
        payload, response, problem = await self._get("drug/label", search=search, limit=params.limit, skip=skip)
        items = payload.get("results") or []
        if not items and mode == "name" and not problem and skip == 0:
            mode = "text"
            payload, response, problem = await self._get(
                "drug/label", search=_and(_words(query), ptype), limit=params.limit, skip=0
            )
            items = payload.get("results") or []
            if items:
                notes.append("no label matched the drug name; results are full-text matches anywhere in the label")
        total = self._total(payload)
        if not items and not problem:
            notes.append(f"no FDA labels matched {query!r}")
        return self._result(
            "search_drug_labels",
            response,
            problem,
            records=[_label_record(i) for i in items],
            total=total,
            next_cursor=self._next(skip, len(items), total, mode),
            notes=notes,
        )

    @tool(
        description="""
        Full FDA label for one product, looked up by SPL set id (from search_drug_labels), RxCUI,
        UNII, product NDC, application number (NDA/BLA/ANDA), or brand/generic name. Returns the
        label record with section excerpts in attributes: boxed_warning, indications_and_usage,
        dosage_and_administration, contraindications, warnings_and_cautions, adverse_reactions,
        drug_interactions, use_in_specific_populations, pregnancy, mechanism_of_action,
        pharmacokinetics, clinical_studies, overdosage (each up to section_chars). Newest label first.
        """
    )
    async def get_drug_label(self, params: GetLabelParams) -> ToolResult:
        value = params.value.strip()
        field = {
            "set_id": "set_id",
            "rxcui": "openfda.rxcui",
            "unii": "openfda.unii",
            "ndc": "openfda.product_ndc",
            "application_number": "openfda.application_number",
            "brand_name": "openfda.brand_name",
            "generic_name": "openfda.generic_name",
        }[params.id_type]
        if params.id_type == "application_number":
            value = value.upper().replace(" ", "")
        elif params.id_type == "unii":
            value = value.upper()
        payload, response, problem = await self._get(
            "drug/label", search=f"{field}:{_q(value)}", limit=params.limit
        )
        items = sorted(payload.get("results") or [], key=lambda i: i.get("effective_time") or "", reverse=True)
        notes = [] if items or problem else [f"no FDA label found for {params.id_type} {value}"]
        return self._result(
            "get_drug_label",
            response,
            problem,
            records=[_label_record(i, params.section_chars, full=True) for i in items],
            total=self._total(payload),
            notes=notes,
        )

    @tool(
        description="""
        Tally FDA Adverse Event Reporting System (FAERS) reports: e.g. top MedDRA reactions reported
        for a drug ({"drug": "semaglutide"}), outcomes/seriousness/sex/age/country/reporter breakdowns,
        reports per year, or the drugs most often on reports of a reaction ({"reaction":
        "pancreatitis", "count_by": "co_reported_drug"}). Optional date range on report receipt date
        and serious_only. Returns data {total_reports, rows: [{term, count}]}: use for
        pharmacovigilance signal exploration. Counts are report counts, not incidence; FAERS
        reports do not establish causation.
        """
    )
    async def adverse_event_counts(self, params: AdverseEventCountsParams) -> ToolResult:
        if not params.drug and not params.reaction and params.count_by == "reaction":
            notes = ["database-wide counts (no drug or reaction filter)"]
        else:
            notes = []
        search = self._event_search(params.drug, params.reaction, params.serious_only, False, None, params)
        field, labels = _COUNT_FIELDS[params.count_by]
        payload, response, problem = await self._get(
            "drug/event", search=search, count=field, limit=None if params.count_by == "year" else params.limit
        )
        buckets = payload.get("results") or []
        rows: list[dict[str, Any]] = []
        if params.count_by == "year":
            years: dict[str, int] = {}
            for b in buckets:
                year = str(b.get("time") or "")[:4]
                if year:
                    years[year] = years.get(year, 0) + int(b.get("count") or 0)
            rows = [{"term": y, "count": c} for y, c in sorted(years.items())]
        else:
            for b in buckets:
                term = str(b.get("term"))
                rows.append({"term": labels.get(term, term) if labels else term, "count": b.get("count")})
        total_reports = None
        if not problem and buckets:
            meta_payload, _, _ = await self._get("drug/event", search=search, limit=1)
            total_reports = self._total(meta_payload)
        if not rows and not problem:
            notes.append("no FAERS reports matched")
        notes.append(
            "FAERS counts are spontaneous report counts (duplicates and reporting bias possible); they do not "
            "establish causation or incidence"
        )
        if params.count_by == "outcome":
            notes.append("a report with several reactions can count under several outcomes, so the rows can sum to more than total_reports")
        return self._result(
            "adverse_event_counts",
            response,
            problem,
            total=total_reports,
            data={"count_by": params.count_by, "field": field, "total_reports": total_reports, "rows": rows},
            notes=notes,
        )

    def _event_search(
        self,
        drug: str | None,
        reaction: str | None,
        serious_only: bool,
        fatal_only: bool,
        raw: str | None,
        dates: _DateRange,
    ) -> str | None:
        drug_clause = None
        if drug:
            q = _q(drug)
            drug_clause = (
                f"patient.drug.openfda.generic_name:{q} OR patient.drug.openfda.brand_name:{q} "
                f"OR patient.drug.medicinalproduct:{q}"
            )
        return (
            _and(
                drug_clause,
                f"patient.reaction.reactionmeddrapt:{_q(reaction)}" if reaction else None,
                "serious:1" if serious_only else None,
                "seriousnessdeath:1" if fatal_only else None,
                _range("receivedate", dates.date_from, dates.date_to),
                raw.strip() if raw and raw.strip() else None,
            )
            or None
        )

    @tool(
        description="""
        Retrieve individual FAERS adverse event case reports (compact): suspect and concomitant
        drugs with indications, MedDRA reactions with outcomes, seriousness criteria, patient sex /
        age / weight, reporter type, country and receipt date. Filter by drug, reaction,
        serious_only, fatal_only, date range, or a raw openFDA expression. Use adverse_event_counts
        for aggregate signal questions; use this to read example cases.
        """
    )
    async def search_adverse_events(self, params: SearchAdverseEventsParams) -> ToolResult:
        _, skip = _parse_cursor(params.cursor)
        search = self._event_search(
            params.drug, params.reaction, params.serious_only, params.fatal_only, params.query, params
        )
        payload, response, problem = await self._get("drug/event", search=search, limit=params.limit, skip=skip)
        items = payload.get("results") or []
        total = self._total(payload)
        notes = [] if items or problem else ["no FAERS reports matched"]
        notes.append("case reports are unverified and do not establish causation")
        return self._result(
            "search_adverse_events",
            response,
            problem,
            records=[_event_record(i) for i in items],
            total=total,
            next_cursor=self._next(skip, len(items), total),
            notes=notes,
        )

    @tool(
        description="""
        Drugs@FDA approval records: one record per application (NDA/BLA/ANDA) with sponsor, original
        approval date, products (brand, strength, dosage form, route, marketing status, TE code) and
        recent submissions (supplements with dates, classes, review priority and label/letter PDF
        links). Search by drug (brand/generic/active ingredient), application_number or sponsor.
        Use for "when was X approved", regulatory history and generic (ANDA) competition.
        """
    )
    async def drug_approvals(self, params: DrugApprovalsParams) -> ToolResult:
        _, skip = _parse_cursor(params.cursor)
        drug_clause = None
        if params.drug:
            q = _q(params.drug)
            drug_clause = (
                f"openfda.generic_name:{q} OR openfda.brand_name:{q} OR products.brand_name:{q} "
                f"OR products.active_ingredients.name:{q}"
            )
        search = _and(
            drug_clause,
            f"application_number:{_q(params.application_number.upper().replace(' ', ''))}"
            if params.application_number
            else None,
            f"sponsor_name:{_q(params.sponsor)}" if params.sponsor else None,
        )
        if not search:
            return ToolResult(
                source=self.name, tool="drug_approvals", notes=["give drug, application_number or sponsor"]
            )
        payload, response, problem = await self._get("drug/drugsfda", search=search, limit=params.limit, skip=skip)
        items = payload.get("results") or []
        total = self._total(payload)
        return self._result(
            "drug_approvals",
            response,
            problem,
            records=[_approval_record(i, params.max_submissions) for i in items],
            total=total,
            next_cursor=self._next(skip, len(items), total),
            notes=[] if items or problem else ["no Drugs@FDA applications matched"],
        )

    @tool(
        description="""
        FDA drug recall enforcement reports: recall number, classification (Class I = most
        serious), status, recalling firm, product description, reason for recall, quantity,
        distribution and dates. Filter by drug name, reason words (e.g. 'nitrosamine',
        'sterility', 'NDMA'), firm, classification, status and recall initiation date range.
        Newest recalls first.
        """
    )
    async def drug_recalls(self, params: DrugRecallsParams) -> ToolResult:
        _, skip = _parse_cursor(params.cursor)
        drug_clause = None
        if params.drug:
            q = _q(params.drug)
            drug_clause = f"openfda.generic_name:{q} OR openfda.brand_name:{q} OR product_description:{q}"
        search = _and(
            drug_clause,
            f"reason_for_recall:{_words(params.reason)}" if params.reason else None,
            f"recalling_firm:{_q(params.firm)}" if params.firm else None,
            f"classification:{_q(params.classification)}" if params.classification else None,
            f"status:{_q(params.status)}" if params.status else None,
            _range("recall_initiation_date", params.date_from, params.date_to),
        )
        payload, response, problem = await self._get(
            "drug/enforcement", search=search or None, limit=params.limit, skip=skip, sort="recall_initiation_date:desc"
        )
        items = payload.get("results") or []
        total = self._total(payload)
        return self._result(
            "drug_recalls",
            response,
            problem,
            records=[_recall_record(i) for i in items],
            total=total,
            next_cursor=self._next(skip, len(items), total),
            notes=[] if items or problem else ["no drug recalls matched"],
        )

    @tool(
        description="""
        NDC Directory lookup: resolve a National Drug Code (product 0169-4132, package 0169-4132-12,
        or 10/11-digit NDC without dashes as found in claims data) or a brand/generic name to
        product listings: labeler, active ingredients with strengths, dosage form, route,
        marketing category and dates, DEA schedule, pharmacologic classes, packages, and
        identifiers ndc / rxcui / unii / spl_set_id / fda_application.
        """
    )
    async def ndc_lookup(self, params: NdcLookupParams) -> ToolResult:
        _, skip = _parse_cursor(params.cursor)
        clauses: list[str] = []
        if params.ndc:
            field, cands = _ndc_candidates(params.ndc)
            clauses.append(" OR ".join(f"{field}:{_q(c)}" for c in cands))
        if params.name:
            q = _q(params.name)
            clauses.append(f"brand_name:{q} OR generic_name:{q}")
        if not clauses:
            return ToolResult(source=self.name, tool="ndc_lookup", notes=["give ndc or name"])
        payload, response, problem = await self._get("drug/ndc", search=_and(*clauses), limit=params.limit, skip=skip)
        items = payload.get("results") or []
        total = self._total(payload)
        return self._result(
            "ndc_lookup",
            response,
            problem,
            records=[_ndc_record(i) for i in items],
            total=total,
            next_cursor=self._next(skip, len(items), total),
            notes=[] if items or problem else ["no NDC Directory products matched (discontinued NDCs are not listed)"],
        )
