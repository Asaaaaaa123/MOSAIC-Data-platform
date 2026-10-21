"""Evaluation: how much should a reader trust each record or merged entity?

The score (0-100) and grade (A-D) combine four transparent signals:

- source reliability tier (``SOURCE_PROFILES``): 1 = authoritative / expert-curated
  (HGNC, UniProt Swiss-Prot, ChEMBL, ClinicalTrials.gov, FDA labels, Reactome ...),
  2 = aggregated / integrative / community-curated (OpenAlex, Open Targets, CIViC ...),
  3 = self-deposited, unreviewed (Zenodo, Figshare, OSF, NDEx ...)
- corroboration: how many independent databases report the same entity
- completeness: title, date, identifiers, description, URL, structured fields
- consistency: penalties when sources disagree on an identifier or field

plus flags that override the number when they matter (retracted article,
preprint, withdrawn/terminated trial, obsolete term, controlled access,
spontaneous FAERS reports, unreviewed/electronic annotations, non-human).
Every point is explained in ``reasons`` so the grade is auditable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ..models import Record


@dataclass(frozen=True)
class SourceProfile:
    tier: int  # 1 authoritative/curated, 2 aggregated/integrative, 3 self-deposited
    kind: str
    note: str = ""


SOURCE_PROFILES: dict[str, SourceProfile] = {
    # literature
    "ncbi": SourceProfile(1, "curated index", "NLM-indexed records (PubMed, GEO, ClinVar, dbSNP ...)"),
    "europepmc": SourceProfile(1, "curated index", "PubMed/PMC plus preprints and text-mined annotations"),
    "crossref": SourceProfile(1, "registration agency", "publisher-deposited DOI metadata"),
    "scopus": SourceProfile(1, "curated index"),
    "clarivate": SourceProfile(1, "curated index", "Web of Science Core Collection"),
    "springer": SourceProfile(1, "publisher"),
    "openalex": SourceProfile(2, "aggregated index", "machine-assembled scholarly graph"),
    "semantic_scholar": SourceProfile(2, "aggregated index", "machine-assembled scholarly graph"),
    "unpaywall": SourceProfile(2, "aggregated index", "open-access location harvester"),
    # clinical / regulatory / drugs
    "clinicaltrials": SourceProfile(1, "registry", "sponsor-reported registry data"),
    "openfda": SourceProfile(1, "regulator", "FDA labels/approvals; FAERS reports are spontaneous and not causal"),
    "rxnorm": SourceProfile(1, "terminology", "NLM normalized drug names"),
    "drugcentral": SourceProfile(1, "expert-curated"),
    "clinpgx": SourceProfile(1, "expert-curated", "PharmGKB/ClinPGx curated pharmacogenomics"),
    # chemistry / proteins
    "chembl": SourceProfile(1, "expert-curated", "literature-extracted bioactivity"),
    "bindingdb": SourceProfile(1, "expert-curated", "measured binding affinities"),
    "pubchem": SourceProfile(2, "aggregated", "depositor-contributed substance data"),
    "uniprot": SourceProfile(1, "expert-curated", "Swiss-Prot reviewed; TrEMBL entries are unreviewed"),
    "rcsb_pdb": SourceProfile(1, "experimental archive"),
    "opentargets": SourceProfile(2, "integrative", "evidence integrated from many sources"),
    # genomics
    "hgnc": SourceProfile(1, "nomenclature authority"),
    "gnomad": SourceProfile(1, "population reference"),
    "gtex": SourceProfile(1, "consortium reference"),
    "gwas_catalog": SourceProfile(1, "expert-curated"),
    "pgs_catalog": SourceProfile(1, "expert-curated"),
    "nci_gdc": SourceProfile(1, "consortium archive"),
    "cbioportal": SourceProfile(1, "curated studies"),
    "civic": SourceProfile(2, "community-curated", "crowd-sourced, expert-moderated evidence"),
    # biology / ontologies
    "reactome": SourceProfile(1, "expert-curated"),
    "ols": SourceProfile(1, "ontology"),
    "quickgo": SourceProfile(1, "annotation", "includes electronic (IEA) annotations"),
    "alliance": SourceProfile(1, "model organism databases"),
    "ctd": SourceProfile(1, "expert-curated", "curated links plus statistically inferred ones"),
    "monarch": SourceProfile(2, "integrative"),
    "ndex": SourceProfile(3, "user-deposited", "networks uploaded by users"),
    # data repositories
    "hubmap": SourceProfile(1, "consortium archive"),
    "physionet": SourceProfile(1, "curated repository"),
    "tcia": SourceProfile(1, "curated repository"),
    "dryad": SourceProfile(2, "curated repository", "curator-reviewed deposits"),
    "dandi": SourceProfile(2, "standards-validated repository", "NWB-validated deposits"),
    "openneuro": SourceProfile(2, "standards-validated repository", "BIDS-validated deposits"),
    "synapse": SourceProfile(2, "governed repository"),
    "metabolomics_workbench": SourceProfile(2, "curated repository"),
    "datacite": SourceProfile(2, "registration agency", "DOI metadata deposited by repositories"),
    "dataverse": SourceProfile(3, "self-deposited repository"),
    "zenodo": SourceProfile(3, "self-deposited repository"),
    "figshare": SourceProfile(3, "self-deposited repository"),
    "osf": SourceProfile(3, "self-deposited repository"),
}
DEFAULT_PROFILE = SourceProfile(2, "unclassified")
TIER_POINTS = {1: 40, 2: 30, 3: 18}
TIER_LABEL = {1: "authoritative/curated", 2: "aggregated/integrative", 3: "self-deposited/unreviewed"}

_HUMAN = {"homo sapiens", "human", "9606", "ncbitaxon:9606"}
_BAD_TRIAL = {"terminated", "withdrawn", "suspended"}
_RESTRICTED = re.compile(r"controlled|restricted|credential|limited|registered|embargo|closed|private|dbgap", re.I)


class Evaluation(BaseModel):
    score: int = Field(description="0-100")
    grade: str = Field(description="A (>=80) B (>=60) C (>=40) D")
    tier: int
    source_kind: str
    corroborated_by: list[str] = Field(default_factory=list)
    completeness: float = 0.0
    flags: list[str] = Field(default_factory=list, description="warnings that change how to use the data")
    reasons: list[str] = Field(default_factory=list, description="how the score was computed")

    def compact(self) -> dict[str, Any]:
        out: dict[str, Any] = {"grade": self.grade, "score": self.score}
        if len(self.corroborated_by) > 1:
            out["corroborated_by"] = self.corroborated_by
        if self.flags:
            out["flags"] = self.flags
        return out


def profile(source: str) -> SourceProfile:
    return SOURCE_PROFILES.get(source, DEFAULT_PROFILE)


def _grade(score: int) -> str:
    return "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def completeness(record: Record) -> float:
    checks = [
        bool(record.title),
        bool(record.identifiers),
        bool(record.url),
        bool(record.summary),
        bool(record.date) or record.type in {"gene", "protein", "pathway", "ontology_term", "disease", "phenotype", "compound", "drug", "target"},
        len(record.attributes) >= 3,
    ]
    if record.type in {"article", "preprint", "book"}:
        checks.append(bool(record.authors))
    return round(sum(checks) / len(checks), 2)


def record_flags(record: Record) -> list[str]:
    """Warnings detectable from one record's own fields."""
    flags: list[str] = []
    attrs = record.attributes
    text_bits = " ".join(s.lower() for key in ("publication_types", "pub_types", "work_type", "types", "type") for s in _strings(attrs.get(key)))
    title = (record.title or "").lower()
    if attrs.get("is_retracted") or "retracted publication" in text_bits or title.startswith(("retracted:", "retraction:", "[retracted")):
        flags.append("retracted")
    if record.type == "preprint" or "preprint" in text_bits or attrs.get("source") == "PPR":
        flags.append("preprint (not peer reviewed)")
    status = str(attrs.get("status") or attrs.get("overall_status") or "").lower()
    if record.type in {"trial", "study"} and status in _BAD_TRIAL:
        flags.append(f"trial {status}")
    elif record.type == "trial" and status == "unknown":
        flags.append("trial status unknown (registry not updated)")
    if attrs.get("is_obsolete") or attrs.get("obsolete"):
        flags.append("obsolete term")
    access_values = [
        v for k, v in attrs.items()
        if "access" in k.lower() and not k.lower().startswith("accession") and "criteria" not in k.lower()
    ]
    if any(_RESTRICTED.search(s) for v in access_values for s in _strings(v)):
        flags.append("access restricted (authorization required)")
    if record.source == "openfda" and record.type == "adverse_event":
        flags.append("spontaneous FAERS reports: not evidence of causation")
    if record.source == "uniprot" and attrs.get("reviewed") is False:
        flags.append("unreviewed (TrEMBL) entry")
    if record.type in {"gene", "protein", "variant", "target"}:
        organism = attrs.get("organism") or attrs.get("species") or attrs.get("taxon")
        names = [s.lower() for s in _strings(organism)]
        if names and not any(any(h in n for h in _HUMAN) for n in names):
            flags.append(f"non-human ({names[0]})")
    evidence = " ".join(s.upper() for s in _strings(attrs.get("evidence_code") or attrs.get("evidence_codes")))
    if evidence and set(evidence.split()) <= {"IEA"}:
        flags.append("electronic annotation only (IEA)")
    return flags


def evaluate(
    records: list[Record],
    *,
    conflicts: dict[str, Any] | None = None,
) -> Evaluation:
    """Evaluate one entity backed by ``records`` (one per source after merging)."""
    sources = sorted({r.source for r in records})
    tiers = [profile(s).tier for s in sources] or [DEFAULT_PROFILE.tier]
    best_tier = min(tiers)
    best_source = next(s for s in sources if profile(s).tier == best_tier) if sources else ""
    reasons = [f"best source tier {best_tier} ({TIER_LABEL[best_tier]}: {best_source}) +{TIER_POINTS[best_tier]}"]
    score = TIER_POINTS[best_tier]

    comp = max((completeness(r) for r in records), default=0.0)
    comp_points = round(30 * comp)
    score += comp_points
    reasons.append(f"completeness {int(comp * 100)}% +{comp_points}")

    n = len(sources)
    corr = 0 if n <= 1 else 12 if n == 2 else 20
    if corr:
        reasons.append(f"corroborated by {n} databases +{corr}")
    score += corr

    n_ids = len({k for r in records for k in r.identifiers})
    id_points = min(10, 3 * n_ids)
    score += id_points
    if id_points:
        reasons.append(f"{n_ids} cross-reference identifier type(s) +{id_points}")

    flags: list[str] = []
    for record in records:
        for flag in record_flags(record):
            if flag not in flags:
                flags.append(flag)
    if conflicts:
        penalty = min(20, 7 * len(conflicts))
        score -= penalty
        flags.append("sources disagree on: " + ", ".join(sorted(conflicts)))
        reasons.append(f"{len(conflicts)} cross-source conflict(s) -{penalty}")
    penalties = {"retracted": 60, "obsolete term": 35, "trial withdrawn": 15, "trial terminated": 10,
                 "trial suspended": 10, "unreviewed (TrEMBL) entry": 10, "preprint (not peer reviewed)": 8,
                 "electronic annotation only (IEA)": 8}
    for flag in flags:
        if flag in penalties:
            score -= penalties[flag]
            reasons.append(f"{flag} -{penalties[flag]}")
    score = max(0, min(100, score))
    if "retracted" in flags:
        score = min(score, 20)
    return Evaluation(
        score=score,
        grade=_grade(score),
        tier=best_tier,
        source_kind=profile(best_source).kind if best_source else DEFAULT_PROFILE.kind,
        corroborated_by=sources,
        completeness=comp,
        flags=flags,
        reasons=reasons,
    )
