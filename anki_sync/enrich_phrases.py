"""
enrich_phrases.py

Stage 1 of the phrase (orange) pipeline: read a by_color orange CSV, call
Claude Haiku 4.5 once per unique phrase, and emit a CSV consumed directly
by phrase_sync.py. There is no pre-sync review step — review happens in
Anki itself, against the created notes.

Usage
-----
    # Default: write enriched_phrases/<stem>_phrases.csv next to the repo.
    python -m anki_sync.enrich_phrases \\
        spanish_kindle_exports/by_color/2026-04-13-percy_jackson_orange.csv

    # Skip LLM calls — populate everything from the cache only (errors out
    # when a row is not cached). Useful for deterministic re-runs.
    python -m anki_sync.enrich_phrases <csv> --offline

    # Custom output path:
    python -m anki_sync.enrich_phrases <csv> --out /tmp/phrases.csv

Output schema
-------------
Columns expected by phrase_sync.py / anki_phrase_sync_config.json:

    source_stem       — filename stem of the source CSV (drives tags via
                        filename_parser regex)
    lemma             — NORMALIZED phrase key (spec §6). Goes into `Phrase`.
    phrase_original   — phrase verbatim from the source CSV (kept for
                        provenance; sync ignores it)
    cloze_sentence    — context with {{cN::answer::hint}} markup
    translation       — English of the full context sentence
    insight           — linguistic note (Spanish-dominant)
    explanation       — plain-English gloss of what the phrase means + when
                        a native would use it
    alternatives      — 1–2 paraphrases
    personal_note     — pre-formatted for bullet-union ingestion (empty or
                        "Notes:<br>• <user text>")
    source            — "<source_stem> · <first location>"
    context_sentence  — the raw context the LLM saw (for reference; sync
                        ignores this column)
    cache_hit         — "yes"/"no" — did this row reuse a cached enrichment
    fallback_used     — "yes"/"no" — did the cloze-span validator fall back
                        to a whole-phrase cloze. "yes" rows are worth
                        hand-reviewing in Anki; the blank is broader than
                        the LLM intended and carries no hint.

Dedup: rows with the same normalized phrase key collapse; the earliest
occurrence wins for context/source/enrichment. Personal notes from multiple
occurrences concatenate into the bullet list.
"""

from __future__ import annotations

import argparse
import csv
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from anki_sync.llm_enrich import (
        DEFAULT_CACHE_PATH,
        EnrichmentCache,
        EnrichmentError,
        EnrichmentResult,
        _build_result,
        _call_llm,
        _load_client,
        _stamp_schema,
        is_cache_entry_fresh,
    )
    from anki_sync.phrase_normalize import (
        find_phrase_in_context,
        normalize_phrase_key,
    )
    from anki_sync.progress import Progress
else:
    from .llm_enrich import (
        DEFAULT_CACHE_PATH,
        EnrichmentCache,
        EnrichmentError,
        EnrichmentResult,
        _build_result,
        _call_llm,
        _load_client,
        _stamp_schema,
        is_cache_entry_fresh,
    )
    from .phrase_normalize import (
        find_phrase_in_context,
        normalize_phrase_key,
    )
    from .progress import Progress


DEFAULT_CONCURRENCY = 8
"""Max in-flight LLM calls. Anthropic's default tier 1 RPM is well above this
for Haiku; bump via --concurrency if you have a higher tier. Raising it past
~16 yields diminishing returns because each call is already sub-second and
token-bound, not latency-bound, at that point."""

_CACHE_SAVE_INTERVAL = 10
"""How often to flush the enrichment cache to disk during the parallel pass.
Saving on every completion rewrites the whole JSON file N times and gets
expensive for large batches (O(N²) in file size). Saving every N keeps
durability — at worst we re-bill N−1 enrichments on a crash — while
amortising the I/O. An unconditional save runs at the end of the run."""


OUTPUT_COLUMNS = [
    "source_stem",
    "lemma",
    "phrase_original",
    "cloze_sentence",
    "translation",
    "insight",
    "explanation",
    "alternatives",
    "personal_note",
    "source",
    "context_sentence",
    "cache_hit",
    "fallback_used",
]


@dataclass
class PhraseRow:
    key: str
    phrase_original: str
    context_sentence: str
    personal_notes: list[str] = field(default_factory=list)
    first_location: str = ""


def _first_context_sentence(raw: str) -> str:
    """The CSV stores context sentences as bullet-prefixed lines. The spec
    requires the first (earliest occurrence)."""
    if not raw:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("• "):
            return line[2:].strip()
        return line
    return ""


def _first_location(source_contexts: str) -> str:
    """CSV source_contexts column is `"Location 28 • orange | Location 394 • orange"`.
    Take the first pipe-segment. (The adjacent `pages` column is empty for
    orange highlights; `source_contexts` is the one that carries location
    info for this pipeline.)"""
    if not source_contexts:
        return ""
    return source_contexts.split("|")[0].strip()


def _format_personal_notes(notes: list[str]) -> str:
    """Format note_text entries for bullet-merge ingestion.

    bullet_merge expects a section header followed by bullet lines. Empty
    string means "no note to merge" — bullet_merge will render the existing
    field value unchanged on update (good), and write empty on creation.
    """
    cleaned = [n.strip() for n in notes if n and n.strip()]
    if not cleaned:
        return ""
    lines = ["Notes:"]
    lines.extend(f"• {n}" for n in cleaned)
    # Sync's _csv_to_html converts \n → <br>; emit plain newlines here.
    return "\n".join(lines)


def collect_rows(csv_path: Path) -> list[PhraseRow]:
    """Read the source orange CSV and collapse duplicate phrases by key.

    First occurrence wins for context/source/phrase_original; notes from
    every occurrence accumulate into personal_notes."""
    by_key: dict[str, PhraseRow] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for src in csv.DictReader(f):
            raw_phrase = (src.get("lemma") or "").strip()
            if not raw_phrase:
                continue
            key = normalize_phrase_key(raw_phrase)
            if not key:
                continue
            ctx = _first_context_sentence(src.get("context_sentences") or "")
            if not ctx:
                # Spec §9 #7: phrase not locatable. Skip but surface.
                print(
                    f"  ⚠ skipping {raw_phrase!r}: no context_sentences in CSV",
                    file=sys.stderr,
                )
                continue
            note = (src.get("note_text") or "").strip()
            loc = _first_location(src.get("source_contexts") or "")

            existing = by_key.get(key)
            if existing is None:
                existing = PhraseRow(
                    key=key,
                    phrase_original=raw_phrase,
                    context_sentence=ctx,
                    first_location=loc,
                )
                by_key[key] = existing
            if note and note not in existing.personal_notes:
                existing.personal_notes.append(note)
    return list(by_key.values())


def _cache_is_fresh(entry: dict | None) -> bool:
    """Thin wrapper around llm_enrich.is_cache_entry_fresh — kept as a local
    name so existing call sites and tests don't break."""
    return is_cache_entry_fresh(entry)


def enrich_and_write(
    rows: list[PhraseRow],
    source_stem: str,
    out_path: Path,
    cache: EnrichmentCache,
    *,
    offline: bool,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[int, int, int, list[str]]:
    """Two-phase enrichment:

    1. Serial cache-lookup pass. Every cached phrase resolves instantly.
    2. Parallel LLM pass for misses, via a ThreadPoolExecutor. Workers call
       the Anthropic API (I/O-bound, GIL-friendly). The main thread owns
       progress, cache writes, and disk saves under a single lock.
    3. CSV write in original row order so review diffs stay stable.

    Returns (num_rows_written, num_llm_calls, num_fallback_used,
    list_of_failure_messages).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concurrency = max(1, concurrency)

    results: list[EnrichmentResult | None] = [None] * len(rows)
    failures: list[str] = []
    calls = 0

    with Progress(len(rows), label="enriching phrases") as prog:
        # Precompute phrase offsets in context once per row. Reused for the
        # cache path (passed into `_build_result`) and the LLM path (passed
        # into `_call_llm` and `_build_result`).
        phrase_spans: list[tuple[int, int] | None] = [
            find_phrase_in_context(r.context_sentence, r.phrase_original)
            for r in rows
        ]

        # ---------- Phase 1: satisfy cache hits ---------------------------
        pending: list[int] = []
        for i, row in enumerate(rows):
            entry = cache.get(row.phrase_original, row.context_sentence)
            if _cache_is_fresh(entry):
                try:
                    r = _build_result(
                        row.phrase_original,
                        row.context_sentence,
                        entry,
                        phrase_spans[i],
                    )
                    r.cache_hit = True
                    results[i] = r
                    prog.update(detail=f"{row.phrase_original} (cached)")
                    continue
                except EnrichmentError:
                    # Cached payload no longer validates (e.g. context edited
                    # upstream). Re-queue for a fresh LLM call.
                    pass
            pending.append(i)

        # ---------- Phase 2: parallel LLM for cache misses ----------------
        if pending:
            if offline:
                # We already confirmed no cache entry exists; surface one
                # failure per row and move on.
                for i in pending:
                    row = rows[i]
                    failures.append(
                        f"{row.phrase_original!r}: offline mode: cache miss "
                        "requires an LLM call but --offline forbids it"
                    )
                    prog.update(detail=f"FAIL {row.phrase_original}")
            else:
                # One shared client across workers — Anthropic's SDK is
                # thread-safe, and `httpx` pools connections under the hood.
                try:
                    client = _load_client()
                except EnrichmentError as e:
                    # Environment problem (missing key, missing SDK). Fail
                    # every pending row identically; nothing else we can do.
                    for i in pending:
                        failures.append(f"{rows[i].phrase_original!r}: {e}")
                        prog.update(detail=f"FAIL {rows[i].phrase_original}")
                    client = None

                if client is not None:
                    cache_lock = threading.Lock()

                    def _worker(idx: int):
                        row = rows[idx]
                        note = row.personal_notes[0] if row.personal_notes else None
                        ph_span = phrase_spans[idx]
                        tool_input = _call_llm(
                            client,
                            row.phrase_original,
                            row.context_sentence,
                            note,
                            ph_span,
                        )
                        # Validate in the worker so a bad payload fails this
                        # row, not the whole batch. Raises EnrichmentError.
                        result = _build_result(
                            row.phrase_original,
                            row.context_sentence,
                            tool_input,
                            ph_span,
                        )
                        return idx, tool_input, result

                    # `calls` is only mutated from this loop, which runs on
                    # the main thread (`as_completed` serializes delivery),
                    # so no separate counter lock is needed.
                    since_save = 0

                    with ThreadPoolExecutor(
                        max_workers=min(concurrency, len(pending)),
                        thread_name_prefix="enrich",
                    ) as pool:
                        futs = {pool.submit(_worker, i): i for i in pending}
                        for fut in as_completed(futs):
                            idx = futs[fut]
                            row = rows[idx]
                            try:
                                _, tool_input, result = fut.result()
                            except EnrichmentError as e:
                                failures.append(f"{row.phrase_original!r}: {e}")
                                prog.update(detail=f"FAIL {row.phrase_original}")
                                continue
                            except Exception as e:  # network/SDK surprises
                                failures.append(f"{row.phrase_original!r}: {e}")
                                prog.update(detail=f"FAIL {row.phrase_original}")
                                continue

                            results[idx] = result
                            # Main-thread-only cache mutation. The lock
                            # guards against future callers that might
                            # share the cache across threads; `as_completed`
                            # already serializes deliveries here.
                            with cache_lock:
                                cache.put(
                                    row.phrase_original,
                                    row.context_sentence,
                                    _stamp_schema(tool_input),
                                )
                                calls += 1
                                since_save += 1
                                # Amortise disk I/O: dump the JSON every
                                # _CACHE_SAVE_INTERVAL completions instead
                                # of every one. A crash loses at most
                                # INTERVAL-1 cache entries, which are
                                # just re-fetched on re-run.
                                if since_save >= _CACHE_SAVE_INTERVAL:
                                    cache.save()
                                    since_save = 0
                            suffix = "LLM*" if result.fallback_used else "LLM"
                            prog.update(detail=f"{row.phrase_original} ({suffix})")

                    # Flush any unsaved cache entries before leaving the
                    # parallel region. The function-level `cache.save()` at
                    # the bottom of enrich_and_write is still there as a
                    # belt-and-suspenders final commit.
                    if since_save > 0:
                        with cache_lock:
                            cache.save()

        # ---------- Phase 3: write CSV in input order ---------------------
        written = 0
        fallbacks = 0
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            for i, row in enumerate(rows):
                result = results[i]
                if result is None:
                    continue  # failed row — already logged
                source_field = source_stem
                if row.first_location:
                    source_field = f"{source_stem} · {row.first_location}"
                writer.writerow(
                    {
                        "source_stem": source_stem,
                        "lemma": row.key,
                        "phrase_original": row.phrase_original,
                        "cloze_sentence": result.cloze_sentence,
                        "translation": result.translation,
                        "insight": result.insight,
                        "explanation": result.explanation,
                        "alternatives": result.alternatives,
                        "personal_note": _format_personal_notes(row.personal_notes),
                        "source": source_field,
                        "context_sentence": result.context_sentence,
                        "cache_hit": "yes" if result.cache_hit else "no",
                        "fallback_used": "yes" if result.fallback_used else "no",
                    }
                )
                written += 1
                if result.fallback_used:
                    fallbacks += 1

    cache.save()
    return written, calls, fallbacks, failures


def _default_out_path(src: Path) -> Path:
    return src.parent.parent / "enriched_phrases" / f"{src.stem}_phrases.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="Path to by_color/<...>_orange.csv")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH,
                        help=f"Enrichment cache path (default: {DEFAULT_CACHE_PATH})")
    parser.add_argument("--offline", action="store_true",
                        help="Do not call the LLM; satisfy only from cache.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            f"Max parallel LLM calls for cache misses (default: "
            f"{DEFAULT_CONCURRENCY}). Raise if you're on a higher "
            "Anthropic rate-limit tier; lower if you hit 429s."
        ),
    )
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    source_stem = args.csv.stem
    out_path = args.out or _default_out_path(args.csv)

    print(f"Source CSV:  {args.csv}")
    print(f"Output:      {out_path}")
    print(f"Cache:       {args.cache}")
    if args.offline:
        print("Mode:        offline (cache only)")
    else:
        print(f"Concurrency: {args.concurrency}")

    rows = collect_rows(args.csv)
    print(f"\nUnique phrases (after dedup): {len(rows)}")

    cache = EnrichmentCache(args.cache)
    written, calls, fallbacks, failures = enrich_and_write(
        rows,
        source_stem,
        out_path,
        cache,
        offline=args.offline,
        concurrency=args.concurrency,
    )
    print(
        f"Enriched: {written}   LLM calls: {calls}   "
        f"Fallback clozes: {fallbacks}   Cache size: {len(cache.data)}"
    )
    if fallbacks:
        print(
            f"  ⚠ {fallbacks} row(s) used the whole-phrase cloze fallback. "
            "Filter the enriched CSV on `fallback_used=yes` to find them "
            "and hand-edit those cards in Anki if you want finer clozes."
        )
    if failures:
        print(f"\n{len(failures)} row(s) failed:", file=sys.stderr)
        for msg in failures:
            print(f"  - {msg}", file=sys.stderr)
    print(f"\nWrote {out_path}")
    print(
        "Next step: run phrase_sync on this CSV. Review happens in Anki "
        "after notes are created — edit fields directly on the cards, and "
        "use the Unused / WIP / Finalized sub-decks to move or demote them."
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
