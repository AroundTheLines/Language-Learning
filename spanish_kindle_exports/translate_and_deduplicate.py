"""
translate_and_deduplicate.py

Reads an enriched Kindle highlights CSV, deduplicates rows by lemma, and adds:
  - context-aware Spanish→English translation (DeepL API)
  - IPA phonetic pronunciation (single words only)
  - word type with gender (single words only, via spaCy es_core_news_sm)

Grouping key is the Stanza lemma for single words (so tengo/tener,
despertarme/despertarse all collapse to one row), and the normalised surface
form for multi-word phrases. The lemma is the Anki card front.

Usage:
    python translate_and_deduplicate.py enriched/<stem>_enriched.csv

Output:
    translated/<stem>_translated.csv

Requirements:
    pip install deepl python-dotenv spacy stanza
    python -m spacy download es_core_news_sm
    python -c "import stanza; stanza.download('es')"
    DEEPL_API_KEY in .env or environment  (free key at https://www.deepl.com/pro-api)
"""

import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from csv_to_ipa import word_to_ipa

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # dotenv optional; fall back to environment variables

try:
    import deepl
except ImportError:
    print("Missing dependency. Run: pip install deepl")
    sys.exit(1)


# Optional progress bar. Import lazily so this script keeps working even if
# anki_sync is not on sys.path.
def _progress(total: int, label: str):
    try:
        _project_root = Path(__file__).parent.parent
        if str(_project_root) not in sys.path:
            sys.path.insert(0, str(_project_root))
        from anki_sync.progress import Progress
        return Progress(total, label=label)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Normalisation (mirrors enrich_highlights.py)
# ---------------------------------------------------------------------------

_PUNCT_STRIP = str.maketrans("", "", ".,;:!?¡¿\"'«»\u2018\u2019—–-…\u00ab\u00bb")
_DASHES = re.compile(r"[—–\-]")


def _normalise(text: str) -> str:
    text = text.strip()
    text = _DASHES.sub(" ", text)
    text = text.translate(_PUNCT_STRIP)
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


_LEADING_TRAILING_PUNCT = re.compile(
    r'^[.,;:!?¡¿"\'«»\u2018\u2019—–\-…\s]+'
    r'|[.,;:!?¡¿"\'«»\u2018\u2019—–\-…\s]+$'
)


def _clean_word(text: str) -> str:
    """Strip leading/trailing punctuation and dashes from a highlight word."""
    return _LEADING_TRAILING_PUNCT.sub("", text)


def _clean_lemma(lemma: str) -> str:
    """Ensure a lemma contains no stray punctuation or dashes."""
    lemma = _clean_word(lemma)
    # Also strip any internal double-dashes that shouldn't be there
    lemma = re.sub(r"[—–]+", "", lemma)
    return lemma.strip()


# ---------------------------------------------------------------------------
# Lemmatization via Stanza  (needed before grouping)
# ---------------------------------------------------------------------------

_stanza_nlp = None
_REFLEXIVE_CLITICS = {"se", "me", "te", "nos", "os"}


def _get_stanza_nlp():
    global _stanza_nlp
    if _stanza_nlp is None:
        try:
            import stanza
            _stanza_nlp = stanza.Pipeline(
                "es",
                processors="tokenize,mwt,pos,lemma",
                verbose=False,
            )
        except ImportError:
            raise ImportError(
                "Lemmatization requires: pip install stanza && "
                "python -c \"import stanza; stanza.download('es')\""
            )
    return _stanza_nlp


def _get_lemma_stanza(word: str, context_sentence: str = "") -> str:
    """
    Return the canonical lemma for a single Spanish word using Stanza.

    - Conjugated verbs → infinitive: tengo→tener, di→dar.
    - Reflexive clitics on infinitives → normalised to -se:
      despertarme→despertarse, ponerse→ponerse.
    - Other words → dictionary headword: bonita→bonito, polvo→polvo.
    - context_sentence is used for disambiguation of ambiguous forms.

    Returns '' for multi-word input or if Stanza produces no useful result.
    """
    word = _clean_word(word.strip())
    if not word or " " in _normalise(word):
        return ""

    nlp = _get_stanza_nlp()
    text = context_sentence.strip() if context_sentence else word
    doc = nlp(text)

    target = word.lower()
    for sent in doc.sentences:
        for mwt in sent.tokens:
            if mwt.text.lower() != target:
                continue
            words = mwt.words

            # MWT expansion: verb + clitic (despertarme → despertar + me)
            verb_word = next((w for w in words if w.upos in ("VERB", "AUX")), None)
            if verb_word and len(words) > 1:
                has_reflexive = any(
                    w.upos == "PRON" and w.text.lower() in _REFLEXIVE_CLITICS
                    for w in words
                )
                return verb_word.lemma + ("se" if has_reflexive else "")

            return words[0].lemma

    # Word not found in the context sentence (rare) — retry in isolation
    if context_sentence:
        return _get_lemma_stanza(word, "")

    return ""


# ---------------------------------------------------------------------------
# LLM-based batch lemmatization (primary) with Stanza fallback
# ---------------------------------------------------------------------------

_LEMMA_BATCH_SIZE = 50
_MAX_CONTEXT_LEN = 200
_LEMMATISE_CONCURRENCY = int(os.environ.get("LEMMATISE_CONCURRENCY", "4"))


def _lemmatise_one_batch(
    client, batch_start: int, batch: list[tuple[str, str]]
) -> dict[int, str]:
    """One LLM round-trip. Returns index → lemma for successes; logs and
    returns an empty dict on failure."""
    lines = []
    for j, (word, ctx) in enumerate(batch):
        idx = batch_start + j
        if ctx:
            ctx_short = ctx[:_MAX_CONTEXT_LEN]
            lines.append(f'{idx}. {word} | Context: "{ctx_short}"')
        else:
            lines.append(f"{idx}. {word}")

    prompt = (
        "You are a Spanish lemmatizer. For each numbered word, return its "
        "dictionary headword:\n"
        "- Conjugated verbs → infinitive (esquivó → esquivar, tengo → tener)\n"
        "- Reflexive verb forms → infinitive + se "
        "(meterme → meterse, despertándose → despertarse)\n"
        "- Feminine/plural nouns or adjectives → masculine singular "
        "(bonita → bonito, gatos → gato)\n"
        "- Words already in dictionary form → return unchanged\n"
        "- Use the context sentence (when provided) to disambiguate\n\n"
        "Return ONLY a JSON object mapping each number to its lemma. "
        'Example: {"0": "esquivar", "1": "meterse"}\n\n'
        + "\n".join(lines)
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        json_match = re.search(r"\{[\s\S]*\}", text)
        if not json_match:
            return {}
        parsed = json.loads(json_match.group())
        return {int(k): _clean_lemma(str(v).strip()) for k, v in parsed.items()}
    except Exception as e:
        print(
            f"  Warning: LLM lemmatisation failed for batch at {batch_start}: {e}",
            file=sys.stderr,
        )
        return {}


def _batch_lemmatise_llm(
    entries: list[tuple[str, str]],
) -> dict[int, str]:
    """
    Use Claude API (Haiku) to lemmatise a list of Spanish words in parallel.
    entries: list of (cleaned_word, context_sentence) tuples.
    Returns: dict mapping entry-index → lemma for successful results.

    Slices into 50-word batches and dispatches them to a ThreadPoolExecutor.
    Each batch is one Anthropic round-trip; running 4 in parallel roughly
    quarters wall-clock for any run with ≥4 batches.
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    batches = [
        (start, entries[start : start + _LEMMA_BATCH_SIZE])
        for start in range(0, len(entries), _LEMMA_BATCH_SIZE)
    ]
    if not batches:
        return {}

    results: dict[int, str] = {}
    prog = _progress(len(entries), "lemmatising (Claude)")
    workers = max(1, min(_LEMMATISE_CONCURRENCY, len(batches)))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lemma") as pool:
        futs = {
            pool.submit(_lemmatise_one_batch, client, start, batch): (start, batch)
            for start, batch in batches
        }
        for fut in as_completed(futs):
            start, batch = futs[fut]
            partial = fut.result()
            results.update(partial)
            if prog:
                # Advance by the whole batch size so the bar reflects words,
                # not batches — gives more granular feedback on long runs.
                prog.update(n=len(batch), detail=f"batch@{start}")

    if prog:
        prog.close()
    return results


def _check_llm_available() -> bool:
    """Check if LLM lemmatisation is available; prompt user if not."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("\n  ⚠  ANTHROPIC_API_KEY is not set.")
        print("  Without it, lemmatisation will use Stanza only, which is less accurate")
        print("  (known issues: incorrect lemmas for some conjugated verbs, punctuation artifacts).")
        print("  Set ANTHROPIC_API_KEY in your .env or environment to use Claude Haiku instead.\n")
        try:
            answer = input("  Continue with Stanza-only lemmatisation? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("Aborted. Set ANTHROPIC_API_KEY and try again.")
            sys.exit(0)
        return False

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("\n  ⚠  anthropic package is not installed.")
        print("  Without it, lemmatisation will use Stanza only, which is less accurate.")
        print("  Install with: pip install anthropic\n")
        try:
            answer = input("  Continue with Stanza-only lemmatisation? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("Aborted. Install anthropic and try again.")
            sys.exit(0)
        return False

    return True


def _batch_lemmatise(
    tasks: list[tuple[int, str, str]],
) -> dict[int, str]:
    """
    Lemmatise single words.  Tries Claude API first, then falls back to
    Stanza for any words that the LLM did not cover.

    tasks: list of (row_index, cleaned_word, context_sentence).
    Returns: dict mapping row_index → lemma.
    """
    use_llm = _check_llm_available()
    entries = [(word, ctx) for _, word, ctx in tasks]

    # Primary: LLM batch
    llm_results = _batch_lemmatise_llm(entries) if use_llm else {}

    result: dict[int, str] = {}
    stanza_count = 0

    for j, (row_idx, word, ctx) in enumerate(tasks):
        if j in llm_results and llm_results[j]:
            result[row_idx] = llm_results[j]
        else:
            # Stanza fallback
            lemma = _get_lemma_stanza(word, ctx)
            result[row_idx] = _clean_lemma(lemma) if lemma else ""
            stanza_count += 1

    if stanza_count:
        print(f"  (Stanza fallback used for {stanza_count} words)")

    return result


# ---------------------------------------------------------------------------
# Grouping — keyed on lemma for single words, normalised form for phrases
# ---------------------------------------------------------------------------


def group_rows(rows: list[dict]) -> list[dict]:
    """
    Deduplicate rows, grouping by lemma for single words so that all
    inflected forms of the same word (tengo, tienes, tuvo → tener) collapse
    into one row. Multi-word phrases are grouped by their normalised text.

    Uses LLM-based lemmatisation (Claude Haiku) for accuracy, with Stanza as
    a fallback.  The lemma becomes the Anki card front.  All surface forms
    that appeared in the highlights are preserved in highlight_forms.
    """
    # -- First pass: collect single words that need lemmatisation -------------
    lemma_tasks: list[tuple[int, str, str]] = []
    for i, row in enumerate(rows):
        raw = row.get("highlight_text", "").strip()
        if not raw:
            continue
        norm = _normalise(raw)
        if not norm or " " in norm:
            continue
        cleaned = _clean_word(raw)
        if not cleaned:
            continue
        context = row.get("context_sentence", "")
        lemma_tasks.append((i, cleaned, context))

    total = len(rows)
    print(f"  Lemmatising {len(lemma_tasks)} single words out of {total} rows...")
    lemma_cache = _batch_lemmatise(lemma_tasks)

    # -- Second pass: group rows using pre-computed lemmas --------------------
    seen: dict[str, dict] = {}
    order: list[str] = []

    for i, row in enumerate(rows):
        raw = row.get("highlight_text", "").strip()
        if not raw:
            continue

        norm = _normalise(raw)
        if not norm:
            continue

        is_phrase = " " in norm

        if is_phrase:
            group_key = norm
            canonical = _clean_word(raw)
        else:
            lemma = lemma_cache.get(i, "") or norm
            group_key = _normalise(lemma)
            canonical = _clean_lemma(lemma)

        if group_key not in seen:
            seen[group_key] = {
                "lemma": canonical,
                "translation": "",
                "ipa": "",
                "word_type": "",
                "note_texts": [],
                "highlight_colors": [],
                "highlight_forms": [],
                "indices": [],
                "grouped_ids": [],
                "pages": [],
                "source_contexts": [],
                "context_sentences": [],
            }
            order.append(group_key)

        g = seen[group_key]

        nt = row.get("note_text", "").strip()
        if nt and nt not in g["note_texts"]:
            g["note_texts"].append(nt)

        if raw not in g["highlight_forms"]:
            g["highlight_forms"].append(raw)

        g["indices"].append(row.get("index", ""))
        g["grouped_ids"].append(row.get("grouped_id", ""))

        color = row.get("highlight_color", "")
        if color and color not in g["highlight_colors"]:
            g["highlight_colors"].append(color)

        page = row.get("page", "")
        if page not in g["pages"]:
            g["pages"].append(page)

        sc = row.get("source_context", "")
        if sc and sc not in g["source_contexts"]:
            g["source_contexts"].append(sc)

        cs = row.get("context_sentence", "")
        if cs and cs not in g["context_sentences"]:
            g["context_sentences"].append(cs)

    print()  # newline after progress indicator
    return [seen[k] for k in order]


# ---------------------------------------------------------------------------
# Translation via DeepL API
# ---------------------------------------------------------------------------

REPORT_EVERY = 50


def check_usage(api_key: str) -> tuple[int, int]:
    """
    Fetch character usage from the DeepL API and print a summary.
    Returns (characters_used, characters_limit).
    """
    import urllib.request
    import json

    req = urllib.request.Request(
        "https://api-free.deepl.com/v2/usage",
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    used = data["character_count"]
    limit = data["character_limit"]
    pct = used / limit * 100 if limit else 0
    remaining = limit - used
    print(f"  DeepL usage: {used:,} / {limit:,} chars used ({pct:.1f}%)  —  {remaining:,} remaining")
    return used, limit


_TRANSLATE_CONCURRENCY = int(os.environ.get("DEEPL_CONCURRENCY", "8"))
"""In-flight DeepL calls. DeepL Free tolerates ~8 concurrent requests
comfortably; Pro tolerates more. Override via env var."""


def translate_batch(translator: deepl.Translator, groups: list[dict]) -> None:
    """
    Translate each group's lemma in-place. The lemma is what gets sent to
    DeepL (with the first context sentence for word-sense disambiguation).

    Runs calls concurrently — each DeepL call is a sub-second network
    round-trip, so an 8-way pool drops wall-clock roughly proportionally for
    batches over ~20 items. The DeepL SDK is thread-safe (it's a thin `requests`
    wrapper); each thread reuses the shared `translator` instance.
    """
    if not groups:
        return

    prog = _progress(len(groups), "translating (DeepL)")

    def _one(g: dict) -> tuple[dict, str | Exception]:
        context_str = g["context_sentences"][0] if g["context_sentences"] else None
        try:
            result = translator.translate_text(
                g["lemma"],
                source_lang="ES",
                target_lang="EN-US",
                context=context_str,
            )
            return g, result.text
        except Exception as e:  # pragma: no cover - network path
            return g, e

    workers = max(1, min(_TRANSLATE_CONCURRENCY, len(groups)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deepl") as pool:
        futs = [pool.submit(_one, g) for g in groups]
        for fut in as_completed(futs):
            g, outcome = fut.result()
            if isinstance(outcome, Exception):
                # Preserve existing behavior's fail-loud feel but don't abort
                # the remaining in-flight requests.
                print(f"  ⚠ DeepL failure on {g['lemma']!r}: {outcome}", file=sys.stderr)
                g["translation"] = ""
            else:
                g["translation"] = outcome
            if prog:
                prog.update(detail=g["lemma"])
    if prog:
        prog.close()


# ---------------------------------------------------------------------------
# IPA annotation
# ---------------------------------------------------------------------------


def add_ipa(groups: list[dict]) -> None:
    """Fill in the 'ipa' field from the lemma for single-word groups."""
    prog = _progress(len(groups), "adding IPA")
    for g in groups:
        if " " in g["lemma"]:
            g["ipa"] = ""
        else:
            try:
                g["ipa"] = word_to_ipa(g["lemma"], "es")
            except Exception:
                g["ipa"] = ""
        if prog:
            prog.update(detail=g["lemma"])
    if prog:
        prog.close()


# ---------------------------------------------------------------------------
# Word type annotation
# ---------------------------------------------------------------------------

_nlp = None

_POS_LABELS = {
    "NOUN":  "noun",
    "PROPN": "proper noun",
    "VERB":  "verb",
    "AUX":   "auxiliary verb",
    "ADJ":   "adjective",
    "ADV":   "adverb",
    "PRON":  "pronoun",
    "DET":   "determiner",
    "ADP":   "preposition",
    "CCONJ": "conjunction",
    "SCONJ": "conjunction",
    "NUM":   "numeral",
    "PART":  "particle",
    "INTJ":  "interjection",
}


def _get_nlp():
    global _nlp
    if _nlp is None:
        try:
            import spacy
            _nlp = spacy.load("es_core_news_sm")
        except ImportError:
            raise ImportError("Word type detection requires: pip install spacy && python -m spacy download es_core_news_sm")
        except OSError:
            raise OSError("Spanish model not found. Run: python -m spacy download es_core_news_sm")
    return _nlp


def get_word_type(word: str) -> str:
    """
    Return a descriptive word type for a single Spanish word, e.g.:
    'masculine noun', 'feminine noun', 'verb', 'adjective', 'adverb',
    'ordinal', 'pronoun', 'preposition', 'conjunction', 'interjection', etc.
    Always called on the lemma so the model sees a canonical dictionary form.
    """
    word = word.strip()
    if not word or " " in _normalise(word):
        return ""

    nlp = _get_nlp()
    doc = nlp(word)

    token = next((t for t in doc if t.pos_ != "PUNCT"), None)
    if token is None:
        return ""

    pos = token.pos_
    morph = token.morph

    num_type = morph.get("NumType")
    if num_type and "Ord" in num_type:
        base = "ordinal"
    else:
        base = _POS_LABELS.get(pos, pos.lower())

    gender_bearing = {"noun", "proper noun", "pronoun", "determiner", "adjective", "ordinal"}
    if base in gender_bearing:
        genders = morph.get("Gender")
        if genders:
            g = genders[0].lower()
            if g == "masc":
                return f"masculine {base}"
            if g == "fem":
                return f"feminine {base}"

    return base


_INVARIABLE_OR = frozenset({
    "mejor", "peor", "mayor", "menor", "superior", "inferior",
    "exterior", "interior", "anterior", "posterior", "ulterior",
})


def _feminine_form(masc: str) -> str:
    """Generate the feminine form of a Spanish adjective from its masculine form."""
    low = masc.lower()

    # -o → -a  (bajo → baja, seco → seca)
    if low.endswith("o"):
        return masc[:-1] + "a"

    # Accented endings that lose their accent and add -a
    for accented, repl in [("ón", "ona"), ("án", "ana"), ("ín", "ina"), ("és", "esa")]:
        if low.endswith(accented):
            return masc[:-len(accented)] + repl

    # -or → -ora  (trabajador → trabajadora), but not comparatives
    if low.endswith("or") and low not in _INVARIABLE_OR:
        return masc + "a"

    # Invariable — no change (grande, feliz, etc.)
    return masc


def add_word_type(groups: list[dict]) -> None:
    """Fill in the 'word_type' field from the lemma for single-word groups."""
    prog = _progress(len(groups), "adding word types")
    for g in groups:
        if " " in g["lemma"]:
            g["word_type"] = ""
        else:
            wt = get_word_type(g["lemma"])
            if wt in ("masculine adjective", "feminine adjective"):
                wt = "adjective"
                fem = _feminine_form(g["lemma"])
                if fem != g["lemma"]:
                    g["lemma"] = f"{g['lemma']}/{fem}"
            g["word_type"] = wt
        if prog:
            prog.update(detail=g["lemma"])
    if prog:
        prog.close()


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _build_personal_context(g: dict) -> str:
    """Build a combined context field for Anki cards from notes, context
    sentences, and highlighted forms."""
    sections = []

    if g["note_texts"]:
        sections.append(
            "Notes:\n" + "\n".join(f"• {n}" for n in g["note_texts"])
        )

    if g["context_sentences"]:
        sections.append(
            "Context:\n" + "\n".join(f"• {s}" for s in g["context_sentences"])
        )

    if g["highlight_forms"]:
        sections.append(
            "Highlighted forms:\n" + "\n".join(f"• {f}" for f in g["highlight_forms"])
        )

    return "\n\n".join(sections)


def save_csv(groups: list[dict], out_path: str) -> None:
    fieldnames = [
        "lemma",
        "translation",
        "ipa",
        "word_type",
        "personal_context",
        "note_text",
        "highlight_forms",
        "highlight_colors",
        "indices",
        "grouped_ids",
        "pages",
        "source_contexts",
        "context_sentences",
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for g in groups:
            writer.writerow({
                "lemma": g["lemma"],
                "translation": g["translation"],
                "ipa": g["ipa"],
                "word_type": g["word_type"],
                "personal_context": _build_personal_context(g),
                "note_text": "\n".join(g["note_texts"]),
                "highlight_forms": " | ".join(g["highlight_forms"]),
                "highlight_colors": " | ".join(g["highlight_colors"]),
                "indices": " | ".join(str(x) for x in g["indices"]),
                "grouped_ids": " | ".join(g["grouped_ids"]),
                "pages": " | ".join(str(p) for p in g["pages"]),
                "source_contexts": " | ".join(g["source_contexts"]),
                "context_sentences": "\n".join(f"• {s}" for s in g["context_sentences"]),
            })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _make_output_path(csv_path: str) -> str:
    script_dir = Path(__file__).parent
    output_dir = script_dir / "translated"
    output_dir.mkdir(exist_ok=True)
    stem = re.sub(r"_enriched$", "", Path(csv_path).stem)
    return str(output_dir / f"{stem}_translated.csv")


def main():
    if len(sys.argv) != 2:
        print("Usage: python translate_and_deduplicate.py enriched/file.csv")
        sys.exit(1)

    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        print("Error: DEEPL_API_KEY environment variable not set.")
        print("Get a free key at https://www.deepl.com/pro-api (500k chars/month free)")
        sys.exit(1)

    csv_path = sys.argv[1]
    out_path = _make_output_path(csv_path)

    print(f"Loading {csv_path}...")
    rows = load_csv(csv_path)
    print(f"  {len(rows)} rows loaded.")

    print("Lemmatising and grouping...")
    groups = group_rows(rows)
    print(f"  {len(groups)} unique lemmas (from {len(rows)} rows).")

    print("\nChecking DeepL API usage...")
    used, limit = check_usage(api_key)
    chars_to_translate = sum(len(g["lemma"]) for g in groups)
    projected = used + chars_to_translate
    pct_after = projected / limit * 100 if limit else 0
    print(f"  Estimated chars to translate: {chars_to_translate:,}")
    print(f"  Projected usage after:        {projected:,} / {limit:,} ({pct_after:.1f}%)")
    print(f"  About to translate {len(groups)} lemmas.")
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

    print(f"Writing output to {out_path}...")
    save_csv(groups, out_path)
    print(f"Done. {len(groups)} rows written.")


if __name__ == "__main__":
    main()
