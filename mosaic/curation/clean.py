"""Cleaning: make records from 48 different upstreams uniform before anyone reads them.

Conservative by design: only changes that are unambiguous improvements.
- text: HTML/JATS stripped, entities decoded, Unicode NFC, control characters
  removed, whitespace collapsed
- placeholders dropped: "", "N/A", "NA", "none", "null", "unknown", "not available",
  "not applicable", ... (symbols such as "-" are kept: a DNA strand is data)
- dates: "2024 Mar 5", "2024/03/05", "2024-03-05T10:00:00Z" -> "2024-03-05"
- lists: empties removed, exact (case-insensitive) duplicates removed
- authors: trimmed and de-duplicated

Every change is counted so the evaluation (and the user) can see what was fixed.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from typing import Any

from ..models import Record

# Symbols like "-" or "+" are real data in places (DNA strand, sign), so only textual placeholders count.
PLACEHOLDERS = {
    "", "n/a", "na", "n.a.", "none", "null", "nil", "nan", "undefined", "unknown",
    "not available", "not applicable", "not reported", "not specified", "unspecified", "missing", "[]", "{}",
}
# Only real HTML / JATS / MathML tags: "IC50<IC90 and EC50>2" must survive untouched.
_TAG = re.compile(
    r"</?(?:[a-z]+:[\w.-]+|a|b|i|u|p|br|hr|em|strong|sub|sup|span|div|ul|ol|li|dl|dt|dd|table|thead|tbody|"
    r"tr|td|th|h[1-6]|font|small|big|img|abbr|cite|code|pre|blockquote|sc|italic|bold|title|sec|list|"
    r"list-item|ext-link|xref|label|caption|section|article|figure|figcaption|center|tt|del|ins|mark|q|s|"
    r"strike|wbr)(?:\s[^<>]{0,300})?/?>",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t  -​  　]+")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


class CleaningLog:
    """Counts of fixes applied (e.g. {'placeholder_removed': 3, 'date_normalized': 1})."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def add(self, kind: str, n: int = 1) -> None:
        if n:
            self.counts[kind] += n

    def summary(self) -> list[str]:
        labels = {
            "placeholder_removed": "empty/placeholder values removed",
            "html_stripped": "HTML/markup stripped",
            "whitespace_fixed": "whitespace/control characters fixed",
            "date_normalized": "dates normalized to ISO",
            "duplicate_removed": "duplicate list values removed",
            "author_deduplicated": "duplicate authors removed",
        }
        return [f"{n} {labels.get(k, k)}" for k, n in sorted(self.counts.items())]


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in PLACEHOLDERS
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def clean_text(value: Any, log: CleaningLog | None = None, *, multiline: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value)
    original = text
    stripped = False
    if "<" in text and _TAG.search(text):
        text = _TAG.sub(" ", text)
        stripped = True
        if log:
            log.add("html_stripped")
    if "&" in text:
        text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text)
    if multiline:
        text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = _SPACES.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()
    if log and not stripped and text != original.strip():
        log.add("whitespace_fixed")
    if text.lower() in PLACEHOLDERS:
        if log:
            log.add("placeholder_removed")
        return None
    return text


def clean_date(value: Any, log: CleaningLog | None = None) -> str | None:
    """Normalize to YYYY, YYYY-MM or YYYY-MM-DD; returns None for unparseable input."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in PLACEHOLDERS:
        return None
    out: str | None = None
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        out = text
    elif m := re.match(r"^(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?(?:[T ].*)?$", text):
        y, mo, d = m.group(1), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12:
            out = f"{y}-{mo:02d}" + (f"-{int(d):02d}" if d and 1 <= int(d) <= 31 else "")
    elif m := re.match(r"^(\d{4})\s+([A-Za-z]{3})[a-z]*\.?(?:\s+(\d{1,2}))?", text):
        mo = _MONTHS.get(m.group(2).lower())
        if mo:
            out = f"{m.group(1)}-{mo:02d}" + (f"-{int(m.group(3)):02d}" if m.group(3) else "")
    elif m := re.match(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})$", text):
        mo = _MONTHS.get(m.group(2).lower())
        if mo:
            out = f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    elif m := re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{4})$", text):
        mo = _MONTHS.get(m.group(1).lower())
        if mo:
            out = f"{m.group(2)}-{mo:02d}"
    elif m := re.match(r"^(\d{4})\b", text):
        out = m.group(1)
    if log and out and out != text:
        log.add("date_normalized")
    return out


def clean_value(value: Any, log: CleaningLog | None = None, depth: int = 0, keep_keys: bool = False) -> Any:
    """Recursively clean an attribute value; returns None when nothing meaningful is left.

    Dicts inside lists are table rows: with ``keep_keys`` their keys survive (placeholders
    become None) so every row keeps the same shape.
    """
    if depth > 6:
        return value
    if isinstance(value, str):
        return clean_text(value, log, multiline=len(value) > 300)
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            cleaned = clean_value(item, log, depth + 1)
            if cleaned is None or (isinstance(cleaned, (list, dict)) and not cleaned):
                if log and item is not None and not isinstance(item, (dict, list)):
                    log.add("placeholder_removed")
                if keep_keys:
                    out[key] = None
                continue
            out[key] = cleaned
        if keep_keys:
            return out if any(v is not None for v in out.values()) else None
        return out or None
    if isinstance(value, (list, tuple, set)):
        out_list: list[Any] = []
        seen: set[str] = set()
        for item in value:
            cleaned = clean_value(item, log, depth + 1, keep_keys=isinstance(item, dict))
            if cleaned is None or (isinstance(cleaned, (list, dict)) and not cleaned):
                continue
            key = cleaned.lower() if isinstance(cleaned, str) else repr(cleaned)
            if key in seen:
                if log:
                    log.add("duplicate_removed")
                continue
            seen.add(key)
            out_list.append(cleaned)
        return out_list or None
    return value


def clean_record(record: Record, log: CleaningLog | None = None) -> Record:
    """Return a cleaned copy of ``record`` (the original is not modified)."""
    log = log or CleaningLog()
    authors: list[str] = []
    seen: set[str] = set()
    for author in record.authors:
        name = clean_text(author, log)
        if not name:
            continue
        key = re.sub(r"[^a-z]", "", name.lower())
        if key in seen:
            log.add("author_deduplicated")
            continue
        seen.add(key)
        authors.append(name)
    attributes = clean_value(record.attributes, log) or {}
    return record.model_copy(
        update={
            "title": clean_text(record.title, log),
            "summary": clean_text(record.summary, log, multiline=True),
            "date": clean_date(record.date, log),
            "authors": authors,
            "attributes": attributes,
            "url": (record.url or "").strip() or None,
        }
    )
