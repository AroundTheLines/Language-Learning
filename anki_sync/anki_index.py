"""
anki_index.py

Builds a one-shot in-memory index of every managed note in the configured deck.

This avoids the N+1 query problem: instead of querying AnkiConnect once per
CSV row (slow for 700+ rows), we pull the whole managed-note set up front and
then look up by ID, by Word, or by note ID against local dicts.

Index shape:
    NoteRecord:
      note_id          int
      word             str   (HTML-stripped Word field)
      id_value         str   (Word-Sync ID like 'LX-000123', '' if unset)
      previous_lemmas  list[str]
      sync_metadata    dict   (parsed JSON from the field, {} if invalid)
      tags             list[str]
      deck             str
      card_ids         list[int]
      raw              dict   (the full notesInfo response, for diff/audit)

    AnkiIndex:
      by_note_id       dict[int, NoteRecord]
      by_id_value      dict[str, NoteRecord]    (only IDs that are populated)
      by_word          dict[str, NoteRecord]    (lowercased word for lookup)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .ankiconnect import AnkiConnect
from .config import Config

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub("", s or "").strip()


def _word_key(s: str) -> str:
    return _strip_html(s).casefold()


@dataclass
class NoteRecord:
    note_id: int
    word: str
    id_value: str
    previous_lemmas: list[str]
    sync_metadata: dict
    tags: list[str]
    deck: str
    card_ids: list[int]
    raw: dict = field(repr=False)

    @property
    def has_id(self) -> bool:
        return bool(self.id_value)

    def field_value(self, name: str) -> str:
        return self.raw["fields"].get(name, {}).get("value", "")


@dataclass
class AnkiIndex:
    by_note_id: dict[int, NoteRecord]
    by_id_value: dict[str, NoteRecord]
    by_word: dict[str, NoteRecord]

    @classmethod
    def empty(cls) -> "AnkiIndex":
        return cls({}, {}, {})

    def find_by_lemma(self, lemma: str) -> NoteRecord | None:
        """Lookup by Word value (case-insensitive)."""
        return self.by_word.get(_word_key(lemma))

    def find_by_id_value(self, id_value: str) -> NoteRecord | None:
        return self.by_id_value.get(id_value)

    def __len__(self) -> int:
        return len(self.by_note_id)


def _parse_previous_lemmas(field_value: str) -> list[str]:
    s = _strip_html(field_value)
    if not s:
        return []
    # Pipe-separated; tolerate commas as fallback.
    if "|" in s:
        parts = s.split("|")
    else:
        parts = s.split(",")
    return [p.strip() for p in parts if p.strip()]


def _parse_sync_metadata(field_value: str) -> dict:
    s = _strip_html(field_value)
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return {}


def build_index(anki: AnkiConnect, cfg: Config) -> AnkiIndex:
    """Query every note in deck_root with the configured note type, plus
    each note's sub-deck membership. Returns the populated index."""
    # Anki search syntax: `note:"<value>"` — quotes wrap the value, not the
    # whole field:value token. The earlier (broken) form `"note:..."` matched
    # the literal string and returned zero notes.
    query = f'deck:"{cfg.deck_root}" note:"{cfg.note_type}"'
    note_ids = anki.find_notes(query)
    if not note_ids:
        # Diagnose. The two common causes are: (a) deck name typo, (b) note
        # type name has invisible whitespace (Anki allows trailing spaces).
        all_decks = anki.deck_names()
        all_models = anki.invoke("modelNames") or []
        deck_hits = [d for d in all_decks if d == cfg.deck_root]
        model_hits = [m for m in all_models if m == cfg.note_type]
        looks_like_models = [m for m in all_models if m.strip() == cfg.note_type.strip()]
        msg = [
            f"Query returned 0 notes: {query!r}",
            f"  deck   '{cfg.deck_root}' exists in Anki: {bool(deck_hits)}",
            f"  model  '{cfg.note_type}' exists in Anki: {bool(model_hits)}",
        ]
        if not model_hits and looks_like_models:
            msg.append(
                "  ⚠ Models with the same name modulo whitespace exist:"
            )
            for m in looks_like_models:
                msg.append(f"      {m!r}  (len={len(m)})")
            msg.append(
                "  → Update note_type in anki_sync_config.json to match exactly, "
                "including any trailing/leading spaces."
            )
        raise RuntimeError("\n".join(msg))

    infos = anki.notes_info(note_ids)

    # Pull deck membership for every card (notes have one or more cards;
    # we use the first card's deck as the note's "location"). For our note
    # type that has a single template, every note has exactly one card.
    all_card_ids: list[int] = []
    for info in infos:
        all_card_ids.extend(info.get("cards", []))
    cards = anki.cards_info(all_card_ids)
    card_to_deck = {c["cardId"]: c["deckName"] for c in cards}

    index = AnkiIndex.empty()
    for info in infos:
        note_id = info["noteId"]
        fields = info["fields"]
        word_raw = fields.get(cfg.key_field, {}).get("value", "")
        id_raw = fields.get(cfg.id_field, {}).get("value", "")
        prev_raw = fields.get(cfg.previous_lemmas_field, {}).get("value", "")
        meta_raw = fields.get(cfg.sync_metadata_field, {}).get("value", "")

        word = _strip_html(word_raw)
        id_value = _strip_html(id_raw)
        card_ids = info.get("cards", [])
        deck = card_to_deck.get(card_ids[0], "<unknown>") if card_ids else "<unknown>"

        record = NoteRecord(
            note_id=note_id,
            word=word,
            id_value=id_value,
            previous_lemmas=_parse_previous_lemmas(prev_raw),
            sync_metadata=_parse_sync_metadata(meta_raw),
            tags=list(info.get("tags", [])),
            deck=deck,
            card_ids=card_ids,
            raw=info,
        )

        index.by_note_id[note_id] = record
        if id_value:
            if id_value in index.by_id_value:
                # Two notes claim the same ID — corruption. Surface loudly.
                raise RuntimeError(
                    f"Duplicate ID {id_value!r} in Anki: notes "
                    f"{index.by_id_value[id_value].note_id} and {note_id}. "
                    "Resolve manually before syncing."
                )
            index.by_id_value[id_value] = record
        if word:
            # First-write wins; if there are duplicate Word values they're
            # likely the legacy on `2.1.` and a re-import. Keep the existing
            # one to make rename-detection work; the bootstrap audit handles
            # dup-Word cleanup.
            index.by_word.setdefault(_word_key(word), record)

    return index
