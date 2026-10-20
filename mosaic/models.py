"""Normalized data model shared by every connector."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .ids import normalize_identifiers

RecordType = Literal[
    "article",
    "preprint",
    "book",
    "journal",
    "author",
    "institution",
    "dataset",
    "repository",
    "trial",
    "drug",
    "compound",
    "substance",
    "target",
    "gene",
    "protein",
    "structure",
    "variant",
    "association",
    "disease",
    "phenotype",
    "pathway",
    "ontology_term",
    "adverse_event",
    "drug_label",
    "recall",
    "assay",
    "activity",
    "sample",
    "study",
    "grant",
    "other",
]


class Record(BaseModel):
    """One entity returned by a source, normalized for cross-database linking."""

    source: str = Field(description="Connector name that produced this record")
    id: str = Field(description="Source-native identifier")
    type: RecordType = "other"
    title: str | None = None
    url: str | None = Field(default=None, description="Human-facing landing page")
    identifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Canonical identifiers (see mosaic.ids.IDENTIFIER_KEYS)",
    )
    date: str | None = Field(default=None, description="ISO date: YYYY, YYYY-MM or YYYY-MM-DD")
    authors: list[str] = Field(default_factory=list)
    summary: str | None = Field(default=None, description="Abstract / description")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Source-specific fields")

    @field_validator("identifiers", mode="before")
    @classmethod
    def _normalize_ids(cls, value: Any) -> dict[str, str]:
        return normalize_identifiers(value or {})

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, value: Any) -> str:
        return str(value)

    def compact(self, summary_chars: int = 600) -> dict[str, Any]:
        """Dict for the LLM: drops empties and truncates long text."""
        data = self.model_dump(exclude_none=True)
        for key in ("identifiers", "authors", "attributes"):
            if not data.get(key):
                data.pop(key, None)
        if data.get("authors") and len(data["authors"]) > 8:
            data["authors"] = data["authors"][:8] + [f"... (+{len(self.authors) - 8} more)"]
        if self.summary and len(self.summary) > summary_chars:
            data["summary"] = self.summary[:summary_chars].rstrip() + " ..."
        return data


class ToolResult(BaseModel):
    """What every connector tool returns."""

    source: str
    tool: str
    query: dict[str, Any] = Field(default_factory=dict, description="Validated input params")
    records: list[Record] = Field(default_factory=list)
    total: int | None = Field(default=None, description="Total hits the source reports, if known")
    next_cursor: str | None = Field(default=None, description="Opaque token/offset for the next page")
    data: Any = Field(
        default=None,
        description="Non-record payload (counts, aggregations, SQL rows, raw JSON excerpt)",
    )
    notes: list[str] = Field(default_factory=list, description="Warnings / caveats for the user")
    request_url: str | None = Field(default=None, description="Upstream URL with secrets redacted")

    def to_llm(self, max_chars: int = 40_000, summary_chars: int = 600) -> str:
        """Serialize compactly for a Claude tool_result, staying under ``max_chars``.

        Shrinks summaries first, then drops trailing records, and says so in
        ``notes`` so the model knows the payload was trimmed.
        """
        records = list(self.records)
        notes = list(self.notes)
        for chars in (summary_chars, 300, 120, 0):
            step_notes = notes
            if chars < summary_chars:
                step_notes = notes + [
                    "summaries trimmed to fit the context budget; fetch individual records for full abstracts"
                    if chars
                    else "summaries omitted (trimmed) to fit the context budget; fetch individual records for abstracts"
                ]
            payload = self._payload(records, step_notes, chars)
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) <= max_chars:
                return text
        while records:
            dropped = max(1, len(records) // 4)
            records = records[:-dropped]
            trim_note = (
                f"output trimmed to {len(records)} of {len(self.records)} records to fit the context budget; "
                "request a smaller page or fetch records individually"
            )
            payload = self._payload(records, notes + [trim_note], 0)
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) <= max_chars:
                return text
        payload = self._payload([], notes + ["payload too large; data truncated"], 0)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return text[:max_chars]

    def _payload(self, records: list[Record], notes: list[str], summary_chars: int) -> dict[str, Any]:
        payload: dict[str, Any] = {"source": self.source, "tool": self.tool}
        if self.total is not None:
            payload["total"] = self.total
        payload["returned"] = len(records)
        if self.next_cursor:
            payload["next_cursor"] = self.next_cursor
        if notes:
            payload["notes"] = notes
        rendered = []
        for record in records:
            item = record.compact(summary_chars=max(summary_chars, 1))
            if summary_chars == 0:
                item.pop("summary", None)
            item.pop("source", None)
            rendered.append(item)
        if rendered:
            payload["records"] = rendered
        if self.data is not None:
            payload["data"] = self.data
        return payload


class ToolSpec(BaseModel):
    """Public description of a connector tool (for the API, UI and Claude)."""

    source: str
    name: str
    qualified_name: str
    description: str
    input_schema: dict[str, Any]
    federated: bool = False


class SourceInfo(BaseModel):
    name: str
    title: str
    category: str
    description: str
    homepage: str
    docs_url: str | None = None
    auth: str
    configured: bool
    missing_settings: list[str] = Field(default_factory=list)
    nih_repository: str | None = None
    tools: list[ToolSpec] = Field(default_factory=list)


class HealthResult(BaseModel):
    source: str
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None
