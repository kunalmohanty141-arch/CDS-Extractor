"""F-20 — Z-wrapper generator.

For SAP views that lack extraction and cannot be modified: a thin projection
over the original with the extraction and CDC annotations added.

The constraint the specification is emphatic about:

    A wrapper inherits the base view's structure. If the base aggregates,
    unions, or has a to-many join, the wrapper fails too. Run the full rule set
    against the *base* before offering a wrapper. Never offer a wrapper that
    cannot work — that is exactly the false promise that would destroy trust in
    the tool.

So this module takes the base view's assessment as a required input, and
refuses on any HARD violation. A refusal is the product working correctly.
"""

from __future__ import annotations

from cdcforge.cds import ANN_CDC_AUTOMATIC, ANN_DATA_CATEGORY
from cdcforge.generator.emit import render_analytics_block, render_element_list, render_header
from cdcforge.generator.mapping import build_cdc_mapping
from cdcforge.generator.naming import NamingConvention
from cdcforge.generator.ztable import GeneratedObject
from cdcforge.model import Assessment, Outcome, Severity, Verdict
from cdcforge.rules.context import ValidationContext


#: The element annotations a wrapper must carry across, and nothing else. An
#: amount or quantity is meaningless — and unactivatable — without the element
#: that says what it is measured in.
_REFERENCE_ANNOTATIONS = (
    "Semantics.amount.currencyCode",
    "Semantics.quantity.unitOfMeasure",
)


def _origin_column(ctx, element_name: str):
    """The base table column this element ultimately reads, if it is knowable."""
    from cdcforge.lineage import trace_element

    origin = trace_element(ctx.metadata, ctx.view, element_name)
    if origin is None:
        return None
    table = ctx.metadata.get_table(origin.table)
    return table.field_by_name(origin.field) if table is not None else None


def _semantic_annotations(item) -> tuple[str, ...]:
    """The currency/unit annotations declared on a base view's element."""
    annotations = getattr(item, "annotations", None)
    if annotations is None:
        return ()
    lines: list[str] = []
    for key in _REFERENCE_ANNOTATIONS:
        value = annotations.get(key)
        if value:
            lines.append(f"@{key}: '{value}'")
    return tuple(lines)


def generate_wrapper(
    ctx: ValidationContext,
    base_assessment: Assessment,
    *,
    naming: NamingConvention | None = None,
    name: str | None = None,
    label: str | None = None,
    data_category: str | None = None,
    include_tables: set[str] | None = None,
) -> GeneratedObject:
    """Generate a projection wrapper over ``ctx.view``.

    ``base_assessment`` must be the result of validating the base view. It is
    not optional and it is not re-derived here: the caller has to have looked
    at the base before a wrapper can be offered.
    """
    naming = naming or NamingConvention()
    base = ctx.view
    wrapper_name = name or naming.for_wrapper(base.name)
    result = GeneratedObject(
        name=wrapper_name, kind="wrapper", source_object=base.name
    )

    # -- refusals: the base's structure is inherited ----------------------
    if base_assessment.verdict is Verdict.UNPARSEABLE:
        result.refused_because = (
            f"{base.name} could not be parsed, so it cannot be established that a "
            f"wrapper over it would work"
        )
        return result

    blockers = [
        r
        for r in base_assessment.results
        if r.outcome is Outcome.VIOLATED and r.severity is Severity.HARD
    ]
    if blockers:
        reasons = "; ".join(f"{r.rule_id}: {r.message}" for r in blockers[:4])
        more = f" (+{len(blockers) - 4} more)" if len(blockers) > 4 else ""
        result.refused_because = (
            f"a wrapper inherits the structure of {base.name}, and the base view "
            f"fails hard — {reasons}{more}. Adding annotations on top cannot fix "
            f"this."
        )
        return result

    if not base.select_items:
        result.refused_because = f"{base.name} exposes no elements to project"
        return result

    # -- decide the CDC annotation ----------------------------------------
    single_table = not base.joins and base.from_source is not None
    mapping_proposal = None
    inherited: list = []
    automatic = False

    if base.annotations is not None and base.annotations.is_true(ANN_CDC_AUTOMATIC):
        # The base already uses the automatic path and the framework derives
        # the mapping itself; a projection over it keeps that property.
        automatic = True
    elif ctx.mapping and not any(e.problems for e in ctx.mapping):
        # The base already carries a working mapping. Carry SAP's own
        # declaration forward rather than re-deriving one: it is authoritative,
        # it already names the right base tables, and the wrapper exposes the
        # same element names, so the viewElement references still resolve.
        #
        # Re-deriving here was actively worse — it refused views like
        # I_GoodsMovementDocumentDEX for not exposing MATDOC's whole key, when
        # SAP had already written a mapping that works.
        inherited = list(ctx.mapping)
    elif single_table and ctx.source_is_table(base.from_source):
        # Projections directly on one table qualify for the automatic path.
        automatic = True
    else:
        mapping_proposal = build_cdc_mapping(ctx, include=include_tables)
        if not mapping_proposal.ok:
            problems = "; ".join(mapping_proposal.problems) or "no mappable table found"
            result.refused_because = (
                f"a CDC mapping for {base.name} could not be derived: {problems}. "
                f"Offering a wrapper without a working mapping would be a false "
                f"promise."
            )
            return result

    # -- carry forward what the base already knows -------------------------
    #
    # Including its element annotations. ``as projection on`` would inherit
    # them; ``as select from`` does not, and SAP rejects an amount whose
    # currency reference is missing:
    #
    #   E  <view>-FUTUREEVALUATEDAMOUNTVALUE reference information missing
    #      SD_CDS_ENTITY(086)
    #
    # so the semantics have to be copied across by hand. Only the reference
    # annotations are carried — the rest of a base view's annotations describe
    # the base, not the wrapper.
    elements = []
    recast: list[str] = []
    for item in base.select_items:
        if not item.name or item.exposed_association:
            continue
        annotations = _semantic_annotations(item)
        expression = item.name
        if not annotations:
            column = _origin_column(ctx, item.name)
            if column is not None and column.is_amount_or_quantity:
                # An amount whose currency the base never declared. DDIC
                # supplies it while the column is read straight from its
                # table, but that is lost at the view boundary — the base view
                # activates and a wrapper over it does not:
                #
                #   E  <view>-FUTUREEVALUATEDAMOUNTVALUE reference information
                #      missing   SD_CDS_ENTITY(086)
                #
                # There is no annotation to carry across, so the column is kept
                # as a plain decimal. Same trade as the table generator makes:
                # the value survives, the semantic type does not.
                expression = (
                    f"cast({item.name} as abap.dec"
                    f"({column.length or 15},{column.decimals or 0}))"
                )
                recast.append(item.name)
        elements.append((item.is_key, expression, item.name, annotations))

    if recast:
        result.warnings.append(
            f"{len(recast)} amount/quantity element(s) carry no currency or "
            f"unit annotation in {base.name}, and the DDIC reference does not "
            f"survive the view boundary — they are cast to plain decimals, "
            f"values unchanged. Affected: {', '.join(recast[:6])}"
            + (f" and {len(recast) - 6} more" if len(recast) > 6 else "")
        )
    if not any(element[0] for element in elements):
        result.refused_because = (
            f"{base.name} declares no key elements, so the wrapper would have no "
            f"row identity (R-17)"
        )
        return result

    base_category = None
    if base.annotations is not None:
        value = base.annotations.get(ANN_DATA_CATEGORY)
        base_category = getattr(value, "name", None)

    mapping_entries = inherited or (
        mapping_proposal.entries if mapping_proposal else None
    )
    analytics = render_analytics_block(
        extraction_enabled=True,
        automatic=automatic,
        mapping=mapping_entries,
        data_category=data_category or base_category,
    )
    header = render_header(
        label=label or f"Extraction wrapper for {base.name}",
        analytics=analytics,
    )

    # ``as select from``, not ``as projection on``. A projection view entity is
    # a RAP *transactional projection*, and SAP refuses to activate one that is
    # not part of a business object:
    #
    #   E  Transactional Projection View must be part of a business object.
    #      SD_CDS_PC_TQ(009)
    #
    # That rejected every wrapper the tool produced — its main answer for
    # standard SAP content — and nothing caught it until the generated DDL was
    # put in front of the real system. A plain view entity selecting from the
    # base is what an extraction wrapper actually wants: it needs no behaviour
    # definition, and the element list is identical either way. Measured on
    # S/4HANA 816: projection rejected, select activated clean.
    lines = [*header, f"define view entity {wrapper_name}", f"  as select from {base.name}"]
    lines += render_element_list(elements)
    result.ddl = "\n".join(lines) + "\n"

    # -- warnings the user must see before writing --------------------------
    review_items = [
        r
        for r in base_assessment.results
        if r.outcome is Outcome.INCONCLUSIVE
        and r.severity in (Severity.HARD, Severity.MANUAL_REVIEW)
    ]
    for item in review_items:
        result.warnings.append(
            f"unresolved in the base view — {item.rule_id}: {item.message}"
        )

    if inherited:
        result.mandatory_elements = [
            element for entry in inherited for element in entry.view_elements
        ]
        result.warnings.append(
            f"the CDC mapping was carried over from {base.name} unchanged — it "
            f"already names the base tables, and the wrapper exposes the same "
            f"element names"
        )
        if len(inherited) > 1:
            result.warnings.append(
                "deletions propagate only when the #MAIN record is deleted "
                "(F-18, Appendix A.6)"
            )

    if mapping_proposal is not None:
        result.mandatory_elements = mapping_proposal.mandatory_elements
        omitted = [d for d in mapping_proposal.decisions if not d.included]
        for decision in omitted:
            result.warnings.append(
                f"{decision.table} is joined but not mapped — changes there will "
                f"not trigger a delta ({decision.reason})"
            )
        if len(mapping_proposal.entries) > 1:
            result.warnings.append(
                "deletions propagate only when the #MAIN record is deleted "
                "(F-18, Appendix A.6)"
            )

    return result
