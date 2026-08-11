"""F-38 — mock / demo mode.

Runs entirely on local DDL fixtures with no SAP connection at all. The
specification makes this MVP rather than V2 for three reasons: the parser and
rule engine can be built and tested before system access exists; a prospect who
will never grant access can still be shown the tool; and it is how the whole
thing gets tested in CI.

Layout::

    fixtures/
      views/*.ddl        one CDS view per file
      tables/*.json      one table object, or a list of them, per file
      objects.json       optional repository headers (package, owner, API state)

Nothing here talks to SAP, and nothing here may start to.
"""

from __future__ import annotations

import json
from pathlib import Path

from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import (
    ApiState,
    FieldMeta,
    ObjectMeta,
    Owner,
    TableClass,
    TableMeta,
    derive_owner,
)


class FixtureError(Exception):
    """A fixture file is malformed. Loud, because a silently-skipped fixture
    turns into a silently-skipped test."""


def _table_from_dict(raw: dict, source_file: Path) -> TableMeta:
    try:
        name = raw["name"]
    except KeyError as exc:  # pragma: no cover - fixture authoring error
        raise FixtureError(f"{source_file}: table entry has no 'name'") from exc

    fields: list[FieldMeta] = []
    for position, raw_field in enumerate(raw.get("fields", []), start=1):
        if isinstance(raw_field, str):
            # Shorthand: "MANDT!" marks a key field, "MATNR" a data field.
            is_key = raw_field.endswith("!")
            fields.append(
                FieldMeta(name=raw_field.rstrip("!"), position=position, is_key=is_key)
            )
            continue
        fields.append(
            FieldMeta(
                name=raw_field["name"],
                position=raw_field.get("position", position),
                is_key=bool(raw_field.get("key", raw_field.get("is_key", False))),
                data_element=raw_field.get("data_element", ""),
                data_type=raw_field.get("type", raw_field.get("data_type", "")),
                length=int(raw_field.get("length", 0)),
                decimals=int(raw_field.get("decimals", 0)),
                label=raw_field.get("label", ""),
                not_null=bool(raw_field.get("not_null", False)),
            )
        )

    owner_raw = raw.get("owner")
    owner = Owner(owner_raw.upper()) if owner_raw else derive_owner(name)

    return TableMeta(
        name=name.upper(),
        table_class=TableClass.parse(raw.get("table_class", "TRANSP")),
        delivery_class=raw.get("delivery_class", ""),
        package=raw.get("package", ""),
        owner=owner,
        description=raw.get("description", ""),
        fields=fields,
        estimated_rows=raw.get("estimated_rows"),
    )


def _object_from_dict(raw: dict) -> ObjectMeta:
    name = raw["name"]
    owner_raw = raw.get("owner")
    return ObjectMeta(
        name=name.upper(),
        kind=raw.get("kind", "DDLS"),
        package=raw.get("package", ""),
        software_component=raw.get("software_component", ""),
        owner=Owner(owner_raw.upper()) if owner_raw else derive_owner(name),
        api_state=ApiState.parse(raw.get("api_state")),
        description=raw.get("description", ""),
    )


class MockMetadataSource(MetadataSource):
    """Fixture-backed metadata."""

    name = "fixtures"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._tables: dict[str, TableMeta] = {}
        self._views: dict[str, str] = {}
        self._view_files: dict[str, Path] = {}
        self._objects: dict[str, ObjectMeta] = {}
        self._load()

    # -- loading ----------------------------------------------------------
    def _load(self) -> None:
        self._load_tables(self.root / "tables")
        self._load_views(self.root / "views")
        self._load_objects(self.root / "objects.json")

    def _load_tables(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            raw = self._read_json(path)
            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                table = _table_from_dict(entry, path)
                self._tables[table.name] = table

    def _load_views(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.ddl")):
            source = path.read_text(encoding="utf-8")
            key = path.stem.upper()
            self._views[key] = source
            self._view_files[key] = path

    def _load_objects(self, path: Path) -> None:
        if not path.is_file():
            return
        raw = self._read_json(path)
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            obj = _object_from_dict(entry)
            self._objects[obj.name] = obj

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{path}: invalid JSON — {exc}") from exc

    # -- MetadataSource ---------------------------------------------------
    def get_table(self, name: str) -> TableMeta | None:
        return self._tables.get(name.upper())

    def get_view_source(self, name: str) -> str | None:
        return self._views.get(name.upper())

    def get_object(self, name: str) -> ObjectMeta | None:
        key = name.upper()
        known = self._objects.get(key)
        if known is not None:
            return known
        # An object present as a fixture but absent from objects.json still has
        # a knowable owner from its namespace. The API state stays UNKNOWN,
        # which the modifiability rule treats as unmodifiable — fail safe.
        if key in self._views:
            return ObjectMeta(name=key, kind="DDLS", owner=derive_owner(key))
        if key in self._tables:
            table = self._tables[key]
            return ObjectMeta(
                name=key, kind="TABL", owner=table.owner, package=table.package
            )
        return None

    def list_tables(self) -> list[str]:
        return sorted(self._tables)

    def list_views(self) -> list[str]:
        return sorted(self._views)

    def describe(self) -> str:
        return (
            f"fixtures at {self.root} "
            f"({len(self._views)} views, {len(self._tables)} tables)"
        )
