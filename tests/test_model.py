"""Verdict model — F-13.

The precedence rules here are the mechanism behind the specification's
non-negotiable principle. They are worth testing directly, because every other
answer the tool gives is derived from them.
"""

from __future__ import annotations

from cdcforge.model import (
    Assessment,
    Outcome,
    ParseIssue,
    RuleResult,
    Severity,
    Verdict,
    worst_verdict,
)


def result(outcome: Outcome, severity: Severity, rule_id: str = "R-XX") -> RuleResult:
    return RuleResult(
        rule_id=rule_id, outcome=outcome, severity=severity, message="test message here"
    )


def assessment(*results: RuleResult) -> Assessment:
    return Assessment(object_name="ZI_TEST", results=list(results))


def test_all_satisfied_is_a_pass():
    a = assessment(
        result(Outcome.SATISFIED, Severity.HARD),
        result(Outcome.NOT_APPLICABLE, Severity.FIXABLE),
    )
    assert a.verdict is Verdict.PASS


def test_inconclusive_never_becomes_a_pass():
    a = assessment(
        result(Outcome.SATISFIED, Severity.HARD),
        result(Outcome.INCONCLUSIVE, Severity.FIXABLE),
    )
    assert a.verdict is Verdict.MANUAL_REVIEW


def test_hard_violation_wins_over_everything():
    a = assessment(
        result(Outcome.VIOLATED, Severity.FIXABLE),
        result(Outcome.INCONCLUSIVE, Severity.MANUAL_REVIEW),
        result(Outcome.VIOLATED, Severity.HARD),
    )
    assert a.verdict is Verdict.FAIL_HARD


def test_manual_review_outranks_fixable():
    # FAIL_FIXABLE is a promise that the tool can generate a working
    # alternative. With something unresolved, that promise cannot be made.
    a = assessment(
        result(Outcome.VIOLATED, Severity.FIXABLE),
        result(Outcome.INCONCLUSIVE, Severity.MANUAL_REVIEW),
    )
    assert a.verdict is Verdict.MANUAL_REVIEW


def test_blocking_gates_writes_without_touching_the_verdict():
    a = assessment(
        result(Outcome.SATISFIED, Severity.HARD),
        result(Outcome.INCONCLUSIVE, Severity.BLOCKING, "R-28"),
    )
    assert a.verdict is Verdict.PASS
    assert a.write_blocked is True
    assert [r.rule_id for r in a.block_reasons] == ["R-28"]


def test_warning_never_moves_the_verdict():
    a = assessment(
        result(Outcome.SATISFIED, Severity.HARD),
        result(Outcome.VIOLATED, Severity.WARNING, "R-30"),
    )
    assert a.verdict is Verdict.PASS
    assert len(a.warnings) == 1


def test_unparseable_outranks_every_finding():
    a = assessment(result(Outcome.VIOLATED, Severity.HARD))
    a.unparseable = True
    a.parse_issues.append(ParseIssue("unterminated string literal", fatal=True))
    assert a.verdict is Verdict.UNPARSEABLE
    assert "unterminated" in a.summary


def test_worst_verdict_ordering():
    assert worst_verdict([]) is Verdict.PASS
    assert worst_verdict([Verdict.PASS, Verdict.FAIL_FIXABLE]) is Verdict.FAIL_FIXABLE
    assert (
        worst_verdict([Verdict.FAIL_FIXABLE, Verdict.MANUAL_REVIEW])
        is Verdict.MANUAL_REVIEW
    )
    assert worst_verdict([Verdict.MANUAL_REVIEW, Verdict.FAIL_HARD]) is Verdict.FAIL_HARD
    assert worst_verdict([Verdict.FAIL_HARD, Verdict.UNPARSEABLE]) is Verdict.UNPARSEABLE


def test_outcome_of_reports_the_most_severe_of_several_results():
    a = assessment(
        result(Outcome.SATISFIED, Severity.FIXABLE, "R-13"),
        result(Outcome.VIOLATED, Severity.FIXABLE, "R-13"),
    )
    assert a.outcome_of("R-13") is Outcome.VIOLATED
    assert a.outcome_of("R-99") is None


def test_summary_names_the_first_problem():
    a = assessment(
        RuleResult(
            rule_id="R-03",
            outcome=Outcome.VIOLATED,
            severity=Severity.HARD,
            message="aggregate SUM() used",
        ),
        RuleResult(
            rule_id="R-04",
            outcome=Outcome.VIOLATED,
            severity=Severity.HARD,
            message="GROUP BY present",
        ),
    )
    assert "aggregate SUM() used" in a.summary
    assert "+1 more" in a.summary
