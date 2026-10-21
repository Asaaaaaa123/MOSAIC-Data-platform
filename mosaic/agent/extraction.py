"""Cross-database extraction: a separate Claude structured-output call over stored records.

``extract_structured`` hands this module the *full* records behind earlier
result handles (complete abstracts and attributes, not the trimmed text Claude
saw). Records are chunked, each chunk is sent to Claude with
``output_config.format`` (a JSON schema built from the requested fields), and
the rows come back keyed by a ``ref`` Mosaic assigned, so provenance (source,
record id, URL, result handle) is attached from Mosaic's own data rather than
from model output.

A chunk that stops on ``max_tokens`` or returns unparseable JSON is split in
half and retried; a refused chunk is reported in ``notes``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import anthropic
from pydantic import Field, field_validator

from ..connectors.base import ToolParams
from ..models import Record
from .claude_api import add_usage, effort_setting, fallback_kwargs, usage_counts

log = logging.getLogger("mosaic.agent.extraction")

FieldType = Literal["string", "number", "integer", "boolean", "array"]

# Provenance columns Mosaic fills itself; requested fields may not reuse them.
PROVENANCE_COLUMNS = ("source", "record_id", "title")
RESERVED_NAMES = frozenset({"ref", "source", "record_id", "title", "url", "result_id", "evidence"})

MAX_FIELDS = 16  # keeps the output schema well inside structured-output grammar limits
MAX_RECORDS = 500
CHUNK_CHARS = 60_000
CHUNK_RECORDS = 25
MAX_SPLITS = 3
CONCURRENCY = 4
MAX_TOKENS = 32_000
MAX_AUTHORS = 25

SYSTEM_PROMPT = """\
You are the extraction component of Mosaic, a biomedical research platform. You read records \
returned by biomedical, literature, drug and clinical databases and fill a fixed table schema.

Rules:
- Use only information stated in the record itself. Do not add facts from memory.
- Use null when the record does not state a value. An empty or partial table is better than a guess.
- Copy identifiers exactly as written in the record; never construct or infer an identifier.
- Keep numbers as numbers (no units) unless the field description asks otherwise.
- "evidence" is a short verbatim quote (under 200 characters) from the record that supports the \
values you filled, or null when every field is null.
- Return exactly one row per record, with "ref" copied from the record."""


class FieldSpec(ToolParams):
    name: str = Field(
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$",
        description="Column name: letters, digits and underscores, starting with a letter (e.g. 'sample_size')",
    )
    type: FieldType = Field("string", description="Value type: string, number, integer, boolean or array (of strings)")
    description: str = Field(min_length=1, max_length=1000, description="What to put in this column and how")

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.lower() in RESERVED_NAMES:
            raise ValueError(f"'{value}' is reserved for provenance columns; choose another name")
        return value


@dataclass(frozen=True)
class SourceRecord:
    result_id: str
    record: Record


@dataclass
class ExtractionTable:
    columns: list[str]
    rows: list[dict[str, Any]]
    provenance: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


def _value_schema(spec: FieldSpec) -> dict[str, Any]:
    if spec.type == "array":
        base: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    else:
        base = {"type": spec.type}
    return {"anyOf": [base, {"type": "null"}], "description": spec.description}


def output_schema(fields: list[FieldSpec]) -> dict[str, Any]:
    properties: dict[str, Any] = {"ref": {"type": "string", "description": "The record's ref, copied exactly"}}
    for spec in fields:
        properties[spec.name] = _value_schema(spec)
    properties["evidence"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": "Short verbatim quote from the record supporting the values",
    }
    row = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"rows": {"type": "array", "items": row}},
        "required": ["rows"],
        "additionalProperties": False,
    }


def _render(ref: str, record: Record, budget: int) -> tuple[str, bool]:
    """One record as compact JSON for the prompt; trims only if it alone exceeds ``budget``."""
    data = record.model_dump(exclude_none=True)
    for key in ("identifiers", "authors", "attributes"):
        if not data.get(key):
            data.pop(key, None)
    if len(data.get("authors") or []) > MAX_AUTHORS:
        data["authors"] = data["authors"][:MAX_AUTHORS] + [f"... (+{len(record.authors) - MAX_AUTHORS} more)"]
    item = {"ref": ref, **data}
    text = json.dumps(item, ensure_ascii=False, default=str)
    if len(text) <= budget:
        return text, False
    item.pop("attributes", None)
    text = json.dumps(item, ensure_ascii=False, default=str)
    while len(text) > budget and item.get("summary"):
        summary = str(item["summary"]).removesuffix(" ...[truncated]")
        keep = max(0, len(summary) - (len(text) - budget) - 32)
        item["summary"] = summary[:keep] + " ...[truncated]" if keep else None
        if item["summary"] is None:
            item.pop("summary")
        text = json.dumps(item, ensure_ascii=False, default=str)
    if len(text) > budget:  # pathological: huge title or identifier map
        item = {"ref": ref, "source": record.source, "id": record.id, "title": (record.title or "")[:1000]}
        text = json.dumps(item, ensure_ascii=False, default=str)
    return text, True


def _chunks(rendered: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for ref, text in rendered:
        if current and (size + len(text) > CHUNK_CHARS or len(current) >= CHUNK_RECORDS):
            chunks.append(current)
            current, size = [], 0
        current.append((ref, text))
        size += len(text) + 1
    if current:
        chunks.append(current)
    return chunks


def _prompt(chunk: list[tuple[str, str]], fields: list[FieldSpec], instructions: str | None) -> str:
    lines = []
    if instructions:
        lines += ["<instructions>", instructions.strip(), "</instructions>", ""]
    lines.append("Extract these fields from every record below:")
    for spec in fields:
        lines.append(f"- {spec.name} ({spec.type}): {spec.description}")
    lines += ["", f"<records count=\"{len(chunk)}\">"]
    lines += [text for _, text in chunk]
    lines.append("</records>")
    return "\n".join(lines)


class StructuredExtractor:
    """Runs extraction calls. ``client_getter`` returns the shared AsyncAnthropic client."""

    def __init__(
        self,
        client_getter: Callable[[], anthropic.AsyncAnthropic | None],
        *,
        model: str,
        effort: str | None = None,
        fallbacks: bool = True,
        audit: Any = None,
    ):
        self._client_getter = client_getter
        self.model = model
        self.effort = effort_setting(effort, "low")
        self.fallbacks = fallbacks
        self.audit = audit

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        client_getter: Callable[[], anthropic.AsyncAnthropic | None],
        *,
        fallbacks: bool,
        audit: Any = None,
    ) -> "StructuredExtractor":
        return cls(
            client_getter,
            model=settings.env("MOSAIC_EXTRACTION_MODEL") or settings.mosaic_model,
            effort=settings.env("MOSAIC_EXTRACTION_EFFORT"),
            fallbacks=fallbacks,
            audit=audit,
        )

    async def extract(
        self,
        items: list[SourceRecord],
        fields: list[FieldSpec],
        instructions: str | None = None,
        *,
        session_id: str | None = None,
    ) -> ExtractionTable:
        client = self._client_getter()
        if client is None:
            raise RuntimeError("Claude is not configured (ANTHROPIC_API_KEY), so extraction cannot run")
        notes: list[str] = []
        usage: dict[str, int] = {}
        by_ref: dict[str, SourceRecord] = {}
        rendered: list[tuple[str, str]] = []
        for index, item in enumerate(items, start=1):
            ref = str(index)
            by_ref[ref] = item
            text, trimmed = _render(ref, item.record, CHUNK_CHARS)
            if trimmed:
                notes.append(f"record {item.record.source}:{item.record.id} was trimmed to fit one extraction call")
            rendered.append((ref, text))

        schema = output_schema(fields)
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def run_chunk(chunk: list[tuple[str, str]], depth: int = 0) -> dict[str, dict[str, Any]]:
            async with semaphore:
                message = await self._call(client, _prompt(chunk, fields, instructions), schema)
            add_usage(usage, usage_counts(message.usage))
            self._audit(session_id, message, len(chunk))
            if message.stop_reason == "refusal":
                details = message.stop_details
                category = getattr(details, "category", None) or "unspecified"
                notes.append(f"{len(chunk)} record(s) not extracted: Claude declined (category: {category})")
                return {}
            parsed = self._parse(message)
            if parsed is None:
                if len(chunk) > 1 and depth < MAX_SPLITS:
                    middle = len(chunk) // 2
                    halves = await asyncio.gather(run_chunk(chunk[:middle], depth + 1), run_chunk(chunk[middle:], depth + 1))
                    return {**halves[0], **halves[1]}
                notes.append(
                    f"{len(chunk)} record(s) not extracted: the extraction output was incomplete "
                    f"(stop_reason {message.stop_reason})"
                )
                return {}
            return {str(row.get("ref")): row for row in parsed if isinstance(row, dict)}

        results = await asyncio.gather(*(run_chunk(c) for c in _chunks(rendered)))
        extracted: dict[str, dict[str, Any]] = {}
        for part in results:
            extracted.update(part)

        names = [spec.name for spec in fields]
        columns = [*PROVENANCE_COLUMNS, *names, "url", "evidence"]
        rows: list[dict[str, Any]] = []
        provenance_rows: list[dict[str, Any]] = []
        missing = 0
        for ref, item in by_ref.items():
            record = item.record
            values = extracted.get(ref)
            if values is None:
                missing += 1
                values = {}
            row = {"source": record.source, "record_id": record.id, "title": record.title}
            row.update({name: values.get(name) for name in names})
            row["url"] = record.url
            row["evidence"] = values.get("evidence")
            rows.append(row)
            provenance_rows.append(
                {"result_id": item.result_id, "source": record.source, "record_id": record.id, "url": record.url}
            )
        if missing:
            notes.append(f"{missing} of {len(by_ref)} row(s) have no extracted values")
        provenance = {
            "method": "extract_structured",
            "model": self.model,
            "effort": self.effort,
            "instructions": instructions,
            "fields": [spec.model_dump() for spec in fields],
            "result_ids": sorted({i.result_id for i in items}, key=_handle_order),
            "rows": provenance_rows,
        }
        return ExtractionTable(columns=columns, rows=rows, provenance=provenance, notes=notes, usage=usage)

    async def _call(self, client: anthropic.AsyncAnthropic, prompt: str, schema: dict[str, Any]) -> Any:
        # Streamed: a large table can take minutes, and the SDK refuses long non-streaming calls.
        async with client.beta.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            **fallback_kwargs(self.fallbacks),
        ) as stream:
            return await stream.get_final_message()

    @staticmethod
    def _parse(message: Any) -> list[Any] | None:
        if message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
            return None
        text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("extraction returned invalid JSON (%d chars)", len(text))
            return None
        rows = data.get("rows") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else None

    def _audit(self, session_id: str | None, message: Any, n_records: int) -> None:
        if self.audit is None or not session_id:
            return
        try:
            self.audit.record_agent_turn(
                session_id=session_id,
                model=getattr(message, "model", None) or self.model,
                stop_reason=message.stop_reason,
                usage=usage_counts(message.usage),
                detail={"purpose": "extract_structured", "records": n_records},
            )
        except Exception:  # noqa: BLE001 - auditing must never break extraction
            log.exception("failed to audit extraction call")


def _handle_order(result_id: str) -> tuple[int, str]:
    digits = result_id[1:]
    return (int(digits), result_id) if digits.isdigit() else (1 << 30, result_id)
