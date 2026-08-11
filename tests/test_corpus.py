"""The agreement corpus — §10.

Every fixture must produce the verdict it was written to produce, *for the
reason it was written to produce it*. And the corpus must between them exercise
every rule, so a rule cannot rot unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdcforge.model import Outcome, Verdict
from cdcforge.rules import rule_ids, validate_all
from cdcforge.triage import Bucket, classify, triage

#: Rules that depend on runtime state a DDL file cannot carry. Covered in
#: tests/test_rules.py with that state injected instead.
RUNTIME_STATE_RULES = {"R-26", "R-28"}

_EXPECTED_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "expected.json"
_EXPECTED = {
    name: entry
    for name, entry in json.loads(_EXPECTED_PATH.read_text(encoding="utf-8")).items()
    if not name.startswith("_")
}
CORPUS = sorted(_EXPECTED)


def test_every_expected_fixture_exists(metadata, expected):
    missing = [name for name in expected if metadata.get_view_source(name) is None]
    assert missing == []


@pytest.mark.parametrize("name", CORPUS)
def test_fixture_verdict_and_reason(name, assess, expected):
    entry = expected[name]
    assessment = assess(name)

    assert assessment.verdict is Verdict(entry["verdict"]), (
        f"{name}: expected {entry['verdict']}, got {assessment.verdict.value} — "
        f"{assessment.summary}"
    )

    for rule_id, want in entry["expect"].items():
        got = assessment.outcome_of(rule_id)
        assert got is Outcome(want), (
            f"{name}: {rule_id} expected {want}, got "
            f"{got.value if got else 'not evaluated'}"
        )


def test_corpus_exercises_every_rule(expected):
    covered = {rule for entry in expected.values() for rule in entry["expect"]}
    catalogue = {r for r in rule_ids() if r.startswith("R-")}
    uncovered = catalogue - covered - RUNTIME_STATE_RULES
    assert uncovered == set(), f"no fixture exercises {sorted(uncovered)}"


def test_corpus_has_both_positive_and_negative_cases(expected):
    verdicts = {entry["verdict"] for entry in expected.values()}
    assert "PASS" in verdicts
    assert {"FAIL_HARD", "FAIL_FIXABLE", "MANUAL_REVIEW", "UNPARSEABLE"} <= verdicts


def test_no_fixture_passes_by_accident(assess, expected):
    """A PASS must have no problem at any verdict-bearing severity.

    The point of the rule: a false PASS is far worse than a false FAIL.
    """
    for name, entry in expected.items():
        if entry["verdict"] != "PASS":
            continue
        assessment = assess(name)
        offenders = [
            r
            for r in assessment.problems
            if r.verdict_contribution() is not Verdict.PASS
        ]
        assert offenders == [], f"{name} passed but has {offenders}"


def test_scan_produces_the_three_lists(metadata):
    assessments = validate_all(metadata)
    summary = triage(assessments, metadata)
    assert summary.total == len(metadata.list_views())
    assert summary.count(Bucket.READY) > 0
    assert summary.count(Bucket.FIXABLE) > 0
    assert summary.count(Bucket.NOT_POSSIBLE) > 0
    assert "ZLEGACY_POOL" in summary.bare_tables


def test_bare_tables_are_those_no_view_reads(metadata):
    summary = triage(validate_all(metadata), metadata)
    # ZCUSTORDER is read by ZI_CUSTORDER, so it is not bare.
    assert "ZCUSTORDER" not in summary.bare_tables


def test_classification_never_promises_a_fix_it_cannot_deliver(assess):
    # An aggregating view is NOT_POSSIBLE, not FIXABLE.
    assert classify(assess("ZI_AGGREGATE")) is Bucket.NOT_POSSIBLE
    assert classify(assess("ZI_NO_EXTRACTION")) is Bucket.FIXABLE


def test_every_reported_problem_explains_itself(metadata):
    """UI rule: verdicts always show *why*, never just a red icon.

    Message length is not the test — "HAVING present" is short and complete.
    What a finding must carry is a reason, somewhere to look, and the authority
    it rests on.
    """
    for assessment in validate_all(metadata):
        for result in assessment.problems:
            where = f"{assessment.object_name}/{result.rule_id}"
            assert result.message.strip(), f"{where}: no message"
            assert result.ref.line or result.node, f"{where}: nothing to point at"
            if result.rule_id.startswith("R-"):
                assert result.sap_source, f"{where}: no SAP source cited"


def test_assessments_serialise(metadata):
    for assessment in validate_all(metadata):
        payload = assessment.to_dict()
        assert payload["object"]
        assert payload["verdict"] in {v.value for v in Verdict}
