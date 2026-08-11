"""F-21 — the CDC mapping builder, and F-22 — mandatory field enforcement.

The design point that matters (Appendix A.7): **the join list and the mapping
list are independent.** A view can join four tables and declare CDC on one.
That is valid, it works, and it silently ignores changes in the other three —
SAP does it deliberately, omitting T001/TVKO from ``C_SalesDocumentItemDEX_1``
so a company-code change does not regenerate every sales delta (Note 3070845).

So this module does not "map everything". It produces a *proposal* with a
stated reason per table, which the UI turns into a per-table question: should
changes here trigger a delta? Map-everything is wrong as often as it is right.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.cds import (
    CdcMappingEntry,
    ROLE_LEFT_OUTER_TO_ONE_JOIN,
    ROLE_MAIN,
    is_client_field,
)
from cdcforge.generator.emit import render_cdc_mapping
from cdcforge.metadata.types import TableMeta
from cdcforge.parsing.nodes import DataSource, JoinCardinality, JoinType
from cdcforge.rules.context import ValidationContext
from cdcforge.rules.stack import NodeKind


@dataclass
class TableDecision:
    """Whether one table's changes should trigger a delta, and why."""

    table: str
    alias: str
    is_main: bool
    included: bool
    reason: str
    is_configuration: bool = False
    is_hot: bool = False
    key_fields: list[str] = field(default_factory=list)
    view_elements: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    """Key fields with no view element exposing them — these block the mapping."""

    @property
    def complete(self) -> bool:
        return not self.missing_keys


@dataclass
class MappingProposal:
    entries: list[CdcMappingEntry] = field(default_factory=list)
    decisions: list[TableDecision] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    mandatory_elements: list[str] = field(default_factory=list)
    """F-22 — elements the user may not deselect: every key field of every
    mapped table, and every ON-condition foreign key."""

    @property
    def ok(self) -> bool:
        return bool(self.entries) and not self.problems

    def render(self, indent: str = "        ") -> str:
        return render_cdc_mapping(self.entries, indent=indent)

    def decision_for(self, table: str) -> TableDecision | None:
        target = table.upper()
        return next((d for d in self.decisions if d.table == target), None)


def _resolve_table(ctx: ValidationContext, source: DataSource) -> TableMeta | None:
    node = ctx.stack.node(source.name)
    if node is not None and node.kind is NodeKind.TABLE and node.table is not None:
        return node.table
    return ctx.metadata.get_table(source.name)


def _default_inclusion(table: TableMeta) -> tuple[bool, str]:
    """The smart default: map transactional tables, omit config tables."""
    if table.is_configuration:
        return False, (
            f"{table.name} looks like configuration/customising content"
            + (f" (delivery class {table.delivery_class})" if table.delivery_class else "")
            + " — a change there would regenerate deltas for every dependent record"
        )
    if table.is_hot:
        return True, (
            f"{table.name} is a high-frequency transactional table — mapped, but "
            f"it carries the highest trigger load"
        )
    return True, "transactional table — changes should trigger a delta"


def build_cdc_mapping(
    ctx: ValidationContext,
    *,
    include: set[str] | None = None,
) -> MappingProposal:
    """Propose a CDC mapping for a view.

    ``include`` overrides the smart default with an explicit set of table names
    the user chose in the UI.
    """
    proposal = MappingProposal()
    view = ctx.view

    if view.from_source is None:
        proposal.problems.append("the view has no FROM clause")
        return proposal

    main_table = _resolve_table(ctx, view.from_source)
    if main_table is None:
        node = ctx.stack.node(view.from_source.name)
        if node is not None and node.kind is NodeKind.VIEW:
            # A view over a view — which is nearly every SAP view. The mapping
            # still has to address base tables, so trace the elements down to
            # the table this view is ultimately rooted on.
            return _build_over_stack(ctx, proposal)
        proposal.problems.append(
            f"FROM {view.from_source.name} could not be resolved to a table "
            f"in the metadata"
        )
        return proposal

    entries: list[CdcMappingEntry] = []
    mandatory: list[str] = []

    def consider(source: DataSource, table: TableMeta, is_main: bool) -> None:
        keys = [f.name for f in table.business_key_fields]
        view_elements: list[str] = []
        table_elements: list[str] = []
        missing: list[str] = []

        for key_field in keys:
            exposing = ctx.elements_exposing(source.name, key_field)
            if exposing:
                view_elements.append(exposing[0])
                table_elements.append(key_field)
            else:
                missing.append(key_field)

        if is_main:
            included, reason = True, "the FROM table is always the #MAIN table"
        elif include is not None:
            included = table.name in {t.upper() for t in include}
            reason = (
                "selected by the user"
                if included
                else "deselected by the user — changes here will not trigger a delta"
            )
        else:
            included, reason = _default_inclusion(table)

        decision = TableDecision(
            table=table.name,
            alias=source.local_name,
            is_main=is_main,
            included=included,
            reason=reason,
            is_configuration=table.is_configuration,
            is_hot=table.is_hot,
            key_fields=keys,
            view_elements=view_elements,
            missing_keys=missing,
        )
        proposal.decisions.append(decision)

        if not included:
            return

        if missing:
            proposal.problems.append(
                f"{table.name}: key field(s) {', '.join(missing)} are not exposed "
                f"as view elements, so the mapping would be incomplete. Expose "
                f"them first — the view activates without them and fails at "
                f"delta time."
            )
            return

        entries.append(
            CdcMappingEntry(
                table=table.name,
                role=ROLE_MAIN if is_main else ROLE_LEFT_OUTER_TO_ONE_JOIN,
                view_elements=view_elements,
                table_elements=table_elements,
            )
        )
        mandatory.extend(view_elements)

    consider(view.from_source, main_table, is_main=True)

    for join in view.joins:
        table = _resolve_table(ctx, join)
        if table is None:
            proposal.problems.append(
                f"joined object {join.name} could not be resolved to a table"
            )
            continue
        if join.join_type in (JoinType.RIGHT_OUTER, JoinType.FULL_OUTER):
            # These can emit rows with no corresponding main-table row, and no
            # mapping entry can rescue that. No view SAP ships with CDC delta
            # declared uses one.
            proposal.problems.append(
                f"{join.describe_join()} on {join.name} can emit rows with no "
                f"corresponding row of the main table, which no mapping can fix"
            )
            continue
        # An inner or cross join is mapped, not refused. This code used to
        # reject it as "not supported by the CDC framework — only LEFT OUTER TO
        # ONE JOIN is", which R-11 abandoned today after 38 C1-released SAP
        # views were found doing exactly that. Leaving it here made the tool
        # contradict itself: the rule engine called I_ProductValuation
        # reviewable while the generator refused to build a mapping for it —
        # and refusing was self-defeating, because *mapping the joined table*
        # is precisely what answers R-11's objection.
        if join.join_cardinality is JoinCardinality.TO_MANY:
            proposal.problems.append(
                f"LEFT OUTER TO MANY JOIN on {join.name} multiplies main-table rows"
            )
            continue
        consider(join, table, is_main=False)

    # F-22 — ON-condition foreign keys are mandatory regardless of the user's
    # field selection, and regardless of whether their table is mapped.
    for join in view.joins:
        for ref in join.on_refs:
            origin = ctx.resolve_ref(ref)
            if origin is None or is_client_field(origin.field_name):
                continue
            mandatory.extend(ctx.elements_exposing(origin.object_name, origin.field_name))

    proposal.entries = entries
    seen: set[str] = set()
    proposal.mandatory_elements = [
        e for e in mandatory if not (e.upper() in seen or seen.add(e.upper()))
    ]
    return proposal


def _build_over_stack(
    ctx: ValidationContext, proposal: MappingProposal
) -> MappingProposal:
    """Map the base tables of a view that sits on other views.

    The root table is whichever leaf the FROM chain terminates on — that is
    what decides row identity, so it is the #MAIN. Other leaf tables reachable
    through the stack are offered as joined entries only when this view
    actually exposes their whole key; a partial key cannot be mapped, and
    guessing would produce a mapping that activates and then fails at delta
    time.
    """
    from cdcforge.lineage import exposed_key_elements

    view = ctx.view
    root_table = _root_table(ctx)
    if root_table is None:
        proposal.problems.append(
            f"could not follow {view.from_source.name} down to a base table, so "
            f"there is no #MAIN to map"
        )
        return proposal

    entries: list[CdcMappingEntry] = []
    mandatory: list[str] = []

    leaf_tables = [n.table for n in ctx.stack.leaf_tables if n.table is not None]
    ordered = [t for t in leaf_tables if t.name == root_table.name]
    ordered += [t for t in leaf_tables if t.name != root_table.name]

    for table in ordered:
        is_main = table.name == root_table.name
        exposed = exposed_key_elements(ctx.metadata, view, table)

        if not exposed:
            if is_main:
                keys = ", ".join(f.name for f in table.business_key_fields)
                proposal.problems.append(
                    f"this view does not expose the whole key of {table.name} "
                    f"({keys}), so no #MAIN mapping can be written. Expose those "
                    f"fields — the view activates without them and fails at "
                    f"delta time."
                )
                return proposal
            proposal.decisions.append(
                TableDecision(
                    table=table.name, alias=table.name, is_main=False,
                    included=False,
                    reason="its key is not exposed by this view, so its changes "
                           "cannot be mapped",
                    missing_keys=[f.name for f in table.business_key_fields],
                )
            )
            continue

        included, reason = (True, "the root table — always #MAIN") if is_main \
            else _default_inclusion(table)
        proposal.decisions.append(
            TableDecision(
                table=table.name, alias=table.name, is_main=is_main,
                included=included, reason=reason,
                is_configuration=table.is_configuration, is_hot=table.is_hot,
                key_fields=[f.name for f in table.business_key_fields],
                view_elements=list(exposed),
            )
        )
        if not included:
            continue

        entries.append(
            CdcMappingEntry(
                table=table.name,
                role=ROLE_MAIN if is_main else ROLE_LEFT_OUTER_TO_ONE_JOIN,
                view_elements=list(exposed),
                table_elements=list(exposed.values()),
            )
        )
        mandatory.extend(exposed)

    proposal.entries = entries
    seen: set[str] = set()
    proposal.mandatory_elements = [
        e for e in mandatory if not (e.upper() in seen or seen.add(e.upper()))
    ]
    return proposal


def _root_table(ctx: ValidationContext) -> TableMeta | None:
    """The table that decides this view's row identity, as DDIC metadata.

    The walk itself lives in :func:`lineage.root_table_name` — there were four
    copies of it, and a defect fixed in two of them survived here.
    """
    from cdcforge.lineage import root_table_name

    if ctx.view.from_source is None:
        return None
    name = root_table_name(ctx.metadata, ctx.view.from_source.name)
    return ctx.metadata.get_table(name) if name else None


def mandatory_elements(ctx: ValidationContext) -> list[str]:
    """F-22 — elements the user must not be allowed to deselect.

    All key fields of every mapped table, and all ON-condition foreign keys.
    The UI greys these out with a tooltip explaining why.
    """
    return build_cdc_mapping(ctx).mandatory_elements
