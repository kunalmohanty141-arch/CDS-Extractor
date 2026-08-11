"""F-09 — given a table, what exists and what can we build on?

Choosing between several standard views over one table is mostly elimination.
These tests protect the gates, because a candidate that passes them wrongly
sends someone to build on a foundation that cannot carry delta.
"""

from __future__ import annotations

from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import FieldMeta, TableClass, TableMeta
from cdcforge.model import Verdict
from cdcforge.successors import find_candidates


def by_name(report):
    return {c.view: c for c in report.candidates}


class InMemorySource(MetadataSource):
    """A metadata source built from literal DDL, for shape-specific tests."""

    def __init__(self, table: TableMeta, views: dict[str, str]):
        self._table = table
        self._views = {k.upper(): v for k, v in views.items()}

    def get_table(self, name: str) -> TableMeta | None:
        return self._table if name.upper() == self._table.name else None

    def get_view_source(self, name: str) -> str | None:
        return self._views.get(name.upper())

    def get_object(self, name: str):
        from cdcforge.metadata.types import ObjectMeta

        return ObjectMeta(name=name.upper()) if name.upper() in self._views else None

    def list_views(self) -> list[str]:
        return sorted(self._views)

    def views_reading_table(self, table: str) -> list[str] | None:
        return sorted(self._views)


# ---------------------------------------------------------------------------
# The basic question
# ---------------------------------------------------------------------------


def test_a_table_with_no_reader_says_build_directly(metadata):
    report = find_candidates(metadata, "ZLEGACY_POOL")
    assert report.candidates == []
    assert report.suggested is None
    assert "Build directly on the table" in report.recommendation


def test_an_unanswerable_where_used_is_not_reported_as_no_readers():
    """The failure mode of running this on a different release.

    `views_reading_table` returning None means the index could not answer. The
    fallback then scans `list_views()`, which is empty when set-based reads are
    unavailable — so zero readers came back looking exactly like "nothing reads
    this table", and the recommendation was "build directly on it". On a system
    where the indexes are simply not exposed, that advises building a second
    view beside the one that was already there.
    """

    class _Blind(InMemorySource):
        def list_views(self):
            return []

        def views_reading_table(self, table):
            return None

    source = _Blind(TableMeta(name="ZCUSTORDER"), {})
    report = find_candidates(source, "ZCUSTORDER")

    assert report.readers_unknown
    assert report.total_readers == 0
    assert "Cannot tell what reads" in report.recommendation
    assert "Build directly" not in report.recommendation
    assert "not the same as finding nothing" in report.recommendation


def test_a_real_search_that_finds_nothing_still_says_build(metadata):
    """The flag must not blunt the honest answer: a search that ran and found
    nothing is exactly when building is right."""
    report = find_candidates(metadata, "ZLEGACY_POOL")
    assert not report.readers_unknown
    assert "Build directly on the table" in report.recommendation


def test_the_action_and_the_recommendation_never_disagree(metadata):
    """One ladder, two renderings. They were two ladders and they drifted.

    EKET produced a row marked WRAP whose own Why said "no wrapper is needed",
    because the prose treated a CDC-carrying view as usable while the sheet
    demanded a fully ready one.
    """
    for table in ("ZCUSTORDER", "ZLEGACY_POOL", "ZORDERITEM"):
        report = find_candidates(metadata, table)
        action, prose = report.action, report.recommendation.lower()

        if action == "USE":
            assert prose.startswith("use "), prose
        elif action == "WRAP":
            assert "wrapper" in prose, prose
        elif action == "BUILD":
            assert "build directly" in prose, prose
        else:
            assert "cannot tell" in prose, prose


def test_readers_of_a_table_are_found(metadata):
    report = find_candidates(metadata, "ZCUSTORDER")
    assert "ZI_CUSTORDER" in by_name(report)


# ---------------------------------------------------------------------------
# Gate 3 — anything that blocks delta
# ---------------------------------------------------------------------------


def test_an_aggregating_view_is_excluded(metadata):
    """ZI_AGGREGATE declares extraction and CDC and has a GROUP BY.

    Annotations are not capability. Suggesting this as a wrapper base would
    send someone to build on something that cannot carry delta.
    """
    candidate = by_name(find_candidates(metadata, "ZCUSTORDER"))["ZI_AGGREGATE"]
    assert candidate.is_annotated
    assert not candidate.usable
    assert not candidate.is_ready
    assert "blocks delta" in candidate.excluded_because
    assert any("R-03" in b or "R-04" in b for b in candidate.blockers)


def test_a_union_view_is_excluded(metadata):
    candidate = by_name(find_candidates(metadata, "ZCUSTORDER")).get("ZI_UNION")
    if candidate is not None:
        assert not candidate.usable
        assert "blocks delta" in candidate.excluded_because


def test_a_distinct_view_is_excluded(metadata):
    candidate = by_name(find_candidates(metadata, "ZORDERITEM")).get("ZI_DISTINCT")
    if candidate is not None:
        assert not candidate.usable


def test_many_to_one_joins_do_not_disqualify(metadata):
    """Appendix A.3: join count is irrelevant, the shape is everything.

    Rejecting on count would discard most viable candidates.
    """
    report = find_candidates(metadata, "SNWD_SO")
    candidate = by_name(report).get("ZI_SALESORDER_CDC")
    assert candidate is not None
    assert candidate.join_count == 2
    assert candidate.usable, candidate.excluded_because


# ---------------------------------------------------------------------------
# Gate 2 — is the table the view's root?
# ---------------------------------------------------------------------------


def test_a_view_rooted_elsewhere_is_excluded(metadata):
    """ZI_SALESORDER_CDC is rooted on SNWD_SO and joins SNWD_SO_I.

    Its rows are one per sales order, not one per item — replicating it to get
    item data would duplicate every order field.
    """
    candidate = by_name(find_candidates(metadata, "SNWD_SO_I")).get("ZI_SALESORDER_CDC")
    assert candidate is not None
    assert not candidate.is_root
    assert not candidate.usable
    assert "not its root" in candidate.excluded_because


def test_the_root_view_itself_passes_the_root_gate(metadata):
    candidate = by_name(find_candidates(metadata, "SNWD_SO"))["ZI_SALESORDER_CDC"]
    assert candidate.is_root


# ---------------------------------------------------------------------------
# Gate 1 — key exposure
# ---------------------------------------------------------------------------


def test_a_view_hiding_the_key_cannot_be_a_wrapper_base(metadata):
    """A CDC mapping addresses base tables, so the wrapper must expose the
    table's key. Without it there is no mapping to write."""
    report = find_candidates(metadata, "ZORDERITEM")
    hiding = by_name(report).get("ZI_MISSING_MAIN_KEY")
    if hiding is not None:
        assert not hiding.exposes_key
        assert not hiding.usable
        assert "full key" in hiding.excluded_because


def test_field_coverage_is_measured_against_the_table(metadata):
    candidate = by_name(find_candidates(metadata, "ZCUSTORDER"))["ZI_CUSTORDER"]
    assert candidate.table_fields == 5  # MANDT excluded
    assert candidate.exposed_fields >= 4
    assert 0 < candidate.coverage <= 1
    assert "table fields" in candidate.detail


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_a_ready_view_is_suggested_over_a_wrappable_one(metadata):
    report = find_candidates(metadata, "ZCUSTORDER")
    assert report.suggested is not None
    assert report.suggested.is_ready
    assert report.suggested.view == report.candidates[0].view
    assert "every rule passes" in report.recommendation


def test_broken_views_never_reach_the_suggestion(metadata):
    report = find_candidates(metadata, "ZCUSTORDER")
    assert report.suggested.view != "ZI_AGGREGATE"
    assert all(c.usable for c in report.ready)
    assert all(c.usable for c in report.wrappable)


def test_excluded_candidates_are_kept_with_their_reason(metadata):
    """A consultant learns more from seeing why three were rejected than from
    being handed one name."""
    report = find_candidates(metadata, "ZCUSTORDER")
    assert report.excluded
    assert all(c.excluded_because for c in report.excluded)


def test_coverage_outranks_stack_depth():
    """Real content settled this the other way round than I first had it.

    MARA has ready views exposing 4% of its columns at depth 1. Ranking depth
    first suggested one of those over a view carrying half the table. One extra
    level of stack is a modest CDC risk; replicating 15 of 367 columns is the
    wrong data.
    """
    from cdcforge.successors import Candidate

    wide_deep = Candidate(
        view="I_WIDE", vdm="BASIC", exposed_fields=150, table_fields=300,
        stack_depth=3, is_root=True, exposes_key=True, has_key_elements=True,
    )
    thin_shallow = Candidate(
        view="I_THIN", vdm="BASIC", exposed_fields=8, table_fields=300,
        stack_depth=1, is_root=True, exposes_key=True, has_key_elements=True,
    )
    assert sorted([thin_shallow, wide_deep], key=lambda c: c.rank_key)[0].view == "I_WIDE"


def test_a_thin_candidate_is_recognised():
    from cdcforge.successors import Candidate

    assert Candidate(view="X", exposed_fields=4, table_fields=367).thin
    assert not Candidate(view="X", exposed_fields=150, table_fields=300).thin


def test_when_every_candidate_is_thin_the_table_is_preferred():
    """MARA's case: five ready views, none carrying meaningful data.

    Reusing one is not reuse — it is replicating the wrong thing.
    """
    from cdcforge.successors import Candidate, SuccessorReport

    report = SuccessorReport(table="MARA")
    report.candidates = [
        Candidate(
            view=f"I_THIN{i}", vdm="BASIC", exposed_fields=5, table_fields=367,
            is_root=True, exposes_key=True, has_key_elements=True,
            extraction_enabled=True, delta_method="CDC", verdict=Verdict.PASS,
        )
        for i in range(3)
    ]
    assert report.ready
    assert report.prefer_table
    assert "Build directly on MARA" in report.recommendation
    assert "not the table" in report.recommendation


def test_a_wide_candidate_is_still_recommended():
    from cdcforge.successors import Candidate, SuccessorReport

    report = SuccessorReport(table="LIKP")
    report.candidates = [
        Candidate(
            view="I_DELIVERYDOCUMENT", vdm="BASIC", exposed_fields=152,
            table_fields=312, is_root=True, exposes_key=True,
            has_key_elements=True, extraction_enabled=True, delta_method="CDC",
            verdict=Verdict.PASS,
        )
    ]
    assert not report.prefer_table
    assert "Use I_DELIVERYDOCUMENT" in report.recommendation


def test_partial_examination_is_admitted():
    """'None of these can carry delta' and 'none of the ones I looked at can'
    are different claims."""
    from cdcforge.successors import SuccessorReport

    budget = SuccessorReport(
        table="EKKO", total_readers=148, prescreened=60, examined=15
    )
    assert budget.partially_examined
    assert "148 view(s) read EKKO" in budget.coverage_note
    assert "60 were read and screened" in budget.coverage_note

    complete = SuccessorReport(
        table="ZTAB", total_readers=3, prescreened=3, examined=3
    )
    assert not complete.partially_examined
    assert complete.coverage_note == ""


def test_screening_every_reader_is_not_partial_examination():
    """A view rejected by the prescreen was judged on facts from its own DDL.

    Only the ones never read, or read but never fully validated, leave the
    answer incomplete — and those are separate sentences because they are
    different admissions.
    """
    from cdcforge.successors import SuccessorReport

    full = SuccessorReport(
        table="EKPO", total_readers=149, prescreened=149, examined=24, deferred=0
    )
    assert not full.partially_examined
    assert full.coverage_note == ""

    deferred = SuccessorReport(
        table="EKPO", total_readers=149, prescreened=149, examined=24, deferred=6
    )
    assert deferred.partially_examined
    assert "were read and screened" in deferred.coverage_note
    assert "6 were not fully validated" in deferred.coverage_note


def _wide_table(name: str = "ZORDERITEM", columns: int = 40) -> TableMeta:
    fields = [
        FieldMeta(name="MANDT", position=1, is_key=True, data_type="CLNT"),
        FieldMeta(name="DOCNO", position=2, is_key=True, data_type="CHAR"),
        FieldMeta(name="ITEMNO", position=3, is_key=True, data_type="NUMC"),
    ]
    fields += [
        FieldMeta(name=f"ATTR{i:02d}", position=i + 3, data_type="CHAR")
        for i in range(1, columns - 2)
    ]
    # Transparent and delivery class A, or R-21 is rightly INCONCLUSIVE about
    # the table class and no candidate can ever reach a PASS verdict.
    return TableMeta(
        name=name,
        table_class=TableClass.TRANSPARENT,
        delivery_class="A",
        fields=fields,
    )


def test_a_good_view_is_not_lost_to_a_shorter_name():
    """The EKPO regression, in miniature.

    F-09 used to rank candidates by name *before* reading any of them, and one
    of the tie-breakers was name length. On EKPO that put I_ARUNSTOITEM in the
    examined set and left I_PURCHASINGDOCUMENTITEM — which carries 41% of the
    table — out of it entirely, so the tool advised building a custom view over
    a table that already had a good standard one.

    Any pre-ranking on names has the same failure mode, so the guarantee is
    stronger than "rank better": every reader gets read.
    """
    table = _wide_table()
    wide_columns = ",\n  ".join(f"t.ATTR{i:02d}" for i in range(1, 31))
    source = InMemorySource(
        table,
        {
            # Short name, real content, and useless: an inner join blocks delta.
            "I_ZSHORT": """
                define view entity I_ZShort as select from ZORDERITEM as t
                  inner join ZOTHER as o on o.DOCNO = t.DOCNO
                { key t.DOCNO, key t.ITEMNO, t.ATTR01 }
            """,
            # Long name, and the one a human would name.
            "I_ZORDERITEMWITHAVERYLONGNAME": f"""
                define view entity I_ZOrderItemWithAVeryLongName
                  as select from ZORDERITEM as t
                {{ key t.DOCNO, key t.ITEMNO,
                  {wide_columns} }}
            """,
        },
    )

    report = find_candidates(source, "ZORDERITEM")

    assert report.prescreened == report.total_readers, (
        "every view reading the table must be read, not pre-filtered by name"
    )
    assert not report.partially_examined
    assert report.suggested is not None
    assert report.suggested.view == "I_ZORDERITEMWITHAVERYLONGNAME"
    assert report.suggested.coverage > 0.5


def test_a_cdc_enabled_view_outranks_a_wider_one_that_needs_a_wrapper():
    """Zero work beats more columns, and the coverage is stated either way.

    A view SAP already ships with working extraction and CDC delta needs no
    wrapper, no transport and no maintenance. Whether the columns it carries
    are the ones you need is a question about the requirement, not the table,
    so the tool ranks the zero-work option first and puts the coverage in front
    of the user rather than deciding for them.
    """
    table = _wide_table()
    wide_columns = ",\n  ".join(f"t.ATTR{i:02d}" for i in range(1, 31))
    source = InMemorySource(
        table,
        {
            "I_ZREADY": """
                @Analytics.dataExtraction.enabled: true
                @Analytics.dataExtraction.delta.changeDataCapture.automatic: true
                define view entity I_ZReady as select from ZORDERITEM as t
                { key t.DOCNO, key t.ITEMNO, t.ATTR01 }
            """,
            "I_ZWIDE": f"""
                define view entity I_ZWide as select from ZORDERITEM as t
                {{ key t.DOCNO, key t.ITEMNO,
                  {wide_columns} }}
            """,
        },
    )

    report = find_candidates(source, "ZORDERITEM")
    assert report.suggested is not None
    assert report.suggested.view == "I_ZREADY"
    assert report.suggested.is_ready

    # ...and the wider option is still offered, not hidden behind the winner.
    assert {c.view for c in report.usable} == {"I_ZREADY", "I_ZWIDE"}
    assert report.has_ready
    wide = by_name(report)["I_ZWIDE"]
    assert wide.usable
    assert wide.coverage > report.suggested.coverage


def _candidate(name: str, ready: bool, exposed: int, table_fields: int = 100):
    from cdcforge.successors import Candidate

    return Candidate(
        view=name,
        is_root=True,
        exposes_key=True,
        has_key_elements=True,
        extraction_enabled=ready,
        delta_method="CDC" if ready else "none",
        verdict=Verdict.PASS,
        exposed_fields=exposed,
        table_fields=table_fields,
    )


def test_thin_candidates_go_to_the_overflow_not_the_main_list():
    """The cut is on substance, not on position.

    A flat top-N padded the list with views carrying 7% of the table that
    nobody would pick. They stay reachable — just not in the way.
    """
    from cdcforge.successors import SuccessorReport

    report = SuccessorReport(
        table="ZT",
        candidates=[
            _candidate("ZWIDE", ready=False, exposed=60),
            *[_candidate(f"ZTHIN{i}", ready=False, exposed=7) for i in range(8)],
        ],
    )
    shown = {c.view for c in report.choices()}
    assert shown == {"ZWIDE"}
    assert len(report.usable) == 9, "the thin ones are omitted, not discarded"


def test_every_cdc_carrying_view_is_shown_even_past_the_limit():
    """The VBAP case. Twelve views already carry CDC delta.

    They are the zero-work answers, so they are the answer set rather than
    entries competing for room in it. A flat cap of ten cut
    C_SalesDocumentItemDEX and its sibling off the bottom of the list.
    """
    from cdcforge.successors import SuccessorReport

    cdc = []
    for i in range(12):
        c = _candidate(f"C_DEX{i:02d}", ready=False, exposed=20 + i)
        c.extraction_enabled = True
        c.delta_method = "CDC"
        cdc.append(c)
    plain = _candidate("I_WIDE", ready=False, exposed=49)

    report = SuccessorReport(table="VBAP", candidates=[*cdc, plain])
    assert all(c.carries_cdc for c in cdc)

    shown = [c.view for c in report.choices()]
    assert len([v for v in shown if v.startswith("C_DEX")]) == 12
    assert "I_WIDE" in shown, "the widest unfiltered candidate is still guaranteed"


def test_a_view_carrying_every_row_is_always_shown():
    """The VBAP case. Nine ready views, each one document category.

    They are all correct for CDC and none of them is VBAP. Coverage counts
    columns, so nothing in the numbers says so — and left to the ordinary
    ranking they filled the list and pushed out I_SalesDocumentItem, which
    carries every row and 49% of the columns.
    """
    from cdcforge.successors import SuccessorReport

    filtered = []
    for i in range(9):
        c = _candidate(f"I_DOCTYPE{i}", ready=True, exposed=30)
        c.row_filtered = True
        filtered.append(c)
    whole_table = _candidate("I_SALESDOCUMENTITEM", ready=False, exposed=49)

    report = SuccessorReport(table="VBAP", candidates=[*filtered, whole_table])
    shown = {c.view for c in report.choices()}
    assert "I_SALESDOCUMENTITEM" in shown, (
        "the best unfiltered candidate must survive a list full of ready ones"
    )


def test_carrying_every_row_outranks_a_filtered_view_of_equal_standing():
    filtered = _candidate("I_SALESORDERITEM", ready=True, exposed=41)
    filtered.row_filtered = True
    whole = _candidate("VC_INTEGRATION_VBAP", ready=True, exposed=41)
    assert sorted([filtered, whole], key=lambda c: c.rank_key)[0] is whole


def test_a_ready_view_is_shown_however_thin_it_is():
    """Zero work is worth showing even at 1%.

    It may carry exactly the columns the user needs, and only they can say.
    """
    from cdcforge.successors import SuccessorReport

    report = SuccessorReport(
        table="ZT",
        candidates=[
            _candidate("ZREADY_TINY", ready=True, exposed=1),
            _candidate("ZWIDE", ready=False, exposed=60),
        ],
    )
    shown = [c.view for c in report.choices()]
    assert shown[0] == "ZREADY_TINY", "zero-work option still leads"
    assert "ZWIDE" in shown


def test_extraction_without_cdc_does_not_outrank_coverage():
    """The VBAP case. Extraction alone is not a head start.

    A view with @Analytics.dataExtraction.enabled but no CDC delta needs
    exactly the same wrapper as one with no annotations at all, so ranking it
    above a much wider view claims a saving that does not exist — it put a 1%
    view above I_SalesDocumentItemBasic at 43%. Only CDC delta earns a tier.
    """
    from cdcforge.successors import Candidate

    def make(name: str, exposed: int, extraction: bool) -> Candidate:
        return Candidate(
            view=name,
            is_root=True,
            exposes_key=True,
            has_key_elements=True,
            extraction_enabled=extraction,
            delta_method="none",
            verdict=Verdict.PASS,
            exposed_fields=exposed,
            table_fields=446,
        )

    thin_enabled = make("ZCDS_ABH_VBAK", exposed=5, extraction=True)
    wide_plain = make("I_SALESDOCUMENTITEMBASIC", exposed=190, extraction=False)

    assert sorted([thin_enabled, wide_plain], key=lambda c: c.rank_key)[0] is (
        wide_plain
    )

    # ...but CDC delta still wins outright, even at a fraction of the coverage.
    ready = make("ZREADY", exposed=5, extraction=True)
    ready.delta_method = "CDC"
    assert ready.is_ready
    assert sorted([ready, wide_plain], key=lambda c: c.rank_key)[0] is ready


def test_the_widest_view_is_always_shown():
    """The VBAK case: three thin enabled views must not hide a 96% one.

    A short list is a convenience. Hiding the candidate that carries most of
    the table is how someone builds on 8% of VBAK and finds ZDDL_VBAK
    afterwards — the exact outcome this search exists to prevent.
    """
    from cdcforge.successors import Candidate, SuccessorReport

    def make(name: str, ready: bool, exposed: int) -> Candidate:
        return Candidate(
            view=name,
            is_root=True,
            exposes_key=True,
            has_key_elements=True,
            extraction_enabled=ready,
            delta_method="CDC" if ready else "none",
            verdict=Verdict.PASS,
            exposed_fields=exposed,
            table_fields=100,
        )

    report = SuccessorReport(
        table="VBAK",
        candidates=[
            make("VC_INTEGRATION_VBAK", ready=True, exposed=84),
            make("ZCDS_VBAK", ready=True, exposed=8),
            make("ZTHIN_READY", ready=True, exposed=5),
            make("ZDDL_VBAK", ready=False, exposed=96),
        ],
    )

    shown = report.choices()
    assert shown[0].view == "VC_INTEGRATION_VBAK", "zero-work option still leads"
    assert "ZDDL_VBAK" in {c.view for c in shown}, (
        "the widest candidate must always be shown"
    )
    assert "ZTHIN_READY" in {c.view for c in shown}, (
        "a ready view is shown however thin — it costs no work"
    )


def test_basic_layer_outranks_consumption():
    from cdcforge.successors import VDM_RANK

    assert VDM_RANK["BASIC"] < VDM_RANK["COMPOSITE"] < VDM_RANK["CONSUMPTION"]
    assert VDM_RANK["PRIVATE"] > VDM_RANK["CONSUMPTION"]


def test_vdm_layer_falls_back_to_the_name_prefix():
    from cdcforge.cds import is_private_layer, vdm_layer

    assert vdm_layer(None, "I_PurchaseOrder") == "BASIC"
    assert vdm_layer(None, "C_PurchaseOrderFS") == "CONSUMPTION"
    assert vdm_layer(None, "P_PurchaseOrderBasic") == "PRIVATE"
    assert is_private_layer(None, "P_Something")
    assert not is_private_layer(None, "I_Something")


def test_a_private_view_is_never_suggested(metadata):
    from cdcforge.successors import Candidate

    private = Candidate(view="P_INTERNAL", private=True, is_root=True, exposes_key=True)
    assert not private.usable
    assert "private view" in private.excluded_because


# ---------------------------------------------------------------------------
# Indexed sources
# ---------------------------------------------------------------------------


def test_a_dependency_index_is_used_instead_of_scanning(metadata):
    """Scanning is fine over fixtures and hopeless over 7,000 views."""

    class Indexed:
        def __init__(self, inner):
            self.inner = inner
            self.asked: list[str] = []

        def views_reading_table(self, table):
            self.asked.append(table)
            return ["ZI_CUSTORDER"]

        def __getattr__(self, item):
            return getattr(self.inner, item)

    indexed = Indexed(metadata)
    report = find_candidates(indexed, "ZCUSTORDER")
    assert indexed.asked == ["ZCUSTORDER"]
    assert [c.view for c in report.candidates] == ["ZI_CUSTORDER"]
    assert report.searched == 0


def test_verdicts_are_recorded_for_every_actionable_candidate(metadata):
    """Anything the user could act on has been through the full rule set.

    Candidates thrown out by the cheap prescreen carry a verdict only when the
    local run was conclusive — a HARD violation in the view's own DDL. One
    rejected for its root or its layer keeps ``None``, because recording "the
    parts I checked passed" as PASS is the false PASS the tool exists to
    prevent.
    """
    report = find_candidates(metadata, "ZCUSTORDER")
    assert report.usable, "fixture should offer at least one usable base"
    assert all(c.verdict is not None for c in report.usable)
    assert any(c.verdict is Verdict.PASS for c in report.candidates)
    assert all(
        c.verdict is not Verdict.PASS for c in report.candidates if not c.usable
    )
