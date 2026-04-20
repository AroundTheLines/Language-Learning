"""
anki_bootstrap.py

One-time migration: prepare existing Anki notes for sync.

Two passes:

  Pass 1 — Audit Auto-Generated Context for hand-edits.
      Scans every managed note and flags ones whose Auto-Generated Context
      contains content that doesn't fit the standard pipeline shape
      (Section: header followed by bullets). Output is a CSV the user can
      walk through and migrate to `Personal Context` manually before any
      sync starts replacing the auto field.

  Pass 2 — Assign IDs and populate Sync Metadata.
      For every managed note that has an empty `ID` field, mint a new
      sequential ID (LX-NNNNNN) and write it. Also populate
      `Sync Metadata` with best-guess first_source from the note's tags
      (the `source_from_filename` tag like `percy_jackson`).

Both passes are dry-run by default. Pass 1 always runs. Pass 2 runs unless
--audit-only is given. Use --apply to actually write to Anki.

Usage
-----
    # See what bootstrap WOULD do (recommended first):
    python -m anki_sync.anki_bootstrap

    # Just the audit, no ID assignment plan:
    python -m anki_sync.anki_bootstrap --audit-only

    # Apply for real (after reviewing audit + any manual migration):
    python -m anki_sync.anki_bootstrap --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.ankiconnect import AnkiConnect, AnkiConnectError
    from anki_sync.anki_index import AnkiIndex, NoteRecord, build_index
    from anki_sync.bullet_merge import parse as parse_sections
    from anki_sync.config import Config, load_config
    from anki_sync.state import State
else:
    from .ankiconnect import AnkiConnect, AnkiConnectError
    from .anki_index import AnkiIndex, NoteRecord, build_index
    from .bullet_merge import parse as parse_sections
    from .config import Config, load_config
    from .state import State


# Sections we recognize as pipeline-generated. Anything else triggers an
# audit flag.
RECOGNIZED_SECTIONS = {"Context:", "Highlighted forms:"}

AUDIT_REASONS = {
    "non_standard_section": "Auto field contains a section header we don't recognize",
    "no_sections": "Auto field has content but no parseable sections (likely free text)",
    "trailing_text": "Auto field has bullets we can't attribute to a known section",
}

DEFAULT_AUDIT_OUTPUT = Path(__file__).parent / "logs" / "bootstrap_audit.csv"


# ---------------------------------------------------------------------------
# Pass 1 — audit
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _has_meaningful_text(s: str) -> bool:
    return bool(_HTML_TAG_RE.sub("", s or "").strip())


def audit_field(value: str) -> list[str]:
    """Return a list of audit-reason codes for one field value. Empty list
    means the field is fine."""
    if not _has_meaningful_text(value):
        return []
    sections = parse_sections(value)
    if not sections:
        return ["no_sections"]
    reasons: list[str] = []
    for header in sections:
        if header not in RECOGNIZED_SECTIONS:
            reasons.append("non_standard_section")
            break
    return reasons


def run_audit(index: AnkiIndex, cfg: Config, output_path: Path) -> int:
    """Write the audit CSV. Returns count of flagged notes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    flagged = []
    for record in index.by_note_id.values():
        auto_value = record.field_value("Auto-Generated Context")
        reasons = audit_field(auto_value)
        if reasons:
            flagged.append((record, reasons))

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["note_id", "id_value", "word", "deck", "reasons", "preview"])
        for record, reasons in flagged:
            preview = _HTML_TAG_RE.sub(
                " ", record.field_value("Auto-Generated Context")
            )[:200].replace("\n", " ")
            writer.writerow(
                [
                    record.note_id,
                    record.id_value or "",
                    record.word,
                    record.deck,
                    "|".join(reasons),
                    preview,
                ]
            )

    return len(flagged)


# ---------------------------------------------------------------------------
# Pass 2 — ID assignment
# ---------------------------------------------------------------------------

def _source_tag_for(record: NoteRecord) -> str:
    """Best-guess first_source from existing tags. Returns the first tag
    that doesn't look like a color name. Empty string if no good guess."""
    color_words = {"yellow", "orange", "pink", "green", "blue", "purple", "red"}
    for t in record.tags:
        if t.lower() in color_words:
            continue
        if t.lower() == "spanish":
            continue
        return t
    return ""


def plan_id_assignments(index: AnkiIndex, cfg: Config, state: State) -> list[dict]:
    """Returns a list of {note_id, new_id, word, source, current_metadata}
    for every note that needs an ID assigned."""
    # First, reserve IDs that already exist on cards (so we never reuse them).
    for record in index.by_note_id.values():
        if record.id_value:
            state.reserve_id(record.id_value)

    plans = []
    for record in index.by_note_id.values():
        if record.has_id:
            continue
        if record.deck == cfg.veto:
            # Skip vetoed cards; they don't need IDs.
            continue
        plans.append(
            {
                "note_id": record.note_id,
                "word": record.word,
                "deck": record.deck,
                "source": _source_tag_for(record),
                "tags": record.tags,
            }
        )
    # Mint IDs in deterministic order (by current_lemma) so reruns are stable.
    plans.sort(key=lambda p: (p["word"].casefold(), p["note_id"]))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for p in plans:
        p["new_id"] = state.mint_id()
        p["new_metadata"] = json.dumps(
            {
                "first_synced": today,
                "first_source": p["source"],
                "last_synced": today,
                "bootstrapped": True,
            },
            ensure_ascii=False, sort_keys=True,
        )
    return plans


def apply_id_assignments(
    cfg: Config,
    state: State,
    anki: AnkiConnect,
    index: AnkiIndex,
    plans: list[dict],
) -> None:
    for p in plans:
        anki.update_note_fields(
            p["note_id"],
            {
                cfg.id_field: p["new_id"],
                cfg.sync_metadata_field: p["new_metadata"],
            },
        )
        state.upsert_entry(
            p["new_id"],
            current_lemma=p["word"],
            anki_note_id=p["note_id"],
            first_source=p["source"],
            status="vetoed" if index.by_note_id[p["note_id"]].deck == cfg.veto else "active",
        )
    state.save()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to Anki (default: dry-run)")
    parser.add_argument("--audit-only", action="store_true",
                        help="Skip Pass 2 (ID assignment); only run audit")
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT_OUTPUT,
                        help=f"Where to write audit CSV (default: {DEFAULT_AUDIT_OUTPUT})")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    state = State(cfg.state_file, id_prefix=cfg.id_prefix, id_padding=cfg.id_padding)

    anki_ro = AnkiConnect(allow_writes=False)
    try:
        index = build_index(anki_ro, cfg)
    except AnkiConnectError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Indexed {len(index)} notes from deck {cfg.deck_root!r} (note type {cfg.note_type!r}).")

    # Pass 1 — audit
    print("\n─── Pass 1: audit Auto-Generated Context for hand-edits ───")
    flagged = run_audit(index, cfg, args.audit_output)
    print(f"  Flagged {flagged} note(s) → {args.audit_output}")
    if flagged:
        print("  Review the CSV. For each flagged note, decide whether to:")
        print("    a) move the hand-edited content to the `Personal Context` field, OR")
        print("    b) leave it (the next sync's bullet-union will dedup but may")
        print("       reorder/reformat hand-written prose alongside bullets).")

    if args.audit_only:
        return 0

    # Pass 2 — ID assignment
    print("\n─── Pass 2: assign IDs to existing cards ───")
    plans = plan_id_assignments(index, cfg, state)
    print(f"  {len(plans)} note(s) need IDs assigned.")
    already = sum(1 for r in index.by_note_id.values() if r.has_id)
    skipped_veto = sum(1 for r in index.by_note_id.values()
                       if not r.has_id and r.deck == cfg.veto)
    print(f"  {already} already have IDs; {skipped_veto} skipped (in veto sub-deck).")
    if plans:
        sample = plans[:5]
        print("\n  Sample assignments:")
        for p in sample:
            print(f"    {p['new_id']}  ←  {p['word']:<25s}  (source guess: {p['source']!r}, deck: {p['deck']})")
        if len(plans) > 5:
            print(f"    … and {len(plans) - 5} more")

    if not args.apply:
        print("\n[dry-run] No changes written. Re-run with --apply to execute Pass 2.")
        return 0

    print("\nApplying ID assignments...")
    anki_rw = AnkiConnect(allow_writes=True)
    try:
        apply_id_assignments(cfg, state, anki_rw, index, plans)
    except AnkiConnectError as e:
        print(f"\nERROR during apply: {e}", file=sys.stderr)
        return 1
    print(f"Done. {len(plans)} IDs assigned; state saved to {cfg.state_file}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
