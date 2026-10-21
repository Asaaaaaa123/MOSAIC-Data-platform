"""NCBI Entrez E-utilities connector: PubMed plus the other Entrez databases.

Docs: https://www.ncbi.nlm.nih.gov/books/NBK25501/ (E-utilities: esearch, esummary,
efetch, elink, einfo). Base URL https://eutils.ncbi.nlm.nih.gov/entrez/eutils/.

Auth: optional API key (settings ``ncbi_api_key`` / ``NCBI_API_KEY``) sent as the
``api_key`` query parameter (redacted by the http layer). Every request also
carries ``tool=mosaic`` and, when BIOPHA_CONTACT_EMAIL is set, ``email=`` (also
redacted), as NCBI asks.

Rate limits: 3 requests/second without a key, 10/second with one (we run a little
below both). Excess traffic gets HTTP 429, which the http layer retries.

Quirks:
- esearch only pages through the first 10,000 hits of PubMed (retstart <= 9999).
- esummary JSON layouts differ per database; each database has its own mapper
  below, with a generic fallback. Several databases embed XML strings inside the
  JSON (sra ``expxml``, biosample ``sampledata``, medgen ``conceptmeta``).
- Most databases want numeric UIDs; accessions (GSE..., PRJNA..., SAMN...,
  VCV..., rs..., PMC...) are resolved to UIDs here before esummary/elink.
- Not-found IDs come back as 200 with an ``error`` entry, not as 404.

Non-canonical identifier keys used here (see mosaic/ids.py): ``taxonomy`` (NCBI
Taxonomy ID), ``genbank`` (INSDC nucleotide/protein accession), ``dbvar``
(nsv/nssv/nstd accession), ``gtr`` (GTR test accession), ``cdd`` (CDD accession),
``assembly`` (GCF_/GCA_ accession), ``medgen`` (MedGen UID), ``ncbi_bookshelf``
(NBK accession).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Literal

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
WEB = "https://www.ncbi.nlm.nih.gov"

EntrezDb = Literal[
    "pubmed", "pmc", "gene", "protein", "nuccore", "gds", "sra", "bioproject", "biosample",
    "clinvar", "snp", "dbvar", "gap", "gtr", "medgen", "mesh", "cdd", "taxonomy", "assembly",
    "structure", "books",
]

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Databases where accessions can be resolved to UIDs with the [ACCN] field.
_ACCN_DBS = {"biosample", "sra", "clinvar", "nuccore", "protein", "assembly", "gtr", "dbvar", "cdd", "structure"}
# GEO accession prefix -> Entrez gds UID offset (documented GEO UID convention).
_GEO_OFFSETS = {"GDS": 0, "GPL": 100000000, "GSE": 200000000, "GSM": 300000000}


# --------------------------------------------------------------------------- params


class PubmedSearchParams(SearchParams):
    sort: Literal["relevance", "pub_date", "first_author", "journal"] = Field(
        "relevance", description="relevance (Best Match, default), pub_date (newest first), first_author, journal"
    )
    mindate: str | None = Field(
        None, description="Earliest date, YYYY or YYYY/MM or YYYY/MM/DD (requires or defaults maxdate to today)"
    )
    maxdate: str | None = Field(None, description="Latest date, YYYY or YYYY/MM or YYYY/MM/DD")
    datetype: Literal["pdat", "edat", "mdat"] = Field(
        "pdat", description="Date field for mindate/maxdate: pdat publication, edat Entrez (added), mdat modified"
    )


class PubmedFetchParams(ToolParams):
    pmids: list[str] = Field(
        min_length=1, max_length=200, description="PubMed IDs (digits, 'PMID:123' accepted), at most 200"
    )


class PubmedLinksParams(ToolParams):
    pmid: str = Field(description="PubMed ID of the source article (digits)")
    relation: Literal["similar", "cited_by", "references"] = Field(
        "similar",
        description="similar = PubMed 'similar articles' (ranked by similarity); cited_by = PubMed/PMC articles "
        "citing this one; references = articles this one cites (only when PubMed has the reference list)",
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum number of linked articles to return")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")


class EntrezSearchParams(ToolParams):
    db: EntrezDb = Field(description="Entrez database to search")
    query: str = Field(description="Entrez query (same syntax as the NCBI web search, with [field] tags)")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of records to return")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")
    sort: str | None = Field(
        None, description="Database-specific esearch sort, e.g. 'relevance', 'pub_date' (pubmed/pmc), 'name' (gene)"
    )


class EntrezSummaryParams(ToolParams):
    db: EntrezDb = Field(description="Entrez database the IDs belong to")
    ids: list[str] = Field(
        min_length=1,
        max_length=200,
        description="Entrez UIDs or accessions (e.g. gene 7157; gds GSE45209; snp rs334; pmc PMC6408961; "
        "clinvar 12375 or VCV000012375; bioproject PRJNA257197; nuccore NM_000546.6), at most 200",
    )


class EntrezLinkParams(ToolParams):
    dbfrom: EntrezDb = Field(description="Database of the input IDs, e.g. pubmed")
    db: EntrezDb = Field(description="Target database, e.g. gds")
    ids: list[str] = Field(
        min_length=1, max_length=100, description="Input UIDs or accessions in dbfrom (at most 100)"
    )
    linkname: str | None = Field(
        None,
        description="Specific Entrez link name, e.g. pubmed_gds, pubmed_pmc, gene_clinvar_specific, "
        "snp_clinvar, pubmed_pubmed_citedin. Default: <dbfrom>_<db>, else the first available link",
    )
    limit: int = Field(20, ge=1, le=100, description="Maximum number of linked records to summarize")
    cursor: str | None = Field(None, description="Pagination cursor (offset) into the linked IDs")


# --------------------------------------------------------------------------- helpers


def _iso_date(value: Any) -> str | None:
    """Turn NCBI dates ('2013/09/16 00:00', '2026 Oct 18', '2019 Mar-Apr', '2019') into ISO."""
    if not value:
        return None
    text = str(value).strip()
    m = re.match(r"^(\d{4})[/-](\d{1,2})(?:[/-](\d{1,2}))?", text)
    if m:
        year, month, day = m.group(1), int(m.group(2)), m.group(3)
        if year.startswith("0") or not 1 <= month <= 12:
            return year if not year.startswith("0") else None
        return f"{year}-{month:02d}" + (f"-{int(day):02d}" if day and int(day) else "")
    m = re.match(r"^(\d{4})(?:\s+([A-Za-z]{3})[a-z]*(?:\s+(\d{1,2}))?)?", text)
    if m:
        year, mon, day = m.group(1), m.group(2), m.group(3)
        month = _MONTHS.get((mon or "").lower())
        if not month:
            return year
        return f"{year}-{month:02d}" + (f"-{int(day):02d}" if day else "")
    return None


def _text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    return re.sub(r"\s+", " ", "".join(elem.itertext())).strip() or None


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, "", [], {}, "0")}


def _norm_pmid(value: str) -> str | None:
    m = re.search(r"\d+", value or "")
    return m.group(0) if m else None


def _strip_version(acc: str | None) -> str | None:
    return acc.split(".")[0] if acc else acc


def _safe_xml(fragment: str | None) -> ET.Element | None:
    if not fragment:
        return None
    try:
        return ET.fromstring(f"<root>{fragment}</root>")
    except ET.ParseError:
        return None


# --------------------------------------------------------------------------- PubMed XML


def _pubmed_date(article: ET.Element) -> str | None:
    pub = article.find("Journal/JournalIssue/PubDate")
    date = None
    if pub is not None:
        year = pub.findtext("Year")
        if year:
            month = pub.findtext("Month") or ""
            mnum = int(month) if month.isdigit() else _MONTHS.get(month[:3].lower())
            day = pub.findtext("Day")
            date = year + (f"-{mnum:02d}" if mnum else "") + (f"-{int(day):02d}" if mnum and day and day.isdigit() else "")
        else:
            date = _iso_date(pub.findtext("MedlineDate"))
    adate = article.find("ArticleDate")
    if adate is not None and adate.findtext("Year"):
        electronic = "-".join(
            [adate.findtext("Year")] + [f"{int(adate.findtext(p)):02d}" for p in ("Month", "Day") if (adate.findtext(p) or "").isdigit()]
        )
        if not date or (len(electronic) > len(date) and electronic[:4] == date[:4]):
            date = electronic
    return date


def _authors(author_list: ET.Element | None) -> list[str]:
    out = []
    for author in (author_list.findall("Author") if author_list is not None else []):
        if author.get("ValidYN") == "N":
            continue
        collective = author.findtext("CollectiveName")
        name = " ".join(x for x in (author.findtext("ForeName") or author.findtext("Initials"), author.findtext("LastName")) if x)
        if name or collective:
            out.append(name or collective)
    return out


def _abstract(abstract: ET.Element | None) -> str | None:
    if abstract is None:
        return None
    parts = []
    for node in abstract.findall("AbstractText"):
        text = _text(node)
        if not text:
            continue
        label = node.get("Label")
        parts.append(f"{label}: {text}" if label and label.upper() != "UNLABELLED" else text)
    return " ".join(parts) or None


def _parse_pubmed_article(node: ET.Element) -> Record | None:
    if node.tag == "PubmedBookArticle":
        return _parse_pubmed_book(node)
    citation = node.find("MedlineCitation")
    if citation is None:
        return None
    pmid = citation.findtext("PMID")
    article = citation.find("Article")
    if not pmid or article is None:
        return None
    ids: dict[str, str] = {"pmid": pmid}
    for aid in node.findall("PubmedData/ArticleIdList/ArticleId"):
        kind, value = aid.get("IdType"), (aid.text or "").strip()
        if kind == "doi" and value:
            ids.setdefault("doi", value)
        elif kind == "pmc" and value:
            ids.setdefault("pmcid", value)
    if "doi" not in ids:
        for loc in article.findall("ELocationID"):
            if loc.get("EIdType") == "doi" and loc.text:
                ids["doi"] = loc.text.strip()
    databanks: dict[str, list[str]] = {}
    for bank in article.findall("DataBankList/DataBank"):
        name = bank.findtext("DataBankName") or "?"
        accs = [a.text.strip() for a in bank.findall("AccessionNumberList/AccessionNumber") if a.text]
        databanks.setdefault(name, []).extend(accs)
    if databanks.get("ClinicalTrials.gov"):
        ids["nct"] = databanks["ClinicalTrials.gov"][0]
    if databanks.get("GEO"):
        ids["geo"] = databanks["GEO"][0]
    journal = article.find("Journal")
    mesh_terms, mesh_ids = [], []
    for heading in citation.findall("MeshHeadingList/MeshHeading"):
        desc = heading.find("DescriptorName")
        if desc is None or not desc.text:
            continue
        major = desc.get("MajorTopicYN") == "Y" or any(q.get("MajorTopicYN") == "Y" for q in heading.findall("QualifierName"))
        mesh_terms.append(desc.text + ("*" if major else ""))
        if desc.get("UI"):
            mesh_ids.append(desc.get("UI"))
    grants = [
        _compact({"id": g.findtext("GrantID"), "agency": g.findtext("Agency"), "country": g.findtext("Country")})
        for g in citation.findall("Article/GrantList/Grant")
    ]
    chemicals = [
        _compact({"name": c.findtext("NameOfSubstance"), "mesh": (c.find("NameOfSubstance").get("UI") if c.find("NameOfSubstance") is not None else None),
                  "registry": c.findtext("RegistryNumber")})
        for c in citation.findall("ChemicalList/Chemical")
    ]
    pub_types = [p.text for p in article.findall("PublicationTypeList/PublicationType") if p.text]
    is_preprint = "Preprint" in pub_types
    attributes = _compact(
        {
            "journal": journal.findtext("Title") if journal is not None else None,
            "journal_abbrev": journal.findtext("ISOAbbreviation") if journal is not None else None,
            "issn": journal.findtext("ISSN") if journal is not None else None,
            "volume": journal.findtext("JournalIssue/Volume") if journal is not None else None,
            "issue": journal.findtext("JournalIssue/Issue") if journal is not None else None,
            "pages": article.findtext("Pagination/MedlinePgn"),
            "publication_types": pub_types,
            "mesh_terms": mesh_terms[:30],
            "mesh_ids": mesh_ids[:30],
            "keywords": [k.text for k in citation.findall("KeywordList/Keyword") if k.text][:15],
            "chemicals": chemicals[:15],
            "grants": [g for g in grants if g][:15],
            "databanks": databanks,
            "language": article.findtext("Language"),
            "status": citation.get("Status"),
        }
    )
    return Record(
        source="ncbi",
        id=pmid,
        type="preprint" if is_preprint else "article",
        title=_text(article.find("ArticleTitle")) or _text(article.find("VernacularTitle")),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        identifiers=ids,
        date=_pubmed_date(article),
        authors=_authors(article.find("AuthorList")),
        summary=_abstract(article.find("Abstract")),
        attributes=attributes,
    )


def _parse_pubmed_book(node: ET.Element) -> Record | None:
    doc = node.find("BookDocument")
    if doc is None or not doc.findtext("PMID"):
        return None
    pmid = doc.findtext("PMID")
    ids = {"pmid": pmid}
    for aid in doc.findall("ArticleIdList/ArticleId"):
        if aid.get("IdType") == "bookaccession" and aid.text:
            ids["ncbi_bookshelf"] = aid.text.strip()
        if aid.get("IdType") == "doi" and aid.text:
            ids["doi"] = aid.text.strip()
    book_title = _text(doc.find("Book/BookTitle"))
    year = doc.findtext("Book/PubDate/Year") or doc.findtext("ContributionDate/Year")
    return Record(
        source="ncbi",
        id=pmid,
        type="book",
        title=_text(doc.find("ArticleTitle")) or book_title,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        identifiers=ids,
        date=year,
        authors=_authors(doc.find("AuthorList")),
        summary=_abstract(doc.find("Abstract")),
        attributes=_compact({"book": book_title, "publisher": doc.findtext("Book/Publisher/PublisherName")}),
    )


def parse_pubmed_xml(xml_text: str) -> list[Record]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable PubMed XML: {exc}") from exc
    records = []
    for node in root:
        if node.tag in ("PubmedArticle", "PubmedBookArticle"):
            record = _parse_pubmed_article(node)
            if record:
                records.append(record)
    return records


# --------------------------------------------------------------------------- esummary mappers


def _articleids(doc: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for aid in doc.get("articleids") or []:
        kind, value = aid.get("idtype"), aid.get("value")
        if not value:
            continue
        if kind in ("pubmed", "pmid") and value.isdigit():
            ids.setdefault("pmid", value)
        elif kind == "doi":
            ids.setdefault("doi", value)
        elif kind in ("pmc", "pmcid") and re.search(r"PMC\d+", value):
            ids.setdefault("pmcid", re.search(r"PMC\d+", value).group(0))
    return ids


def _map_pubmed(uid: str, doc: dict[str, Any]) -> Record:
    ids = {"pmid": uid, **_articleids(doc)}
    return Record(
        source="ncbi", id=uid, type="article", title=strip_html(doc.get("title")),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/", identifiers=ids,
        date=_iso_date(doc.get("sortpubdate") or doc.get("pubdate")),
        authors=[a.get("name") for a in doc.get("authors") or [] if a.get("name")],
        attributes=_compact({
            "journal": doc.get("fulljournalname") or doc.get("source"),
            "volume": doc.get("volume"), "issue": doc.get("issue"), "pages": doc.get("pages"),
            "publication_types": doc.get("pubtype"), "pmc_ref_count": doc.get("pmcrefcount"),
        }),
    )


def _map_pmc(uid: str, doc: dict[str, Any]) -> Record:
    pmcid = f"PMC{uid}"
    ids = {**_articleids(doc), "pmcid": pmcid}
    return Record(
        source="ncbi", id=pmcid, type="article", title=strip_html(doc.get("title")),
        url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", identifiers=ids,
        date=_iso_date(doc.get("sortdate") or doc.get("epubdate") or doc.get("pubdate")),
        authors=[a.get("name") for a in doc.get("authors") or [] if a.get("name")],
        attributes=_compact({
            "journal": doc.get("fulljournalname") or doc.get("source"), "volume": doc.get("volume"),
            "issue": doc.get("issue"), "pages": doc.get("pages"), "pmc_live_date": doc.get("pmclivedate"),
        }),
    )


def _map_gene(uid: str, doc: dict[str, Any]) -> Record:
    organism = doc.get("organism") or {}
    loc = (doc.get("genomicinfo") or [{}])[0] or {}
    symbol = doc.get("nomenclaturesymbol") or doc.get("name")
    return Record(
        source="ncbi", id=uid, type="gene",
        title=" - ".join(x for x in (doc.get("name"), doc.get("description")) if x) or uid,
        url=f"{WEB}/gene/{uid}",
        identifiers={"entrez_gene": uid, "gene_symbol": symbol, "omim": (doc.get("mim") or [None])[0]},
        summary=doc.get("summary") or None,
        attributes=_compact({
            "organism": organism.get("scientificname"), "taxid": organism.get("taxid"),
            "chromosome": doc.get("chromosome"), "map_location": doc.get("maplocation"),
            "aliases": doc.get("otheraliases"), "full_name": doc.get("nomenclaturename") or doc.get("description"),
            "location": _compact({"accession": loc.get("chraccver"), "start": loc.get("chrstart"), "stop": loc.get("chrstop"),
                                  "exons": loc.get("exoncount")}),
            "status": "discontinued" if str(doc.get("status")) == "2" else ("replaced" if str(doc.get("status")) == "1" else None),
            "replaced_by": doc.get("currentid") or None,
            "omim": doc.get("mim"),
        }),
    )


def _map_gds(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("accession") or uid
    pmids = [str(p) for p in doc.get("pubmedids") or []]
    sra = next((r.get("targetobject") for r in doc.get("extrelations") or [] if r.get("relationtype") == "SRA"), None)
    bioproject = next((r.get("targetobject") for r in doc.get("extrelations") or [] if r.get("relationtype") == "BioProject"), None)
    entry = doc.get("entrytype")
    return Record(
        source="ncbi", id=acc, type="sample" if entry == "GSM" else "dataset",
        title=doc.get("title"),
        url=f"{WEB}/geo/query/acc.cgi?acc={acc}" if not acc.startswith("GDS") else f"{WEB}/sites/GDSbrowser?acc={acc}",
        identifiers={"geo": acc, "pmid": pmids[0] if pmids else None, "sra": sra, "bioproject": bioproject},
        date=_iso_date(doc.get("pdat")), summary=doc.get("summary") or None,
        attributes=_compact({
            "entry_type": entry, "experiment_type": doc.get("gdstype"), "organism": doc.get("taxon"),
            "n_samples": doc.get("n_samples"), "platform": f"GPL{doc['gpl']}".split(";")[0] if doc.get("gpl") else None,
            "series": f"GSE{doc['gse']}".split(";")[0] if doc.get("gse") and entry != "GSE" else None,
            "pubmed_ids": pmids, "supplementary_files": doc.get("suppfile"),
            "sra_study": sra, "uid": uid,
        }),
    )


def _map_sra(uid: str, doc: dict[str, Any]) -> Record:
    exp = _safe_xml(doc.get("expxml"))
    runs = _safe_xml(doc.get("runs"))

    def find(path: str) -> ET.Element | None:
        return exp.find(path) if exp is not None else None

    def attr(path: str, name: str) -> str | None:
        node = find(path)
        return node.get(name) if node is not None else None

    exp_acc = attr("Experiment", "acc") or uid
    stats = find("Summary/Statistics")
    run_accs = [r.get("acc") for r in (runs.findall("Run") if runs is not None else []) if r.get("acc")]
    layout = find("Library_descriptor/LIBRARY_LAYOUT")
    return Record(
        source="ncbi", id=exp_acc, type="dataset",
        title=(find("Summary/Title").text if find("Summary/Title") is not None else None) or attr("Experiment", "name"),
        url=f"{WEB}/sra/{exp_acc}",
        identifiers={"sra": exp_acc, "bioproject": find("Bioproject").text if find("Bioproject") is not None else None,
                     "biosample": find("Biosample").text if find("Biosample") is not None else None,
                     "taxonomy": attr("Organism", "taxid")},
        date=_iso_date(doc.get("createdate")),
        attributes=_compact({
            "study": attr("Study", "acc"), "study_title": attr("Study", "name"), "sample": attr("Sample", "acc"),
            "organism": attr("Organism", "ScientificName"), "platform": find("Summary/Platform").text if find("Summary/Platform") is not None else None,
            "instrument": attr("Summary/Platform", "instrument_model"),
            "library_strategy": find("Library_descriptor/LIBRARY_STRATEGY").text if find("Library_descriptor/LIBRARY_STRATEGY") is not None else None,
            "library_source": find("Library_descriptor/LIBRARY_SOURCE").text if find("Library_descriptor/LIBRARY_SOURCE") is not None else None,
            "library_layout": layout[0].tag if layout is not None and len(layout) else None,
            "runs": run_accs[:20], "total_runs": stats.get("total_runs") if stats is not None else None,
            "total_spots": stats.get("total_spots") if stats is not None else None,
            "total_bases": stats.get("total_bases") if stats is not None else None,
            "center": attr("Submitter", "center_name"), "uid": uid,
        }),
    )


def _map_clinvar(uid: str, doc: dict[str, Any]) -> Record:
    vset = (doc.get("variation_set") or [{}])[0] or {}
    xrefs = vset.get("variation_xrefs") or []
    rs = next((x.get("db_id") for x in xrefs if x.get("db_source") == "dbSNP"), None)
    genes = doc.get("genes") or []
    germ = doc.get("germline_classification") or doc.get("clinical_significance") or {}
    traits = [
        _compact({"name": t.get("trait_name"), **{x.get("db_source", "").lower(): x.get("db_id") for x in t.get("trait_xrefs") or []}})
        for t in germ.get("trait_set") or []
    ]
    loc = next((v for v in vset.get("variation_loc") or [] if v.get("assembly_name") == "GRCh38"), None) or {}
    return Record(
        source="ncbi", id=uid, type="variant", title=doc.get("title"),
        url=f"{WEB}/clinvar/variation/{uid}/",
        identifiers={"clinvar": uid, "rsid": f"rs{rs}" if rs else None,
                     "gene_symbol": genes[0].get("symbol") if len(genes) == 1 else None,
                     "entrez_gene": genes[0].get("geneid") if len(genes) == 1 else None,
                     "hgvs": vset.get("variation_name") or None},
        date=_iso_date(germ.get("last_evaluated")),
        attributes=_compact({
            "accession": doc.get("accession_version") or doc.get("accession"),
            "classification": germ.get("description"), "review_status": germ.get("review_status"),
            "traits": traits[:10], "variant_type": doc.get("obj_type") or vset.get("variant_type"),
            "protein_change": doc.get("protein_change"), "molecular_consequences": doc.get("molecular_consequence_list"),
            "genes": list(dict.fromkeys(g.get("symbol") for g in genes if g.get("symbol")))[:20],
            "grch38_location": f"chr{loc.get('chr')}:{loc.get('start')}-{loc.get('stop')}" if loc.get("chr") else None,
            "spdi": vset.get("canonical_spdi"),
            "xrefs": {x.get("db_source"): x.get("db_id") for x in xrefs if x.get("db_source")},
        }),
    )


def _map_snp(uid: str, doc: dict[str, Any]) -> Record:
    rsid = f"rs{doc.get('snp_id') or uid}"
    genes = doc.get("genes") or []
    return Record(
        source="ncbi", id=rsid, type="variant",
        title=rsid + (f" ({', '.join(g.get('name') for g in genes if g.get('name'))})" if genes else ""),
        url=f"{WEB}/snp/{rsid}",
        identifiers={"rsid": rsid, "gene_symbol": genes[0].get("name") if len(genes) == 1 else None,
                     "entrez_gene": genes[0].get("gene_id") if len(genes) == 1 else None},
        date=_iso_date(doc.get("createdate")),
        attributes=_compact({
            "chromosome": doc.get("chr"), "position": doc.get("chrpos"), "spdi": (doc.get("spdi") or "").split(",")[:4],
            "variant_class": doc.get("snp_class"), "functional_class": doc.get("fxn_class"),
            "clinical_significance": doc.get("clinical_significance"),
            "genes": [g.get("name") for g in genes if g.get("name")],
            "global_mafs": [f"{m.get('study')}: {m.get('freq')}" for m in (doc.get("global_mafs") or [])[:5]],
            "updated": _iso_date(doc.get("updatedate")),
        }),
    )


def _map_gap(uid: str, doc: dict[str, Any]) -> Record:
    kind = doc.get("d_object_type") or "study"
    if kind == "study":
        study = doc.get("d_study_results") or {}
        acc = study.get("d_study_id") or uid
        return Record(
            source="ncbi", id=acc, type="study", title=study.get("d_study_name"),
            url=f"{WEB}/projects/gap/cgi-bin/study.cgi?study_id={acc}",
            identifiers={"dbgap": acc.split(".")[0]},
            date=_iso_date(study.get("d_study_release_date") or doc.get("d_study_release_date")),
            attributes=_compact({
                "version": acc, "study_type": (study.get("d_study_type") or "").strip() or study.get("d_study_type_list"),
                "design": study.get("d_study_design"),
                "diseases": list(dict.fromkeys(d.get("d_disease_name") for d in study.get("d_study_disease_list") or [] if isinstance(d, dict) and d.get("d_disease_name")))[:10],
                "molecular_data_types": [m.get("d_molecular_data_type_name") for m in study.get("d_study_molecular_data_type_list") or []][:15],
                "platforms": [p.get("d_genotype_platform") for p in study.get("d_study_genotype_platform_list") or []][:10],
                "participants": study.get("d_num_participants_in_subtree"), "variables": study.get("d_num_variables_in_subtree"),
                "has_sra": study.get("d_study_has_sra"), "status": study.get("d_study_status"),
                "projects": study.get("d_study_project_list"), "parent": [a.get("d_ancestor_id") for a in study.get("d_study_ancestor") or [] if isinstance(a, dict)],
            }),
        )
    section = doc.get(f"d_{kind}_results") or {}
    obj_id = section.get(f"d_{kind}_id") or uid
    parent = next((p.get("d_parent_id") for p in section.get(f"d_{kind}_parent") or [] if isinstance(p, dict)), None)
    study_acc = (parent or obj_id).split("&")[0]
    return Record(
        source="ncbi", id=obj_id, type="study" if kind == "study" else "other",
        title=section.get(f"d_{kind}_name") or obj_id, summary=section.get(f"d_{kind}_description") or None,
        url=f"{WEB}/gap/?term={study_acc}",
        identifiers={"dbgap": study_acc.split(".")[0] if study_acc.startswith("phs") else None},
        attributes=_compact({"object_type": kind, "study": study_acc,
                             "parent_name": next((p.get("d_parent_name") for p in section.get(f"d_{kind}_parent") or [] if isinstance(p, dict)), None)}),
    )


def _map_bioproject(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("project_acc") or uid
    return Record(
        source="ncbi", id=acc, type="study",
        title=doc.get("project_title") or doc.get("project_name"), url=f"{WEB}/bioproject/{acc}",
        identifiers={"bioproject": acc, "taxonomy": doc.get("taxid")},
        date=_iso_date(doc.get("registration_date")), summary=strip_html(doc.get("project_description")),
        attributes=_compact({
            "name": doc.get("project_name"), "project_type": doc.get("project_type"),
            "data_type": doc.get("project_data_type"), "target_scope": doc.get("project_target_scope"),
            "method": doc.get("project_methodtype"), "organism": doc.get("organism_name"),
            "relevance": [k.split("_", 1)[1] for k in ("relevance_medical", "relevance_agricultural", "relevance_environmental",
                                                       "relevance_industrial", "relevance_evolution", "relevance_model")
                          if str(doc.get(k) or "").lower() == "yes"],
            "uid": uid,
        }),
    )


def _map_biosample(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("accession") or uid
    sample = _safe_xml(doc.get("sampledata"))
    attrs: dict[str, str] = {}
    bioproject = None
    if sample is not None:
        for node in sample.iter("Attribute"):
            name = node.get("harmonized_name") or node.get("attribute_name")
            if name and node.text and len(attrs) < 20:
                attrs[name] = node.text.strip()[:200]
        for link in sample.iter("Link"):
            if (link.get("target") or "").lower() == "bioproject":
                label = link.get("label") or ""
                bioproject = label if label.startswith("PRJ") else (f"PRJNA{link.text.strip()}" if (link.text or "").strip().isdigit() else None)
                break
    return Record(
        source="ncbi", id=acc, type="sample", title=doc.get("title"), url=f"{WEB}/biosample/{acc}",
        identifiers={"biosample": acc, "bioproject": bioproject, "taxonomy": doc.get("taxonomy")},
        date=_iso_date(doc.get("publicationdate") or doc.get("date")),
        attributes=_compact({
            "organism": doc.get("organism"), "organization": doc.get("organization"), "package": doc.get("package"),
            "sample_ids": doc.get("identifiers"), "infraspecies": doc.get("infraspecies"), "attributes": attrs, "uid": uid,
        }),
    )


_MEDGEN_SAB = {"MSH": "mesh", "OMIM": "omim", "HPO": "hp", "MONDO": "mondo", "ORDO": "orphanet", "NCI": "nci_thesaurus",
               "SNOMEDCT_US": "snomedct"}


def _map_medgen(uid: str, doc: dict[str, Any]) -> Record:
    cui = doc.get("conceptid") or uid
    meta = _safe_xml(doc.get("conceptmeta"))
    ids: dict[str, Any] = {"umls": cui if re.match(r"^C[N\d]", cui) else None, "medgen": uid}
    genes = []
    xrefs: dict[str, list[str]] = {}
    if meta is not None:
        for name in meta.iter("Name"):
            key = _MEDGEN_SAB.get(name.get("SAB") or "")
            code = name.get("SDUI") or name.get("CODE")
            if key and code and code not in xrefs.setdefault(key, []):
                xrefs[key].append(code)
        for node in meta.iter("MIM"):
            mim = (node.text or "").strip()
            if mim.isdigit() and mim not in xrefs.setdefault("omim", []):
                xrefs["omim"].insert(0, mim)
        genes = [g.text for g in meta.iter("Gene") if g.text][:15]
    for key in ("mesh", "omim", "hp", "mondo", "orphanet"):
        values = xrefs.get(key) or []
        value = next((v for v in values if v), None)
        if key == "orphanet" and value:
            value = value.replace("Orphanet_", "")
        if key in ("mondo", "hp") and value:
            value = value.replace("_", ":")
        ids[key] = value
    semtype = (doc.get("semantictype") or {}).get("value") if isinstance(doc.get("semantictype"), dict) else doc.get("semantictype")
    definition = doc.get("definition")
    if isinstance(definition, dict):
        definition = definition.get("value")
    return Record(
        source="ncbi", id=cui, type="phenotype" if semtype in ("Finding", "Sign or Symptom") else "disease",
        title=doc.get("title"), url=f"{WEB}/medgen/{cui}", identifiers=ids, summary=strip_html(definition),
        attributes=_compact({"semantic_type": semtype, "associated_genes": genes, "uid": uid,
                             "xrefs": {k: v[:5] for k, v in xrefs.items() if k != "snomedct"}}),
    )


def _map_taxonomy(uid: str, doc: dict[str, Any]) -> Record:
    taxid = str(doc.get("taxid") or uid)
    name = doc.get("scientificname")
    return Record(
        source="ncbi", id=taxid, type="other",
        title=name + (f" ({doc['commonname']})" if doc.get("commonname") else "") if name else taxid,
        url=f"{WEB}/Taxonomy/Browser/wwwtax.cgi?id={taxid}", identifiers={"taxonomy": taxid},
        attributes=_compact({"rank": doc.get("rank"), "division": doc.get("division"), "common_name": doc.get("commonname"),
                             "genbank_division": doc.get("genbankdivision"), "status": doc.get("status")}),
    )


def _map_sequence(db: str) -> Callable[[str, dict[str, Any]], Record]:
    def mapper(uid: str, doc: dict[str, Any]) -> Record:
        acc = doc.get("accessionversion") or doc.get("caption") or uid
        refseq = doc.get("sourcedb") == "refseq" or bool(re.match(r"^[A-Z]{2}_", acc))
        return Record(
            source="ncbi", id=acc, type="protein" if db == "protein" else "other", title=doc.get("title"),
            url=f"{WEB}/{db}/{acc}",
            identifiers={"refseq" if refseq else "genbank": _strip_version(acc), "taxonomy": doc.get("taxid")},
            date=_iso_date(doc.get("createdate")),
            attributes=_compact({
                "accession_version": acc, "organism": doc.get("organism"), "length": doc.get("slen"),
                "molecule": doc.get("biomol") or doc.get("moltype"), "topology": doc.get("topology"),
                "source_db": doc.get("sourcedb"), "gi": doc.get("gi"), "updated": _iso_date(doc.get("updatedate")),
                "completeness": doc.get("completeness"),
            }),
        )

    return mapper


def _map_dbvar(uid: str, doc: dict[str, Any]) -> Record:
    is_study = str(doc.get("obj_type") or "").upper() == "STUDY"
    acc = (doc.get("st") if is_study else doc.get("sv")) or doc.get("st") or uid
    placements = [
        f"{p.get('assembly')} chr{p.get('chr')}:{p.get('chr_start')}-{p.get('chr_end')}"
        for p in doc.get("dbvarplacementlist") or [] if p.get("chr")
    ]
    vtypes = doc.get("dbvarvarianttypelist") or []
    genes = [g.get("name") for g in doc.get("dbvargenelist") or [] if isinstance(g, dict) and g.get("name")]
    return Record(
        source="ncbi", id=acc, type="study" if is_study else "variant",
        title=" ".join(x for x in (acc, ", ".join(vtypes)) if x) + (f" ({', '.join(genes[:5])})" if genes else ""),
        url=f"{WEB}/dbvar/{'studies' if is_study else 'variants'}/{acc}/",
        identifiers={"dbvar": acc},
        attributes=_compact({
            "study": doc.get("st"), "variant_types": vtypes, "clinical_significance": doc.get("dbvarclinicalsignificancelist"),
            "placements": placements[:4], "genes": genes[:20], "methods": doc.get("dbvarmethodlist"),
            "organism": doc.get("organism"), "variant_count": doc.get("variant_count") or doc.get("variant_call_count"),
            "uid": uid,
        }),
    )


def _map_gtr(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("accession") or uid
    analytes = doc.get("analytes") or []
    gene_analytes = [a for a in analytes if a.get("analytetype") == "Gene"]
    conditions = [_compact({"name": c.get("name"), "umls": c.get("cui")}) for c in doc.get("conditionlist") or []]
    return Record(
        source="ncbi", id=acc, type="assay",
        title=" - ".join(x for x in (doc.get("testname"), doc.get("offerer")) if x) or acc,
        url=f"{WEB}/gtr/tests/{doc.get('id') or uid}/",
        identifiers={"gtr": acc, "gene_symbol": gene_analytes[0].get("name") if len(gene_analytes) == 1 else None,
                     "entrez_gene": gene_analytes[0].get("geneid") if len(gene_analytes) == 1 else None},
        summary=doc.get("summary") or None,
        attributes=_compact({
            "test_type": doc.get("testtype"), "offerer": doc.get("offerer"),
            "country": (doc.get("offererlocation") or {}).get("country"), "conditions": conditions[:15],
            "genes": [a.get("name") for a in gene_analytes][:30],
            "methods": [c.get("name") for m in doc.get("method") or [] for c in m.get("categorylist") or []][:10],
        }),
    )


def _map_mesh(uid: str, doc: dict[str, Any]) -> Record:
    terms = doc.get("ds_meshterms") or []
    ui = doc.get("ds_meshui") or uid
    return Record(
        source="ncbi", id=ui, type="ontology_term", title=terms[0] if terms else ui,
        url=f"https://meshb.nlm.nih.gov/record/ui?ui={ui}", identifiers={"mesh": ui},
        summary=doc.get("ds_scopenote") or None,
        attributes=_compact({
            "entry_terms": terms[1:15], "tree_numbers": [x.get("treenum") for x in doc.get("ds_idxlinks") or [] if x.get("treenum")],
            "year_introduced": doc.get("ds_yearintroduced"), "record_type": doc.get("ds_recordtype"),
            "mapped_to": doc.get("ds_headingmappedto") or None, "registry_number": doc.get("ds_registrynumber"),
        }),
    )


def _map_cdd(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("accession") or uid
    return Record(
        source="ncbi", id=acc, type="other",
        title=" - ".join(x for x in (doc.get("title"), doc.get("subtitle")) if x),
        url=f"{WEB}/Structure/cdd/cddsrv.cgi?uid={acc}", identifiers={"cdd": acc},
        date=_iso_date(doc.get("pubdate")), summary=strip_html(doc.get("abstract")),
        attributes=_compact({"database": doc.get("database"), "organism": doc.get("organism"), "uid": uid}),
    )


def _map_assembly(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("assemblyaccession") or uid
    bps = [b.get("bioprojectaccn") for b in (doc.get("rs_bioprojects") or []) + (doc.get("gb_bioprojects") or []) if b.get("bioprojectaccn")]
    return Record(
        source="ncbi", id=acc, type="dataset",
        title=" ".join(x for x in (doc.get("assemblyname"), f"({doc['organism']})" if doc.get("organism") else None) if x),
        url=f"{WEB}/datasets/genome/{acc}/",
        identifiers={"assembly": acc, "taxonomy": doc.get("taxid"), "bioproject": bps[0] if bps else None,
                     "biosample": doc.get("biosampleaccn")},
        date=_iso_date(doc.get("seqreleasedate") or doc.get("asmreleasedate_refseq") or doc.get("asmreleasedate_genbank")),
        attributes=_compact({
            "assembly_name": doc.get("assemblyname"), "organism": doc.get("organism"), "status": doc.get("assemblystatus"),
            "assembly_type": doc.get("assemblytype"), "ucsc_name": doc.get("ucscname"), "refseq_category": doc.get("refseq_category"),
            "coverage": doc.get("coverage"), "submitter": doc.get("submitterorganization"), "bioprojects": bps[:5],
            "contig_n50": doc.get("contign50"), "scaffold_n50": doc.get("scaffoldn50"),
        }),
    )


def _map_structure(uid: str, doc: dict[str, Any]) -> Record:
    pdb = doc.get("pdbacc") or uid
    return Record(
        source="ncbi", id=pdb, type="structure", title=doc.get("pdbdescr"),
        url=f"{WEB}/Structure/pdb/{pdb}", identifiers={"pdb": pdb},
        date=_iso_date(doc.get("pdbdepositdate")),
        attributes=_compact({
            "method": doc.get("expmethod"), "resolution": doc.get("resolution"), "class": doc.get("pdbclass"),
            "organisms": doc.get("organismlist"), "ligands": (doc.get("ligcode") or "").split("|") if doc.get("ligcode") else None,
            "protein_chains": doc.get("proteinmoleculecount"), "mmdb_id": uid,
        }),
    )


def _map_books(uid: str, doc: dict[str, Any]) -> Record:
    acc = doc.get("accessionid") or doc.get("chapteraccessionid") or uid
    info = _safe_xml(doc.get("bookinfo"))
    book = None
    if info is not None:
        book = next((t.text for t in info.iter("Title") if t.text), None)
    return Record(
        source="ncbi", id=acc, type="book", title=doc.get("title"), url=f"{WEB}/books/{acc}/",
        identifiers={"ncbi_bookshelf": acc}, date=_iso_date(doc.get("pubdate")),
        attributes=_compact({"book": book, "book_accession": doc.get("bookaccessionid"), "section_type": doc.get("rtype")}),
    )


def _map_generic(db: str) -> Callable[[str, dict[str, Any]], Record]:
    def mapper(uid: str, doc: dict[str, Any]) -> Record:
        title = doc.get("title") or doc.get("name") or doc.get("caption") or doc.get("description")
        scalars = {k: v for k, v in doc.items() if isinstance(v, (str, int, float)) and k != "uid" and len(str(v)) < 200}
        return Record(
            source="ncbi", id=uid, type="other", title=strip_html(str(title)) if title else f"{db} {uid}",
            url=f"{WEB}/{db}/{uid}", attributes=_compact(dict(list(scalars.items())[:15])),
        )

    return mapper


MAPPERS: dict[str, Callable[[str, dict[str, Any]], Record]] = {
    "pubmed": _map_pubmed, "pmc": _map_pmc, "gene": _map_gene, "gds": _map_gds, "sra": _map_sra,
    "clinvar": _map_clinvar, "snp": _map_snp, "gap": _map_gap, "bioproject": _map_bioproject,
    "biosample": _map_biosample, "medgen": _map_medgen, "taxonomy": _map_taxonomy,
    "nuccore": _map_sequence("nuccore"), "protein": _map_sequence("protein"), "dbvar": _map_dbvar,
    "gtr": _map_gtr, "mesh": _map_mesh, "cdd": _map_cdd, "assembly": _map_assembly,
    "structure": _map_structure, "books": _map_books,
}


def map_summary(db: str, uid: str, doc: dict[str, Any]) -> Record:
    mapper = MAPPERS.get(db) or _map_generic(db)
    try:
        record = mapper(uid, doc)
    except Exception:  # never let one odd document break a page
        record = _map_generic(db)(uid, doc)
    record.attributes.setdefault("entrez_db", db)
    return record


# --------------------------------------------------------------------------- connector


class NCBI(Connector):
    name = "ncbi"
    title = "NCBI Entrez (PubMed, Gene, GEO, ClinVar, dbSNP, SRA, dbGaP ...)"
    category = "literature"
    description = (
        "NCBI E-utilities over PubMed (37M+ citations with abstracts, MeSH, grants) and the other Entrez "
        "databases: Gene, GEO, SRA, ClinVar, dbSNP, dbVar, dbGaP, BioProject, BioSample, MedGen, GTR, RefSeq/"
        "GenBank, Taxonomy, Assembly, CDD, Structure, MeSH, Bookshelf. Best for authoritative PubMed searches and "
        "for hopping between literature and NCBI data records via Entrez links."
    )
    homepage = "https://www.ncbi.nlm.nih.gov/"
    docs_url = "https://www.ncbi.nlm.nih.gov/books/NBK25501/"
    auth = "optional_api_key"
    rate_limit = 2.8
    health_query = "CRISPR"
    nih_repositories = (
        "ClinVar",
        "Conserved Domains Database (CDD)",
        "Database for Genotypes and Phenotypes (dbGaP)",
        "Database of human genomic Structural Variation (dbVar)",
        "GenBank",
        "Gene Expression Omnibus (GEO)",
        "NCBI Datasets: Gene, Genome, Assembly, Orthologs, and Taxonomy Database",
        "NIH Genetic Testing Registry (GTR)",
        "Reference Sequences (RefSeq)",
        "Sequence Read Archive (SRA)",
        "Single Nucleotide Polymorphism Database (dbSNP)",
    )

    def _key(self) -> str | None:
        # Read the declared Settings field (not Connector.setting, which also falls back to .env).
        return getattr(self.settings, "ncbi_api_key", None) or None

    def effective_rate(self) -> float:
        return 9.0 if self._key() else self.rate_limit

    # ---- low-level E-utilities calls ----
    def _params(self, **params: Any) -> dict[str, Any]:
        out = {k: v for k, v in params.items() if v is not None}
        out["tool"] = "mosaic"
        email = getattr(self.settings, "biopha_contact_email", None)
        if email:
            out["email"] = email
        key = self._key()
        if key:
            out["api_key"] = key
        return out

    async def _eutil_json(self, util: str, **params: Any) -> tuple[dict[str, Any], str]:
        response = await self.request("GET", f"{EUTILS}/{util}.fcgi", params=self._params(retmode="json", **params))
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, f"{util}: unexpected non-JSON response", url=response.url) from exc
        if isinstance(payload, dict) and payload.get("error") and util != "esummary":
            raise SourceError(self.name, f"{util} error: {str(payload['error'])[:200]}", url=response.url)
        return payload, response.url

    async def _esearch(self, db: str, term: str, retstart: int, retmax: int, **extra: Any) -> tuple[dict[str, Any], str]:
        payload, url = await self._eutil_json("esearch", db=db, term=term, retstart=retstart, retmax=retmax, **extra)
        result = payload.get("esearchresult") or {}
        if result.get("ERROR"):
            raise SourceError(self.name, f"esearch error: {str(result['ERROR'])[:200]}", url=url)
        return result, url

    async def _esummary(self, db: str, uids: list[str]) -> tuple[list[Record], list[str], str | None]:
        """Summaries in input order; returns (records, missing_uids, request_url)."""
        if not uids:
            return [], [], None
        payload, url = await self._eutil_json("esummary", db=db, id=",".join(uids))
        result = payload.get("result") or {}
        records, missing = [], []
        for uid in uids:
            doc = result.get(uid)
            if not isinstance(doc, dict) or doc.get("error"):
                missing.append(uid)
                continue
            records.append(map_summary(db, uid, doc))
        return records, missing, url

    async def _efetch_pubmed(self, pmids: list[str]) -> tuple[list[Record], str]:
        response = await self.request(
            "GET", f"{EUTILS}/efetch.fcgi", params=self._params(db="pubmed", id=",".join(pmids), retmode="xml")
        )
        try:
            records = parse_pubmed_xml(response.text)
        except ValueError as exc:
            raise SourceError(self.name, str(exc), url=response.url) from exc
        order = {p: i for i, p in enumerate(pmids)}
        records.sort(key=lambda r: order.get(r.id, len(order)))
        return records, response.url

    async def _resolve_uids(self, db: str, ids: list[str]) -> tuple[list[str], list[str]]:
        """Map accessions to Entrez UIDs. Returns (uids, notes)."""
        uids: list[str] = []
        pending: list[str] = []
        notes: list[str] = []
        for raw in ids:
            value = raw.strip()
            if not value:
                continue
            if value.isdigit():
                uids.append(value)
            elif db == "snp" and re.fullmatch(r"(?i)rs\d+", value):
                uids.append(value[2:])
            elif db == "pmc" and re.fullmatch(r"(?i)pmc\d+", value):
                uids.append(value[3:])
            elif db == "pubmed" and _norm_pmid(value):
                uids.append(_norm_pmid(value))  # type: ignore[arg-type]
            elif db == "clinvar" and re.fullmatch(r"(?i)VCV0*\d+(\.\d+)?", value):
                uids.append(str(int(re.search(r"\d+", value).group(0))))  # type: ignore[union-attr]
            elif db == "gds" and re.fullmatch(r"(?i)(GDS|GPL|GSE|GSM)\d+", value):
                uids.append(str(_GEO_OFFSETS[value[:3].upper()] + int(value[3:])))
            elif db == "bioproject" and re.fullmatch(r"(?i)PRJNA\d+", value):
                uids.append(str(int(value[5:])))
            elif db == "mesh" and re.fullmatch(r"[DC]\d{6,9}", value):
                uids.append(("68" if value[0] == "D" else "67") + value[1:])
            else:
                pending.append(value)
        if pending:
            if db in _ACCN_DBS:
                term = " OR ".join(f"{v}[ACCN]" for v in pending)
            elif db == "gene":
                term = " OR ".join(f"{v}[sym] AND human[orgn]" if re.fullmatch(r"[A-Za-z0-9-]+", v) else v for v in pending)
                notes.append("gene symbols were resolved against human genes; pass Entrez Gene IDs for other organisms")
            elif db == "medgen":
                term = " OR ".join(f"{v}[CUI]" for v in pending)
            elif db == "gap":
                term = " OR ".join(f"{v.split('.')[0]}[Study ID]" if v.lower().startswith("phs") else v for v in pending)
            else:
                term = " OR ".join(pending)
            result, _ = await self._esearch(db, term, 0, max(len(pending) * 5, 20))
            found = result.get("idlist") or []
            if not found and db in _ACCN_DBS:  # some databases do not index accessions as [ACCN]
                result, _ = await self._esearch(db, " OR ".join(pending), 0, max(len(pending) * 5, 20))
                found = result.get("idlist") or []
            if not found:
                notes.append(f"no {db} UIDs found for: {', '.join(pending)}")
            uids.extend(found)
        seen: set[str] = set()
        return [u for u in uids if not (u in seen or seen.add(u))], notes

    # ---- tools ----
    @tool(
        description="""
        Search PubMed (37M+ biomedical citations) with full PubMed query syntax and get complete records
        WITH abstracts: PMID, DOI, PMCID, title, journal, date, authors, MeSH terms (* = major topic),
        publication types, grants, chemicals and linked NCT/GEO accessions. The authoritative source for
        biomedical literature searches; use Europe PMC for preprints/full text and OpenAlex/Semantic Scholar
        for citation graphs. Syntax: field tags [tiab] [ti] [au] [mh] (MeSH) [majr] [pt] [dp] [la] [affil],
        Boolean AND/OR/NOT, quotes for phrases, e.g. '"glioblastoma"[mh] AND pembrolizumab[tiab] AND
        clinical trial[pt]'. Date filtering: mindate/maxdate (YYYY[/MM[/DD]]) with datetype, or
        '2020:2024[dp]' in the query. sort: relevance | pub_date | first_author | journal.
        Only the first 10,000 hits can be paged.
        """,
        federated=True,
    )
    async def pubmed_search(self, params: PubmedSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        notes: list[str] = []
        if offset > 9999:
            return ToolResult(source=self.name, tool="pubmed_search",
                              notes=["PubMed esearch cannot page beyond the first 10,000 hits; narrow the query"])
        sort = {"relevance": None, "pub_date": "pub_date", "first_author": "Author", "journal": "JournalName"}[params.sort]
        dates: dict[str, Any] = {}
        if params.mindate or params.maxdate:
            dates = {"mindate": params.mindate or "1800", "maxdate": params.maxdate or "3000", "datetype": params.datetype}
        result, url = await self._esearch("pubmed", params.query, offset, params.limit, sort=sort, **dates)
        pmids = result.get("idlist") or []
        total = int(result.get("count") or 0)
        notes.extend(self._search_notes(result))
        records: list[Record] = []
        if pmids:
            records, _ = await self._efetch_pubmed(pmids)
        nxt = offset + len(pmids)
        return ToolResult(
            source=self.name, tool="pubmed_search", records=records, total=total,
            next_cursor=str(nxt) if pmids and nxt < min(total, 10000) else None,
            request_url=url, notes=notes,
            data={"query_translation": result.get("querytranslation")} if result.get("querytranslation") else None,
        )

    @staticmethod
    def _search_notes(result: dict[str, Any]) -> list[str]:
        notes = []
        errors = result.get("errorlist") or {}
        warnings = result.get("warninglist") or {}
        if errors.get("phrasesnotfound"):
            notes.append(f"phrases not found: {', '.join(errors['phrasesnotfound'][:10])}")
        if errors.get("fieldsnotfound"):
            notes.append(f"fields not found: {', '.join(errors['fieldsnotfound'][:10])}")
        if warnings.get("quotedphrasesnotfound"):
            notes.append(f"quoted phrases not found: {', '.join(warnings['quotedphrasesnotfound'][:10])}")
        if warnings.get("phrasesignored"):
            notes.append(f"phrases ignored: {', '.join(warnings['phrasesignored'][:10])}")
        for message in (warnings.get("outputmessages") or [])[:3]:
            notes.append(str(message))
        return notes

    @tool(
        description="""
        Fetch complete PubMed records for up to 200 PMIDs: title, abstract, authors, journal, date, DOI,
        PMCID, MeSH terms and IDs, publication types, grants, chemicals and DataBank accessions (NCT, GEO).
        Use after another tool gave you PMIDs (e.g. entrez_link, Europe PMC, ClinVar) or to read abstracts.
        """
    )
    async def pubmed_fetch(self, params: PubmedFetchParams) -> ToolResult:
        pmids = list(dict.fromkeys(p for p in (_norm_pmid(x) for x in params.pmids) if p))
        if not pmids:
            return ToolResult(source=self.name, tool="pubmed_fetch", notes=["no valid PMIDs given"])
        records, url = await self._efetch_pubmed(pmids)
        found = {r.id for r in records}
        missing = [p for p in pmids if p not in found]
        notes = [f"PMIDs not found in PubMed: {', '.join(missing)}"] if missing else []
        return ToolResult(source=self.name, tool="pubmed_fetch", records=records, total=len(records), request_url=url, notes=notes)

    @tool(
        description="""
        Related literature for one PubMed article via Entrez links: relation='similar' (PubMed similar
        articles, ranked by text similarity, with score), 'cited_by' (PubMed Central articles citing it) or
        'references' (articles it cites, when PubMed holds its reference list). Returns full PubMed records
        with abstracts, paged with cursor. For broader citation counts use OpenAlex / Europe PMC.
        """
    )
    async def pubmed_links(self, params: PubmedLinksParams) -> ToolResult:
        pmid = _norm_pmid(params.pmid)
        if not pmid:
            return ToolResult(source=self.name, tool="pubmed_links", notes=["invalid PMID"])
        linkname = {"similar": "pubmed_pubmed", "cited_by": "pubmed_pubmed_citedin", "references": "pubmed_pubmed_refs"}[params.relation]
        payload, url = await self._eutil_json(
            "elink", dbfrom="pubmed", db="pubmed", id=pmid, linkname=linkname,
            cmd="neighbor_score" if params.relation == "similar" else "neighbor",
        )
        links: list[tuple[str, Any]] = []
        for linkset in payload.get("linksets") or []:
            for lsdb in linkset.get("linksetdbs") or []:
                if lsdb.get("linkname") == linkname:
                    links.extend(_link_items(lsdb.get("links")))
        links = [(i, s) for i, s in links if i != pmid]
        offset = parse_offset(params.cursor)
        page = links[offset: offset + params.limit]
        records: list[Record] = []
        if page:
            records, _ = await self._efetch_pubmed([i for i, _ in page])
            scores = dict(page)
            for record in records:
                if scores.get(record.id) is not None:
                    record.attributes["similarity_score"] = scores[record.id]
        notes = [] if links else [f"no '{params.relation}' links found for PMID {pmid}"]
        nxt = offset + len(page)
        return ToolResult(
            source=self.name, tool="pubmed_links", records=records, total=len(links),
            next_cursor=str(nxt) if page and nxt < len(links) else None, request_url=url, notes=notes,
        )

    @tool(
        description="""
        Search any major Entrez database and get normalized summary records with cross-reference
        identifiers. db choices: pubmed, pmc (full-text articles), gene, protein, nuccore (RefSeq/GenBank),
        gds (GEO DataSets/Series), sra, bioproject, biosample, clinvar, snp (dbSNP), dbvar, gap (dbGaP),
        gtr (genetic tests), medgen (diseases/phenotypes with UMLS/OMIM/HPO/MONDO), mesh, cdd, taxonomy,
        assembly, structure, books. Query uses Entrez syntax with db-specific fields, e.g. gene:
        'BRCA1[sym] AND human[orgn]'; gds: 'glioblastoma AND gse[etyp] AND "Homo sapiens"[orgn]';
        clinvar: 'TP53[gene] AND pathogenic[clinsig]'; snp: 'APOE[gene]'; gap studies:
        'diabetes AND 1[s_discriminator]'; sra: 'BRCA1 AND rna seq[strategy]'. For PubMed articles with
        abstracts prefer pubmed_search.
        """
    )
    async def entrez_search(self, params: EntrezSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        result, url = await self._esearch(params.db, params.query, offset, params.limit, sort=params.sort)
        uids = result.get("idlist") or []
        total = int(result.get("count") or 0)
        notes = self._search_notes(result)
        records, missing, _ = await self._esummary(params.db, uids)
        if missing:
            notes.append(f"{len(missing)} UID(s) returned no summary")
        nxt = offset + len(uids)
        return ToolResult(
            source=self.name, tool="entrez_search", records=records, total=total,
            next_cursor=str(nxt) if uids and nxt < total else None, request_url=url, notes=notes,
            data={"query_translation": result.get("querytranslation")} if result.get("querytranslation") else None,
        )

    @tool(
        description="""
        Normalized summaries for specific records in an Entrez database (up to 200). Accepts numeric UIDs
        or accessions, which are resolved automatically: gds 'GSE45209', snp 'rs334', pmc 'PMC6408961',
        clinvar 'VCV000012375' or variation ID '12375', bioproject 'PRJNA257197', biosample 'SAMN02604305',
        nuccore 'NM_000546.6', protein 'NP_000537.3', assembly 'GCF_000001405.40', medgen CUI 'C0010674',
        mesh 'D001249', gene ID '7157' (or a human gene symbol). Use for get-by-ID across NCBI databases.
        """
    )
    async def entrez_summary(self, params: EntrezSummaryParams) -> ToolResult:
        uids, notes = await self._resolve_uids(params.db, params.ids)
        if not uids:
            return ToolResult(source=self.name, tool="entrez_summary", notes=notes or ["no IDs to look up"])
        records, missing, url = await self._esummary(params.db, uids)
        if missing:
            notes.append(f"not found in {params.db}: {', '.join(missing[:20])}")
        return ToolResult(source=self.name, tool="entrez_summary", records=records, total=len(records), request_url=url, notes=notes)

    @tool(
        description="""
        Cross-database links between Entrez databases (the key tool for hopping from literature to data
        and between NCBI data types). Give dbfrom, db and input IDs/accessions; returns the linked records
        in db (normalized summaries) plus data.by_input (linked IDs per input) and data.linknames (link
        types available with counts). Examples: pubmed->gds (GEO datasets from a paper), gds->pubmed,
        pubmed->pmc (free full text), pubmed->clinvar, gene->clinvar (linkname gene_clinvar_specific for
        variants in that gene only), gene->snp, gene->pubmed, snp->clinvar, clinvar->pubmed, gds->sra,
        bioproject->sra, bioproject->biosample, sra->biosample, medgen->gene, medgen->gtr, gene->protein,
        nuccore->gene, taxonomy->assembly, pubmed->pubmed with linkname pubmed_pubmed_citedin.
        """
    )
    async def entrez_link(self, params: EntrezLinkParams) -> ToolResult:
        uids, notes = await self._resolve_uids(params.dbfrom, params.ids)
        if not uids:
            return ToolResult(source=self.name, tool="entrez_link", notes=notes or ["no input IDs"])
        query: list[tuple[str, Any]] = [(k, v) for k, v in self._params(dbfrom=params.dbfrom, db=params.db, retmode="json", cmd="neighbor").items()]
        if params.linkname:
            query.append(("linkname", params.linkname))
        query.extend(("id", u) for u in uids)  # repeated id= keeps one linkset per input
        response = await self.request("GET", f"{EUTILS}/elink.fcgi", params=query)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(self.name, "elink: unexpected non-JSON response", url=response.url) from exc
        if isinstance(payload, dict) and payload.get("ERROR"):
            raise SourceError(self.name, f"elink error: {str(payload['ERROR'])[:200]}", url=response.url)
        counts: dict[str, int] = {}
        per_link: dict[str, dict[str, list[str]]] = {}
        for linkset in payload.get("linksets") or []:
            source_ids = [str(i) for i in linkset.get("ids") or []]
            for lsdb in linkset.get("linksetdbs") or []:
                if lsdb.get("dbto") != params.db:
                    continue
                name = lsdb.get("linkname") or f"{params.dbfrom}_{params.db}"
                ids = [i for i, _ in _link_items(lsdb.get("links"))]
                counts[name] = counts.get(name, 0) + len(ids)
                for sid in source_ids:
                    per_link.setdefault(name, {}).setdefault(sid, []).extend(ids)
        chosen = params.linkname or (f"{params.dbfrom}_{params.db}" if f"{params.dbfrom}_{params.db}" in per_link else next(iter(per_link), None))
        by_input = per_link.get(chosen or "", {})
        linked = list(dict.fromkeys(i for ids in by_input.values() for i in ids))
        if params.dbfrom == params.db:
            linked = [i for i in linked if i not in set(uids)]
        offset = parse_offset(params.cursor)
        page = linked[offset: offset + params.limit]
        records, missing, _ = await self._esummary(params.db, page)
        if not linked:
            notes.append(f"no {params.dbfrom}->{params.db} links found" + (f" via {params.linkname}" if params.linkname else ""))
        if counts and len(counts) > 1:
            notes.append(f"used linkname {chosen}; other link types are listed in data.linknames")
        nxt = offset + len(page)
        budget, compact_by_input = 500, {}
        for sid, ids in by_input.items():
            compact_by_input[sid] = ids[: max(0, min(50, budget))]
            budget -= len(compact_by_input[sid])
        if sum(len(v) for v in by_input.values()) > sum(len(v) for v in compact_by_input.values()):
            notes.append("data.by_input is truncated (50 IDs per input, 500 overall); page through records with cursor")
        return ToolResult(
            source=self.name, tool="entrez_link", records=records, total=len(linked),
            next_cursor=str(nxt) if page and nxt < len(linked) else None, request_url=response.url, notes=notes,
            data={
                "linkname": chosen, "linknames": counts,
                "by_input": compact_by_input, "input_uids": uids,
            },
        )


def _link_items(links: Any) -> list[tuple[str, Any]]:
    out = []
    for item in links or []:
        if isinstance(item, dict):
            if item.get("id") is not None:
                out.append((str(item["id"]), item.get("score")))
        elif item is not None:
            out.append((str(item), None))
    return out
