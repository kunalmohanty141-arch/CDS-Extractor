"""§6 store tests.

The rule that matters most here is cache invalidation. A stale verdict is worse
than no verdict: it is confidently wrong and nothing downstream can tell.
"""

from __future__ import annotations

from cdcforge.inventory import compare_snapshots
from cdcforge.model import (
    Assessment,
    Outcome,
    RuleResult,
    Severity,
    SourceRef,
    Verdict,
)
from cdcforge.store import Store, ViewRecord, source_hash


def make_store(tmp_path, profile="TEST") -> Store:
    return Store(tmp_path / "store.sqlite", profile_id=profile)


# ---------------------------------------------------------------------------
# An assessment with no findings is still an assessment
# ---------------------------------------------------------------------------


def make_unparseable(name="ZI_BROKEN") -> Assessment:
    from cdcforge.model import ParseIssue

    return Assessment(
        object_name=name,
        results=[],
        parse_issues=[
            ParseIssue("unterminated string literal", SourceRef(line=7), fatal=True)
        ],
        unparseable=True,
    )


def test_an_unparseable_assessment_survives_the_store(tmp_path):
    """It has no rule results, and the store used to record only results.

    So it wrote no rows and vanished — from cached_assessment and from
    all_assessments alike. UNPARSEABLE is the verdict the specification is most
    insistent about being a correct output, and the store forgot it.
    """
    store = make_store(tmp_path)
    source = "define view entity ZI_BROKEN as select from t001 { 'oops }"
    digest = source_hash(source)
    store.put_verdicts(make_unparseable(), digest)

    revived = store.cached_assessment("ZI_BROKEN", digest)
    assert revived is not None, "an assessment with no findings still exists"
    assert revived.unparseable
    assert revived.verdict is Verdict.UNPARSEABLE, (
        "without the unparseable flag it would have no results and no fatal "
        "issue, which computes to PASS — a stored 'I could not read this' "
        "coming back as 'this is fine'"
    )
    assert [i.message for i in revived.parse_issues] == [
        "unterminated string literal"
    ]
    assert revived.parse_issues[0].fatal
    assert revived.parse_issues[0].ref.line == 7


def test_all_assessments_includes_one_with_no_findings(tmp_path):
    store = make_store(tmp_path)
    store.put_verdicts(make_assessment("ZI_NORMAL"), source_hash("a"))
    store.put_verdicts(make_unparseable("ZI_BROKEN"), source_hash("b"))

    names = {a.object_name for a in store.all_assessments()}
    assert names == {"ZI_NORMAL", "ZI_BROKEN"}, (
        "listing objects by the findings they produced omits those that "
        "produced none, and the report's totals then disagree with reality"
    )


def test_a_clean_pass_with_no_findings_also_survives(tmp_path):
    """Not only the unparseable case: a view that violates nothing at all."""
    store = make_store(tmp_path)
    digest = source_hash("clean")
    store.put_verdicts(Assessment(object_name="ZI_CLEAN", results=[]), digest)

    revived = store.cached_assessment("ZI_CLEAN", digest)
    assert revived is not None
    assert not revived.unparseable
    assert revived.results == []


def make_assessment(name="ZI_X", rule="R-01", outcome=Outcome.VIOLATED) -> Assessment:
    return Assessment(
        object_name=name,
        results=[
            RuleResult(
                rule_id=rule,
                outcome=outcome,
                severity=Severity.FIXABLE,
                message="extraction annotation is missing",
                ref=SourceRef(line=4),
                node=name,
                sap_source="Appendix E.2",
                remediation="Add the annotation.",
                detail={"k": "v"},
            )
        ],
    )


# ---------------------------------------------------------------------------
# Sources and hashing
# ---------------------------------------------------------------------------


def test_source_round_trip(tmp_path):
    store = make_store(tmp_path)
    digest = store.put_source("ZI_X", "define view entity ZI_X as select from t { key t.k as K }")
    assert store.get_source("ZI_X").startswith("define")
    assert store.get_source_hash("ZI_X") == digest


def test_hash_ignores_line_endings(tmp_path):
    # Otherwise a CRLF round-trip through Windows invalidates the whole cache.
    assert source_hash("a\nb\n") == source_hash("a\r\nb\r\n")


def test_lookup_is_case_insensitive(tmp_path):
    store = make_store(tmp_path)
    store.put_source("zi_x", "source")
    assert store.get_source("ZI_X") == "source"


# ---------------------------------------------------------------------------
# Cache invalidation — the critical rule
# ---------------------------------------------------------------------------


def test_cached_verdicts_are_returned_for_matching_source(tmp_path):
    store = make_store(tmp_path)
    digest = store.put_source("ZI_X", "original source")
    store.put_verdicts(make_assessment(), digest)

    cached = store.cached_assessment("ZI_X", digest)
    assert cached is not None
    assert cached.results[0].rule_id == "R-01"
    assert cached.results[0].ref.line == 4
    assert cached.results[0].detail == {"k": "v"}


def test_cached_verdicts_are_refused_when_the_source_changed(tmp_path):
    store = make_store(tmp_path)
    old_digest = store.put_source("ZI_X", "original source")
    store.put_verdicts(make_assessment(), old_digest)

    new_digest = store.put_source("ZI_X", "the source has changed")
    assert new_digest != old_digest
    assert store.cached_assessment("ZI_X", new_digest) is None


def test_reporting_still_sees_verdicts_whose_source_moved_on(tmp_path):
    # The report describes what the last scan found. Dropping objects silently
    # would make the totals disagree with the list printed beside them.
    store = make_store(tmp_path)
    digest = store.put_source("ZI_X", "original")
    store.put_verdicts(make_assessment(), digest)
    store.put_source("ZI_X", "changed")

    assert store.cached_assessment("ZI_X", store.get_source_hash("ZI_X")) is None
    assert [a.object_name for a in store.all_assessments()] == ["ZI_X"]


def test_reverdicting_replaces_rather_than_appends(tmp_path):
    store = make_store(tmp_path)
    digest = store.put_source("ZI_X", "src")
    store.put_verdicts(make_assessment(rule="R-01"), digest)
    store.put_verdicts(make_assessment(rule="R-02"), digest)
    cached = store.cached_assessment("ZI_X", digest)
    assert [r.rule_id for r in cached.results] == ["R-02"]


# ---------------------------------------------------------------------------
# Views, tables, dependencies
# ---------------------------------------------------------------------------


def test_view_records_round_trip(tmp_path):
    store = make_store(tmp_path)
    store.put_view(
        ViewRecord(
            ddl_name="ZI_X",
            entity_type="VIEW_ENTITY",
            extraction_enabled=True,
            cdc_type="automatic",
            base_tables=["ZCUSTORDER", "T001"],
            verdict="PASS",
            bucket="READY",
        )
    )
    row = store.views()[0]
    assert row["ddl_name"] == "ZI_X"
    assert row["extraction_enabled"] == 1
    assert row["base_tables"] == ["T001", "ZCUSTORDER"]
    assert store.views(bucket="READY")
    assert store.views(bucket="FIXABLE") == []


def test_view_upsert_does_not_duplicate(tmp_path):
    store = make_store(tmp_path)
    store.put_view(ViewRecord(ddl_name="ZI_X", verdict="PASS"))
    store.put_view(ViewRecord(ddl_name="ZI_X", verdict="FAIL_HARD"))
    assert store.view_count() == 1
    assert store.views()[0]["verdict"] == "FAIL_HARD"


def test_dependencies_answer_where_used(tmp_path):
    store = make_store(tmp_path)
    store.put_dependencies("ZI_A", [("ZCUSTORDER", "TABLE", 1, True)])
    store.put_dependencies("ZI_B", [("ZCUSTORDER", "TABLE", 1, True)])
    assert store.dependents_of("ZCUSTORDER") == ["ZI_A", "ZI_B"]
    assert store.dependents_of("VBAK") == []


def test_bare_tables_are_those_with_no_view(tmp_path):
    store = make_store(tmp_path)
    store.put_table(table_name="ZCUSTORDER", has_view=1)
    store.put_table(table_name="ZLONELY", has_view=0)
    assert [t["table_name"] for t in store.tables(bare_only=True)] == ["ZLONELY"]
    assert len(store.tables()) == 2


def test_profiles_are_isolated_in_one_file(tmp_path):
    path = tmp_path / "shared.sqlite"
    dev = Store(path, profile_id="DEV")
    qas = Store(path, profile_id="QAS")
    dev.put_view(ViewRecord(ddl_name="ZI_ONLY_IN_DEV"))
    assert dev.view_count() == 1
    assert qas.view_count() == 0


# ---------------------------------------------------------------------------
# Snapshots and drift (F-35)
# ---------------------------------------------------------------------------


def test_snapshot_captures_the_inventory(tmp_path):
    store = make_store(tmp_path)
    store.put_view(ViewRecord(ddl_name="ZI_X", verdict="PASS", extraction_enabled=True))
    payload = store.take_snapshot("baseline")
    assert payload["ZI_X"]["verdict"] == "PASS"
    assert store.get_snapshot("baseline") == payload
    assert store.snapshots()[0]["view_count"] == 1


def test_drift_detects_additions_and_removals():
    before = {"ZI_A": {"verdict": "PASS"}, "ZI_GONE": {"verdict": "PASS"}}
    after = {"ZI_A": {"verdict": "PASS"}, "ZI_NEW": {"verdict": "FAIL_HARD"}}
    drift = compare_snapshots(before, after)
    assert drift.added == ["ZI_NEW"]
    assert drift.removed == ["ZI_GONE"]


def test_drift_flags_a_view_that_lost_extraction():
    # The classic post-upgrade regression, and the reason F-35 exists.
    before = {"ZI_A": {"extraction_enabled": 1, "verdict": "PASS"}}
    after = {"ZI_A": {"extraction_enabled": 0, "verdict": "FAIL_FIXABLE"}}
    drift = compare_snapshots(before, after)
    assert drift.lost_extraction == ["ZI_A"]
    assert drift.any
    assert "LOST EXTRACTION" in drift.render()


def test_no_drift_between_identical_snapshots():
    payload = {"ZI_A": {"verdict": "PASS", "extraction_enabled": 1}}
    drift = compare_snapshots(payload, dict(payload))
    assert not drift.any
    assert drift.render() == "No drift."


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_stats_counts_each_table(tmp_path):
    store = make_store(tmp_path)
    store.put_source("ZI_X", "src")
    store.put_view(ViewRecord(ddl_name="ZI_X"))
    assert store.stats()["views"] == 1
    assert store.stats()["view_sources"] == 1


def test_store_can_be_used_as_a_context_manager(tmp_path):
    with Store(tmp_path / "s.sqlite") as store:
        store.put_view(ViewRecord(ddl_name="ZI_X"))
    reopened = Store(tmp_path / "s.sqlite")
    assert reopened.view_count() == 1
