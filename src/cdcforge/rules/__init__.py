"""The rule engine — R-01 … R-30 plus advisories.

Zero SAP dependency: fixtures in, verdicts out.
"""

from cdcforge.rules.advisories import RiskLevel, advisories, trigger_load_risk
from cdcforge.rules.base import Rule, RuleSpec, all_rules, get_rule, rule_ids
from cdcforge.rules.context import (
    CardinalityEvidence,
    CardinalityResult,
    RuleConfig,
    SubscriptionState,
    ValidationContext,
)
from cdcforge.rules.engine import (
    run_single_rule,
    validate_all,
    validate_object,
    validate_source,
    validate_view,
)
from cdcforge.rules.stack import NodeKind, StackNode, ViewStack, resolve_stack

__all__ = [
    "CardinalityEvidence",
    "CardinalityResult",
    "NodeKind",
    "RiskLevel",
    "Rule",
    "RuleConfig",
    "RuleSpec",
    "StackNode",
    "SubscriptionState",
    "ValidationContext",
    "ViewStack",
    "advisories",
    "all_rules",
    "get_rule",
    "resolve_stack",
    "rule_ids",
    "run_single_rule",
    "trigger_load_risk",
    "validate_all",
    "validate_object",
    "validate_source",
    "validate_view",
]
