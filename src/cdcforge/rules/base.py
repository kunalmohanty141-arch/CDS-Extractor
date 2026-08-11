"""Rule registry and result constructors (F-12).

Each rule returns: verdict, offending node, source line, plain-English
explanation, and the SAP rule or KBA it derives from. That obligation is
encoded in :class:`RuleSpec` and in the constructors below, so a rule cannot be
registered without declaring where its authority comes from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from cdcforge.model import Outcome, RuleResult, Severity, SourceRef
from cdcforge.rules.context import ValidationContext


@dataclass(frozen=True)
class RuleSpec:
    id: str
    title: str
    severity: Severity
    sap_source: str
    tier: str = "MVP"
    rationale: str = ""
    """Why the constraint exists, in one sentence — shown in the rule drawer."""


RuleFn = Callable[[ValidationContext, RuleSpec], Iterable[RuleResult]]


@dataclass(frozen=True)
class Rule:
    spec: RuleSpec
    fn: RuleFn

    @property
    def id(self) -> str:
        return self.spec.id

    def run(self, ctx: ValidationContext) -> list[RuleResult]:
        """Run the rule, converting an unexpected error into INCONCLUSIVE.

        A rule that crashes must not take the whole assessment down, and must
        certainly not be skipped silently — a skipped rule reads as a passed
        rule. It becomes INCONCLUSIVE, which forces MANUAL_REVIEW.
        """
        try:
            return list(self.fn(ctx, self.spec))
        except Exception as exc:  # pragma: no cover - defensive
            return [
                RuleResult(
                    rule_id=self.spec.id,
                    outcome=Outcome.INCONCLUSIVE,
                    severity=self.spec.severity,
                    message=f"rule could not be evaluated: {exc.__class__.__name__}: {exc}",
                    sap_source=self.spec.sap_source,
                )
            ]


_REGISTRY: dict[str, Rule] = {}


def rule(
    id: str,
    title: str,
    severity: Severity,
    sap_source: str,
    *,
    tier: str = "MVP",
    rationale: str = "",
) -> Callable[[RuleFn], RuleFn]:
    """Register a rule implementation."""

    def decorator(fn: RuleFn) -> RuleFn:
        if id in _REGISTRY:  # pragma: no cover - programming error
            raise ValueError(f"rule {id} is already registered")
        spec = RuleSpec(
            id=id,
            title=title,
            severity=severity,
            sap_source=sap_source,
            tier=tier,
            rationale=rationale,
        )
        _REGISTRY[id] = Rule(spec=spec, fn=fn)
        return fn

    return decorator


def all_rules() -> list[Rule]:
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def get_rule(id: str) -> Rule | None:
    return _REGISTRY.get(id)


def rule_ids() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Result constructors
# ---------------------------------------------------------------------------


def _result(
    spec: RuleSpec,
    outcome: Outcome,
    message: str,
    *,
    ref: SourceRef | None = None,
    node: str = "",
    remediation: str = "",
    severity: Severity | None = None,
    detail: dict | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=spec.id,
        outcome=outcome,
        severity=severity or spec.severity,
        message=message,
        ref=ref or SourceRef(),
        node=node,
        sap_source=spec.sap_source,
        remediation=remediation,
        detail=detail or {},
    )


def satisfied(spec: RuleSpec, message: str = "", **kwargs) -> RuleResult:
    return _result(spec, Outcome.SATISFIED, message or spec.title, **kwargs)


def violated(spec: RuleSpec, message: str, **kwargs) -> RuleResult:
    return _result(spec, Outcome.VIOLATED, message, **kwargs)


def inconclusive(spec: RuleSpec, message: str, **kwargs) -> RuleResult:
    """The rule could not decide. This never becomes a PASS."""
    return _result(spec, Outcome.INCONCLUSIVE, message, **kwargs)


def not_applicable(spec: RuleSpec, message: str = "", **kwargs) -> RuleResult:
    return _result(spec, Outcome.NOT_APPLICABLE, message or "not applicable", **kwargs)
