"""
config.py

Loads and validates anki_sync_config.json.

The config defines:
  - Anki deck and sub-deck routing
  - The note type and field mapping (CSV column → Anki field, with policy)
  - ID format (prefix, padding)
  - Tag derivation rules from filenames
  - State file path

Validation is strict: missing required keys raise immediately rather than
failing mid-sync.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "anki_sync_config.json"

VALID_POLICIES = {
    "key",                       # the field used to identify the note (Word)
    "create_only",               # set on creation, never updated
    "managed_bullet_union",      # bullet-union merge on each sync
    "never_touch",               # sync ignores this field entirely
    "sync_internal",             # ID, Previous Lemmas, Sync Metadata
}


@dataclass
class FieldPolicy:
    field_name: str
    csv_column: str | None
    policy: str


@dataclass
class Config:
    deck_root: str
    new_destination: str
    active_for_update: tuple[str, ...]
    veto: str

    note_type: str
    key_field: str
    id_field: str
    previous_lemmas_field: str
    sync_metadata_field: str

    id_prefix: str
    id_padding: int

    field_policies: tuple[FieldPolicy, ...]

    tag_policy: str
    source_from_filename: bool
    color_from_filename: bool
    extra_tags: tuple[str, ...]
    filename_regex: re.Pattern

    state_file: Path

    raw: dict = field(default_factory=dict)

    # ----------------------------------------------------------- Lookups

    def policy_for(self, anki_field: str) -> FieldPolicy | None:
        for fp in self.field_policies:
            if fp.field_name == anki_field:
                return fp
        return None

    def csv_to_field_map(self) -> dict[str, FieldPolicy]:
        return {
            fp.csv_column: fp
            for fp in self.field_policies
            if fp.csv_column is not None
        }

    def all_subdecks(self) -> list[str]:
        return [self.new_destination, *self.active_for_update, self.veto]


def _require(d: dict, *keys: str, where: str = "config") -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            raise ValueError(f"{where}: missing required key {' → '.join(keys)!r}")
        cur = cur[k]
    return cur


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        # Default: alongside this file.
        path = Path(__file__).parent / CONFIG_FILENAME
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    deck_root = _require(raw, "deck_root")
    new_destination = _require(raw, "subdecks", "new_destination")
    active_for_update = tuple(_require(raw, "subdecks", "active_for_update"))
    veto = _require(raw, "subdecks", "veto")

    if new_destination not in active_for_update:
        # The new destination must be one of the active sub-decks (otherwise
        # we'd create cards in a deck the sync doesn't update).
        raise ValueError(
            f"Config: subdecks.new_destination ({new_destination!r}) must "
            f"appear in subdecks.active_for_update ({active_for_update!r})."
        )
    if veto in active_for_update:
        raise ValueError(
            f"Config: subdecks.veto ({veto!r}) must NOT appear in "
            f"subdecks.active_for_update — veto is mutually exclusive."
        )

    note_type = _require(raw, "note_type")
    key_field = _require(raw, "key_field")
    id_field = _require(raw, "id_field")
    previous_lemmas_field = _require(raw, "previous_lemmas_field")
    sync_metadata_field = _require(raw, "sync_metadata_field")

    id_prefix = _require(raw, "id_format", "prefix")
    id_padding = int(_require(raw, "id_format", "padding"))

    fm = _require(raw, "field_mapping")
    if not isinstance(fm, dict) or not fm:
        raise ValueError("Config: field_mapping must be a non-empty object.")

    field_policies: list[FieldPolicy] = []
    seen_keys = 0
    for fname, spec in fm.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Config: field_mapping[{fname!r}] must be an object.")
        policy = spec.get("policy")
        if policy not in VALID_POLICIES:
            raise ValueError(
                f"Config: field_mapping[{fname!r}].policy = {policy!r} "
                f"is not one of {sorted(VALID_POLICIES)}"
            )
        if policy == "key":
            seen_keys += 1
            if fname != key_field:
                raise ValueError(
                    f"Config: field_mapping marks {fname!r} as 'key' but "
                    f"key_field is {key_field!r}; they must match."
                )
        field_policies.append(
            FieldPolicy(
                field_name=fname,
                csv_column=spec.get("csv"),
                policy=policy,
            )
        )

    if seen_keys != 1:
        raise ValueError(
            f"Config: field_mapping must have exactly one field with policy 'key', "
            f"found {seen_keys}."
        )

    # Make sure the system-internal fields are declared in the mapping.
    declared_field_names = {fp.field_name for fp in field_policies}
    for required_internal in (id_field, previous_lemmas_field, sync_metadata_field):
        if required_internal not in declared_field_names:
            raise ValueError(
                f"Config: required system field {required_internal!r} is not "
                f"declared in field_mapping. Add it with policy 'sync_internal'."
            )

    tags = raw.get("tags", {})
    tag_policy = tags.get("policy", "add_only")
    if tag_policy != "add_only":
        # Add other policies later if needed; for now, only add_only is implemented.
        raise ValueError(
            f"Config: tags.policy = {tag_policy!r} is not supported "
            "(only 'add_only' is implemented)."
        )

    parser_regex = _require(raw, "filename_parser", "regex")
    try:
        filename_regex = re.compile(parser_regex)
    except re.error as e:
        raise ValueError(f"Config: filename_parser.regex is not valid: {e}")
    for grp in ("date", "source", "color"):
        if grp not in filename_regex.groupindex:
            raise ValueError(
                f"Config: filename_parser.regex must contain a named group "
                f"(?P<{grp}>...). Got groups: {list(filename_regex.groupindex)}"
            )

    state_file = Path(_require(raw, "state_file"))
    if not state_file.is_absolute():
        # Resolve relative to the project root (parent of anki_sync/).
        project_root = Path(__file__).parent.parent
        state_file = (project_root / state_file).resolve()

    return Config(
        deck_root=deck_root,
        new_destination=new_destination,
        active_for_update=active_for_update,
        veto=veto,
        note_type=note_type,
        key_field=key_field,
        id_field=id_field,
        previous_lemmas_field=previous_lemmas_field,
        sync_metadata_field=sync_metadata_field,
        id_prefix=id_prefix,
        id_padding=id_padding,
        field_policies=tuple(field_policies),
        tag_policy=tag_policy,
        source_from_filename=tags.get("source_from_filename", True),
        color_from_filename=tags.get("color_from_filename", True),
        extra_tags=tuple(tags.get("extra_tags", [])),
        filename_regex=filename_regex,
        state_file=state_file,
        raw=raw,
    )


def parse_filename(stem: str, cfg: Config) -> dict[str, str]:
    """Parse a by_color CSV stem (without .csv) into {date, source, color}."""
    m = cfg.filename_regex.match(stem)
    if not m:
        raise ValueError(
            f"Filename {stem!r} doesn't match filename_parser.regex "
            f"({cfg.filename_regex.pattern!r}). Expected shape: "
            "YYYY-MM-DD-<source>_<color>"
        )
    return m.groupdict()
