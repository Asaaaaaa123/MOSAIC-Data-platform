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
    compare: list[str] | None = Field(
        None, max_length=3, description="Up to 3 more queries to compare with the first (each searched the same way)"
    )

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @field_validator("compare")
    @classmethod
    def _strip_compare(cls, value: list[str] | None) -> list[str] | None:
        if not value:
            return None
        out: list[str] = []
        for item in value:
            item = (item or "").strip()
            if len(item) > 1000:
                raise ValueError("each compared query must be at most 1000 characters")
            if item and item.lower() not in {o.lower() for o in out}:
                out.append(item)
        return out or None

    def queries(self) -> list[str]:
        """The query, then the compared queries (case-insensitive duplicates of the query dropped)."""
        return [self.query] + [q for q in self.compare or [] if q.lower() != self.query.lower()]


class SearchExportRequest(SearchRequest):
    group_by: Literal["type", "species", "source"] = Field(
        "type", description="Split into one table per data type (papers, trials ...), per species, or per database"
    )
    name: str | None = Field(None, max_length=120, description="Name for the package (default: the query)")
    full_records: bool = Field(True, description="Re-fetch every hit's complete record from its source before exporting")
    download_files: bool = Field(True, description="Include small open data tables from the datasets' repositories")
    include_figures: bool = Field(
        True, description="Add the mind map and charts (SVG, PNG, PDF via Figure Studio), figure_data.csv and mindmap.json"
    )


class SpeciesCompareRequest(_Body):
    query: str = Field(min_length=1, max_length=200, description="Drug or chemical compound name, ChEMBL ID or InChIKey")
    species: list[str] | None = Field(None, max_length=20, description="Species keys (GET /api/species/meta); default all")
    max_papers: int = Field(5, ge=1, le=20, description="Top PubMed papers kept per species")
    extract_with_claude: bool = Field(False, description="Extract dose / effect from abstracts with Claude (uses API credits)")

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class BodyMapRequest(_Body):
    query: str = Field(min_length=1, max_length=200, description="Drug, chemical compound or disease (name or ID)")
    kind: Literal["auto", "drug", "compound", "disease"] = Field(
        "auto", description="What the query is; auto decides from exact matches in the identifier resolvers"
    )

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
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
