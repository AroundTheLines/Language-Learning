#!/usr/bin/env python3
"""
csv_to_ipa.py — Convert words in a CSV column to IPA phonetic spelling.

Usage:
  python csv_to_ipa.py words.csv --language english --column word
  python csv_to_ipa.py words.csv --language spanish --column 0
  python csv_to_ipa.py words.csv --language chinese --column hanzi --output result.csv

Required packages (install only what you need):
  English:    pip install eng-to-ipa
  Chinese:    pip install pypinyin
  All others: pip install epitran
  (epitran covers: Spanish, French, German, Italian, Portuguese, Russian, Arabic, Japanese, Korean, and more)
"""

import argparse
import csv
import re
import sys
import unicodedata
from pathlib import Path


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

# Maps user-facing names/codes → internal code used in converter dispatch
LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en", "en": "en",
    "spanish": "es", "es": "es",
    "french":  "fr", "fr": "fr",
    "german":  "de", "de": "de",
    "italian": "it", "it": "it",
    "portuguese": "pt", "pt": "pt",
    "chinese":  "zh", "zh": "zh", "mandarin": "zh",
    "japanese": "ja", "ja": "ja",
    "korean":   "ko", "ko": "ko",
    "arabic":   "ar", "ar": "ar",
    "russian":  "ru", "ru": "ru",
    "turkish":  "tr", "tr": "tr",
    "dutch":    "nl", "nl": "nl",
    "polish":   "pl", "pl": "pl",
    "hindi":    "hi", "hi": "hi",
}

# epitran codes for each internal language code
# Full list: https://github.com/dmort27/epitran#language-support
EPITRAN_CODES: dict[str, str] = {
    "es": "spa-Latn",
    "fr": "fra-Latn",
    "de": "deu-Latn",
    "it": "ita-Latn",
    "pt": "por-Latn",
    "ja": "jpn-Hira",
    "ko": "kor-Hang",
    "ar": "ara-Arab",
    "ru": "rus-Cyrl",
    "tr": "tur-Latn",
    "nl": "nld-Latn",
    "pl": "pol-Latn",
    "hi": "hin-Deva",
}


# ---------------------------------------------------------------------------
# Per-language converters
# ---------------------------------------------------------------------------

def _to_ipa_english(word: str) -> str:
    try:
        import eng_to_ipa as ipa  # pip install eng-to-ipa
    except ImportError:
        raise ImportError("English IPA requires:  pip install eng-to-ipa")
    result = ipa.convert(word)
    # eng_to_ipa returns the original word unchanged when it can't convert
    return result


def _to_ipa_chinese(word: str) -> str:
    """
    Returns standard pinyin with tone diacritics (the conventional phonetic
    notation for Mandarin). Full IPA mapping for Chinese is complex; pinyin
    is the widely accepted near-IPA representation.
    """
    try:
        from pypinyin import pinyin, Style  # pip install pypinyin
    except ImportError:
        raise ImportError("Chinese IPA requires:  pip install pypinyin")
    syllables = pinyin(word, style=Style.TONE, heteronym=False)
    return " ".join(item[0] for item in syllables)


def _get_epitran_instance(lang_code: str):
    """Return a cached Epitran instance for *lang_code*."""
    if lang_code not in _epitran_cache:
        epitran_code = EPITRAN_CODES.get(lang_code)
        if epitran_code is None:
            raise ValueError(
                f"No epitran mapping for language code '{lang_code}'. "
                "Add it to EPITRAN_CODES or open an issue."
            )
        try:
            import epitran  # pip install epitran
        except ImportError:
            raise ImportError(
                f"This language requires epitran:  pip install epitran\n"
                f"  Some languages also need: pip install panphon"
            )
        _epitran_cache[lang_code] = epitran.Epitran(epitran_code)
    return _epitran_cache[lang_code]


def _to_ipa_epitran(word: str, lang_code: str) -> str:
    return _get_epitran_instance(lang_code).transliterate(word)


# ---------------------------------------------------------------------------
# Spanish IPA prosody — syllabification + stress markers
# ---------------------------------------------------------------------------

_SP_VOWELS          = frozenset("aeiou")
_SP_GLIDES          = frozenset("jw")
_COMBINING_ACUTE    = "\u0301"
_SP_ACCENTED_WEAK   = frozenset("íúÍÚ")          # precomposed accented i/u
_SP_ACCENTED_ALL    = frozenset("áéíóúÁÉÍÓÚ")    # all precomposed accented vowels
_ORTHO_VOWELS       = frozenset("aeiouáéíóú")

# Valid two-consonant onset clusters in Spanish IPA
_VALID_ONSETS = frozenset({
    "bl", "bɾ", "fl", "fɾ", "kl", "kɾ",
    "ɡl", "ɡɾ", "pl", "pɾ", "tɾ", "dɾ",
    "tʃ", "t\u0361ʃ",                      # affricate with and without tie-bar
})

# Multi-character IPA symbols to tokenise as single units.
# Longer sequences must come first (t͡ʃ uses a combining tie-bar U+0361).
_IPA_MULTI = ("t\u0361ʃ", "d\u0361ʒ", "tʃ", "dʒ", "ts")

# Regex matching alphabetic Spanish characters (NFC precomposed)
_SP_ALPHA = re.compile(r"([a-záéíóúüñA-ZÁÉÍÓÚÜÑ]+)")


def _build_ipa_from_tuples(tuples_list) -> tuple[list[str], list[bool]]:
    """
    Build an IPA character list from epitran's word_to_tuples output.

    Also returns a parallel ``is_accented`` boolean list.  An entry is True
    when the IPA character came from an orthographic í or ú (with or without
    the combining acute decomposed as a separate 'M'-category tuple).  These
    positions must *not* be converted to glides — they represent hiatus vowels.
    """
    entries = [(t[2], t[3], t[0]) for t in tuples_list]   # (orth, ipa, cat)
    ipa_chars: list[str] = []
    is_accented: list[bool] = []

    i = 0
    while i < len(entries):
        orth, ipa_out, cat = entries[i]

        if cat == "M":          # Combining mark (e.g. U+0301) — empty IPA, skip
            i += 1
            continue

        accented = False
        if cat == "L":
            if orth in _SP_ACCENTED_WEAK:           # precomposed í / ú (NFC)
                accented = True
            elif orth in "iuIU":                    # plain i/u — peek at next
                if i + 1 < len(entries) and entries[i + 1][2] == "M":
                    accented = True                 # NFD: followed by combining accent

        for ch in ipa_out:
            ipa_chars.append(ch)
            is_accented.append(accented)

        i += 1

    return ipa_chars, is_accented


def _apply_glide_rules(ipa_chars: list[str], is_accented: list[bool]) -> str:
    """
    Convert unaccented i/u into glides j/w where Spanish phonology requires it.
    Accented i/u (is_accented=True) are always left as full vowels (hiatus).

    Two diphthong positions:
      Rising  (C + i/u + V):  glide before the vowel  — e.g. 'susio'    → 'susjo'
      Falling (V + i/u + C):  glide after  the vowel  — e.g. 'tɾeinta'  → 'tɾejnta'
                                                           and 'ɾestauɾante' → 'ɾestawɾante'
    """
    result = list(ipa_chars)
    n = len(result)
    for idx, ch in enumerate(result):
        if ch not in ("i", "u"):
            continue
        if idx < len(is_accented) and is_accented[idx]:
            continue                                # hiatus — keep as full vowel
        glide = "j" if ch == "i" else "w"
        prev_is_cons  = idx == 0 or result[idx - 1] not in _SP_VOWELS
        next_is_vowel = idx + 1 < n and result[idx + 1] in _SP_VOWELS

        if prev_is_cons and next_is_vowel:          # rising  diphthong: C+i/u+V
            result[idx] = glide
        elif not prev_is_cons and not next_is_vowel:# falling diphthong: V+i/u+C
            result[idx] = glide
    return "".join(result)


def _fix_isolated_glides(ipa: str) -> str:
    """
    Convert w/j back to u/i when they have no adjacent vowel at all.

    Epitran sometimes maps 'gu' → 'ɡw' even when u is a full vowel
    (e.g. 'aguja' → 'aɡwxa').  A glide isolated between two consonants
    cannot be part of any diphthong, so restore it to its vowel form.
    """
    chars = list(ipa)
    n = len(chars)
    for idx, ch in enumerate(chars):
        if ch not in ("w", "j"):
            continue
        prev_vowel = idx > 0 and chars[idx - 1] in _SP_VOWELS
        next_vowel = idx + 1 < n and chars[idx + 1] in _SP_VOWELS
        if not prev_vowel and not next_vowel:
            chars[idx] = "u" if ch == "w" else "i"
    return "".join(chars)


def _tokenize_ipa(s: str) -> list[str]:
    """Split an IPA string into phoneme tokens, treating multi-char symbols as one."""
    tokens: list[str] = []
    i = 0
    while i < len(s):
        matched = False
        for mc in _IPA_MULTI:
            if s[i: i + len(mc)] == mc:
                tokens.append(mc)
                i += len(mc)
                matched = True
                break
        if not matched:
            tokens.append(s[i])
            i += 1
    return tokens


def _onset_size(consonants: list[str]) -> int:
    """
    Return how many trailing consonants in *consonants* belong to the next
    syllable's onset (onset-maximisation principle).
    """
    n = len(consonants)
    if n == 0:
        return 0
    if consonants[-1] in _SP_GLIDES:               # glide always goes to onset
        return min(n, 2)
    if n >= 2 and (consonants[-2] + consonants[-1]) in _VALID_ONSETS:
        return 2
    return 1


def _syllabify_ipa(tokens: list[str]) -> list[list[str]]:
    """Group IPA tokens into syllables using Spanish phonotactics."""
    nuclei = [i for i, t in enumerate(tokens) if t in _SP_VOWELS]
    if not nuclei:
        return [list(tokens)]

    syllables: list[list[str]] = []
    syl_start = 0

    for k, nuc_pos in enumerate(nuclei):
        nuc_end = nuc_pos + 1
        next_nuc = nuclei[k + 1] if k + 1 < len(nuclei) else len(tokens)

        # Falling diphthong: vowel immediately followed by a glide (e.g. ej, ow)
        if nuc_end < next_nuc and tokens[nuc_end] in _SP_GLIDES:
            nuc_end += 1

        inter = tokens[nuc_end:next_nuc]
        on_sz = _onset_size(inter)
        coda_sz = len(inter) - on_sz

        syllables.append(list(tokens[syl_start: nuc_end + coda_sz]))
        syl_start = nuc_end + coda_sz

    if syl_start < len(tokens):     # trailing coda goes to last syllable
        syllables[-1] += tokens[syl_start:]

    return syllables


def _stress_index(original_word: str, n_syl: int) -> int:
    """
    Return the 0-based index of the stressed syllable for a Spanish word.

    Rules (in priority order):
      1. Explicit accent mark (á/é/í/ó/ú) → the syllable containing that vowel.
      2. Word ends in a vowel, n, or s → penultimate syllable.
      3. Otherwise → final syllable.
    """
    if n_syl <= 1:
        return 0

    word = unicodedata.normalize("NFC", original_word).lower()
    vowels = [c for c in word if c in _ORTHO_VOWELS]
    acc_pos = next((i for i, c in enumerate(vowels) if c in _SP_ACCENTED_ALL), None)

    if acc_pos is not None:
        n_v = len(vowels)
        if n_v == 0:
            return 0
        # Map vowel index → syllable index (proportional, handles diphthongs)
        if n_v <= n_syl:
            return min(acc_pos, n_syl - 1)
        return min(round(acc_pos * (n_syl - 1) / (n_v - 1)), n_syl - 1)

    # Default rules based on word ending
    last_alpha = next((c for c in reversed(word) if c.isalpha()), "")
    if last_alpha in _ORTHO_VOWELS or last_alpha in ("n", "s"):
        return max(0, n_syl - 2)    # penultimate
    return n_syl - 1                # final


def _ipa_with_prosody(alpha_word: str, epi) -> str:
    """Convert a single alphabetic Spanish word to IPA with stress + syllable dots."""
    if not alpha_word:
        return ""
    try:
        ipa_chars, is_acc = _build_ipa_from_tuples(epi.word_to_tuples(alpha_word))
    except Exception:
        raw = epi.transliterate(alpha_word)
        ipa_chars, is_acc = list(raw), [False] * len(raw)

    ipa_str   = _apply_glide_rules(ipa_chars, is_acc)
    ipa_str   = _fix_isolated_glides(ipa_str)
    tokens    = _tokenize_ipa(ipa_str)
    syllables = _syllabify_ipa(tokens)
    n         = len(syllables)
    si        = _stress_index(alpha_word, n)

    parts = []
    for i, syl in enumerate(syllables):
        s = "".join(syl)
        parts.append(("ˈ" + s) if (n > 1 and i == si) else s)
    return ".".join(parts)


def _to_ipa_spanish(text: str, epi) -> str:
    """
    Convert Spanish text — a word, a phrase, or a word with grammatical
    notation such as 'sentar(se) en' or 'bonito (/-a)' — to IPA with stress
    markers (ˈ) and syllable boundaries (.).

    Non-alphabetic characters (parentheses, slashes, hyphens …) are preserved
    in place; each contiguous alphabetic run is converted independently.
    """
    text = unicodedata.normalize("NFC", text)
    result_tokens = []
    for token in text.split():
        parts = _SP_ALPHA.split(token)   # alternating non-alpha / alpha runs
        out = []
        for part in parts:
            if _SP_ALPHA.fullmatch(part):
                out.append(_ipa_with_prosody(part, epi))
            else:
                out.append(part)
        result_tokens.append("".join(out))
    return " ".join(result_tokens)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Cache epitran instances so we don't reload the model for every word
_epitran_cache: dict[str, object] = {}


def word_to_ipa(word: str, lang_code: str) -> str:
    """Return the IPA (or phonetic) transcription of *word* for *lang_code*."""
    word = word.strip()
    if not word:
        return ""

    if lang_code == "en":
        return _to_ipa_english(word)
    if lang_code == "zh":
        return _to_ipa_chinese(word)
    if lang_code == "es":
        return _to_ipa_spanish(word, _get_epitran_instance("es"))
    return _to_ipa_epitran(word, lang_code)


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------

def resolve_column(fieldnames: list[str], column: str) -> str:
    """Return the actual column name, supporting both name and 0-based index."""
    if column in fieldnames:
        return column
    try:
        idx = int(column)
        return fieldnames[idx]
    except (ValueError, IndexError):
        pass
    raise SystemExit(
        f"Column '{column}' not found.\nAvailable columns: {fieldnames}"
    )


def process_csv(
    input_path: str,
    language: str,
    column: str,
    output_path: str | None = None,
) -> Path:
    input_path = Path(input_path)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    lang_code = LANGUAGE_ALIASES.get(language.lower())
    if lang_code is None:
        raise SystemExit(
            f"Unknown language '{language}'.\n"
            f"Supported: {', '.join(sorted(set(LANGUAGE_ALIASES.values())))}"
        )

    # Default output: same directory, stem + _ipa suffix
    if output_path is None:
        out = input_path.parent / f"{input_path.stem}_ipa{input_path.suffix}"
    else:
        out = Path(output_path)

    # ---- Read ----------------------------------------------------------------
    with open(input_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit("CSV file is empty or has no header row.")
        fieldnames = list(reader.fieldnames)
        word_col = resolve_column(fieldnames, column)
        rows = list(reader)

    # ---- Build new column list (IPA column inserted right after word col) ---
    ipa_col = f"{word_col}_ipa"
    insert_at = fieldnames.index(word_col) + 1
    new_fieldnames = fieldnames[:insert_at] + [ipa_col] + fieldnames[insert_at:]

    # ---- Convert -------------------------------------------------------------
    total = len(rows)
    print(f"Converting {total} entr{'y' if total == 1 else 'ies'} "
          f"[language={language}, column='{word_col}'] …")

    error_count = 0
    for i, row in enumerate(rows, 1):
        word = row.get(word_col, "")
        try:
            row[ipa_col] = word_to_ipa(word, lang_code)
        except Exception as exc:
            row[ipa_col] = ""
            error_count += 1
            if error_count <= 5:
                print(f"  [!] '{word}': {exc}", file=sys.stderr)
        if total >= 200 and i % 100 == 0:
            print(f"  {i}/{total} …")

    # ---- Write ---------------------------------------------------------------
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=new_fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done → {out}")
    if error_count:
        print(f"Note: {error_count} word(s) could not be converted (left blank).")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="csv_to_ipa",
        description="Add an IPA phonetic column to a CSV file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python csv_to_ipa.py vocab.csv --language english  --column word
  python csv_to_ipa.py vocab.csv --language spanish  --column 0
  python csv_to_ipa.py vocab.csv --language chinese  --column hanzi
  python csv_to_ipa.py vocab.csv --language french   --column mot --output french_ipa.csv

supported languages (name or 2-letter code):
  english (en)     spanish (es)     french (fr)      german (de)
  italian (it)     portuguese (pt)  chinese (zh)     japanese (ja)
  korean (ko)      arabic (ar)      russian (ru)     turkish (tr)
  dutch (nl)       polish (pl)      hindi (hi)

installation (install only what you need):
  pip install eng-to-ipa        # English
  pip install pypinyin           # Chinese
  pip install epitran            # everything else
        """,
    )
    parser.add_argument("input", help="Path to the input CSV file")
    parser.add_argument(
        "--language", "-l", required=True,
        help="Language of the words (name or ISO 639-1 code)",
    )
    parser.add_argument(
        "--column", "-c", required=True,
        help="Column name or 0-based index containing the words",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output CSV path (default: <input_stem>_ipa.csv)",
    )

    args = parser.parse_args()
    process_csv(args.input, args.language, args.column, args.output)


if __name__ == "__main__":
    main()
