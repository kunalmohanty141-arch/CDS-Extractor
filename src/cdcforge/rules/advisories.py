"""Advisories — operational warnings that are not pass/fail rules.

These come from the feature list rather than the rule table (F-17 trigger load,
F-18 delete semantics, F-24 semantic annotations, Appendix A.8 on DDIC-only
annotations in view entities). They carry WARNING severity, so they never move
the verdict — a view can be perfectly CDC-valid and still carry every one of
them.

They are kept out of ``catalog.py`` deliberately: R-01…R-30 are the *rules*, and
mixing advisory findings into that namespace would make the rule-agreement
benchmark (tool verdict vs SAP checkruns) meaningless.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterator

from cdcforge.cds import ANN_DATA_CATEGORY, ANN_LABEL, DDIC_ONLY_ANNOTATIONS
from cdcforge.model import Outcome, RuleResult, Severity
from cdcforge.parsing.nodes import EntityKind
from cdcforge.rules.context import ValidationContext


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


def _advisory(
    advisory_id: str,
    message: str,
    *,
    node: str = "",
    sap_source: str = "",
    remediation: str = "",
    detail: dict | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=advisory_id,
        outcome=Outcome.VIOLATED,
        severity=Severity.WARNING,
        message=message,
        node=node,
        sap_source=sap_source,
        remediation=remediation,
        detail=detail or {},
    )


def trigger_load_risk(ctx: ValidationContext) -> tuple[RiskLevel, list[str]]:
    """F-17 — score the trigger load this view would add.

    CDC is trigger-based at table level, so enabling it changes the write path
    of a live ERP system. The score combines the known-hot list with row-count
    estimates.

    Deliberately qualitative. No SAP-published number caps how many tables can
    safely be CDC-enabled, and inventing a threshold here would be the kind of
    false precision the specification warns against.
    """
    hot: list[str] = []
    large: list[str] = []
    unknown = 0

    tables = [n.table for n in ctx.stack.leaf_tables if n.table is not None]
    if ctx.mapping:
        mapped = {e.table for e in ctx.mapping if e.table}
        if mapped:
            tables = [t for t in tables if t.name in mapped] or tables

    for table in tables:
        if table.is_hot:
            hot.append(table.name)
        if table.estimated_rows is None:
            unknown += 1
        elif table.estimated_rows > 100_000_000:
            large.append(table.name)

    if hot:
        return RiskLevel.HIGH, sorted(set(hot) | set(large))
    if large:
        return RiskLevel.MEDIUM, sorted(set(large))
    if not tables or unknown == len(tables):
        return RiskLevel.UNKNOWN, []
    return RiskLevel.LOW, []


def advisories(ctx: ValidationContext) -> Iterator[RuleResult]:
    """Every advisory that applies to this view."""

    # -- F-17 trigger load ------------------------------------------------
    level, tables = trigger_load_risk(ctx)
    if level is RiskLevel.HIGH:
        yield _advisory(
            "F-17",
            f"high trigger-load risk: {', '.join(sorted(tables))} are "
            f"high-frequency transactional tables. Enabling CDC adds "
            f"INSERT/UPDATE/DELETE triggers to the live write path.",
            node=ctx.view.name,
            sap_source="Appendix B.4 — SAP publishes no cap on how many tables "
            "can safely be CDC-enabled; guidance is qualitative",
            remediation="Map only the tables whose changes should actually "
            "trigger a delta.",
            detail={"risk": level.value, "tables": sorted(tables)},
        )
    elif level is RiskLevel.MEDIUM:
        yield _advisory(
            "F-17",
            f"moderate trigger-load risk: {', '.join(sorted(tables))} are large "
            f"tables",
            node=ctx.view.name,
            detail={"risk": level.value, "tables": sorted(tables)},
        )

    # -- F-18 delete semantics --------------------------------------------
    mapping = ctx.mapping or []
    joined_tables = len(ctx.view.joins) > 0 or len(mapping) > 1
    if joined_tables:
        non_main = [e.table for e in mapping if not e.is_main and e.table]
        detail_names = ", ".join(non_main) if non_main else "the joined tables"
        yield _advisory(
            "F-18",
            f"delete semantics: deletions propagate only when the #MAIN record is "
            f"deleted. Deletes on {detail_names} will not delete the output "
            f"record — they may generate an update if joined attributes change.",
            node=ctx.view.name,
            sap_source="Appendix A.6 — SAP: deletions with regard to this CDS view "
            "only happen if the record in the main table is deleted",
            remediation="This is a property of correctly-working CDC, not a "
            "defect. It surfaces months later as orphaned rows in Datasphere, so "
            "decide now whether the target needs separate housekeeping.",
            detail={"joined_tables": non_main},
        )

    # -- A.8 DDIC-only annotations in a view entity -----------------------
    annotations = ctx.view.annotations
    if ctx.view.entity_kind is EntityKind.VIEW_ENTITY and annotations is not None:
        offenders = [path for path in DDIC_ONLY_ANNOTATIONS if annotations.has(path)]
        if offenders:
            yield _advisory(
                "A.8",
                f"view entity carries DDIC-only annotation(s): "
                f"{', '.join(offenders)}",
                node=ctx.view.name,
                sap_source="Appendix A.8 — for view entities, DDIC-only "
                "annotations must be removed or extraction validation fails",
                remediation="Remove them. A view entity generates no separate "
                "DDIC SQL view, so they have nothing to describe.",
                detail={"annotations": offenders},
            )

    # -- F-24 semantic annotations ----------------------------------------
    if annotations is not None and not annotations.has(ANN_DATA_CATEGORY):
        yield _advisory(
            "F-24",
            "@Analytics.dataCategory is not set (#FACT / #DIMENSION)",
            node=ctx.view.name,
            sap_source="F-24 — Datasphere and SAC consumption needs more than "
            "extraction",
            remediation="Set dataCategory, currency/unit reference fields and an "
            "@EndUserText.label. Views land in Datasphere far more usable when "
            "these are right, and they are nearly always forgotten.",
        )
    elif annotations is not None and not annotations.has(ANN_LABEL):
        yield _advisory(
            "F-24",
            "@EndUserText.label is not set",
            node=ctx.view.name,
            remediation="Add a label; it is what the consumer sees in Datasphere.",
        )
