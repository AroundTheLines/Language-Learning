"""
phrase_normalize.py

Dedup-key normalization and cloze-markup helpers for the phrase pipeline.

Normalization rules:
  1. Convert all Unicode whitespace (NBSP, em-space, tab, etc.) to ASCII space.
  2. Collapse runs of multiple spaces to a single space.
  3. Trim leading/trailing whitespace.
  4. Lowercase (casefold).

MUST-NOT rules:
  - Do not fold accents (sé ≠ se, mas ≠ más).
  - Do not strip or alter punctuation — for phrase cards the punctuation
    carries meaning (¿?, ¡!, periods, commas all differentiate constructions).
    "¿Verdad?" is intentionally NOT the same key as "verdad". This departs
    from the spec §6 rules 5–6 and yellow's conventions; see the note in
    phrase_cards_spec.md.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Trailing punctuation we tolerate when locating a phrase inside its context
# sentence. Unlike yellow's dedup logic, these are NOT stripped from the
# dedup key — they're only used by find_phrase_in_context to probe the
# sentence with a looser match when the CSV's highlighted text has a
# trailing mark that isn't present verbatim in the context. The dedup key
# keeps them intact.
_TRAILING_PUNCT = ".!?…,;:\"'“”‘’«»"


def normalize_phrase_key(s: str) -> str:
    """Return the dedup key for a highlighted phrase. See rules above.

    Punctuation is preserved: "¿Verdad?" and "verdad" intentionally produce
    different keys because for phrase cards the punctuation carries meaning.
    """
    if not s:
        return ""
    # Step 1: any Unicode whitespace → ASCII space.
    s = "".join(" " if ch.isspace() else ch for ch in s)
    # Step 2 + 3: collapse and trim.
    s = re.sub(r" +", " ", s).strip()
    # Step 4: lowercase. We use casefold for unicode-correctness (ß → ss, etc.);
    # Spanish doesn't trigger this, but casefold is the canonical choice.
    s = s.casefold()
    return s


# ---------------------------------------------------------------------------
# Cloze markup
# ---------------------------------------------------------------------------

# Sequences that break Anki's cloze parser if they appear inside an answer or
# hint. The spec (§4.2, §9 error handling) requires us to reject these.
_FORBIDDEN_IN_CLOZE = ("::", "}}")


class ClozeError(ValueError):
    """Raised when cloze spans/hints violate a structural constraint."""


@dataclass
class ClozeSpan:
    start: int  # Python str character offset (inclusive)
    end: int    # end-exclusive, same semantics as slicing
    hint: str


def validate_spans(context: str, spans: list[ClozeSpan]) -> None:
    """Check that spans are in bounds, non-empty, non-overlapping, and contain
    no forbidden sequences. Raises ClozeError with a human message on failure.
    """
    if not spans:
        raise ClozeError("at least one cloze span is required")
    if len(spans) > 3:
        raise ClozeError(f"too many cloze spans ({len(spans)}); hard cap is 3")

    n = len(context)
    # Sort by start for overlap detection; caller order is preserved for output.
    ordered = sorted(range(len(spans)), key=lambda i: spans[i].start)
    prev_end = -1
    for idx in ordered:
        sp = spans[idx]
        if sp.start < 0 or sp.end > n or sp.start >= sp.end:
            raise ClozeError(
                f"span {idx} ({sp.start}, {sp.end}) is out of bounds for "
                f"context of length {n} or non-positive"
            )
        # Reject both strict overlap AND touching spans. Touching spans
        # render as `{{c1::abc}}{{c2::def}}` which Anki shows as two
        # visually-merged blanks with no gap — confusing to read and
        # indistinguishable from a single blank. Require at least one
        # character of context between cloze answers.
        if sp.start <= prev_end:
            raise ClozeError(
                f"span {idx} starts at {sp.start} but previous span ended at "
                f"{prev_end} — spans must not overlap or touch"
            )
        prev_end = sp.end

        answer = context[sp.start : sp.end]
        for bad in _FORBIDDEN_IN_CLOZE:
            if bad in answer:
                raise ClozeError(
                    f"span {idx} answer {answer!r} contains forbidden {bad!r}"
                )
        for bad in _FORBIDDEN_IN_CLOZE:
            if bad in sp.hint:
                raise ClozeError(
                    f"span {idx} hint {sp.hint!r} contains forbidden {bad!r}"
                )


def apply_clozes(context: str, spans: list[ClozeSpan]) -> str:
    """Return the context sentence with `{{cN::answer::hint}}` markup applied.

    Cloze numbering is assigned in order of appearance in the sentence
    (leftmost span → c1). This ensures stable markup regardless of the input
    order of the spans list.
    """
    validate_spans(context, spans)

    # Assign c-numbers by left-to-right position.
    ordered = sorted(spans, key=lambda sp: sp.start)
    numbering = {id(sp): i + 1 for i, sp in enumerate(ordered)}

    out: list[str] = []
    cursor = 0
    for sp in ordered:
        out.append(context[cursor : sp.start])
        n = numbering[id(sp)]
        answer = context[sp.start : sp.end]
        hint = sp.hint.strip()
        if hint:
            out.append(f"{{{{c{n}::{answer}::{hint}}}}}")
        else:
            out.append(f"{{{{c{n}::{answer}}}}}")
        cursor = sp.end
    out.append(context[cursor:])
    return "".join(out)


def find_phrase_in_context(context: str, phrase: str) -> tuple[int, int] | None:
    """Locate `phrase` inside `context` using accent-preserving,
    case-insensitive match. Returns (start, end) char offsets or None.

    Used as a fallback cloze target when the LLM's spans fail validation but
    the phrase itself clearly occurs in the sentence.
    """
    if not phrase or not context:
        return None
    ctx_nfc = unicodedata.normalize("NFC", context)
    ph_nfc = unicodedata.normalize("NFC", phrase).strip()
    ctx_lower = ctx_nfc.casefold()
    ph_lower = ph_nfc.casefold()
    # Trim trailing punctuation on the probe (the CSV sometimes stores the
    # phrase with a trailing period that doesn't appear verbatim in context).
    ph_trimmed = ph_lower.rstrip(_TRAILING_PUNCT + " ")
    for probe in (ph_lower, ph_trimmed):
        if not probe:
            continue
        idx = ctx_lower.find(probe)
        if idx >= 0:
            return idx, idx + len(probe)
    return None
