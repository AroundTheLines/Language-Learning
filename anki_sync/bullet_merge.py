"""
bullet_merge.py

Section-aware bullet union for the `Auto-Generated Context` Anki field.

Field shape (fully sync-managed; user prose lives in `Personal Context`):

    Context:
    • bullet 1
    • bullet 2

    Highlighted forms:
    • form a

The merger:
  1. Parses both the existing field text and the new CSV-derived text into
     {section_header: [bullet, ...]}.
  2. For each section, takes the union of (existing ∪ new), preserving the
     order: existing bullets first (in their original order), then new bullets
     not already present.
  3. Renders back to text using <br> as the line separator (Anki's preferred
     in-field newline).

Bullet equality is whitespace- and trailing-punctuation-insensitive (so
"Asusta." and "Asusta" dedupe), but the *first* form encountered is preserved
verbatim — we never silently rewrite a bullet's casing or punctuation.

Section equality is exact-match on the header line (e.g. "Context:"). New
sections are appended after existing ones in their first-seen order.
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict


# Lines starting with one of these markers are treated as bullets.
_BULLET_MARKERS = "•·*\u2022\u2023\u25E6\u2043"
_BULLET_RE = re.compile(rf"^\s*[{re.escape(_BULLET_MARKERS)}\-]\s*(.+?)\s*$")
_HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
# Closing tags for block-level elements that imply a line break in rendered HTML.
# Anki sometimes wraps each line in <div>...</div>, so we need to treat
# </div> (and friends) as line separators *before* stripping all tags.
_HTML_BLOCK_CLOSE_RE = re.compile(
    r"</\s*(div|p|li|ul|ol|h[1-6]|tr|blockquote)\s*>", re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _to_lines(text: str) -> list[str]:
    """Split a field's text into logical lines, treating <br> as a separator."""
    if not text:
        return []
    # Normalize <br> variants to \n.
    normalized = _HTML_BR_RE.sub("\n", text)
    # Treat closing block-level tags as newlines (Anki's <div>-per-line shape).
    normalized = _HTML_BLOCK_CLOSE_RE.sub("\n", normalized)
    # Strip any remaining HTML tags. Preserve their text content.
    normalized = _HTML_TAG_RE.sub("", normalized)
    # Decode common HTML entities.
    normalized = (
        normalized.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return [ln.rstrip() for ln in normalized.split("\n")]


def _normalize_for_dedup(s: str) -> str:
    """Return a comparison key for bullet content."""
    # Unicode-normalize so e.g. composed vs decomposed accents match.
    s = unicodedata.normalize("NFC", s)
    # Collapse all whitespace to single spaces.
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing punctuation that doesn't change meaning.
    s = s.rstrip(".,;:!?¿¡…")
    return s.casefold()


def parse(text: str) -> "OrderedDict[str, list[str]]":
    """Parse field text into {section_header: [bullet, ...]} preserving order.

    A section header is any non-bullet line ending in ':'. Bullets are lines
    matching `[•·*-]\\s+content`. Lines that are neither (blank lines, stray
    text) are ignored — this field is sync-managed, so anything else is
    considered noise.

    A bullet that appears before any section header is dropped (no section
    to attach it to). In practice this never happens because the pipeline
    always emits a header first.
    """
    sections: "OrderedDict[str, list[str]]" = OrderedDict()
    current: str | None = None
    for raw in _to_lines(text):
        line = raw.strip()
        if not line:
            continue
        m = _BULLET_RE.match(line)
        if m:
            if current is not None:
                sections[current].append(m.group(1).strip())
            continue
        # Non-bullet line: treat as section header if it ends with ':'.
        if line.endswith(":"):
            current = line
            sections.setdefault(current, [])
    return sections


def union(
    existing: "OrderedDict[str, list[str]]",
    new: "OrderedDict[str, list[str]]",
) -> "OrderedDict[str, list[str]]":
    """Per-section bullet union. Existing order is preserved; new sections
    are appended after existing ones in the order they appear in `new`."""
    result: "OrderedDict[str, list[str]]" = OrderedDict()
    # Seed with existing sections in order.
    for header, bullets in existing.items():
        result[header] = list(bullets)
    # Layer in new sections / new bullets.
    for header, new_bullets in new.items():
        if header not in result:
            result[header] = []
        bucket = result[header]
        seen = {_normalize_for_dedup(b) for b in bucket if b.strip()}
        for b in new_bullets:
            key = _normalize_for_dedup(b)
            if not key or key in seen:
                continue
            seen.add(key)
            bucket.append(b)
    return result


def render(sections: "OrderedDict[str, list[str]]", separator: str = "<br>") -> str:
    """Render sections back to a text blob using <br> as the line separator.

    Sections are separated by an empty <br> line for readability in Anki.
    """
    out: list[str] = []
    first = True
    for header, bullets in sections.items():
        if not bullets:
            # Skip empty sections — emitting a header with no content adds noise.
            continue
        if not first:
            out.append("")  # blank line between sections
        first = False
        out.append(header)
        for b in bullets:
            out.append(f"• {b}")
    return separator.join(out)


def merge(existing_text: str, new_text: str, separator: str = "<br>") -> str:
    """End-to-end: parse both, union, render.

    Returns the new text for the Auto-Generated Context field.
    Idempotent: merge(x, x) == x (modulo whitespace / separator normalization).
    """
    existing_sections = parse(existing_text)
    new_sections = parse(new_text)
    merged = union(existing_sections, new_sections)
    return render(merged, separator=separator)
