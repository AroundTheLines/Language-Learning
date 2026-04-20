"""
ankiconnect.py

Thin HTTP client for the AnkiConnect add-on (https://foosoft.net/projects/anki-connect/).

Safety model:
  - Every action is classified READ or WRITE.
  - The client is constructed with `allow_writes` explicitly. If False, calling
    a WRITE action raises before the HTTP request is sent.
  - Unknown actions are rejected outright (defense against typos that might
    accidentally hit a destructive endpoint).

Stdlib only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"
ANKICONNECT_VERSION = 6

# Read-only actions used by the sync system. Add to this list as needed; new
# actions must be classified before the client will dispatch them.
READ_ACTIONS = frozenset({
    "deckNames",
    "deckNamesAndIds",
    "modelNames",
    "modelNamesAndIds",
    "modelFieldNames",
    "findNotes",
    "findCards",
    "notesInfo",
    "cardsInfo",
    "cardsToNotes",
    "getTags",
    "version",
})

# Write actions used by the sync system.
WRITE_ACTIONS = frozenset({
    "addNote",
    "updateNoteFields",
    "addTags",
    "removeTags",
    "changeDeck",
})


class AnkiConnectError(RuntimeError):
    pass


class AnkiConnect:
    """AnkiConnect HTTP client.

    Parameters
    ----------
    url : str
        The AnkiConnect URL (default http://127.0.0.1:8765).
    allow_writes : bool
        If False (default), any WRITE action raises AnkiConnectError before
        the HTTP request. Set True only when --apply is in effect.
    timeout : float
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        allow_writes: bool = False,
        timeout: float = 15.0,
    ):
        self.url = url
        self.allow_writes = allow_writes
        self.timeout = timeout

    def invoke(self, action: str, **params):
        if action in WRITE_ACTIONS:
            if not self.allow_writes:
                raise AnkiConnectError(
                    f"Refusing WRITE action '{action}': client is read-only "
                    "(construct AnkiConnect(allow_writes=True) to permit)."
                )
        elif action not in READ_ACTIONS:
            raise AnkiConnectError(
                f"Action '{action}' is not in the READ or WRITE whitelist. "
                "Classify it in ankiconnect.py before dispatching."
            )

        payload = json.dumps(
            {"action": action, "version": ANKICONNECT_VERSION, "params": params}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise AnkiConnectError(
                f"Cannot reach AnkiConnect at {self.url}: {e}. "
                "Is Anki running with the AnkiConnect add-on installed?"
            ) from e

        if body.get("error"):
            raise AnkiConnectError(f"AnkiConnect error on '{action}': {body['error']}")
        return body.get("result")

    # -------------------------------------------------------------------------
    # Convenience wrappers for the actions used by sync.
    # -------------------------------------------------------------------------

    def find_notes(self, query: str) -> list[int]:
        return self.invoke("findNotes", query=query) or []

    def notes_info(self, note_ids: list[int]) -> list[dict]:
        if not note_ids:
            return []
        return self.invoke("notesInfo", notes=note_ids) or []

    def cards_info(self, card_ids: list[int]) -> list[dict]:
        if not card_ids:
            return []
        return self.invoke("cardsInfo", cards=card_ids) or []

    def deck_names(self) -> list[str]:
        return self.invoke("deckNames") or []

    def model_field_names(self, model_name: str) -> list[str]:
        return self.invoke("modelFieldNames", modelName=model_name) or []

    def add_note(
        self,
        deck_name: str,
        model_name: str,
        fields: dict[str, str],
        tags: list[str],
    ) -> int:
        """Returns the new note ID."""
        return self.invoke(
            "addNote",
            note={
                "deckName": deck_name,
                "modelName": model_name,
                "fields": fields,
                "tags": tags,
                "options": {
                    # Don't allow duplicate Word values across the same note type.
                    # If this fires, the sync's lookup logic missed something —
                    # better to surface as an error than silently create a dup.
                    "allowDuplicate": False,
                    "duplicateScope": "deck",
                    "duplicateScopeOptions": {
                        "deckName": deck_name,
                        "checkChildren": True,
                        "checkAllModels": False,
                    },
                },
            },
        )

    def update_note_fields(self, note_id: int, fields: dict[str, str]) -> None:
        self.invoke(
            "updateNoteFields",
            note={"id": note_id, "fields": fields},
        )

    def add_tags(self, note_ids: list[int], tags: str) -> None:
        """Anki's `addTags` takes a space-separated tag string."""
        if not note_ids or not tags.strip():
            return
        self.invoke("addTags", notes=note_ids, tags=tags)

    def change_deck(self, card_ids: list[int], deck: str) -> None:
        if not card_ids:
            return
        self.invoke("changeDeck", cards=card_ids, deck=deck)
