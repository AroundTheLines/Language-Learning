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

- Every orange highlight becomes exactly one Anki note.
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
- Multiple clozes allowed per note (`{{c1::…}} {{c2::…}}`), generating multiple cards from one note. Typical: 1 cloze. Occasionally: 2. Rarely: more.
- Each blank carries a short LLM-generated **Spanish-only micro-hint** via Anki's `{{c1::answer::hint}}` syntax. The hint exists to disambiguate between valid synonyms so the card is not failed for the wrong reason.
- I review and edit cloze targets at card-creation time before the note goes live.

### 4.3 Front (visible before flip)
- The cloze sentence, rendered by Anki from the `Cloze Sentence` field. Blanks with micro-hints.
- Nothing else. No English, no picture, no source tag visible.

### 4.4 Back (after flip)
- Revealed cloze answer(s).
- `Translation` — English of the full sentence.
- `Insight` — LLM-generated, Spanish-dominant with English glosses woven in. Tight: 1–2 lines.
- `Alternatives` — 1–2 paraphrases in Spanish with terse English. Very tight. One line each max.
- `Personal Note` — verbatim from the CSV's `note_text` column when present. Blank when not. Never LLM-touched.
- `Source` — book + location footer.

### 4.5 Insight style guide
- Spanish-dominant. English is used for quick comparison and glosses — e.g., to say "this is a literary use of future tense (≈ *you see*)" — not for full-sentence explanations.
- Objective/linguistic in tone: what construction is it, what does it do, where else would I see it.
- Examples of good insight content: "fixed expression," "colloquial filler," "subjunctive triggered by X," "dative of interest," "literary framing use of future."

### 4.6 Alternatives style guide
- 1–2 ways a native might rephrase the same thing.
- Very short. Example for *"Verás, en las excursiones me pasan cosas malas"*:
  - *"Mira, en las excursiones..."* (more casual)
  - *"Te cuento: en las excursiones..."* (same function, different register)

## 5. Note type — field spec

**Anki note type name**: `2.2. Spanish Phrases`
**Model**: Cloze

| Field | Purpose | Sync policy |
|---|---|---|
| `Cloze Sentence` | Context sentence with `{{cN::answer::hint}}` markup. Drives the front. | `create_only` |
| `Translation` | English of the full sentence. | `create_only` |
| `Insight` | LLM-generated linguistic note, mixed Spanish/English. | `create_only` |
| `Alternatives` | 1–2 tight paraphrases. | `create_only` |
| `Personal Note` | User's `note_text` from CSV. Union-merged across re-highlights. | `managed_bullet_union` |
| `Source` | Book + location, first-seen only. | `create_only` |
| `ID` | `LP-######` (P for phrase; padding 6 to match `LX-`). | `sync_internal` |
| `Previous Phrases` | Audit trail of prior `Cloze Sentence` values if manually edited. | `sync_internal` |
| `Sync Metadata` | Same as yellow. | `sync_internal` |

**Sub-decks**: mirror yellow's layout — `Intensive Spanish Deck::WIP Spanish Phrases` for new, `::Finalized Spanish Phrases` once graduated, `::Unused Spanish Phrases` for vetoes.

**Tags**: same filename-driven scheme as yellow (source + color). Color tag will always be `orange` for this pipeline.

## 6. Dedup & merge behavior

**Key**: normalized phrase text — trim, lowercase, strip trailing punctuation (`.`, `!`, `?`, `…`). Normalization applies only for matching; the stored `Cloze Sentence` preserves original casing and punctuation.

**On re-highlight of the same phrase**:
- `Personal Note` unions (bullet-merge, same mechanism as `Auto-Generated Context` in yellow).
- All other fields: **first capture wins, do not regenerate**. Means the LLM does *not* re-run on an existing phrase even if the new highlight has a different context sentence.
- Rationale: for orange highlights the insight/alternatives/cloze are about the phrase as a phenomenon, not about any single occurrence. Re-running the LLM would produce churn without learning value.
- If I want a better cloze sentence later, I edit in Anki. `Previous Phrases` tracks that.

## 7. LLM enrichment — requirements (not prompt)

This section captures what the enrichment step must produce, not *how* to prompt it. Prompt design is pipeline work.

**Input**:
- `phrase` (CSV `lemma`)
- `context_sentence` (CSV `context_sentences`, first entry)
- optional `personal_note` (CSV `note_text`)

**Output** (structured, e.g. JSON):
- `cloze_spans`: list of 1–3 (start, end) indices within `context_sentence` to wrap in cloze markers.
- `cloze_hints`: list of short Spanish-only hints, one per span.
- `insight`: 1–2 line mixed Spanish/English linguistic note.
- `alternatives`: 1–2 tight paraphrases.
- `translation`: English of the full sentence.

**Constraints**:
- Model class: Haiku or cheap-Sonnet. Price-per-call must be negligible at the current highlight volume.
- Must not regenerate for a phrase key that already exists in sync state.
- Output must be deterministic enough to diff stably (for the review/edit step).

## 8. Review/edit UX — requirements

At card-creation time (before notes hit Anki), I want a lightweight review pass where I can:
- See the auto-generated `Cloze Sentence`, `Insight`, `Alternatives`, `Translation` side-by-side with the source.
- Edit any field inline.
- Approve → note is created. Skip → route to the "Unused" sub-deck.

Implementation is pipeline work. Could be a CSV edit loop, a TUI, or a pre-sync JSON file. Lowest-friction option preferred.

## 9. Open questions for pipeline phase

These are explicitly deferred from card design:

1. **Where does the LLM enrichment step live?** Inside `spanish_kindle_exports/` (near `enrich_highlights.py` and `translate_and_deduplicate.py`) or as a new stage in `anki_sync/`?
2. **Config file shape** — extend the existing `anki_sync_config.json` with a per-color config block, or split into two config files (one per note type)?
3. **LLM prompt design** — the actual prompt that turns `(phrase, context, optional note)` into the structured output in §7.
4. **Review/edit step implementation** — §8. How much tooling is worth building here vs. editing CSVs directly.
5. **Sync state keying** — current state file keys by `LX-` IDs; needs to cleanly accommodate `LP-` without collision. Likely just works, but needs verification.
6. **Idempotency of the enrichment step** — cache LLM outputs by phrase hash so re-running the pipeline on the same CSV doesn't re-bill.
7. **Error handling** — what happens when the LLM returns malformed JSON, picks a cloze span that doesn't exist in the sentence, or generates an empty insight.

## 10. Not in scope (explicit)

- Sentence audio generation (HyperTTS handles it).
- Pictures of any kind.
- Production-direction cards (English → Spanish).
- Cross-book context union for phrases.
- Auto-tagging insights by grammar category (future enhancement).
- Green and pink highlights — different spec, different purpose.
