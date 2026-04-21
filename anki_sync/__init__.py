"""anki_sync — bidirectional sync between Kindle-derived CSVs and Anki.

Modules:
  ankiconnect          — HTTP client for the AnkiConnect add-on, with explicit
                         read/write action whitelists.
  bullet_merge         — section-aware bullet-union for the Auto-Generated
                         Context field.
  state                — state file (anki_sync_state.json) management.
  config               — config loader for anki_sync_config.json.

Scripts:
  anki_discover        — read-only introspection of the Anki collection.
  anki_bootstrap       — one-time migration: assign IDs to existing notes and
                         audit Auto-Generated Context for hand-edits.
  anki_sync            — main sync for the yellow (word) pipeline.
  enrich_phrases       — stage 1 of the orange (phrase) pipeline: LLM
                         enrichment into a reviewable CSV.
  phrase_sync          — stage 2 of the orange pipeline: sync reviewed
                         enriched CSV into Anki.
  anki_rebuild_state   — disaster recovery: rebuild state file from Anki.
"""
