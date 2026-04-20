"""
anki_discover.py

Read-only introspection of your Anki setup via AnkiConnect.
Nothing in your Anki collection is modified. Only these AnkiConnect actions
are used: deckNames, modelNames, modelFieldNames, findNotes, notesInfo, getTags.

Usage:
    python anki_discover.py
    python anki_discover.py --deck "Spanish"
    python anki_discover.py --deck "Spanish" --sample 5

Requirements:
    - Anki running on the same machine
    - AnkiConnect add-on installed (code 2055492159), listening on :8765
    - Python stdlib only
"""

import argparse
import json
import sys
from collections import Counter

# Support running as a script OR as a module (`python -m anki_sync.anki_discover`).
if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.ankiconnect import AnkiConnect, AnkiConnectError
else:
    from .ankiconnect import AnkiConnect, AnkiConnectError

_anki = AnkiConnect(allow_writes=False)


def invoke(action: str, **params):
    try:
        return _anki.invoke(action, **params)
    except AnkiConnectError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def truncate(s: str, limit: int = 120) -> str:
    s = (s or "").replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def pick_deck(requested: str | None) -> str:
    decks = invoke("deckNames")
    if requested:
        if requested in decks:
            return requested
        # Try case-insensitive match and prefix match for convenience
        matches = [d for d in decks if d.lower() == requested.lower()]
        if not matches:
            matches = [d for d in decks if requested.lower() in d.lower()]
        if len(matches) == 1:
            return matches[0]
        print(f"Deck '{requested}' not found. Available decks:")
        for d in decks:
            print(f"  {d}")
        sys.exit(1)

    print("Available decks:")
    for d in decks:
        print(f"  {d}")
    print()
    # Heuristic: if there's a deck with 'spanish' in the name, suggest it
    candidates = [d for d in decks if "span" in d.lower()]
    if candidates:
        print("Heuristic guess (Spanish-related):")
        for c in candidates:
            print(f"  {c}")
    print()
    print("Re-run with --deck \"<name>\" to inspect a specific deck.")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deck",
        help="Deck name to inspect. If omitted, lists all decks and exits.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=3,
        help="Number of sample notes per model to show (default: 3)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the full report as JSON to stdout (for later tooling).",
    )
    args = parser.parse_args()

    report: dict = {}

    deck = pick_deck(args.deck)
    report["deck"] = deck

    if not args.as_json:
        print(f"\n=== Inspecting deck: {deck} ===\n")

    note_ids = invoke("findNotes", query=f'deck:"{deck}"')
    report["note_count"] = len(note_ids)

    if not args.as_json:
        print(f"Total notes in deck: {len(note_ids):,}")

    if not note_ids:
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # Pull note info in one batched call (AnkiConnect handles big arrays fine).
    all_infos = invoke("notesInfo", notes=note_ids)

    # Bucket by model (note type) so we see the real field layout.
    by_model: dict[str, list[dict]] = {}
    tag_counter: Counter = Counter()
    for info in all_infos:
        by_model.setdefault(info["modelName"], []).append(info)
        for t in info.get("tags", []):
            tag_counter[t] += 1

    report["models"] = {}
    report["tag_frequency"] = dict(tag_counter.most_common())

    for model, notes in by_model.items():
        field_names = list(notes[0]["fields"].keys())
        # Per-field: how often is the field non-empty across the deck?
        fill_rates = {f: 0 for f in field_names}
        for n in notes:
            for f, v in n["fields"].items():
                if v.get("value", "").strip():
                    fill_rates[f] = fill_rates.get(f, 0) + 1

        samples = []
        for n in notes[: args.sample]:
            samples.append(
                {
                    "noteId": n["noteId"],
                    "tags": n.get("tags", []),
                    "fields": {
                        f: truncate(v.get("value", "")) for f, v in n["fields"].items()
                    },
                }
            )

        report["models"][model] = {
            "note_count": len(notes),
            "fields": field_names,
            "fill_rates": fill_rates,
            "samples": samples,
        }

        if not args.as_json:
            print(f"\n--- Note type: {model}  ({len(notes)} notes) ---")
            print(f"Fields: {field_names}")
            print("Fill rate (non-empty / total):")
            for f in field_names:
                pct = fill_rates[f] / len(notes) * 100
                print(f"  {f:30s}  {fill_rates[f]:>5d} / {len(notes):<5d}  ({pct:5.1f}%)")
            print(f"\nSample notes (first {min(args.sample, len(notes))}):")
            for s in samples:
                print(f"  [noteId={s['noteId']}]  tags={s['tags']}")
                for f, v in s["fields"].items():
                    print(f"    {f:30s}  {v}")
                print()

    if not args.as_json:
        print("\n=== Tag frequency (top 30) ===")
        for tag, count in tag_counter.most_common(30):
            print(f"  {count:>5d}  {tag}")

    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
