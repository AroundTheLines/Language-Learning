# languages — Kindle → Anki pipeline

Turn Kindle highlights into Anki flashcards that follow Fluent Forever
principles. Two parallel pipelines by highlight color:

- **Yellow** → `2.1. Spanish Picture Words ` — single words / vocabulary.
  Picture, IPA, gender, bullet-merged context across re-highlights.
- **Orange** → `2.2. Spanish Phrases` — phrases, idioms, and grammar
  curiosities. Cloze deletion on a context sentence, LLM-generated linguistic
  insight, alternatives, translation.

Green and pink are captured in CSV but don't yet have an Anki destination
(spec work TBD).

---

## Contents

- [One-time setup](#one-time-setup)
- [The recurring pipeline](#the-recurring-pipeline)
  - [Step 1: Export Kindle highlights](#step-1-export-kindle-highlights)
  - [Step 2: Process highlights](#step-2-process-highlights)
  - [Step 3a: Yellow → Anki](#step-3a-yellow--anki)
  - [Step 3b: Orange → Anki (two sub-steps)](#step-3b-orange--anki-two-sub-steps)
- [Maintaining state over time](#maintaining-state-over-time)
- [Directory layout](#directory-layout)
- [Troubleshooting](#troubleshooting)

---

## One-time setup

### 1. System prerequisites

- **Anki** (desktop), with your collection containing the deck
  `Intensive Spanish Deck` and these sub-decks:
  - `Intensive Spanish Deck::WIP Spanish Deck`
  - `Intensive Spanish Deck::Finalized Spanish Deck`
  - `Intensive Spanish Deck::Unused Spanish Deck`
  - `Intensive Spanish Deck::Cloze Spanish Deck`   ← new, for phrase cards
- **AnkiConnect** add-on installed (add-on code `2055492159`). It listens on
  `http://127.0.0.1:8765` when Anki is running.
- **Python 3.10+**.

### 2. Note types (manual, in Anki)

| Pipeline | Note type                     | Setup doc                         |
|----------|-------------------------------|-----------------------------------|
| Yellow   | `2.1. Spanish Picture Words ` | already exists in your collection |
| Orange   | `2.2. Spanish Phrases`        | [docs/phrase_cards_setup.md](docs/phrase_cards_setup.md) |

The trailing space on the yellow note type is intentional — historical
accident, not a convention to propagate. The phrase note type has no trailing
space. Both are case-sensitive; a single invisible whitespace mismatch
between the note name in Anki and the name in the configs will cause
"Query returned 0 notes" errors (the sync tells you exactly what to check).

Before running any sync, verify both note types are discoverable:

```bash
python -c "
from anki_sync.anki_index import build_index
from anki_sync.ankiconnect import AnkiConnect
from anki_sync.config import load_config
for path in ('anki_sync/anki_sync_config.json',
             'anki_sync/anki_phrase_sync_config.json'):
    cfg = load_config(path)
    idx = build_index(AnkiConnect(), cfg)
    print(f'{path}: indexed {len(idx)} notes ({cfg.note_type!r})')
"
```

Both should return without error (the counts may be zero on first setup).

### 3. Python dependencies

```bash
pip install ebooklib beautifulsoup4       # EPUB reading for context sentences
pip install deepl python-dotenv           # Spanish→English translation
pip install spacy stanza                  # lemmatization, word types
python -m spacy download es_core_news_sm
python -c "import stanza; stanza.download('es')"
pip install epitran                       # IPA for Spanish
pip install anthropic                     # LLM enrichment for orange phrases
```

A `requirements.txt` is not yet checked in; the list above is authoritative.

### 4. API keys

Two services are used:

- **DeepL** — translations. Free tier gives 500k chars/month.
  https://www.deepl.com/pro-api
- **Anthropic** — Claude Haiku 4.5 for phrase enrichment.
  https://console.anthropic.com/

Put them in a `.env` at the repo root (gitignored):

```
DEEPL_API_KEY=...
ANTHROPIC_API_KEY=sk-ant-...
```

The yellow pipeline loads `.env` via `python-dotenv`. The orange pipeline
reads `ANTHROPIC_API_KEY` from the environment; if you use `.env`, source it
before running (`set -a; source .env; set +a`) or export the key manually.

### 5. Books

Drop each source book as an EPUB into `books/` (gitignored). Filename is
free-form; you pass the exact path on the command line.

---

## The recurring pipeline

After finishing a reading session (or whenever you've accumulated enough
highlights to be worth processing), repeat these steps.

### Step 1: Export Kindle highlights

Open the book's notebook on Kindle Cloud Reader:

1. Go to either:
   - `https://read.amazon.com/` → open book → Notebook (older layout), or
   - `https://read.amazon.ca/notebook` (newer notebook layout).
2. Open the browser dev console (⌥⌘I on macOS).
3. Paste **one** of the scripts depending on which page you're on:
   - [spanish_kindle_exports/export_kindle_highlights.js](spanish_kindle_exports/export_kindle_highlights.js) for the main reader page.
   - [spanish_kindle_exports/export_notebook_highlights.js](spanish_kindle_exports/export_notebook_highlights.js) for the notebook page.
4. Each script downloads a CSV of all highlights (color, text, note, page/location).
5. Rename the download to the conventional date-prefixed form and drop it in
   `spanish_kindle_exports/highlight_csvs/`:

   ```
   spanish_kindle_exports/highlight_csvs/YYYY-MM-DD-<source>.csv
   ```

   e.g. `2026-04-13-percy_jackson.csv`. The date prefix becomes the import
   date; `<source>` becomes the Anki source tag. Use `snake_case`, no spaces.

### Step 2: Process highlights

One script does three stages: add context sentences from the EPUB, translate
and add IPA/gender via DeepL + spaCy, and split by color.

```bash
cd spanish_kindle_exports
python process_highlights.py \
    highlight_csvs/2026-04-13-percy_jackson.csv \
    ../books/"Percy Jackson y los dioses del - Rick Riordan.epub"
```

Outputs:

- `enriched/<stem>_enriched.csv` — every highlight with its sentence context
- `translated/<stem>_translated.csv` — deduplicated, translated, IPA, gender
- `by_color/<stem>_<color>.csv` — one file per color (yellow/orange/pink/green)

You can also pass `--sync-to-anki [--apply]` to chain the yellow sync
immediately; see `process_highlights.py --help`.

**Re-running is safe.** The pipeline is deterministic given the same inputs;
output files are overwritten. DeepL and spaCy don't cache in this repo, so
re-running re-translates — only re-run when the input actually changed.

### Step 3a: Yellow → Anki

```bash
# Dry-run:
python -m anki_sync.anki_sync \
    spanish_kindle_exports/by_color/2026-04-13-percy_jackson_yellow.csv

# Apply:
python -m anki_sync.anki_sync \
    spanish_kindle_exports/by_color/2026-04-13-percy_jackson_yellow.csv --apply
```

What it does (per CSV row):

- **New word** → mints an `LX-NNNNNN` ID, creates a note in the WIP sub-deck.
- **Known word** (found in state or by ID in Anki) → bullet-merges new
  context into `Auto-Generated Context`; other fields are `create_only` and
  not touched.
- **Renamed word** (lemma in CSV differs from Anki's `Word` field) → records
  the old value in `Previous Lemmas`, updates `Word`.
- **Moved to `Unused`** → marked vetoed in state; the sync will not
  re-populate it in future runs.
- **Hard-deleted in Anki** → detected by absence, marked `hard_deleted` in
  state; the sync will refuse to recreate it.

Dry-run is the default. Always read the plan output before passing `--apply`.

### Step 3b: Orange → Anki (two sub-steps)

Phrase cards need LLM enrichment, then sync. Review happens in Anki, not in
a CSV.

**Sub-step 1 — enrich**. Calls Claude Haiku 4.5 once per unique phrase (with
a local cache, so re-runs are free). Runs calls in parallel; default 8
concurrent workers, tunable via `--concurrency`:

```bash
python -m anki_sync.enrich_phrases \
    spanish_kindle_exports/by_color/2026-04-13-percy_jackson_orange.csv
```

Output: `enriched_phrases/<stem>_phrases.csv` with columns for cloze markup,
translation, insight, explanation (plain English), alternatives, and
personal note.

**Sub-step 2 — sync**. Identical interface to yellow, but against the phrase
config. Every row in the enriched CSV is pushed as a note — no pre-sync
gate:

```bash
# Dry-run:
python -m anki_sync.phrase_sync \
    enriched_phrases/2026-04-13-percy_jackson_orange_phrases.csv

# Apply:
python -m anki_sync.phrase_sync \
    enriched_phrases/2026-04-13-percy_jackson_orange_phrases.csv --apply
```

**Review in Anki**, not in the CSV:

- Edit any field directly on the note in the Anki browser
  (`Cloze Sentence`, `Insight`, `Explanation`, `Alternatives`,
  `Translation`). Those edits survive future re-sync.
- Move unwanted notes to `Intensive Spanish Deck::Unused Spanish Deck` —
  the sync's veto rule prevents re-creation of the same phrase key.
- Move notes you want to polish further to `WIP Spanish Deck` or promote
  to `Finalized Spanish Deck` (shared with yellow; sync filters by note
  type so the two pipelines don't collide).

Design notes to be aware of:

- Once a phrase note exists, Anki is the source of truth — the CSV is no
  longer authoritative for `Cloze Sentence`, `Insight`, `Explanation`,
  `Alternatives`, `Translation`, or `Source`. You edit these in Anki.
  Re-running the sync on the same CSV won't clobber your edits
  (`create_only` policy).
- `Personal Note` is the exception: it bullet-unions across re-highlights.
  Multiple highlights of the same phrase accumulate notes over time.
- Phrase dedup keys preserve punctuation — `¿Verdad?` and `verdad` are
  different keys. See [docs/phrase_cards_spec.md](docs/phrase_cards_spec.md)
  §6 for why.

---

## Maintaining state over time

The sync keeps two small state files in `anki_sync/state/` (gitignored):

```
anki_sync/state/anki_sync_state.json          ← yellow
anki_sync/state/anki_phrase_sync_state.json   ← orange
anki_sync/state/phrase_enrichment_cache.json  ← LLM cache
```

They record which lemma/phrase maps to which Anki note ID, and the
rename/veto/delete history. **None of this is authoritative** — Anki is. The
state file is a speed cache for the sync and a record of hard-deletes (which
Anki can't tell us about otherwise).

### When things go wrong: rebuilding state

If a state file gets corrupted, accidentally deleted, or you switched
machines:

```bash
# Preview what would be reconstructed:
python -m anki_sync.anki_rebuild_state \
    --config anki_sync/anki_sync_config.json                     # yellow
python -m anki_sync.anki_rebuild_state \
    --config anki_sync/anki_phrase_sync_config.json              # orange

# Commit the rebuild (backs up the old file alongside):
python -m anki_sync.anki_rebuild_state \
    --config anki_sync/anki_phrase_sync_config.json --apply
```

The rebuild reads every managed note in Anki and pulls ID, `Word`/`Phrase`,
`Previous Lemmas`/`Previous Phrases`, and `Sync Metadata` back out.

**What rebuild cannot recover**: lemmas you *hard-deleted* from Anki.
Workaround: prefer the "move to `Unused Spanish Deck`" workflow over hard
delete. Moving keeps the note around as a veto signal the sync can see.

### Bootstrapping legacy cards

If you start managing a note type where cards already exist without IDs
(e.g. your original yellow cards before the sync system existed), run:

```bash
# Yellow — your cards already have IDs from the first bootstrap. Running
# again is safe; it'll find nothing to do.
python -m anki_sync.anki_bootstrap

# Orange — run once after creating the 2.2. Spanish Phrases note type and
# before your first enrichment run, if you've manually added any phrase
# notes in Anki.
python -m anki_sync.anki_bootstrap \
    --config anki_sync/anki_phrase_sync_config.json
```

Bootstrap does two passes: (1) audits the auto-managed context field for
hand-edits that would be overwritten, (2) mints IDs for existing notes.
Dry-run by default; pass `--apply` to commit.

### Day-to-day workflow in Anki

- **Veto a card**: move it to `Intensive Spanish Deck::Unused Spanish Deck`.
  The next sync will mark it vetoed and stop updating it.
- **Graduate a card**: move it to `WIP` when it needs editing, then
  `Finalized` when polished. Both decks are `active_for_update` — the sync
  continues bullet-merging new context into the note wherever it lives.
- **Hard delete**: only when you're sure. The sync detects it on the next
  run and records `hard_deleted` in state. A future re-import of the same
  lemma will not recreate the note unless you clear that record manually.
- **Rename a lemma**: edit the `Word` (yellow) or `Phrase` (orange) field in
  Anki. The next sync will detect the rename, append the old value to
  `Previous Lemmas`/`Previous Phrases`, and update state.

### Re-highlighting the same phrase or word

Safe. The sync looks up by ID first, state second, lemma/phrase third. New
context sentences bullet-merge into the existing note. LLM enrichment is
cached by `(phrase, context)` so a re-highlight of a phrase in the same
sentence is free; a re-highlight in a different sentence re-bills (but the
phrase-level enrichment fields — insight, alternatives, translation — are
`create_only` and won't regenerate).

---

## Directory layout

```
languages/
├── README.md                      (this file)
├── .env                           (gitignored; API keys)
├── books/                         (gitignored; source EPUBs)
├── docs/
│   ├── phrase_cards_spec.md       (design spec — orange pipeline)
│   └── phrase_cards_setup.md      (one-time Anki setup for 2.2. Spanish Phrases)
├── spanish_kindle_exports/
│   ├── export_kindle_highlights.js   (browser console script)
│   ├── export_notebook_highlights.js (browser console script)
│   ├── process_highlights.py      (enrich + translate + split)
│   ├── enrich_highlights.py       (EPUB context sentence extraction)
│   ├── translate_and_deduplicate.py (DeepL + spaCy + Stanza)
│   ├── split_by_color.py          (splits translated CSV per color)
│   ├── csv_to_ipa.py              (standalone IPA utility)
│   ├── highlight_csvs/            (gitignored; raw Kindle exports)
│   ├── enriched/                  (gitignored; Step 1 output)
│   ├── translated/                (gitignored; Step 2 output)
│   └── by_color/                  (gitignored; Step 3 input, color-split)
├── enriched_phrases/              (gitignored; orange enrichment output)
└── anki_sync/
    ├── anki_sync_config.json      (yellow config)
    ├── anki_phrase_sync_config.json (orange config)
    ├── anki_sync.py               (yellow sync CLI)
    ├── enrich_phrases.py          (orange stage 1 CLI)
    ├── phrase_sync.py             (orange stage 2 CLI)
    ├── anki_bootstrap.py          (one-time migration)
    ├── anki_rebuild_state.py      (disaster recovery)
    ├── anki_discover.py           (read-only deck introspection)
    ├── ankiconnect.py             (HTTP client with read/write whitelists)
    ├── anki_index.py              (one-shot in-memory index of managed notes)
    ├── config.py                  (config loader + validation)
    ├── state.py                   (state file mutations)
    ├── bullet_merge.py            (section-aware bullet union)
    ├── phrase_normalize.py        (phrase dedup key + cloze markup)
    ├── llm_enrich.py              (Claude Haiku 4.5 enrichment + cache)
    ├── state/                     (gitignored; runtime state + LLM cache)
    ├── logs/                      (gitignored; sync run logs)
    └── tests/
```

Gitignored outputs are regenerable from the committed code + your Kindle
highlights + Anki collection. The repo intentionally commits code only;
CSVs, EPUBs, Anki state, and API keys stay local.

---

## Troubleshooting

**"Query returned 0 notes" on first sync.** The note type name has invisible
whitespace (yellow's `2.1. Spanish Picture Words ` has a *trailing* space on
purpose; the phrase note type `2.2. Spanish Phrases` has *no* trailing
space). The error message prints the literal length of each near-match model
— use that to locate the rogue whitespace.

**`AnkiConnect` connection refused.** Anki must be open with the
AnkiConnect add-on installed and enabled. Check
`http://127.0.0.1:8765` returns a plain-text "AnkiConnect v.6" in a browser.

**LLM enrichment failures after retries.** Usually transient API errors. Run
`enrich_phrases.py` again — successful enrichments are cached, so only the
failed rows re-bill. For deterministic cache-only runs, pass `--offline`.

**A managed field was clobbered by the sync.** It shouldn't be possible for
`create_only` fields, which is the policy for most phrase fields and
yellow's translation/IPA/gender. For `managed_bullet_union` fields
(`Personal Note`, `Auto-Generated Context`), the sync unions in new CSV
content but preserves all existing bullets. If you see loss, check the
state file for an ID mismatch — the sync might be treating the note as new.
Run `anki_rebuild_state` to resync.

**Want to see what a sync will do for one specific ID before applying?** Use
`--diff`:

```bash
python -m anki_sync.anki_sync   <csv> --diff LX-000123
python -m anki_sync.phrase_sync <csv> --diff LP-000007
```

**Need to inspect what Anki actually holds for a deck?**

```bash
python -m anki_sync.anki_discover --deck "Intensive Spanish Deck"
```

Read-only; prints note-type breakdown, field fill-rates, tag frequency, and
sample rows.
