"""PhysioNet connector (physiologic signal, clinical and imaging datasets, software and models).

Docs: https://github.com/MIT-LCP/physionet-build/blob/dev/physionet-django/export/views.py
      (no published reference page; the routes live in ``export/urls.py``)
Base: https://physionet.org/api/v1/
Auth: none for metadata. Data files of Credentialed / Restricted / Contributor Review projects
      need a PhysioNet account, a signed DUA and (credentialed) identity verification + CITI
      training; this connector only reads metadata.
Rate limits: the API code declares DRF throttles of 20 requests/hour for anonymous clients
      (100/hour authenticated); not observed to be enforced (Oct 2026), but searches and version
      lists are cached for 6 h to stay polite.

Endpoints used
- ``project/published/search/?search_term=&resource_type=``: latest version of every matching
  project (all terms must match; no pagination; 4-25 s). Fields: slug, version, title,
  short_description, abstract (HTML), core_doi (all versions), version_doi, is_latest_version,
  publish_date, license, dua, access_policy (Open | Credentialed | Restricted | Contributor
  Review), resource_type (Database | Software | Challenge | Model), topics, storage sizes,
  source_url. Its ordering is weak (term frequency), so we re-rank client-side (title/slug/
  topics weighted, canonical slug boosted). Without search_term it lists everything.
- ``project/published/`` lists every version of every project but takes 60-140 s to render
  (~1.3 MB), so it is not used.
- ``project/published/{slug}/``: version list with ``citation`` (the only place authors appear).
  Unknown slug -> 200 ``[]``.
- ``project/published/{slug}/{version}/``: one version with ``doi``, ``publish_datetime``,
  ``project_home_page``. Unknown -> 404 ``{"detail": ...}``.
- DOIs are resolved through DataCite (``api.datacite.org/dois/{doi}`` -> landing URL), because
  the PhysioNet API has no DOI lookup.

Identifier keys: ``doi`` (the version DOI, which is what papers cite), ``physionet``
(non-canonical: the project slug, e.g. ``mimiciv``). The version-independent "core" DOI is kept
in attributes (``core_doi``).
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://physionet.org/api/v1"
WEB = "https://physionet.org"
DATACITE = "https://api.datacite.org/dois"
SEARCH_TTL = 6 * 3600

AccessPolicy = Literal["open", "credentialed", "restricted", "contributor_review"]
ResourceType = Literal["database", "software", "challenge", "model"]

ACCESS_REQUIREMENTS: dict[str, str] = {
    "open": "Open access: files can be downloaded by anyone without an account, under the stated license.",
    "credentialed": (
        "Credentialed access: requires a PhysioNet account with credentialing (identity verification and "
        "completed human-subjects research training such as CITI 'Data or Specimens Only Research') and "
        "signing the project's data use agreement before files can be downloaded."
    ),
    "restricted": (
        "Restricted access: requires a logged-in PhysioNet user to sign the project's data use agreement "
        "before files can be downloaded."
    ),
    "contributor_review": (
        "Contributor Review access: requires credentialing, signing the data use agreement AND approval of a "
        "project-specific data request by the data contributors."
    ),
}


class SearchProjectsParams(SearchParams):
    access_policy: AccessPolicy | None = Field(
        None,
        description="Only projects with this access policy: open (download freely), credentialed (PhysioNet "
        "credentialing + DUA, e.g. MIMIC), restricted (DUA only), contributor_review (DUA + contributor approval)",
    )
    resource_type: ResourceType | None = Field(
        None, description="Only this resource type: database, software, challenge or model"
    )


class GetProjectParams(ToolParams):
    project: str = Field(
        description="Project slug ('mimiciv', 'mitdb', 'challenge-2019'), a physionet.org URL "
        "(https://physionet.org/content/mimiciv/3.1/) or a PhysioNet DOI (10.13026/kpb9-mt58)"
    )
    version: str | None = Field(None, description="Version string, e.g. '3.1' or '1.0.0'. Default: latest version")


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {})}


def _policy_key(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().replace(" ", "_")


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-]", version or ""))


def parse_project_ref(value: str) -> tuple[str | None, str | None, str | None]:
    """Return (slug, version, doi) from a slug, physionet.org URL or DOI."""
    text = value.strip()
    m = re.search(r"10\.13026/[^\s/?#]+", text, re.IGNORECASE)
    if m:
        return None, None, normalize_doi(m.group(0))
    m = re.search(r"physionet\.org/(?:content|files|projects)/([a-z0-9][a-z0-9\-_.]*)(?:/([0-9][^/?#]*))?", text, re.I)
    if m:
        return m.group(1).lower(), m.group(2), None
    m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9\-_.]*)(?:/([0-9][^/?#]*))?/?", text)
    if m:
        return m.group(1).lower(), m.group(2), None
    return None, None, None


def _size_gb(value: Any) -> float | None:
    try:
        return round(int(value) / 1e9, 3)
    except (TypeError, ValueError):
        return None


def _authors_from_citation(citation: str | None, title: str | None) -> list[str]:
    if not citation:
        return []
    cut = -1
    if title:
        cut = citation.find(f". {title}")
    if cut < 0:
        cut = citation.find(". ")
    if cut <= 0:
        return []
    names = [n.strip() for n in citation[:cut].split(",")]
    return [n for n in names if n and len(n) < 80]


def _record(item: dict[str, Any], *, citation: str | None = None, full: bool = False) -> Record:
    slug = item.get("slug") or ""
    version = item.get("version") or ""
    policy = item.get("access_policy")
    key = _policy_key(policy)
    license_name = (item.get("license") or {}).get("name") if isinstance(item.get("license"), dict) else None
    dua = (item.get("dua") or {}).get("name") if isinstance(item.get("dua"), dict) else None
    doi = item.get("version_doi") or item.get("doi") or item.get("core_doi")
    url = item.get("source_url") or f"{WEB}/content/{slug}/{version}/"
    attrs: dict[str, Any] = {
        "access_policy": policy,
        "requires_credentialing": key in ("credentialed", "contributor_review") if key else None,
        "license": license_name,
        "data_use_agreement": dua,
        "resource_type": item.get("resource_type"),
        "version": version,
        "is_latest_version": item.get("is_latest_version"),
        "topics": item.get("topics") or None,
        "size_gb": _size_gb(item.get("main_storage_size")),
        "compressed_size_gb": _size_gb(item.get("compressed_storage_size")),
        "core_doi": normalize_doi(item["core_doi"]) if item.get("core_doi") else None,
        "short_description": item.get("short_description") if not full else None,
    }
    if full:
        attrs.update(
            {
                "access_requirements": ACCESS_REQUIREMENTS.get(key or ""),
                "short_description": item.get("short_description"),
                "project_home_page": item.get("project_home_page"),
                "files_url": f"{WEB}/files/{slug}/{version}/" if slug and version else None,
                "download_hint": (
                    f"wget -r -N -c -np {WEB}/files/{slug}/{version}/" if key == "open" and slug else None
                ),
                "citation": citation,
                "uuid": item.get("public_project_uuid"),
            }
        )
    date = item.get("publish_date") or (item.get("publish_datetime") or "")[:10] or None
    return Record(
        source="physionet",
        id=f"{slug}/{version}" if version else slug,
        type="dataset" if (item.get("resource_type") or "Database") in ("Database", "Challenge") else "other",
        title=item.get("title"),
        url=url,
        identifiers={"doi": doi, "physionet": slug},
        date=date,
        authors=_authors_from_citation(citation, item.get("title")),
        summary=strip_html(item.get("abstract")) or item.get("short_description"),
        attributes=_compact(attrs),
    )


_FIELD_WEIGHTS = (("title", 6.0), ("slug", 5.0), ("topics", 4.0), ("text", 2.0))


def _haystacks(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": (item.get("title") or "").lower(),
        "slug": (item.get("slug") or "").lower(),
        "topics": " | ".join(str(t) for t in item.get("topics") or []).lower(),
        "text": ((item.get("short_description") or "") + " | " + (strip_html(item.get("abstract")) or "")).lower(),
    }


def _score(item: dict[str, Any], terms: list[str], phrase: str) -> tuple[int, float]:
    hay = _haystacks(item)
    matched = 0
    score = 0.0
    for term in terms:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(term))
        hit = False
        for field, weight in _FIELD_WEIGHTS:
            if pattern.search(hay[field]):
                score += weight
                hit = True
        matched += hit
    if len(terms) > 1 and phrase:
        for field, weight in _FIELD_WEIGHTS:
            if phrase in hay[field]:
                score += 2 * weight
    # canonical projects: slug equal to / starting with the query, title starting with it
    squashed = re.sub(r"[^a-z0-9]", "", phrase)
    slug = re.sub(r"[^a-z0-9]", "", hay["slug"])
    if squashed and slug == squashed:
        score += 20
    elif squashed and slug.startswith(squashed):
        score += 4
    if phrase and hay["title"].startswith(phrase):
        score += 6
    return matched, score


class PhysioNet(Connector):
    name = "physionet"
    title = "PhysioNet"
    category = "datasets"
    description = (
        "NIH-supported repository of ~550 physiologic-signal and clinical datasets (ECG, EEG, PPG, sleep "
        "polysomnography, ICU/EHR databases such as MIMIC and eICU), software, models and challenges. Every "
        "project lists its access policy (open vs credentialed/restricted), license and DOI."
    )
    homepage = "https://physionet.org/"
    docs_url = "https://github.com/MIT-LCP/physionet-build/blob/dev/physionet-django/export/urls.py"
    auth = "none"
    rate_limit = 1.0
    rate_burst = 3
    health_query = "ecg arrhythmia"
    nih_repositories = ("PhysioNet",)

    async def _search(self, term: str | None, resource_type: str | None) -> tuple[list[dict[str, Any]], str]:
        response = await self.request(
            "GET",
            f"{API}/project/published/search/",
            params={"search_term": term, "resource_type": resource_type},
            cache_ttl=SEARCH_TTL,
            timeout=90,
        )
        payload = response.json()
        if isinstance(payload, dict):  # defensive: in case the endpoint becomes paginated
            payload = payload.get("results") or []
        return [p for p in payload or [] if isinstance(p, dict) and p.get("slug")], response.url

    @tool(
        description="""
        Search PhysioNet published projects (physiologic signal databases: ECG/arrhythmia, EEG, PPG,
        blood pressure, sleep polysomnography, gait, fetal monitoring; clinical/ICU databases such as
        MIMIC-III/IV, eICU, MIMIC-CXR; plus software, models and Computing-in-Cardiology challenges).
        Uses PhysioNet's own search (every term must appear in the title/abstract/topics) and
        re-ranks hits so title/slug/topic matches and canonical projects come first. Each record carries the ACCESS POLICY
        (Open / Credentialed / Restricted / Contributor Review), license, version DOI, size, topics.
        Filter with access_policy='open' to find freely downloadable data. Use '*' as the query to
        list everything (newest first) with the filters. Example: 'atrial fibrillation ecg'.
        For other physiological/neuro data also try OpenNeuro (MRI/EEG BIDS) or DANDI (neurophysiology).
        """,
        federated=True,
    )
    async def search_projects(self, params: SearchProjectsParams) -> ToolResult:
        query = params.query.strip().lower()
        browse = query in ("", "*")
        items, url = await self._search(None if browse else params.query.strip(), params.resource_type)
        if params.access_policy:
            items = [p for p in items if _policy_key(p.get("access_policy")) == params.access_policy]
        if params.resource_type:
            items = [p for p in items if (p.get("resource_type") or "").lower() == params.resource_type]

        notes: list[str] = []
        if browse:
            ranked = sorted(items, key=lambda p: (p.get("publish_date") or "", p.get("slug") or ""), reverse=True)
        else:
            terms = [t for t in re.findall(r"[a-z0-9][a-z0-9\-+.]*", query) if len(t) > 1 or t.isdigit()]
            terms = list(dict.fromkeys(terms)) or [query]
            phrase = " ".join(terms)
            scored = []
            for rank, p in enumerate(items):
                matched, score = _score(p, terms, phrase)
                # the server already applied its own matching: keep its hits even if our tokenizer misses
                scored.append((matched, score, -len(p.get("title") or ""), -rank, p))
            full = sum(1 for s in scored if s[0] == len(terms))
            if len(terms) > 1 and full < len(scored):
                notes.append(
                    f"{full} project(s) match all query terms and are ranked first; the other "
                    f"{len(scored) - full} match only some terms"
                )
            # more terms matched > higher field-weighted score > shorter (canonical) title > server rank
            scored.sort(key=lambda s: s[:4], reverse=True)
            ranked = [s[4] for s in scored]

        offset = parse_offset(params.cursor)
        page = ranked[offset : offset + params.limit]
        records = [_record(p) for p in page]
        gated = sum(1 for r in records if (r.attributes.get("access_policy") or "Open") != "Open")
        if gated:
            notes.append(
                f"{gated} of {len(records)} returned project(s) are not open access (Credentialed/Restricted/"
                "Contributor Review): files need a PhysioNet account and a signed DUA; see get_project"
            )
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search_projects",
            records=records,
            total=len(ranked),
            next_cursor=str(nxt) if page and nxt < len(ranked) else None,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Get one PhysioNet project version in full: title, abstract, authors (from the official
        citation), ACCESS POLICY with what is required to download (open / credentialed / restricted /
        contributor review), license and data use agreement, version DOI and core (all-versions) DOI,
        publication date, topics, size, project home page, files URL and the list of all versions.
        Accepts a slug ('mimiciv', 'ptb-xl'), 'slug/version', a physionet.org URL or a PhysioNet DOI
        (10.13026/...). Default version: latest.
        """
    )
    async def get_project(self, params: GetProjectParams) -> ToolResult:
        slug, version, doi = parse_project_ref(params.project)
        version = params.version or version
        if doi:
            resolved = await self.request("GET", f"{DATACITE}/{quote(doi, safe='/')}", allow_status=(404,))
            payload = resolved.json() if resolved.status == 200 else {}
            landing = (((payload or {}).get("data") or {}).get("attributes") or {}).get("url") or ""
            slug, doi_version, _ = parse_project_ref(landing)
            if not slug or "physionet.org" not in landing:
                return ToolResult(
                    source=self.name,
                    tool="get_project",
                    notes=[f"DOI {doi} does not resolve to a published PhysioNet project"],
                    request_url=resolved.url,
                )
            version = version or doi_version
        if not slug:
            return ToolResult(
                source=self.name,
                tool="get_project",
                notes=[f"could not parse a PhysioNet project from {params.project!r}; expected a slug such as 'mimiciv'"],
            )

        versions_resp = await self.request(
            "GET", f"{API}/project/published/{quote(slug)}/", cache_ttl=6 * 3600, allow_status=(404,)
        )
        versions = versions_resp.json() if versions_resp.status == 200 else []
        versions = [v for v in versions or [] if isinstance(v, dict) and v.get("version")]
        if not versions and not version:
            return ToolResult(
                source=self.name,
                tool="get_project",
                notes=[f"PhysioNet project {slug!r} not found (no published versions)"],
                request_url=versions_resp.url,
            )
        if not version:
            version = max((v["version"] for v in versions), key=_version_key)

        response = await self.request(
            "GET", f"{API}/project/published/{quote(slug)}/{quote(version)}/", allow_status=(404,)
        )
        if response.status == 404:
            known = ", ".join(v["version"] for v in versions)
            return ToolResult(
                source=self.name,
                tool="get_project",
                notes=[f"PhysioNet project {slug!r} version {version!r} not found"
                       + (f"; published versions: {known}" if known else "")],
                request_url=response.url,
            )
        detail = response.json() or {}
        this = next((v for v in versions if v.get("version") == version), {})
        latest = max((v["version"] for v in versions), key=_version_key) if versions else version
        detail.setdefault("is_latest_version", version == latest)
        record = _record(detail, citation=this.get("citation"), full=True)
        record.attributes["versions"] = [v["version"] for v in versions]
        key = _policy_key(detail.get("access_policy"))
        notes = []
        if key and key != "open":
            notes.append(f"ACCESS: {detail.get('access_policy')}. {ACCESS_REQUIREMENTS.get(key, '')}".strip())
        if version != latest:
            notes.append(f"version {version} is not the latest; latest published version is {latest}")
        return ToolResult(
            source=self.name,
            tool="get_project",
            records=[record],
            total=1,
            request_url=response.url,
            notes=notes,
        )
