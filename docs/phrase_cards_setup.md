# Phrase Cards — One-Time Anki Setup

Read this **before** running `enrich_phrases.py` or `phrase_sync.py`. The
pipeline expects an exact Anki configuration; a typo in a field name will
cause silent sync failures (same failure mode as the trailing-whitespace
incident captured in `anki_sync_config.json`'s `_note_type_warning`).

> **Migration note (added)**: an `Explanation` field has been added between
> `Insight` and `Alternatives`. If you created `2.2. Spanish Phrases` before
> this was introduced:
>
> 1. Manage Note Types → `2.2. Spanish Phrases` → Fields → **Add** a field
>    named exactly `Explanation` in the slot shown in §3 below.
> 2. Re-paste the updated back template from §4 to surface the new field.
> 3. The LLM cache at `anki_sync/state/phrase_enrichment_cache.json` treats
>    pre-existing entries as stale (they lack `explanation`) and will
>    re-bill on the next `enrich_phrases.py` run. This is expected —
>    roughly one Haiku call per unique phrase, one-time.

Everything below is a one-time setup. After this, the recurring flow is:

```
orange CSV  →  enrich_phrases.py  →  enriched CSV  →  phrase_sync.py --apply
                                                        ↓
                                                  review in Anki
```

There is no pre-sync review pass — all enriched rows go straight into Anki.
Review each card in the Anki browser after sync: edit fields in place (those
edits survive re-sync), or move unwanted notes to `Unused Spanish Deck`.

## 1. Create the sub-deck

In Anki, create:

```
Intensive Spanish Deck::Cloze Spanish Deck
```

This is the new permanent home for phrase cards. Most phrase notes will live
here forever (no graduation to Finalized — see spec §5). The deck must sit as
a **child of your existing `Intensive Spanish Deck`**, peer to `WIP`,
`Finalized`, and `Unused`.

Leave the existing `Unused`, `WIP`, and `Finalized` sub-decks in place;
phrase notes share them with yellow cards. The sync query filters by note
type, so there is no cross-contamination.

## 2. Create the note type

Tools → Manage Note Types → Add → **Clone: Cloze**.

Name the new note type **exactly**:

```
2.2. Spanish Phrases
```

(No trailing whitespace. This is deliberately *not* copying the historical
trailing space in `2.1. Spanish Picture Words `.)

## 3. Define fields

Manage Note Types → select `2.2. Spanish Phrases` → Fields.

Starting from the default Cloze fields (`Text`, `Back Extra`), **delete
both** and add these fields in this order. Names are case-sensitive and must
have no leading/trailing whitespace:

| # | Field name           | Notes |
|---|----------------------|-------|
| 1 | `Phrase`             | Hidden on the card. Stores the normalized dedup key. |
| 2 | `Cloze Sentence`     | The field Anki uses for `{{cloze:…}}`. Contains `{{cN::answer::hint}}` markup. |
| 3 | `Translation`        | English of the full sentence. |
| 4 | `Insight`            | Linguistic note, Spanish-dominant. |
| 5 | `Explanation`        | Plain-English gloss of what the phrase means + when to use it. Sibling of `Insight`. |
| 6 | `Alternatives`       | 1–2 Spanish paraphrases. |
| 7 | `Personal Note`      | Your `note_text` from the CSV. Bullet-union merged. |
| 8 | `Source`             | Book + location footer. |
| 9 | `ID`                 | `LP-000001` etc. Hidden on the card. |
| 10| `Previous Phrases`   | Hidden audit trail of prior dedup keys. |
| 11| `Sync Metadata`      | JSON blob. Hidden. |

Before saving, double-check by copy/pasting each name from this table — a
single stray space is invisible in the Anki UI and will show up later as
"Query returned 0 notes." The sync's error path explicitly surfaces
whitespace mismatches, so if something goes wrong the log will point at it.

## 4. Configure the Cloze card template

Manage Note Types → select `2.2. Spanish Phrases` → **Cards…**

Anki's Cloze model has exactly one card type (`Card 1`) — do not try to add
another; cloze cards are generated per `{{cN::…}}` marker, not per template.

### Front template

```
{{cloze:Cloze Sentence}}
```

Nothing else. No English, no source footer.

### Back template

```
{{cloze:Cloze Sentence}}

<hr id=answer>

<div class="translation">{{Translation}}</div>

{{#Explanation}}
<div class="explanation"><b>Meaning:</b> {{Explanation}}</div>
{{/Explanation}}

{{#Insight}}
<div class="insight"><b>Insight:</b> {{Insight}}</div>
{{/Insight}}

{{#Alternatives}}
<div class="alternatives"><b>Alt:</b> {{Alternatives}}</div>
{{/Alternatives}}

{{#Personal Note}}
<div class="personal-note">{{Personal Note}}</div>
{{/Personal Note}}

<div class="source">{{Source}}</div>
```

The `{{#Field}}…{{/Field}}` blocks are Anki's conditional syntax — they
suppress the section entirely when the field is empty, so a note with no
Personal Note won't render an empty "Personal Note:" row.

### Styling (optional, but improves review)

Paste into the **Styling** box:

```css
.card {
  font-family: arial, sans-serif;
  font-size: 20px;
  text-align: center;
  color: black;
  background: #f7f5ef;
}

.translation  { margin: 10px 0; color: #222; }
.explanation  { margin: 8px 0;  color: #333; font-size: 0.95em; }
.insight      { margin: 8px 0;  color: #555; font-size: 0.9em; }
.alternatives { margin: 8px 0;  color: #555; font-size: 0.9em; }
.personal-note{ margin: 8px 0;  color: #7a5c2e; font-size: 0.9em; font-style: italic; }
.source       { margin-top: 16px; color: #999; font-size: 0.75em; }

.cloze { color: #2469b3; font-weight: bold; }
```

## 5. HyperTTS (optional, recommended)

Spec §3 delegates sentence audio to HyperTTS inside Anki rather than
generating audio in this pipeline. Configure HyperTTS against the
`Cloze Sentence` field if you want per-card audio. It will play the Spanish
sentence (with cloze answers filled in) on the back of each card.

## 6. Verify before first enrichment

Before calling the LLM, confirm the sync can see an empty deck + note type:

```
python -m anki_sync.phrase_sync /dev/null --config anki_sync/anki_phrase_sync_config.json
```

You'll get "CSV not found" — that's fine, the argparse check fires first.
The useful check is:

```
python -c "
from anki_sync.anki_index import build_index
from anki_sync.ankiconnect import AnkiConnect
from anki_sync.config import load_config
cfg = load_config('anki_sync/anki_phrase_sync_config.json')
anki = AnkiConnect(allow_writes=False)
idx = build_index(anki, cfg)
print(f'Indexed {len(idx)} phrase notes.')
"
```

Expected: `Indexed 0 phrase notes.` If you see a `RuntimeError` about the
model name or whitespace, go back to step 2 and check the exact spelling.

## 7. Install the Anthropic SDK + set the API key

The enrichment step calls Claude Haiku 4.5. One-time:

```
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

## 8. Run the pipeline

Once setup passes:

```
# Stage 1 — enrich. Writes enriched_phrases/<stem>_phrases.csv.
python -m anki_sync.enrich_phrases \
    spanish_kindle_exports/by_color/2026-04-13-percy_jackson_orange.csv

# Stage 2 — dry-run plan:
python -m anki_sync.phrase_sync \
    enriched_phrases/2026-04-13-percy_jackson_orange_phrases.csv

# Stage 2 — apply:
python -m anki_sync.phrase_sync \
    enriched_phrases/2026-04-13-percy_jackson_orange_phrases.csv --apply
```

Every row in the enriched CSV becomes an Anki note — no pre-sync review
gate. Review happens in Anki afterward: edit fields directly (those edits
survive future re-sync thanks to `create_only`), or move unwanted cards to
`Unused Spanish Deck` (the sync's veto rule prevents re-creation).

Re-running `enrich_phrases.py` on the same source is free: results are cached
in `anki_sync/state/phrase_enrichment_cache.json` (keyed by phrase +
context). Re-running `phrase_sync.py` is also safe — the `create_only`
policies mean Anki-side edits to `Cloze Sentence`, `Insight`, `Explanation`,
etc. are never clobbered (spec §8).

## 9. Common pitfalls

- **"Query returned 0 notes" after adding notes manually**: the note-type
  name has invisible whitespace. See step 2. The sync error message will
  point this out explicitly.
- **"allowDuplicate=False" blocks a CREATE**: two phrases normalized to the
  same key exist in the CSV but got different `Phrase` field values through
  editing. Pick one dedup key and re-run.
- **"missing required columns"**: you passed the raw `by_color` CSV to
  `phrase_sync.py` instead of the enriched output. Run `enrich_phrases.py`
  first.
- **LLM cost concerns**: cache is keyed by `(phrase, context)`. Editing the
  enriched CSV does not invalidate the cache. Only a change to the highlight
  text or the context sentence re-bills.
