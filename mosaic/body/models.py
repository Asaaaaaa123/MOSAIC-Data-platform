"""Body map results: per-organ evidence from the databases, key facts and checks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["treats", "studied", "disease", "phenotype", "adverse_event", "label", "toxicity", "expression"]
Level = Literal["primary", "safety", "investigational", "expression"]

# How each kind of evidence colours an organ: where it is approved to act or the disease is (primary),
# where harm is reported (safety), where it is only being tested in trials (investigational), or only
# where its protein targets / the disease's genes are made (expression).
LEVEL_OF: dict[str, Level] = {
    "treats": "primary", "disease": "primary", "phenotype": "primary",
    "adverse_event": "safety", "label": "safety", "toxicity": "safety",
    "studied": "investigational", "expression": "expression",
}
LEVEL_LABELS: dict[str, str] = {
    "primary": "Approved use / disease site",
    "safety": "Adverse effects reported",
    "investigational": "In clinical trials",
    "expression": "Target / gene expressed",
}
KIND_LABELS: dict[str, str] = {
    "treats": "Treats (approved indication)",
    "studied": "Tested in clinical trials for",
    "disease": "Disease site",
    "phenotype": "Clinical feature (HPO)",
    "adverse_event": "Adverse event reports (FAERS)",
    "label": "FDA label warning",
    "toxicity": "Toxicity (PubChem / Haz-Map)",
    "expression": "Target expressed (GTEx)",
}


class Evidence(BaseModel):
    kind: EvidenceKind
    organ: str
    text: str = Field(description="What the database says, e.g. 'Non-small cell lung carcinoma (approved)'")
    matched: str = Field(description="The words or tissue that put it on this organ")
    value: float | None = None
    unit: str | None = None
    weight: float = 1.0
    source: str
    record_id: str | None = None
    url: str | None = None


class OrganFinding(BaseModel):
    key: str
    en: str
    level: Level
    score: float
    summary: str = Field(description="One-line reason shown next to the organ")
    kinds: dict[str, int] = Field(default_factory=dict, description="evidence kind -> number of items")
    evidence: list[Evidence] = Field(default_factory=list)


class KeyFact(BaseModel):
    label: str
    value: str
    source: str | None = None
    url: str | None = None


class BodyCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class BodyMap(BaseModel):
    map_id: str
    query: str
    kind: Literal["drug", "compound", "disease"]
    name: str
    identifiers: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    generated_at: str
    organs: list[OrganFinding] = Field(default_factory=list, description="Organs with evidence, most relevant first")
    minor: list[Evidence] = Field(default_factory=list, description="Evidence below the threshold for highlighting")
    facts: list[KeyFact] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    checks: list[BodyCheck] = Field(default_factory=list)
