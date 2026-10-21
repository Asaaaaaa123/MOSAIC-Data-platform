"""Canonical identifier keys and normalizers.

Cross-database linking works by putting every identifier a source returns into
``Record.identifiers`` under one of the canonical keys below, normalized with
:func:`normalize_identifiers`. Two records from different databases that share a
normalized identifier (same DOI, same InChIKey, same NCT number, ...) describe
the same entity.

Use these keys exactly. If a source exposes an identifier type that is not
listed, use a short lowercase snake_case key and mention it in the connector
docstring.
"""

from __future__ import annotations

import re
from typing import Any, Callable

IDENTIFIER_KEYS: dict[str, str] = {
    # --- publications ---
    "doi": "Digital Object Identifier, lowercase, no https://doi.org/ prefix (articles AND datasets)",
    "pmid": "PubMed ID (digits)",
    "pmcid": "PubMed Central ID (PMC + digits)",
    "arxiv": "arXiv ID without prefix or version (2204.01289, hep-th/9901001)",
    "s2": "Semantic Scholar paperId",
    "openalex": "OpenAlex ID (W..., A..., S..., I..., C...)",
    "mag": "Microsoft Academic Graph ID (digits; shared by OpenAlex and Semantic Scholar)",
    "scopus_eid": "Scopus EID (2-s2.0-...)",
    "wos_uid": "Web of Science UID (WOS:...)",
    "issn": "ISSN",
    "isbn": "ISBN",
    "orcid": "ORCID iD (0000-0000-0000-0000)",
    "ror": "ROR institution ID",
    # --- clinical ---
    "nct": "ClinicalTrials.gov NCT number (NCT########)",
    "euctr": "EU Clinical Trials Register number",
    # --- drugs / chemistry ---
    "chembl": "ChEMBL ID (CHEMBL...) for molecules, targets, assays",
    "pubchem_cid": "PubChem compound ID (digits)",
    "pubchem_sid": "PubChem substance ID (digits)",
    "inchikey": "Standard InChIKey (uppercase)",
    "smiles": "Canonical SMILES",
    "cas": "CAS registry number",
    "unii": "FDA UNII",
    "drugcentral": "DrugCentral struct_id",
    "rxcui": "RxNorm concept ID",
    "drugbank": "DrugBank ID (DB#####)",
    "atc": "WHO ATC code",
    "ndc": "National Drug Code",
    "spl_set_id": "FDA SPL set id",
    "fda_application": "FDA application number (NDA/BLA/ANDA...)",
    "pharmgkb": "PharmGKB / ClinPGx accession (PA...)",
    "bindingdb": "BindingDB monomer/reactant ID",
    # --- genes / proteins / structures ---
    "gene_symbol": "HGNC-approved gene symbol (uppercase for human)",
    "hgnc": "HGNC ID (HGNC:####)",
    "entrez_gene": "NCBI Gene ID (digits)",
    "ensembl_gene": "Ensembl gene ID (ENSG...)",
    "ensembl_transcript": "Ensembl transcript ID (ENST...)",
    "uniprot": "UniProtKB accession",
    "refseq": "RefSeq accession",
    "pdb": "PDB entry ID (uppercase, 4 chars)",
    # --- diseases / phenotypes / ontologies ---
    "mesh": "MeSH descriptor ID (D######)",
    "efo": "EFO term (EFO_#######)",
    "mondo": "MONDO term (MONDO:#######)",
    "hp": "Human Phenotype Ontology term (HP:#######)",
    "doid": "Disease Ontology ID (DOID:####)",
    "omim": "OMIM number",
    "orphanet": "Orphanet ID",
    "umls": "UMLS CUI",
    "go": "Gene Ontology term (GO:#######)",
    "reactome": "Reactome stable ID (R-HSA-...)",
    "chebi": "ChEBI ID (CHEBI:#####)",
    "kegg_compound": "KEGG compound ID (C#####)",
    # --- variants / genetics ---
    "rsid": "dbSNP rsID (rs...)",
    "clinvar": "ClinVar variation ID",
    "hgvs": "HGVS expression",
    "gnomad_variant": "gnomAD variant ID (chrom-pos-ref-alt)",
    "gwas_study": "GWAS Catalog study accession (GCST...)",
    "pgs": "PGS Catalog score ID (PGS######)",
    "civic": "CIViC entity ID",
    # --- omics / datasets ---
    "geo": "GEO accession (GSE/GDS/GSM/GPL...)",
    "sra": "SRA accession (SRR/SRX/SRP/SRS...)",
    "bioproject": "BioProject accession (PRJNA...)",
    "biosample": "BioSample accession (SAMN...)",
    "dbgap": "dbGaP study accession (phs######, version suffix stripped)",
    "gdc_project": "NCI GDC project ID (e.g. TCGA-BRCA)",
    "gdc_file": "NCI GDC file UUID",
    "zenodo": "Zenodo record ID",
    "figshare": "Figshare article ID",
    "dryad": "Dryad dataset DOI or ID",
    "dataverse": "Dataverse persistent ID",
    "osf": "OSF node GUID",
    "synapse": "Synapse ID (syn...)",
    "dandi": "DANDI dandiset ID",
    "openneuro": "OpenNeuro dataset ID (ds######)",
    "metabolomics_workbench": "Metabolomics Workbench study ID (ST######)",
    "re3data": "re3data repository registry DOI",
    "cbioportal": "cBioPortal study ID (e.g. skcm_tcga_gdc)",
    "ndex": "NDEx network UUID",
    "hubmap": "HuBMAP dataset ID (HBM...)",
    "physionet": "PhysioNet project slug",
    "tcia": "TCIA collection short title",
    # --- terminologies / model organisms ---
    "ncit": "NCI Thesaurus code (C#####)",
    "kegg_drug": "KEGG drug ID (D#####)",
    # Model-organism ids keep their CURIE prefix (as Alliance and Monarch emit them); bare ids are prefixed.
    "mgi": "Mouse Genome Informatics ID (MGI:#####)",
    "rgd": "Rat Genome Database ID (RGD:#####)",
    "zfin": "ZFIN gene ID (ZFIN:ZDB-GENE-...)",
    "xenbase": "Xenbase gene ID (Xenbase:XB-GENE-...)",
    "flybase": "FlyBase gene ID (FB:FBgn...)",
    "wormbase": "WormBase gene ID (WB:WBGene...)",
    "sgd": "Saccharomyces Genome Database ID (SGD:S000...)",
    "panther": "PANTHER family ID",
}

_DOI_PREFIX = re.compile(r"^(?:(?:https?://)?(?:dx\.|www\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(value: str) -> str:
    return _DOI_PREFIX.sub("", value.strip()).strip().lower()


def _digits(value: str) -> str:
    match = re.search(r"\d+", value)
    return match.group(0) if match else value.strip()


def normalize_pmcid(value: str) -> str:
    digits = _digits(value)
    return f"PMC{digits}" if digits.isdigit() else value.strip()


def normalize_nct(value: str) -> str:
    return value.strip().upper()


def _upper(value: str) -> str:
    return value.strip().upper()


def _strip(value: str) -> str:
    return value.strip()


def normalize_arxiv(value: str) -> str:
    """``arXiv:2204.01289v2`` / ``https://arxiv.org/abs/2204.01289`` -> ``2204.01289``.

    Zenodo stores arXiv alternate ids as ``arXiv:<id>`` while Semantic Scholar reports the bare id.
    """
    text = re.sub(r"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/", "", value.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    return re.sub(r"v\d+$", "", text)


_ENSEMBL_ID = re.compile(r"^ENS([A-Z]*?)([GTPER])\d{6,}$")


def _ensembl(value: str, kind: str) -> str:
    """``ENSEMBL:ENSG00000207702.3`` -> ``ENSG00000207702``; an id of the wrong type (a transcript
    or protein under ``ensembl_gene``) is dropped rather than linked to the wrong entity."""
    text = re.sub(r"^ENSEMBL:\s*", "", value.strip().upper()).split(".")[0]
    match = _ENSEMBL_ID.match(text)
    if match and match.group(2) != kind:
        return ""
    return text


def _mod_curie(prefix: str, bare: str) -> Callable[[str], str]:
    """Model-organism ids: ``ZDB-GENE-1`` / ``zfin:ZDB-GENE-1`` -> ``ZFIN:ZDB-GENE-1``."""

    def norm(value: str) -> str:
        text = value.strip()
        head, sep, local = text.partition(":")
        if sep and head.upper() == prefix.upper():
            return f"{prefix}:{local.strip()}"
        if re.match(bare, text, flags=re.IGNORECASE):
            return f"{prefix}:{text}"
        return text

    return norm


def normalize_orcid(value: str) -> str:
    return re.sub(r"^https?://orcid\.org/", "", value.strip(), flags=re.IGNORECASE)


def _curie(value: str, prefix: str, sep: str) -> str:
    """``MONDO_0009061`` / ``mondo:0009061`` / IRI -> ``MONDO:0009061`` (with ``sep``)."""
    text = value.strip().rsplit("/", 1)[-1]
    match = re.match(rf"^{prefix}[:_]?(.+)$", text, flags=re.IGNORECASE)
    return f"{prefix}{sep}{match.group(1)}" if match else text


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    """``Orphanet:586`` / ``ORPHA_586`` -> ``586``."""
    text = value.strip()
    for prefix in prefixes:
        match = re.match(rf"^{prefix}[:_\s]+(.+)$", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return text


_NORMALIZERS: dict[str, Callable[[str], str]] = {
    "doi": normalize_doi,
    "pmid": _digits,
    "pmcid": normalize_pmcid,
    "nct": normalize_nct,
    "chembl": _upper,
    "pubchem_cid": _digits,
    "pubchem_sid": _digits,
    "inchikey": _upper,
    "unii": _upper,
    "rxcui": _digits,
    "drugbank": _upper,
    "entrez_gene": _digits,
    "ensembl_gene": lambda v: _ensembl(v, "G"),
    "ensembl_transcript": lambda v: _ensembl(v, "T"),
    "uniprot": _upper,
    "pdb": _upper,
    "hgnc": lambda v: v.strip().upper() if v.strip().upper().startswith("HGNC:") else f"HGNC:{_digits(v)}",
    "orcid": normalize_orcid,
    "rsid": lambda v: v.strip().lower(),
    "geo": _upper,
    "dbgap": lambda v: re.sub(r"\.v\d+(?:\.p\d+)?$", "", v.strip().lower()),
    "arxiv": normalize_arxiv,
    "mgi": _mod_curie("MGI", r"^\d+$"),
    "rgd": _mod_curie("RGD", r"^\d+$"),
    "zfin": _mod_curie("ZFIN", r"^ZDB-"),
    "xenbase": _mod_curie("Xenbase", r"^XB-"),
    "flybase": _mod_curie("FB", r"^FB[a-z]{2}\d+$"),
    "wormbase": _mod_curie("WB", r"^WB[A-Za-z]+\d+$"),
    "sgd": _mod_curie("SGD", r"^S\d{9}$"),
    # Ontology CURIEs: one canonical separator per ontology so sources link.
    "mondo": lambda v: _curie(v, "MONDO", ":"),
    "hp": lambda v: _curie(v, "HP", ":"),
    "doid": lambda v: _curie(v, "DOID", ":"),
    "go": lambda v: _curie(v, "GO", ":"),
    "chebi": lambda v: _curie(v, "CHEBI", ":"),
    "efo": lambda v: _curie(v, "EFO", "_"),
    "orphanet": lambda v: _strip_prefix(v, ("orphanet", "orpha")),
    "omim": lambda v: _strip_prefix(v, ("omim", "mim")),
    "mesh": lambda v: _strip_prefix(v, ("mesh",)).upper(),
    "umls": lambda v: _strip_prefix(v, ("umls",)).upper(),
    "icd10": lambda v: _strip_prefix(v, ("icd10cm", "icd10")).upper(),
}


def normalize_identifier(key: str, value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalizer = _NORMALIZERS.get(key, _strip)
    try:
        out = normalizer(text)
    except Exception:  # never let a weird ID break a record
        out = text
    return out or None


def normalize_identifiers(identifiers: dict[str, Any] | None) -> dict[str, str]:
    """Normalize a mapping of identifiers, dropping empty values.

    Values that are lists keep only the first non-empty item; put additional
    values in ``Record.attributes`` if they matter.
    """
    out: dict[str, str] = {}
    for key, value in (identifiers or {}).items():
        if isinstance(value, (list, tuple)):
            value = next((v for v in value if v not in (None, "")), None)
        norm = normalize_identifier(key, value)
        if norm:
            out[key] = norm
    return out
