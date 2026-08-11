"""F-12 — the rule engine.

Parses, resolves the stack, runs every registered rule, and folds the results
into one verdict. The engine holds no state and talks to no system.
"""

from __future__ import annotations

from cdcforge.metadata.base import MetadataSource, NullMetadataSource
from cdcforge.metadata.types import ObjectMeta
from cdcforge.model import Assessment, Outcome, RuleResult, Severity, SourceRef
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.parsing.nodes import EntityKind, ParsedView
from cdcforge.rules import catalog  # noqa: F401  — importing registers R-01…R-30
from cdcforge.rules.advisories import advisories
from cdcforge.rules.base import all_rules, get_rule
from cdcforge.rules.context import (
    CardinalityEvidence,
    RuleConfig,
    SubscriptionState,
    ValidationContext,
)


def validate_source(
    source_text: str,
    *,
    name: str = "",
    metadata: MetadataSource | None = None,
    config: RuleConfig | None = None,
    object_meta: ObjectMeta | None = None,
    subscription_state: SubscriptionState = SubscriptionState.UNKNOWN,
    cardinality_evidence: dict[str, CardinalityEvidence] | None = None,
    include_advisories: bool = True,
    only_rules: list[str] | None = None,
) -> Assessment:
    """Validate DDL text."""
    view = parse_ddl(source_text, name_hint=name)
    return validate_view(
        view,
        metadata=metadata,
        config=config,
        object_meta=object_meta,
        subscription_state=subscription_state,
        cardinality_evidence=cardinality_evidence,
        include_advisories=include_advisories,
        only_rules=only_rules,
    )


def validate_view(
    view: ParsedView,
    *,
    metadata: MetadataSource | None = None,
    config: RuleConfig | None = None,
    object_meta: ObjectMeta | None = None,
    subscription_state: SubscriptionState = SubscriptionState.UNKNOWN,
    cardinality_evidence: dict[str, CardinalityEvidence] | None = None,
    include_advisories: bool = True,
    only_rules: list[str] | None = None,
    context: ValidationContext | None = None,
) -> Assessment:
    """Validate an already-parsed view.

    Pass ``context`` to reuse a :class:`ValidationContext` the caller has
    already built. F-09 needs the dependency stack to decide whether a
    candidate is rooted on the table, and then validates it — building the
    context twice walked that stack twice, per candidate, for no benefit. When
    a context is given the other keyword arguments describing it are ignored,
    since it already carries them.
    """
    metadata = metadata or NullMetadataSource()
    assessment = Assessment(
        object_name=view.name or "<anonymous>",
        parse_issues=list(view.issues),
        source_text=view.source_text,
    )

    if view.has_fatal_issue:
        # No confident AST. Per F-11 this is UNPARSEABLE, and no rule runs —
        # a rule evaluated against a half-built AST would produce a verdict
        # nobody should act on.
        assessment.unparseable = True
        return assessment

    if not view.entity_kind.is_selecting_view and view.entity_kind is not EntityKind.TABLE_FUNCTION:
        assessment.results.append(
            RuleResult(
                rule_id="E-01",
                outcome=Outcome.VIOLATED,
                severity=Severity.HARD,
                message=(
                    f"{view.name or 'this object'} is a "
                    f"{view.entity_kind.value.replace('_', ' ').lower()}, not a "
                    f"data-selecting view — data extraction and CDC do not apply"
                ),
                ref=SourceRef(line=1, column=1),
                node=view.name,
                sap_source="F-13 — engine-level determination, not one of R-01…R-30",
                remediation="Point the assessment at the view that reads the data.",
            )
        )
        return assessment

    resolved_object_meta = object_meta
    if resolved_object_meta is None and view.name:
        resolved_object_meta = metadata.get_object(view.name)

    ctx = context or ValidationContext(
        view=view,
        metadata=metadata,
        config=config or RuleConfig(),
        object_meta=resolved_object_meta,
        subscription_state=subscription_state,
        cardinality_evidence=cardinality_evidence or {},
    )

    selected = set(only_rules) if only_rules else None
    for rule_obj in all_rules():
        if selected is not None and rule_obj.id not in selected:
            continue
        assessment.results.extend(rule_obj.run(ctx))

    if include_advisories and selected is None:
        assessment.results.extend(advisories(ctx))

    assessment.stack = ctx.stack
    return assessment


def validate_object(
    name: str,
    metadata: MetadataSource,
    **kwargs,
) -> Assessment:
    """Validate a named object, reading its source from the metadata source."""
    source = metadata.get_view_source(name)
    if source is None:
        assessment = Assessment(object_name=name)
        assessment.results.append(
            RuleResult(
                rule_id="E-02",
                outcome=Outcome.INCONCLUSIVE,
                severity=Severity.MANUAL_REVIEW,
                message=f"no DDL source available for {name} in {metadata.describe()}",
                node=name,
            )
        )
        return assessment
    kwargs.setdefault("name", name)
    kwargs.setdefault("metadata", metadata)
    return validate_source(source, **kwargs)


def validate_all(metadata: MetadataSource, **kwargs) -> list[Assessment]:
    """Validate every view the metadata source knows about."""
    return [validate_object(name, metadata, **kwargs) for name in metadata.list_views()]


def run_single_rule(rule_id: str, ctx: ValidationContext) -> list[RuleResult]:
    """Run one rule. Used by the per-rule unit tests."""
    rule_obj = get_rule(rule_id)
    if rule_obj is None:
        raise KeyError(f"unknown rule {rule_id}")
    return rule_obj.run(ctx)
