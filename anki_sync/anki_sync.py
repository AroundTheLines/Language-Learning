"""
anki_sync.py

Main sync command. Default is dry-run; pass --apply to mutate Anki.

Usage
-----
    # Dry-run plan against one by_color CSV:
    python -m anki_sync.anki_sync \\
        spanish_kindle_exports/by_color/2026-04-13-percy_jackson_yellow.csv

    # Apply for real:
    python -m anki_sync.anki_sync ... --apply

    # Show field-by-field before/after for a single ID (planning only):
    python -m anki_sync.anki_sync ... --diff LX-000087

Sync algorithm (per CSV row)
----------------------------
    Looking up the lemma uses, in order:

      1. State file by `current_lemma` or `previous_lemmas`  → known ID.
      2. State file says ID? Cross-check with Anki index by ID value.
      3. Otherwise look up by Word (lemma) in Anki — bootstrap path for
         legacy cards that pre-date the sync system.
      4. Otherwise it's genuinely new.

    Then per outcome:

      CREATE       — assign new ID, addNote() in WIP sub-deck, tag accordingly.
      UPDATE       — updateNoteFields() with managed fields only; addTags().
      RENAME       — Word in Anki ≠ CSV lemma; record old in `previous_lemmas`,
                     update field. Reported in dry-run.
      SKIP_VETOED  — note exists in Unused sub-deck; do nothing, mark vetoed
                     in state.
      SKIP_DELETED — state says hard_deleted; never recreate.
      DETECT_DEL   — state had a note ID but Anki no longer has the card.
                     Mark hard_deleted in state.
      BOOTSTRAP    — Anki has the lemma but no ID; assign one and update.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Support `python anki_sync/anki_sync.py ...` and `python -m anki_sync.anki_sync ...`.
if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.ankiconnect import AnkiConnect, AnkiConnectError
    from anki_sync.anki_index import AnkiIndex, NoteRecord, build_index
    from anki_sync.bullet_merge import merge as bullet_merge
    from anki_sync.config import Config, load_config, parse_filename
    from anki_sync.progress import Progress
    from anki_sync.state import State
else:
    from .ankiconnect import AnkiConnect, AnkiConnectError
    from .anki_index import AnkiIndex, NoteRecord, build_index
    from .bullet_merge import merge as bullet_merge
    from .config import Config, load_config, parse_filename
    from .progress import Progress
    from .state import State


# ---------------------------------------------------------------------------
# Plan: a per-row decision, computed without writing to Anki.
# ---------------------------------------------------------------------------

ACTION_CREATE = "CREATE"
ACTION_UPDATE = "UPDATE"
ACTION_BOOTSTRAP = "BOOTSTRAP"
ACTION_SKIP_VETOED = "SKIP_VETOED"
ACTION_SKIP_DELETED = "SKIP_DELETED"
ACTION_DETECT_DELETE = "DETECT_DELETE"
ACTION_NOOP = "NOOP"


@dataclass
class PlannedAction:
    action: str
    csv_row: dict
    lemma: str
    id_value: str | None = None
    note_id: int | None = None
    rename_from: str | None = None
    new_field_values: dict[str, str] = field(default_factory=dict)
    tags_to_add: list[str] = field(default_factory=list)
    note: str = ""  # human-readable explanation


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_csv_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Field value derivation
# ---------------------------------------------------------------------------

def _csv_to_html(s: str) -> str:
    """Convert CSV cell newlines to <br> for storage in Anki."""
    if not s:
        return ""
    # Normalize CR/LF to single \n then to <br>.
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _build_create_fields(
    cfg: Config, row: dict, id_value: str, source: str
) -> dict[str, str]:
    """Field map for a brand-new note. Includes managed + create_only fields,
    plus the system-internal fields. Picture / Personal Context / etc. are
    intentionally omitted (left blank in Anki)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metadata = json.dumps(
        {"first_synced": today, "first_source": source, "last_synced": today},
        ensure_ascii=False,
        sort_keys=True,
    )

    fields: dict[str, str] = {}
    for fp in cfg.field_policies:
        if fp.policy == "key":
            fields[fp.field_name] = (row.get(fp.csv_column, "") or "").strip()
        elif fp.policy == "create_only":
            value = (row.get(fp.csv_column, "") if fp.csv_column else "") or ""
            fields[fp.field_name] = _csv_to_html(value.strip())
        elif fp.policy == "managed_bullet_union":
            value = (row.get(fp.csv_column, "") if fp.csv_column else "") or ""
            # On creation, the merge is just "render the new content cleanly".
            fields[fp.field_name] = bullet_merge("", _csv_to_html(value))
        elif fp.policy == "sync_internal":
            if fp.field_name == cfg.id_field:
                fields[fp.field_name] = id_value
            elif fp.field_name == cfg.previous_lemmas_field:
                fields[fp.field_name] = ""
            elif fp.field_name == cfg.sync_metadata_field:
                fields[fp.field_name] = metadata
        # never_touch fields: omit. Anki creates them empty.
    return fields


def _build_update_fields(
    cfg: Config,
    row: dict,
    record: NoteRecord,
    rename_to: str | None,
) -> dict[str, str]:
    """Field map for updating an existing note. Includes ONLY:
      - managed fields (bullet-union merge with current Anki value)
      - the renamed Word field, if applicable
      - the previous_lemmas field, if a rename occurred
      - the sync_metadata field's last_synced bumped

    Notably excludes: create_only fields (translation, IPA, gender),
    never_touch fields, and the ID field (which is immutable post-creation).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out: dict[str, str] = {}

    for fp in cfg.field_policies:
        if fp.policy == "managed_bullet_union":
            new_value = (row.get(fp.csv_column, "") if fp.csv_column else "") or ""
            existing = record.field_value(fp.field_name)
            merged = bullet_merge(existing, _csv_to_html(new_value))
            if merged != existing:
                out[fp.field_name] = merged
        elif fp.policy == "key" and rename_to is not None:
            out[fp.field_name] = rename_to
        elif fp.policy == "sync_internal":
            if fp.field_name == cfg.previous_lemmas_field and rename_to is not None:
                # Append the old lemma to previous_lemmas (pipe-separated).
                prev = list(record.previous_lemmas)
                if record.word and record.word not in prev and record.word != rename_to:
                    prev.append(record.word)
                out[fp.field_name] = "|".join(prev)
            elif fp.field_name == cfg.sync_metadata_field:
                # Bump last_synced; preserve first_* fields.
                meta = dict(record.sync_metadata)
                meta["last_synced"] = today
                meta.setdefault("first_synced", today)
                # If first_source was unset (legacy bootstrap), don't overwrite blindly.
                meta.setdefault("first_source", "")
                out[fp.field_name] = json.dumps(
                    meta, ensure_ascii=False, sort_keys=True
                )
        # create_only and never_touch: never updated.

    return out


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def plan(
    cfg: Config,
    state: State,
    index: AnkiIndex,
    csv_rows: Iterable[dict],
    source_tag: str,
    color_tag: str,
) -> tuple[list[PlannedAction], list[PlannedAction]]:
    """Compute planned actions for each CSV row, plus a sweep of ghost-deletes
    (state-known IDs whose Anki notes have vanished).

    Returns (row_plans, ghost_plans).
    """
    row_plans: list[PlannedAction] = []
    ghost_plans: list[PlannedAction] = []

    base_tags = []
    if cfg.source_from_filename and source_tag:
        base_tags.append(source_tag)
    if cfg.color_from_filename and color_tag:
        base_tags.append(color_tag)
    base_tags.extend(cfg.extra_tags)

    veto_deck = cfg.veto
    active_decks = set(cfg.active_for_update)

    for row in csv_rows:
        lemma = (row.get("lemma") or "").strip()
        if not lemma:
            continue

        # 1. State lookup
        id_value, entry = state.find_by_lemma(lemma)

        # 1a. State knows it's hard-deleted
        if entry and entry.get("status") == "hard_deleted":
            row_plans.append(
                PlannedAction(
                    ACTION_SKIP_DELETED,
                    csv_row=row,
                    lemma=lemma,
                    id_value=id_value,
                    note=f"state says hard-deleted (was {entry.get('current_lemma')!r})",
                )
            )
            continue

        record: NoteRecord | None = None
        if id_value:
            record = index.find_by_id_value(id_value)
            if record is None:
                # State had it; Anki doesn't. Hard-delete detected.
                ghost_plans.append(
                    PlannedAction(
                        ACTION_DETECT_DELETE,
                        csv_row=row,
                        lemma=lemma,
                        id_value=id_value,
                        note=(
                            f"state had {id_value} → note "
                            f"{entry.get('anki_note_id')} but Anki has no such note"
                        ),
                    )
                )
                # Treat as deleted for THIS row too (don't recreate).
                row_plans.append(
                    PlannedAction(
                        ACTION_SKIP_DELETED,
                        csv_row=row,
                        lemma=lemma,
                        id_value=id_value,
                        note="(card was hard-deleted in Anki since last sync)",
                    )
                )
                continue

        # 2. No state hit — try Anki by Word (bootstrap)
        if record is None:
            record = index.find_by_lemma(lemma)
            if record is not None:
                if record.has_id:
                    # Anki already has an ID we don't know about (state was wiped or rebuilt).
                    id_value = record.id_value
                else:
                    id_value = state.mint_id()
                # Treat as bootstrap: assign ID + populate metadata + run normal update.
                _record_planned_update(
                    cfg, row, record, base_tags, lemma, id_value, source_tag,
                    action=ACTION_BOOTSTRAP, row_plans=row_plans,
                    veto_deck=veto_deck, active_decks=active_decks,
                )
                continue

        # 3. Have neither state nor Anki record → CREATE
        if record is None:
            new_id = id_value or state.mint_id()
            new_fields = _build_create_fields(cfg, row, new_id, source_tag)
            row_plans.append(
                PlannedAction(
                    ACTION_CREATE,
                    csv_row=row,
                    lemma=lemma,
                    id_value=new_id,
                    new_field_values=new_fields,
                    tags_to_add=list(base_tags),
                    note=f"new lemma → assigned {new_id}, will create in {cfg.new_destination}",
                )
            )
            continue

        # 4. Have an existing record (matched by ID). Decide UPDATE vs SKIP.
        _record_planned_update(
            cfg, row, record, base_tags, lemma, id_value, source_tag,
            action=ACTION_UPDATE, row_plans=row_plans,
            veto_deck=veto_deck, active_decks=active_decks,
        )

    return row_plans, ghost_plans


def _record_planned_update(
    cfg: Config,
    row: dict,
    record: NoteRecord,
    base_tags: list[str],
    lemma: str,
    id_value: str,
    source_tag: str,
    *,
    action: str,
    row_plans: list[PlannedAction],
    veto_deck: str,
    active_decks: set[str],
) -> None:
    if record.deck == veto_deck:
        row_plans.append(
            PlannedAction(
                ACTION_SKIP_VETOED,
                csv_row=row,
                lemma=lemma,
                id_value=id_value,
                note_id=record.note_id,
                note=f"in {veto_deck} → soft-deleted, will not modify",
            )
        )
        return
    if record.deck not in active_decks:
        row_plans.append(
            PlannedAction(
                ACTION_NOOP,
                csv_row=row,
                lemma=lemma,
                id_value=id_value,
                note_id=record.note_id,
                note=f"note is in deck {record.deck!r}, not in active_for_update; skipping",
            )
        )
        return

    # Detect rename
    rename_from: str | None = None
    if record.word and record.word != lemma:
        rename_from = record.word

    fields_to_update = _build_update_fields(cfg, row, record, rename_to=lemma if rename_from else None)
    tags_missing = [t for t in base_tags if t and t not in record.tags]

    # Build a final action object. For BOOTSTRAP, we ALSO need to set ID and
    # populate Sync Metadata if missing.
    if action == ACTION_BOOTSTRAP:
        if not record.has_id:
            fields_to_update[cfg.id_field] = id_value
        if not record.sync_metadata:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            fields_to_update[cfg.sync_metadata_field] = json.dumps(
                {"first_synced": today, "first_source": source_tag, "last_synced": today, "bootstrapped": True},
                ensure_ascii=False, sort_keys=True,
            )

    note_msg_parts = []
    if rename_from:
        note_msg_parts.append(f"RENAME: {rename_from!r} → {lemma!r}")
    if fields_to_update:
        note_msg_parts.append(
            f"updating {sorted(fields_to_update.keys())}"
        )
    else:
        note_msg_parts.append("no field changes")
    if tags_missing:
        note_msg_parts.append(f"adding tags {tags_missing}")

    final_action = action
    if action == ACTION_UPDATE and not fields_to_update and not tags_missing and not rename_from:
        final_action = ACTION_NOOP

    row_plans.append(
        PlannedAction(
            final_action,
            csv_row=row,
            lemma=lemma,
            id_value=id_value,
            note_id=record.note_id,
            rename_from=rename_from,
            new_field_values=fields_to_update,
            tags_to_add=tags_missing,
            note="; ".join(note_msg_parts),
        )
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_plan(
    cfg: Config,
    state: State,
    anki: AnkiConnect,
    row_plans: list[PlannedAction],
    ghost_plans: list[PlannedAction],
    source_tag: str,
) -> None:
    """Execute the plan against Anki. Caller must have constructed `anki`
    with allow_writes=True."""
    for ghost in ghost_plans:
        if ghost.id_value:
            state.mark_status(ghost.id_value, "hard_deleted")

    # Count only the plans that actually perform Anki writes — NOOP and the
    # SKIP_* variants don't touch Anki and would otherwise make the bar look
    # artificially slow.
    writing_actions = {
        ACTION_CREATE, ACTION_UPDATE, ACTION_BOOTSTRAP,
        ACTION_SKIP_VETOED, ACTION_SKIP_DELETED,
    }
    write_total = sum(1 for p in row_plans if p.action in writing_actions)
    prog = Progress(write_total, label="applying")

    for p in row_plans:
        if p.action == ACTION_CREATE:
            note_id = anki.add_note(
                deck_name=cfg.new_destination,
                model_name=cfg.note_type,
                fields=p.new_field_values,
                tags=p.tags_to_add,
            )
            state.upsert_entry(
                p.id_value,
                current_lemma=p.lemma,
                anki_note_id=note_id,
                first_source=source_tag,
                status="active",
            )
            prog.update(detail=f"{p.action} {p.id_value} {p.lemma}")

        elif p.action in (ACTION_UPDATE, ACTION_BOOTSTRAP):
            if p.new_field_values:
                anki.update_note_fields(p.note_id, p.new_field_values)
            if p.tags_to_add:
                anki.add_tags([p.note_id], " ".join(p.tags_to_add))
            state.upsert_entry(
                p.id_value,
                current_lemma=p.lemma,
                anki_note_id=p.note_id,
                first_source=source_tag,
                status="active",
            )
            prog.update(detail=f"{p.action} {p.id_value} {p.lemma}")

        elif p.action == ACTION_SKIP_VETOED:
            if p.id_value:
                state.upsert_entry(
                    p.id_value,
                    current_lemma=p.lemma,
                    anki_note_id=p.note_id,
                    first_source=source_tag,
                    status="vetoed",
                )
            prog.update(detail=f"{p.action} {p.id_value}")

        elif p.action == ACTION_SKIP_DELETED:
            if p.id_value:
                state.mark_status(p.id_value, "hard_deleted")
            prog.update(detail=f"{p.action} {p.id_value}")

        # NOOP and DETECT_DELETE handled elsewhere.

    prog.close()
    state.save()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _summarize(row_plans: list[PlannedAction], ghost_plans: list[PlannedAction]) -> dict:
    counts: dict[str, int] = {}
    for p in row_plans:
        counts[p.action] = counts.get(p.action, 0) + 1
    for p in ghost_plans:
        counts[p.action] = counts.get(p.action, 0) + 1
    return counts


def _print_plan(
    cfg: Config,
    csv_path: Path,
    source_tag: str,
    color_tag: str,
    row_plans: list[PlannedAction],
    ghost_plans: list[PlannedAction],
    verbose: bool,
) -> None:
    print(f"Source CSV:  {csv_path}")
    print(f"Deck:        {cfg.deck_root}")
    print(f"Note type:   {cfg.note_type}")
    print(f"New cards →  {cfg.new_destination}")
    print(f"Tags:        {[source_tag, color_tag] + list(cfg.extra_tags)}")
    print("─" * 72)
    counts = _summarize(row_plans, ghost_plans)
    label_width = max((len(k) for k in counts), default=0)
    for label in (
        ACTION_CREATE,
        ACTION_UPDATE,
        ACTION_BOOTSTRAP,
        ACTION_SKIP_VETOED,
        ACTION_SKIP_DELETED,
        ACTION_DETECT_DELETE,
        ACTION_NOOP,
    ):
        c = counts.get(label, 0)
        if c:
            print(f"  {label:<{label_width}}  {c:>5d}")
    print("─" * 72)

    # Renames are interesting enough to always surface.
    renames = [p for p in row_plans if p.rename_from]
    if renames:
        print(f"\nRENAMES detected ({len(renames)}):")
        for p in renames:
            print(f"  {p.id_value}  {p.rename_from!r}  →  {p.lemma!r}")

    if ghost_plans:
        print(f"\nDELETE-DETECT (will be marked hard_deleted in state):")
        for p in ghost_plans:
            print(f"  {p.id_value}  ({p.note})")

    if verbose:
        print("\nPer-row detail:")
        for p in row_plans:
            print(f"  [{p.action:<14}] {p.lemma:<24} {p.id_value or '':<12} {p.note}")


def _print_diff(cfg: Config, index: AnkiIndex, plan_actions: list[PlannedAction], target_id: str) -> None:
    """Show field-by-field before/after for a specific ID."""
    target_plan = next((p for p in plan_actions if p.id_value == target_id), None)
    if target_plan is None:
        print(f"No planned action for ID {target_id!r}")
        return
    print(f"\n=== Diff for {target_id} ({target_plan.lemma}) ===")
    print(f"Action: {target_plan.action}")
    print(f"Note:   {target_plan.note}")
    record = index.find_by_id_value(target_id) if target_id else None
    if not target_plan.new_field_values:
        print("No field changes planned.")
        return
    for fname, new_val in target_plan.new_field_values.items():
        old_val = record.field_value(fname) if record else "<no existing note>"
        print(f"\n--- Field: {fname} ---")
        print(f"OLD: {old_val[:400]}{'…' if len(old_val) > 400 else ''}")
        print(f"NEW: {new_val[:400]}{'…' if len(new_val) > 400 else ''}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync a by_color CSV into Anki. Dry-run by default."
    )
    parser.add_argument("csv", type=Path, help="Path to by_color/<...>.csv")
    parser.add_argument("--apply", action="store_true",
                        help="Actually mutate Anki (default: dry-run only)")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to anki_sync_config.json (default: alongside this file)")
    parser.add_argument("--diff", metavar="ID", default=None,
                        help="Show field-level before/after for one specific ID")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-row plan details")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    parsed = parse_filename(args.csv.stem, cfg)
    source_tag = parsed["source"]
    color_tag = parsed["color"]

    state = State(cfg.state_file, id_prefix=cfg.id_prefix, id_padding=cfg.id_padding)

    # Read-only client for the planning phase.
    anki_ro = AnkiConnect(allow_writes=False)
    try:
        index = build_index(anki_ro, cfg)
    except AnkiConnectError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    rows = load_csv_rows(args.csv)
    row_plans, ghost_plans = plan(cfg, state, index, rows, source_tag, color_tag)

    _print_plan(cfg, args.csv, source_tag, color_tag, row_plans, ghost_plans, args.verbose)

    if args.diff:
        _print_diff(cfg, index, row_plans, args.diff)

    if not args.apply:
        print("\n[dry-run] No changes written. Re-run with --apply to execute.")
        return 0

    # Apply phase.
    print("\nApplying changes...")
    anki_rw = AnkiConnect(allow_writes=True)
    try:
        apply_plan(cfg, state, anki_rw, row_plans, ghost_plans, source_tag)
    except AnkiConnectError as e:
        print(f"\nERROR during apply: {e}", file=sys.stderr)
        print("State file was NOT saved. Re-run after resolving the error.")
        return 1
    print("Done. State saved to", cfg.state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
