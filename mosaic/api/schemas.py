"""Request bodies for the JSON API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallRequest(_Body):
    source: str = Field(min_length=1, max_length=64, description="Connector name, e.g. 'crossref'")
    tool: str = Field(min_length=1, max_length=128, description="Tool name, e.g. 'search_works'")
    params: dict[str, Any] = Field(default_factory=dict, description="Tool parameters (see GET /api/tools)")
    timeout: float = Field(60.0, gt=0, le=300, description="Seconds before the call is abandoned")


class SearchRequest(_Body):
    query: str = Field(min_length=1, max_length=1000)
    categories: list[str] | None = Field(None, description="Restrict to these source categories")
    sources: list[str] | None = Field(None, description="Restrict to these sources (overrides categories)")
    limit_per_source: int = Field(5, ge=1, le=50)
    timeout: float = Field(30.0, gt=0, le=120)

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class ResolveRequest(_Body):
    kind: Literal["gene", "drug", "disease", "article", "variant"]
    query: str = Field(min_length=1, max_length=300)


class ChatRequest(_Body):
    message: str = Field(min_length=1, max_length=50_000)
    session_id: str | None = Field(None, pattern=SESSION_ID_PATTERN)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class HealthRequest(_Body):
    sources: list[str] | None = Field(None, description="Sources to check (default: all)")
    timeout: float = Field(20.0, gt=0, le=120)
