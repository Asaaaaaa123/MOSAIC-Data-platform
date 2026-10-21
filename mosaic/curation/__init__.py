"""Data curation: clean, de-duplicate, merge and evaluate records from every source.

Mosaic returns data, not just links: ``curate(records)`` turns raw multi-source
records into consolidated entities whose fields are cleaned, merged across
databases with field-level provenance, and graded for quality.
"""

from .clean import CleaningLog, clean_record, clean_text, clean_value
from .consolidate import CuratedEntity, CuratedResult, curate
from .evaluate import SOURCE_PROFILES, Evaluation, evaluate, record_flags

__all__ = [
    "SOURCE_PROFILES",
    "CleaningLog",
    "CuratedEntity",
    "CuratedResult",
    "Evaluation",
    "clean_record",
    "clean_text",
    "clean_value",
    "curate",
    "evaluate",
    "record_flags",
]
