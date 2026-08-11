"""F-14 — the cardinality prover, planning half.

    ABAP treats a wrong join cardinality as a *warning*, not an error, and does
    not validate it at runtime. So a view declaring [0..1] on a genuinely 1:N
    relationship activates cleanly, loads fine initially, and then produces
    duplicate or missing delta records that nobody catches for weeks.

This module decides *what would have to be true* for each declared to-one join,
and resolves as much as it can without reading any data. Execution lives in
``cdcforge.connect.prober`` — planning is pure logic and belongs in the offline
core, where it can be tested against fixtures.

Two tiers, and the first one matters more than it looks:

* **Structural proof.** If the joined table's side of the ON condition contains
  that table's whole primary key, at most one row can ever match. That is a
  guarantee from the key constraint, stronger than any sample of data, and it
  costs nothing. In practice a large share of real joins are exactly this
  shape — joins on the key.

* **Empirical probe.** Everything else needs data: count rows per join key and
  see whether any key has more than one. That is the only way to catch the
  expensive case, and it is what no competitor does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.parsing.nodes import DataSource, JoinCardinality, JoinType
from cdcforge.rules.context import (
    CardinalityEvidence,
    CardinalityResult,
    ValidationContext,
)


@dataclass
class ProbePlan:
    """What must hold for one declared to-one join, and how to establish it."""

    join_alias: str
    table: str
    join_fields: list[str] = field(default_factory=list)
    """The joined table's own columns used in the ON condition."""

    key_fields: list[str] = field(default_factory=list)
    """That table's primary key, for comparison."""

    client_field: str = ""
    structural: bool = False
    """True when the key constraint already guarantees to-one."""

    blocked: str = ""
    """Why this join cannot be probed, when it cannot."""

    reason: str = ""

    @property
    def needs_data(self) -> bool:
        return not self.structural and not self.blocked

    def evidence(self) -> CardinalityEvidence | None:
        """The evidence this plan yields without running anything."""
        if self.structural:
            return CardinalityEvidence(
                join_alias=self.join_alias,
                table=self.table,
                result=CardinalityResult.PROVEN_TO_ONE,
                max_count=1,
            )
        if self.blocked:
            return CardinalityEvidence(
                join_alias=self.join_alias,
                table=self.table,
                result=CardinalityResult.DECLARED_ONLY,
            )
        return None


def plan_cardinality_checks(ctx: ValidationContext) -> list[ProbePlan]:
    """One plan per declared to-one join in the view."""
    plans: list[ProbePlan] = []

    for join in ctx.view.joins:
        if join.join_type is not JoinType.LEFT_OUTER:
            continue
        if join.join_cardinality is not JoinCardinality.TO_ONE:
            # An undeclared cardinality is R-10's business, not evidence about
            # a claim nobody made.
            continue
        plans.append(_plan_for(ctx, join))

    return plans


def _multiplies_rows(view) -> bool:
    """Could this view emit more than one row per row of its root table?

    Answered conservatively: anything not *declared* to-one counts as
    multiplying. A bare ``LEFT OUTER JOIN`` and an inner join can both match
    several rows, and only a declared TO ONE says otherwise — the same reason
    R-10 refuses to read an undeclared cardinality as safe.

    A WHERE clause is deliberately not counted: it removes rows, and removing
    rows cannot turn a to-one match into a to-many one.
    """
    from cdcforge.parsing.nodes import JoinCardinality, JoinType

    if view.union_sources or view.set_operations:
        return True
    if view.group_by_ref is not None or view.aggregates:
        return True
    return any(
        j.join_type is not JoinType.LEFT_OUTER
        or j.join_cardinality is not JoinCardinality.TO_ONE
        for j in view.joins
    )


def _root_table_of(ctx: ValidationContext, view, depth: int = 6):
    """The table this view is rooted on, when its whole FROM chain is one-to-one.

    ``None`` as soon as any level could multiply rows, or the chain runs into
    something unreadable. Following only the FROM is the point: a join adds
    columns, but what a row *is* comes from the root.
    """
    from cdcforge.parsing.ddl import parse_ddl

    if depth <= 0 or view is None or _multiplies_rows(view):
        return None
    root = view.from_source
    if root is None:
        return None

    table = ctx.metadata.get_table(root.name)
    if table is not None:
        return table

    source = ctx.metadata.get_view_source(root.name)
    if source is None:
        return None
    nested = parse_ddl(source, name_hint=root.name)
    if nested.has_fatal_issue:
        return None
    return _root_table_of(ctx, nested, depth - 1)


def _plan_for_view_target(
    ctx: ValidationContext, join: DataSource, plan: ProbePlan, joined
) -> ProbePlan:
    """A join whose target is a CDS view rather than a table.

    Most of SAP's content joins views, so treating this as permanently
    unprovable left R-26 INCONCLUSIVE on the majority of real candidates — and
    a verdict that can never be reached is not a useful verdict.

    It reduces to the table case. If the elements the ON condition uses trace,
    through lineage, to the whole primary key of the table the joined view is
    rooted on, then joining on them is joining on that key, and the key
    constraint guarantees at most one match. The proof rests on a real database
    key, not on the view's own ``key`` declaration — CDS does not enforce that,
    so believing it would be exactly the assumption this tool exists to avoid.

    Two conditions have to hold, and both are checked rather than assumed: the
    view must not multiply rows, and every element in the ON condition must
    trace to that one table.
    """
    from cdcforge.lineage import element_origins

    root_table = _root_table_of(ctx, joined)
    if root_table is None:
        plan.blocked = (
            f"{join.name}'s rows cannot be tied to a key constraint — somewhere "
            f"down its FROM chain it can emit more than one row per base row, "
            f"or the chain could not be read"
        )
        return plan

    key_fields = {f.name.upper() for f in root_table.business_key_fields}
    if not key_fields:
        plan.blocked = f"{root_table.name} has no primary key to compare against"
        return plan

    # Which elements of the joined view does the ON condition name?
    used = {
        ref.leaf.upper()
        for ref in join.on_refs
        if ref.is_qualified and ref.root.upper() == join.local_name.upper()
    }
    if not used:
        plan.blocked = (
            f"no element of {join.name} could be traced in the ON condition, so "
            f"there is nothing to compare against a key"
        )
        return plan

    origins = element_origins(ctx.metadata, joined)
    covered = {
        origin.field.upper()
        for name, origin in origins.items()
        if name.upper() in used and origin.table.upper() == root_table.name.upper()
    }

    plan.table = root_table.name
    plan.key_fields = sorted(key_fields)
    plan.join_fields = sorted(covered)

    if key_fields <= covered:
        plan.structural = True
        plan.reason = (
            f"the ON condition uses elements of {join.name} that trace to the "
            f"whole primary key of {root_table.name} "
            f"({', '.join(sorted(key_fields))}), so at most one row can match — "
            f"guaranteed by that key constraint, not by the view's own key "
            f"declaration, which CDS does not enforce"
        )
        return plan

    missing = sorted(key_fields - covered)
    plan.blocked = (
        f"the ON condition reaches {', '.join(sorted(covered)) or 'nothing'} of "
        f"{root_table.name}'s key, missing {', '.join(missing)} — a count probe "
        f"would have to run against the view itself, which is a different and "
        f"far more expensive question"
    )
    return plan


def _plan_for(ctx: ValidationContext, join: DataSource) -> ProbePlan:
    plan = ProbePlan(join_alias=join.local_name, table=join.name.upper())

    table = ctx.table_for_source(join)
    if table is None:
        node = ctx.stack.node(join.name)
        if node is not None and node.view is not None:
            return _plan_for_view_target(ctx, join, plan, node.view)
        plan.blocked = f"{join.name} is not a known table"
        return plan

    plan.table = table.name
    plan.key_fields = [f.name for f in table.business_key_fields]
    client = table.client_field
    plan.client_field = client.name if client else ""

    # Which of this table's own columns does the ON condition use?
    joined_side: list[str] = []
    for ref in join.on_refs:
        origin = ctx.resolve_ref(ref)
        if origin is None:
            continue
        if origin.object_name.upper() != join.name.upper():
            continue
        if origin.field_name.upper() == plan.client_field.upper():
            continue  # the client is framework-handled, not part of the claim
        if origin.field_name.upper() not in {f.upper() for f in joined_side}:
            joined_side.append(origin.field_name)
    plan.join_fields = joined_side

    if not joined_side:
        plan.blocked = (
            "no column of this table could be traced in the ON condition, so "
            "there is nothing to count by"
        )
        return plan

    if not plan.key_fields:
        plan.blocked = f"{table.name} has no primary key to compare against"
        return plan

    # The free proof: joining on the whole key can match at most one row.
    if {f.upper() for f in plan.key_fields} <= {f.upper() for f in joined_side}:
        plan.structural = True
        plan.reason = (
            f"the ON condition covers the whole primary key of {table.name} "
            f"({', '.join(plan.key_fields)}), so at most one row can match — "
            f"guaranteed by the key constraint, not inferred from a sample"
        )
        return plan

    missing = [
        k for k in plan.key_fields
        if k.upper() not in {f.upper() for f in joined_side}
    ]
    plan.reason = (
        f"the ON condition uses {', '.join(joined_side)}, which does not cover "
        f"{table.name}'s key (missing {', '.join(missing)}), so nothing "
        f"guarantees uniqueness — this needs data"
    )
    return plan


def structural_evidence(ctx: ValidationContext) -> dict[str, CardinalityEvidence]:
    """Everything provable without reading a single row.

    Worth calling on its own: it costs nothing, needs no data-access
    permission, and on real content resolves the joins that are simply joins on
    the key.
    """
    evidence: dict[str, CardinalityEvidence] = {}
    for plan in plan_cardinality_checks(ctx):
        found = plan.evidence()
        if found is not None and found.result is CardinalityResult.PROVEN_TO_ONE:
            evidence[plan.join_alias] = found
    return evidence


def summarise(plans: list[ProbePlan]) -> str:
    if not plans:
        return "no declared to-one joins"
    structural = sum(1 for p in plans if p.structural)
    blocked = sum(1 for p in plans if p.blocked)
    return (
        f"{len(plans)} to-one join(s): {structural} proven by key constraint, "
        f"{len(plans) - structural - blocked} need data, {blocked} cannot be probed"
    )
