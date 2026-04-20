"""
state.py

Manages anki_sync_state.json — the local mirror of which lemmas the sync
system has assigned IDs to and what their current Anki status is.

Crucially: this file is *not* the source of truth. Anki is. The sync writes
ID/Previous Lemmas/Sync Metadata into the Anki cards themselves, so this
file can be deleted at any time and reconstructed via anki_rebuild_state.py.

State format (version 1):
{
  "version": 1,
  "next_id": 124,
  "id_prefix": "LX-",
  "id_padding": 6,
  "last_sync": "2026-04-19T10:00:00Z",
  "entries": {
    "LX-000001": {
      "current_lemma": "asustar",
      "previous_lemmas": [],
      "anki_note_id": 1775693877353,
      "first_synced": "2026-04-08",
      "first_source": "percy_jackson",
      "last_synced": "2026-04-19",
      "status": "active"           # active | vetoed | hard_deleted
    }
  }
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


STATE_VERSION = 1

VALID_STATUSES = {"active", "vetoed", "hard_deleted"}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(
        self,
        path: str | Path,
        id_prefix: str = "LX-",
        id_padding: int = 6,
    ):
        self.path = Path(path)
        self.id_prefix = id_prefix
        self.id_padding = id_padding
        self.data = self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> dict:
        if not self.path.exists():
            return {
                "version": STATE_VERSION,
                "next_id": 1,
                "id_prefix": self.id_prefix,
                "id_padding": self.id_padding,
                "last_sync": None,
                "entries": {},
            }
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != STATE_VERSION:
            raise RuntimeError(
                f"State file {self.path} has version {data.get('version')}, "
                f"expected {STATE_VERSION}. Migrate or rebuild."
            )
        # Honor saved formatting if present.
        self.id_prefix = data.get("id_prefix", self.id_prefix)
        self.id_padding = data.get("id_padding", self.id_padding)
        return data

    def save(self) -> None:
        self.data["last_sync"] = _now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp + rename.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(self.path)

    # -------------------------------------------------------------- ID mint

    def mint_id(self) -> str:
        """Allocate the next sequential ID and bump the counter."""
        n = self.data["next_id"]
        self.data["next_id"] = n + 1
        return f"{self.id_prefix}{n:0{self.id_padding}d}"

    def reserve_id(self, id_str: str) -> None:
        """Ensure next_id is past `id_str`. Used by rebuild + bootstrap."""
        try:
            n = int(id_str.removeprefix(self.id_prefix))
        except ValueError:
            return
        if n >= self.data["next_id"]:
            self.data["next_id"] = n + 1

    # ----------------------------------------------------------- Lookups

    def get(self, id_: str) -> dict | None:
        return self.data["entries"].get(id_)

    def find_by_lemma(self, lemma: str) -> tuple[str | None, dict | None]:
        """Return (id, entry) where the lemma matches current_lemma OR
        appears in previous_lemmas. Returns (None, None) if not found."""
        for id_, entry in self.data["entries"].items():
            if entry.get("current_lemma") == lemma:
                return id_, entry
            if lemma in entry.get("previous_lemmas", ()):
                return id_, entry
        return None, None

    def all_entries(self) -> Iterator[tuple[str, dict]]:
        return iter(self.data["entries"].items())

    # ----------------------------------------------------------- Mutations

    def upsert_entry(
        self,
        id_: str,
        *,
        current_lemma: str,
        anki_note_id: int | None,
        first_source: str,
        status: str = "active",
    ) -> dict:
        """Create or update an entry. Detects renames automatically: if the
        existing current_lemma differs from the new one, the old value is
        moved into previous_lemmas."""
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")

        existing = self.data["entries"].get(id_)
        today = _today()

        if existing is None:
            entry = {
                "current_lemma": current_lemma,
                "previous_lemmas": [],
                "anki_note_id": anki_note_id,
                "first_synced": today,
                "first_source": first_source,
                "last_synced": today,
                "status": status,
            }
            self.data["entries"][id_] = entry
            self.reserve_id(id_)
            return entry

        # Update path.
        if existing["current_lemma"] != current_lemma:
            prev = existing.setdefault("previous_lemmas", [])
            old_lemma = existing["current_lemma"]
            if old_lemma and old_lemma not in prev and old_lemma != current_lemma:
                prev.append(old_lemma)
            existing["current_lemma"] = current_lemma

        if anki_note_id is not None:
            existing["anki_note_id"] = anki_note_id
        existing["last_synced"] = today
        existing["status"] = status
        # first_source is immutable once set.
        existing.setdefault("first_source", first_source)
        existing.setdefault("first_synced", today)
        return existing

    def mark_status(self, id_: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        entry = self.data["entries"].get(id_)
        if entry is None:
            return
        entry["status"] = status
        entry["last_synced"] = _today()
        if status == "hard_deleted":
            entry["anki_note_id"] = None

    def record_rename(self, id_: str, new_lemma: str) -> str | None:
        """Record that the Anki Word field for `id_` is now `new_lemma`.
        Returns the previous lemma if a rename occurred, else None."""
        entry = self.data["entries"].get(id_)
        if entry is None:
            return None
        old = entry["current_lemma"]
        if old == new_lemma:
            return None
        prev = entry.setdefault("previous_lemmas", [])
        if old and old not in prev:
            prev.append(old)
        entry["current_lemma"] = new_lemma
        entry["last_synced"] = _today()
        return old
