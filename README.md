# Mosaic: Claude-powered cross-database research platform

[![tests](https://github.com/Asaaaaaa123/MOSAIC-Data-platform/actions/workflows/tests.yml/badge.svg)](https://github.com/Asaaaaaa123/MOSAIC-Data-platform/actions/workflows/tests.yml)

Mosaic lets Claude query literature, clinical trials, drug labels and safety reports, chemistry,
protein and genomics databases, and the NIH scientific data repositories in **one conversation**,
link the same entity across sources by shared identifiers, and save the extracted table as
CSV or JSON.

- **49 connectors, 220 tools**: every source has a typed, rate-limited,
  cached and audited connector that normalizes records to one model with canonical identifiers
  (DOI, PMID, PMCID, NCT, ChEMBL, PubChem CID, InChIKey, RxCUI, UNII, UniProt, Ensembl, HGNC,
  MONDO/EFO/MeSH, rsID, GEO, ...).
- **NIH repository catalog**: all 232 repositories on NIH's
  [Accessing Scientific Data](https://grants.nih.gov/policy-and-compliance/policy-topics/sharing-policies/accessing-data/scientific)
  page (223 domain-specific and 9 generalist), each marked with whether Mosaic has a live
  connector. Claude reaches the rest with Anthropic's server-side web search/fetch.
- **Cross-database extraction**: `resolve_entity` pins a gene / drug / disease / article / variant
  to its identifiers across databases. `federated_search` runs one query everywhere and clusters
  records that share an identifier. `extract_structured` turns full records into a table with per-row
  provenance, and `save_table` writes CSV + JSON.
- **Data, not links: cleaned and evaluated**: every record is cleaned (markup, placeholders,
  duplicates, dates) and graded A-D. Cross-database results are merged into one entity per real-world
  thing, each field taken from the most reliable source with its provenance, conflicts between
  sources shown, and warning flags (retracted, preprint, terminated trial, restricted access, FAERS,
  unreviewed entries...). See *Data quality* below.
- **Cross-species comparison**: a compound's targets, ortholog sequence similarity % and evidence
  (papers, doses, measurements) in 12 species versus human, as a dashboard and per-species data package.
- **Claude agent** (`claude-opus-5-5`, adaptive thinking, streaming, prompt caching, tool search over
  the connector tools, server-side refusal fallback), a web app, a JSON API and a CLI.

## Quick start

**One click:** double-click `start-mosaic.bat` (Windows) or run `./start-mosaic.sh` (macOS/Linux).
The first run creates the Python environment and a `.env` for your keys. After that it starts the
server and opens http://127.0.0.1:8000.

Manual setup:

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"     # Windows (use .venv/bin/python elsewhere)
copy .env.example .env                                  # then fill in keys (see below)
.venv\Scripts\python.exe -m mosaic serve                # http://127.0.0.1:8000
```

Only `ANTHROPIC_API_KEY` is needed for the agent. Keyless sources work without anything else.
Keyed sources show as *not configured* until their key is set, and nothing else breaks.

## Configuration (`.env`)

| Setting | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (agent, extraction) |
| `MOSAIC_MODEL` / `MOSAIC_EFFORT` | Model (default `claude-opus-5-5`) and effort (`low`…`max`, default `high`) |
| `MOSAIC_TOOL_LOADING` | `deferred` (default: connector tools found via tool search) or `eager` |
| `MOSAIC_ENABLE_WEB_TOOLS` | Let Claude use server-side `web_search` / `web_fetch` (default `true`) |
| `MOSAIC_FALLBACKS` | Server-side refusal fallback (`default`, or `off`) |
| `MOSAIC_EXTRACTION_MODEL` / `MOSAIC_EXTRACTION_EFFORT` | Model / effort for `extract_structured` (defaults: main model, `low`) |
| `MOSAIC_FIGUREGEN_PATH` | Figure Studio folder (the "graph generation" project) that draws the figures; empty = a `graph generation` folder next to Mosaic |
| `BIOPHA_CONTACT_EMAIL` | Contact email for the Crossref / OpenAlex / NCBI polite pools; required by Unpaywall |
| `NCBI_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `OPENALEX_API_KEY`, `ELSEVIER_SCOPUS_API_KEY` (+ optional `ELSEVIER_INSTTOKEN`), `SPRINGER_META_API_KEY`, `SPRINGER_OPENACCESS_API_KEY`, `OPENFDA_API_KEY`, `CLARIVATE_API_KEY` | Keyed sources |
| `DRUGCENTRAL_PG_*` | DrugCentral's public read-only PostgreSQL (defaults are DrugCentral's published credentials) |
| `BIOPHA_DATA_DIR`, `BIOPHA_AUDIT_DB`, `BIOPHA_CACHE_DB`, `BIOPHA_CACHE_TTL` | Data dir, audit log, HTTP cache (seconds; `0` disables) |
| `BIOPHA_API_HOST` / `BIOPHA_API_PORT` | Server bind (default `127.0.0.1:8000`) |

`.env` is git-ignored. Keys are sent as headers where the upstream allows it. Query-string keys
and the contact email are redacted from logs, audit rows, tool output and API errors.

## Using Mosaic

**Web app** (`mosaic serve`): *Ask Mosaic* (streaming chat with live tool-call cards, reasoning
summaries, table downloads and saved sessions), *Sources* (health checks, try any tool from a
generated form), *Cross-database search*, *NIH repositories*, *Exports*, *Audit log*.

**CLI**

```bash
mosaic sources                                   # all connectors and their status
mosaic health                                    # live health check of every source
mosaic species metformin --species mouse rat zebrafish
mosaic resolve drug imatinib                     # identifiers across PubChem, ChEMBL, RxNorm, DrugCentral, Open Targets
mosaic search "KRAS G12C" --categories clinical drugs literature
mosaic call clinicaltrials search_studies --params '{"query": "BRAF V600E melanoma", "limit": 5}'
mosaic catalog "single cell" --kind domain_specific
mosaic ask "For KRAS G12C inhibitors: approved drugs, phase 3 trials, key papers and FAERS top reactions; save a table"
mosaic exports
```

**JSON API**: `GET /api/sources`, `GET /api/tools`, `POST /api/call`, `POST /api/search`,
`POST /api/resolve`, `GET /api/catalog`, `POST /api/chat` (Server-Sent Events), `GET /api/sessions`,
`GET /api/exports/{id}.csv|.json`, `GET /api/audit/tool-calls`, `GET /api/health`.

## Sources

49 connectors and 220 tools. 55 of the 232 NIH catalog repositories are served live by a connector (column *NIH repositories*); Claude reaches the others through the catalog's access pages and server-side web tools.

### Literature, citations & scholarly metadata

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [Web of Science (Clarivate)](https://www.webofscience.com/) (`clarivate`) | key | `search_documents`, `get_document`, `citing_documents`, `cited_references`, `related_documents`, `citation_counts` | - |
| [Crossref](https://www.crossref.org/) (`crossref`) | keyless | `search_works`, `get_work` | - |
| [Europe PMC](https://europepmc.org/) (`europepmc`) | keyless | `search`, `get_article`, `get_citations`, `get_references`, `full_text`, `annotations` | - |
| [NCBI Entrez (PubMed, Gene, GEO, ClinVar, dbSNP, SRA, dbGaP ...)](https://www.ncbi.nlm.nih.gov/) (`ncbi`) | optional key | `pubmed_search`, `pubmed_fetch`, `pubmed_links`, `entrez_search`, `entrez_summary`, `entrez_link` | ClinVar; Conserved Domains Database (CDD); Database for Genotypes and Phenotypes (dbGaP); Database of human genomic Structural Variation (dbVar); GenBank; Gene Expression Omnibus (GEO); NCBI Datasets: Gene, Genome, Assembly, Orthologs, and Taxonomy Database; NIH Genetic Testing Registry (GTR); Reference Sequences (RefSeq); Sequence Read Archive (SRA); Single Nucleotide Polymorphism Database (dbSNP) |
| [OpenAlex](https://openalex.org/) (`openalex`) | optional key | `search_works`, `get_work`, `works_group_by`, `search_authors`, `get_author`, `search_institutions` | - |
| [Scopus (Elsevier)](https://www.scopus.com/) (`scopus`) | key | `search`, `get_abstract`, `serial_metrics`, `get_full_text`, `search_authors`, `sciencedirect_search` | - |
| [Semantic Scholar](https://www.semanticscholar.org/) (`semantic_scholar`) | optional key | `search_papers`, `get_paper`, `get_papers_batch`, `paper_citations`, `search_authors`, `author_papers`, `recommend_papers`, `snippet_search` | - |
| [Springer Nature](https://dev.springernature.com/) (`springer`) | key | `search`, `get_by_doi`, `search_open_access`, `get_open_access_full_text`, `count_facets` | - |
| [Unpaywall](https://unpaywall.org/) (`unpaywall`) | key | `get_oa_status`, `get_oa_status_batch` | - |

### Clinical trials, regulatory & safety

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [ClinicalTrials.gov](https://clinicaltrials.gov/) (`clinicaltrials`) | keyless | `search_studies`, `get_study`, `get_study_results`, `studies_by_identifier`, `count_studies`, `field_values` | ClinicalTrials.gov |
| [openFDA](https://open.fda.gov/) (`openfda`) | optional key | `search_drug_labels`, `get_drug_label`, `adverse_event_counts`, `search_adverse_events`, `drug_approvals`, `drug_recalls`, `ndc_lookup` | - |

### Drugs & pharmacology

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [ClinPGx (PharmGKB)](https://www.clinpgx.org/) (`clinpgx`) | keyless | `search`, `get_gene`, `get_chemical`, `clinical_annotations`, `dosing_guidelines`, `drug_labels` | ClinPGx |
| [DrugCentral](https://drugcentral.org/) (`drugcentral`) | PostgreSQL | `search_drugs`, `get_drug`, `drug_targets`, `drugs_for_target`, `drugs_for_indication`, `adverse_events`, `describe_schema`, `sql_query` | - |
| [RxNorm (NLM RxNav)](https://www.nlm.nih.gov/research/umls/rxnorm/) (`rxnorm`) | keyless | `search`, `get_concept`, `drug_classes`, `find_by_id`, `ndcs_for_rxcui` | RxNorm |

### Chemistry & bioactivity

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [BindingDB](https://www.bindingdb.org/) (`bindingdb`) | keyless | `ligands_for_target`, `ligands_for_pdb`, `targets_for_compound` | Binding Database (BindingDB) |
| [ChEMBL](https://www.ebi.ac.uk/chembl/) (`chembl`) | keyless | `search_molecules`, `get_molecule`, `search_targets`, `get_target`, `get_activities`, `get_mechanisms`, `get_drug_indications` | - |
| [PubChem](https://pubchem.ncbi.nlm.nih.gov/) (`pubchem`) | keyless | `search_compounds`, `get_compound`, `compound_xrefs`, `bioassay_summary`, `similarity_search`, `compound_annotations` | PubChem |

### Targets, proteins & structures

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [Open Targets Platform](https://platform.opentargets.org/) (`opentargets`) | keyless | `search`, `get_target`, `get_disease`, `disease_associated_targets`, `target_associated_diseases`, `get_drug`, `association_evidence` | - |
| [RCSB Protein Data Bank](https://www.rcsb.org/) (`rcsb_pdb`) | keyless | `search_structures`, `get_entry`, `structures_for_uniprot`, `structures_for_ligand` | Protein Data Bank |
| [UniProt (UniProtKB)](https://www.uniprot.org/) (`uniprot`) | keyless | `search_proteins`, `get_protein`, `map_ids` | Universal Protein Resource (UniProt) |

### Genomics, variants, expression & cancer genomics

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [cBioPortal for Cancer Genomics](https://www.cbioportal.org/) (`cbioportal`) | keyless | `search_studies`, `get_study`, `gene_alteration_frequency`, `mutations_in_gene` | cBioPortal |
| [Ensembl](https://www.ensembl.org/) (`ensembl`) | keyless | `get_gene`, `orthologs` | - |
| [CIViC (Clinical Interpretation of Variants in Cancer)](https://civicdb.org/) (`civic`) | keyless | `search`, `gene_evidence`, `evidence_for_therapy`, `molecular_profile`, `assertions` | Clinical Interpretation of Variants in Cancer (CIViC) |
| [gnomAD (Genome Aggregation Database)](https://gnomad.broadinstitute.org/) (`gnomad`) | keyless | `search`, `gene_constraint`, `get_variant`, `gene_variants` | gnomAD Genome Aggregation Database |
| [GTEx Portal (Genotype-Tissue Expression)](https://gtexportal.org/) (`gtex`) | keyless | `search_genes`, `median_expression`, `single_tissue_eqtls`, `tissues`, `top_expressed_genes` | Genotype-Tissue Expression (GTEx) |
| [GWAS Catalog (NHGRI-EBI)](https://www.ebi.ac.uk/gwas/) (`gwas_catalog`) | keyless | `search`, `associations_for_variant`, `associations_for_gene`, `associations_for_trait`, `get_study` | GWAS Catalog (NHGRI-EBI Catalog) |
| [HGNC (HUGO Gene Nomenclature Committee)](https://www.genenames.org/) (`hgnc`) | keyless | `search_genes`, `fetch_gene`, `gene_group` | HUGO Gene Nomenclature Committee (HGNC) |
| [NCI Genomic Data Commons (GDC)](https://portal.gdc.cancer.gov/) (`nci_gdc`) | keyless | `search_projects`, `get_project`, `gene_mutation_frequency`, `top_mutated_genes`, `search_files` | Genomic Data Commons (GDC) |
| [PGS Catalog](https://www.pgscatalog.org/) (`pgs_catalog`) | keyless | `search`, `get_score`, `scores_for_trait`, `get_publication` | PGS Catalog |

### Pathways, interactions, ontologies & model organisms

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [Alliance of Genome Resources](https://www.alliancegenome.org/) (`alliance`) | keyless | `search`, `get_gene`, `orthologs`, `gene_diseases`, `gene_models`, `gene_phenotypes` | Alliance of Genome Resources (AGR); Rat Genome Database (RGD); Zebrafish Information Network (ZFIN); WormBase; FlyBase: A Drosophila Genomic and Genetic Database; Saccharomyces Genome Database (SGD); Xenbase |
| [Comparative Toxicogenomics Database (CTD)](https://ctdbase.org/) (`ctd`) | keyless | `chemical_gene_interactions`, `chemical_disease_associations`, `gene_disease_associations`, `disease_chemicals` | Comparative Toxicogenomics Database (CTD) |
| [Monarch Initiative](https://monarchinitiative.org/) (`monarch`) | keyless | `search`, `get_entity`, `associations`, `phenotype_similarity_search` | Monarch Initiative; Human Phenotype Ontology (HPO) |
| [NDEx - The Network Data Exchange](https://www.ndexbio.org/) (`ndex`) | keyless | `search_networks`, `network_summary`, `query_network` | The Network Data Exchange (NDEx) |
| [Ontology Lookup Service (OLS4)](https://www.ebi.ac.uk/ols4/) (`ols`) | keyless | `search_terms`, `get_term`, `term_hierarchy`, `map_term` | Disease Ontology Knowledgebase (DO-KB) |
| [QuickGO (Gene Ontology)](https://www.ebi.ac.uk/QuickGO/) (`quickgo`) | keyless | `search_terms`, `get_terms`, `gene_annotations`, `genes_for_term` | Gene Ontology Consortium and Knowledgebase |
| [Reactome](https://reactome.org/) (`reactome`) | keyless | `search`, `get_pathway`, `pathways_for_entity`, `enrichment_analysis` | Reactome |

### Data repositories (NIH generalist & domain-specific)

| Source | Access | Tools | NIH repositories |
|---|---|---|---|
| [DANDI Archive](https://dandiarchive.org/) (`dandi`) | keyless | `search_dandisets`, `get_dandiset`, `list_assets` | Distributed Archives for Neurophysiology Data Integration (The DANDI Archive) |
| [DataCite](https://datacite.org/) (`datacite`) | keyless | `search_datasets`, `get_doi`, `datasets_for_article`, `repositories` | Mendeley Data; IEEE Dataport; Vivli |
| [Harvard Dataverse](https://dataverse.harvard.edu/) (`dataverse`) | keyless | `search`, `get_dataset`, `datasets_for_article` | Dataverse |
| [Dryad](https://datadryad.org/) (`dryad`) | keyless | `search`, `get_dataset`, `datasets_for_article` | Dryad |
| [Figshare](https://figshare.com/) (`figshare`) | keyless | `search`, `get_article`, `items_for_article` | Figshare |
| [HuBMAP Data Portal](https://portal.hubmapconsortium.org/) (`hubmap`) | keyless | `search_datasets`, `get_dataset`, `search_publications`, `get_entity`, `dataset_facets` | HuBMAP Data Portal |
| [Metabolomics Workbench](https://www.metabolomicsworkbench.org/) (`metabolomics_workbench`) | keyless | `search_studies`, `get_study`, `compound_lookup`, `refmet_lookup`, `studies_for_compound` | Metabolomics Workbench - National Metabolomics Data Repository |
| [OpenNeuro](https://openneuro.org/) (`openneuro`) | keyless | `search_datasets`, `get_dataset`, `list_files` | OpenNeuro |
| [Open Science Framework](https://osf.io/) (`osf`) | keyless | `search`, `get_node`, `list_files` | Open Science Framework |
| [PhysioNet](https://physionet.org/) (`physionet`) | keyless | `search_projects`, `get_project` | PhysioNet |
| [Synapse (Sage Bionetworks)](https://www.synapse.org/) (`synapse`) | optional key | `search`, `get_entity`, `list_children` | Synapse |
| [The Cancer Imaging Archive (TCIA)](https://www.cancerimagingarchive.net/) (`tcia`) | keyless | `search_collections`, `get_collection`, `series_for_collection`, `studies_for_collection` | The Cancer Imaging Archive (TCIA) |
| [Zenodo](https://zenodo.org/) (`zenodo`) | optional key | `search`, `get_record`, `records_for_article` | Zenodo |

## Data quality

Mosaic answers with the associated data itself; links are kept as provenance.

1. **Cleaning** (`mosaic/curation/clean.py`, applied to every tool result): HTML/JATS stripped,
   entities decoded, whitespace and control characters fixed, placeholder values (`N/A`, `unknown`,
   `not available` ...) removed, dates normalized to ISO, duplicate list values and authors removed.
   Symbols that are data (a `-` DNA strand) are kept.
2. **Consolidation** (`consolidate.py`): records from different databases that share a normalized
   identifier (or the same title, kind and year) become one entity. Each field comes from the
   reference source for that kind of entity (ChEMBL/DrugCentral for drugs, HGNC/UniProt for genes,
   ClinicalTrials.gov for trials, PubMed/Europe PMC for articles ...) with field-level provenance.
   Disagreeing identifiers are reported as conflicts; legitimately multi-valued IDs (ingredient vs
   brand RxCUI, NDCs, ATC codes, print/electronic ISSN) are not.
3. **Evaluation** (`evaluate.py`): a 0-100 score and A-D grade from source reliability tier
   (authoritative/curated, aggregated/integrative, self-deposited), corroboration across databases,
   completeness, identifier coverage and conflicts, with flags that change how to use the data:
   retracted, preprint, terminated/withdrawn trial, obsolete term, restricted access, FAERS
   (not causal), unreviewed TrEMBL entry, electronic-only GO annotation, non-human. Every grade
   carries the reasons it was computed from (hover the grade in the web app).

## Cross-species comparison

For a drug, medicine or chemical compound, Mosaic compares human with macaque, mouse, rat, dog,
pig, chicken, frog, zebrafish, fruit fly, worm, yeast and Arabidopsis:

- **targets**: the compound's human targets (ChEMBL mechanisms; for non-drug chemicals, the human
  proteins it is most active on)
- **similarity %**: sequence identity and similarity of each target's ortholog to the human protein,
  from Ensembl Compara, cross-checked against Mosaic's own global alignment (Biopython, BLOSUM62) of the
  UniProt canonical sequences of the Alliance / UniProt / UniRef50 ortholog; the method is recorded
  per value
- **evidence per species**: PubMed studies (counts with species MeSH filters, top papers classified
  clinical / in vivo / in vitro), ChEMBL potency, pharmacokinetic, toxicity and in vivo measurements
  with doses, ClinicalTrials.gov studies for human; optionally Claude extracts model, dose and effect
  from the abstracts
- a dashboard (overview graph with human at the centre, heatmap table, identity bars, measurement
  bars, dose-effect scatter, paper list with references), a card in chat answers, and a data package
  with one CSV per species

Use the *Cross-species* tab, ask in chat ("compare metformin in human, mouse and zebrafish"),
`mosaic species metformin`, or `POST /api/species/compare`.

## Search results: data, mind map, charts, body map, comparison

The *Cross-database search* view shows what a search found in five tabs:

- **Data**: the cleaned, merged, graded entities (the data itself, sources as references).
- **Mind map**: the query, the kinds of data found (with counts) and the best-graded entities of
  each with their key facts (phase, status, enrollment, journal, citations, max phase ...). Branches open and
  close; clicking an entry shows its data and can jump to its card or add it to a comparison.
- **Charts**: data by kind, records per database, quality grades, data by year, trials by phase and
  status, which database supplied which data, and (when comparing) shared versus unique data. Each value is
  counted from the returned records and keeps the entity numbers it counted (*the numbers and what each one
  counted* under each chart). The figures are drawn by **Figure Studio** (`figuregen` from the separate
  "graph generation" project, Nature house style) as SVG, PNG (300 dpi) and PDF, plus a multi-panel overview
  figure; without Figure Studio the same numbers are drawn in the browser.
- **Compare**: put up to 4 entities side by side (differences highlighted). Enter up to 3 more terms in
  *Compare with* (for example `sotorasib` vs `adagrasib`) to search each one the same way: the charts,
  the mind map and a table then show the queries side by side, with the entities they share.

- **Body map**: where the searched drug, chemical or disease acts in the human body, drawn by
  Figure Studio's body and organ illustrations (and as an interactive body on the page). Every organ comes
  from a database record, coloured by what kind of evidence it is:
  - red, *approved use / disease site*: approved indications (ChEMBL); for a disease, its name, parent
    terms (MONDO via OLS, Open Targets) and therapeutic areas, and HPO phenotypes (Monarch)
  - yellow, *adverse effects reported*: FAERS reaction counts (openFDA; terms naming the treated disease,
    death or off-label use are not counted), FDA label warnings and boxed warnings, PubChem / Haz-Map toxicity
  - purple, *in clinical trials*: indications in phase 1-3 (pharmacokinetic studies in liver or kidney
    impairment are not counted)
  - blue, *target / gene expressed*: GTEx tissues where the drug's targets (or the disease's top genes)
    are made at 2x or more their median level; genes expressed everywhere are not drawn

  On the page the body is assembled from Figure Studio's illustrations (one picture per organ, plus the
  skeleton, blood vessels and nerves as whole-body systems), each served as SVG from
  `GET /api/body/art/{name}.svg?tone=…`. It is fully interactive: scroll or pinch to zoom, drag to move,
  click or Tab + Enter on any organ to fly to it and see its evidence, double-click to zoom, keys + − 0,
  arrows, N / P (next / previous organ) and Esc; toggle evidence levels, labels, body systems, all organs,
  natural colours and female / male organs; find an organ by name; full screen.

  Next to it: key facts (phase and approval, mechanism, targets, indications, trials by phase, FAERS
  totals, boxed warning, formula; for diseases definition, drugs, top genes, trials, phenotypes), each
  organ's evidence with the exact words or GTEx tissue that placed it, and an export (organs.csv,
  organ_evidence.csv, key_facts.csv, figures, Excel, ZIP). `POST /api/body {"query": "imatinib"}`.

**Data checks**: every chart is added up again and compared with the data (entities per kind,
records per database against each database's reported count, grades, trial phases and status, years, every
mind-map item); exports also recount the written tables. The result (for example *13/13 passed*) is shown
above the tabs and written to the package README.

API: `POST /api/search` (with `"compare": [...]`) returns `visuals` and a `search_id`;
`GET /api/search/{search_id}/figures/{name}.svg|png|pdf`, `/figure_data.csv` and `/mindmap.json`.

## Data packages (exports)

Exports contain the actual data, not links. *Export data* on the search view, or asking Claude
to save or export data, writes a folder `data/exports/<name>__<id>/`:

- one CSV per kind of data (`papers.csv`, `clinical_trials.csv`, `projects_studies.csv`,
  `datasets_repositories.csv`, `drugs_compounds.csv`, `genes_proteins.csv`, ...), or per species
  (`human.csv`, `mouse.csv`, `zebrafish.csv`, `plant.csv`, ...), or per database. Each CSV has the
  columns that fit its rows.
- every search hit is first re-fetched as its source's full record (complete trial protocols,
  abstracts with MeSH terms, compound properties, dataset file lists ...)
- `data_files.csv`: every file of every dataset (name, size, format, checksum, download URL), and
  `files/`: the small open tables (CSV/TSV/TXT/JSON/Excel up to 10 MB each, 50 MB per package)
  downloaded from the repositories, previewed as sheets in the workbook
- `figures/`: the mind map, the overview figure and every chart as SVG, PNG and PDF with a drafted legend
  (Figure Studio); `figure_data.csv`: every plotted number with how it was counted and the entities it
  counted; `mindmap.json`; for compared searches a `matched_queries` column in every table
- `mosaic_data.xlsx` (one sheet per table, plus a *Figures* sheet with the pictures and a *figure_data* sheet),
  `README.txt` (including how much of the package is data values versus identifiers and links, and the data
  checks), `manifest.json` and `mosaic_data.zip`

## How it fits together

```
mosaic/
  config.py        settings from env / .env
  http.py          shared async client: per-source token buckets, retries, SQLite cache, redaction
  models.py        Record / ToolResult (normalized, context-budgeted serialization for Claude)
  ids.py           canonical identifier keys + normalizers (the basis of cross-database linking)
  connectors/      one module per source, auto-discovered (Connector subclass + @tool methods)
  registry.py      discovery, dispatch, auditing, federated search
  crosslink/       identifier clustering (merge.py) and entity resolution (resolver.py)
  curation/        cleaning, cross-database consolidation and quality evaluation
  species/         cross-species comparison engine (orthologs, alignment, per-species evidence)
  datapackage.py   multi-table exports; enrich.py full records; datafiles.py dataset files
  visuals/         mind map, charts and data checks counted from the data; Figure Studio bridge
  body/            body map: organ evidence from indications, FAERS, labels, phenotypes, GTEx
  catalog/         NIH repository catalog (nih_repositories.json) + BM25 search
  agent/           Claude agent: streaming tool loop, meta tools, extraction, exports, sessions
  api/             FastAPI app (JSON + SSE), security middleware
  web/             no-build single-page app (no CDN dependencies)
  cli.py           `mosaic` command
```

**Adding a source**: create `mosaic/connectors/<name>.py` with a `Connector` subclass and
`@tool`-decorated async methods taking one pydantic params model. Put every cross-reference in
`Record.identifiers` using the keys in `mosaic/ids.py`. It is discovered automatically; see
`mosaic/connectors/crossref.py` and `tests/connectors/test_crossref.py` for the pattern.

## Tests

```bash
.venv\Scripts\python.exe -m pytest                                   # offline (mocked HTTP and Claude)
set MOSAIC_LIVE_TESTS=1 && .venv\Scripts\python.exe -m pytest -m live   # every connector against the real APIs
```

## Notes and limits

- **Controlled-access data** (dbGaP, GDC controlled files, Vivli, many NIH repositories) needs the
  repository's own authorization. Mosaic finds and describes such studies but never retrieves
  controlled data.
- **FAERS / openFDA** reports do not establish causation; the connector says so in its results.
- **Entitlements of the keys tested here**: Scopus off-campus gives STANDARD search, META abstracts
  and serial metrics; author search, ScienceDirect search and abstracts need an institutional
  token (`ELSEVIER_INSTTOKEN`). Clarivate: Web of Science API Expanded works; WoS Starter and
  JCR are not subscribed. Springer: each key works on its own API (Meta vs Open Access), with
  basic-plan query limits. Unpaywall's search endpoint was retired upstream (DOI lookups work).
- Mosaic binds to `127.0.0.1` by default; there is no user authentication. Do not expose it publicly as is.
  Binding to a non-loopback address (`MOSAIC_BIND_HOST=0.0.0.0`) also weakens the DNS-rebinding
  protection unless `MOSAIC_ALLOWED_HOSTS` lists the host names you serve.
