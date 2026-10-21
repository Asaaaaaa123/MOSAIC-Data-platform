"""Unpaywall connector: open-access status and legal free full-text locations by DOI.

Docs: https://unpaywall.org/products/api (data format: https://unpaywall.org/data-format).

Auth: every request must carry ``email=<contact address>``; the email acts as the
credential (Unpaywall identifies and throttles callers by it), so
``BIOPHA_CONTACT_EMAIL`` is a required setting. ``email`` is redacted from
request URLs by the HTTP layer.

Rate limits: 100,000 calls/day; the connector uses 5 req/s.

Quirks:
- Only DOI lookup exists. The title search endpoint ``/v2/search`` was retired
  on 2026-09-18 (HTTP 410, "use OpenAlex instead"), so this connector has NO
  federated free-text search; use openalex.search_works for discovery.
- Unknown DOIs return HTTP 404 (handled as an empty result + note).
- Unpaywall now runs on the OpenAlex database.

Identifiers: doi, issn (ISSN-L), pmcid when an OA location is in PubMed Central.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..ids import normalize_doi
from ..models import HealthResult, Record, ToolResult
from .base import Connector, ToolParams, tool

API = "https://api.unpaywall.org/v2"

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_PMC_RE = re.compile(r"(?:pubmedcentral\.nih\.gov:|/pmc/articles/(?:PMC)?)(\d+)", re.IGNORECASE)


class OaStatusParams(ToolParams):
    doi: str = Field(description="DOI, e.g. '10.1038/s41586-019-1711-4' (https://doi.org/ or doi: prefixes accepted)")


class OaStatusBatchParams(ToolParams):
    dois: list[str] = Field(
        min_length=1, max_length=50, description="Up to 50 DOIs (any prefix form); looked up concurrently"
    )


def _location(loc: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "url": loc.get("url_for_landing_page") or loc.get("url"),
            "pdf_url": loc.get("url_for_pdf"),
            "license": loc.get("license"),
            "version": loc.get("version"),
            "host_type": loc.get("host_type"),
            "repository": loc.get("repository_institution"),
            "oa_date": loc.get("oa_date"),
            "is_best": loc.get("is_best") or None,
        }.items()
        if v not in (None, "")
    }


def oa_record(item: dict[str, Any]) -> Record:
    doi = normalize_doi(item.get("doi") or "")
    best = item.get("best_oa_location") or {}
    locations = item.get("oa_locations") or []
    pmcid = None
    for loc in [best] + locations:
        for value in (loc.get("pmh_id"), loc.get("url_for_landing_page"), loc.get("url")):
            match = _PMC_RE.search(value or "")
            if match:
                pmcid = f"PMC{match.group(1)}"
                break
        if pmcid:
            break
    authors = []
    for a in item.get("z_authors") or []:
        name = a.get("raw_author_name") or " ".join(x for x in (a.get("given"), a.get("family")) if x)
        if name:
            authors.append(name)
    genre = item.get("genre") or ""
    issns = (item.get("journal_issns") or "").split(",")
    attributes = {
        k: v
        for k, v in {
            "is_oa": item.get("is_oa"),
            "oa_status": item.get("oa_status"),
            "best_oa_url": best.get("url_for_landing_page") or best.get("url"),
            "best_oa_pdf": best.get("url_for_pdf"),
            "best_oa_license": best.get("license"),
            "best_oa_version": best.get("version"),
            "best_oa_host_type": best.get("host_type"),
            "has_repository_copy": item.get("has_repository_copy"),
            "journal": item.get("journal_name"),
            "journal_is_oa": item.get("journal_is_oa"),
            "journal_is_in_doaj": item.get("journal_is_in_doaj"),
            "publisher": item.get("publisher"),
            "genre": genre or None,
            "oa_locations": [_location(loc) for loc in locations[:6]] or None,
            "oa_locations_count": len(locations) if len(locations) > 6 else None,
            "updated": item.get("updated"),
        }.items()
        if v not in (None, "", [])
    }
    return Record(
        source="unpaywall",
        id=doi,
        type="dataset" if genre == "dataset" else "preprint" if genre == "posted-content" else "book"
        if genre.startswith("book") else "article",
        title=item.get("title"),
        url=item.get("doi_url") or (f"https://doi.org/{doi}" if doi else None),
        identifiers={
            "doi": doi,
            "issn": item.get("journal_issn_l") or (issns[0].strip() if issns and issns[0] else None),
            "pmcid": pmcid,
        },
        date=item.get("published_date") or (str(item["year"]) if item.get("year") else None),
        authors=authors,
        attributes=attributes,
    )


class Unpaywall(Connector):
    name = "unpaywall"
    title = "Unpaywall"
    category = "literature"
    description = (
        "Open-access status for any Crossref DOI (~150M works): whether a legal free copy exists, OA type "
        "(gold/green/hybrid/bronze/closed), best PDF/landing URL, license and version, from publishers and "
        "repositories such as PubMed Central. Use to find free full text for known DOIs; it has no search."
    )
    homepage = "https://unpaywall.org/"
    docs_url = "https://unpaywall.org/products/api"
    auth = "api_key"  # the contact email is the credential
    required_settings = ("biopha_contact_email",)
    rate_limit = 5.0
    health_query = "10.1038/s41586-019-1711-4"

    def _email(self) -> dict[str, str]:
        return {"email": self.setting("biopha_contact_email") or ""}

    async def _lookup(self, doi: str) -> tuple[Record | None, str]:
        response = await self.request(
            "GET", f"{API}/{quote(doi, safe='/')}", params=self._email(), allow_status=(404, 422)
        )
        if response.status in (404, 422):
            return None, response.url
        return oa_record(response.json() or {}), response.url

    @tool(
        description="""
        Open-access status for ONE DOI from Unpaywall: is_oa, oa_status (gold, green, hybrid, bronze,
        diamond, closed), best OA location (landing URL, PDF URL, license, version
        submitted/accepted/published, host_type publisher/repository), whether the journal is fully OA /
        in DOAJ, and a compact list of all OA locations (e.g. PubMed Central, institutional repositories).
        Use to find a legal free full-text copy of a known paper. Accepts '10.xxxx/...' with or without
        https://doi.org/ or doi: prefix. For many DOIs use get_oa_status_batch.
        """
    )
    async def get_oa_status(self, params: OaStatusParams) -> ToolResult:
        doi = normalize_doi(params.doi)
        if not _DOI_RE.match(doi):
            return ToolResult(
                source=self.name, tool="get_oa_status",
                notes=[f"{params.doi!r} is not a DOI (expected 10.<registrant>/<suffix>); nothing looked up"],
            )
        record, url = await self._lookup(doi)
        if record is None:
            return ToolResult(
                source=self.name, tool="get_oa_status", request_url=url,
                notes=[f"DOI {doi} not found in Unpaywall (only Crossref-registered DOIs are covered)"],
            )
        return ToolResult(source=self.name, tool="get_oa_status", records=[record], total=1, request_url=url)

    @tool(
        description="""
        Open-access status for up to 50 DOIs at once (looked up concurrently, rate-limited): same fields as
        get_oa_status per DOI. Use to check which papers in a result set (e.g. from PubMed, Crossref or
        OpenAlex) have free full text. DOIs not in Unpaywall are listed in notes.
        """
    )
    async def get_oa_status_batch(self, params: OaStatusBatchParams) -> ToolResult:
        dois: list[str] = []
        invalid: list[str] = []
        for raw in params.dois:
            doi = normalize_doi(raw)
            if not _DOI_RE.match(doi):
                invalid.append(raw)
            elif doi not in dois:
                dois.append(doi)
        results = await asyncio.gather(*(self._lookup(d) for d in dois), return_exceptions=True)
        records, missing, failed = [], [], []
        for doi, result in zip(dois, results):
            if isinstance(result, SourceError):
                failed.append(f"{doi} ({result.status or 'error'})")
            elif isinstance(result, BaseException):
                raise result
            elif result[0] is None:
                missing.append(doi)
            else:
                records.append(result[0])
        if failed and not records and not missing:
            raise SourceError(self.name, f"all {len(failed)} lookups failed: {', '.join(failed[:5])}")
        notes = []
        if invalid:
            notes.append(f"{len(invalid)} value(s) are not DOIs and were skipped: {', '.join(repr(i) for i in invalid[:10])}")
        if missing:
            notes.append(f"{len(missing)} DOI(s) not found in Unpaywall: {', '.join(missing[:20])}")
        if failed:
            notes.append(f"{len(failed)} lookup(s) failed upstream: {', '.join(failed[:20])}")
        oa = sum(1 for r in records if r.attributes.get("is_oa"))
        return ToolResult(
            source=self.name,
            tool="get_oa_status_batch",
            records=records,
            total=len(records),
            data={"requested": len(dois), "found": len(records), "open_access": oa},
            request_url=f"{API}/{{doi}}?email=***",
            notes=notes,
        )

    async def healthcheck(self) -> HealthResult:
        """No search endpoint: health = a known-DOI lookup."""
        if not self.configured:
            return HealthResult(source=self.name, ok=False, detail=f"missing {', '.join(self.missing_settings())}")
        started = time.perf_counter()
        try:
            result = await self.call("get_oa_status", {"doi": self.health_query})
        except Exception as exc:
            return HealthResult(
                source=self.name, ok=False, latency_ms=(time.perf_counter() - started) * 1000, detail=str(exc)[:300]
            )
        return HealthResult(
            source=self.name,
            ok=bool(result.records),
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=f"get_oa_status: {len(result.records)} record(s)",
        )
