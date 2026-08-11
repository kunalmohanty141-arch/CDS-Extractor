"""F-19 — Z-table → CDS view generator.

Input: a Z-table. Output: a compliant ``DEFINE VIEW ENTITY`` with all fields,
correct key marking from DD03L, client handling, extraction enabled, and the
appropriate CDC annotation.

Single-table views qualify for ``changeDataCapture.automatic: true``. The
generator emits that rather than a hand-rolled mapping — the framework derives
the key mapping itself, and a hand-written mapping is one more thing that can
drift out of step with the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.generator.emit import render_analytics_block, render_element_list, render_header
from cdcforge.generator.naming import NamingConvention
from cdcforge.metadata.types import TableClass, TableMeta


@dataclass
class GeneratedObject:
    """A generated DDL source, or a refusal to generate one."""

    name: str = ""
    ddl: str = ""
    kind: str = ""
    source_object: str = ""
    warnings: list[str] = field(default_factory=list)
    mandatory_elements: list[str] = field(default_factory=list)
    refused_because: str = ""
    """Set when the tool declines to generate. A refusal is a correct output.

    Offering DDL that cannot work is the false promise that would destroy trust
    in the tool faster than any missing feature.
    """

    @property
    def ok(self) -> bool:
        return bool(self.ddl) and not self.refused_because


def refuse_reason(table: TableMeta) -> str:
    """Why a view over this table cannot be generated, or empty if it can.

    Split out from the generator so a *recommendation* can ask the same
    question the generation will. Found by running twenty unseen tables: the
    plan proposed ``BUILD`` for BSID and BSAK, and `apply` then refused both —
    they are S/4HANA compatibility views over ACDOCA, not tables, and CDC needs
    a database trigger on something real. The tool knew that at planning time
    and said it two steps later.

    Not rare, either. BSID, BSAK, BSIK, BSAD, BSIS and BSAS are all views in
    S/4, which is a large share of any finance backlog.
    """
    if table.table_class is TableClass.UNKNOWN:
        return (
            f"the table class of {table.name} is unknown, so it cannot be "
            f"confirmed as a transparent table (R-21)"
        )
    if table.table_class is not TableClass.TRANSPARENT:
        return (
            f"{table.name} is a {table.table_class.value} table. CDC uses "
            f"database triggers, which need a transparent table (R-21)."
        )
    if not table.fields:
        return f"no field metadata is available for {table.name}"
    if not table.has_primary_key:
        return (
            f"{table.name} has no primary key beyond the client field. A "
            f"Datasphere Replication Flow requires a table with a primary key "
            f"(R-22)."
        )
    return ""


def generate_view_for_table(
    table: TableMeta,
    *,
    naming: NamingConvention | None = None,
    name: str | None = None,
    fields: list[str] | None = None,
    label: str | None = None,
    data_category: str | None = None,
    view_entity: bool = True,
) -> GeneratedObject:
    """Generate an extraction- and CDC-enabled view over one table.

    ``fields`` selects which columns to expose (F-22). Key fields are always
    included whatever the selection says, because the delta logging table is
    built from them.
    """
    naming = naming or NamingConvention()
    view_name = name or naming.for_table(table.name)
    result = GeneratedObject(name=view_name, kind="view_entity", source_object=table.name)

    refusal = refuse_reason(table)
    if refusal:
        result.refused_because = refusal
        return result

    # -- field selection --------------------------------------------------
    key_fields = table.business_key_fields
    mandatory = [naming.element_name(f.name) for f in key_fields]

    if fields is None:
        selected = [f for f in table.fields if not f.is_client]
    else:
        wanted = {f.upper() for f in fields}
        selected = [
            f
            for f in table.fields
            if not f.is_client and (f.name.upper() in wanted or f.is_key)
        ]
        dropped_keys = [f.name for f in key_fields if f.name.upper() not in wanted]
        if dropped_keys:
            result.warnings.append(
                f"key field(s) {', '.join(dropped_keys)} were not selected but "
                f"have been included anyway — all key fields of the main table "
                f"must be exposed (R-13)"
            )

    if table.has_client_field:
        client = table.client_field
        result.warnings.append(
            f"the client field {client.name if client else 'MANDT'} is excluded "
            f"from the projection; a CDS view is client-dependent by default and "
            f"exposing it as an ordinary key is problematic (R-23, Note 2890171)"
        )

    # -- amounts and quantities whose reference lives elsewhere ------------
    #
    # A CURR or QUAN column is meaningless without the currency or unit it is
    # measured in, and CDS enforces that. SAP resolves the reference itself
    # when it points at a column of this table that the view also exposes.
    # When it points at another table it cannot: EKPO.NETPR is priced in
    # EKKO.WAERS, and a single-table view over EKPO has nothing to point at.
    # Activation then fails with
    #
    #   E  <view>-NETPR reference information missing or data type wrong
    #      SD_CDS_ENTITY(086)
    #
    # which rejected the generated view for most business tables — EKPO had 19
    # such columns, BKPF both of its two. Measured against S/4HANA 816: the
    # column can be kept by casting it to a plain decimal, which preserves the
    # value and gives up the semantic type. Dropping the money fields from an
    # extraction view would be worse.
    exposed = {f.name.upper() for f in selected}
    recast: list[str] = []
    casts: dict[str, str] = {}
    for column in selected:
        if not column.is_amount_or_quantity:
            continue
        local = column.reference_is_local(table.name)
        if local and column.ref_field.upper() in exposed:
            continue  # SAP resolves this one by itself
        casts[column.name.upper()] = (
            f"cast({column.name.lower()} as abap.dec"
            f"({column.length or 15},{column.decimals or 0}))"
        )
        where = (
            f"{column.ref_table}.{column.ref_field}"
            if column.ref_table
            else "an unknown column"
        )
        recast.append(f"{column.name} (measured in {where})")

    if recast:
        result.warnings.append(
            f"{len(recast)} amount/quantity column(s) are measured in a unit "
            f"this view cannot reach, so they are cast to plain decimals and "
            f"lose their currency/unit semantics — the values are unchanged. "
            f"Replicate the referenced table alongside to interpret them. "
            f"Affected: {', '.join(recast[:6])}"
            + (f" and {len(recast) - 6} more" if len(recast) > 6 else "")
        )

    # -- emit ---------------------------------------------------------------
    elements = [
        (
            f.is_key and not f.is_client,
            casts.get(f.name.upper(), f.name.upper()),
            naming.element_name(f.name),
        )
        for f in selected
    ]

    analytics = render_analytics_block(
        extraction_enabled=True,
        automatic=True,
        data_category=data_category,
    )
    header = render_header(
        label=label or table.description or f"Extraction view for {table.name}",
        analytics=analytics,
        sql_view_name=None if view_entity else naming.sql_view_name(view_name),
    )

    keyword = "define view entity" if view_entity else "define view"
    lines = [*header, f"{keyword} {view_name}", f"  as select from {table.name.lower()}"]
    lines += render_element_list(elements)

    result.ddl = "\n".join(lines) + "\n"
    result.mandatory_elements = mandatory

    if not view_entity:
        result.kind = "classic_view"
        result.warnings.append(
            "a classic DEFINE VIEW was requested; classic views are deprecated "
            "since ABAP 7.57 / S/4HANA 2022 — generate view entities for new "
            "objects (Appendix A.8)"
        )
    if table.is_hot:
        result.warnings.append(
            f"{table.name} is a high-frequency transactional table — CDC adds "
            f"INSERT/UPDATE/DELETE triggers to its write path (F-17)"
        )
    if table.estimated_rows and table.estimated_rows > 2_000_000_000:
        result.warnings.append(
            f"{table.name} holds an estimated {table.estimated_rows:,} rows, "
            f"above the ~2bn ceiling (R-30)"
        )
    return result
