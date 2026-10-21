"""ClinicalTrials.gov API v2 connector.

Docs: https://clinicaltrials.gov/data-api/api  (OpenAPI: https://clinicaltrials.gov/api/v2/version)
Auth: none (keyless).
Rate limit: about 50 requests/minute per IP; we stay at 0.8 req/s.

Quirks:
- ``/studies`` takes Essie expressions in ``query.*`` params; structured filters go in
  ``filter.overallStatus`` (comma list) and ``filter.advanced`` (``AREA[Field]value``,
  ``AREA[StartDate]RANGE[2020-01-01,MAX]``, boolean AND/OR/NOT).
- Pagination is token based: ``pageToken`` <- ``nextPageToken``; ``countTotal=true`` is needed to
  get ``totalCount`` (only reported on the first page).
- ``fields=`` takes "piece" names (NCTId, BriefTitle, ...) and keeps payloads small.
- ``/studies/{nct}`` returns 404 with a plain-text body for unknown NCT numbers.
- ``/stats/field/values`` reports database-wide value distributions (not query-scoped), so
  query-scoped counts are computed with ``countTotal`` on ``pageSize=1`` requests.

Identifiers: ``nct``, ``euctr`` (EudraCT number) and ``eu_ct`` (EU CTIS number, non-canonical key). Reference PMIDs, NIH grant
numbers, other registry ids and condition / intervention MeSH ids are kept in ``attributes``
(they describe related entities, not the trial itself).
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field, field_validator

from ..ids import normalize_nct
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

API = "https://clinicaltrials.gov/api/v2"
STUDY_URL = "https://clinicaltrials.gov/study/"

_NCT_RE = re.compile(r"^NCT\d{8}$")
_EUCTR_RE = re.compile(r"^\d{4}-\d{6}-\d{2}(?:-\d{2})?$")
_GRANT_RE = re.compile(r"^(?=.*[A-Za-z])(?=(?:.*\d){4})[A-Za-z0-9-]{6,}$")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|~])")

_SEARCH_FIELDS = (
    "NCTId,BriefTitle,OfficialTitle,Acronym,OrgStudyId,SecondaryIdInfo,OverallStatus,WhyStopped,"
    "Phase,StudyType,Condition,ConditionMeshTerm,ConditionMeshId,InterventionType,InterventionName,"
    "InterventionMeshTerm,InterventionMeshId,LeadSponsorName,LeadSponsorClass,StartDate,"
    "PrimaryCompletionDate,CompletionDate,StudyFirstPostDate,LastUpdatePostDate,EnrollmentCount,"
    "EnrollmentType,BriefSummary,HasResults,ReferencePMID,DesignPrimaryPurpose,DesignAllocation"
)

Status = Literal[
    "RECRUITING",
    "NOT_YET_RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "COMPLETED",
    "TERMINATED",
    "SUSPENDED",
    "WITHDRAWN",
    "UNKNOWN",
    "AVAILABLE",
    "NO_LONGER_AVAILABLE",
    "TEMPORARILY_NOT_AVAILABLE",
    "APPROVED_FOR_MARKETING",
    "WITHHELD",
]
Phase = Literal["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
StudyType = Literal["INTERVENTIONAL", "OBSERVATIONAL", "EXPANDED_ACCESS"]

_SORTS = {
    "relevance": None,
    "last_updated": "LastUpdatePostDate:desc",
    "start_date": "StartDate:desc",
    "first_posted": "StudyFirstPostDate:desc",
    "enrollment": "EnrollmentCount:desc",
}

_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


# ---------------------------------------------------------------- params
class _TrialFilters(ToolParams):
    condition: str | None = Field(
        None, description="Condition/disease (query.cond, Essie syntax, synonyms expanded), e.g. 'melanoma'"
    )
    intervention: str | None = Field(
        None, description="Intervention / drug name (query.intr), e.g. 'pembrolizumab' or 'CAR-T'"
    )
    sponsor: str | None = Field(None, description="Lead sponsor or collaborator name (query.spons), e.g. 'Pfizer'")
    location: str | None = Field(None, description="Site location: city, state, country or facility (query.locn)")
    status: list[Status] | None = Field(
        None, description="Overall recruitment status(es), e.g. ['RECRUITING', 'NOT_YET_RECRUITING']"
    )
    phase: list[Phase] | None = Field(
        None, description="Phase(s): EARLY_PHASE1, PHASE1, PHASE2, PHASE3, PHASE4, NA (not applicable)"
    )
    study_type: StudyType | None = Field(None, description="INTERVENTIONAL, OBSERVATIONAL or EXPANDED_ACCESS")
    start_date_from: str | None = Field(None, description="Study start date on/after (YYYY, YYYY-MM or YYYY-MM-DD)")
    start_date_to: str | None = Field(None, description="Study start date on/before (YYYY, YYYY-MM or YYYY-MM-DD)")
    has_results: bool | None = Field(None, description="true = only studies with posted results; false = without")
    advanced: str | None = Field(
        None,
        description=(
            "Extra Essie filter ANDed into filter.advanced, e.g. 'AREA[LeadSponsorClass]NIH' or "
            "'AREA[EnrollmentCount]RANGE[500,MAX]' or 'AREA[LocationCountry]Germany'"
        ),
    )

    @field_validator("start_date_from", "start_date_to")
    @classmethod
    def _check_date(cls, value: str | None) -> str | None:
        if value and not _DATE_RE.match(value.strip()):
            raise ValueError("use YYYY, YYYY-MM or YYYY-MM-DD")
        return value.strip() if value else value


class SearchStudiesParams(SearchParams, _TrialFilters):
    sort: Literal["relevance", "last_updated", "start_date", "first_posted", "enrollment"] = Field(
        "relevance", description="Sort order (descending for dates/enrollment)"
    )


class GetStudyParams(ToolParams):
    nct: str = Field(description="ClinicalTrials.gov NCT number, e.g. NCT05665595")


class GetStudyResultsParams(ToolParams):
    nct: str = Field(description="NCT number of a study with posted results (hasResults=true)")
    max_outcomes: int = Field(10, ge=1, le=50, description="Maximum outcome measures to include")
    top_adverse_events: int = Field(
        15, ge=1, le=100, description="Number of most frequent serious and other adverse event terms to include"
    )


class StudiesByIdentifierParams(ToolParams):
    id_type: Literal["nct", "pmid", "secondary_id", "org_study_id"] = Field(
        description=(
            "nct = batch-fetch NCT numbers; pmid = trials whose record cites this PubMed ID; "
            "secondary_id = other registry / grant ids (EudraCT/EU CT number, NIH grant like U10CA180821, "
            "jRCT, ISRCTN, CTRI ...); org_study_id = sponsor protocol number (e.g. MK-3475-006)"
        )
    )
    ids: list[str] = Field(min_length=1, max_length=50, description="Identifiers to look up (max 50)")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of studies to return")


class CountStudiesParams(_TrialFilters):
    query: str | None = Field(None, description="Free-text query (query.term), optional")
    group_by: Literal["phase", "status", "study_type", "none"] = Field(
        "phase", description="Break the total down by this field (one upstream request per value)"
    )


class FieldValuesParams(ToolParams):
    fields: list[str] = Field(
        min_length=1,
        max_length=5,
        description=(
            "Field piece names, e.g. ['Phase'], ['OverallStatus'], ['InterventionMeshTerm'], ['ConditionMeshTerm'], "
            "['LeadSponsorName'], ['LocationCountry'], ['StudyType']"
        ),
    )
    top: int = Field(25, ge=1, le=200, description="Number of top values to return per field")


# ---------------------------------------------------------------- helpers
def _clean_nct(value: str) -> str:
    return normalize_nct(value).replace(" ", "")


def _date(struct: dict[str, Any] | None) -> str | None:
    if not isinstance(struct, dict):
        return None
    value = struct.get("date")
    return value if isinstance(value, str) and value else None


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def _truncate(text: str | None, chars: int) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text if len(text) <= chars else text[:chars].rstrip() + " ..."


def _secondary_ids(ident: dict[str, Any]) -> tuple[dict[str, str], list[str], list[str]]:
    """Return (identifiers, other_ids, nih_grants) from the identification module."""
    identifiers: dict[str, str] = {}
    other: list[str] = []
    grants: list[str] = []
    for info in ident.get("secondaryIdInfos") or []:
        sid = (info.get("id") or "").strip()
        if not sid:
            continue
        stype = (info.get("type") or "").upper()
        domain = (info.get("domain") or "").strip()
        if stype == "NIH":
            # upstream sometimes stores bare years ("2014") as NIH grants; keep real grant numbers only
            if _GRANT_RE.match(sid.replace(" ", "")):
                grants.append(sid)
            continue
        if _EUCTR_RE.match(sid):
            if stype == "EUDRACT_NUMBER" or "EUDRACT" in domain.upper():
                identifiers["euctr"] = sid
            elif "EU CT" in domain.upper() or stype == "CTIS":
                identifiers.setdefault("eu_ct", sid)
        other.append(f"{sid} ({domain or stype.lower()})" if (domain or stype) else sid)
    return identifiers, other, grants


def _unescape_md(text: str | None) -> str | None:
    """CT.gov text fields are Markdown with backslash escapes (a backslash before [ or >); drop them."""
    if not text:
        return text
    return _MD_ESCAPE.sub(lambda m: m.group(1), text)


def _mesh_terms(browse: dict[str, Any] | None) -> list[str]:
    return [f"{m.get('term')} [{m.get('id')}]" for m in (browse or {}).get("meshes") or [] if m.get("id")]


def _study_record(study: dict[str, Any], *, detail: bool = False) -> Record:
    proto = study.get("protocolSection") or {}
    derived = study.get("derivedSection") or {}
    ident = proto.get("identificationModule") or {}
    status = proto.get("statusModule") or {}
    sponsor = proto.get("sponsorCollaboratorsModule") or {}
    design = proto.get("designModule") or {}
    desc = proto.get("descriptionModule") or {}
    conds = proto.get("conditionsModule") or {}
    arms = proto.get("armsInterventionsModule") or {}
    refs = proto.get("referencesModule") or {}
    nct = ident.get("nctId") or ""
    sec_ids, other_ids, grants = _secondary_ids(ident)
    enrollment = design.get("enrollmentInfo") or {}
    design_info = design.get("designInfo") or {}
    start = _date(status.get("startDateStruct"))
    first_posted = _date(status.get("studyFirstPostDateStruct"))
    interventions = [
        f"{(i.get('type') or '').title()}: {i.get('name')}" if i.get("type") else i.get("name")
        for i in arms.get("interventions") or []
        if i.get("name")
    ]
    pmids = [r.get("pmid") for r in refs.get("references") or [] if r.get("pmid")]
    attributes: dict[str, Any] = {
        "status": status.get("overallStatus"),
        "why_stopped": status.get("whyStopped"),
        "phases": design.get("phases"),
        "study_type": design.get("studyType"),
        "acronym": ident.get("acronym"),
        "official_title": ident.get("officialTitle") if ident.get("officialTitle") != ident.get("briefTitle") else None,
        "conditions": conds.get("conditions"),
        "condition_mesh": _mesh_terms(derived.get("conditionBrowseModule")),
        "interventions": interventions,
        "intervention_mesh": _mesh_terms(derived.get("interventionBrowseModule")),
        "lead_sponsor": (sponsor.get("leadSponsor") or {}).get("name"),
        "sponsor_class": (sponsor.get("leadSponsor") or {}).get("class"),
        "enrollment": enrollment.get("count"),
        "enrollment_type": enrollment.get("type"),
        "primary_purpose": design_info.get("primaryPurpose"),
        "allocation": design_info.get("allocation"),
        "start_date": start,
        "primary_completion_date": _date(status.get("primaryCompletionDateStruct")),
        "completion_date": _date(status.get("completionDateStruct")),
        "first_posted": first_posted,
        "last_updated": _date(status.get("lastUpdatePostDateStruct")),
        "has_results": study.get("hasResults"),
        "org_study_id": (ident.get("orgStudyIdInfo") or {}).get("id"),
        "secondary_ids": other_ids,
        "nih_grants": grants,
        "reference_pmids": pmids,
    }
    if detail:
        attributes.update(_detail_attributes(proto, derived, study))
    return Record(
        source="clinicaltrials",
        id=nct,
        type="trial",
        title=ident.get("briefTitle") or ident.get("officialTitle"),
        url=f"{STUDY_URL}{nct}" if nct else None,
        identifiers={"nct": nct, **sec_ids},
        date=start or first_posted,
        summary=_unescape_md(strip_html(desc.get("briefSummary"))),
        attributes=_compact(attributes),
    )


def _detail_attributes(proto: dict[str, Any], derived: dict[str, Any], study: dict[str, Any]) -> dict[str, Any]:
    sponsor = proto.get("sponsorCollaboratorsModule") or {}
    design = proto.get("designModule") or {}
    design_info = design.get("designInfo") or {}
    masking = design_info.get("maskingInfo") or {}
    arms_mod = proto.get("armsInterventionsModule") or {}
    outcomes = proto.get("outcomesModule") or {}
    elig = proto.get("eligibilityModule") or {}
    locs = proto.get("contactsLocationsModule") or {}
    refs = proto.get("referencesModule") or {}
    ipd = proto.get("ipdSharingStatementModule") or {}
    conds = proto.get("conditionsModule") or {}
    oversight = proto.get("oversightModule") or {}
    docs = ((study.get("documentSection") or {}).get("largeDocumentModule") or {}).get("largeDocs") or []
    locations = locs.get("locations") or []
    countries = sorted({loc.get("country") for loc in locations if loc.get("country")})

    def outcome_list(items: list[dict[str, Any]], cap: int) -> list[str]:
        out = []
        for o in items[:cap]:
            measure = o.get("measure")
            if measure:
                out.append(f"{measure} (time frame: {o['timeFrame']})" if o.get("timeFrame") else measure)
        if len(items) > cap:
            out.append(f"... (+{len(items) - cap} more)")
        return out

    detailed = _unescape_md(strip_html((proto.get("descriptionModule") or {}).get("detailedDescription")))
    return _compact(
        {
            "detailed_description": _truncate(detailed, 1500),
            "keywords": conds.get("keywords"),
            "collaborators": [c.get("name") for c in sponsor.get("collaborators") or [] if c.get("name")],
            "responsible_party": (sponsor.get("responsibleParty") or {}).get("type"),
            "intervention_model": design_info.get("interventionModel"),
            "masking": masking.get("masking"),
            "masked_roles": masking.get("whoMasked"),
            "observational_model": design_info.get("observationalModel"),
            "time_perspective": design_info.get("timePerspective"),
            "arms": [
                _compact(
                    {
                        "label": a.get("label"),
                        "type": a.get("type"),
                        "interventions": a.get("interventionNames"),
                        "description": _truncate(a.get("description"), 300),
                    }
                )
                for a in arms_mod.get("armGroups") or []
            ],
            "intervention_details": [
                _compact(
                    {
                        "type": i.get("type"),
                        "name": i.get("name"),
                        "other_names": i.get("otherNames"),
                        "description": _truncate(i.get("description"), 300),
                    }
                )
                for i in arms_mod.get("interventions") or []
            ],
            "primary_outcomes": outcome_list(outcomes.get("primaryOutcomes") or [], 10),
            "secondary_outcomes": outcome_list(outcomes.get("secondaryOutcomes") or [], 15),
            "eligibility": _compact(
                {
                    "sex": elig.get("sex"),
                    "minimum_age": elig.get("minimumAge"),
                    "maximum_age": elig.get("maximumAge"),
                    "age_groups": elig.get("stdAges"),
                    "healthy_volunteers": elig.get("healthyVolunteers"),
                    "criteria": _truncate(_unescape_md(elig.get("eligibilityCriteria")), 2000),
                }
            ),
            "location_count": len(locations),
            "countries": countries,
            "sites_sample": [
                ", ".join(x for x in (loc.get("facility"), loc.get("city"), loc.get("country")) if x)
                for loc in locations[:10]
            ],
            "officials": [
                ", ".join(x for x in (o.get("name"), o.get("role"), o.get("affiliation")) if x)
                for o in locs.get("overallOfficials") or []
            ],
            "references": [
                _compact({"pmid": r.get("pmid"), "type": r.get("type"), "citation": _truncate(r.get("citation"), 300)})
                for r in (refs.get("references") or [])[:20]
            ],
            "see_also_links": [link.get("url") for link in refs.get("seeAlsoLinks") or [] if link.get("url")],
            "ipd_sharing": _compact(
                {
                    "plan": ipd.get("ipdSharing"),
                    "description": _truncate(ipd.get("description"), 500),
                    "info_types": ipd.get("infoTypes"),
                    "time_frame": _truncate(ipd.get("timeFrame"), 300),
                    "access_criteria": _truncate(ipd.get("accessCriteria"), 300),
                    "url": ipd.get("url"),
                }
            ),
            "fda_regulated_drug": oversight.get("isFdaRegulatedDrug"),
            "fda_regulated_device": oversight.get("isFdaRegulatedDevice"),
            "has_dmc": oversight.get("oversightHasDmc"),
            "documents": [
                _compact(
                    {
                        "label": d.get("label"),
                        "date": d.get("date"),
                        "url": f"https://cdn.clinicaltrials.gov/large-docs/{proto.get('identificationModule', {}).get('nctId', '')[-2:]}/"
                        f"{proto.get('identificationModule', {}).get('nctId', '')}/{d.get('filename')}"
                        if d.get("filename")
                        else None,
                    }
                )
                for d in docs
            ],
            "condition_mesh_ancestors": [
                m.get("term") for m in ((derived.get("conditionBrowseModule") or {}).get("ancestors") or [])[:15]
            ],
        }
    )


def _advanced_expression(p: _TrialFilters, extra: str | None = None) -> str | None:
    parts: list[str] = []
    if p.phase:
        phases = sorted(set(p.phase))
        parts.append(f"AREA[Phase]{phases[0]}" if len(phases) == 1 else f"AREA[Phase]({' OR '.join(phases)})")
    if p.study_type:
        parts.append(f"AREA[StudyType]{p.study_type}")
    if p.start_date_from or p.start_date_to:
        parts.append(f"AREA[StartDate]RANGE[{p.start_date_from or 'MIN'},{p.start_date_to or 'MAX'}]")
    if p.has_results is not None:
        parts.append(f"AREA[HasResults]{'true' if p.has_results else 'false'}")
    if p.advanced and p.advanced.strip():
        parts.append(f"({p.advanced.strip()})" if " " in p.advanced.strip() else p.advanced.strip())
    if extra:
        parts.append(extra)
    return " AND ".join(parts) or None


def _query_params(p: _TrialFilters, query: str | None, extra_advanced: str | None = None) -> dict[str, Any]:
    return {
        "query.term": query.strip() if query and query.strip() else None,
        "query.cond": p.condition,
        "query.intr": p.intervention,
        "query.spons": p.sponsor,
        "query.locn": p.location,
        "filter.overallStatus": ",".join(dict.fromkeys(p.status)) if p.status else None,
        "filter.advanced": _advanced_expression(p, extra_advanced),
    }


def _num(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value
    return value


def _summarize_results(results: dict[str, Any], max_outcomes: int, top_ae: int) -> dict[str, Any]:
    out: dict[str, Any] = {}

    flow = results.get("participantFlowModule") or {}
    if flow:
        fgroups = {g.get("id"): g.get("title") for g in flow.get("groups") or []}

        def by_group(items: list[dict[str, Any]], key: str = "numSubjects") -> dict[str, Any]:
            return {fgroups.get(a.get("groupId"), a.get("groupId")): _num(a.get(key)) for a in items or []}

        out["participant_flow"] = _compact(
            {
                "groups": list(fgroups.values()),
                "recruitment_details": _truncate(flow.get("recruitmentDetails"), 500),
                "periods": [
                    _compact(
                        {
                            "title": period.get("title"),
                            "milestones": [
                                {"milestone": m.get("type"), "counts": by_group(m.get("achievements") or [])}
                                for m in period.get("milestones") or []
                            ],
                            "dropouts": [
                                {"reason": d.get("type"), "counts": by_group(d.get("reasons") or [])}
                                for d in period.get("dropWithdraws") or []
                            ],
                        }
                    )
                    for period in flow.get("periods") or []
                ],
            }
        )

    baseline = results.get("baselineCharacteristicsModule") or {}
    if baseline:
        bgroups = {g.get("id"): g.get("title") for g in baseline.get("groups") or []}
        denoms = baseline.get("denoms") or []
        measures = []
        for m in (baseline.get("measures") or [])[:15]:
            rows = []
            for cls in m.get("classes") or []:
                for cat in cls.get("categories") or []:
                    label = " / ".join(x for x in (cls.get("title"), cat.get("title")) if x) or "value"
                    values = {}
                    for meas in cat.get("measurements") or []:
                        value = meas.get("value")
                        if meas.get("spread"):
                            value = f"{value} ({meas['spread']})"
                        elif meas.get("lowerLimit") or meas.get("upperLimit"):
                            value = f"{value} [{meas.get('lowerLimit')}, {meas.get('upperLimit')}]"
                        values[bgroups.get(meas.get("groupId"), meas.get("groupId"))] = value
                    rows.append({"category": label, "values": values})
            measures.append(
                _compact(
                    {
                        "title": m.get("title"),
                        "param": m.get("paramType"),
                        "dispersion": m.get("dispersionType"),
                        "unit": m.get("unitOfMeasure"),
                        "rows": rows[:12],
                    }
                )
            )
        out["baseline"] = _compact(
            {
                "groups": list(bgroups.values()),
                "participants": {
                    bgroups.get(c.get("groupId"), c.get("groupId")): _num(c.get("value"))
                    for c in (denoms[0].get("counts") if denoms else []) or []
                },
                "measures": measures,
            }
        )

    om_module = results.get("outcomeMeasuresModule") or {}
    oms = om_module.get("outcomeMeasures") or []
    if oms:
        rendered = []
        for om in oms[:max_outcomes]:
            ogroups = {g.get("id"): g.get("title") for g in om.get("groups") or []}
            denoms = om.get("denoms") or []
            rows = []
            for cls in om.get("classes") or []:
                for cat in cls.get("categories") or []:
                    label = " / ".join(x for x in (cls.get("title"), cat.get("title")) if x) or "value"
                    values = {}
                    for meas in cat.get("measurements") or []:
                        value = meas.get("value")
                        if meas.get("spread"):
                            value = f"{value} ({meas['spread']})"
                        elif meas.get("lowerLimit") or meas.get("upperLimit"):
                            value = f"{value} [{meas.get('lowerLimit')}, {meas.get('upperLimit')}]"
                        if meas.get("comment"):
                            value = f"{value} ({_truncate(meas['comment'], 120)})"
                        values[ogroups.get(meas.get("groupId"), meas.get("groupId"))] = value
                    rows.append({"category": label, "values": values})
            analyses = []
            for an in (om.get("analyses") or [])[:5]:
                ci = None
                if an.get("ciLowerLimit") or an.get("ciUpperLimit"):
                    ci = f"{an.get('ciPctValue') or ''}% CI {an.get('ciLowerLimit')} to {an.get('ciUpperLimit')}".strip()
                analyses.append(
                    _compact(
                        {
                            "groups": [ogroups.get(g, g) for g in an.get("groupIds") or []],
                            "method": an.get("statisticalMethod"),
                            "p_value": an.get("pValue"),
                            "estimate": f"{an.get('paramType')}: {an.get('paramValue')}" if an.get("paramValue") else None,
                            "ci": ci,
                            "non_inferiority": an.get("nonInferiorityType"),
                        }
                    )
                )
            rendered.append(
                _compact(
                    {
                        "type": om.get("type"),
                        "title": om.get("title"),
                        "time_frame": om.get("timeFrame"),
                        "status": om.get("reportingStatus"),
                        "param": om.get("paramType"),
                        "dispersion": om.get("dispersionType"),
                        "unit": om.get("unitOfMeasure"),
                        "population": _truncate(om.get("populationDescription"), 300),
                        "participants": {
                            ogroups.get(c.get("groupId"), c.get("groupId")): _num(c.get("value"))
                            for c in (denoms[0].get("counts") if denoms else []) or []
                        },
                        "rows": rows[:20],
                        "analyses": analyses,
                    }
                )
            )
        out["outcome_measures"] = rendered
        if len(oms) > max_outcomes:
            out["outcome_measures_omitted"] = len(oms) - max_outcomes

    ae = results.get("adverseEventsModule") or {}
    if ae:
        egroups = {g.get("id"): g.get("title") for g in ae.get("eventGroups") or []}

        def top_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
            scored = []
            for ev in events or []:
                stats = ev.get("stats") or []
                affected = sum(int(s.get("numAffected") or 0) for s in stats)
                scored.append(
                    (
                        affected,
                        {
                            "term": ev.get("term"),
                            "organ_system": ev.get("organSystem"),
                            "affected_total": affected,
                            "by_group": {
                                egroups.get(s.get("groupId"), s.get("groupId")): f"{s.get('numAffected')}/{s.get('numAtRisk')}"
                                for s in stats
                            },
                        },
                    )
                )
            scored.sort(key=lambda x: -x[0])
            return [item for _, item in scored[:top_ae]]

        out["adverse_events"] = _compact(
            {
                "time_frame": ae.get("timeFrame"),
                "frequency_threshold_pct": ae.get("frequencyThreshold"),
                "vocabulary": next(
                    (e.get("sourceVocabulary") for e in (ae.get("seriousEvents") or []) if e.get("sourceVocabulary")),
                    None,
                ),
                "groups": [
                    _compact(
                        {
                            "group": g.get("title"),
                            "deaths": f"{g.get('deathsNumAffected')}/{g.get('deathsNumAtRisk')}"
                            if g.get("deathsNumAtRisk") is not None
                            else None,
                            "serious": f"{g.get('seriousNumAffected')}/{g.get('seriousNumAtRisk')}"
                            if g.get("seriousNumAtRisk") is not None
                            else None,
                            "other": f"{g.get('otherNumAffected')}/{g.get('otherNumAtRisk')}"
                            if g.get("otherNumAtRisk") is not None
                            else None,
                        }
                    )
                    for g in ae.get("eventGroups") or []
                ],
                "serious_event_terms": len(ae.get("seriousEvents") or []),
                "other_event_terms": len(ae.get("otherEvents") or []),
                "top_serious_events": top_events(ae.get("seriousEvents") or []),
                "top_other_events": top_events(ae.get("otherEvents") or []),
            }
        )

    more = results.get("moreInfoModule") or {}
    if more.get("limitationsAndCaveats"):
        out["limitations"] = _truncate((more.get("limitationsAndCaveats") or {}).get("description"), 500)
    return out


# ---------------------------------------------------------------- connector
class ClinicalTrials(Connector):
    name = "clinicaltrials"
    title = "ClinicalTrials.gov"
    category = "clinical"
    description = (
        "NLM registry and results database of 550k+ clinical studies worldwide: conditions, interventions, "
        "phases, status, sponsors, enrollment, outcomes, eligibility and posted results. Best source for "
        "trial discovery, trial design details and registered results by NCT number."
    )
    homepage = "https://clinicaltrials.gov/"
    docs_url = "https://clinicaltrials.gov/data-api/api"
    auth = "none"
    rate_limit = 0.8
    health_query = "pembrolizumab"
    nih_repositories = ("ClinicalTrials.gov",)

    async def _studies(self, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        response = await self.request("GET", f"{API}/studies", params=params)
        payload = response.json()
        return (payload if isinstance(payload, dict) else {}), response.url

    @tool(
        description="""
        Search ClinicalTrials.gov registered studies. Returns one record per trial (NCT id, title,
        status, phases, conditions + MeSH terms, interventions, sponsor, enrollment, key dates,
        has_results, reference PMIDs). Use for "which trials test X in Y", trial landscapes and to
        find NCT numbers; then call get_study / get_study_results for details.
        `query` is free text (Essie syntax: AND/OR/NOT, quotes, AREA[Field]value; may be "" when only
        filters are used). Prefer the structured filters: condition, intervention, sponsor, location,
        status (e.g. ['RECRUITING']), phase (e.g. ['PHASE3']), study_type, start_date_from/to,
        has_results, advanced (raw Essie, e.g. 'AREA[LeadSponsorClass]NIH').
        Example: {"query": "", "condition": "melanoma", "intervention": "pembrolizumab", "phase": ["PHASE3"]}.
        Paginate with next_cursor.
        """,
        federated=True,
    )
    async def search_studies(self, params: SearchStudiesParams) -> ToolResult:
        query = _query_params(params, params.query)
        query.update(
            {
                "pageSize": params.limit,
                "pageToken": params.cursor or None,
                "countTotal": "true",
                "fields": _SEARCH_FIELDS,
                "sort": _SORTS.get(params.sort),
            }
        )
        payload, url = await self._studies(query)
        studies = payload.get("studies") or []
        notes = []
        if not studies:
            notes.append("no studies matched; try fewer filters or the condition/intervention fields")
        return ToolResult(
            source=self.name,
            tool="search_studies",
            records=[_study_record(s) for s in studies],
            total=payload.get("totalCount"),
            next_cursor=payload.get("nextPageToken") if studies else None,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Full protocol record for one trial by NCT number (e.g. NCT05665595): titles, status and why
        stopped, phases, design (allocation, masking, model), conditions with MeSH terms and
        keywords, arms and interventions, primary/secondary outcomes, enrollment, all key dates,
        sponsor/collaborators, eligibility criteria, site count and countries, references with
        PMIDs, IPD-sharing statement, protocol/SAP documents and has_results. Use get_study_results
        for the posted results tables.
        """
    )
    async def get_study(self, params: GetStudyParams) -> ToolResult:
        nct = _clean_nct(params.nct)
        if not _NCT_RE.match(nct):
            return ToolResult(source=self.name, tool="get_study", notes=[f"{params.nct!r} is not a valid NCT number"])
        response = await self.request("GET", f"{API}/studies/{quote(nct)}", allow_status=(404,))
        if response.status == 404:
            return ToolResult(
                source=self.name,
                tool="get_study",
                notes=[f"{nct} not found in ClinicalTrials.gov"],
                request_url=response.url,
            )
        study = response.json()
        record = _study_record(study, detail=True)
        notes = []
        if study.get("hasResults"):
            notes.append("results are posted: call get_study_results for outcome values and adverse events")
        return ToolResult(
            source=self.name, tool="get_study", records=[record], total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        Posted results for one trial (NCT number), summarized for extraction: participant flow
        (started/completed/dropouts per arm), baseline characteristics (demographics per arm),
        outcome measures with per-arm values, dispersion/CI and statistical analyses (p-values,
        hazard/odds ratios), and adverse events (deaths/serious/other per arm plus the most frequent
        serious and other MedDRA terms). Returns the trial record plus `data` with these tables.
        Only studies with has_results=true have results (search with has_results=true to find them).
        """
    )
    async def get_study_results(self, params: GetStudyResultsParams) -> ToolResult:
        nct = _clean_nct(params.nct)
        if not _NCT_RE.match(nct):
            return ToolResult(
                source=self.name, tool="get_study_results", notes=[f"{params.nct!r} is not a valid NCT number"]
            )
        response = await self.request("GET", f"{API}/studies/{quote(nct)}", allow_status=(404,))
        if response.status == 404:
            return ToolResult(
                source=self.name,
                tool="get_study_results",
                notes=[f"{nct} not found in ClinicalTrials.gov"],
                request_url=response.url,
            )
        study = response.json()
        record = _study_record(study)
        results = study.get("resultsSection") or {}
        if not results:
            return ToolResult(
                source=self.name,
                tool="get_study_results",
                records=[record],
                total=1,
                request_url=response.url,
                notes=[f"{nct} has no posted results (status: {record.attributes.get('status', 'unknown')})"],
            )
        data = _summarize_results(results, params.max_outcomes, params.top_adverse_events)
        return ToolResult(
            source=self.name,
            tool="get_study_results",
            records=[record],
            total=1,
            data=data,
            request_url=response.url,
            notes=["values are as posted by the sponsor; 'NA' means not estimable / not reached"],
        )

    @tool(
        description="""
        Find trials by an external identifier, for cross-database linking: id_type 'nct' batch-fetches
        up to 50 NCT numbers in one call; 'pmid' finds trials whose registry record cites a PubMed ID
        (results publications, background papers); 'secondary_id' matches other registry or funding ids
        (EudraCT / EU CT numbers such as 2022-501417-31-00, NIH grant numbers such as U10CA180821,
        jRCT, ISRCTN, CTRI ids); 'org_study_id' matches sponsor protocol numbers (e.g. MK-3475-006).
        Returns the same compact trial records as search_studies.
        """
    )
    async def studies_by_identifier(self, params: StudiesByIdentifierParams) -> ToolResult:
        ids = [i.strip() for i in params.ids if i and i.strip()]
        query: dict[str, Any] = {"pageSize": params.limit, "countTotal": "true", "fields": _SEARCH_FIELDS}
        notes: list[str] = []
        if params.id_type == "nct":
            bad = [i for i in ids if not _NCT_RE.match(_clean_nct(i))]
            ncts = [_clean_nct(i) for i in ids if _NCT_RE.match(_clean_nct(i))]
            if bad:
                notes.append(f"ignored invalid NCT numbers: {', '.join(bad)}")
            if not ncts:
                return ToolResult(source=self.name, tool="studies_by_identifier", notes=notes or ["no ids"])
            query["filter.ids"] = ",".join(dict.fromkeys(ncts))
        else:
            area = {"pmid": "ReferencePMID", "secondary_id": "SecondaryId", "org_study_id": "OrgStudyId"}[params.id_type]
            values = [re.sub(r"[\[\]()\"]", "", i) for i in ids]
            if params.id_type == "pmid":
                values = [re.sub(r"\D", "", v) for v in values]
            values = [v for v in values if v]
            terms = [f'AREA[{area}]"{v}"' if " " in v else f"AREA[{area}]{v}" for v in values]
            query["query.term"] = " OR ".join(terms)
        payload, url = await self._studies(query)
        studies = payload.get("studies") or []
        if not studies:
            notes.append(f"no trials found for {params.id_type} {', '.join(ids)}")
        elif params.id_type == "nct":
            found = {(s.get("protocolSection") or {}).get("identificationModule", {}).get("nctId") for s in studies}
            missing = [i for i in query["filter.ids"].split(",") if i not in found]
            if missing:
                notes.append(f"not found: {', '.join(missing)}")
        return ToolResult(
            source=self.name,
            tool="studies_by_identifier",
            records=[_study_record(s) for s in studies],
            total=payload.get("totalCount"),
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Count trials matching a query/filters, broken down by phase, status or study type (or just the
        total with group_by='none'). Same filters as search_studies (condition, intervention, sponsor,
        location, status, phase, study_type, start dates, has_results, advanced). Returns `data`
        {total, breakdown: [{value, count}]}. Use for trial-landscape questions such as "how many
        recruiting phase 3 trials of GLP-1 agonists in obesity". Each breakdown value is one upstream
        request (slow: about 1 request/second).
        """
    )
    async def count_studies(self, params: CountStudiesParams) -> ToolResult:
        async def count(extra: str | None = None, status_override: list[str] | None = None) -> tuple[int, str]:
            q = _query_params(params, params.query, extra)
            if status_override is not None:
                q["filter.overallStatus"] = ",".join(status_override)
            q.update({"pageSize": 1, "countTotal": "true", "fields": "NCTId"})
            payload, url = await self._studies(q)
            return int(payload.get("totalCount") or 0), url

        total, url = await count()
        breakdown: list[dict[str, Any]] = []
        if params.group_by != "none" and total:
            if params.group_by == "phase":
                values = list(params.phase) if params.phase else list(Phase.__args__)  # type: ignore[attr-defined]
                for value in values:
                    n, _ = await count(extra=f"AREA[Phase]{value}")
                    breakdown.append({"value": value, "count": n})
            elif params.group_by == "status":
                values = list(params.status) if params.status else [
                    "RECRUITING",
                    "NOT_YET_RECRUITING",
                    "ACTIVE_NOT_RECRUITING",
                    "ENROLLING_BY_INVITATION",
                    "COMPLETED",
                    "TERMINATED",
                    "SUSPENDED",
                    "WITHDRAWN",
                    "UNKNOWN",
                ]
                for value in values:
                    n, _ = await count(status_override=[value])
                    breakdown.append({"value": value, "count": n})
            else:
                values = [params.study_type] if params.study_type else list(StudyType.__args__)  # type: ignore[attr-defined]
                for value in values:
                    n, _ = await count(extra=f"AREA[StudyType]{value}")
                    breakdown.append({"value": value, "count": n})
        notes = []
        if params.group_by == "phase" and breakdown:
            notes.append("multi-phase trials (e.g. PHASE1|PHASE2) are counted under each of their phases")
        return ToolResult(
            source=self.name,
            tool="count_studies",
            total=total,
            data={"total": total, "group_by": params.group_by, "breakdown": breakdown},
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Database-wide value distribution for ClinicalTrials.gov fields (NOT filtered by a query):
        e.g. fields=['Phase'], ['OverallStatus'], ['InterventionMeshTerm'], ['ConditionMeshTerm'],
        ['LeadSponsorName'], ['LocationCountry']. Returns `data` per field with the number of studies
        per value (top N) and missing/unique counts. Use count_studies for query-scoped counts.
        """
    )
    async def field_values(self, params: FieldValuesParams) -> ToolResult:
        fields = [re.sub(r"[^A-Za-z0-9.]", "", f) for f in params.fields]
        fields = [f for f in fields if f]
        response = await self.request(
            "GET", f"{API}/stats/field/values", params={"fields": ",".join(fields)}, allow_status=(400, 404)
        )
        if response.status in (400, 404):
            return ToolResult(
                source=self.name,
                tool="field_values",
                notes=[f"unknown field(s) {', '.join(fields)}: {response.text[:200]}"],
                request_url=response.url,
            )
        payload = response.json()
        data = []
        for item in payload if isinstance(payload, list) else []:
            data.append(
                _compact(
                    {
                        "field": item.get("piece"),
                        "path": item.get("field"),
                        "type": item.get("type"),
                        "missing_studies": item.get("missingStudiesCount"),
                        "unique_values": item.get("uniqueValuesCount"),
                        "top_values": [
                            {"value": v.get("value"), "studies": v.get("studiesCount")}
                            for v in (item.get("topValues") or [])[: params.top]
                        ],
                    }
                )
            )
        return ToolResult(source=self.name, tool="field_values", data=data, request_url=response.url)
