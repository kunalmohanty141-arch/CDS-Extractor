"""F-25 — the naming convention engine.

Configurable templates, bulk preview, and collision detection against existing
objects *before* anything is written. A batch that discovers a name collision
half way through activation is a batch that leaves the system in a state
somebody has to unpick by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cdcforge.metadata.base import MetadataSource

#: Maximum length of a DDL source / CDS entity name.
MAX_CDS_NAME = 30

#: Maximum length of the generated DDIC SQL view name for a classic view.
MAX_SQL_VIEW_NAME = 16

_INVALID = re.compile(r"[^A-Z0-9_/]")


@dataclass(frozen=True)
class NamingConvention:
    """Templates for generated object names.

    Placeholders: ``{TABLE}``, ``{VIEW}``, ``{NAME}``, ``{PREFIX}``.
    """

    view_from_table: str = "ZI_{TABLE}"
    wrapper_from_view: str = "ZC_{VIEW}_EX"
    sql_view_from_name: str = "ZV{NAME}"
    prefix: str = "Z"
    element_style: str = "preserve"
    """``preserve`` keeps DDIC field names as element names; ``camel`` converts
    to CamelCase.

    ``preserve`` is the default because it makes the generated view's element
    names identical to the table's field names, which keeps the CDC mapping
    unambiguous and makes a generated view trivially reviewable against DD03L.
    """

    def _render(self, template: str, **values: str) -> str:
        rendered = template.format(PREFIX=self.prefix, **values)
        return _INVALID.sub("_", rendered.upper())

    def for_table(self, table_name: str) -> str:
        return self._render(self.view_from_table, TABLE=_strip_namespace(table_name).upper())

    def for_wrapper(self, view_name: str) -> str:
        return self._render(self.wrapper_from_view, VIEW=_strip_namespace(view_name).upper())

    def sql_view_name(self, cds_name: str) -> str:
        """Only needed for classic views; view entities generate nothing separate."""
        stem = _strip_namespace(cds_name).upper()
        for lead in ("ZI_", "ZC_", "Z_"):
            if stem.startswith(lead):
                stem = stem[len(lead) :]
                break
        return self._render(self.sql_view_from_name, NAME=stem)[:MAX_SQL_VIEW_NAME]

    def element_name(self, field_name: str) -> str:
        if self.element_style == "camel":
            return _to_camel(field_name)
        return field_name.upper()


def _strip_namespace(name: str) -> str:
    """``/ACME/TAB`` → ``ACME_TAB``, so a namespaced source yields a legal Z name."""
    return name.strip("/").replace("/", "_")


def _to_camel(field_name: str) -> str:
    parts = [p for p in field_name.split("_") if p]
    return "".join(p.capitalize() for p in parts) or field_name


@dataclass
class NameCheck:
    """The result of checking one proposed name."""

    proposed: str
    source_object: str = ""
    too_long: bool = False
    collides_with: str = ""
    invalid_reason: str = ""

    @property
    def ok(self) -> bool:
        return not (self.too_long or self.collides_with or self.invalid_reason)

    @property
    def problem(self) -> str:
        if self.too_long:
            return (
                f"{self.proposed!r} is {len(self.proposed)} characters; the limit "
                f"is {MAX_CDS_NAME}"
            )
        if self.collides_with:
            return f"{self.proposed!r} already exists in the system ({self.collides_with})"
        return self.invalid_reason


@dataclass
class NamePreview:
    """Bulk preview for a batch (F-25)."""

    checks: list[NameCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def problems(self) -> list[NameCheck]:
        return [c for c in self.checks if not c.ok]

    def add(self, check: NameCheck) -> None:
        self.checks.append(check)


def check_name(
    proposed: str, metadata: MetadataSource, *, source_object: str = ""
) -> NameCheck:
    """Validate one proposed name against length, charset and collisions."""
    check = NameCheck(proposed=proposed, source_object=source_object)

    if not proposed:
        check.invalid_reason = "empty name"
        return check
    if len(proposed) > MAX_CDS_NAME:
        check.too_long = True
        return check
    if not proposed[0].isalpha() and not proposed.startswith("/"):
        check.invalid_reason = f"{proposed!r} must start with a letter"
        return check
    if proposed[0].upper() not in ("Z", "Y") and not proposed.startswith("/"):
        check.invalid_reason = (
            f"{proposed!r} is not in the customer namespace — generated objects "
            f"must start with Z or Y, or sit in a registered namespace"
        )
        return check

    if metadata.get_view_source(proposed) is not None:
        check.collides_with = "existing CDS view"
    elif metadata.get_table(proposed) is not None:
        check.collides_with = "existing table"
    elif metadata.get_object(proposed) is not None:
        check.collides_with = "existing repository object"
    return check


def preview_names(
    proposals: dict[str, str], metadata: MetadataSource
) -> NamePreview:
    """Check a whole batch. ``proposals`` maps source object → proposed name."""
    preview = NamePreview()
    seen: dict[str, str] = {}
    for source_object, proposed in proposals.items():
        check = check_name(proposed, metadata, source_object=source_object)
        # A batch can also collide with itself — two tables whose names differ
        # only outside the template's placeholder produce one name twice.
        if check.ok and proposed.upper() in seen:
            check.collides_with = f"also generated for {seen[proposed.upper()]}"
        seen.setdefault(proposed.upper(), source_object)
        preview.add(check)
    return preview
