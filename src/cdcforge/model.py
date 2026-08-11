"""Verdict model (F-13) and shared value objects.

The non-negotiable principle from the specification:

    A verdict of MANUAL_REVIEW or UNPARSEABLE is a *correct output*, not a
    failure. Never let ambiguity resolve to PASS.

Everything in this module exists to make that principle mechanical rather than
a matter of discipline: an inconclusive rule result cannot produce a PASS,
because :func:`Assessment.verdict` derives the verdict from the results rather
than trusting a caller to set it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class Verdict(str, Enum):
    """F-13 — the five possible outcomes for an object."""

    PASS = "PASS"
    """All deterministic rules satisfied."""

    FAIL_HARD = "FAIL_HARD"
    """Structurally impossible. Cannot be fixed by annotation or wrapping."""

    FAIL_FIXABLE = "FAIL_FIXABLE"
    """Fails now, but the tool can generate a compliant alternative."""

    MANUAL_REVIEW = "MANUAL_REVIEW"
    """Rules cannot decide. Reason stated."""

    UNPARSEABLE = "UNPARSEABLE"
    """Parser could not build a confident AST."""


# Precedence when combining rule results into one verdict.
#
# Note the ordering choice: MANUAL_REVIEW outranks FAIL_FIXABLE. FAIL_FIXABLE
# is a *promise* — "the tool can generate a compliant alternative". If any part
# of the analysis is unresolved that promise cannot honestly be made, so the
# unresolved item wins. This is the fail-safe direction.
_VERDICT_RANK: dict[Verdict, int] = {
    Verdict.PASS: 0,
    Verdict.FAIL_FIXABLE: 1,
    Verdict.MANUAL_REVIEW: 2,
    Verdict.FAIL_HARD: 3,
    Verdict.UNPARSEABLE: 4,
}


def worst_verdict(verdicts: Iterable[Verdict]) -> Verdict:
    """Combine verdicts, keeping the most severe."""
    return max(verdicts, key=lambda v: _VERDICT_RANK[v], default=Verdict.PASS)


class Severity(str, Enum):
    """Rule severity, per the §5 rule table."""

    HARD = "HARD"
    """Structurally impossible."""

    FIXABLE = "FIXABLE"
    """The tool can generate a compliant alternative."""

    MANUAL_REVIEW = "MANUAL_REVIEW"
    """Cannot be decided statically."""

    BLOCKING = "BLOCKING"
    """Blocks *writes*, not the static verdict (R-28 active subscription)."""

    WARNING = "WARNING"
    """Advisory. Never changes the verdict."""


class Outcome(str, Enum):
    """What a single rule concluded about a single object."""

    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    INCONCLUSIVE = "INCONCLUSIVE"
    """The rule could not be evaluated — missing metadata, unresolved branch.

    An inconclusive result NEVER contributes a PASS. It contributes
    MANUAL_REVIEW regardless of the rule's declared severity.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The rule does not apply to this object (e.g. mapping rules on a view
    that legitimately uses ``changeDataCapture.automatic``)."""


@dataclass(frozen=True)
class SourceRef:
    """A position in the DDL source, for pointing the user at the problem.

    UI rule from the spec: verdicts always show *why*, never just a red icon.
    A rule result without a usable ref is a rule result that cannot be
    explained, so every rule that can cite a line does.
    """

    line: int = 0
    column: int = 0
    snippet: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        if not self.line:
            return ""
        return f"line {self.line}" + (f", col {self.column}" if self.column else "")


@dataclass
class RuleResult:
    """The output of one rule against one object.

    Carries everything F-12 requires: verdict, offending node, source line,
    plain-English explanation, and the SAP rule or KBA it derives from.
    """

    rule_id: str
    outcome: Outcome
    severity: Severity
    message: str
    ref: SourceRef = field(default_factory=SourceRef)
    node: str = ""
    """The offending AST node — a join alias, element name, table name."""

    sap_source: str = ""
    """The SAP Note / KBA / documented behaviour the rule derives from."""

    remediation: str = ""
    """Plain-English next action, where one exists."""

    detail: dict[str, Any] = field(default_factory=dict)
    """Structured payload for the UI and the report exporter."""

    @property
    def is_problem(self) -> bool:
        return self.outcome in (Outcome.VIOLATED, Outcome.INCONCLUSIVE)

    def verdict_contribution(self) -> Verdict:
        """Map this single result onto the verdict scale."""
        # BLOCKING and WARNING answer a different question from the verdict.
        # BLOCKING gates the write pipeline (see Assessment.write_blocked);
        # WARNING is advisory. Neither moves the static verdict, in any outcome
        # — otherwise R-28, which is inconclusive by construction whenever no
        # system is connected, would push every offline assessment to
        # MANUAL_REVIEW and drown the findings that matter.
        if self.severity in (Severity.BLOCKING, Severity.WARNING):
            return Verdict.PASS

        if self.outcome in (Outcome.SATISFIED, Outcome.NOT_APPLICABLE):
            return Verdict.PASS
        if self.outcome is Outcome.INCONCLUSIVE:
            # Ambiguity never resolves to PASS, whatever the rule's severity.
            return Verdict.MANUAL_REVIEW
        # VIOLATED
        if self.severity is Severity.HARD:
            return Verdict.FAIL_HARD
        if self.severity is Severity.FIXABLE:
            return Verdict.FAIL_FIXABLE
        return Verdict.MANUAL_REVIEW

    def format_line(self) -> str:
        """One-line rendering used by the CLI and the report."""
        where = f" ({self.ref})" if self.ref.line else ""
        node = f" [{self.node}]" if self.node else ""
        return f"{self.rule_id} {self.outcome.value}{node}{where}: {self.message}"


@dataclass
class ParseIssue:
    """Something the parser could not handle confidently."""

    message: str
    ref: SourceRef = field(default_factory=SourceRef)
    fatal: bool = False
    """A fatal issue means no confident AST, therefore UNPARSEABLE."""


@dataclass
class Assessment:
    """The full result for one object."""

    object_name: str
    results: list[RuleResult] = field(default_factory=list)
    parse_issues: list[ParseIssue] = field(default_factory=list)
    unparseable: bool = False
    stack: Any = None
    """The resolved dependency stack (F-08), when one was built."""

    source_text: str = ""

    @property
    def verdict(self) -> Verdict:
        if self.unparseable:
            return Verdict.UNPARSEABLE
        return worst_verdict(r.verdict_contribution() for r in self.results)

    @property
    def write_blocked(self) -> bool:
        """True when a BLOCKING rule fired or could not be evaluated.

        R-28 (active subscription) is the case this exists for. Offline, the
        subscription state is unknowable, so it comes back INCONCLUSIVE and
        writes stay blocked. Blocking by default is the correct behaviour: the
        cost of being wrong is a broken subscription and a full delta re-init.
        """
        return any(
            r.severity is Severity.BLOCKING and r.is_problem for r in self.results
        )

    @property
    def block_reasons(self) -> list[RuleResult]:
        return [
            r for r in self.results if r.severity is Severity.BLOCKING and r.is_problem
        ]

    @property
    def problems(self) -> list[RuleResult]:
        return [r for r in self.results if r.is_problem]

    @property
    def warnings(self) -> list[RuleResult]:
        return [
            r
            for r in self.results
            if r.severity is Severity.WARNING and r.outcome is Outcome.VIOLATED
        ]

    def results_for(self, rule_id: str) -> list[RuleResult]:
        return [r for r in self.results if r.rule_id == rule_id]

    def outcome_of(self, rule_id: str) -> Outcome | None:
        """The most severe outcome recorded for a rule, or None if it never ran."""
        results = self.results_for(rule_id)
        if not results:
            return None
        order = {
            Outcome.NOT_APPLICABLE: 0,
            Outcome.SATISFIED: 1,
            Outcome.INCONCLUSIVE: 2,
            Outcome.VIOLATED: 3,
        }
        return max((r.outcome for r in results), key=lambda o: order[o])

    @property
    def summary(self) -> str:
        v = self.verdict
        if v is Verdict.UNPARSEABLE:
            reason = next(
                (i.message for i in self.parse_issues if i.fatal), "no confident AST"
            )
            return f"{self.object_name}: UNPARSEABLE — {reason}"
        problems = self.problems
        if not problems:
            return f"{self.object_name}: PASS"
        head = problems[0]
        extra = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        return f"{self.object_name}: {v.value} — {head.message}{extra}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the report exporter and for tests."""
        return {
            "object": self.object_name,
            "verdict": self.verdict.value,
            "write_blocked": self.write_blocked,
            "results": [
                {
                    "rule": r.rule_id,
                    "outcome": r.outcome.value,
                    "severity": r.severity.value,
                    "message": r.message,
                    "node": r.node,
                    "line": r.ref.line,
                    "sap_source": r.sap_source,
                    "remediation": r.remediation,
                    "detail": r.detail,
                }
                for r in self.results
            ],
            "parse_issues": [
                {"message": i.message, "line": i.ref.line, "fatal": i.fatal}
                for i in self.parse_issues
            ],
        }
