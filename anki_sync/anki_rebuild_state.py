"""
anki_rebuild_state.py

Disaster recovery: rebuild anki_sync_state.json from scratch by reading
every managed note in Anki.

What we can fully recover from Anki:
    - ID                  ← from the ID field on each note
    - current_lemma       ← from the Word field
    - previous_lemmas     ← from the Previous Lemmas field (pipe-separated)
    - anki_note_id        ← from the AnkiConnect query
    - status              ← from the card's sub-deck:
                                active   if in active_for_update
                                vetoed   if in veto sub-deck
    - first_synced        ← from Sync Metadata JSON
    - first_source        ← from Sync Metadata JSON
    - last_synced         ← from Sync Metadata JSON

What we CANNOT recover:
    - Lemmas the user hard-deleted from Anki entirely. After rebuild,
      the next sync of a CSV containing such a lemma will recreate it.
      Workaround: prefer the "move to Unused sub-deck" workflow over
      hard delete, so the veto signal lives in Anki itself.

Usage:
    # Show what would be written, don't touch the file:
    python -m anki_sync.anki_rebuild_state

    # Replace the state file:
    python -m anki_sync.anki_rebuild_state --apply

    # Write to a custom path (for review):
    python -m anki_sync.anki_rebuild_state --apply --out /tmp/state.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.ankiconnect import AnkiConnect, AnkiConnectError
    from anki_sync.anki_index import AnkiIndex, build_index
    from anki_sync.config import Config, load_config
else:
    from .ankiconnect import AnkiConnect, AnkiConnectError
    from .anki_index import AnkiIndex, build_index
    from .config import Config, load_config


def rebuild(index: AnkiIndex, cfg: Config) -> dict:
    """Construct a fresh state dict from the Anki index."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries: dict[str, dict] = {}
    max_n = 0
    skipped_no_id = 0

    active_decks = set(cfg.active_for_update)

    for record in index.by_note_id.values():
        if not record.has_id:
            # No ID = the bootstrap pass never ran (or this is a card created
            # outside the sync system). Can't include it in state until it
            # has an ID. Surface a count so the user knows.
            skipped_no_id += 1
            continue
        id_value = record.id_value
        if id_value in entries:
            raise RuntimeError(
                f"Duplicate ID {id_value!r} found in Anki: notes "
                f"{entries[id_value]['anki_note_id']} and {record.note_id}. "
                "Resolve before rebuilding."
            )

        # Status from sub-deck membership.
        if record.deck == cfg.veto:
            status = "vetoed"
        elif record.deck in active_decks:
            status = "active"
        else:
            # Card is in some other deck under deck_root we don't know about.
            # Treat as active for safety; the user can adjust.
            status = "active"

        meta = record.sync_metadata or {}
        entries[id_value] = {
            "current_lemma": record.word,
            "previous_lemmas": list(record.previous_lemmas),
            "anki_note_id": record.note_id,
            "first_synced": meta.get("first_synced", today),
            "first_source": meta.get("first_source", ""),
            "last_synced": meta.get("last_synced", today),
            "status": status,
        }

        # Track max numeric portion for next_id.
        try:
            n = int(id_value.removeprefix(cfg.id_prefix))
            if n > max_n:
                max_n = n
        except ValueError:
            pass

    state_dict = {
        "version": 1,
        "next_id": max_n + 1,
        "id_prefix": cfg.id_prefix,
        "id_padding": cfg.id_padding,
        "last_sync": now_iso,
        "rebuilt_at": now_iso,
        "rebuild_skipped_no_id": skipped_no_id,
        "entries": dict(sorted(entries.items())),
    }
    return state_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="Write the rebuilt state file (default: dry-run preview)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Override output path (default: cfg.state_file)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    output_path = args.out or cfg.state_file

    anki_ro = AnkiConnect(allow_writes=False)
    try:
        index = build_index(anki_ro, cfg)
    except AnkiConnectError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Indexed {len(index)} notes from {cfg.deck_root!r} ({cfg.note_type!r}).")
    state = rebuild(index, cfg)
    print(f"Rebuilt {len(state['entries'])} state entries; next_id = {state['next_id']}.")
    if state["rebuild_skipped_no_id"]:
        print(
            f"WARNING: {state['rebuild_skipped_no_id']} note(s) have no ID "
            "and were not included. Run anki_bootstrap to assign IDs first."
        )

    # Stats
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for entry in state["entries"].values():
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        by_source[entry.get("first_source", "")] = by_source.get(entry.get("first_source", ""), 0) + 1
    print("\nBy status:")
    for k, v in sorted(by_status.items()):
        print(f"  {k:<12s}  {v}")
    print("\nBy first_source:")
    for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {k or '(unknown)':<24s}  {v}")

    if not args.apply:
        print(f"\n[dry-run] Would write to {output_path}")
        print("Re-run with --apply to overwrite the state file.")
        # Show first 3 entries for sanity.
        print("\nFirst 3 entries (preview):")
        for k, v in list(state["entries"].items())[:3]:
            print(f"  {k}: {v}")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        # Back up old state alongside.
        backup = output_path.with_suffix(
            output_path.suffix + f".backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        )
        output_path.rename(backup)
        print(f"\nBacked up existing state → {backup}")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote rebuilt state → {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
