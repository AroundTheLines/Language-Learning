# Phrase Cards — Product Design Spec

**Status**: Design locked. Implementation pending.
**Scope**: Orange-highlight pipeline — from Kindle CSV to Anki notes.
**Relationship to existing work**: Parallel to the yellow (Spanish Picture Words) pipeline, not a replacement. Yellow continues to handle lexical/vocabulary learning. This spec covers phrases, idioms, and grammar/syntax curiosities.

---

## 1. Problem

Orange highlights are already captured into `spanish_kindle_exports/by_color/YYYY-MM-DD-<source>_orange.csv` but have no destination in the Anki pipeline. They differ from yellow highlights in kind:

- **Yellow** = a word I want to learn (lexical). Pictures + IPA + multiple example contexts.
- **Orange** = a phrase, idiom, or construction that struck me as "huh, that's how Spanish does that" (grammatical/idiomatic/stylistic). Context *sentence* matters more than breadth of usages.

These are a bad fit for `2.1. Spanish Picture Words`. They need their own note type, card design, and sync path.

Additionally: I often highlight without adding a personal note, because noting interrupts reading flow. The design must not require notes.

## 2. Goals

- Every unique orange highlight (by dedup key per §6) becomes exactly one Anki note. Re-highlights of the same phrase merge into the existing note.
- Cards follow Fluent Forever principles: no English on the front, recognition-only (no production cards), minimum-information per card.
- Personal notes are preserved verbatim when present, never required.
- Missing notes are compensated by an LLM-generated objective explanation — so every card has depth, with or without my input.
- Cloze target selection is automated but reviewable/editable at card-creation time.
- Pipeline is computationally cheap enough to run Haiku- or small-Sonnet-class LLM calls over every highlight.

## 3. Non-goals

- No pictures (too expensive for grammar-focused cards; the sentence itself is the visual anchor).
- No English → Spanish production cards. Recognition only.
- No sentence audio generation in this system — HyperTTS inside Anki handles TTS from the sentence field. (Possible future expansion.)
- No merging of source contexts across re-highlights. For phrases, one canonical context sentence is enough (unlike yellow's multi-context union).

## 4. Card design — locked decisions

### 4.1 Card type and direction
- Anki **Cloze** note type.
- Recognition only: Spanish sentence with blank → recall filled word(s), translation, and insight.

### 4.2 Cloze target
- LLM picks the cloze target(s) from the highlighted phrase within its context sentence.
- **Not** whole-chunk cloze. The LLM must select a focused span — the element that makes the highlight interesting.
- Multiple clozes allowed per note (`{{c1::…}} {{c2::…}}`), generating multiple cards from one note. Typical: 1 cloze. Occasionally: 2. Hard cap: 3.
- Each blank carries a short LLM-generated **Spanish-only micro-hint** via Anki's `{{c1::answer::hint}}` syntax. The hint exists to disambiguate between valid synonyms so the card is not failed for the wrong reason.
- The LLM must not emit literal `::` or `}}` sequences inside a cloze answer or hint — Anki's parser will break. Validation happens pre-insertion; see §9 error handling.
- I review and edit cloze targets at card-creation time before the note goes live.

### 4.3 Front (visible before flip)
- The cloze sentence, rendered by Anki from the `Cloze Sentence` field. Blanks with micro-hints.
- Nothing else. No English, no picture, no source tag visible.

### 4.4 Back (after flip)
- Revealed cloze answer(s).
- `Translation` — English of the full sentence.
- `Insight` — LLM-generated, Spanish-dominant with English glosses woven in. Tight: 1–2 lines.
- `Explanation` — LLM-generated, plain-English gloss of what the phrase/saying means and when a native speaker would use it. 1–2 sentences. Distinct from `Translation` (which renders the sentence) and from `Insight` (which frames the grammar). This is the fallback when the Spanish-dominant `Insight` is too dense to parse mid-review.
- `Alternatives` — 1–2 paraphrases in Spanish with terse English. Very tight. One line each max.
- `Personal Note` — verbatim from the CSV's `note_text` column when present. Blank when not. Never LLM-touched.
- `Source` — book + location footer.

Display order on the back is defined by the Anki card template, not this spec. The field list above is the set of fields that must appear; the template decides layout.

### 4.5 Insight style guide
- Spanish-dominant. English is used for quick comparison and glosses — e.g., to say "this is a literary use of future tense (≈ *you see*)" — not for full-sentence explanations.
- Objective/linguistic in tone: what construction is it, what does it do, where else would I see it.
- Examples of good insight content: "fixed expression," "colloquial filler," "subjunctive triggered by X," "dative of interest," "literary framing use of future."

### 4.5a Explanation style guide
- Plain English. No Spanish vocabulary except when quoting the phrase itself.
- Focuses on **meaning and situational use** of the saying, not grammar. Answer: "what does it mean, and when would I hear it?"
- 1–2 sentences. A learner scanning the back of a card should understand the phrase in under five seconds.
- Must not duplicate `Translation` (sentence-level) or `Insight` (grammar framing). If the phrase is non-idiomatic and the translation already conveys everything, `Explanation` should still add context — e.g., "Common tag question used to seek agreement, like English 'right?' or 'you know?'".
- Examples: "Common conversational filler used to soften a disagreement, similar to English 'look,' or 'the thing is.'"; "Fixed expression meaning 'just in case,' used before a preventive action."

### 4.6 Alternatives style guide
- 1–2 ways a native might rephrase the same thing.
- Very short — one line each, terse English annotation in parentheses.
- Example for *"Verás, en las excursiones me pasan cosas malas"*:
  - *"Mira, en las excursiones..."* — more casual
  - *"Te cuento: en las excursiones..."* — same function, different register

## 5. Note type — field spec

**Anki note type name**: `2.2. Spanish Phrases` — no trailing whitespace. Yellow's `"2.1. Spanish Picture Words "` has a trailing space that is treated as a historical accident, not a convention to propagate. Future note types follow the no-trailing-whitespace rule.

**Model**: Cloze

**Who creates the note type**: user, manually in Anki, before first sync. Follows the yellow precedent (`anki_bootstrap.py` handles pre-existing notes, not model creation). The sync pipeline does not call `createModel` via AnkiConnect. Field names must match this spec exactly — typos will cause silent sync failures similar to the trailing-whitespace incident captured in the yellow config's `_note_type_warning`. Recommended check after creation: run the sync in dry-run mode; it should find zero existing notes but confirm the model and fields resolve.

| Field | Purpose | Sync policy |
|---|---|---|
| `Cloze Sentence` | Context sentence with `{{cN::answer::hint}}` markup. Drives the front. | `create_only` |
| `Translation` | English of the full sentence. | `create_only` |
| `Insight` | LLM-generated linguistic note, mixed Spanish/English. | `create_only` |
| `Explanation` | LLM-generated plain-English gloss of meaning/usage. Sibling of `Insight`. | `create_only` |
| `Alternatives` | 1–2 tight paraphrases. | `create_only` |
| `Personal Note` | User's `note_text` from CSV. Union-merged across re-highlights. | `managed_bullet_union` |
| `Source` | Book + location, first-seen only. | `create_only` |
| `ID` | `LP-######` (P for phrase; padding 6). | `sync_internal` |
| `Previous Phrases` | Audit trail of prior phrase *keys* when the dedup key is edited. Parallel to yellow's `Previous Lemmas`. Not a log of prior `Cloze Sentence` values. | `sync_internal` |
| `Sync Metadata` | Same as yellow. | `sync_internal` |

**ID format**: per-note-type `id_format` block. `LP-` prefix + padding 6 for phrases, `LX-` prefix + padding 6 for yellow (unchanged). Counters are independent — `LP-000001` and `LX-000001` can coexist; the prefix differentiates them. Config shape must support per-note-type `id_format`.

**Sub-decks**:

| Role | Deck | Notes |
|---|---|---|
| New destination and default permanent home | `Intensive Spanish Deck::Cloze Spanish Deck` | New peer sub-deck. First home for all new phrase cloze notes; most live here forever with no automatic graduation to Finalized. |
| Veto target | `Intensive Spanish Deck::Unused Spanish Deck` | Shared with yellow. |
| Manual graduation target (optional) | `Intensive Spanish Deck::WIP Spanish Deck` | User moves a phrase note here when it needs editing or personal context. Shared with yellow. |
| Manual graduation target (optional) | `Intensive Spanish Deck::Finalized Spanish Deck` | User may promote here after review. Shared with yellow. |

`active_for_update` for phrase sync must include Cloze + WIP + Finalized — bullet-union updates to `Personal Note` should still land wherever the user has moved the note. `veto` = Unused. `Unused` and the manual graduation targets are shared across both pipelines; deck membership is resolved by note type, not deck name.

**Shared-deck query invariant**: because Unused/WIP/Finalized hold both yellow and phrase notes, every sync query must filter by *both* deck and note type — `deck:"..." note:"..."` — never deck-only. A deck-only query would return the other pipeline's notes and cross-contaminate updates. This is load-bearing; don't drop the note-type filter anywhere in the query path.

A single daily review cap is configured on the root `Intensive Spanish Deck` in Anki itself, applies across both note types. Anki-side concern, not in this spec.

**Tags**: same filename-driven scheme as yellow (source + color). Color tag will always be `orange` for this pipeline.

## 6. Dedup & merge behavior

**Key**: normalized phrase text. Normalization applies only for matching; the stored `Cloze Sentence` preserves original casing and punctuation.

**Normalization steps** (applied in this order):
1. Convert all Unicode whitespace (non-breaking space `U+00A0`, em-space, tab, etc.) to ASCII space.
2. Collapse runs of multiple spaces to a single space.
3. Trim leading/trailing whitespace.
4. Lowercase (casefold).

**Must-not rules**:
- **Do not fold accents.** *"sé"* vs *"se"* and *"mas"* vs *"más"* are semantically distinct in Spanish; collapsing them would create false-positive dedup collisions.
- **Do not strip punctuation.** Unlike yellow's word-level dedup, phrase keys preserve `¿`, `¡`, `.`, `!`, `?`, `…`, `,`, `;`, `:`, and quotation marks verbatim. Phrase cards are about *how* Spanish uses a construction, and punctuation carries meaning — *"¿Verdad?"* as a confirmation tag is a different phenomenon than *"verdad"* as a noun. Treating them as one key would collapse teachable distinctions. Trade-off: if the same phrase is highlighted once with a trailing period and once without, it becomes two notes; fine, the user can merge manually if needed.
- **Do not alter internal punctuation.**

**On re-highlight of the same phrase**:
- `Personal Note` unions (bullet-merge, same mechanism as `Auto-Generated Context` in yellow). Multiple highlights of the same phrase can accumulate personal notes over time.
- `Insight`, `Alternatives`, `Translation`: **first capture wins, do not regenerate**. These describe the phrase as a phenomenon, not any single occurrence — re-running the LLM would produce churn without learning value.
- `Cloze Sentence`: also first capture wins, for idempotency. Tied to the first occurrence's sentence. If a later occurrence has a better example, override via Anki edit, not by re-running sync.
- `Source`: first capture wins. Cross-book context breadth is an explicit non-goal (see §3).
- `Previous Phrases` tracks prior dedup *keys* — used when the phrase key itself is edited (e.g., typo correction), parallel to yellow's `Previous Lemmas`.

## 7. LLM enrichment — requirements (not prompt)

This section captures what the enrichment step must produce, not *how* to prompt it. Prompt design is pipeline work.

**Input**:
- `phrase` (CSV `lemma`)
- `context_sentence` — the first bullet in the CSV's `context_sentences` column, after splitting on `• `. The upstream pipeline (`enrich_highlights.py` / `translate_and_deduplicate.py`) orders context sentences by earliest occurrence in the source book, so "first bullet" = "earliest occurrence." If upstream ordering ever changes, this contract has to be revisited.
- optional `personal_note` (CSV `note_text`)

**Output** (structured JSON, strict schema):
- `cloze_spans`: list of 1–3 `(start, end)` index pairs within `context_sentence` to wrap in cloze markers. Indices are Python-`str` character offsets (Unicode code points), end-exclusive — the same semantics as Python slicing. Not byte offsets, not grapheme clusters. Spans must be non-overlapping, must not contain `::` or `}}`.
- `cloze_hints`: list of short Spanish-only hints, one per span, same length as `cloze_spans`.
- `insight`: 1–2 line mixed Spanish/English linguistic note.
- `explanation`: 1–2 sentence plain-English gloss of what the phrase means and when a native would use it. Distinct from `translation` and `insight` per §4.5a.
- `alternatives`: 1–2 tight paraphrases, each with a terse English annotation.
- `translation`: English of the full sentence.

**Constraints**:
- Model: default to Claude Haiku 4.5 (`claude-haiku-4-5-20251001`). Upgrade path to a small Sonnet if output quality proves insufficient. Price-per-call must remain negligible at the current highlight volume.
- Temperature: 0. Structured output enforced via strict JSON schema (tool use or structured outputs API, not free-form parsing).
- Must not regenerate for a phrase key that already exists in sync state (see §6).
- Prompt cache reuse: the system prompt / style guide should be held constant across calls to benefit from caching.

## 8. Review UX — in Anki

Review happens **in Anki, not before sync**. Every enriched phrase is pushed as a note; the reviewer curates after the fact. Rationale: the LLM output is good enough to trust as a first draft, and reviewing in Anki keeps the loop tight — the same surface the card will be studied on is the surface it's edited on.

Post-sync curation uses tools Anki already provides:
- **Edit a field**: open the note in the browser, edit `Cloze Sentence` / `Insight` / `Explanation` / `Alternatives` / `Translation` directly. `create_only` sync policies guarantee these edits survive any future re-sync.
- **Demote an unwanted card**: move the note to `Intensive Spanish Deck::Unused Spanish Deck`. The sync's `veto` rule prevents future re-creation of the same phrase key.
- **Promote / park**: move the note to `WIP Spanish Deck` (needs more work) or `Finalized Spanish Deck` (polished). Shared with yellow; note-type filtering keeps the two pipelines from crossing over.

**Source-of-truth after first creation**: Anki wins. Once a note exists, the CSV is no longer authoritative for any field except `Personal Note` (which union-merges on re-highlight). Re-running the sync on the same CSV does not clobber Anki-side edits — the `create_only` policies on `Cloze Sentence`, `Translation`, `Insight`, `Explanation`, `Alternatives`, `Source` enforce this.

**No pre-sync gate**: the enriched CSV has no `status` column. `enrich_phrases.py` emits a CSV; `phrase_sync.py` consumes every row of it unconditionally.

## 9. Open questions for pipeline phase

These are explicitly deferred from card design:

1. **Where does the LLM enrichment step live?** Inside `spanish_kindle_exports/` (near `enrich_highlights.py` and `translate_and_deduplicate.py`) or as a new stage in `anki_sync/`?
2. **Config file shape** — extend the existing `anki_sync_config.json` with a per-note-type config block (required by §5's per-note-type `id_format`, distinct sub-deck routing, and distinct field mappings), or split into two config files. Decision affects refactoring scope of the existing yellow config.
3. **LLM prompt design** — the actual prompt that turns `(phrase, context, optional note)` into the structured output in §7. Must cover the insight/alternatives style guides from §4.5 and §4.6.
4. **Review/edit step implementation** — §8. How much tooling is worth building here vs. editing CSVs directly.
5. **Sync state keying** — current state file keys by `LX-` IDs; needs to cleanly accommodate `LP-` in parallel. Likely just works because prefix-scoped, but needs verification.
6. **Idempotency of the enrichment step** — cache LLM outputs by phrase hash so re-running the pipeline on the same CSV doesn't re-bill.
7. **Error handling**. Concrete failure modes to cover:
   - LLM returns malformed JSON (should not happen with structured outputs, but guard anyway).
   - LLM picks `cloze_spans` that don't map to valid substrings of `context_sentence`.
   - LLM emits `::` or `}}` inside a cloze answer or hint — must reject and regenerate or drop the offending span.
   - `cloze_spans` and `cloze_hints` have mismatched lengths (§7 requires them to be equal).
   - LLM returns empty `insight`, empty `alternatives`, or a translation that's obviously identical to the Spanish.
   - Overlapping cloze spans.
   - Phrase not found within its own context sentence (CSV corruption or post-hoc edit).

## 10. Not in scope (explicit)

- Sentence audio generation (HyperTTS handles it).
- Pictures of any kind.
- Production-direction cards (English → Spanish).
- Cross-book context union for phrases.
- Auto-tagging insights by grammar category (future enhancement).
- Green and pink highlights — different color, different purpose. Future specs TBD under `docs/` (e.g., `docs/green_cards_spec.md`, `docs/pink_cards_spec.md`).
