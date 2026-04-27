"""
llm_enrich.py

LLM enrichment for orange (phrase) highlights. Spec §7.

Input per call:
    phrase          — the user's highlighted text (raw CSV `lemma`)
    context_sentence— the first bullet from CSV `context_sentences` (earliest
                      occurrence in the source book)
    personal_note   — optional user note_text

Output (structured JSON via Anthropic tool use):
    cloze_spans     — 1–3 (start, end) Python-str char offsets into
                      context_sentence
    cloze_hints     — Spanish-only micro-hints, same length as cloze_spans
    insight         — 1–2 line mixed Spanish/English linguistic note
    alternatives    — 1–2 paraphrases, each with terse English annotation
    translation     — English of the full context sentence

Caching:
    Keyed by sha256(phrase || "\\x1f" || context_sentence). `personal_note` is
    intentionally NOT part of the key — re-running with a new note should not
    re-bill, because the fields driven by the note live in Anki (managed
    bullet-union) not in the enrichment output.

Error handling (spec §9 #7):
    - validate cloze spans map to valid, non-overlapping substrings
    - reject `::` or `}}` inside any cloze answer or hint
    - fall back to `find_phrase_in_context` when the LLM's spans don't pass
      validation and the phrase is clearly present in the sentence
    - re-raise EnrichmentError if even the fallback can't produce valid markup
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.phrase_normalize import (
        ClozeError,
        ClozeSpan,
        apply_clozes,
        find_phrase_in_context,
        validate_spans,
    )
else:
    from .phrase_normalize import (
        ClozeError,
        ClozeSpan,
        apply_clozes,
        find_phrase_in_context,
        validate_spans,
    )


MODEL_ID = "claude-haiku-4-5-20251001"
DEFAULT_CACHE_PATH = (
    Path(__file__).parent / "state" / "phrase_enrichment_cache.json"
)


# Held constant across calls so prompt caching can land. Changing this
# invalidates the Anthropic prompt cache AND our local enrichment cache.
SYSTEM_PROMPT = """\
You are enriching Spanish-language highlights for a Fluent-Forever-style Anki
deck. Each input is a short phrase the user highlighted (something
grammatically, idiomatically, or stylistically interesting — NOT a word they
want to memorize) plus the exact context sentence it came from.

You produce a structured enrichment so the user can learn the phrase in
context, with no English on the front of the card.

Hard rules:
- No English on the cloze answers or hints. Hints are Spanish-only.
- The `insight` and `alternatives` fields are Spanish-dominant, using English
  only for terse glosses in parentheses (e.g. "dativo de interés (≈ affected
  party)"). Never full English sentences in insight.
- Insight is objective/linguistic: what construction is this, what does it do,
  where would the learner see it again. 1–2 lines max.
- `explanation` is a SEPARATE plain-English gloss of the saying itself: what
  it means, and when/why a native speaker would use it. Written for a learner
  who finds the Spanish-dominant insight too dense. 1–2 sentences, English
  only. Do NOT translate the sentence (that's `translation`) and do NOT
  duplicate the linguistic framing from `insight` — focus on the meaning and
  situational use of the phrase as a whole.
- Alternatives: 1–2 Spanish paraphrases, each with a terse English annotation
  like "(more casual)" or "(same function, different register)". One line
  each.
- `cloze_spans` are Python-str character offsets (end-exclusive) into the
  supplied `context_sentence`. They MUST be non-overlapping, in-bounds, and
  fall ENTIRELY within the highlighted phrase as located in the context. The
  user message gives you `phrase_offsets_in_context: [start, end]` — every
  cloze span must satisfy `start >= phrase_offsets[0]` and
  `end <= phrase_offsets[1]`. Never cloze words outside the highlighted
  phrase, even if they look interesting; the user is studying the phrase
  they highlighted, not adjacent material.
- Within the highlighted phrase, default to clozing the WHOLE phrase. Pick a
  narrower sub-span only when the phrase is long (≥5 words) AND there is a
  clear focused "hook" inside it (an idiom, a tricky construction, the
  surprising word). Short phrases (1–4 words) should always be clozed in
  full.
- `personal_note` (when present) is the user's stated reason for highlighting
  this phrase — usually a short gloss like "= of course" or "means 'right?'".
  Treat it as authoritative: the cloze MUST cover the part of the phrase the
  note is about. If the note glosses the whole phrase, cloze the whole
  phrase. Do NOT cloze a sub-span that excludes what the note refers to.
- Never emit `::` or `}}` inside a cloze answer or hint — those break Anki.
- Hints exist to disambiguate synonyms so the card isn't failed for the wrong
  reason; keep each under ~25 characters.
- Cloze target count: default 1. Occasionally 2. Hard cap 3.
- `translation` is a natural English rendering of the FULL context sentence
  (not just the cloze), useful to a learner who wants to confirm meaning.
"""


# JSON-Schema for the tool. Anthropic's tool-use enforces structured output.
ENRICHMENT_TOOL = {
    "name": "emit_phrase_enrichment",
    "description": (
        "Emit the structured enrichment for one highlighted Spanish phrase."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "cloze_spans",
            "cloze_hints",
            "insight",
            "explanation",
            "alternatives",
            "translation",
        ],
        "properties": {
            "cloze_spans": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "integer", "minimum": 0},
                },
                "description": (
                    "List of [start, end] Python-str offsets into "
                    "context_sentence, end-exclusive. Non-overlapping."
                ),
            },
            "cloze_hints": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 60},
                "description": (
                    "One Spanish-only hint per cloze_span, same length as "
                    "cloze_spans."
                ),
            },
            "insight": {
                "type": "string",
                "description": (
                    "1–2 line Spanish-dominant linguistic note, English only "
                    "for terse glosses."
                ),
            },
            "explanation": {
                "type": "string",
                "description": (
                    "Plain-English explanation of what the phrase means and "
                    "when a native speaker would use it. 1–2 sentences, "
                    "English only. Different from `translation` (which "
                    "renders the sentence) and from `insight` (which frames "
                    "the grammar)."
                ),
            },
            "alternatives": {
                "type": "string",
                "description": (
                    "1–2 Spanish paraphrases, each with a terse English "
                    "annotation. One line each, separated by <br>."
                ),
            },
            "translation": {
                "type": "string",
                "description": "Natural English of the full context sentence.",
            },
        },
    },
}


class EnrichmentError(RuntimeError):
    pass


@dataclass
class EnrichmentResult:
    phrase: str
    context_sentence: str
    cloze_sentence: str           # post-markup, ready for Anki
    translation: str
    insight: str
    explanation: str
    alternatives: str
    spans: list[ClozeSpan] = field(default_factory=list)
    cache_hit: bool = False
    fallback_used: bool = False
    """True when the LLM's cloze spans failed validation and we fell back
    to a whole-phrase cloze. These cards have a hintless single blank
    instead of focused spans + hints — worth flagging to the reviewer so
    they can decide whether to hand-edit the note in Anki."""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class EnrichmentCache:
    """Flat JSON cache on disk, keyed by sha256(phrase + 0x1F + context)."""

    def __init__(self, path: Path | str = DEFAULT_CACHE_PATH):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                # Corrupt cache — start fresh rather than crash. The file is
                # rebuildable from the LLM.
                self.data = {}

    @staticmethod
    def key(phrase: str, context: str) -> str:
        h = hashlib.sha256()
        h.update(phrase.encode("utf-8"))
        h.update(b"\x1f")
        h.update(context.encode("utf-8"))
        return h.hexdigest()

    def get(self, phrase: str, context: str) -> dict | None:
        return self.data.get(self.key(phrase, context))

    def put(self, phrase: str, context: str, value: dict) -> None:
        self.data[self.key(phrase, context)] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

def _load_client():
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError as e:
        raise EnrichmentError(
            "The 'anthropic' Python SDK is required for enrichment. "
            "Install with: pip install anthropic"
        ) from e
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnrichmentError(
            "ANTHROPIC_API_KEY is not set. Export it before running "
            "enrichment: export ANTHROPIC_API_KEY=sk-ant-..."
        )
    return anthropic.Anthropic(api_key=api_key)


def _build_user_message(
    phrase: str,
    context: str,
    note: str | None,
    phrase_span: tuple[int, int] | None,
) -> str:
    """Build the user message. `phrase_span` is the result of
    `find_phrase_in_context(context, phrase)` — passed in so callers can
    reuse it for downstream validation without recomputing."""
    parts = [
        f"phrase: {phrase}",
        f"context_sentence: {context}",
    ]
    # When phrase_span is present we hand the LLM hard offset bounds for
    # cloze selection. Without this, the LLM tends to drift away from what
    # the user actually highlighted (e.g. clozing a different construction
    # in the same sentence). If we can't locate it, fall back to the
    # looser instruction in the system prompt.
    if phrase_span is not None:
        start, end = phrase_span
        parts.append(
            f"phrase_offsets_in_context: [{start}, {end}] "
            f"(highlighted text: {context[start:end]!r}). "
            "Cloze spans MUST stay strictly inside this range."
        )
    else:
        parts.append(
            "phrase_offsets_in_context: (could not be located verbatim — "
            "cloze the substring of context_sentence that best corresponds "
            "to the phrase, and nothing else)."
        )
    if note:
        parts.append(
            f"personal_note (drives cloze selection — cloze MUST cover the "
            f"part of the phrase this note is about): {note}"
        )
    else:
        parts.append("personal_note: (none)")
    parts.append(
        "\nEmit the enrichment via the emit_phrase_enrichment tool. "
        "cloze_spans are character offsets into the EXACT string given as "
        "context_sentence above."
    )
    return "\n".join(parts)


def _call_llm(
    client,
    phrase: str,
    context: str,
    note: str | None,
    phrase_span: tuple[int, int] | None,
    max_retries: int = 2,
) -> dict:
    """Invoke the LLM with tool use. Returns the tool input dict.

    `phrase_span` is `find_phrase_in_context(context, phrase)` — passed in
    so callers can reuse the same span for downstream span-bounds validation
    without scanning the sentence twice."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.messages.create(
                model=MODEL_ID,
                max_tokens=1024,
                temperature=0,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[ENRICHMENT_TOOL],
                tool_choice={"type": "tool", "name": ENRICHMENT_TOOL["name"]},
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_message(
                            phrase, context, note, phrase_span
                        ),
                    }
                ],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == ENRICHMENT_TOOL["name"]:
                    return dict(block.input)
            raise EnrichmentError(
                f"LLM returned no tool_use block (stop_reason={resp.stop_reason})"
            )
        except EnrichmentError:
            raise
        except Exception as e:  # network / transient API errors
            last_err = e
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise EnrichmentError(f"LLM call failed after retries: {e}") from e
    raise EnrichmentError(f"unreachable: {last_err!r}")


# ---------------------------------------------------------------------------
# Post-processing & validation
# ---------------------------------------------------------------------------

def _coerce_spans(raw: Any) -> list[tuple[int, int]]:
    """The tool schema allows arrays-of-int. Coerce defensively."""
    out: list[tuple[int, int]] = []
    for s in raw or []:
        try:
            start, end = int(s[0]), int(s[1])
        except (TypeError, ValueError, IndexError) as e:
            raise EnrichmentError(f"malformed cloze_span entry: {s!r}") from e
        out.append((start, end))
    return out


def _build_result(
    phrase: str,
    context: str,
    tool_input: dict,
    phrase_span: tuple[int, int] | None = None,
) -> EnrichmentResult:
    """Validate the LLM's tool output and produce an `EnrichmentResult`.

    `phrase_span`: precomputed `find_phrase_in_context(context, phrase)`. When
    None, we recompute here. We surface a one-line stderr warning when the
    phrase can't be located in the context — those rows skip the in-phrase
    bounds check and rely on prompt instructions alone, which is worth
    knowing about during a re-run.
    """
    spans_raw = _coerce_spans(tool_input.get("cloze_spans"))
    hints = list(tool_input.get("cloze_hints") or [])
    if len(hints) != len(spans_raw):
        raise EnrichmentError(
            f"cloze_spans ({len(spans_raw)}) and cloze_hints ({len(hints)}) "
            "have different lengths"
        )
    spans = [ClozeSpan(s, e, h) for (s, e), h in zip(spans_raw, hints)]

    # Try the LLM's spans first; fall back to a whole-phrase cloze if they
    # don't validate (spec §9 #7). Record whether the fallback fired so we
    # can flag the row for hand-review downstream.
    #
    # We additionally enforce that every span lies within the highlighted
    # phrase's offsets in the context. The LLM is instructed to do this in
    # the prompt, but it sometimes drifts to nearby words; this check keeps
    # the cloze anchored to what the user actually highlighted.
    fallback_used = False
    if phrase_span is None:
        phrase_span = find_phrase_in_context(context, phrase)
        if phrase_span is None:
            # The phrase doesn't appear verbatim in the context (e.g. the
            # CSV stored a normalised form, or the EPUB uses curly quotes
            # the user's highlight didn't). Bounds check is skipped for
            # this row; flag it so reviewers know which cards relied on
            # prompt-only anchoring.
            print(
                f"  ⚠ phrase {phrase!r} not located verbatim in context; "
                "skipping in-phrase span bounds check.",
                file=sys.stderr,
            )
    try:
        validate_spans(context, spans)
        if phrase_span is not None:
            ph_start, ph_end = phrase_span
            for sp in spans:
                if sp.start < ph_start or sp.end > ph_end:
                    raise ClozeError(
                        f"span ({sp.start}, {sp.end}) escapes highlighted "
                        f"phrase bounds ({ph_start}, {ph_end})"
                    )
    except ClozeError as e:
        if phrase_span is None:
            raise EnrichmentError(
                f"cloze spans invalid ({e}) and phrase not found in context"
            ) from e
        start, end = phrase_span
        spans = [ClozeSpan(start, end, "")]
        validate_spans(context, spans)
        fallback_used = True
        # Surface the degradation. The CSV also carries this via the
        # `fallback_used` column so the reviewer sees which cards need
        # hand-editing in Anki. Flatten any newlines in the exception
        # message so the warning stays on a single stderr line — otherwise
        # it can fragment the progress bar's in-place redraw.
        err_msg = " ".join(str(e).splitlines())
        print(
            f"  ⚠ cloze-span fallback for {phrase!r}: {err_msg}. "
            "Using whole-phrase cloze with no hint.",
            file=sys.stderr,
        )

    cloze_sentence = apply_clozes(context, spans)

    translation = (tool_input.get("translation") or "").strip()
    insight = (tool_input.get("insight") or "").strip()
    explanation = (tool_input.get("explanation") or "").strip()
    alternatives = (tool_input.get("alternatives") or "").strip()
    if not translation:
        raise EnrichmentError("LLM returned empty translation")
    if not insight:
        raise EnrichmentError("LLM returned empty insight")
    if not explanation:
        raise EnrichmentError("LLM returned empty explanation")
    # `alternatives` may legitimately be a single paraphrase; we don't
    # hard-fail on length. `.strip() or ""` above already covers the empty
    # case, so no extra normalisation needed here.

    return EnrichmentResult(
        phrase=phrase,
        context_sentence=context,
        cloze_sentence=cloze_sentence,
        translation=translation,
        insight=insight,
        explanation=explanation,
        alternatives=alternatives,
        spans=spans,
        fallback_used=fallback_used,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Cache schema sentinel. Bump when a change to the enrichment pipeline
# invalidates older cached LLM outputs (e.g. new fields, new validation
# rules). Cached entries are considered fresh only when their `_schema_v`
# matches CACHE_SCHEMA_VERSION.
#
# v1: introduced when we added `explanation`.
# v2: spans must lie within the highlighted phrase's offsets (anchoring fix).
#     Older cached entries may have spans that drift outside the highlight;
#     re-fetching them is cheaper than bespoke fixup logic.
CACHE_SCHEMA_VERSION = 2


def is_cache_entry_fresh(entry: dict | None) -> bool:
    """Single source of truth for cache freshness. Both the public
    `enrich_phrase` path and the parallel batch path in `enrich_phrases.py`
    consult this so cache hits resolve identically."""
    if entry is None:
        return False
    if not (entry.get("explanation") or "").strip():
        return False
    if int(entry.get("_schema_v") or 0) < CACHE_SCHEMA_VERSION:
        return False
    return True


def _stamp_schema(tool_input: dict) -> dict:
    """Tag a freshly-produced tool_input with the current schema version
    before caching. The sentinel is stored alongside the LLM fields and
    ignored by `_build_result` (which only reads the named fields)."""
    stamped = dict(tool_input)
    stamped["_schema_v"] = CACHE_SCHEMA_VERSION
    return stamped


def enrich_phrase(
    phrase: str,
    context_sentence: str,
    personal_note: str | None = None,
    *,
    cache: EnrichmentCache | None = None,
    client=None,
) -> EnrichmentResult:
    """Enrich a single phrase. Hits the cache if present; otherwise calls the
    LLM and stores the result.

    The caller is expected to `cache.save()` periodically (e.g. after a batch
    or at end of run)."""
    if cache is None:
        cache = EnrichmentCache()

    phrase_span = find_phrase_in_context(context_sentence, phrase)
    cached = cache.get(phrase, context_sentence)
    if is_cache_entry_fresh(cached):
        result = _build_result(phrase, context_sentence, cached, phrase_span)
        result.cache_hit = True
        return result

    if client is None:
        client = _load_client()

    tool_input = _call_llm(
        client, phrase, context_sentence, personal_note, phrase_span
    )
    # Validate before caching so we don't permanently cache garbage.
    result = _build_result(phrase, context_sentence, tool_input, phrase_span)
    cache.put(phrase, context_sentence, _stamp_schema(tool_input))
    return result
