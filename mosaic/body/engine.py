"""Body map engine: where in the human body a drug, chemical or disease acts, from database evidence.

``build_body_map(registry, query, kind="auto")`` returns a ``BodyMap``:

- drug / compound: indications (ChEMBL) -> organs treated; FAERS adverse event reports (openFDA)
  and FDA label warnings -> organs where harm is reported; PubChem / Haz-Map toxicity for chemicals;
  tissue expression of the protein targets (GTEx) -> where the drug's targets are made
- disease: its name, parent terms (Open Targets, MONDO via OLS) and therapeutic areas -> disease site;
  HPO phenotypes (Monarch) -> organs showing clinical features

Organs come only from these records, through ``anatomy.organs_for_text`` (medical words) or the
GTEx tissue table; every evidence item keeps its source, record and the words that matched, and
``checks`` re-verifies them. Failing sources are recorded in ``errors`` and never stop the map.
"""

from __future__ import annotations

import asyncio
import statistics
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..species.calls import SafeCaller
from .anatomy import DISEASE_WORDS, ORGANS_BY_KEY, THERAPEUTIC_AREAS, gtex_organ, organs_for_text
from .models import LEVEL_OF, BodyCheck, BodyMap, Evidence, KeyFact, OrganFinding

LEVEL_RANK = {"primary": 0, "safety": 1, "investigational": 2, "expression": 3}
GENES_FOR_EXPRESSION = 5     # top disease-associated genes looked up in GTEx
ENRICHMENT = 2.0           # a tissue counts when the gene's TPM there is >= 2x its median over all tissues
FAERS_ROWS = 100
FAERS_MIN_SHARE = 1.0      # % of all reports an organ needs before it is highlighted
EXPRESSION_TOP = 3         # tissues per target
MIN_TPM = 1.0
_WARNING_TITLE = re.compile(r"(?:^|(?<=[.)])\s+)([A-Z][A-Za-z0-9 ,/()'\-]{2,80}?):\s")
_PHASES = {"EARLY_PHASE1": "early phase 1", "PHASE1": "phase 1", "PHASE2": "phase 2", "PHASE3": "phase 3",
           "PHASE4": "phase 4", "NA": "not applicable"}
_TOX_SECTIONS = ("Adverse Effects", "Signs and Symptoms", "Health Effects", "Target Organs")
# Early-phase "indications" like these are pharmacokinetic studies in organ impairment, not treatment.
_IMPAIRMENT = re.compile(r"^(liver|kidney|hepatic|renal) (disease|insufficiency|impairment|failure)s?$|^hepatic insufficiency|^renal insufficiency")
# Whole-system words (from parent terms or therapeutic areas) are dropped when a specific organ of that
# system is already named: type 2 diabetes is a "digestive system disorder" through the pancreas only.
_SYSTEMS: dict[str, tuple[re.Pattern, set[str]]] = {
    "digestive": (re.compile(r"gastro-?intestinal|digestive"), {"esophagus", "stomach", "small_intestine", "colon", "liver", "pancreas"}),
    "cardiovascular": (re.compile(r"cardiovascular"), {"heart", "blood"}),
    "nervous": (re.compile(r"nervous system"), {"brain", "nerve"}),
    "musculoskeletal": (re.compile(r"musculoskeletal"), {"bone", "muscle"}),
    "urinary": (re.compile(r"urinary (system|tract)"), {"kidney", "bladder"}),
}


_NOT_EVENTS = ("death", "off label use", "product use issue")


def _names_treated_disease(term: str, treated: list[str]) -> bool:
    """Whether a FAERS reaction term names the disease being treated (or its course), not an adverse event.

    ``treated`` are the drug's indications that are themselves cancers. A cancer term counts as the treated
    disease when it names no organ or shares one with such an indication; a drug that treats no cancer keeps
    every cancer report (a second cancer, e.g. lymphoma on an immunosuppressant, is a safety signal).
    """
    low = term.lower()
    if low in _NOT_EVENTS or "progression" in low:
        return True
    if not treated or not DISEASE_WORDS.search(low):
        return False
    organs = {m.organ for m in organs_for_text(term)}
    for indication in treated:
        treated_organs = {m.organ for m in organs_for_text(indication)}
        if not organs or not treated_organs or organs & treated_organs:
            return True
    return False


def _drop_broad(evidence: list[Evidence]) -> tuple[list[Evidence], int]:
    """Remove whole-system matches where a specific organ of the same system has its own evidence."""
    keep: list[Evidence] = []
    dropped = 0
    for ev in evidence:
        system = next((s for s, (pattern, _) in _SYSTEMS.items() if pattern.search(ev.matched.lower())), None)
        if system is not None and ev.kind in ("disease", "phenotype"):
            organs = _SYSTEMS[system][1]
            specific = any(o.organ in organs and not _SYSTEMS[system][0].search(o.matched.lower())
                           and o.kind in ("disease", "phenotype") for o in evidence)
            if specific:
                dropped += 1
                continue
        keep.append(ev)
    return keep, dropped


def _phase_text(phase: Any) -> str:
    try:
        p = float(phase)
    except (TypeError, ValueError):
        return "phase not stated"
    if p >= 4:
        return "approved"
    return f"phase {p:g}" if p > 0 else "preclinical"


def _first_sentence(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    m = re.search(r"(?<=[.!?])\s", text)
    out = text[: m.start()] if m else text
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- kind
async def decide_kind(caller: SafeCaller, query: str) -> tuple[str, Any, Any]:
    """'drug' or 'disease' for a free-text query, from exact matches in the identifier resolvers."""
    from ..crosslink.resolver import EntityResolver

    resolver = EntityResolver(caller.registry, per_source_timeout=45)
    drug, disease = await asyncio.gather(
        resolver.resolve("drug", query, session_id=caller.session_id),
        resolver.resolve("disease", query, session_id=caller.session_id),
        return_exceptions=True,
    )
    drug = None if isinstance(drug, BaseException) else drug
    disease = None if isinstance(disease, BaseException) else disease
    n_drug = sum(1 for c in (drug.candidates if drug else []) if c.exact_match)
    n_disease = sum(1 for c in (disease.candidates if disease else []) if c.exact_match)
    if n_disease > n_drug:
        return "disease", drug, disease
    if n_drug or not (disease and disease.candidates):
        return "drug", drug, disease
    return "disease", drug, disease


# ---------------------------------------------------------------- organs from evidence
def _organs(evidence: list[Evidence]) -> tuple[list[OrganFinding], list[Evidence]]:
    """Group evidence per organ; organs with only weak FAERS signal go to ``minor``."""
    by_organ: dict[str, list[Evidence]] = defaultdict(list)
    for ev in evidence:
        by_organ[ev.organ].append(ev)
    findings: list[OrganFinding] = []
    minor: list[Evidence] = []
    for key, items in by_organ.items():
        organ = ORGANS_BY_KEY.get(key)
        if organ is None:
            continue
        kinds: dict[str, int] = defaultdict(int)
        for ev in items:
            kinds[ev.kind] += 1
        only_faers = set(kinds) == {"adverse_event"}
        faers_share = sum(ev.weight for ev in items if ev.kind == "adverse_event")
        if only_faers and faers_share < FAERS_MIN_SHARE:
            minor.extend(items)
            continue
        level = min((LEVEL_OF[k] for k in kinds), key=LEVEL_RANK.__getitem__)
        score = 0.0
        for kind in kinds:  # each kind counts up to a cap, so one noisy source cannot dominate
            score += min(6.0, sum(ev.weight for ev in items if ev.kind == kind))
        items.sort(key=lambda ev: (LEVEL_RANK[LEVEL_OF[ev.kind]], -ev.weight))
        findings.append(OrganFinding(key=key, en=organ.en, level=level, score=round(score, 2),
                                     summary=_summary(items), kinds=dict(kinds), evidence=items))
    findings.sort(key=lambda f: (LEVEL_RANK[f.level], -f.score, f.key))
    return findings, minor


def _summary(items: list[Evidence]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for ev in items:
        if ev.kind in seen:
            continue
        seen.add(ev.kind)
        if ev.kind == "treats":
            parts.append("Treats " + ev.text)
        elif ev.kind == "studied":
            n = sum(1 for e in items if e.kind == "studied")
            parts.append("In trials for " + ev.text + (f" (+{n - 1} more)" if n > 1 else ""))
        elif ev.kind == "disease":
            parts.append(ev.text)
        elif ev.kind == "phenotype":
            n = sum(1 for e in items if e.kind == "phenotype")
            parts.append(ev.text + (f" (+{n - 1} more)" if n > 1 else ""))
        elif ev.kind == "adverse_event":
            terms = [e for e in items if e.kind == "adverse_event"]
            mentions = int(sum(e.value or 0 for e in terms))
            top = ev.text.split(":")[0].lower()
            parts.append(f"FAERS: {top}" + (f" +{len(terms) - 1} terms" if len(terms) > 1 else "") + f", {mentions} mentions")
        elif ev.kind == "label":
            parts.append(ev.text)
        elif ev.kind == "toxicity":
            parts.append(f"Toxicity: {ev.matched}")
        elif ev.kind == "expression":  # "KRAS (target) 24.5 TPM in Nerve Tibial"
            parts.append(ev.text.split(",")[0].replace(" (target)", "").replace(" (disease gene)", ""))
        if len(parts) == 2:
            break
    return " · ".join(parts)


def _expression(symbol: str, result: Any, raw: dict[str, Any], role: str) -> tuple[list[Evidence], bool]:
    """Tissues where a gene is enriched (>= ENRICHMENT x its median TPM over all tissues), top organs first.

    Returns the evidence and whether the gene is expressed broadly (no enriched tissue)."""
    tissues = (result.data or {}).get("tissues") if result else None
    if not tissues:
        return [], False
    gencode = (result.data or {}).get("gencode_id")
    for t in tissues:
        raw.setdefault("gtex", {})[(gencode, t.get("tissue"))] = t.get("median_tpm")
    values = sorted(float(t.get("median_tpm") or 0) for t in tissues)
    median = statistics.median(values) if values else 0.0
    out: list[Evidence] = []
    seen: set[str] = set()
    for t in tissues:  # GTEx returns them highest first
        tpm = float(t.get("median_tpm") or 0)
        organ = gtex_organ(str(t.get("tissue")))
        if organ is None or organ in seen or tpm < MIN_TPM or tpm < ENRICHMENT * max(median, 0.01):
            continue
        seen.add(organ)
        fold = tpm / median if median > 0 else float("inf")
        out.append(Evidence(kind="expression", organ=organ,
                            text=f"{symbol} ({role}) {tpm:.1f} TPM in {str(t['tissue']).replace('_', ' ')}, "
                                 f"{fold:.1f}x its median across {len(tissues)} tissues",
                            matched=str(t["tissue"]), value=t.get("median_tpm"), unit="TPM",
                            weight=1.0 if not out else 0.6, source="gtex", record_id=gencode,
                            url=f"https://gtexportal.org/home/gene/{symbol}"))
        if len(out) >= EXPRESSION_TOP:
            break
    return out, not out


# ---------------------------------------------------------------- checks
def _checks(evidence: list[Evidence], findings: list[OrganFinding], raw: dict[str, Any]) -> list[BodyCheck]:
    checks: list[BodyCheck] = []
    bare = [f.key for f in findings if not any(ev.source and (ev.record_id or ev.url or ev.value is not None) for ev in f.evidence)]
    checks.append(BodyCheck(name="Every highlighted organ has database evidence", ok=not bare,
                            detail=f"{len(findings)} organs" + (f"; without a record: {', '.join(bare)}" if bare else "")))
    unmatched = []
    for ev in evidence:
        if ev.kind == "expression":
            ok = gtex_organ(ev.matched) == ev.organ
        elif ev.text.startswith("Therapeutic area: "):
            ok = ev.organ in THERAPEUTIC_AREAS.get(ev.matched.lower(), ())
        else:
            ok = ev.matched.lower() in ev.text.lower() and any(m.organ == ev.organ for m in organs_for_text(ev.matched))
        if not ok:
            unmatched.append(f"{ev.organ}: '{ev.matched}' in '{ev.text[:60]}'")
    checks.append(BodyCheck(name="Every organ assignment comes from the record's own words or GTEx tissue", ok=not unmatched,
                            detail="; ".join(unmatched[:5]) or f"{len(evidence)} evidence items re-checked"))
    total = raw.get("faers_total")
    if total:
        bad = [ev.matched for ev in evidence if ev.kind == "adverse_event"
               and (ev.value is None or ev.value > total or abs(ev.weight - 100 * ev.value / total) > 0.01)]
        checks.append(BodyCheck(name="FAERS counts and shares match the reports", ok=not bad,
                                detail="; ".join(bad[:5]) or f"{total} reports in total"))
    tables = raw.get("gtex", {})
    if tables:
        bad = [f"{ev.text}" for ev in evidence if ev.kind == "expression"
               and tables.get((ev.record_id, ev.matched)) != ev.value]
        checks.append(BodyCheck(name="Expression values are GTEx's own numbers", ok=not bad,
                                detail="; ".join(bad[:5]) or f"{sum(1 for e in evidence if e.kind == 'expression')} values"))
    wrong = [f.key for f in findings if f.level != min((LEVEL_OF[k] for k in f.kinds), key=LEVEL_RANK.__getitem__)]
    checks.append(BodyCheck(name="Organ colours follow their strongest evidence", ok=not wrong,
                            detail=", ".join(wrong) or "ok"))
    return checks


# ---------------------------------------------------------------- drug / compound
async def _drug_map(caller: SafeCaller, query: str, map_id: str) -> BodyMap:
    from ..species.compare import _compound, _target_details, _targets_from_activities, _targets_from_mechanisms

    info, molecule, _synonyms, _records = await _compound(caller, query)
    chembl_id = info.identifiers.get("chembl")
    name = info.name or query
    picked = await _targets_from_mechanisms(caller, chembl_id) if chembl_id else []
    if not picked and chembl_id:
        picked = await _targets_from_activities(caller, [], chembl_id)
    contexts, _ = await _target_details(caller, picked)
    symbols = [c.info.gene_symbol for c in contexts if c.info.gene_symbol]
    cid = info.identifiers.get("pubchem_cid")

    async def nothing() -> None:
        return None

    calls = {
        "mechanisms": caller.call("chembl", "get_mechanisms", {"molecule_chembl_id": chembl_id, "limit": 50}) if chembl_id else nothing(),
        "indications": caller.call("chembl", "get_drug_indications", {"molecule_chembl_id": chembl_id, "limit": 100}) if chembl_id else nothing(),
        "faers": caller.call("openfda", "adverse_event_counts", {"drug": name, "limit": FAERS_ROWS}),
        "labels": caller.call("openfda", "search_drug_labels", {"query": name, "limit": 1}),
        "trials": caller.call("clinicaltrials", "count_studies", {"intervention": name, "group_by": "phase"}),
        "toxicity": caller.call("pubchem", "compound_annotations", {"cid": int(cid), "heading": "Toxicity", "max_items": 100,
                                                                   "max_chars": 600}) if cid and str(cid).isdigit() else nothing(),
    }
    for symbol in symbols:
        calls[f"gtex:{symbol}"] = caller.call("gtex", "median_expression", {"gene": symbol}, label=f"gtex.median_expression:{symbol}")
    results = dict(zip(calls, await asyncio.gather(*calls.values())))

    evidence: list[Evidence] = []
    raw: dict[str, Any] = {"gtex": {}}
    facts: list[KeyFact] = []
    attrs = molecule.attributes if molecule is not None else {}

    # indications -> organs treated
    indications = results["indications"].records if results["indications"] else []
    impairment_studies = 0
    treated_cancers: list[str] = []
    for rec in indications:
        a = rec.attributes
        term = a.get("efo_term") or rec.title or ""
        phase = a.get("max_phase_for_indication")
        phase_text = _phase_text(phase)
        if _IMPAIRMENT.search(term.lower()) and (phase is None or float(phase) < 2):
            impairment_studies += 1
            continue
        mesh = a.get("mesh_heading") or ""
        treated_cancers += [t for t in (term, mesh) if t and DISEASE_WORDS.search(t.lower())]
        matches = organs_for_text(term)
        if not matches and mesh:  # the MeSH heading names the organ: show it in the evidence
            matches = organs_for_text(mesh)
            term = f"{term} [MeSH: {mesh}]" if mesh.lower() != term.lower() else term
        approved = phase_text == "approved"
        try:
            weight = 5.0 if approved else {3: 3.0, 2: 2.0}.get(int(float(phase)), 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        for m in matches:
            evidence.append(Evidence(kind="treats" if approved else "studied", organ=m.organ, text=f"{term} ({phase_text})",
                                     matched=m.words, value=float(phase) if phase is not None else None, unit="max phase",
                                     weight=weight, source="chembl", record_id=rec.identifiers.get("mesh") or rec.id, url=rec.url))

    # FAERS -> organs where adverse events are reported
    faers = results["faers"]
    total = int(((faers.data or {}).get("total_reports") or 0)) if faers else 0
    raw["faers_total"] = total
    skipped_disease = 0
    top_reactions: list[str] = []
    if faers and total:
        for row in (faers.data or {}).get("rows") or []:
            term = str(row.get("term") or "")
            count = int(row.get("count") or 0)
            if not term or not count:
                continue
            if _names_treated_disease(term, treated_cancers):
                skipped_disease += 1
                continue
            if len(top_reactions) < 6:
                top_reactions.append(f"{term.capitalize()} {count}")
            share = 100 * count / total
            for m in organs_for_text(term):
                evidence.append(Evidence(kind="adverse_event", organ=m.organ,
                                         text=f"{term.capitalize()}: {count} of {total} reports ({share:.1f}%)",
                                         matched=m.words, value=float(count), unit="reports", weight=round(share, 4),
                                         source="openfda", record_id=f"FAERS:{term}",
                                         url="https://open.fda.gov/data/faers/"))
    # FDA label warnings
    labels = results["labels"].records if results["labels"] else []
    boxed = None
    if labels:
        set_id = labels[0].identifiers.get("spl_set_id") or labels[0].id
        full = await caller.call("openfda", "get_drug_label", {"value": set_id}, label="openfda.get_drug_label")
        la = full.records[0].attributes if full and full.records else labels[0].attributes
        url = f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"
        boxed = (la.get("boxed_warning") or "").strip() or None
        titles: list[str] = []
        warnings = la.get("warnings_and_cautions") or la.get("warnings") or la.get("warnings_excerpt") or ""
        for m in _WARNING_TITLE.finditer(warnings[:3000]):
            title = m.group(1).strip()
            if title not in titles and not title.lower().startswith(("monitor", "advise", "if ", "see ")):
                titles.append(title)
        for title in titles[:12]:
            for tm in organs_for_text(title):
                evidence.append(Evidence(kind="label", organ=tm.organ, text=f"Label warning: {title}", matched=tm.words,
                                         weight=3.0, source="openfda", record_id=set_id, url=url))
        if boxed:
            sentence = _first_sentence(boxed, 300)
            for tm in organs_for_text(sentence):
                evidence.append(Evidence(kind="label", organ=tm.organ, text=f"Boxed warning: {sentence}",
                                         matched=tm.words, weight=4.0, source="openfda", record_id=set_id, url=url))
        if titles:
            facts.append(KeyFact(label="Label warnings", value="; ".join(titles[:8]), source="openfda", url=url))
    # PubChem / Haz-Map toxicity (chemicals)
    tox = results["toxicity"]
    if tox and info.kind != "drug":
        for ann in (tox.data or {}).get("annotations") or []:
            section = str(ann.get("section") or "")
            if not any(s in section for s in _TOX_SECTIONS):
                continue
            text = re.sub(r"\s+", " ", str(ann.get("text") or "")).strip()[:300]
            for m in organs_for_text(text):
                evidence.append(Evidence(kind="toxicity", organ=m.organ, text=f"{section.split(' > ')[-1]}: {text}",
                                         matched=m.words, weight=2.0, source="pubchem", record_id=f"CID{cid}",
                                         url=ann.get("url") or f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Toxicity"))
    # target expression (GTEx)
    broad: list[str] = []
    for symbol in symbols:
        items, is_broad = _expression(symbol, results.get(f"gtex:{symbol}"), raw, "target")
        evidence += items
        if is_broad:
            broad.append(symbol)

    findings, minor = _organs(evidence)

    # key facts
    kind_label = "drug" if info.kind == "drug" else "chemical compound"
    facts.insert(0, KeyFact(label="Type",
                            value=", ".join(filter(None, [kind_label, attrs.get("molecule_type")])), source="chembl" if molecule else None,
                            url=info.url))
    if info.max_phase is not None:
        phase = "approved (phase 4)" if info.max_phase >= 4 else f"phase {info.max_phase:g}"
        first = attrs.get("first_approval")
        facts.insert(1, KeyFact(label="Highest development phase",
                                value=phase + (f", first approved {first}" if first else ""), source="chembl", url=info.url))
    mech = results["mechanisms"].records if results["mechanisms"] else []
    moas = list(dict.fromkeys(r.attributes.get("mechanism_of_action") for r in mech if r.attributes.get("mechanism_of_action")))
    if moas:
        facts.append(KeyFact(label="Mechanism of action", value="; ".join(moas[:3]), source="chembl"))
    if contexts:
        facts.append(KeyFact(label="Human targets", source="chembl",
                             value="; ".join(f"{c.info.gene_symbol}" + (f" ({c.info.action.lower()})" if c.info.action else "")
                                             for c in contexts)))
    if indications:
        ranked_ind = sorted(indications, key=lambda r: -(r.attributes.get("max_phase_for_indication") or 0))
        facts.append(KeyFact(label="Indications", source="chembl",
                             value="; ".join(f"{r.attributes.get('efo_term') or r.title} ({_phase_text(r.attributes.get('max_phase_for_indication'))})"
                                             for r in ranked_ind[:6]) + (f"; +{len(indications) - 6} more" if len(indications) > 6 else "")))
    trials = results["trials"]
    if trials and trials.data:
        breakdown = ", ".join(f"{_PHASES.get(b.get('value'), b.get('value'))}: {b.get('count')}" for b in trials.data.get("breakdown") or [])
        facts.append(KeyFact(label="Clinical trials", source="clinicaltrials",
                             value=f"{trials.data.get('total')} studies" + (f" ({breakdown})" if breakdown else ""),
                             url=f"https://clinicaltrials.gov/search?intr={name}"))
    if total:
        facts.append(KeyFact(label="FAERS adverse event reports", source="openfda",
                             value=f"{total} reports; most reported: " + ", ".join(top_reactions[:5])
                             + " (spontaneous reports, not proof of causation)"))
    if boxed:
        facts.append(KeyFact(label="Boxed warning", value=_first_sentence(boxed, 300), source="openfda"))
    formula = attrs.get("full_molformula") or attrs.get("molecular_formula") or attrs.get("formula")
    weight = attrs.get("full_mwt") or attrs.get("mw") or attrs.get("molecular_weight")
    if formula or weight:
        facts.append(KeyFact(label="Formula / molecular weight",
                             value=" / ".join(str(x) for x in (formula, weight) if x), source="chembl" if molecule else "pubchem"))

    notes = []
    if impairment_studies:
        notes.append(f"{impairment_studies} early-phase 'indications' such as liver or kidney disease are pharmacokinetic studies "
                     "in organ impairment and were not counted as organs the drug treats")
    if skipped_disease:
        notes.append(f"{skipped_disease} FAERS terms naming the treated disease itself (a cancer it treats, progression, death ...) "
                     "were not counted as adverse events")
    if total:
        notes.append("FAERS reports are spontaneous; they do not establish that the drug caused the event")
    if symbols:
        notes.append("Target expression shows where the drug's protein targets are made (GTEx median TPM, tissues at "
                     f">= {ENRICHMENT:g}x the target's median), not where the drug has been shown to act")
    if broad:
        notes.append(f"Expressed broadly, no enriched tissue (not drawn): {', '.join(broad)}")
    return BodyMap(
        map_id=map_id, query=query, kind="drug" if info.kind == "drug" else "compound", name=name,
        identifiers=info.identifiers, url=info.url, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        organs=findings, minor=minor, facts=facts, targets=symbols, sources_used=sorted(caller.sources_used),
        errors=dict(caller.errors), notes=notes, checks=_checks(evidence, findings, raw),
    )


# ---------------------------------------------------------------- disease
async def _disease_map(caller: SafeCaller, query: str, resolved: Any, map_id: str) -> BodyMap:
    if resolved is None:
        from ..crosslink.resolver import EntityResolver

        try:
            resolved = await EntityResolver(caller.registry, per_source_timeout=45).resolve("disease", query, session_id=caller.session_id)
        except Exception as exc:  # noqa: BLE001
            caller.errors["resolver"] = str(exc)[:300]
    ids = dict(resolved.identifiers) if resolved is not None else {}
    mondo = ids.get("mondo")
    ot_key = (mondo.replace(":", "_") if mondo else None) or ids.get("efo") or query
    ot = await caller.call("opentargets", "get_disease", {"disease": ot_key, "max_drugs": 30}, timeout=60)
    rec = ot.records[0] if ot and ot.records else None
    if rec is not None:
        for k, v in rec.identifiers.items():
            ids.setdefault(k, v)
        mondo = mondo or ids.get("mondo")
    name = rec.title if rec is not None and rec.title else query
    a = rec.attributes if rec is not None else {}

    async def nothing() -> None:
        return None

    calls = {
        "ancestors": caller.call("ols", "term_hierarchy", {"term_id": mondo, "direction": "ancestors"}) if mondo else nothing(),
        "phenotypes": caller.call("monarch", "associations", {"entity_id": mondo, "category": "disease_phenotype", "limit": 100})
        if mondo else nothing(),
        "genes": caller.call("opentargets", "disease_associated_targets", {"disease": ot_key, "limit": 10}),
        "trials": caller.call("clinicaltrials", "count_studies", {"condition": name, "group_by": "phase"}),
    }
    results = dict(zip(calls, await asyncio.gather(*calls.values())))
    evidence: list[Evidence] = []
    url = rec.url if rec is not None else None
    for i, text in enumerate([name] + [s for s in (a.get("synonyms") or [])[:10] if isinstance(s, str)]):
        for m in organs_for_text(text):
            evidence.append(Evidence(kind="disease", organ=m.organ, text=f"Disease: {text}" if i == 0 else f"Also called: {text}",
                                     matched=m.words, weight=5.0 if i == 0 else 3.0, source="opentargets",
                                     record_id=mondo or (rec.id if rec else None), url=url))
    parents = [(p.get("name"), p.get("id")) for p in a.get("parents") or [] if isinstance(p, dict)]
    ancestors = results["ancestors"].records if results["ancestors"] else []
    parents += [(r.title, r.identifiers.get("mondo") or r.id) for r in ancestors]
    seen: set[str] = set()
    for label, pid in parents:
        if not label or label in seen:
            continue
        seen.add(label)
        for m in organs_for_text(label):
            evidence.append(Evidence(kind="disease", organ=m.organ, text=f"Parent term: {label} ({pid})", matched=m.words, weight=3.0,
                                     source="ols" if any(label == r.title for r in ancestors) else "opentargets",
                                     record_id=str(pid) if pid else None))
    for ta in a.get("therapeutic_areas") or []:
        for organ in THERAPEUTIC_AREAS.get(str(ta).lower(), ()):
            evidence.append(Evidence(kind="disease", organ=organ, text=f"Therapeutic area: {ta}", matched=str(ta), weight=2.0,
                                     source="opentargets", record_id=mondo, url=url))
    phenotypes = results["phenotypes"].records if results["phenotypes"] else []
    pheno_labels: list[str] = []
    for prec in phenotypes:
        pa = prec.attributes
        label = pa.get("object_label") if pa.get("subject") == mondo else None
        hp = pa.get("object")
        if not label or not str(hp or "").startswith("HP:") or label in pheno_labels:
            continue
        pheno_labels.append(label)
        for m in organs_for_text(label):
            evidence.append(Evidence(kind="phenotype", organ=m.organ, text=f"Phenotype: {label} ({hp})", matched=m.words,
                                     weight=2.0, source="monarch", record_id=hp,
                                     url=f"https://monarchinitiative.org/{hp}"))
    # where the disease's most associated genes are made (GTEx), as supporting context
    genes = results["genes"].records if results["genes"] else []
    gene_symbols = [g.identifiers.get("gene_symbol") or g.title for g in genes[:GENES_FOR_EXPRESSION]]
    gene_symbols = [s for s in gene_symbols if s]
    raw: dict[str, Any] = {}
    broad: list[str] = []
    gtex = await asyncio.gather(*(caller.call("gtex", "median_expression", {"gene": s}, label=f"gtex.median_expression:{s}")
                                  for s in gene_symbols))
    for symbol, result in zip(gene_symbols, gtex):
        items, is_broad = _expression(symbol, result, raw, "disease gene")
        evidence += items[:2]
        if is_broad:
            broad.append(symbol)
    evidence, dropped = _drop_broad(evidence)
    findings, minor = _organs(evidence)

    facts: list[KeyFact] = []
    if rec is not None and rec.summary:
        facts.append(KeyFact(label="Definition", value=_first_sentence(rec.summary, 400), source="opentargets", url=url))
    shown_ids = {k: ids[k] for k in ("mondo", "efo", "omim", "orphanet", "mesh", "icd10", "doid") if ids.get(k)}
    if shown_ids:
        facts.append(KeyFact(label="Identifiers", value=", ".join(f"{k.upper()} {v}" for k, v in shown_ids.items())))
    if a.get("therapeutic_areas"):
        facts.append(KeyFact(label="Therapeutic areas", value="; ".join(a["therapeutic_areas"]), source="opentargets"))
    drugs = [d for d in a.get("drugs_and_candidates") or [] if isinstance(d, dict)]
    if drugs:
        approved = [d for d in drugs if str(d.get("max_stage") or "").upper() in ("APPROVAL", "4", "PHASE_4")]
        listed = (approved or drugs)[:8]
        facts.append(KeyFact(label="Drugs for this disease" + (" (approved)" if approved else " (clinical candidates)"),
                             value="; ".join(f"{d.get('name')}" + (f" ({str(d.get('max_stage')).lower()})" if d.get("max_stage") else "")
                                             for d in listed)
                             + (f"; {a.get('n_drugs_and_candidates')} drugs and candidates in total" if a.get("n_drugs_and_candidates") else ""),
                             source="opentargets", url=url))
    genes = results["genes"].records if results["genes"] else []
    if genes:
        facts.append(KeyFact(label="Most associated genes", source="opentargets",
                             value="; ".join(f"{g.identifiers.get('gene_symbol') or g.title}"
                                             + (f" ({float(g.attributes.get('score')):.2f})" if g.attributes.get("score") is not None else "")
                                             for g in genes[:8])))
    trials = results["trials"]
    if trials and trials.data:
        breakdown = ", ".join(f"{_PHASES.get(b.get('value'), b.get('value'))}: {b.get('count')}" for b in trials.data.get("breakdown") or [])
        facts.append(KeyFact(label="Clinical trials", source="clinicaltrials",
                             value=f"{trials.data.get('total')} studies" + (f" ({breakdown})" if breakdown else "")))
    if pheno_labels:
        facts.append(KeyFact(label="Clinical features (HPO)", source="monarch",
                             value=f"{len(pheno_labels)} phenotypes: " + "; ".join(pheno_labels[:8])
                             + (f"; +{len(pheno_labels) - 8} more" if len(pheno_labels) > 8 else "")))
    notes = ["Disease sites come from the disease's name, its parent terms (MONDO / Open Targets) and therapeutic areas; "
             "clinical features from HPO phenotype annotations"]
    if dropped:
        notes.append(f"{dropped} whole-system matches (e.g. 'digestive system disorder') were not drawn because a specific "
                     "organ of that system is already named")
    if gene_symbols:
        notes.append(f"Blue organs: tissues where the {len(gene_symbols)} most associated genes (Open Targets) are made at "
                     f">= {ENRICHMENT:g}x their median (GTEx); supporting context, not disease sites")
    if broad:
        notes.append(f"Expressed broadly, no enriched tissue (not drawn): {', '.join(broad)}")
    return BodyMap(
        map_id=map_id, query=query, kind="disease", name=name, identifiers=shown_ids or ids, url=url,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), organs=findings, minor=minor, facts=facts,
        sources_used=sorted(caller.sources_used), errors=dict(caller.errors), notes=notes,
        checks=_checks(evidence, findings, raw),
    )


async def build_body_map(registry: Any, query: str, kind: str = "auto", *, session_id: str | None = None) -> BodyMap:
    query = query.strip()
    caller = SafeCaller(registry, session_id)
    map_id = uuid.uuid4().hex
    disease_res = None
    if kind == "auto":
        kind, _, disease_res = await decide_kind(caller, query)
    if kind == "disease":
        return await _disease_map(caller, query, disease_res, map_id)
    return await _drug_map(caller, query, map_id)
