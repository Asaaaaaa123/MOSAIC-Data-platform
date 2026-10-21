"""RxNorm / RxNav / RxClass connector (NLM).

Docs: https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html and
https://lhncbc.nlm.nih.gov/RxNav/APIs/RxClassAPIs.html
Auth: none. Rate limit: at most 20 requests/second per IP; we use 15.

Quirks:
- Append ``.json`` to every path for JSON. Unknown RxCUIs return 200 with ``{}`` (properties) and
  malformed ones 404 ("Path or Query Parameter error"); both are mapped to empty results.
- ``approximateTerm`` returns one candidate per atom (several per RxCUI) without names for some
  sources, so candidates are de-duplicated by RxCUI and names/TTYs fetched from ``properties``.
- ``drugs.json`` returns clinical/branded drug products (SCD/SBD/packs) but not ingredients.
- RxClass sources: ATC, ATCPROD, CDC, DAILYMED, FDASPL, FMTSME, MEDRT, RXNORM, SNOMEDCT, VA. The old
  MESHPA source is gone; MED-RT ``may_treat``/``may_prevent``/``ci_with`` disease classes use MeSH
  descriptor ids (D######), which we expose as ``mesh`` identifiers.
- The RxNav drug-interaction API was discontinued in January 2024 and is not implemented.

Identifiers: ``rxcui`` everywhere; ``get_concept`` adds ``unii``, ``drugbank`` and ``atc`` from the
concept's source codes; class records carry ``atc`` or ``mesh``; NDCs are 11-digit (``ndc`` key).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_identifiers
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://rxnav.nlm.nih.gov/REST"
RXNAV_URL = "https://mor.nlm.nih.gov/RxNav/search?searchBy=RXCUI&searchTerm="

TTY_LABELS = {
    "IN": "ingredient",
    "PIN": "precise ingredient (salt/form)",
    "MIN": "multiple-ingredient combination",
    "BN": "brand name",
    "DF": "dose form",
    "DFG": "dose form group",
    "SCD": "semantic clinical drug",
    "SBD": "semantic branded drug",
    "SCDC": "clinical drug component",
    "SBDC": "branded drug component",
    "SCDF": "clinical dose form",
    "SBDF": "branded dose form",
    "SCDG": "clinical dose form group",
    "SBDG": "branded dose form group",
    "SCDFP": "clinical dose form (prescribable)",
    "SBDFP": "branded dose form (prescribable)",
    "SCDGP": "clinical dose form group (prescribable)",
    "GPCK": "generic pack",
    "BPCK": "branded pack",
}
_TTY_ORDER = ["IN", "PIN", "MIN", "BN", "SCD", "SBD", "GPCK", "BPCK", "SCDC", "SBDC", "SCDF", "SBDF", "DF"]
_RELATED_DEFAULT = ["IN", "PIN", "MIN", "BN", "DF", "SCD", "SBD", "GPCK", "BPCK"]

Tty = Literal[
    "IN", "PIN", "MIN", "BN", "DF", "DFG", "SCD", "SBD", "SCDC", "SBDC", "SCDF", "SBDF", "SCDG", "SBDG", "GPCK", "BPCK"
]

_CLASS_TYPES: dict[str, tuple[str | None, str | None]] = {
    "atc": ("ATC", None),
    "epc": ("DAILYMED", "has_epc"),
    "moa": ("MEDRT", "has_moa"),
    "pe": ("MEDRT", "has_pe"),
    "pk": ("MEDRT", "has_pk"),
    "may_treat": ("MEDRT", "may_treat"),
    "may_prevent": ("MEDRT", "may_prevent"),
    "may_diagnose": ("MEDRT", "may_diagnose"),
    "ci_with": ("MEDRT", "ci_with"),
    "chemical_structure": ("DAILYMED", "has_chemical_structure"),
    "va": ("VA", "has_VAClass"),
    "all": (None, None),
}

_ID_TYPES = {
    "NDC": "NDC",
    "UNII": "UNII_CODE",
    "ATC": "ATC",
    "DRUGBANK": "DRUGBANK",
    "SNOMEDCT": "SNOMEDCT",
    "SPL_SET_ID": "SPL_SET_ID",
    "VUID": "VUID",
    "NDA": "NDA",
    "ANDA": "ANDA",
    "BLA": "BLA",
    "MMSL_CODE": "MMSL_CODE",
    "GCN_SEQNO": "GCN_SEQNO",
    "USP": "USP",
    "CVX": "CVX",
    "HCPCS": "HCPCS",
}


# ---------------------------------------------------------------- params
class RxSearchParams(SearchParams):
    query: str = Field(
        description="Drug name, possibly misspelled or with strength/form, e.g. 'metformin', 'Keytruda', "
        "'atorvastatin 20 mg tablet'"
    )
    tty: list[Tty] | None = Field(
        None, description="Keep only these term types, e.g. ['IN'] for ingredients or ['SCD','SBD'] for products"
    )


class GetConceptParams(ToolParams):
    rxcui: str = Field(description="RxNorm concept id (digits), e.g. 6809 (metformin) or 1657751")
    related_ttys: list[Tty] | None = Field(
        None, description="Related term types to include (default IN, PIN, MIN, BN, DF, SCD, SBD, GPCK, BPCK)"
    )
    max_per_group: int = Field(25, ge=1, le=200, description="Maximum related concepts listed per term type")


class DrugClassesParams(ToolParams):
    rxcui: str | None = Field(None, description="RxCUI of an ingredient (IN/PIN), e.g. 6809")
    drug_name: str | None = Field(None, description="Ingredient name, e.g. 'metformin' (used when rxcui is not given)")
    class_type: Literal[
        "atc", "epc", "moa", "pe", "pk", "may_treat", "may_prevent", "may_diagnose", "ci_with",
        "chemical_structure", "va", "all",
    ] = Field(
        "all",
        description=(
            "atc = WHO ATC classes; epc = FDA established pharmacologic class; moa / pe / pk = MED-RT mechanism, "
            "physiologic effect, pharmacokinetics; may_treat / may_prevent / may_diagnose / ci_with = MED-RT "
            "disease relations (MeSH ids); chemical_structure; va = VA drug class; all = everything"
        ),
    )
    include_combinations: bool = Field(
        False, description="Also include classes reached only through combination products (MIN concepts)"
    )


class FindByIdParams(ToolParams):
    id_type: Literal[
        "NDC", "UNII", "ATC", "DRUGBANK", "SNOMEDCT", "SPL_SET_ID", "VUID", "NDA", "ANDA", "BLA",
        "MMSL_CODE", "GCN_SEQNO", "USP", "CVX", "HCPCS",
    ] = Field(description="Type of the external identifier")
    id: str = Field(
        description="Identifier value, e.g. NDC 00006302602 or 0006-3026-02, UNII 9100L32L2N, ATC A10BA02, "
        "DrugBank DB00331, NDA020357"
    )


class NdcsParams(ToolParams):
    rxcui: str = Field(description="RxCUI of a clinical/branded drug or pack (SCD, SBD, GPCK, BPCK), e.g. 860975")
    status: Literal["active", "all"] = Field(
        "active", description="active = current NDCs with packaging/labeler; all = full NDC history with dates"
    )
    limit: int = Field(100, ge=1, le=1000, description="Maximum NDCs returned")


# ---------------------------------------------------------------- helpers
def _clean_rxcui(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits or None


def _concept_record(props: dict[str, Any], **extra: Any) -> Record:
    rxcui = str(props.get("rxcui") or "")
    tty = props.get("tty")
    attributes = {
        "tty": tty,
        "tty_label": TTY_LABELS.get(tty or ""),
        "synonym": props.get("synonym") or None,
        "suppress": props.get("suppress") if props.get("suppress") not in (None, "N") else None,
        **extra,
    }
    return Record(
        source="rxnorm",
        id=rxcui,
        type="drug",
        title=props.get("name"),
        url=f"{RXNAV_URL}{rxcui}" if rxcui else None,
        identifiers={"rxcui": rxcui},
        attributes={k: v for k, v in attributes.items() if v not in (None, "", [], {})},
    )


def _ndc11(value: str) -> str:
    """Normalize an NDC to 11 digits (5-4-2) when it is dashed; otherwise strip non-digits."""
    value = value.strip()
    if "-" in value:
        parts = value.split("-")
        if len(parts) == 3:
            lab, prod, pkg = parts
            return f"{lab.zfill(5)}{prod.zfill(4)}{pkg.zfill(2)}"
    return re.sub(r"\D", "", value)


# ---------------------------------------------------------------- connector
class RxNorm(Connector):
    name = "rxnorm"
    title = "RxNorm (NLM RxNav)"
    category = "drugs"
    description = (
        "NLM's normalized names and codes for all US prescription and OTC drugs (ingredients, brands, clinical "
        "and branded products, packs) with RxClass drug classes (ATC, FDA EPC, MED-RT MoA and may_treat). Best "
        "for resolving drug names to RxCUIs and mapping NDC/UNII/ATC/DrugBank codes."
    )
    homepage = "https://www.nlm.nih.gov/research/umls/rxnorm/"
    docs_url = "https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html"
    auth = "none"
    rate_limit = 15.0
    health_query = "metformin"
    nih_repositories = ("RxNorm",)

    async def _json(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
        response = await self.request("GET", f"{API}/{path}", params=params, allow_status=(400, 404))
        if response.status in (400, 404):
            return {}, response.url
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return (payload if isinstance(payload, dict) else {}), response.url

    async def _properties(self, rxcui: str) -> dict[str, Any]:
        payload, _ = await self._json(f"rxcui/{quote(rxcui)}/properties.json")
        return payload.get("properties") or {}

    async def _status_note(self, rxcui: str) -> str:
        payload, _ = await self._json(f"rxcui/{quote(rxcui)}/historystatus.json")
        status = (payload.get("rxcuiStatusHistory") or {}).get("metaData") or {}
        state = status.get("status")
        if not state or state == "UNKNOWN":
            return f"RxCUI {rxcui} not found in RxNorm"
        derived = ((payload.get("rxcuiStatusHistory") or {}).get("derivedConcepts") or {}).get("remappedConcept") or []
        remap = ", ".join(f"{c.get('remappedRxCui')} ({c.get('remappedName')})" for c in derived[:5])
        return f"RxCUI {rxcui} is {state.lower()}" + (f"; remapped to {remap}" if remap else "")

    # ---- tools
    @tool(
        description="""
        Resolve a drug name (brand, generic, misspelled, or with strength/form) to RxNorm concepts
        (RxCUI + name + term type). Approximate-match candidates come first (usually the ingredient IN
        or brand BN), followed by clinical/branded products (SCD/SBD) containing the drug. Use this
        to normalize drug mentions before linking to other sources (RxCUI is the hub id for US drugs);
        then call get_concept for codes/related concepts or drug_classes for ATC/EPC/MoA classes.
        Filter with tty, e.g. ['IN'] for ingredients only. Example: {"query": "Glucophage"}.
        """,
        federated=True,
    )
    async def search(self, params: RxSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        wanted = set(params.tty or [])
        approx, url = await self._json(
            "approximateTerm.json", {"term": params.query, "maxEntries": 20, "option": 1}
        )
        ranked: list[tuple[str, str]] = []  # (rxcui, rank)
        for cand in (approx.get("approximateGroup") or {}).get("candidate") or []:
            rxcui = str(cand.get("rxcui") or "")
            if rxcui and rxcui not in [r for r, _ in ranked]:
                ranked.append((rxcui, str(cand.get("rank") or "")))
        props_list = await asyncio.gather(*(self._properties(r) for r, _ in ranked[:20]))
        concepts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for (rxcui, rank), props in zip(ranked, props_list):
            if props and (not wanted or props.get("tty") in wanted):
                concepts.append((props, {"match": "approximate", "match_rank": int(rank) if rank.isdigit() else None}))
                seen.add(rxcui)
        drugs, _ = await self._json("drugs.json", {"name": params.query})
        groups = (drugs.get("drugGroup") or {}).get("conceptGroup") or []
        groups.sort(key=lambda g: _TTY_ORDER.index(g.get("tty")) if g.get("tty") in _TTY_ORDER else 99)
        for group in groups:
            for props in group.get("conceptProperties") or []:
                rxcui = str(props.get("rxcui") or "")
                if rxcui and rxcui not in seen and (not wanted or props.get("tty") in wanted):
                    concepts.append((props, {"match": "product containing name"}))
                    seen.add(rxcui)
        page = concepts[offset : offset + params.limit]
        notes: list[str] = []
        if not concepts:
            sugg, _ = await self._json("spellingsuggestions.json", {"name": params.query})
            suggestions = ((sugg.get("suggestionGroup") or {}).get("suggestionList") or {}).get("suggestion") or []
            notes.append(
                f"no RxNorm concepts matched {params.query!r}"
                + (f"; spelling suggestions: {', '.join(suggestions[:5])}" if suggestions else "")
            )
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_concept_record(p, **{k: v for k, v in extra.items() if v is not None}) for p, extra in page],
            total=len(concepts),
            next_cursor=str(nxt) if page and nxt < len(concepts) else None,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Full RxNorm concept by RxCUI: name, term type, source codes (UNII, DrugBank, ATC, SNOMED CT,
        VUID, count of SPL set ids) mapped to identifiers, and related concepts grouped by term type:
        IN (ingredients), PIN (salts), MIN (combinations), BN (brand names), DF (dose forms), SCD /
        SBD (clinical / branded products with strength), GPCK / BPCK (packs). Use to go from an
        ingredient to all its brands and products (or a product to its ingredient), and to get the
        UNII / DrugBank / ATC cross-references for linking. Reports retired/remapped RxCUIs.
        """
    )
    async def get_concept(self, params: GetConceptParams) -> ToolResult:
        rxcui = _clean_rxcui(params.rxcui)
        if not rxcui:
            return ToolResult(source=self.name, tool="get_concept", notes=[f"{params.rxcui!r} is not an RxCUI"])
        props_payload, url = await self._json(f"rxcui/{rxcui}/properties.json")
        props = props_payload.get("properties") or {}
        if not props:
            return ToolResult(
                source=self.name, tool="get_concept", notes=[await self._status_note(rxcui)], request_url=url
            )
        codes_payload, related_payload = await asyncio.gather(
            self._json(f"rxcui/{rxcui}/allProperties.json", {"prop": "codes attributes"}),
            self._json(f"rxcui/{rxcui}/allrelated.json"),
        )
        codes: dict[str, list[str]] = {}
        for item in ((codes_payload[0].get("propConceptGroup") or {}).get("propConcept")) or []:
            name, value = item.get("propName"), item.get("propValue")
            if name and value and value not in codes.setdefault(name, []):
                codes[name].append(value)
        wanted = params.related_ttys or _RELATED_DEFAULT
        related: dict[str, list[str]] = {}
        related_counts: dict[str, int] = {}
        for group in ((related_payload[0].get("allRelatedGroup") or {}).get("conceptGroup")) or []:
            tty = group.get("tty")
            if tty not in wanted:
                continue
            items = group.get("conceptProperties") or []
            entries = [f"{c.get('name')} [{c.get('rxcui')}]" for c in items if str(c.get("rxcui")) != rxcui]
            if entries:
                related_counts[tty] = len(entries)
                related[tty] = entries[: params.max_per_group]
        spl = codes.get("SPL_SET_ID") or []
        record = _concept_record(
            props,
            codes=dict(
                (k, v if len(v) <= 10 else v[:10] + [f"... (+{len(v) - 10} more)"])
                for k, v in codes.items()
                if k in ("ATC", "DRUGBANK", "UNII_CODE", "SNOMEDCT", "VUID", "MMSL_CODE", "USP", "NDA", "ANDA", "BLA")
            )
            or None,
            spl_set_id_count=len(spl) or None,
            prescribable=(codes.get("PRESCRIBABLE") or [None])[0],
            human_drug=(codes.get("RXNAV_HUMAN_DRUG") or [None])[0],
            related={k: related[k] for k in wanted if k in related} or None,
            related_counts={k: related_counts[k] for k in wanted if k in related_counts} or None,
        )
        uniis = codes.get("UNII_CODE") or []
        record.identifiers.update(
            normalize_identifiers(
                {
                    "unii": uniis[0] if len(uniis) == 1 else None,
                    "drugbank": codes.get("DRUGBANK"),
                    "atc": codes.get("ATC"),
                    "spl_set_id": spl[0] if len(spl) == 1 else None,
                }
            )
        )
        notes = []
        if any(n > params.max_per_group for n in related_counts.values()):
            notes.append(f"related lists truncated to {params.max_per_group} per term type (see related_counts)")
        return ToolResult(
            source=self.name, tool="get_concept", records=[record], total=1, request_url=url, notes=notes
        )

    @tool(
        description="""
        Drug classes for an ingredient from RxClass (by rxcui or drug_name): WHO ATC classes, FDA
        established pharmacologic class (EPC), MED-RT mechanism of action (MoA), physiologic effect,
        pharmacokinetics, and disease relations may_treat / may_prevent / may_diagnose / ci_with
        (contraindicated-with), plus VA classes. One record per class with class id (ATC code or MeSH
        D-number as identifiers), class type and relation. Use for "what class is X", MoA lookups and
        drug-indication evidence; combination-product classes are excluded unless
        include_combinations=true.
        """
    )
    async def drug_classes(self, params: DrugClassesParams) -> ToolResult:
        source, rela = _CLASS_TYPES[params.class_type]
        query: dict[str, Any] = {"relaSource": source, "relas": rela}
        if params.rxcui:
            rxcui = _clean_rxcui(params.rxcui)
            if not rxcui:
                return ToolResult(source=self.name, tool="drug_classes", notes=[f"{params.rxcui!r} is not an RxCUI"])
            payload, url = await self._json("rxclass/class/byRxcui.json", {"rxcui": rxcui, **query})
        elif params.drug_name:
            payload, url = await self._json("rxclass/class/byDrugName.json", {"drugName": params.drug_name, **query})
        else:
            return ToolResult(source=self.name, tool="drug_classes", notes=["give rxcui or drug_name"])
        items = (payload.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo") or []
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        skipped = 0
        for item in items:
            concept = item.get("minConcept") or {}
            cls = item.get("rxclassMinConceptItem") or {}
            # ATCPROD = ATC classes of products containing the drug (includes combination products)
            if (concept.get("tty") == "MIN" or item.get("relaSource") == "ATCPROD") and not params.include_combinations:
                skipped += 1
                continue
            key = (str(cls.get("classId")), item.get("rela") or "")
            entry = merged.setdefault(key, {"class": cls, "sources": [], "via": []})
            if item.get("relaSource") and item["relaSource"] not in entry["sources"]:
                entry["sources"].append(item["relaSource"])
            via = f"{concept.get('name')} [{concept.get('rxcui')}]"
            if via not in entry["via"]:
                entry["via"].append(via)
        records = []
        order = {"ATC": 0, "DAILYMED": 1, "FDASPL": 2, "MEDRT": 3, "ATCPROD": 4}
        for (class_id, rela_name), entry in merged.items():
            cls = entry["class"]
            entry["sources"].sort(key=lambda src: order.get(src, 9))
            source_name = entry["sources"][0] if entry["sources"] else ""
            identifiers: dict[str, Any] = {}
            if source_name in ("ATC", "ATCPROD") and re.match(r"^[A-Z]\d{2}[A-Z]{0,2}\d{0,2}$", class_id):
                identifiers["atc"] = class_id
            elif re.match(r"^D\d{6,9}$", class_id):
                identifiers["mesh"] = class_id
            records.append(
                Record(
                    source="rxnorm",
                    id=class_id,
                    type="disease" if cls.get("classType") == "DISEASE" else "ontology_term",
                    title=cls.get("className"),
                    url=f"https://mor.nlm.nih.gov/RxClass/search?query={quote(class_id)}&searchBy=class",
                    identifiers=identifiers,
                    attributes={
                        k: v
                        for k, v in {
                            "class_type": cls.get("classType"),
                            "relation": rela_name or None,
                            "rela_source": source_name,
                            "also_in": entry["sources"][1:],
                            "drug_concepts": entry["via"][:10],
                        }.items()
                        if v
                    },
                )
            )
        records.sort(key=lambda r: (order.get(r.attributes.get("rela_source", ""), 9), r.attributes.get("relation", "")))
        notes = []
        if skipped:
            notes.append(f"{skipped} class links via combination products or product-level ATC (ATCPROD) omitted (include_combinations=true to show)")
        if not records:
            notes.append("no RxClass classes found (classes attach to ingredients: pass an IN/PIN rxcui or name)")
        return ToolResult(
            source=self.name,
            tool="drug_classes",
            records=records,
            total=len(records),
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Map an external drug identifier to RxNorm concept(s): NDC (10/11-digit or dashed; historical
        NDCs resolved via NDC status), UNII, ATC code, DrugBank id, SNOMED CT, SPL set id, VUID,
        NDA/ANDA/BLA application, MMSL, GCN_SEQNO, USP, CVX (vaccines) or HCPCS. Returns one record per
        RxCUI with name and term type; for NDCs also the NDC status. Use to bridge FDA, claims, VA and
        DrugBank data into RxNorm.
        """
    )
    async def find_by_id(self, params: FindByIdParams) -> ToolResult:
        value = params.id.strip()
        notes: list[str] = []
        extra: dict[str, Any] = {}
        if params.id_type == "NDC":
            ndc = _ndc11(value)
            payload, url = await self._json("ndcstatus.json", {"ndc": ndc})
            status = payload.get("ndcStatus") or {}
            rxcuis = [str(status["rxcui"])] if status.get("rxcui") else []
            if status:
                extra = {
                    "ndc_status": status.get("status"),
                    "ndc11": status.get("ndc11"),
                    "concept_status": status.get("conceptStatus"),
                }
            if not rxcuis and status.get("status"):
                notes.append(f"NDC {ndc} status {status.get('status')}: no RxNorm concept")
        else:
            idtype = _ID_TYPES[params.id_type]
            if params.id_type in ("UNII", "ATC", "DRUGBANK", "NDA", "ANDA", "BLA"):
                value = value.upper().replace(" ", "")
            payload, url = await self._json("rxcui.json", {"idtype": idtype, "id": value})
            rxcuis = [str(r) for r in (payload.get("idGroup") or {}).get("rxnormId") or []]
        props_list = await asyncio.gather(*(self._properties(r) for r in rxcuis[:25]))
        records = []
        for rxcui, props in zip(rxcuis, props_list):
            rec = _concept_record(props or {"rxcui": rxcui}, matched_id=f"{params.id_type}:{value}", **extra)
            if params.id_type == "NDC" and extra.get("ndc11"):
                rec.identifiers["ndc"] = extra["ndc11"]
            elif params.id_type in ("UNII", "ATC", "DRUGBANK"):
                key = params.id_type.lower()
                if props.get("tty") in ("IN", "PIN", "MIN"):
                    rec.identifiers[key] = value
            records.append(rec)
        if not records and not notes:
            notes.append(f"no RxNorm concept for {params.id_type} {value}")
        return ToolResult(
            source=self.name, tool="find_by_id", records=records, total=len(records), request_url=url, notes=notes
        )

    @tool(
        description="""
        NDC package codes for an RxNorm drug product (SCD, SBD, GPCK or BPCK RxCUI; ingredients have
        no NDCs, so first get product RxCUIs from get_concept or search). status='active' returns
        current 11-digit NDCs with packaging, labeler, marketing category/status, application number
        and SPL set id; status='all' returns the full NDC history with start/end months. Returns
        data rows.
        """
    )
    async def ndcs_for_rxcui(self, params: NdcsParams) -> ToolResult:
        rxcui = _clean_rxcui(params.rxcui)
        if not rxcui:
            return ToolResult(source=self.name, tool="ndcs_for_rxcui", notes=[f"{params.rxcui!r} is not an RxCUI"])
        rows: list[dict[str, Any]] = []
        if params.status == "active":
            payload, url = await self._json("ndcproperties.json", {"id": rxcui, "ndcstatus": "active"})
            for item in (payload.get("ndcPropertyList") or {}).get("ndcProperty") or []:
                props: dict[str, str] = {}
                for p in (item.get("propertyConceptList") or {}).get("propertyConcept") or []:
                    props.setdefault(p.get("propName"), p.get("propValue"))
                rows.append(
                    {
                        k: v
                        for k, v in {
                            "ndc11": item.get("ndcItem"),
                            "ndc10": item.get("ndc10"),
                            "product_ndc": item.get("ndc9"),
                            "rxcui": item.get("rxcui"),
                            "spl_set_id": item.get("splSetIdItem"),
                            "packaging": "; ".join((item.get("packagingList") or {}).get("packaging") or []) or None,
                            "labeler": props.get("LABELER"),
                            "marketing_category": props.get("MARKETING_CATEGORY"),
                            "marketing_status": props.get("MARKETING_STATUS"),
                            "application": props.get("NDA") or props.get("ANDA") or props.get("BLA"),
                            "label_type": props.get("LABEL_TYPE"),
                        }.items()
                        if v
                    }
                )
        else:
            payload, url = await self._json(f"rxcui/{rxcui}/allhistoricalndcs.json")
            for block in (payload.get("historicalNdcConcept") or {}).get("historicalNdcTime") or []:
                for t in block.get("ndcTime") or []:
                    for ndc in t.get("ndc") or []:
                        rows.append(
                            {
                                "ndc11": ndc,
                                "rxcui": block.get("rxcui"),
                                "link": block.get("status"),
                                "start": t.get("startDate"),
                                "end": t.get("endDate"),
                            }
                        )
        total = len(rows)
        notes = []
        if not rows:
            props = await self._properties(rxcui)
            tty = props.get("tty")
            notes.append(
                f"no {params.status} NDCs for RxCUI {rxcui}"
                + (f" (term type {tty}: NDCs attach to SCD/SBD/GPCK/BPCK products)" if tty else " (unknown RxCUI)")
            )
        elif total > params.limit:
            notes.append(f"showing {params.limit} of {total} NDCs")
        return ToolResult(
            source=self.name,
            tool="ndcs_for_rxcui",
            total=total,
            data=rows[: params.limit],
            request_url=url,
            notes=notes,
        )
