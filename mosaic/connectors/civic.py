"""CIViC (Clinical Interpretation of Variants in Cancer) connector.

Docs: https://griffithlab.github.io/civic-v2/ ; GraphQL endpoint https://civicdb.org/api/graphql
(introspectable; interactive explorer at https://civicdb.org/api/graphiql).
Auth: none. No published rate limit; we use 2 requests/second.

Quirks:
* ``evidenceItems(molecularProfileName:)`` / ``assertions(molecularProfileName:)`` are *prefix*
  matches on the molecular-profile name ("AR" also matches "ARAF S214F", "AREG Expression"),
  so gene queries are post-filtered to profiles whose name contains the gene symbol as a whole
  token. A page can therefore hold fewer than ``limit`` items; keep paging with ``next_cursor``.
* Pagination is Relay-style: ``first`` + ``after`` (``pageInfo.endCursor``) and ``totalCount``.
* ``search(query)`` is the site's quick search (max ~10 hits per entity type, no paging).
* Unknown ids return ``null`` rather than an error.

Identifiers: ``civic`` uses CIViC's prefixed ids (EID = evidence item, AID = assertion,
MPID = molecular profile, VID = variant, GID = gene/feature, TID = therapy, DID = disease);
also ``gene_symbol``, ``entrez_gene`` (gene records), ``doid`` (DOID:####, disease records),
``rsid``, ``clinvar``, ``hgvs`` (single-variant molecular profiles) and the custom key ``ncit`` (NCI
Thesaurus code of a therapy). Evidence items and assertions carry only their EID/AID: their gene,
disease and source paper are related entities kept in attributes (``genes``, ``entrez_genes``,
``disease_doid``, ``pmid``, ``pmcid``); a molecular profile's gene is likewise ``attributes['genes']``.
Otherwise every BRAF evidence item would merge with the BRAF gene and every melanoma item with melanoma.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, tool

API = "https://civicdb.org/api/graphql"
WEB = "https://civicdb.org"

EvidenceType = Literal["PREDICTIVE", "DIAGNOSTIC", "PROGNOSTIC", "PREDISPOSING", "ONCOGENIC", "FUNCTIONAL"]
EvidenceLevel = Literal["A", "B", "C", "D", "E"]

_SEARCH_TYPES = {
    "genes": "FEATURE",
    "variants": "VARIANT",
    "molecular_profiles": "MOLECULAR_PROFILE",
    "therapies": "THERAPY",
    "diseases": "DISEASE",
    "evidence": "EVIDENCE_ITEM",
    "assertions": "ASSERTION",
}

_EVIDENCE_FIELDS = """
  id name description evidenceType evidenceLevel evidenceDirection significance evidenceRating
  variantOrigin status therapyInteractionType
  molecularProfile { id name variants { id name ... on GeneVariant { feature { name featureInstance { ... on Gene { entrezId } } } } } }
  disease { id name doid }
  therapies { id name ncitId }
  source { citationId sourceType citation publicationYear pmcId title journal }
"""

_EVIDENCE_QUERY = (
    """
query($mp: String, $therapy: String, $disease: String, $type: EvidenceType, $level: EvidenceLevel,
      $status: EvidenceStatusFilter, $first: Int, $after: String) {
  evidenceItems(molecularProfileName: $mp, therapyName: $therapy, diseaseName: $disease, evidenceType: $type,
                evidenceLevel: $level, status: $status, first: $first, after: $after,
                sortBy: {column: EVIDENCE_LEVEL, direction: ASC}) {
    totalCount pageInfo { endCursor hasNextPage }
    nodes { %s }
  }
}
"""
    % _EVIDENCE_FIELDS
)

_GENE_QUERY = """
query($symbol: String!) { gene(entrezSymbol: $symbol) { id name fullName entrezId description link featureAliases } }
"""

_MP_FIELDS = """
  id name description link molecularProfileScore molecularProfileAliases isComplex
  evidenceCountsByType { predictiveCount diagnosticCount prognosticCount predisposingCount oncogenicCount functionalCount }
  assertions(first: 25) { totalCount nodes { id name ampLevel assertionType assertionDirection significance
                                              disease { name doid } therapies { name } } }
  variants { id name link ... on GeneVariant {
      alleleRegistryId clinvarIds hgvsDescriptions maneSelectTranscript variantAliases
      variantTypes { name }
      coordinates { chromosome start stop referenceBases variantBases referenceBuild }
      myVariantInfo { dbsnpRsid cosmicId }
      feature { id name featureInstance { ... on Gene { entrezId } } } } }
"""

_MP_BY_ID = "query($id: Int!) { molecularProfile(id: $id) { %s } }" % _MP_FIELDS
_MP_BY_NAME = "query($name: String!) { molecularProfiles(name: $name, first: 10) { nodes { id name } } }"

_ASSERTION_QUERY = """
query($mp: String, $therapy: String, $disease: String, $first: Int, $after: String) {
  assertions(molecularProfileName: $mp, therapyName: $therapy, diseaseName: $disease, first: $first, after: $after) {
    totalCount pageInfo { endCursor hasNextPage }
    nodes { id name summary description assertionType assertionDirection significance ampLevel status
            regulatoryApproval fdaCompanionTest nccnGuidelineVersion evidenceItemsCount variantOrigin
            nccnGuideline { name } acmgCodes { code }
            molecularProfile { id name variants { id ... on GeneVariant { feature { name featureInstance { ... on Gene { entrezId } } } } } }
            disease { id name doid } therapies { id name ncitId } }
  }
}
"""


class CivicSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=50, description="Maximum number of records to return")
    types: list[Literal["genes", "variants", "molecular_profiles", "therapies", "diseases", "evidence", "assertions"]] | None = Field(
        None, description="Restrict to these entity types (default: all)"
    )


class GeneEvidenceParams(ToolParams):
    gene: str = Field(description="HGNC gene symbol, e.g. BRAF, EGFR, ALK")
    evidence_type: EvidenceType | None = Field(None, description="Only this evidence type (PREDICTIVE = therapy response)")
    evidence_level: EvidenceLevel | None = Field(
        None, description="Only this level: A validated, B clinical, C case study, D preclinical, E inferential"
    )
    include_unreviewed: bool = Field(False, description="Include submitted (not yet accepted) evidence")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of evidence items per page")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


class TherapyEvidenceParams(ToolParams):
    therapy: str = Field(description="Therapy/drug name, e.g. vemurafenib, osimertinib, trastuzumab")
    disease: str | None = Field(None, description="Optional disease name filter, e.g. Melanoma, Lung Non-small Cell Carcinoma")
    evidence_level: EvidenceLevel | None = Field(None, description="Only this evidence level (A-E)")
    include_unreviewed: bool = Field(False, description="Include submitted (not yet accepted) evidence")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of evidence items per page")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


class MolecularProfileParams(ToolParams):
    profile: str = Field(
        description="CIViC molecular profile id (12, MPID12) or exact name (e.g. 'BRAF V600E', 'EGFR L858R', 'ALK Fusion')"
    )


class AssertionParams(ToolParams):
    gene_or_profile: str | None = Field(
        None, description="Gene symbol or molecular profile name prefix, e.g. BRAF or 'BRAF V600E'"
    )
    therapy: str | None = Field(None, description="Therapy name filter, e.g. dabrafenib")
    disease: str | None = Field(None, description="Disease name filter, e.g. Melanoma")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of assertions")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _doid(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text if text.upper().startswith("DOID:") else f"DOID:{text}"


def _genes(mp: dict[str, Any] | None) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for v in (mp or {}).get("variants") or []:
        feature = v.get("feature") or {}
        name = feature.get("name")
        if name and name not in [g for g, _ in out]:
            out.append((name, (feature.get("featureInstance") or {}).get("entrezId")))
    return out


def _source_ids(source: dict[str, Any] | None) -> dict[str, Any]:
    source = source or {}
    ids: dict[str, Any] = {}
    if source.get("sourceType") == "PUBMED":
        ids["pmid"] = source.get("citationId")
    if source.get("pmcId"):
        ids["pmcid"] = source.get("pmcId")
    return ids


def _related_ids(genes: list[tuple[str, Any]], disease: dict[str, Any] | None, source: dict[str, Any] | None) -> dict[str, Any]:
    """Ids of the entities a CIViC record is *about* (attributes, not identifiers)."""
    return {
        "genes": [g for g, _ in genes] or None,
        "entrez_genes": [str(e) for _, e in genes if e] or None,
        "disease_doid": _doid((disease or {}).get("doid")),
        **{k: str(v) for k, v in _source_ids(source).items() if v},
    }


def _label(value: str | None) -> str | None:
    return value.replace("_", " ").lower() if value else value


def evidence_record(item: dict[str, Any]) -> Record:
    mp = item.get("molecularProfile") or {}
    disease = item.get("disease") or {}
    therapies = item.get("therapies") or []
    source = item.get("source") or {}
    genes = _genes(mp)
    therapy_names = [t.get("name") for t in therapies if t.get("name")]
    joiner = " + " if item.get("therapyInteractionType") == "COMBINATION" else " / "
    parts = [mp.get("name"), disease.get("name"), joiner.join(therapy_names) or None]
    title = " | ".join(p for p in parts if p)
    title += f" ({_label(item.get('evidenceType'))}, {_label(item.get('significance'))}, level {item.get('evidenceLevel')})"
    identifiers: dict[str, Any] = {"civic": f"EID{item.get('id')}"}
    return Record(
        source="civic",
        id=f"EID{item.get('id')}",
        type="association",
        title=title,
        url=f"{WEB}/evidence/{item.get('id')}",
        identifiers=identifiers,
        date=str(source["publicationYear"]) if source.get("publicationYear") else None,
        summary=item.get("description"),
        attributes=_compact(
            {
                "molecular_profile": mp.get("name"),
                "molecular_profile_id": f"MPID{mp['id']}" if mp.get("id") else None,
                **_related_ids(genes, disease, source),
                "disease": disease.get("name"),
                "therapies": therapy_names,
                "therapy_ncit": [t.get("ncitId") for t in therapies if t.get("ncitId")],
                "therapy_interaction": item.get("therapyInteractionType"),
                "evidence_type": item.get("evidenceType"),
                "evidence_level": item.get("evidenceLevel"),
                "evidence_direction": item.get("evidenceDirection"),
                "significance": item.get("significance"),
                "rating": item.get("evidenceRating"),
                "variant_origin": item.get("variantOrigin"),
                "status": item.get("status"),
                "citation": source.get("citation"),
                "source_title": source.get("title"),
                "journal": source.get("journal"),
                "source_type": source.get("sourceType") if source.get("sourceType") != "PUBMED" else None,
                "source_id": source.get("citationId") if source.get("sourceType") != "PUBMED" else None,
            }
        ),
    )


def assertion_record(item: dict[str, Any]) -> Record:
    mp = item.get("molecularProfile") or {}
    disease = item.get("disease") or {}
    therapies = [t.get("name") for t in item.get("therapies") or [] if t.get("name")]
    genes = _genes(mp)
    title = " | ".join(p for p in (mp.get("name"), disease.get("name"), " + ".join(therapies) or None) if p)
    title += f" ({_label(item.get('assertionType'))}, {_label(item.get('significance'))})"
    return Record(
        source="civic",
        id=f"AID{item.get('id')}",
        type="association",
        title=title,
        url=f"{WEB}/assertions/{item.get('id')}",
        identifiers={"civic": f"AID{item.get('id')}"},
        summary=" ".join(x for x in (item.get("summary"), item.get("description")) if x) or None,
        attributes=_compact(
            {
                "molecular_profile": mp.get("name"),
                "molecular_profile_id": f"MPID{mp['id']}" if mp.get("id") else None,
                **_related_ids(genes, disease, None),
                "disease": disease.get("name"),
                "therapies": therapies,
                "assertion_type": item.get("assertionType"),
                "assertion_direction": item.get("assertionDirection"),
                "significance": item.get("significance"),
                "amp_level": item.get("ampLevel"),
                "acmg_codes": [c.get("code") for c in item.get("acmgCodes") or [] if c.get("code")],
                "regulatory_approval": item.get("regulatoryApproval"),
                "fda_companion_test": item.get("fdaCompanionTest"),
                "nccn_guideline": " ".join(
                    x for x in ((item.get("nccnGuideline") or {}).get("name"), item.get("nccnGuidelineVersion")) if x
                ) or None,
                "evidence_items": item.get("evidenceItemsCount"),
                "variant_origin": item.get("variantOrigin"),
                "status": item.get("status"),
            }
        ),
    )


def profile_record(mp: dict[str, Any]) -> Record:
    variants = mp.get("variants") or []
    genes = _genes(mp)
    hgvs: list[str] = []
    clinvar: list[str] = []
    rsids: list[str] = []
    detail = []
    for v in variants:
        hgvs += [h for h in v.get("hgvsDescriptions") or [] if h not in hgvs]
        clinvar += [str(c) for c in v.get("clinvarIds") or [] if str(c) not in clinvar]
        rs = (v.get("myVariantInfo") or {}).get("dbsnpRsid")
        if rs and rs not in rsids:
            rsids.append(rs)
        coords = v.get("coordinates") or {}
        detail.append(
            _compact(
                {
                    "variant_id": f"VID{v.get('id')}",
                    "gene": (v.get("feature") or {}).get("name"),
                    "name": v.get("name"),
                    "variant_types": [t.get("name") for t in v.get("variantTypes") or [] if t.get("name")],
                    "coordinates": (
                        f"{coords.get('referenceBuild')} chr{coords.get('chromosome')}:{coords.get('start')}-{coords.get('stop')} "
                        f"{coords.get('referenceBases') or '-'}>{coords.get('variantBases') or '-'}"
                        if coords.get("chromosome") and coords.get("start")
                        else None
                    ),
                    "allele_registry_id": v.get("alleleRegistryId"),
                    "mane_select": v.get("maneSelectTranscript"),
                    "cosmic": (v.get("myVariantInfo") or {}).get("cosmicId"),
                }
            )
        )
    counts = mp.get("evidenceCountsByType") or {}
    assertions = mp.get("assertions") or {}
    single = len(variants) == 1
    preferred_hgvs = next((h for h in hgvs if h.startswith("NM_") or h.startswith("ENST")), hgvs[0] if hgvs else None)
    return Record(
        source="civic",
        id=f"MPID{mp.get('id')}",
        type="variant",
        title=mp.get("name"),
        url=f"{WEB}/molecular-profiles/{mp.get('id')}",
        # Variant ids identify the profile only when it is a single variant (a complex profile is not
        # the same entity as its first variant); the gene is a related entity.
        identifiers={
            "civic": f"MPID{mp.get('id')}",
            "rsid": rsids[0] if single and len(rsids) == 1 else None,
            "clinvar": clinvar[0] if single and clinvar else None,
            "hgvs": preferred_hgvs if single else None,
        },
        summary=mp.get("description"),
        attributes=_compact(
            {
                **_related_ids(genes, None, None),
                "aliases": mp.get("molecularProfileAliases"),
                "is_complex": mp.get("isComplex") or None,
                "score": mp.get("molecularProfileScore"),
                "evidence_counts": {k.replace("Count", ""): v for k, v in counts.items() if v},
                "variants": detail,
                "hgvs": hgvs[:10],
                "clinvar_ids": clinvar if len(clinvar) > 1 else None,
                "rsids": rsids if len(rsids) > 1 else None,
                "assertion_count": assertions.get("totalCount"),
                "assertions": [
                    _compact(
                        {
                            "id": f"AID{a.get('id')}",
                            "type": a.get("assertionType"),
                            "significance": a.get("significance"),
                            "amp_level": a.get("ampLevel"),
                            "disease": (a.get("disease") or {}).get("name"),
                            "therapies": [t.get("name") for t in a.get("therapies") or []],
                        }
                    )
                    for a in assertions.get("nodes") or []
                ],
            }
        ),
    )


_RESULT_TYPES = {
    "FEATURE": ("gene", "GID", "features"),
    "VARIANT": ("variant", "VID", "variants"),
    "MOLECULAR_PROFILE": ("variant", "MPID", "molecular-profiles"),
    "THERAPY": ("drug", "TID", "therapies"),
    "DISEASE": ("disease", "DID", "diseases"),
    "EVIDENCE_ITEM": ("association", "EID", "evidence"),
    "ASSERTION": ("association", "AID", "assertions"),
    "VARIANT_GROUP": ("other", "VGID", "variant-groups"),
}


def search_record(hit: dict[str, Any]) -> Record:
    rtype, prefix, path = _RESULT_TYPES.get(hit.get("resultType") or "", ("other", "", ""))
    name = hit.get("name") or ""
    identifiers: dict[str, Any] = {"civic": f"{prefix}{hit.get('id')}"}
    if hit.get("resultType") == "FEATURE":
        identifiers["gene_symbol"] = name
    elif hit.get("resultType") == "VARIANT" and " - " in name:
        identifiers["gene_symbol"] = name.split(" - ", 1)[0]
    elif hit.get("resultType") == "MOLECULAR_PROFILE" and not re.search(r"(AND|OR|NOT)", name):
        token = name.split(" ", 1)[0]
        if re.fullmatch(r"[A-Z][A-Z0-9-]{1,14}", token):
            identifiers["gene_symbol"] = token
    link = hit.get("link") or (f"/{path}/{hit.get('id')}" if path else "")
    return Record(
        source="civic",
        id=f"{prefix}{hit.get('id')}",
        type=rtype,  # type: ignore[arg-type]
        title=name,
        url=f"{WEB}{link}" if link else None,
        identifiers=identifiers,
        summary=hit.get("matchingText") or None,
        attributes={"entity_type": hit.get("resultType")},
    )


def _gene_token_match(name: str, gene: str) -> bool:
    return re.search(rf"(^|[\s:(]){re.escape(gene)}($|[\s:)\-])", name, flags=re.IGNORECASE) is not None


class Civic(Connector):
    name = "civic"
    title = "CIViC (Clinical Interpretation of Variants in Cancer)"
    category = "genomics"
    description = (
        "Open, expert-curated knowledgebase of the clinical relevance of cancer variants: ~10k evidence "
        "items and AMP/ASCO/CAP-tiered assertions linking molecular profiles (e.g. BRAF V600E) to "
        "diseases (DOID), therapies and outcomes with PubMed sources. Use for therapy response, "
        "prognosis and diagnosis evidence for somatic/germline cancer variants."
    )
    homepage = "https://civicdb.org/"
    docs_url = "https://griffithlab.github.io/civic-v2/"
    auth = "none"
    rate_limit = 2.0
    health_query = "BRAF"
    nih_repositories = ("Clinical Interpretation of Variants in Cancer (CIViC)",)

    async def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return await self.graphql(API, query, {k: v for k, v in variables.items() if v is not None})

    @tool(
        description="""
        Quick search across CIViC genes, variants, molecular profiles, therapies, diseases, evidence
        and assertions by name (e.g. "BRAF V600E", "EGFR", "osimertinib", "melanoma"). Returns typed
        records with CIViC ids (GID/VID/MPID/TID/DID/EID/AID) and links; restrict with types. Then
        use gene_evidence, molecular_profile, evidence_for_therapy or assertions for details.
        """,
        federated=True,
    )
    async def search(self, params: CivicSearchParams) -> ToolResult:
        variables: dict[str, Any] = {"q": params.query.strip()}
        if params.types:
            variables["types"] = [_SEARCH_TYPES[t] for t in params.types]
            query = "query($q: String!, $types: [SearchableEntities!]) { search(query: $q, types: $types) { id name resultType matchingText link } }"
        else:
            query = "query($q: String!) { search(query: $q) { id name resultType matchingText link } }"
        data = await self._gql(query, variables)
        hits = data.get("search") or []
        # Prefer the most specific entity types first (profiles/variants/genes before evidence noise).
        order = ["FEATURE", "MOLECULAR_PROFILE", "VARIANT", "THERAPY", "DISEASE", "ASSERTION", "EVIDENCE_ITEM"]
        q = params.query.strip().lower()
        hits = sorted(
            hits,
            key=lambda h: (
                0 if (h.get("name") or "").lower().replace(" - ", " ") == q else 1,
                order.index(h.get("resultType")) if h.get("resultType") in order else 99,
            ),
        )
        return ToolResult(
            source=self.name,
            tool="search",
            records=[search_record(h) for h in hits[: params.limit]],
            total=len(hits),
            request_url=API,
            notes=[] if hits else [f"no CIViC entity matches {params.query!r}"],
        )

    async def _evidence(self, tool_name: str, variables: dict[str, Any], limit: int, cursor: str | None,
                        gene: str | None = None) -> ToolResult:
        kept: list[dict[str, Any]] = []
        dropped = 0
        total = None
        after = cursor
        has_next = False
        # Gene queries are prefix matches upstream; keep paging (asking only for the remaining
        # slots, so nothing is skipped) until enough items for this gene are collected.
        for _ in range(5 if gene else 1):
            data = await self._gql(_EVIDENCE_QUERY, {**variables, "first": limit - len(kept), "after": after})
            conn = data.get("evidenceItems") or {}
            total = conn.get("totalCount") if total is None else total
            nodes = conn.get("nodes") or []
            for node in nodes:
                if gene and not _gene_token_match((node.get("molecularProfile") or {}).get("name") or "", gene):
                    dropped += 1
                else:
                    kept.append(node)
            page = conn.get("pageInfo") or {}
            has_next = bool(page.get("hasNextPage"))
            after = page.get("endCursor") or after
            if len(kept) >= limit or not has_next:
                break
        notes: list[str] = []
        if dropped:
            notes.append(
                f"skipped {dropped} item(s) for other genes whose name starts with {gene!r}; "
                "total counts all prefix matches"
            )
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[evidence_record(n) for n in kept],
            total=total,
            next_cursor=after if has_next else None,
            request_url=API,
            notes=notes,
        )

    @tool(
        description="""
        Clinical evidence items for a gene's variants in CIViC (gene symbol, e.g. BRAF, EGFR), best
        evidence level first (A validated > B clinical > C case study > D preclinical > E inferential):
        molecular profile (variant), disease (DOID), therapies, evidence type (PREDICTIVE = therapy
        response, PROGNOSTIC, DIAGNOSTIC, PREDISPOSING, ONCOGENIC, FUNCTIONAL), direction,
        significance (e.g. SENSITIVITYRESPONSE, RESISTANCE), rating and source PMID. Accepted
        evidence only unless include_unreviewed. data.gene gives the gene's Entrez id/description.
        """
    )
    async def gene_evidence(self, params: GeneEvidenceParams) -> ToolResult:
        gene = params.gene.strip().upper()
        result = await self._evidence(
            "gene_evidence",
            {
                "mp": gene,
                "type": params.evidence_type,
                "level": params.evidence_level,
                "status": "NON_REJECTED" if params.include_unreviewed else "ACCEPTED",
            },
            params.limit,
            params.cursor,
            gene=gene,
        )
        if not params.cursor:
            info = (await self._gql(_GENE_QUERY, {"symbol": gene})).get("gene")
            if info:
                result.data = {
                    "gene": _compact(
                        {
                            "civic_id": f"GID{info.get('id')}",
                            "symbol": info.get("name"),
                            "full_name": info.get("fullName"),
                            "entrez_gene": info.get("entrezId"),
                            "aliases": info.get("featureAliases"),
                            "description": info.get("description"),
                            "url": f"{WEB}{info['link']}" if info.get("link") else None,
                        }
                    )
                }
            elif not result.records:
                result.notes.append(f"gene {gene} is not in CIViC")
        return result

    @tool(
        description="""
        CIViC evidence items involving a therapy (drug name, e.g. vemurafenib, osimertinib,
        cetuximab), optionally limited to a disease name: which molecular profiles predict
        sensitivity/resistance, in which diseases, at what evidence level, with source PMIDs.
        """
    )
    async def evidence_for_therapy(self, params: TherapyEvidenceParams) -> ToolResult:
        return await self._evidence(
            "evidence_for_therapy",
            {
                "therapy": params.therapy.strip(),
                "disease": params.disease.strip() if params.disease else None,
                "level": params.evidence_level,
                "status": "NON_REJECTED" if params.include_unreviewed else "ACCEPTED",
            },
            params.limit,
            params.cursor,
        )

    @tool(
        description="""
        Details for one CIViC molecular profile (by id, MPID12, or exact name like 'BRAF V600E'):
        description, aliases, evidence counts by type, assertions (AMP tier, disease, therapies), and
        each variant's genomic coordinates, HGVS expressions, ClinVar ids, dbSNP rsID, ClinGen allele
        registry id and COSMIC id.
        """
    )
    async def molecular_profile(self, params: MolecularProfileParams) -> ToolResult:
        value = params.profile.strip()
        digits = re.fullmatch(r"(?:MPID|MP)?\s*(\d+)", value, flags=re.IGNORECASE)
        notes: list[str] = []
        if digits:
            mp_id = int(digits.group(1))
        else:
            nodes = ((await self._gql(_MP_BY_NAME, {"name": value})).get("molecularProfiles") or {}).get("nodes") or []
            exact = next((n for n in nodes if (n.get("name") or "").lower() == value.lower()), None)
            chosen = exact or (nodes[0] if nodes else None)
            if not chosen:
                return ToolResult(source=self.name, tool="molecular_profile", notes=[f"no CIViC molecular profile named {value!r}; try search"])
            if not exact:
                notes.append(
                    f"no exact name match; using {chosen.get('name')} (others: "
                    + ", ".join(n.get("name") for n in nodes[1:5])
                    + ")"
                )
            mp_id = int(chosen["id"])
        mp = (await self._gql(_MP_BY_ID, {"id": mp_id})).get("molecularProfile")
        if not mp:
            return ToolResult(source=self.name, tool="molecular_profile", notes=[f"molecular profile {mp_id} not found in CIViC"])
        return ToolResult(
            source=self.name, tool="molecular_profile", records=[profile_record(mp)], total=1, request_url=API, notes=notes
        )

    @tool(
        description="""
        CIViC assertions: curated, guideline-style summaries of many evidence items (AMP/ASCO/CAP tier,
        ACMG codes, FDA companion test / regulatory approval, NCCN guideline) for a gene or molecular
        profile name prefix, therapy and/or disease. Example: gene_or_profile="BRAF V600E", disease="Melanoma".
        """
    )
    async def assertions(self, params: AssertionParams) -> ToolResult:
        if not (params.gene_or_profile or params.therapy or params.disease):
            return ToolResult(source=self.name, tool="assertions", notes=["give gene_or_profile, therapy or disease"])
        data = await self._gql(
            _ASSERTION_QUERY,
            {
                "mp": params.gene_or_profile.strip() if params.gene_or_profile else None,
                "therapy": params.therapy.strip() if params.therapy else None,
                "disease": params.disease.strip() if params.disease else None,
                "first": params.limit,
                "after": params.cursor,
            },
        )
        conn = data.get("assertions") or {}
        nodes = conn.get("nodes") or []
        notes = []
        query_mp = (params.gene_or_profile or "").strip()
        if query_mp and " " not in query_mp:
            kept = [n for n in nodes if _gene_token_match((n.get("molecularProfile") or {}).get("name") or "", query_mp)]
            if len(kept) < len(nodes):
                notes.append(f"dropped {len(nodes) - len(kept)} assertion(s) for other genes starting with {query_mp!r}")
            nodes = kept
        page = conn.get("pageInfo") or {}
        return ToolResult(
            source=self.name,
            tool="assertions",
            records=[assertion_record(n) for n in nodes],
            total=conn.get("totalCount"),
            next_cursor=page.get("endCursor") if page.get("hasNextPage") else None,
            request_url=API,
            notes=notes + ([] if nodes else ["no CIViC assertions match these filters"]),
        )
