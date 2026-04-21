"""
phrase_sync.py

Stage 2 of the phrase (orange) pipeline: read an enriched-phrases CSV and
drive the same plan/apply logic used by anki_sync.py, but against the phrase
note type and decks.

Review happens in Anki, not before sync. Every row in the enriched CSV is
pushed as a note; after sync, edit the notes directly or move them to
Unused / WIP / Finalized sub-decks.

Usage
-----
    # Dry run against an enriched CSV:
    python -m anki_sync.phrase_sync \\
        enriched_phrases/2026-04-13-percy_jackson_orange_phrases.csv

    # Apply for real:
    python -m anki_sync.phrase_sync <enriched_csv> --apply

    # Show field-level before/after for one ID:
    python -m anki_sync.phrase_sync <enriched_csv> --diff LP-000007

The source_stem column on each row drives the filename-style tags
(`<source>`, `orange`) — this lets a single enriched CSV aggregate phrases
from several source files without losing the per-row tag provenance.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.ankiconnect import AnkiConnect, AnkiConnectError
    from anki_sync.anki_index import build_index
    from anki_sync.anki_sync import (
        _print_diff,
        _print_plan,
        apply_plan,
        plan,
    )
    from anki_sync.config import load_config, parse_filename
    from anki_sync.state import State
else:
    from .ankiconnect import AnkiConnect, AnkiConnectError
    from .anki_index import build_index
    from .anki_sync import (
        _print_diff,
        _print_plan,
        apply_plan,
        plan,
    )
    from .config import load_config, parse_filename
    from .state import State


DEFAULT_CONFIG = Path(__file__).parent / "anki_phrase_sync_config.json"


def _load_rows(csv_path: Path) -> list[dict]:
    """Read the enriched CSV.

    Validates that required columns exist so enrichment schema drift fails
    loudly rather than silently producing empty Anki notes.
    """
    # All columns the sync reads from (either directly, or as fields mapped
    # into Anki). Missing `explanation` here was a real hazard: an
    # older-schema CSV would pass validation and silently create notes with
    # an empty Explanation field.
    required = {
        "source_stem",
        "lemma",
        "cloze_sentence",
        "translation",
        "insight",
        "explanation",
        "alternatives",
        "personal_note",
        "source",
    }
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RuntimeError(f"{csv_path}: no header row")
        missing = required - set(reader.fieldnames)
        if missing:
            raise RuntimeError(
                f"{csv_path}: missing required columns {sorted(missing)}. "
                "Did you pass a raw by_color CSV instead of an enriched one? "
                "Run enrich_phrases first."
            )
        return list(reader)


def _group_by_source(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by source_stem so each tag-batch is planned with its own
    (source, color) tag pair."""
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        stem = (row.get("source_stem") or "").strip()
        if not stem:
            # Row with no stem gets a pseudo-key so we still plan it, but
            # without source/color tags.
            stem = ""
        out[stem].append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync an enriched-phrases CSV into Anki."
    )
    parser.add_argument("csv", type=Path, help="Enriched phrases CSV.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually mutate Anki (default: dry-run)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"Phrase sync config (default: {DEFAULT_CONFIG.name})")
    parser.add_argument("--diff", metavar="ID", default=None,
                        help="Show field-level before/after for one ID")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    rows_to_sync = _load_rows(args.csv)

    print(f"Enriched CSV:   {args.csv}")
    print(f"Config:         {args.config.name}")
    print(f"Rows:           {len(rows_to_sync)}")
    if not rows_to_sync:
        print("\nNo rows in CSV. Nothing to do.")
        return 0

    state = State(cfg.state_file, id_prefix=cfg.id_prefix, id_padding=cfg.id_padding)

    anki_ro = AnkiConnect(allow_writes=False)
    try:
        index = build_index(anki_ro, cfg)
    except AnkiConnectError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Plan per source_stem batch, aggregating results.
    all_row_plans = []
    all_ghost_plans = []
    per_batch_source_tags: list[tuple[str, str]] = []
    grouped = _group_by_source(rows_to_sync)
    for stem, rows in grouped.items():
        source_tag = ""
        color_tag = ""
        if stem:
            try:
                parsed = parse_filename(stem, cfg)
                source_tag = parsed["source"]
                color_tag = parsed["color"]
            except ValueError as e:
                print(
                    f"WARNING: source_stem {stem!r} doesn't match "
                    f"filename_parser.regex — tags will be skipped. ({e})",
                    file=sys.stderr,
                )
        per_batch_source_tags.append((source_tag, color_tag))
        row_plans, ghost_plans = plan(cfg, state, index, rows, source_tag, color_tag)
        all_row_plans.extend(row_plans)
        all_ghost_plans.extend(ghost_plans)

    primary_source = per_batch_source_tags[0][0] if per_batch_source_tags else ""
    primary_color = per_batch_source_tags[0][1] if per_batch_source_tags else ""
    _print_plan(cfg, args.csv, primary_source, primary_color,
                all_row_plans, all_ghost_plans, args.verbose)

    if args.diff:
        _print_diff(cfg, index, all_row_plans, args.diff)

    if not args.apply:
        print("\n[dry-run] No changes written. Re-run with --apply to execute.")
        return 0

    print("\nApplying changes...")
    anki_rw = AnkiConnect(allow_writes=True)
    try:
        # Pass the first batch's source as the "sync source" used in state
        # entries; per-row source/color tags were baked into each plan at
        # plan() time.
        apply_plan(cfg, state, anki_rw, all_row_plans, all_ghost_plans,
                   primary_source)
    except AnkiConnectError as e:
        print(f"\nERROR during apply: {e}", file=sys.stderr)
        print("State file was NOT saved. Re-run after resolving the error.")
        return 1
    print(f"Done. State saved to {cfg.state_file}")
    print(
        "Review the new notes in Anki. Move unwanted cards to "
        "`Unused Spanish Deck` and edits you want preserved on re-sync are "
        "already safe — `create_only` policies prevent clobbering."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
