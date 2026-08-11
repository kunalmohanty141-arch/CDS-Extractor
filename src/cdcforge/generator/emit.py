"""DDL emission helpers.

Everything the generator writes goes through here, so the annotation shape the
tool emits is the shape the rule engine validates. The layout follows the
verified working example in SAP's TechEd DA281 repository (Appendix A.2).
"""

from __future__ import annotations

from cdcforge.cds import CdcMappingEntry, ROLE_MAIN


def quote(value: str) -> str:
    """CDS string literal, with the ``''`` escape."""
    return "'" + value.replace("'", "''") + "'"


def quote_list(values: list[str]) -> str:
    return "[" + ", ".join(quote(v) for v in values) + "]"


def render_mapping_entry(entry: CdcMappingEntry, *, width: int = 0) -> str:
    parts = [f"table: {quote(entry.table):<{width}}" if width else f"table: {quote(entry.table)}"]
    parts.append(f"role: #{entry.role or ROLE_MAIN}")
    if entry.view_elements:
        parts.append(f"viewElement: {quote_list(entry.view_elements)}")
    if entry.table_elements:
        parts.append(f"tableElement: {quote_list(entry.table_elements)}")
    if entry.filter is not None:
        # Python's repr is not CDS syntax — quote strings properly, and leave
        # anything else to render itself.
        rendered = quote(entry.filter) if isinstance(entry.filter, str) else str(entry.filter)
        parts.append(f"filter: {rendered}")
    return "{" + ", ".join(parts) + "}"


def render_cdc_mapping(entries: list[CdcMappingEntry], indent: str = "        ") -> str:
    """Render the ``mapping:`` array, main entry first."""
    ordered = [e for e in entries if e.is_main] + [e for e in entries if not e.is_main]
    width = max((len(quote(e.table)) for e in ordered), default=0)
    lines = [render_mapping_entry(e, width=width) for e in ordered]
    body = (",\n" + indent).join(lines)
    return body


def render_analytics_block(
    *,
    extraction_enabled: bool = True,
    automatic: bool = False,
    mapping: list[CdcMappingEntry] | None = None,
    data_category: str | None = None,
) -> list[str]:
    """The ``@Analytics`` header block.

    Exactly one of ``automatic`` / ``mapping`` should be given. Single-table
    views qualify for ``changeDataCapture.automatic: true`` and should use it
    rather than a hand-rolled mapping (F-19).
    """
    lines: list[str] = []
    opener = "@Analytics: {"
    if data_category:
        opener += f" dataCategory: #{data_category},"
    lines.append(opener)
    lines.append(f"  dataExtraction: {{ enabled: {'true' if extraction_enabled else 'false'},")

    if automatic:
        lines.append("    delta.changeDataCapture.automatic: true } }")
        return lines

    if mapping:
        lines.append("    delta.changeDataCapture: {")
        lines.append("      mapping: [")
        body = render_cdc_mapping(mapping, indent="        ")
        lines.append("        " + body)
        lines.append("      ] } } }")
        return lines

    lines.append("  } }")
    return lines


def render_header(
    *,
    label: str | None,
    analytics: list[str],
    sql_view_name: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if sql_view_name:
        lines.append(f"@AbapCatalog.sqlViewName: {quote(sql_view_name)}")
    if label:
        lines.append(f"@EndUserText.label: {quote(label)}")
    lines.extend(analytics)
    return lines


def render_element_list(elements: list[tuple]) -> list[str]:
    """Render the projection.

    ``elements`` is a list of ``(is_key, expression, alias)``, optionally with
    a fourth entry: annotation lines to emit above that element. An empty alias
    means the expression is exposed under its own name.

    The fourth entry exists for currency and unit semantics. ``as select from``
    does not inherit a base view's element annotations the way ``as projection
    on`` would, and an amount without its ``@Semantics.amount.currencyCode`` is
    rejected outright — so a wrapper has to carry them across itself.
    """
    # One entry per element, each carrying its own annotations. Elements are
    # separated by commas; an element's annotations are part of it and must sit
    # above it with no comma in between, or the parser reports Unexpected
    # word "@".
    entries: list[str] = []
    for element in elements:
        is_key, expression, alias = element[0], element[1], element[2]
        annotations = element[3] if len(element) > 3 else ()
        prefix = "  key " if is_key else "      "
        text = expression if not alias or alias == expression else f"{expression} as {alias}"
        block = [f"      {annotation}" for annotation in annotations]
        block.append(prefix + text)
        entries.append("\n".join(block))

    return ["{", ",\n".join(entries), "}"]
