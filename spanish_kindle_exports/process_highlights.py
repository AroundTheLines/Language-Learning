"""
process_highlights.py

Master pipeline: runs both enrichment and translation/deduplication in one go,
producing two intermediate artifacts that can be inspected independently:

  enriched/<stem>_enriched.csv    — one row per highlight with context sentence
  translated/<stem>_translated.csv — deduplicated, translated, with IPA and gender

Usage:
    python process_highlights.py highlights.csv book.epub

Requirements:
    pip install ebooklib beautifulsoup4 deepl python-dotenv spacy
    python -m spacy download es_core_news_sm
    DEEPL_API_KEY in .env or environment  (free key at https://www.deepl.com/pro-api)
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from enrich_highlights import (
    load_highlights,
    extract_epub_text_in_order,
    find_contexts,
    save_enriched,
)
from translate_and_deduplicate import (
    load_csv,
    group_rows,
    check_usage,
    translate_batch,
    add_ipa,
    add_word_type,
    save_csv,
)
from split_by_color import split_by_color

try:
    import deepl
except ImportError:
    print("Missing dependency. Run: pip install deepl")
    sys.exit(1)


def _enriched_path(csv_path: str) -> str:
    script_dir = Path(__file__).parent
    output_dir = script_dir / "enriched"
    output_dir.mkdir(exist_ok=True)
    stem = Path(csv_path).stem
    return str(output_dir / f"{stem}_enriched.csv")


def _translated_path(enriched_path: str) -> str:
    script_dir = Path(__file__).parent
    output_dir = script_dir / "translated"
    output_dir.mkdir(exist_ok=True)
    stem = re.sub(r"_enriched$", "", Path(enriched_path).stem)
    return str(output_dir / f"{stem}_translated.csv")


def _maybe_sync_to_anki(by_color_dir: Path, source_filter: str | None, apply: bool) -> None:
    """Optionally invoke the anki_sync pipeline on the freshly-split by_color
    files. Imported lazily so the highlights pipeline doesn't depend on
    AnkiConnect being reachable for normal use."""
    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))
    from anki_sync.anki_sync import main as anki_sync_main

    csv_files = sorted(by_color_dir.glob("*.csv"))
    if source_filter:
        csv_files = [p for p in csv_files if source_filter in p.stem]

    if not csv_files:
        print("No by_color CSVs to sync.")
        return

    print("\n" + "=" * 60)
    print(f"STEP 4: Syncing to Anki  ({'APPLY' if apply else 'dry-run'})")
    print("=" * 60)
    for csv_path in csv_files:
        print(f"\n>>> {csv_path.name}")
        argv = [str(csv_path)]
        if apply:
            argv.append("--apply")
        rc = anki_sync_main(argv)
        if rc != 0:
            print(f"  anki_sync exited with code {rc} on {csv_path.name}; stopping.")
            return


def main():
    parser_argv = sys.argv[1:]
    # Strip our optional flags before falling back to positional parsing.
    sync_flag = "--sync-to-anki" in parser_argv
    apply_flag = "--apply" in parser_argv
    source_filter = None
    if "--sync-source" in parser_argv:
        i = parser_argv.index("--sync-source")
        if i + 1 < len(parser_argv):
            source_filter = parser_argv[i + 1]
            del parser_argv[i:i + 2]
    parser_argv = [a for a in parser_argv if a not in {"--sync-to-anki", "--apply"}]

    if len(parser_argv) != 2:
        print(
            "Usage: python process_highlights.py highlights.csv book.epub "
            "[--sync-to-anki] [--apply] [--sync-source <stem-substring>]"
        )
        sys.exit(1)
    sys.argv = [sys.argv[0], *parser_argv]

    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        print("Error: DEEPL_API_KEY environment variable not set.")
        print("Get a free key at https://www.deepl.com/pro-api (500k chars/month free)")
        sys.exit(1)

    csv_path, epub_path = sys.argv[1], sys.argv[2]
    enriched_out = _enriched_path(csv_path)
    translated_out = _translated_path(enriched_out)

    # -------------------------------------------------------------------------
    # Step 1: Enrich — add context_sentence to each highlight row
    # -------------------------------------------------------------------------
    print("=" * 60)
    print("STEP 1: Enriching highlights with context sentences")
    print("=" * 60)

    print(f"Loading highlights from {csv_path}...")
    highlights = load_highlights(csv_path)
    print(f"  {len(highlights)} rows loaded.")

    print(f"Extracting text from {epub_path}...")
    full_text = extract_epub_text_in_order(epub_path)
    print(f"  {len(full_text):,} characters extracted.")

    print("Matching highlights to EPUB text...")
    contexts = find_contexts(highlights, full_text)

    found = sum(1 for c in contexts if c and c != "[not found in EPUB]")
    not_found = sum(1 for c in contexts if c == "[not found in EPUB]")
    skipped = sum(1 for c in contexts if c == "")
    print(f"  Found: {found}  |  Not found: {not_found}  |  Skipped (empty): {skipped}")

    print(f"Writing enriched output to {enriched_out}...")
    save_enriched(highlights, contexts, enriched_out)
    print(f"  Saved: {enriched_out}\n")

    # -------------------------------------------------------------------------
    # Step 2: Deduplicate, translate, annotate
    # -------------------------------------------------------------------------
    print("=" * 60)
    print("STEP 2: Deduplicating, translating, and annotating")
    print("=" * 60)

    enriched_rows = load_csv(enriched_out)
    print(f"  {len(enriched_rows)} enriched rows loaded.")

    print("Lemmatising and grouping...")
    groups = group_rows(enriched_rows)
    print(f"  {len(groups)} unique lemmas (from {len(enriched_rows)} rows).")

    print("\nChecking DeepL API usage...")
    used, limit = check_usage(api_key)
    chars_to_translate = sum(len(g["lemma"]) for g in groups)
    projected = used + chars_to_translate
    pct_after = projected / limit * 100 if limit else 0
    print(f"  Estimated chars to translate: {chars_to_translate:,}")
    print(f"  Projected usage after:        {projected:,} / {limit:,} ({pct_after:.1f}%)")
    print(f"  About to translate {len(groups)} terms.")
    try:
        input("  Press Enter to proceed, or Ctrl-C to cancel... ")
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)

    print("\nTranslating via DeepL API...")
    translator = deepl.Translator(api_key)
    translate_batch(translator, groups)

    print("\nDeepL API usage after translation:")
    check_usage(api_key)

    print("\nAdding IPA for single-word lemmas...")
    add_ipa(groups)

    print("Adding word type for single-word lemmas...")
    add_word_type(groups)

    print(f"Writing translated output to {translated_out}...")
    save_csv(groups, translated_out)
    print(f"  Saved: {translated_out}\n")

    # -------------------------------------------------------------------------
    # Step 3: Split by highlight color
    # -------------------------------------------------------------------------
    print("=" * 60)
    print("STEP 3: Splitting by highlight color")
    print("=" * 60)
    split_by_color(translated_out)
    print()

    print("=" * 60)
    print("Pipeline complete.")
    print(f"  Per-highlight (inspectable): {enriched_out}")
    print(f"  Deduplicated + translated:   {translated_out}")
    print(f"  Split by color:              by_color/")
    print("=" * 60)

    if sync_flag:
        by_color_dir = Path(__file__).parent / "by_color"
        # Default the source filter to this run's stem so we don't sync
        # unrelated older books that still have CSVs lying around.
        stem_for_filter = source_filter or Path(csv_path).stem
        _maybe_sync_to_anki(by_color_dir, stem_for_filter, apply=apply_flag)


if __name__ == "__main__":
    main()
