"""Europe PMC connector: articles, preprints, citations, OA full text and text-mined annotations.

Docs: https://europepmc.org/RestfulWebService (REST API,
https://www.ebi.ac.uk/europepmc/webservices/rest/) and
https://europepmc.org/AnnotationsApi (https://www.ebi.ac.uk/europepmc/annotations_api/).

Auth: none. Rate: no published hard limit; EBI asks for moderate use, we stay at ~8 req/s.

Coverage: PubMed/MEDLINE (SRC:MED), PubMed Central (PMC), preprints (PPR: bioRxiv,
medRxiv, Research Square, ...), patents (PAT), Agricola (AGR), Chinese Biological
Abstracts (CBA), CiteXplorer (CIT), theses (ETH), NHS evidence (HIR), CTX.

Quirks:
- Records are addressed by (source, id): for SRC:MED the id is the PMID, for preprints
  a PPR id. Search pagination uses ``cursorMark`` (opaque, passed as our cursor).
- ``fullTextXML`` is only available for the open-access / author-manuscript subset and
  answers HTTP 404 or 500 for everything else; we turn both into a note.
- Citations/references are paged with ``page`` (1-based) + ``pageSize``.
- Annotation tags carry URIs (UniProt, Ensembl, UMLS, ChEBI, GO, identifiers.org...);
  they are mapped to canonical identifier keys. Non-canonical keys used here:
  ``chebi`` (CHEBI:n), ``taxonomy`` (NCBI Taxonomy ID), ``genbank`` (ENA/INSDC accession),
  ``arrayexpress``, ``ega``, ``emdb``, ``interpro``, ``pfam``, ``pride``, ``biostudies``,
  ``uberon``/``cl``/... (other OBO ontologies as PREFIX:ID).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterator, Literal
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ANNOTATIONS = "https://www.ebi.ac.uk/europepmc/annotations_api"
SITE = "https://europepmc.org"

SourceCode = Literal["MED", "PMC", "PPR", "PAT", "AGR", "CBA", "CIT", "CTX", "ETH", "HIR"]
SectionName = Literal["abstract", "introduction", "methods", "results", "discussion", "conclusions", "other"]


# --------------------------------------------------------------------------- params


class SearchArticlesParams(SearchParams):
    sort: Literal["relevance", "date", "cited"] = Field(
        "relevance", description="relevance (default), date (newest first) or cited (most cited first)"
    )
    open_access_only: bool = Field(False, description="Only open-access articles (adds OPEN_ACCESS:y)")
    source: SourceCode | None = Field(
        None, description="Restrict to one Europe PMC source: MED (PubMed), PMC, PPR (preprints), PAT (patents), ..."
    )


class ArticleParams(ToolParams):
    identifier: str = Field(
        description="PMID (e.g. 30742122), PMCID (PMC6408961), DOI (10.1038/...), preprint id (PPR123456) "
        "or SOURCE:ID (e.g. MED:30742122, PPR:PPR123456)"
    )


class CitationParams(ArticleParams):
    limit: int = Field(25, ge=1, le=100, description="Maximum number of records to return")
    cursor: str | None = Field(None, description="Pagination cursor (page number) from a previous result")


class FullTextParams(ArticleParams):
    sections: list[SectionName] | None = Field(
        None, description="Only these sections (default: all): abstract, introduction, methods, results, discussion, "
        "conclusions, other (data availability, extended data, back matter)"
    )
    max_chars: int = Field(
        20000, ge=1000, le=35000, description="Total character budget across returned sections (long sections are truncated)"
    )


class AnnotationParams(ArticleParams):
    types: list[str] | None = Field(
        None,
        description="Annotation types to keep, e.g. ['Gene_Proteins', 'Diseases', 'Chemicals', 'Organisms', "
        "'Gene Ontology', 'Accession Numbers', 'Experimental Methods', 'Cell', 'Organ Tissue', 'Clinical Drug', "
        "'Gene Disease Relationship']. Default: all",
    )
    limit: int = Field(100, ge=1, le=300, description="Maximum number of distinct entities to return")


# --------------------------------------------------------------------------- helpers


def _yes(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    return str(value).upper() == "Y"


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, "", [], {})}


def parse_identifier(value: str) -> tuple[str, str]:
    """Classify an article identifier: ('pmid'|'pmcid'|'doi'|'src', value). 'src' values are 'SRC:ID'."""
    text = value.strip()
    m = re.fullmatch(r"(?i)(MED|PMC|PPR|PAT|AGR|CBA|CIT|CTX|ETH|HIR)[:/](\S+)", text)
    if m:
        src, ident = m.group(1).upper(), m.group(2)
        if src == "PMC" or ident.upper().startswith("PMC"):
            return "pmcid", "PMC" + re.sub(r"(?i)^pmc", "", ident)
        if src == "MED":
            return "pmid", ident
        return "src", f"{src}:{ident}"
    if re.fullmatch(r"(?i)pmid:?\s*\d+", text) or text.isdigit():
        return "pmid", re.search(r"\d+", text).group(0)  # type: ignore[union-attr]
    if re.fullmatch(r"(?i)pmc\d+", text):
        return "pmcid", text.upper()
    if re.fullmatch(r"(?i)ppr\d+", text):
        return "src", f"PPR:{text.upper()}"
    if "10." in text:
        return "doi", normalize_doi(text)
    return "query", text


def _identifier_query(kind: str, value: str) -> str:
    if kind == "pmid":
        return f"EXT_ID:{value} AND SRC:MED"
    if kind == "pmcid":
        return f"PMCID:{value}"
    if kind == "doi":
        return f'DOI:"{value}"'
    if kind == "src":
        src, ident = value.split(":", 1)
        return f"EXT_ID:{ident} AND SRC:{src}"
    return value


def _date(item: dict[str, Any]) -> str | None:
    for key in ("firstPublicationDate", "electronicPublicationDate"):
        if item.get(key):
            return str(item[key])[:10]
    journal = item.get("journalInfo") or {}
    if journal.get("printPublicationDate"):
        return str(journal["printPublicationDate"])[:10]
    return str(item.get("pubYear")) if item.get("pubYear") else None


def _authors(item: dict[str, Any]) -> list[str]:
    authors = []
    for author in (item.get("authorList") or {}).get("author") or []:
        name = " ".join(x for x in (author.get("firstName"), author.get("lastName")) if x)
        name = name or author.get("collectiveName") or author.get("fullName")
        if name:
            authors.append(name)
    if not authors and item.get("authorString"):
        authors = [a.strip().rstrip(".") for a in item["authorString"].split(",") if a.strip()]
    return authors


def _record(item: dict[str, Any]) -> Record:
    source = item.get("source") or "MED"
    ident = str(item.get("id") or item.get("pmid") or item.get("pmcid") or item.get("doi") or "")
    journal_info = item.get("journalInfo") or {}
    journal = journal_info.get("journal") or {}
    pub_types = (item.get("pubTypeList") or {}).get("pubType") or []
    if isinstance(pub_types, str):
        pub_types = [pub_types]
    mesh = []
    for heading in (item.get("meshHeadingList") or {}).get("meshHeading") or []:
        name = heading.get("descriptorName")
        if not name:
            continue
        major = heading.get("majorTopic_YN") == "Y" or any(
            q.get("majorTopic_YN") == "Y" for q in (heading.get("meshQualifierList") or {}).get("meshQualifier") or []
        )
        mesh.append(name + ("*" if major else ""))
    grants = [
        _compact({"agency": g.get("agency"), "id": g.get("grantId"), "acronym": g.get("acronym")})
        for g in (item.get("grantsList") or {}).get("grant") or []
    ]
    chemicals = [
        _compact({"name": c.get("name"), "registry": c.get("registryNumber") if c.get("registryNumber") not in ("0", None) else None})
        for c in (item.get("chemicalList") or {}).get("chemical") or []
    ]
    full_text_ids = (item.get("fullTextIdList") or {}).get("fullTextId") or []
    oa_url = next(
        (u.get("url") for u in (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []
         if u.get("availabilityCode") in ("OA", "F") and u.get("url")),
        None,
    )
    is_preprint = source == "PPR" or any("preprint" in str(p).lower() for p in pub_types)
    rtype = "preprint" if is_preprint else ("other" if source == "PAT" else "article")
    pmcid = item.get("pmcid") or (ident if source == "PMC" else None)
    return Record(
        source="europepmc",
        id=ident,
        type=rtype,  # type: ignore[arg-type]
        title=strip_html(item.get("title")),
        url=f"{SITE}/article/{source}/{ident}" if ident else None,
        identifiers={
            "pmid": item.get("pmid"),
            "pmcid": pmcid,
            "doi": item.get("doi"),
            "issn": journal.get("issn") or journal.get("essn"),
        },
        date=_date(item),
        authors=_authors(item),
        summary=strip_html(item.get("abstractText")),
        attributes=_compact({
            "source": source,
            "journal": journal.get("title"),
            "journal_abbrev": journal.get("isoabbreviation") or journal.get("medlineAbbreviation"),
            "volume": journal_info.get("volume"), "issue": journal_info.get("issue"), "pages": item.get("pageInfo"),
            "cited_by": item.get("citedByCount"),
            "is_open_access": _yes(item.get("isOpenAccess")),
            "has_full_text": bool(full_text_ids) or _yes(item.get("inEPMC")),
            "in_pmc": _yes(item.get("inPMC")),
            "has_pdf": _yes(item.get("hasPDF")),
            "publication_types": pub_types[:10],
            "mesh_terms": mesh[:40],
            "keywords": ((item.get("keywordList") or {}).get("keyword") or [])[:25],
            "grants": [g for g in grants if g][:25],
            "chemicals": [c for c in chemicals if c][:25],
            "text_mined_accession_types": (item.get("tmAccessionTypeList") or {}).get("accessionType"),
            "db_cross_references": (item.get("dbCrossReferenceList") or {}).get("dbName"),
            "license": item.get("license"),
            "language": item.get("language"),
            "oa_url": oa_url,
            "preprint_server": (item.get("bookOrReportDetails") or {}).get("publisher") if source == "PPR" else None,
        }),
    )


def _citation_record(item: dict[str, Any]) -> Record:
    source = item.get("source") or ""
    ident = item.get("id")
    authors = [a.strip().rstrip(".") for a in (item.get("authorString") or "").split(",") if a.strip()]
    title = strip_html(item.get("title")) or strip_html(item.get("publicationTitle"))
    return Record(
        source="europepmc",
        id=str(ident or f"ref-{item.get('citedOrder', '')}"),
        type="preprint" if source == "PPR" else "article",
        title=title,
        url=f"{SITE}/article/{source}/{ident}" if ident and source else None,
        identifiers={
            "pmid": ident if source == "MED" else None,
            "pmcid": ident if source == "PMC" else None,
            "doi": item.get("doi"),
            "issn": item.get("issn") or item.get("essn"),
        },
        date=str(item["pubYear"]) if item.get("pubYear") else None,
        authors=authors,
        attributes=_compact({
            "source": source or None, "journal": item.get("journalAbbreviation"), "volume": item.get("volume"),
            "issue": item.get("issue"), "pages": item.get("pageInfo"), "cited_by": item.get("citedByCount"),
            "citation_type": item.get("citationType"), "cited_order": item.get("citedOrder"),
            "matched": _yes(item.get("match")),
        }),
    )


# --------------------------------------------------------------------------- JATS full text

_SKIP_TAGS = {"table", "disp-formula", "inline-formula", "math", "tex-math", "alternatives", "graphic",
              "inline-graphic", "media", "object-id", "label"}
_BLOCK_TAGS = {"title", "p", "list-item", "sec", "caption", "def", "term"}
_SECTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("introduction", ("intro", "background")),
    ("methods", ("method", "material", "experimental", "patients and", "study design", "procedures", "participants")),
    ("results", ("result", "findings")),
    ("discussion", ("discussion",)),
    ("conclusions", ("conclusion",)),
]


def _local(tag: Any) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _iter_text(elem: ET.Element) -> Iterator[str]:
    tag = _local(elem.tag)
    if tag in _SKIP_TAGS or (tag == "xref" and elem.get("ref-type") in ("bibr", "fig", "table", "supplementary-material")):
        return
    if elem.text:
        yield elem.text
    for child in elem:
        yield from _iter_text(child)
        if _local(child.tag) in _BLOCK_TAGS:
            yield " "
        if child.tail:
            yield child.tail


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\(\s*[,;–-]*\s*\)|\[\s*[,;–-]*\s*\]", "", text)
    return re.sub(r"\s+([,.;:])", r"\1", text).strip()


def _render_section(sec: ET.Element, depth: int = 0) -> list[str]:
    out: list[str] = []
    for child in sec:
        tag = _local(child.tag)
        if tag == "title":
            if depth:
                title = _clean("".join(_iter_text(child)))
                if title:
                    out.append(f"## {title}")
        elif tag == "sec":
            out.extend(_render_section(child, depth + 1))
        elif tag in ("fig", "table-wrap"):
            label = _clean(child.findtext("label") or "")
            caption = child.find("caption")
            text = _clean("".join(_iter_text(caption))) if caption is not None else ""
            if text:
                out.append(f"[{label or tag}] {text}")
        elif tag in ("p", "list", "disp-quote", "boxed-text", "statement", "def-list"):
            text = _clean("".join(_iter_text(child)))
            if text:
                out.append(text)
    return out


def _classify(title: str, sec_type: str | None) -> str:
    probe = f"{sec_type or ''} {title}".lower()
    for name, keys in _SECTION_RULES:
        if any(k in probe for k in keys):
            return name
    return "other"


def parse_jats(xml_bytes: bytes) -> dict[str, Any]:
    """Parse a JATS article into metadata + ordered sections."""
    root = ET.fromstring(xml_bytes)
    meta = root.find("front/article-meta")
    ids: dict[str, str] = {}
    info: dict[str, Any] = {}
    sections: list[dict[str, Any]] = []
    if meta is not None:
        for aid in meta.findall("article-id"):
            kind = aid.get("pub-id-type")
            if kind in ("pmid", "doi") and aid.text:
                ids[kind] = aid.text.strip()
            elif kind in ("pmc", "pmcid", "pmcaid") and aid.text and "pmcid" not in ids:
                value = aid.text.strip()
                ids["pmcid"] = value if value.upper().startswith("PMC") else f"PMC{value}"
        title = meta.find("title-group/article-title")
        info["title"] = _clean("".join(_iter_text(title))) if title is not None else None
        authors = []
        for contrib in meta.findall("contrib-group/contrib"):
            if contrib.get("contrib-type") not in (None, "author"):
                continue
            name = " ".join(x for x in (contrib.findtext("name/given-names"), contrib.findtext("name/surname")) if x)
            name = name or (contrib.findtext("collab") or "").strip()
            if name:
                authors.append(name)
        info["authors"] = authors
        dates = {d.get("pub-type") or d.get("date-type"): d for d in meta.findall("pub-date")}
        date = next((dates[k] for k in ("epub", "pub", "ppub", "collection") if k in dates), None)
        if date is None:
            date = meta.find("pub-date")
        if date is not None and date.findtext("year"):
            parts = [date.findtext("year")] + [f"{int(date.findtext(p)):02d}" for p in ("month", "day") if (date.findtext(p) or "").isdigit()]
            info["date"] = "-".join(parts)
        abstract = meta.find("abstract")
        if abstract is not None:
            text = "\n".join(_render_section(abstract, 1)) or _clean("".join(_iter_text(abstract)))
            if text:
                sections.append({"section": "abstract", "title": "Abstract", "text": text})
    journal = root.find("front/journal-meta/journal-title-group/journal-title")
    info["journal"] = journal.text.strip() if journal is not None and journal.text else None
    body = root.find("body")
    if body is not None:
        loose = [c for c in body if _local(c.tag) == "p"]
        if loose:
            text = "\n".join(_clean("".join(_iter_text(p))) for p in loose)
            sections.append({"section": "other", "title": "Body", "text": text})
        for sec in body.findall("sec"):
            title = _clean("".join(_iter_text(sec.find("title")))) if sec.find("title") is not None else ""
            text = "\n".join(_render_section(sec))
            if text:
                sections.append({"section": _classify(title, sec.get("sec-type")), "title": title or None, "text": text})
    back = root.find("back")
    if back is not None:
        for sec in list(back.findall("sec")) + list(back.findall("notes")):
            title = _clean("".join(_iter_text(sec.find("title")))) if sec.find("title") is not None else ""
            text = "\n".join(_render_section(sec))
            if text:
                sections.append({"section": "other", "title": title or sec.get("sec-type") or "Notes", "text": text})
    return {"ids": ids, **info, "sections": sections}


def _fit_budget(sections: list[dict[str, Any]], budget: int) -> None:
    """Water-fill ``budget`` characters across sections, truncating the longest first."""
    lengths = [len(s["text"]) for s in sections]
    if sum(lengths) <= budget:
        for s in sections:
            s["chars"], s["truncated"] = len(s["text"]), False
        return
    remaining, open_idx = budget, sorted(range(len(sections)), key=lambda i: lengths[i])
    caps = [0] * len(sections)
    while open_idx:
        share = remaining // len(open_idx)
        i = open_idx[0]
        if lengths[i] <= share:
            caps[i] = lengths[i]
            remaining -= lengths[i]
            open_idx.pop(0)
        else:
            for j in open_idx:
                caps[j] = share
            break
    for s, cap, full in zip(sections, caps, lengths):
        s["chars"] = full
        s["truncated"] = full > cap
        if full > cap:
            cut = s["text"][:cap]
            cut = cut[: cut.rfind(" ")] if " " in cut[-200:] else cut
            s["text"] = cut.rstrip() + " [...]"


# --------------------------------------------------------------------------- annotations

_IDORG_KEYS = {
    "geo": "geo", "clinicaltrials": "nct", "doi": "doi", "pdb": "pdb", "uniprot": "uniprot", "ena.embl": "genbank",
    "insdc": "genbank", "ena": "genbank", "refseq": "refseq", "dbsnp": "rsid", "refsnp": "rsid", "arrayexpress": "arrayexpress",
    "bioproject": "bioproject", "biosample": "biosample", "ega.study": "ega", "ega.dataset": "ega", "emdb": "emdb",
    "interpro": "interpro", "pfam": "pfam", "omim": "omim", "mim": "omim", "ensembl": "ensembl_gene", "go": "go",
    "hgnc": "hgnc", "chembl.compound": "chembl", "chembl": "chembl", "assembly": "assembly", "gca": "assembly",
    "eudract": "euctr", "pride": "pride", "pxd": "pride", "biostudies": "biostudies", "rrid": "rrid",
    "cellosaurus": "cellosaurus", "orphanet": "orphanet", "dbgap": "dbgap", "taxonomy": "taxonomy",
    "pubmed": "pmid", "pmc": "pmcid", "mesh": "mesh", "chebi": "chebi", "metabolights": "metabolights",
    "alphafold": "uniprot", "empiar": "empiar", "complexportal": "complexportal", "intact": "intact",
}
_OBO_KEYS = {"EFO": ("efo", "_"), "HP": ("hp", ":"), "MONDO": ("mondo", ":"), "DOID": ("doid", ":"), "GO": ("go", ":"),
             "CHEBI": ("chebi", ":")}
_OBO_PREFIXES = set(_OBO_KEYS) | {"UBERON", "CL", "MI", "SO", "NCIT", "OBI", "BAO", "ORDO", "ORPHANET", "ECO", "PR", "BTO"}
_TYPE_MAP = {
    "Gene_Proteins": "protein", "Diseases": "disease", "Chemicals": "compound", "Clinical Drug": "drug",
    "Gene Ontology": "ontology_term", "Organisms": "other", "Experimental Methods": "ontology_term", "Cell": "ontology_term",
    "Cell Line": "other", "Organ Tissue": "ontology_term", "Sequence": "ontology_term",
}
_ACCESSION_TYPES = {"geo": "dataset", "nct": "trial", "doi": "article", "pdb": "structure", "uniprot": "protein",
                    "rsid": "variant", "arrayexpress": "dataset", "bioproject": "study", "biosample": "sample",
                    "ega": "dataset", "pride": "dataset", "biostudies": "dataset", "genbank": "other", "refseq": "other",
                    "emdb": "structure", "dbgap": "study", "euctr": "trial", "chembl": "compound", "metabolights": "dataset"}


def tag_identifiers(uri: str | None, name: str | None = None) -> dict[str, str]:
    """Map an annotation tag URI to canonical identifiers."""
    if not uri:
        return {}
    m = re.search(r"uniprot\.org/uniprot(?:kb)?/([A-Z0-9]+)", uri, re.I)
    if m:
        return {"uniprot": m.group(1)}
    m = re.search(r"[?;&]g=(ENS[A-Z]*G\d+)", uri) or re.search(r"ensembl\.org/id/(ENS[A-Z]*G\d+)", uri)
    if m:
        return {"ensembl_gene": m.group(1)}
    m = re.search(r"umls-concept/(C\d+)", uri)
    if m:
        return {"umls": m.group(1)}
    m = re.search(r"RXNORM/(\d+)", uri)
    if m:
        return {"rxcui": m.group(1)}
    m = re.search(r"(CHEMBL\d+)", uri)
    if m:
        return {"chembl": m.group(1)}
    m = (re.search(r"short_form=([A-Za-z]+)_([A-Za-z0-9]+)", uri)
         or re.search(r"(?:obo/|identifiers\.org/|/efo/)(?:[a-z]+[:/])?([A-Za-z]+)[_:]([A-Za-z0-9]+)$", uri))
    if m and m.group(1).upper() in _OBO_PREFIXES:
        prefix = m.group(1)
        key, sep = _OBO_KEYS.get(prefix.upper(), (prefix.lower(), ":"))
        if prefix.lower() in ("ordo", "orphanet"):
            return {"orphanet": m.group(2)}
        return {key: f"{prefix.upper()}{sep}{m.group(2)}"}
    m = re.search(r"identifiers\.org/([A-Za-z.]+)[:/](.+)$", uri)
    if m:
        prefix, acc = m.group(1).lower(), m.group(2)
        key = _IDORG_KEYS.get(prefix)
        if key == "nct":
            acc = acc.split(":")[-1]
        if key:
            return {key: acc}
        return {prefix.replace(".", "_"): acc}
    m = re.search(r"ncbi\.nlm\.nih\.gov/Taxonomy/.*?id=(\d+)", uri)
    if m:
        return {"taxonomy": m.group(1)}
    return {}


def _human_url(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith("http://identifiers.org/"):
        return "https://identifiers.org/" + uri[len("http://identifiers.org/"):]
    return uri


def _section_name(value: str | None) -> str | None:
    return value.split(" (")[0].strip() if value else None


# --------------------------------------------------------------------------- connector


class EuropePMC(Connector):
    name = "europepmc"
    title = "Europe PMC"
    category = "literature"
    description = (
        "Europe PMC: 45M+ life-science abstracts (all of PubMed) plus PMC full text, preprints (bioRxiv, medRxiv...), "
        "patents and Agricola, with citations, open-access full text and text-mined entities (genes, diseases, "
        "chemicals, GO terms, accession numbers). Best for preprints, OA full-text extraction and literature-to-database links."
    )
    homepage = "https://europepmc.org/"
    docs_url = "https://europepmc.org/RestfulWebService"
    rate_limit = 8.0
    health_query = "CRISPR"

    async def _search(self, query: str, page_size: int, cursor: str = "*", sort: str | None = None) -> tuple[dict[str, Any], str]:
        response = await self.request(
            "GET", f"{API}/search",
            params={"query": query, "format": "json", "resultType": "core", "pageSize": page_size,
                    "cursorMark": cursor or "*", "sort": sort},
        )
        return response.json(), response.url

    async def _resolve(self, identifier: str) -> tuple[str | None, str | None, dict[str, Any] | None, str | None]:
        """Resolve to (source, id, core_item, request_url). Uses a search except for PMIDs / SRC:ID."""
        kind, value = parse_identifier(identifier)
        if kind == "pmid":
            return "MED", value, None, None
        if kind == "src":
            src, ident = value.split(":", 1)
            return src, ident, None, None
        payload, url = await self._search(_identifier_query(kind, value), 5)
        items = (payload.get("resultList") or {}).get("result") or []
        if not items:
            return None, None, None, url
        items.sort(key=lambda i: {"MED": 0, "PMC": 1}.get(i.get("source"), 2))
        item = items[0]
        return item.get("source"), str(item.get("id")), item, url

    @tool(
        description="""
        Search Europe PMC (all of PubMed plus PMC full text, preprints, patents) with Europe PMC query
        syntax. Returns articles with PMID/PMCID/DOI, authors, journal, date, abstract, citation count,
        open-access/full-text flags, MeSH, grants (agency + grant id) and chemicals. Full-text search is
        the default for PMC articles. Prefer this over PubMed for preprints (source=PPR or SRC:PPR),
        open-access filtering, grant/funder queries and full-text matches. Syntax: fields TITLE:, ABSTRACT:,
        AUTH:"Doudna JA", JOURNAL:, PUB_YEAR:[2020 TO 2024], OPEN_ACCESS:y, SRC:PPR, GRANT_AGENCY:"NCI",
        GRANT_ID:, MESH:, ACCESSION_TYPE:geo, HAS_FT:y, Boolean AND/OR/NOT, e.g.
        'TITLE:"CRISPR" AND PUB_YEAR:[2020 TO 2024] AND OPEN_ACCESS:y'. sort: relevance | date | cited.
        """,
        federated=True,
    )
    async def search(self, params: SearchArticlesParams) -> ToolResult:
        query = params.query
        if params.open_access_only:
            query = f"({query}) AND OPEN_ACCESS:y"
        if params.source:
            query = f"({query}) AND SRC:{params.source}"
        sort = {"relevance": None, "date": "P_PDATE_D desc", "cited": "CITED desc"}[params.sort]
        payload, url = await self._search(query, params.limit, params.cursor or "*", sort)
        items = (payload.get("resultList") or {}).get("result") or []
        total = payload.get("hitCount")
        nxt = payload.get("nextCursorMark")
        more = bool(items) and nxt and nxt != (params.cursor or "*") and len(items) >= params.limit
        return ToolResult(
            source=self.name, tool="search", records=[_record(i) for i in items], total=total,
            next_cursor=nxt if more else None, request_url=url,
        )

    @tool(
        description="""
        Get one article's full Europe PMC metadata by PMID, PMCID, DOI or preprint id (PPR...):
        abstract, authors, journal, dates, citation count, open-access/full-text availability, MeSH,
        grants, chemicals, text-mined accession types and OA links. A DOI can return both a preprint and
        its published version.
        """
    )
    async def get_article(self, params: ArticleParams) -> ToolResult:
        kind, value = parse_identifier(params.identifier)
        payload, url = await self._search(_identifier_query(kind, value), 5)
        items = (payload.get("resultList") or {}).get("result") or []
        if not items:
            return ToolResult(source=self.name, tool="get_article", request_url=url,
                              notes=[f"{params.identifier} not found in Europe PMC"])
        notes = [f"{len(items)} Europe PMC records share this identifier (e.g. preprint and published version)"] if len(items) > 1 else []
        return ToolResult(source=self.name, tool="get_article", records=[_record(i) for i in items], total=len(items),
                          request_url=url, notes=notes)

    async def _citation_list(self, params: CitationParams, kind: str) -> ToolResult:
        tool_name = "get_citations" if kind == "citations" else "get_references"
        source, ident, _, resolve_url = await self._resolve(params.identifier)
        if not source:
            return ToolResult(source=self.name, tool=tool_name, request_url=resolve_url,
                              notes=[f"{params.identifier} not found in Europe PMC"])
        page = max(1, int(params.cursor)) if (params.cursor or "").isdigit() else 1
        response = await self.request(
            "GET", f"{API}/{quote(source)}/{quote(ident or '')}/{kind}",
            params={"format": "json", "page": page, "pageSize": params.limit}, allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(source=self.name, tool=tool_name, request_url=response.url,
                              notes=[f"no {kind} available for {source}:{ident}"])
        payload = response.json()
        list_key, item_key = ("citationList", "citation") if kind == "citations" else ("referenceList", "reference")
        items = (payload.get(list_key) or {}).get(item_key) or []
        total = payload.get("hitCount")
        notes = [] if items or page > 1 else [f"Europe PMC has no {kind} for {source}:{ident}"]
        return ToolResult(
            source=self.name, tool=tool_name, records=[_citation_record(i) for i in items], total=total,
            next_cursor=str(page + 1) if items and total and page * params.limit < total else None,
            request_url=response.url, notes=notes, data={"article": f"{source}:{ident}"},
        )

    @tool(
        description="""
        Articles that CITE a given article (Europe PMC citation network, mostly PubMed/PMC citing
        papers), newest first, with PMIDs, titles, journals, years and their own citation counts. Input:
        PMID, PMCID, DOI or SOURCE:ID. Paged (cursor = page number).
        """
    )
    async def get_citations(self, params: CitationParams) -> ToolResult:
        return await self._citation_list(params, "citations")

    @tool(
        description="""
        The reference list of an article (works it cites) as parsed by Europe PMC, in citation order,
        with PMID/DOI where the reference was matched. Input: PMID, PMCID, DOI or SOURCE:ID. Paged
        (cursor = page number). Use NCBI pubmed_links(relation=references) as an alternative.
        """
    )
    async def get_references(self, params: CitationParams) -> ToolResult:
        return await self._citation_list(params, "references")

    @tool(
        description="""
        Open-access full text of an article as plain text split by section (abstract, introduction,
        methods, results, discussion, conclusions, other = data availability/back matter), with figure and
        table captions, for detailed extraction (methods, cohort sizes, results, dataset accessions).
        Input: PMCID, PMID or DOI. Only the Europe PMC open-access / author-manuscript subset has full
        text; otherwise a note is returned. Filter with sections and bound size with max_chars (long
        sections are truncated, marked truncated=true, with their full length in chars).
        """
    )
    async def full_text(self, params: FullTextParams) -> ToolResult:
        kind, value = parse_identifier(params.identifier)
        item: dict[str, Any] | None = None
        notes: list[str] = []
        if kind == "pmcid":
            pmcid = value
        else:
            payload, url = await self._search(_identifier_query(kind, value), 5)
            items = (payload.get("resultList") or {}).get("result") or []
            item = next((i for i in items if i.get("pmcid")), items[0] if items else None)
            if item is None:
                return ToolResult(source=self.name, tool="full_text", request_url=url,
                                  notes=[f"{params.identifier} not found in Europe PMC"])
            pmcid = item.get("pmcid")
            if not pmcid:
                return ToolResult(source=self.name, tool="full_text", records=[_record(item)], request_url=url,
                                  notes=["no PMC full text for this article in Europe PMC (abstract only); "
                                         "try the publisher via the DOI or oa_url"])
        response = await self.request("GET", f"{API}/{quote(pmcid)}/fullTextXML", allow_status=(404, 500), retries=1,
                                      headers={"Accept": "application/xml"})
        if response.status != 200 or not response.content.strip():
            return ToolResult(source=self.name, tool="full_text", records=[_record(item)] if item else [],
                              request_url=response.url,
                              notes=[f"full text XML not available for {pmcid} (only the open-access / author-manuscript "
                                     "subset has full text in Europe PMC)"])
        try:
            parsed = parse_jats(response.content)
        except ET.ParseError:
            return ToolResult(source=self.name, tool="full_text", request_url=response.url,
                              notes=[f"full text XML for {pmcid} could not be parsed"])
        available = [s["section"] for s in parsed["sections"]]
        sections = [s for s in parsed["sections"] if not params.sections or s["section"] in params.sections]
        _fit_budget(sections, params.max_chars)
        if any(s["truncated"] for s in sections):
            notes.append("some sections were truncated to fit max_chars; request specific sections for more text")
        abstract = next((s["text"] for s in parsed["sections"] if s["section"] == "abstract"), None)
        record = _record(item) if item else Record(
            source="europepmc", id=parsed["ids"].get("pmid") or pmcid, type="article", title=parsed.get("title"),
            url=f"{SITE}/article/PMC/{pmcid}", identifiers={**parsed["ids"], "pmcid": pmcid},
            date=parsed.get("date"), authors=parsed.get("authors") or [], summary=abstract,
            attributes=_compact({"journal": parsed.get("journal"), "has_full_text": True}),
        )
        record.attributes["full_text_chars"] = sum(len(s["text"]) for s in parsed["sections"])
        return ToolResult(
            source=self.name, tool="full_text", records=[record], total=1, request_url=response.url, notes=notes,
            data={"pmcid": pmcid, "available_sections": list(dict.fromkeys(available)), "sections": sections},
        )

    @tool(
        description="""
        Text-mined entities in an article from the Europe PMC Annotations API, aggregated per entity with
        mention counts and sections: genes/proteins (UniProt/Ensembl IDs), diseases (UMLS), chemicals
        (ChEBI), clinical drugs (RxNorm), organisms (NCBI taxonomy), GO terms, cell types, tissues,
        experimental methods and ACCESSION NUMBERS cited in the text (GEO, NCT, PDB, UniProt, ENA/GenBank,
        RefSeq, dbSNP rsIDs, ArrayExpress, BioProject, EGA, PRIDE, DOIs...), each mapped to canonical
        identifiers. data.relationships lists text-mined gene-disease / drug-disease / gene-drug sentences.
        The best tool for linking a paper to the databases, datasets and trials it mentions. Input: PMID,
        PMCID or DOI.
        """
    )
    async def annotations(self, params: AnnotationParams) -> ToolResult:
        kind, value = parse_identifier(params.identifier)
        if kind == "pmid":
            article_id, ids = f"MED:{value}", {"pmid": value}
        elif kind == "pmcid":
            article_id, ids = f"PMC:{value}", {"pmcid": value}
        else:
            source, ident, item, url = await self._resolve(params.identifier)
            if not source:
                return ToolResult(source=self.name, tool="annotations", request_url=url,
                                  notes=[f"{params.identifier} not found in Europe PMC"])
            article_id = f"{source}:{ident}"
            ids = {"pmid": (item or {}).get("pmid"), "pmcid": (item or {}).get("pmcid"), "doi": (item or {}).get("doi")}
        response = await self.request(
            "GET", f"{ANNOTATIONS}/annotationsByArticleIds", params={"articleIds": article_id, "format": "JSON"},
            allow_status=(404,),
        )
        payload = response.json() if response.status == 200 else []
        articles = payload if isinstance(payload, list) else [payload]
        wanted = {t.lower() for t in params.types} if params.types else None
        entities: dict[tuple[str, str], dict[str, Any]] = {}
        relationships: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for article in articles:
            if not isinstance(article, dict):
                continue
            ids.setdefault("pmcid", article.get("pmcid"))
            for ann in article.get("annotations") or []:
                atype = ann.get("type") or "Other"
                if wanted and atype.lower() not in wanted:
                    continue
                counts[atype] = counts.get(atype, 0) + 1
                tags = ann.get("tags") or []
                if "Relationship" in atype or len(tags) > 1:
                    if len(relationships) < 30:
                        relationships.append(_compact({
                            "type": atype, "entities": [t.get("name") for t in tags if t.get("name")],
                            "identifiers": [tag_identifiers(t.get("uri"), t.get("name")) for t in tags],
                            "sentence": (ann.get("exact") or "")[:300], "section": _section_name(ann.get("section")),
                        }))
                    continue
                for tag in tags[:1]:
                    name = tag.get("name") or ann.get("exact")
                    idents = tag_identifiers(tag.get("uri"), name)
                    key = (atype, next(iter(idents.values()), None) or (name or "").lower())
                    entry = entities.setdefault(key, {
                        "type": atype, "name": name, "uri": tag.get("uri"), "identifiers": idents,
                        "mentions": 0, "sections": [], "exact": [], "providers": [], "subtype": ann.get("subType"),
                    })
                    entry["mentions"] += 1
                    section = _section_name(ann.get("section"))
                    if section and section not in entry["sections"]:
                        entry["sections"].append(section)
                    exact = ann.get("exact")
                    if exact and exact not in entry["exact"] and len(entry["exact"]) < 5:
                        entry["exact"].append(exact)
                    if ann.get("provider") and ann["provider"] not in entry["providers"]:
                        entry["providers"].append(ann["provider"])
        ordered = sorted(entities.values(), key=lambda e: (e["type"] != "Accession Numbers", e["type"], -e["mentions"]))
        records = [self._entity_record(e) for e in ordered[: params.limit]]
        notes = []
        if not counts:
            notes.append(f"no text-mined annotations for {article_id}" + (" of the requested types" if wanted else ""))
        if len(ordered) > params.limit:
            notes.append(f"{len(ordered)} distinct entities; returned the first {params.limit} (accession numbers first, then by type and mentions)")
        return ToolResult(
            source=self.name, tool="annotations", records=records, total=len(ordered), request_url=response.url,
            notes=notes,
            data={"article": _compact({"id": article_id, **ids}), "mentions_by_type": counts,
                  "relationships": relationships} if counts else None,
        )

    @staticmethod
    def _entity_record(entry: dict[str, Any]) -> Record:
        idents: dict[str, str] = entry["identifiers"]
        primary_key = next(iter(idents), None)
        if entry["type"] == "Accession Numbers":
            rtype = _ACCESSION_TYPES.get(primary_key or "", "other")
        elif entry["type"] == "Gene_Proteins":
            rtype = "gene" if primary_key == "ensembl_gene" else "protein"
        else:
            rtype = _TYPE_MAP.get(entry["type"], "other")
        return Record(
            source="europepmc",
            id=idents[primary_key] if primary_key else (entry["name"] or "?"),
            type=rtype,  # type: ignore[arg-type]
            title=entry["name"],
            url=_human_url(entry["uri"]),
            identifiers=idents,
            attributes=_compact({
                "annotation_type": entry["type"], "accession_type": entry.get("subtype"), "mentions": entry["mentions"],
                "sections": entry["sections"][:8], "matched_text": entry["exact"], "providers": entry["providers"],
            }),
        )
