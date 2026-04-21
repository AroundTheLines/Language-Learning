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
  supplied `context_sentence`. They must be non-overlapping, in-bounds, and
  inside the highlighted phrase (or, if the phrase is long, a focused
  sub-span that captures the "huh, that's how Spanish does that" hook).
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


def _build_user_message(phrase: str, context: str, note: str | None) -> str:
    parts = [
        f"phrase: {phrase}",
        f"context_sentence: {context}",
    ]
    if note:
        parts.append(f"personal_note: {note}")
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
    max_retries: int = 2,
) -> dict:
    """Invoke the LLM with tool use. Returns the tool input dict."""
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
                        "content": _build_user_message(phrase, context, note),
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
) -> EnrichmentResult:
    spans_raw = _coerce_spans(tool_input.get("cloze_spans"))
    hints = list(tool_input.get("cloze_hints") or [])
    if len(hints) != len(spans_raw):
        raise EnrichmentError(
            f"cloze_spans ({len(spans_raw)}) and cloze_hints ({len(hints)}) "
            "have different lengths"
        )
    spans = [ClozeSpan(s, e, h) for (s, e), h in zip(spans_raw, hints)]

    # Try the LLM's spans first; fall back to a whole-phrase cloze if they
    # don't validate (spec §9 #7).
    try:
        validate_spans(context, spans)
    except ClozeError as e:
        fallback = find_phrase_in_context(context, phrase)
        if fallback is None:
            raise EnrichmentError(
                f"cloze spans invalid ({e}) and phrase not found in context"
            ) from e
        start, end = fallback
        spans = [ClozeSpan(start, end, "")]
        validate_spans(context, spans)

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
    # Alternatives may legitimately be a single paraphrase; we don't hard-fail
    # on length. But an entirely empty field is suspect.
    if not alternatives:
        alternatives = ""

    return EnrichmentResult(
        phrase=phrase,
        context_sentence=context,
        cloze_sentence=cloze_sentence,
        translation=translation,
        insight=insight,
        explanation=explanation,
        alternatives=alternatives,
        spans=spans,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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

    cached = cache.get(phrase, context_sentence)
    # Treat pre-`explanation` cache entries as stale. They were produced before
    # the schema gained the English-explanation field; re-fetching is cheap and
    # avoids bespoke backfill logic downstream.
    if cached is not None and (cached.get("explanation") or "").strip():
        result = _build_result(phrase, context_sentence, cached)
        result.cache_hit = True
        return result

    if client is None:
        client = _load_client()

    tool_input = _call_llm(client, phrase, context_sentence, personal_note)
    # Validate before caching so we don't permanently cache garbage.
    result = _build_result(phrase, context_sentence, tool_input)
    cache.put(phrase, context_sentence, tool_input)
    return result
