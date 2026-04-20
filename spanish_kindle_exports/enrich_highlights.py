"""
enrich_highlights.py

Adds a 'context_sentence' column to a Kindle highlights CSV by finding
each highlight in the EPUB text and extracting the surrounding sentence.

Usage:
    python enrich_highlights.py highlights.csv book.epub

Output is written to an 'enriched/' subdirectory next to this script,
named after the input CSV with an '_enriched' suffix.

Requirements:
    pip install ebooklib beautifulsoup4
"""

import csv
import re
import sys
import zipfile
from pathlib import Path

try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install ebooklib beautifulsoup4")
    sys.exit(1)


# ---------------------------------------------------------------------------
# EPUB helpers
# ---------------------------------------------------------------------------


def extract_epub_text_in_order(epub_path: str) -> str:
    """
    Extract all readable text from the EPUB in spine order,
    joined into one big string. Uses plain zip + BeautifulSoup
    so it works even with EPUBs that ebooklib can't fully parse.
    """
    spine_texts = []

    with zipfile.ZipFile(epub_path, "r") as zf:
        # Try to read the OPF spine to get chapter order
        opf_path = _find_opf(zf)
        ordered_items = _spine_order(zf, opf_path) if opf_path else []

        # Fall back: just grab all HTML/XHTML files alphabetically
        if not ordered_items:
            ordered_items = sorted(
                [n for n in zf.namelist() if n.endswith((".html", ".xhtml", ".htm"))]
            )

        for item_path in ordered_items:
            try:
                raw = zf.read(item_path)
                soup = BeautifulSoup(raw, "html.parser")
                # Remove script/style noise
                for tag in soup(["script", "style"]):
                    tag.decompose()
                text = soup.get_text(separator=" ")
                # Collapse whitespace but preserve sentence boundaries
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\n{2,}", "\n", text)
                spine_texts.append(text.strip())
            except Exception:
                continue

    return "\n".join(spine_texts)


def _find_opf(zf: zipfile.ZipFile) -> str | None:
    """Locate the OPF file path via META-INF/container.xml."""
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
        match = re.search(r'full-path="([^"]+\.opf)"', container)
        return match.group(1) if match else None
    except Exception:
        return None


def _spine_order(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    """Return HTML file paths in spine order from the OPF manifest/spine."""
    try:
        opf_dir = str(Path(opf_path).parent)
        raw = zf.read(opf_path).decode("utf-8", errors="replace")
        soup = BeautifulSoup(raw, "html.parser")

        # Build id -> href map from manifest
        manifest = {}
        for item in soup.find_all("item"):
            item_id = item.get("id")
            href = item.get("href")
            media_type = item.get("media-type", "")
            if item_id and href and "html" in media_type:
                # Resolve relative to OPF directory
                full = str(Path(opf_dir) / href) if opf_dir != "." else href
                # Normalize path separators
                full = full.replace("\\", "/")
                manifest[item_id] = full

        # Follow spine order
        ordered = []
        for itemref in soup.find_all("itemref"):
            idref = itemref.get("idref")
            if idref and idref in manifest:
                ordered.append(manifest[idref])

        return ordered
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Text normalisation for matching
# ---------------------------------------------------------------------------

# Characters that appear in highlight_text but may differ in the EPUB
_PUNCT_STRIP = str.maketrans("", "", ".,;:!?¡¿\"'«»''—–-…\u00ab\u00bb")
_DASHES = re.compile(r"[—–\-]")


def _normalise(text: str) -> str:
    """Lower-case, strip leading em-dash / dialogue markers, collapse spaces."""
    text = text.strip()
    # Kindle sometimes includes leading em-dash for dialogue
    text = _DASHES.sub(" ", text)
    text = text.translate(_PUNCT_STRIP)
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Sentence extraction
# ---------------------------------------------------------------------------

# Sentence boundary: period / ! / ? followed by space or end, but not inside
# abbreviations. Good enough for Spanish fiction.
_SENT_SPLIT = re.compile(r"(?<=[.!?»\"])\s+")


def _extract_sentence(full_text: str, match_start: int, match_end: int) -> str:
    """
    Given character offsets into full_text where the highlight was found,
    return the sentence (or short window) that contains the highlight.
    """
    # Search backward for sentence start
    window_start = max(0, match_start - 300)
    window_end = min(len(full_text), match_end + 300)
    left_chunk = full_text[window_start:match_start]
    right_chunk = full_text[match_end:window_end]

    # Find the nearest sentence-ending punctuation to the left
    left_boundary = max(
        (m.end() for m in re.finditer(r"[.!?»\"]\s", left_chunk)),
        default=0,
    )
    # Find the nearest sentence-ending punctuation to the right
    right_match = re.search(r"[.!?»\"]", right_chunk)
    right_boundary = right_match.end() if right_match else len(right_chunk)

    sentence = (
        left_chunk[left_boundary:]
        + full_text[match_start:match_end]
        + right_chunk[:right_boundary]
    )
    sentence = re.sub(r"\s+", " ", sentence).strip()
    return sentence


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------


def find_contexts(highlights: list[dict], full_text: str) -> list[str]:
    """
    For each highlight (in order), find the FIRST occurrence of its text
    at or after the previous match position. Returns a list of context
    sentences in the same order as highlights.
    """
    # Pre-normalise the full corpus once
    norm_full = _normalise(full_text)

    # We also need a character-offset map from norm positions back to original.
    # Simplest approach: build a parallel list of (norm_char, orig_char) pairs.
    # Because normalisation changes length we rebuild the mapping explicitly.
    norm_to_orig = _build_offset_map(full_text, norm_full)

    cursor = 0  # position in norm_full; advances after each successful match
    contexts = []

    for row in highlights:
        highlight = row.get("highlight_text", "").strip()

        # Skip empty highlights (note-only rows)
        if not highlight:
            contexts.append("")
            continue

        norm_highlight = _normalise(highlight)

        if not norm_highlight:
            contexts.append("")
            continue

        # Search from cursor onward
        pos = norm_full.find(norm_highlight, cursor)

        if pos == -1:
            # Try from the beginning (handles edge cases where order is off)
            pos = norm_full.find(norm_highlight)

        if pos == -1:
            # Partial match fallback: use first significant word
            words = norm_highlight.split()
            if words:
                pos = norm_full.find(words[0], cursor)

        if pos == -1:
            contexts.append("[not found in EPUB]")
            continue

        norm_end = pos + len(norm_highlight)

        # Map normalised offsets back to original text offsets
        orig_start = norm_to_orig.get(pos, pos)
        orig_end = norm_to_orig.get(norm_end, norm_end)

        sentence = _extract_sentence(full_text, orig_start, orig_end)
        contexts.append(sentence)

        # Advance cursor past this match so next highlight starts here
        cursor = norm_end

    return contexts


def _build_offset_map(original: str, normalised: str) -> dict[int, int]:
    """
    Build a mapping from normalised string positions to original string positions.
    Re-applies the normalisation steps character-by-character so the mapping
    stays accurate over the entire text.
    """
    # Characters that _normalise removes entirely (via _PUNCT_STRIP),
    # excluding dashes which become spaces first.
    punct_remove = set(".,;:!?¡¿\"'«»\u2018\u2019\u2026\u00ab\u00bb")
    dash_chars = set("\u2014\u2013-")  # —, –, -
    ws_chars = set(" \t\n\r")

    mapping = {}
    o_idx = 0
    n_idx = 0
    o_len = len(original)
    n_len = len(normalised)

    while n_idx < n_len and o_idx < o_len:
        # Skip original characters that normalisation removes entirely
        while o_idx < o_len and original[o_idx] in punct_remove:
            o_idx += 1

        if o_idx >= o_len:
            break

        n_char = normalised[n_idx]

        if n_char == " ":
            # A normalised space can come from: whitespace, dashes, or a
            # run of whitespace/dashes/punctuation that collapsed together.
            mapping[n_idx] = o_idx
            # Advance past the entire run of whitespace + dashes + punctuation
            while o_idx < o_len and (
                original[o_idx] in ws_chars
                or original[o_idx] in dash_chars
                or original[o_idx] in punct_remove
            ):
                o_idx += 1
        else:
            # Regular character – should match after lowercasing
            mapping[n_idx] = o_idx
            o_idx += 1

        n_idx += 1

    # Map any remaining normalised positions to end of original
    for i in range(n_idx, n_len + 1):
        mapping[i] = min(o_idx, o_len)

    return mapping


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def load_highlights(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_enriched(rows: list[dict], contexts: list[str], out_path: str) -> None:
    if not rows:
        print("No rows to write.")
        return

    fieldnames = list(rows[0].keys()) + ["context_sentence"]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row, context in zip(rows, contexts):
            writer.writerow({**row, "context_sentence": context})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _make_output_path(csv_path: str) -> str:
    """
    Build the output path: <script_dir>/enriched/<stem>_enriched.csv.
    Creates the enriched/ directory if it doesn't exist.
    """
    script_dir = Path(__file__).parent
    output_dir = script_dir / "enriched"
    output_dir.mkdir(exist_ok=True)

    stem = Path(csv_path).stem
    return str(output_dir / f"{stem}_enriched.csv")


def main():
    if len(sys.argv) != 3:
        print("Usage: python enrich_highlights.py highlights.csv book.epub")
        sys.exit(1)

    csv_path, epub_path = sys.argv[1], sys.argv[2]
    out_path = _make_output_path(csv_path)

    print(f"Loading highlights from {csv_path}...")
    highlights = load_highlights(csv_path)
    print(f"  {len(highlights)} rows loaded.")

    print(f"Extracting text from {epub_path}...")
    full_text = extract_epub_text_in_order(epub_path)
    print(f"  {len(full_text):,} characters extracted.")

    print("Matching highlights to EPUB text...")
    contexts = find_contexts(highlights, full_text)

    found = sum(1 for c in contexts if c and c != "[not found in EPUB]")
    not_found = sum(1 for c in contexts if c == "[not found in EPUB]")
    skipped = sum(1 for c in contexts if c == "")
    print(f"  Found: {found}  |  Not found: {not_found}  |  Skipped (empty): {skipped}")

    print(f"Writing output to {out_path}...")
    save_enriched(highlights, contexts, out_path)
    print("Done.")


if __name__ == "__main__":
    main()
