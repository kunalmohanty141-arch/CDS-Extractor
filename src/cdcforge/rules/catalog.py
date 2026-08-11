"""R-01 … R-30 — the CDC constraint set.

Every rule is independently testable against a DDL fixture, and every rule
states the SAP source it derives from. Where a rule cannot decide, it says
INCONCLUSIVE rather than guessing; the verdict model turns that into
MANUAL_REVIEW, never PASS.

Two implementation decisions worth calling out, because neither is spelled out
in the rule table:

* **R-10 vs R-11 split.** R-11 owns the join *type* (INNER / RIGHT / CROSS /
  FULL are structurally impossible). R-10 owns the *cardinality* of a LEFT
  OUTER join. Reporting one defect twice under two rule IDs would bury the real
  reason, so each join produces one finding, from one rule.

* **A bare ``LEFT OUTER JOIN`` is MANUAL_REVIEW, not PASS and not FAIL.** The
  framework needs a to-one shape. Plain ``LEFT OUTER JOIN`` neither declares nor
  denies one, and ABAP does not validate cardinality at runtime (Appendix F.2).
  Passing it would be exactly the false PASS the tool exists to prevent;
  failing it would discard viable candidates.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from cdcforge.cds import (
    ANN_CDC_AUTOMATIC,
    ANN_CDC_MAPPING,
    ANN_DELTA_BY_ELEMENT,
    ANN_EXTRACTION_ENABLED,
    is_client_field,
)
from cdcforge.metadata.types import TableClass, TableMeta
from cdcforge.model import RuleResult, Severity, SourceRef
from cdcforge.parsing.lexer import TokenKind
from cdcforge.parsing.nodes import EntityKind, JoinCardinality, JoinType, ParsedView
from cdcforge.rules.base import (
    RuleSpec,
    inconclusive,
    not_applicable,
    rule,
    satisfied,
    violated,
)
from cdcforge.rules.context import CardinalityResult, SubscriptionState, ValidationContext
from cdcforge.rules.stack import NodeKind, StackNode

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stack_view_nodes(ctx: ValidationContext) -> list[StackNode]:
    """Every parsed view in the stack, root first, then by depth."""
    root_key = ctx.stack.root_name.upper()
    nodes = [
        n for n in ctx.stack.nodes.values() if n.kind is NodeKind.VIEW and n.view
    ]
    return sorted(nodes, key=lambda n: (0 if n.name == root_key else 1, n.depth, n.name))


def _where(ctx: ValidationContext, node: StackNode) -> str:
    """Locate a finding that may sit in a view further down the stack."""
    if node.name == ctx.stack.root_name.upper():
        return ""
    return f" in {node.name}, which this view reads"


def _view_ref(view: ParsedView) -> SourceRef:
    return SourceRef(line=1, column=1, snippet=view.name)


def _mentions_identifier(ctx: ValidationContext, name: str) -> bool:
    """Is this identifier mentioned by an element the tool could *not* trace?

    Used to separate "the field is not exposed" (a finding) from "it may be
    buried in an expression I cannot trace" (an inconclusive).

    Only untraceable elements are scanned. Elements whose origin resolved
    cleanly are already accounted for in the origin index, and counting them
    here would let ``VBAP.VBELN`` vouch for ``VBAK.VBELN`` simply because both
    columns happen to share a name — turning a real finding into a shrug.
    """
    target = name.upper()
    for item in ctx.view.select_items:
        if item.is_virtual or item.exposed_association:
            continue
        if ctx.origin_of(item) is not None:
            continue
        for token in item.tokens:
            if token.kind is TokenKind.IDENT and token.upper == target:
                return True
    return False


def _exposure(
    ctx: ValidationContext, table_name: str, field_name: str
) -> tuple[str, list[str]]:
    """Is ``table.field`` exposed as a view element?

    Returns ``("exposed", elements)``, ``("missing", [])`` or
    ``("uncertain", [])``.
    """
    elements = ctx.elements_exposing(table_name, field_name)
    if elements:
        return "exposed", elements
    if _mentions_identifier(ctx, field_name):
        return "uncertain", []
    return "missing", []


def _business_keys(table: TableMeta) -> list[str]:
    return [f.name for f in table.business_key_fields]


def _needs_in_place_change(ctx: ValidationContext) -> bool:
    """Would making this view CDC-ready require editing the object itself?

    Deliberately narrow, and answered from the annotations alone rather than by
    consulting other rules — R-25 stays independently testable, which is the
    property that makes each rule verifiable against a fixture.

    The annotations are the right test because every in-place fix the tool
    offers is an annotation change: adding ``dataExtraction.enabled``, adding a
    CDC annotation, or correcting a mapping. Structural defects are not fixable
    in place at all, and exposing a missing key field is a projection change
    that also lands here as a mapping correction.
    """
    annotations = ctx.view.annotations
    if annotations is None:
        return True
    if not annotations.is_true(ANN_EXTRACTION_ENABLED):
        return True
    if not (annotations.is_true(ANN_CDC_AUTOMATIC) or ctx.mapping):
        return True
    # It declares both. Only a broken mapping would still force an edit.
    return any(entry.problems for entry in (ctx.mapping or []))


# ---------------------------------------------------------------------------
# R-01 / R-02 — the annotations themselves
# ---------------------------------------------------------------------------


@rule(
    "R-01",
    "@Analytics.dataExtraction.enabled: true is present",
    Severity.FIXABLE,
    "Annotation required for ODP exposure; without it the view does not appear "
    "in the CDS_EXTRACTION container (Appendix E.2)",
    rationale="A view that is not extraction-enabled is invisible to Datasphere.",
)
def r01_extraction_enabled(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    annotations = ctx.view.annotations
    if annotations is not None and annotations.is_true(ANN_EXTRACTION_ENABLED):
        yield satisfied(spec, "extraction is enabled")
        return

    present = annotations is not None and annotations.has(ANN_EXTRACTION_ENABLED)
    value = annotations.get(ANN_EXTRACTION_ENABLED) if annotations else None
    message = (
        f"@Analytics.dataExtraction.enabled is set to {value!r}, not true"
        if present
        else "@Analytics.dataExtraction.enabled is missing"
    )
    yield violated(
        spec,
        message,
        ref=annotations.ref(ANN_EXTRACTION_ENABLED) if annotations else _view_ref(ctx.view),
        node=ctx.view.name,
        remediation="Add @Analytics.dataExtraction.enabled: true to the view header.",
    )


@rule(
    "R-02",
    "A CDC annotation is present (automatic or mapping)",
    Severity.FIXABLE,
    "Appendix A.2 — the two annotation paths",
    rationale="Extraction alone gives a full load. Delta needs a CDC annotation.",
)
def r02_cdc_annotation(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    annotations = ctx.view.annotations
    automatic = annotations is not None and annotations.is_true(ANN_CDC_AUTOMATIC)
    mapping = ctx.mapping

    if automatic:
        yield satisfied(
            spec,
            "changeDataCapture.automatic: true — the framework derives the key "
            "mapping itself",
            ref=annotations.ref(ANN_CDC_AUTOMATIC),
        )
    elif mapping:
        yield satisfied(
            spec,
            f"changeDataCapture.mapping declares {len(mapping)} table(s)",
            ref=annotations.ref(ANN_CDC_MAPPING) if annotations else None,
        )
    else:
        empty = annotations is not None and annotations.has(ANN_CDC_MAPPING)
        yield violated(
            spec,
            "changeDataCapture.mapping is present but empty"
            if empty
            else "no changeDataCapture annotation — the view can be extracted "
            "but not delta-loaded",
            ref=annotations.ref(ANN_CDC_MAPPING) if annotations else _view_ref(ctx.view),
            node=ctx.view.name,
            remediation="Single-table views qualify for changeDataCapture.automatic: "
            "true. Multi-table views need an explicit mapping array.",
        )

    # A mapping the framework cannot read is not a CDC annotation. Surface any
    # structural damage here rather than letting the element rules report it as
    # a series of unrelated symptoms.
    for index, entry in enumerate(mapping or [], start=1):
        for problem in entry.problems:
            yield violated(
                spec,
                f"mapping entry {index} is malformed: {problem}",
                ref=entry.ref,
                node=entry.table or f"entry {index}",
                remediation="Fix the mapping annotation; the framework rejects "
                "it at activation with exception 151054.",
            )


# ---------------------------------------------------------------------------
# R-03 … R-09 — forbidden constructs, anywhere in the stack
# ---------------------------------------------------------------------------


@rule(
    "R-03",
    "No aggregate function anywhere in the stack",
    Severity.HARD,
    "Appendix A.5 — aggregation cannot be delta-reconstructed from base-row changes",
    rationale="A changed base row cannot be mapped back to an aggregated result row.",
)
def r03_no_aggregates(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    found = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        for function_name, ref in node.view.aggregates:
            found = True
            yield violated(
                spec,
                f"aggregate {function_name}() used{_where(ctx, node)}",
                ref=ref,
                node=node.name,
            )
    if not found:
        yield satisfied(spec, "no aggregate functions in the stack")


@rule(
    "R-04",
    "No GROUP BY",
    Severity.HARD,
    "Appendix A.5",
    rationale="Grouping collapses rows; delta cannot reconstruct which one changed.",
)
def r04_no_group_by(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    found = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        if node.view.group_by_ref is not None:
            found = True
            yield violated(
                spec,
                f"GROUP BY present{_where(ctx, node)}",
                ref=node.view.group_by_ref,
                node=node.name,
            )
    if not found:
        yield satisfied(spec, "no GROUP BY in the stack")


@rule(
    "R-05",
    "No HAVING",
    Severity.HARD,
    "Appendix A.5",
    rationale="HAVING implies aggregation.",
)
def r05_no_having(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    found = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        if node.view.having_ref is not None:
            found = True
            yield violated(
                spec,
                f"HAVING present{_where(ctx, node)}",
                ref=node.view.having_ref,
                node=node.name,
            )
    if not found:
        yield satisfied(spec, "no HAVING in the stack")


@rule(
    "R-06",
    "No UNION / UNION ALL",
    Severity.HARD,
    "Appendix A.5 — set operations are not supported",
    rationale="A base-row change cannot be attributed to a branch of a union.",
)
def r06_no_set_operations(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    found = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        for label, ref in node.view.set_operations:
            found = True
            yield violated(
                spec,
                f"{label} present{_where(ctx, node)}",
                ref=ref,
                node=node.name,
            )
    if not found:
        yield satisfied(spec, "no set operations in the stack")


@rule(
    "R-07",
    "DISTINCT cannot collapse distinct base rows",
    Severity.HARD,
    "Appendix A.5 — DISTINCT is unsupported where it can merge rows",
    rationale="DISTINCT breaks row identity only when it can actually merge two "
    "different base records. With the main table's whole key in the projection "
    "it cannot: rows differing in the key are never collapsed, and rows "
    "identical in every column including the key are ones CDC could not tell "
    "apart in the first place.",
)
def r07_no_distinct(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    """Asks what DISTINCT can do here, not whether the keyword is present.

    Measured against the 887 views S/4HANA ships with CDC delta declared: six
    use DISTINCT, three of them C1-released. Reading each one showed the flat
    ban was wrong for four and right for one:

      I_CostAnalysisResource   DISTINCT over CSKR, no joins, whole key
                               projected — it provably removes nothing
      VC_INTEGRATION_DD03L     whole key projected, one join; DISTINCT can only
      VC_INTEGRATION_DD04T     drop rows identical in every column, which delta
      VC_INTEGRATION_DD07T_CPS could never have distinguished anyway
      I_CustSalesAreaTax       TVKWZ's key is VKORG, VTWEG, WERKS and only
                               VKORG and VTWEG are exposed — DISTINCT merges
                               rows from different plants. A real defect.

    So the finding survives where it means something and disappears where it
    was noise. Where the main table cannot be determined the answer is
    INCONCLUSIVE, never a quiet pass.
    """
    found = False
    reported = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        if not node.view.has_distinct:
            continue
        found = True
        location = _where(ctx, node)
        table = ctx.main_table

        if table is None:
            reported = True
            yield inconclusive(
                spec,
                f"SELECT DISTINCT used{location} and the main table could not "
                f"be determined, so whether it can merge two base records is "
                f"undecided",
                ref=node.view.distinct_ref,
                node=node.name,
            )
            continue

        key = {f.name.upper() for f in table.business_key_fields}
        exposed = {
            field
            for (obj, field) in ctx.origin_index
            if obj == table.name.upper()
        }
        missing = sorted(key - exposed)

        if key and not missing:
            continue  # cannot merge anything the framework could tell apart

        reported = True
        yield violated(
            spec,
            f"SELECT DISTINCT used{location} while {table.name}'s key field(s) "
            f"{', '.join(missing) or '(unknown)'} are not exposed — it can "
            f"merge rows that belong to different base records",
            ref=node.view.distinct_ref,
            node=node.name,
            remediation=f"Expose {', '.join(missing) or 'the whole key'} in the "
            f"projection, or drop DISTINCT.",
        )

    if not found:
        yield satisfied(spec, "no DISTINCT in the stack")
    elif not reported:
        # Only when every DISTINCT in the stack was cleared. Yielding this
        # alongside a violation would have the rule both fail and pass.
        yield satisfied(spec, "DISTINCT cannot merge distinct base records")


@rule(
    "R-08",
    "No CDS view parameters",
    Severity.HARD,
    "Note 2890171; Replication Flows do not support input parameters",
    rationale="There is no way for a replication flow to supply a parameter value.",
)
def r08_no_parameters(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    found = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        for parameter in node.view.parameters:
            found = True
            yield violated(
                spec,
                f"view parameter {parameter.name!r} declared{_where(ctx, node)}",
                ref=parameter.ref,
                node=f"{node.name}.{parameter.name}",
            )
    if not found:
        yield satisfied(spec, "no view parameters in the stack")


@rule(
    "R-09",
    "No table function / AMDP in the stack",
    Severity.HARD,
    "KBA 2884410; Appendix D.3 — dependency resolution through a table function "
    "is the biggest metadata blind spot",
    rationale="An AMDP body is opaque: neither SAP's metadata nor this tool can "
    "see what it reads.",
)
def r09_no_table_functions(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    if ctx.view.entity_kind is EntityKind.TABLE_FUNCTION:
        yield violated(
            spec,
            "this object is a table function, not a view — CDC is not available",
            ref=_view_ref(ctx.view),
            node=ctx.view.name,
        )
        return

    functions = ctx.stack.table_functions
    if not functions:
        yield satisfied(spec, "no table functions in the stack")
        return
    for node in functions:
        parents = ", ".join(node.parents) or "this view"
        yield violated(
            spec,
            f"table function {node.name} is read by {parents}",
            node=node.name,
            remediation="A view stack containing a table function cannot be "
            "CDC-enabled. Rebuild the branch as plain CDS, or extract the "
            "underlying tables directly.",
        )


# ---------------------------------------------------------------------------
# R-10 / R-11 / R-12 — join and association shape
# ---------------------------------------------------------------------------


@rule(
    "R-10",
    "Every join is LEFT OUTER TO ONE",
    Severity.HARD,
    "Appendix A.3 — only the Left-Outer-to-One shape is supported by the CDC "
    "framework",
    rationale="A to-one join cannot multiply rows of the main table, so one "
    "main-table change maps to exactly one output record.",
)
def r10_join_cardinality(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    checked = 0
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        for join in node.view.joins:
            if join.join_type is not JoinType.LEFT_OUTER:
                continue  # R-11 owns disallowed join types
            checked += 1
            location = _where(ctx, node)
            if join.join_cardinality is JoinCardinality.TO_ONE:
                continue
            if join.join_cardinality is JoinCardinality.TO_MANY:
                yield violated(
                    spec,
                    f"LEFT OUTER TO MANY JOIN on {join.name}{location} — a to-many "
                    f"join multiplies main-table rows",
                    ref=join.ref,
                    node=join.local_name,
                    remediation="Restructure so the join is to-one, or extract "
                    "the tables separately and join in Datasphere.",
                )
            else:
                yield inconclusive(
                    spec,
                    f"LEFT OUTER JOIN on {join.name}{location} does not declare "
                    f"TO ONE — the CDC framework requires a to-one shape and the "
                    f"DDL neither declares nor denies it",
                    ref=join.ref,
                    node=join.local_name,
                    remediation="Declare LEFT OUTER TO ONE JOIN if the "
                    "relationship really is to-one, then prove it against real "
                    "data (F-14) — ABAP does not validate cardinality at runtime.",
                )
    if checked == 0:
        yield not_applicable(spec, "the stack contains no left outer joins")
    else:
        yield satisfied(spec, f"{checked} left outer join(s) examined")


def _tables_under(ctx: ValidationContext, name: str) -> set[str]:
    """Base tables reachable from this node of the stack.

    A join target is often a view, while a CDC mapping addresses base tables
    (Appendix A.5) — so "is this join covered by the mapping" cannot be
    answered by comparing names.
    """
    start = ctx.stack.node(name)
    if start is None:
        return set()
    if start.kind is NodeKind.TABLE:
        return {start.name.upper()}

    found: set[str] = set()
    seen: set[str] = set()
    queue = [start.name.upper()]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for node in ctx.stack.nodes.values():
            if current not in {p.upper() for p in node.parents}:
                continue
            if node.kind is NodeKind.TABLE:
                found.add(node.name.upper())
            else:
                queue.append(node.name.upper())
    return found


@rule(
    "R-11",
    "Inner and cross joins are covered by the CDC mapping",
    Severity.HARD,
    "Appendix A.3 — a change in a joined table must still produce a delta, "
    "which it only does when that table is mapped and carries its own trigger",
    rationale="Right outer and full outer joins can emit rows that do not "
    "correspond to any main-table row, which delta reconstruction cannot "
    "resolve. An inner join is different: it restricts rows rather than "
    "inventing them, and the CDC framework handles it by mapping the joined "
    "table so changes there raise their own delta.",
)
def r11_join_types(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    """Measured against SAP's own content rather than read off the wording.

    This rule used to fail any non-left-outer join outright, on the documented
    line that "only Left-Outer-to-One joins are supported". Checking the 887
    views that S/4HANA itself ships with CDC delta declared shows that reading
    is wrong: 38 of them — every one C1-released, across production, valuation,
    maintenance and supplier — use inner joins, 55 occurrences in all. An
    earlier 20-view sample had suggested this was a single outlier. It is not.

    Of those 57 inner and cross joins, 50 join a table that the view's own CDC
    mapping covers. The 7 that do not are customizing tables (TCMS_SEC_AST,
    CRMC_PROC_TYPE) and two targets whose base tables could not be resolved. So
    the constraint is about the mapping ``role``, not the SQL join keyword, and
    what actually matters is whether a change in the joined table can raise a
    delta at all.

    Hence: right and full outer joins stay a hard failure, and no SAP delta
    view uses one. Inner and cross joins pass when the mapping covers them and
    are referred for review when it does not — never a silent PASS, and no
    longer a false FAIL that hid good wrapper bases from F-09.
    """
    checked = 0
    automatic = bool(ctx.view.annotations.get(ANN_CDC_AUTOMATIC))
    mapped = {e.table.upper() for e in (ctx.mapping or []) if e.table}

    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        for join in node.view.joins:
            if join.join_type in (JoinType.LEFT_OUTER, None):
                continue
            checked += 1
            location = _where(ctx, node)

            if join.join_type in (JoinType.RIGHT_OUTER, JoinType.FULL_OUTER):
                yield violated(
                    spec,
                    f"{join.describe_join()} on {join.name}{location} — it can "
                    f"emit rows with no corresponding main-table row",
                    ref=join.ref,
                    node=join.local_name,
                    remediation="Rewrite as a LEFT OUTER TO ONE JOIN from the "
                    "main table, or extract the tables separately and join in "
                    "Datasphere.",
                )
                continue

            if automatic:
                continue  # the framework derives a mapping over every base table

            targets = {join.name.upper()} | _tables_under(ctx, join.name)
            if targets & mapped:
                continue

            if not mapped:
                yield inconclusive(
                    spec,
                    f"{join.describe_join()} on {join.name}{location} and the "
                    f"view declares no CDC mapping yet, so whether changes "
                    f"there raise a delta is undecided",
                    ref=join.ref,
                    node=join.local_name,
                    remediation=f"Include {join.name} in the mapping as "
                    f"#LEFT_OUTER_TO_ONE_JOIN when generating the wrapper, or "
                    f"use changeDataCapture.automatic and let the framework "
                    f"derive it.",
                )
            else:
                yield inconclusive(
                    spec,
                    f"{join.describe_join()} on {join.name}{location} is not "
                    f"covered by the CDC mapping — a change there would not "
                    f"raise a delta, though SAP ships this for customizing "
                    f"tables that never change in production",
                    ref=join.ref,
                    node=join.local_name,
                    remediation=f"Add {join.name} to the mapping, or confirm it "
                    f"is configuration whose changes need no delta.",
                )

    if checked == 0:
        yield satisfied(spec, "no inner, right outer, full outer or cross joins")
    else:
        yield satisfied(spec, f"{checked} non-left-outer join(s) examined")


@rule(
    "R-12",
    "No to-many association followed in the projection",
    Severity.HARD,
    "Appendix A.3 — to-many associations fan out like to-many joins",
    rationale="Declaring a to-many association is harmless, and so is exposing "
    "it as a navigation. Following it in a path expression is not: that pulls "
    "data through and multiplies rows exactly as a to-many join does.",
)
def r12_to_many_associations(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    found = False
    for node in _stack_view_nodes(ctx):
        assert node.view is not None
        view = node.view
        associations = view.association_map
        for used in sorted(view.used_association_names):
            association = associations.get(used)
            if association is None:
                continue
            card = association.cardinality
            if not card.is_specified:
                found = True
                yield inconclusive(
                    spec,
                    f"association {association.name} is used in the projection "
                    f"but its cardinality could not be read{_where(ctx, node)}",
                    ref=association.ref,
                    node=association.name,
                )
            elif card.is_to_many and association.is_filtered:
                # Declared to-many, but the ON condition also pins a status, a
                # type or a validity window. CDS cannot express "to-many, but
                # this filter makes it to-one", so SAP writes the pessimistic
                # cardinality and ships the view with CDC delta regardless —
                # I_DFS_EquipmentBasicDEX and I_DFS_EquipmentDEX, both
                # C1-released, do exactly that. The declaration is no longer
                # the last word, and only data can settle it.
                found = True
                yield inconclusive(
                    spec,
                    f"to-many association {association.name} {card} is followed "
                    f"in a path expression{_where(ctx, node)}, but its ON "
                    f"condition filters the target, so it may yield one row",
                    ref=association.ref,
                    node=association.name,
                    remediation="Prove the filter really restricts it to one "
                    "row before relying on delta — a wrong assumption here "
                    "produces duplicates that nobody notices for weeks.",
                )
            elif card.is_to_many:
                found = True
                yield violated(
                    spec,
                    f"to-many association {association.name} {card} is followed "
                    f"in a path expression{_where(ctx, node)}",
                    ref=association.ref,
                    node=association.name,
                    remediation="Remove the path expression, or restrict the "
                    "association to a to-one cardinality. Exposing the "
                    "association as an element is fine — only following it "
                    "multiplies rows.",
                )
    if not found:
        yield satisfied(spec, "no to-many association is followed in a projection")


# ---------------------------------------------------------------------------
# R-13 … R-15 — key and foreign-key exposure
# ---------------------------------------------------------------------------


def _report_key_exposure(
    ctx: ValidationContext,
    spec: RuleSpec,
    table: TableMeta,
    *,
    declared_table_elements: Iterable[str] | None,
    role_label: str,
) -> Iterator[RuleResult]:
    """Shared body for R-13 and R-14.

    Two paths. With an explicit mapping, the annotation itself declares which
    table key fields are exposed, so the check is against ``tableElement``.
    Without one (``automatic``), exposure has to be traced through the
    projection.
    """
    keys = _business_keys(table)
    if not keys:
        yield inconclusive(
            spec,
            f"{table.name} has no non-client key field in the metadata, so its "
            f"key exposure cannot be checked",
            node=table.name,
        )
        return

    if declared_table_elements is not None:
        declared = {name.upper() for name in declared_table_elements}
        missing = [k for k in keys if k.upper() not in declared]
        if missing:
            yield violated(
                spec,
                f"{role_label} table {table.name}: key field(s) "
                f"{', '.join(missing)} are not mapped",
                node=table.name,
                remediation=f"Add {', '.join(missing)} to tableElement, and the "
                f"view elements exposing them to viewElement.",
                detail={"table": table.name, "missing": missing},
            )
        else:
            yield satisfied(
                spec,
                f"{role_label} table {table.name}: all {len(keys)} key field(s) mapped",
                node=table.name,
            )
        return

    missing: list[str] = []
    uncertain: list[str] = []
    for key_field in keys:
        status, _ = _exposure(ctx, table.name, key_field)
        if status == "missing":
            missing.append(key_field)
        elif status == "uncertain":
            uncertain.append(key_field)

    if missing:
        yield violated(
            spec,
            f"{role_label} table {table.name}: key field(s) "
            f"{', '.join(missing)} are not exposed as view elements",
            node=table.name,
            remediation="Delta entries go into logging tables whose key fields "
            "are derived from the main table's key fields. The view activates "
            "without this and fails at delta time.",
            detail={"table": table.name, "missing": missing},
        )
    if uncertain:
        yield inconclusive(
            spec,
            f"{role_label} table {table.name}: key field(s) "
            f"{', '.join(uncertain)} appear only inside computed expressions, so "
            f"exposure could not be confirmed",
            node=table.name,
            detail={"table": table.name, "uncertain": uncertain},
        )
    if not missing and not uncertain:
        yield satisfied(
            spec,
            f"{role_label} table {table.name}: all {len(keys)} key field(s) exposed",
            node=table.name,
        )


@rule(
    "R-13",
    "All key fields of the main table are exposed as elements",
    Severity.FIXABLE,
    "Appendix A.4 — SAP: all key fields of the main table and all foreign key "
    "fields used by all on-conditions need to be exposed as elements",
    rationale="Delta entries go into logging tables whose key fields are derived "
    "from the main table's key fields. Key exposure is not cosmetic — it is what "
    "the logging table is built from.",
)
def r13_main_key_exposure(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    table = ctx.main_table
    if table is None:
        name = ctx.main_table_name
        yield inconclusive(
            spec,
            f"main table {name} is not in the metadata, so its key fields are unknown"
            if name
            else "the main table could not be determined — FROM does not resolve "
            "to a known table and there is no #MAIN mapping entry",
            node=name or ctx.view.name,
        )
        return

    declared = (
        ctx.main_entry.table_elements
        if ctx.main_entry is not None and ctx.main_entry.table_elements
        else None
    )
    yield from _report_key_exposure(
        ctx, spec, table, declared_table_elements=declared, role_label="#MAIN"
    )


@rule(
    "R-14",
    "All key fields of every mapped table are exposed",
    Severity.FIXABLE,
    "Appendix A.4 — logging table keys derive from these",
    rationale="Every mapped table contributes to the logging table key.",
)
def r14_mapped_key_exposure(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    mapping = ctx.mapping
    if not mapping:
        yield not_applicable(
            spec, "no explicit CDC mapping — the framework derives it (automatic)"
        )
        return

    checked = 0
    for entry in mapping:
        if entry.is_main or not entry.table:
            continue
        table = ctx.table_meta(entry.table)
        if table is None:
            yield inconclusive(
                spec,
                f"mapped table {entry.table} is not in the metadata",
                ref=entry.ref,
                node=entry.table,
            )
            continue
        checked += 1
        yield from _report_key_exposure(
            ctx,
            spec,
            table,
            declared_table_elements=entry.table_elements or None,
            role_label="mapped",
        )
    if checked == 0 and len(mapping) == 1:
        yield satisfied(spec, "the mapping declares only the main table")


@rule(
    "R-15",
    "Every ON-condition equality has at least one side exposed",
    Severity.FIXABLE,
    "Appendix A.4 — the framework needs the join key value in the output",
    rationale="CDC reconstructs which output row a base-table change belongs "
    "to from the join key. After the join both sides of an equality hold the "
    "same value in every output row, so exposing either one supplies it. "
    "Neither side exposed is the real defect: then the key is not in the "
    "output at all and no mapping can locate the row.",
)
def r15_on_condition_exposure(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    """Rewritten after measuring it against SAP's own delta content.

    This rule used to demand that *both* sides of every ON equality appear in
    the projection, and the rationale I wrote for it said, in as many words,
    that the values are equal but "the requirement is written about the
    fields". That sentence contained its own refutation.

    Of the 887 views S/4HANA ships with CDC delta declared, 123 have joins and
    the strict reading flagged **117 of them** — 109 C1-released. A rule that
    rejects 95% of the vendor's own shipped content is not finding defects, it
    is measuring the wrong thing. Nor was scope the problem: 58 of the flagged
    fields sat on a table the view's own mapping addresses, which is precisely
    the case the strict reading was meant for.

    The mechanism explains it. ``ON a.vbeln = b.vbeln`` guarantees a.vbeln ==
    b.vbeln in every output row, so one exposed side gives the framework the
    value; the second is redundant, and SAP consistently omits it. What CDC
    cannot survive is neither side being exposed — then the join key is absent
    from the output entirely.
    """
    joins = ctx.view.joins
    if not joins:
        yield not_applicable(spec, "the view has no joins")
        return

    missing: list[str] = []
    uncertain: list[str] = []
    checked = 0

    for join in joins:
        for left, right in join.on_equalities:
            sides: list[str] = []
            exposed = False
            unknown = False

            for ref in [*left, *right]:
                origin = ctx.resolve_ref(ref)
                if origin is None:
                    unknown = True
                    continue
                if is_client_field(origin.field_name):
                    exposed = True  # framework-handled, never a missing key
                    break
                sides.append(f"{origin.object_name}.{origin.field_name}")
                status, _ = _exposure(ctx, origin.object_name, origin.field_name)
                if status == "missing":
                    continue
                if status == "uncertain":
                    unknown = True
                else:
                    exposed = True
                    break

            if exposed:
                checked += 1
            elif unknown:
                uncertain.append(" = ".join(sides) or "<untraceable>")
            else:
                missing.append(" = ".join(sides))

    if missing:
        unique = sorted(set(missing))
        yield violated(
            spec,
            f"neither side of the join condition(s) {'; '.join(unique)} is "
            f"exposed, so the join key is not in the output at all",
            node=ctx.view.name,
            remediation="Expose one side of each equality. Either will do — "
            "they hold the same value in every joined row.",
            detail={"missing": unique},
        )
    if uncertain:
        unique = sorted(set(uncertain))
        yield inconclusive(
            spec,
            f"could not confirm either side of the join condition(s) "
            f"{'; '.join(unique)} is exposed",
            node=ctx.view.name,
            detail={"uncertain": unique},
        )
    if not missing and not uncertain:
        yield satisfied(
            spec, f"all {checked} join condition(s) expose their key"
        )


# ---------------------------------------------------------------------------
# R-16 … R-20 — mapping integrity
# ---------------------------------------------------------------------------


@rule(
    "R-16",
    "Exactly one #MAIN in the mapping",
    Severity.FIXABLE,
    "Appendix A.2 — #MAIN is the root table; its key must equal the view key",
    rationale="The main table defines row identity and is the only table whose "
    "deletions propagate.",
)
def r16_single_main(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    mapping = ctx.mapping
    if not mapping:
        yield not_applicable(spec, "no explicit CDC mapping")
        return

    mains = [e for e in mapping if e.is_main]
    if len(mains) == 1:
        yield satisfied(spec, f"#MAIN is {mains[0].table}", node=mains[0].table)
    elif not mains:
        yield violated(
            spec,
            "the mapping declares no #MAIN table",
            ref=mapping[0].ref,
            node=ctx.view.name,
            remediation="Exactly one entry must carry role: #MAIN — the table the "
            "view's key comes from.",
        )
    else:
        names = ", ".join(e.table for e in mains)
        yield violated(
            spec,
            f"the mapping declares {len(mains)} #MAIN tables ({names})",
            ref=mains[1].ref,
            node=names,
            remediation="Keep one #MAIN; the others become "
            "#LEFT_OUTER_TO_ONE_JOIN.",
        )


@rule(
    "R-17",
    "The view key corresponds to the #MAIN table key",
    Severity.FIXABLE,
    "RSODP_ABAP_CDS 201, KBA 3008492 — 'No representative key element found in "
    "CDS view'",
    rationale="Row identity in the target is the view key; it has to be the main "
    "table's identity too.",
)
def r17_view_key_matches_main(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    key_items = [i for i in ctx.view.key_items if i.name]
    if not key_items:
        yield violated(
            spec,
            "the view declares no key elements",
            ref=_view_ref(ctx.view),
            node=ctx.view.name,
            remediation="Mark the elements exposing the main table's key with KEY.",
        )
        return

    key_names = {i.name.upper() for i in key_items}

    entry = ctx.main_entry
    if entry is not None and entry.view_elements:
        declared = {name.upper() for name in entry.view_elements}
        if declared == key_names:
            yield satisfied(
                spec,
                f"the view key ({', '.join(sorted(key_names))}) matches the "
                f"#MAIN mapping",
                node=entry.table,
            )
            return
        missing = sorted(declared - key_names)
        extra = sorted(key_names - declared)
        parts = []
        if missing:
            parts.append(f"mapped as #MAIN key but not marked KEY: {', '.join(missing)}")
        if extra:
            parts.append(f"marked KEY but not part of the #MAIN key: {', '.join(extra)}")
        yield violated(
            spec,
            "the view key does not correspond to the #MAIN table key — " + "; ".join(parts),
            ref=entry.ref,
            node=entry.table,
            remediation="Align the KEY elements with the #MAIN viewElement list.",
            detail={"missing": missing, "extra": extra},
        )
        return

    table = ctx.main_table
    if table is None:
        yield inconclusive(
            spec,
            "the main table is unknown, so the view key cannot be compared to it",
            node=ctx.view.name,
        )
        return

    unmarked: list[str] = []
    uncertain: list[str] = []
    for key_field in _business_keys(table):
        elements = ctx.elements_exposing(table.name, key_field)
        if not elements:
            status, _ = _exposure(ctx, table.name, key_field)
            (uncertain if status == "uncertain" else unmarked).append(key_field)
        elif not any(name.upper() in key_names for name in elements):
            unmarked.append(key_field)

    if unmarked:
        yield violated(
            spec,
            f"main table key field(s) {', '.join(unmarked)} are not marked KEY in "
            f"the view",
            node=table.name,
            remediation="Every main-table key field must be a KEY element of the view.",
            detail={"table": table.name, "unmarked": unmarked},
        )
    elif uncertain:
        yield inconclusive(
            spec,
            f"could not confirm that key field(s) {', '.join(uncertain)} are "
            f"marked KEY",
            node=table.name,
        )
    else:
        yield satisfied(
            spec, f"the view key corresponds to the key of {table.name}", node=table.name
        )


@rule(
    "R-18",
    "Every tableElement exists and is a key field of that table",
    Severity.FIXABLE,
    "Exception 151054 — 'Error reading CDC annotations: mapping field does not "
    "exist' / 'CDC begin marker failed'",
    rationale="The classic incomplete-mapping failure, and exactly what a "
    "pre-activation validator exists to catch.",
)
def r18_table_elements_valid(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    mapping = ctx.mapping
    if not mapping:
        yield not_applicable(spec, "no explicit CDC mapping")
        return

    checked = 0
    problems = 0
    for entry in mapping:
        if not entry.table or not entry.table_elements:
            continue
        table = ctx.table_meta(entry.table)
        if table is None:
            problems += 1
            yield inconclusive(
                spec,
                f"table {entry.table} is not in the metadata, so its tableElement "
                f"list cannot be verified",
                ref=entry.ref,
                node=entry.table,
            )
            continue
        for element in entry.table_elements:
            checked += 1
            field_meta = table.field_by_name(element)
            if field_meta is None:
                problems += 1
                yield violated(
                    spec,
                    f"tableElement {element!r} does not exist in {table.name}",
                    ref=entry.ref,
                    node=f"{table.name}.{element}",
                    remediation="Activation fails with exception 151054.",
                )
            elif not field_meta.is_key:
                problems += 1
                yield violated(
                    spec,
                    f"tableElement {element!r} exists in {table.name} but is not a "
                    f"key field",
                    ref=entry.ref,
                    node=f"{table.name}.{element}",
                    remediation="Only key fields belong in tableElement — the "
                    "logging table key is built from them.",
                )
    if problems == 0:
        yield satisfied(spec, f"all {checked} tableElement reference(s) are valid keys")


@rule(
    "R-19",
    "Every viewElement exists in the view",
    Severity.FIXABLE,
    "Exception 151054",
    rationale="A mapping that names an element the view does not expose fails at "
    "activation.",
)
def r19_view_elements_exist(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    mapping = ctx.mapping
    if not mapping:
        yield not_applicable(spec, "no explicit CDC mapping")
        return

    missing: list[str] = []
    checked = 0
    for entry in mapping:
        for element in entry.view_elements:
            checked += 1
            if not ctx.has_element(element):
                missing.append(element)
                yield violated(
                    spec,
                    f"viewElement {element!r} (mapped for {entry.table or 'a table'}) "
                    f"is not an element of this view",
                    ref=entry.ref,
                    node=element,
                    remediation="Add the element to the projection, or correct the "
                    "mapping. Activation fails with exception 151054.",
                )
    if not missing:
        yield satisfied(spec, f"all {checked} viewElement reference(s) exist")


@rule(
    "R-20",
    "Every mapped table is a real transparent table",
    Severity.FIXABLE,
    "Appendix A.9 — only tables that should actually trigger a delta belong in "
    "the mapping",
    rationale="The mapping addresses database tables, not views. Mapping an "
    "intermediate view is a common and silent mistake.",
)
def r20_mapped_tables_real(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    mapping = ctx.mapping
    if not mapping:
        yield not_applicable(spec, "no explicit CDC mapping")
        return

    problems = 0
    for entry in mapping:
        if not entry.table:
            continue
        table = ctx.table_meta(entry.table)
        if table is None:
            problems += 1
            source = ctx.metadata.get_view_source(entry.table)
            if source is not None:
                yield violated(
                    spec,
                    f"{entry.table} is mapped as a CDC table but it is a CDS view",
                    ref=entry.ref,
                    node=entry.table,
                    remediation="For stacked views, map the underlying database "
                    "tables, not the intermediate views (Appendix A.5).",
                )
            else:
                yield inconclusive(
                    spec,
                    f"mapped table {entry.table} is not in the metadata",
                    ref=entry.ref,
                    node=entry.table,
                )
            continue
        if table.table_class is not TableClass.TRANSPARENT:
            problems += 1
            yield violated(
                spec,
                f"{table.name} is a {table.table_class.value} table, not a "
                f"transparent table",
                ref=entry.ref,
                node=table.name,
            )
    if problems == 0:
        yield satisfied(spec, "every mapped table is a transparent table")


# ---------------------------------------------------------------------------
# R-21 … R-23 — base table properties
# ---------------------------------------------------------------------------


@rule(
    "R-21",
    "Base tables are transparent, not cluster or pool",
    Severity.HARD,
    "Appendix A.9 — cluster and pool tables are unsuitable",
    rationale="Database triggers need a real database table underneath.",
)
def r21_transparent_base_tables(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    leaves = ctx.stack.leaf_tables
    if not leaves:
        yield inconclusive(
            spec, "no base table could be resolved from the stack", node=ctx.view.name
        )
        return

    problems = 0
    for node in leaves:
        table = node.table
        if table is None or table.table_class is TableClass.UNKNOWN:
            problems += 1
            yield inconclusive(
                spec,
                f"table class of {node.name} is unknown",
                node=node.name,
            )
        elif table.table_class is not TableClass.TRANSPARENT:
            problems += 1
            yield violated(
                spec,
                f"{table.name} is a {table.table_class.value} table",
                node=table.name,
                remediation="Cluster and pool tables cannot carry CDC triggers.",
            )
    if problems == 0:
        yield satisfied(spec, f"all {len(leaves)} base table(s) are transparent")


@rule(
    "R-22",
    "Base tables have a genuine primary key",
    Severity.HARD,
    "Datasphere Replication Flow prerequisite (Appendix A.9)",
    rationale="Replication needs a stable row identity in the source.",
)
def r22_primary_key(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    leaves = ctx.stack.leaf_tables
    if not leaves:
        yield inconclusive(
            spec, "no base table could be resolved from the stack", node=ctx.view.name
        )
        return

    problems = 0
    for node in leaves:
        table = node.table
        if table is None or not table.fields:
            problems += 1
            yield inconclusive(
                spec, f"field metadata for {node.name} is unavailable", node=node.name
            )
        elif not table.has_primary_key:
            problems += 1
            yield violated(
                spec,
                f"{table.name} has no primary key beyond the client field",
                node=table.name,
                remediation="A Replication Flow requires tables that have a "
                "primary key.",
            )
    if problems == 0:
        yield satisfied(spec, f"all {len(leaves)} base table(s) have a primary key")


@rule(
    "R-23",
    "The client field is handled as a client, not exposed as an ordinary key",
    Severity.FIXABLE,
    "Note 2890171 — tables with a client field as key are problematic",
    rationale="The framework handles client separately; exposing MANDT as a "
    "normal key element confuses row identity.",
)
def r23_client_handling(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    offenders: list[str] = []
    for item in ctx.view.key_items:
        origin = ctx.origin_of(item)
        name = item.name
        if origin is not None and is_client_field(origin.field_name):
            offenders.append(name or origin.field_name)
        elif origin is None and name and is_client_field(name):
            offenders.append(name)

    if offenders:
        for name in offenders:
            yield violated(
                spec,
                f"element {name!r} exposes the client field as a KEY element",
                node=name,
                remediation="Remove the client from the projection. The framework "
                "supplies it; a CDS view is client-dependent by default.",
            )
        return

    mapped_client = [
        f"{entry.table}.{element}"
        for entry in (ctx.mapping or [])
        for element in entry.table_elements
        if is_client_field(element)
    ]
    if mapped_client:
        yield violated(
            spec,
            f"client field mapped as a CDC key: {', '.join(mapped_client)}",
            node=mapped_client[0],
            remediation="Drop the client field from tableElement.",
        )
        return

    yield satisfied(spec, "the client field is not exposed as an ordinary key")


# ---------------------------------------------------------------------------
# R-24 / R-25 — delta method and modifiability
# ---------------------------------------------------------------------------


@rule(
    "R-24",
    "The delta method is CDC, not timestamp-based",
    Severity.FIXABLE,
    "KBA 3514600 — a view carrying only delta.byElement will not offer the delta "
    "option; Replication Flows support CDC delta only",
    rationale="Generic timestamp/date-based delta predates CDC and is not "
    "supported by Replication Flows.",
)
def r24_delta_method(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    annotations = ctx.view.annotations
    has_by_element = annotations is not None and annotations.has(ANN_DELTA_BY_ELEMENT)
    has_cdc = (annotations is not None and annotations.is_true(ANN_CDC_AUTOMATIC)) or bool(
        ctx.mapping
    )

    if has_by_element and not has_cdc:
        yield violated(
            spec,
            "the view declares delta.byElement (timestamp-based delta) and no CDC "
            "annotation — Replication Flows will not offer the delta option",
            ref=annotations.ref(ANN_DELTA_BY_ELEMENT) if annotations else None,
            node=ctx.view.name,
            remediation="Convert to changeDataCapture. The tool can generate the "
            "replacement annotation.",
        )
    elif has_by_element and has_cdc:
        yield satisfied(
            spec,
            "CDC delta is declared; the additional delta.byElement annotation is "
            "redundant for Replication Flows",
            ref=annotations.ref(ANN_DELTA_BY_ELEMENT) if annotations else None,
        )
    elif has_cdc:
        yield satisfied(spec, "CDC delta is declared")
    else:
        yield not_applicable(
            spec, "no delta annotation at all — reported by R-02"
        )


@rule(
    "R-25",
    "The object is modifiable (not a C1/C2-released SAP view)",
    Severity.FIXABLE,
    "Appendix D.5 — release state via the APIS transport object "
    "(R3TR APIS <name> DDLS)",
    rationale="Modifiability says nothing about whether CDC works — it decides "
    "which fix to offer. A released SAP view must never be modified, so its "
    "route is a Z-wrapper rather than an in-place annotation. Where state "
    "cannot be determined, assume unmodifiable — fail safe.",
)
def r25_modifiable(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    # Only relevant if something would have to change. A released SAP view that
    # is already extraction- and CDC-enabled needs no edit, so whether it may be
    # edited is beside the point — and saying "must not be modified" about a
    # view nobody was going to modify is noise that buries the real findings.
    if not _needs_in_place_change(ctx):
        yield not_applicable(
            spec,
            "the view already declares extraction and CDC, so no in-place "
            "change is required and its release state does not matter",
        )
        return

    meta = ctx.object_meta or ctx.metadata.get_object(ctx.view.name)
    if meta is None:
        yield inconclusive(
            spec,
            "ownership and release state are unknown — assumed unmodifiable, so a "
            "Z-wrapper is the safe route",
            node=ctx.view.name,
            remediation="Confirm the API state before modifying in place.",
        )
        return

    if meta.is_modifiable:
        yield satisfied(spec, meta.modifiability_reason, node=meta.name)
        return

    yield violated(
        spec,
        f"{meta.name} must not be modified — {meta.modifiability_reason}",
        node=meta.name,
        remediation="Generate a Z-wrapper (F-20) instead of annotating in place. "
        "Run the full rule set against the base view first — a wrapper inherits "
        "the base's structure and cannot fix an aggregation, union or to-many join.",
        detail={"api_state": meta.api_state.value, "owner": meta.owner.value},
    )


# ---------------------------------------------------------------------------
# R-26 … R-30 — the things static analysis cannot settle
# ---------------------------------------------------------------------------


@rule(
    "R-26",
    "Declared to-one cardinality matches the actual data",
    Severity.MANUAL_REVIEW,
    "Appendix F.2 — ABAP does not validate cardinality at runtime; a mismatch is "
    "only a syntax-check warning and the runtime result is documented as undefined",
    tier="V1",
    rationale="The single most expensive silent failure in the CDC domain: the "
    "view activates cleanly, the initial load looks right, and delta produces "
    "duplicates or gaps weeks later.",
)
def r26_cardinality_evidence(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    to_one_joins = [
        join
        for join in ctx.view.joins
        if join.join_type is JoinType.LEFT_OUTER
        and join.join_cardinality is JoinCardinality.TO_ONE
    ]
    if not to_one_joins:
        yield not_applicable(spec, "the view declares no to-one joins")
        return

    for join in to_one_joins:
        evidence = ctx.cardinality_evidence.get(join.local_name) or ctx.cardinality_evidence.get(
            join.name.upper()
        )
        if evidence is None or evidence.result is CardinalityResult.DECLARED_ONLY:
            message = (
                f"{join.name} declares TO ONE but the cardinality has not been "
                f"proven against real data"
            )
            remediation = (
                "Run the cardinality prover (F-14), or accept the risk knowingly. "
                "A wrong to-one declaration produces duplicate or missing delta "
                "records that nobody catches for weeks."
            )
            if ctx.config.require_cardinality_evidence:
                yield inconclusive(
                    spec, message, ref=join.ref, node=join.local_name,
                    remediation=remediation,
                )
            else:
                # The risk has been accepted by configuration. The finding is
                # still stated — it is downgraded, not hidden.
                yield satisfied(
                    spec,
                    message + " (evidence not required by configuration)",
                    ref=join.ref,
                    node=join.local_name,
                    remediation=remediation,
                )
        elif evidence.result is CardinalityResult.VIOLATED:
            # Proven wrong against real data. That is no longer a matter for
            # review — it is a defect, and it is reported at HARD severity even
            # though the rule's declared severity is MANUAL_REVIEW.
            yield violated(
                spec,
                f"{join.name} declares TO ONE but the data disproves it — "
                f"{evidence.max_count} rows found for key {evidence.sample_key!r}",
                ref=join.ref,
                node=join.local_name,
                severity=Severity.HARD,
                remediation="Fix the join or the model. Delta on this view will "
                "produce duplicates.",
                detail={
                    "table": evidence.table,
                    "max_count": evidence.max_count,
                    "sample_key": evidence.sample_key,
                },
            )
        else:
            yield satisfied(
                spec,
                f"{join.name} to-one cardinality proven against real data",
                ref=join.ref,
                node=join.local_name,
            )


@rule(
    "R-27",
    "The dependency tree is fully resolved",
    Severity.MANUAL_REVIEW,
    "Appendix D.3 — the metadata objects do not reliably represent table "
    "functions, generated providers, or calculated columns",
    rationale="An unresolved branch means the constructs inside it were never "
    "checked, so no rule above it can be trusted.",
)
def r27_stack_resolved(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    stack = ctx.stack
    clean = True

    for node in stack.unresolved:
        clean = False
        read_by = (
            f"; it is read by {', '.join(node.parents)}" if node.parents else ""
        )
        yield inconclusive(
            spec,
            f"{node.name} could not be resolved ({node.reason}){read_by}",
            node=node.name,
        )

    for parent, child in stack.cycles:
        clean = False
        yield inconclusive(
            spec,
            f"circular dependency: {parent or ctx.view.name} → {child}",
            node=child,
        )

    if stack.depth_cap_hit:
        clean = False
        yield inconclusive(
            spec,
            f"the dependency walk hit the depth cap of {ctx.config.max_stack_depth}",
            node=ctx.view.name,
        )

    if stack.node_budget_hit:
        clean = False
        yield inconclusive(
            spec,
            f"the dependency walk stopped after {ctx.config.max_stack_nodes} "
            f"objects — this view's stack is larger than the budget, so part of "
            f"it was never examined and the forbidden-construct rules "
            f"(R-03…R-09) could not see all of it",
            node=ctx.view.name,
            remediation="Raise RuleConfig.max_stack_nodes if you need the whole "
            "tree and can afford the time; against a live system every "
            "unvisited object is a round-trip.",
            detail={"budget": ctx.config.max_stack_nodes, "visited": len(stack.nodes)},
        )

    if clean:
        yield satisfied(spec, f"dependency tree fully resolved — {stack.describe()}")


@rule(
    "R-28",
    "No active subscription blocks re-activation",
    Severity.BLOCKING,
    "SODQ666 / E:RSODP_ABAP_CDS:341 — 'Change data capture annotations changed "
    "incompatibly' (Appendix B.6)",
    rationale="Re-activating a CDS view that already has a live CDC subscription "
    "breaks it and forces a full delta re-initialisation — hours of reload and a "
    "gap in the target.",
)
def r28_no_active_subscription(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    state = ctx.subscription_state
    if state is SubscriptionState.NONE:
        yield satisfied(spec, "no active CDC subscription on this view")
    elif state is SubscriptionState.ACTIVE:
        yield violated(
            spec,
            "this view has an ACTIVE CDC subscription — modifying it will break "
            "the subscription and force a full delta re-initialisation",
            node=ctx.view.name,
            remediation="Stop Logging for the affected views/tables in DHCDCMON, "
            "then re-initialise the subscription. On a large table this means "
            "hours of reload and a gap in the target. Acknowledge explicitly "
            "before proceeding.",
        )
    else:
        yield inconclusive(
            spec,
            "subscription state is unknown (no system connection) — writes stay "
            "blocked",
            node=ctx.view.name,
            remediation="Check DHCDCMON registered objects / ODQ before any write.",
        )


@rule(
    "R-29",
    "View stack depth is within limits",
    Severity.MANUAL_REVIEW,
    "KBA 3467820 — the framework may reject a view as 'too complex for automatic "
    "CDC delta'",
    rationale="No published threshold exists; deep stacks are the usual trigger.",
)
def r29_stack_depth(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    depth = ctx.stack.max_depth_reached
    limit = ctx.config.manual_review_stack_depth
    if depth <= limit:
        yield satisfied(spec, f"stack depth {depth} (review threshold {limit})")
        return
    yield violated(
        spec,
        f"stack depth is {depth}. SAP publishes no maximum, but the framework "
        f"rejects views it considers too complex, and deep stacks are the usual "
        f"cause. This threshold ({limit}) is the tool's own conservatism, not an "
        f"SAP number.",
        node=ctx.view.name,
        remediation="Delegate to SAP's own check (F-15, /sap/bc/adt/checkruns) "
        "before relying on this view.",
        detail={"depth": depth, "threshold": limit},
    )


@rule(
    "R-30",
    "Estimated row count is within practical limits",
    Severity.WARNING,
    "Appendix E.4 — CDS views beyond ~2bn records hit an internal HANA "
    "memory/row limit",
    rationale="Above the limit the extraction fails; the workaround is splitting "
    "the data across custom views.",
)
def r30_row_count(ctx: ValidationContext, spec: RuleSpec) -> Iterator[RuleResult]:
    threshold = ctx.config.row_count_review_threshold
    flagged = False
    known = 0
    for node in ctx.stack.leaf_tables:
        table = node.table
        if table is None or table.estimated_rows is None:
            continue
        known += 1
        if table.estimated_rows > threshold:
            flagged = True
            yield violated(
                spec,
                f"{table.name} holds an estimated {table.estimated_rows:,} rows, "
                f"above the ~{threshold:,} row ceiling",
                node=table.name,
                remediation="Split the data across custom views and replicate "
                "into the same target, or union the targets.",
                detail={"table": table.name, "rows": table.estimated_rows},
            )
    if not flagged:
        if known == 0:
            yield not_applicable(spec, "no row-count estimates available")
        else:
            yield satisfied(spec, f"{known} table(s) within the row-count ceiling")
