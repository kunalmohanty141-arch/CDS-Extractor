"""The decision sheet round trip.

The tests that matter are the ones about a spreadsheet coming back changed —
edited by someone who was not there when it was generated, in Excel, with a
column inserted and a name typed by hand. Everything this module does is easy;
everything that goes wrong with it is the file.
"""

from __future__ import annotations

import pytest

from cdcforge.decisions import (
    BUILD,
    SKIP,
    USE,
    WRAP,
    Decision,
    PlanError,
    _default_target,
    load,
    read_plan,
    suggest,
    validate,
    write_plan,
)

openpyxl = pytest.importorskip("openpyxl")


# ---------------------------------------------------------------------------
# Validation — a sheet is judged whole, before any of it runs
# ---------------------------------------------------------------------------


def test_a_wrap_without_a_base_is_refused():
    problems = validate([Decision("VBAP", action=WRAP, row=2)])
    assert problems and "needs a view name in Base" in problems[0]


def test_a_use_without_a_base_is_refused():
    problems = validate([Decision("VBAP", action=USE, row=2)])
    assert problems and "needs a view name in Base" in problems[0]


def test_a_build_with_a_base_says_the_base_would_be_ignored():
    """Silently dropping it would build the right object for the wrong reason,
    and the user would never learn their sheet said something impossible."""
    problems = validate(
        [Decision("VBAP", action=BUILD, base="I_SALESDOCUMENTITEM", row=2)]
    )
    assert problems and "would be ignored" in problems[0]


def test_an_unknown_action_names_the_row_and_the_choices():
    problems = validate([Decision("VBAP", action="MAKE", row=7)])
    assert problems
    assert "row 7" in problems[0]
    assert "USE, WRAP, BUILD, SKIP" in problems[0]


def test_an_unknown_action_does_not_also_complain_about_a_missing_base():
    """One mistake, one message. Nothing below the action can be judged until
    the action is known, and three complaints about one typo is noise."""
    problems = validate([Decision("VBAP", action="MAKE", row=7)])
    assert len(problems) == 1


@pytest.mark.parametrize("target", ["I_SALESDOC", "MARA", "1ZED", "SD_THING"])
def test_a_generated_target_must_be_customer_namespace(target):
    problems = validate([Decision("VBAP", action=BUILD, target=target, row=2)])
    assert problems and "customer-namespace" in problems[0]


def test_a_use_row_needs_no_target_name():
    """USE generates nothing, so there is nothing to name."""
    assert validate([Decision("VBAP", action=USE, base="I_X", row=2)]) == []


def test_skip_rows_are_never_a_problem():
    assert validate([Decision("VBAP", action=SKIP, row=2)]) == []
    assert validate([Decision("VBAP", action=SKIP, base="ANYTHING", row=2)]) == []


def test_two_rows_generating_the_same_name_is_caught():
    """Neither party spots this one unaided.

    The second create fails with "already exists" and reads like a stale
    object from a previous run, so the user deletes something they should not.
    """
    problems = validate(
        [
            Decision("VBAP", action=BUILD, target="ZI_ITEM", row=2),
            Decision("EKPO", action=BUILD, target="ZI_ITEM", row=3),
        ]
    )
    assert problems
    assert "already used by VBAP" in problems[0]
    assert "row 2" in problems[0]


def test_the_same_name_on_a_skipped_row_does_not_collide():
    assert (
        validate(
            [
                Decision("VBAP", action=BUILD, target="ZI_ITEM", row=2),
                Decision("EKPO", action=SKIP, target="ZI_ITEM", row=3),
            ]
        )
        == []
    )


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def _sample() -> list[Decision]:
    return [
        Decision(
            "VBAP", kind="TABLE", action=WRAP, base="I_SALESDOCUMENTITEM",
            target="ZW_SALESDOCUMENTITEM", note="finance only",
            suggested_action=WRAP, suggested_base="I_SALESDOCUMENTITEM",
            coverage="41%", why="wrap the widest released view",
            candidates="I_SALESDOCUMENTITEM 41% | C_SALESDOCUMENTITEMDEX 38%*",
        ),
        Decision(
            "EKPO", kind="TABLE", action=USE, base="I_PURCHASEORDERITEM",
            suggested_action=USE, suggested_base="I_PURCHASEORDERITEM",
            coverage="63%",
        ),
        Decision("KNA1", kind="TABLE", action=BUILD, target="ZI_KNA1",
                 suggested_action=BUILD),
    ]


def test_a_sheet_survives_the_round_trip(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    back = read_plan(path)

    assert [d.object_name for d in back] == ["VBAP", "EKPO", "KNA1"]
    assert [d.action for d in back] == [WRAP, USE, BUILD]
    assert back[0].base == "I_SALESDOCUMENTITEM"
    assert back[0].target == "ZW_SALESDOCUMENTITEM"
    assert back[0].note == "finance only"
    assert back[0].candidates.startswith("I_SALESDOCUMENTITEM 41%")
    assert validate(back) == []


def test_rows_carry_their_sheet_line_number(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    back = read_plan(path)
    assert [d.row for d in back] == [2, 3, 4]


def test_columns_are_found_by_name_not_position(tmp_path):
    """The first thing anyone does to a spreadsheet is insert a column.

    Reading by position would shift every value one place and build the wrong
    objects without a word.
    """
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["Decisions"]
    sheet.insert_cols(1)
    sheet["A1"] = "Priority"
    sheet["A2"] = "high"
    workbook.save(path)

    back = read_plan(path)
    assert back[0].object_name == "VBAP"
    assert back[0].action == WRAP
    assert back[0].base == "I_SALESDOCUMENTITEM"


def test_a_reordered_sheet_still_reads(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["Decisions"]
    sheet.move_range("C1:C4", cols=8)  # Action off to the right
    workbook.save(path)

    assert [d.action for d in read_plan(path)] == [WRAP, USE, BUILD]


def test_hand_typed_values_are_normalised(tmp_path):
    """Someone will type lowercase, and someone will leave a trailing space."""
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["Decisions"]
    sheet["C2"] = "  wrap "
    sheet["D2"] = "i_salesdocumentitem"
    sheet["E2"] = "zw_thing "
    workbook.save(path)

    back = read_plan(path)
    assert back[0].action == WRAP
    assert back[0].base == "I_SALESDOCUMENTITEM"
    assert back[0].target == "ZW_THING"


def test_blank_rows_are_skipped(tmp_path):
    """Deleting a row's contents rather than the row is normal, and must not
    produce a nameless object."""
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["Decisions"]
    for column in "ABCDEFGHIJK":
        sheet[f"{column}3"] = None
    workbook.save(path)

    back = read_plan(path)
    assert [d.object_name for d in back] == ["VBAP", "KNA1"]


def test_a_missing_action_is_a_skip_not_a_guess(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    workbook["Decisions"]["C2"] = None
    workbook.save(path)

    back = read_plan(path)
    assert back[0].action == SKIP
    assert validate(back) == []


def test_a_workbook_that_is_not_a_decision_sheet_says_so(tmp_path):
    workbook = openpyxl.Workbook()
    workbook.active.append(["Table", "Owner"])
    workbook.active.append(["VBAP", "someone"])
    path = tmp_path / "notaplan.xlsx"
    workbook.save(path)

    with pytest.raises(PlanError, match="does not look like a decision sheet"):
        read_plan(path)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(PlanError, match="does not exist"):
        read_plan(tmp_path / "nope.xlsx")


def test_the_decisions_sheet_is_found_by_name_among_others(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    workbook.create_sheet("Notes", 0).append(["ignore me"])
    workbook.save(path)

    assert [d.object_name for d in read_plan(path)] == ["VBAP", "EKPO", "KNA1"]


def test_it_reads_from_bytes_for_the_ui(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    assert len(read_plan(path.read_bytes())) == 3


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


class _FakeCandidate:
    def __init__(
        self, view, coverage=0.5, ready=False, cdc=False,
        filtered=False, api_state="C1",
    ):
        self.view = view
        self.coverage = coverage
        self.is_ready = ready
        self.carries_cdc = cdc
        self.row_filtered = filtered
        self.api_state = api_state


class _FakeReport:
    def __init__(self, best=None, choices=(), prefer_table=False):
        self.suggested = best
        self._choices = list(choices)
        self.prefer_table = prefer_table
        self.readers_unknown = False
        self.from_delta_index: list[str] = []
        self.recommendation = "because reasons"

    @property
    def action(self):
        """Mirrors SuccessorReport.action, which is the single decider."""
        if self.readers_unknown:
            return "UNKNOWN"
        if self.prefer_table or self.suggested is None:
            return BUILD
        if self.suggested.is_ready or self.suggested.carries_cdc:
            return USE
        return WRAP

    def choices(self, limit=10):
        return self._choices


def test_a_view_that_already_carries_delta_is_used_not_wrapped():
    """Generating over it would be work for its own sake, and a second object
    to keep in step with the first."""
    best = _FakeCandidate("C_SALESDOCUMENTITEMDEX", 0.38, ready=True, cdc=True)
    decision = suggest("VBAP", _FakeReport(best, [best]))

    assert decision.action == USE
    assert decision.base == "C_SALESDOCUMENTITEMDEX"
    assert decision.target == "", "USE generates nothing, so it names nothing"


def test_a_cdc_view_with_open_findings_is_used_not_wrapped():
    """The row must not contradict its own explanation.

    Measured on EKET: the sheet said WRAP over C_PurOrdScheduleLineDEX while
    the Why column read "it already declares extraction and CDC delta ... so no
    wrapper is needed". The ladder was encoded twice — the prose needed only a
    CDC-carrying view, the sheet demanded a *ready* one — and they drifted.

    The prose was right. A wrapper inherits the base's findings, the stack
    depth and the trigger load, and fixes none of them.
    """
    best = _FakeCandidate("C_PURORDSCHEDULELINEDEX", 0.36, cdc=True, filtered=True)
    decision = suggest("EKET", _FakeReport(best, [best]))

    assert decision.action == USE
    assert decision.base == "C_PURORDSCHEDULELINEDEX"
    assert decision.target == "", "USE generates nothing, so it names nothing"


def test_a_usable_view_that_lacks_delta_is_wrapped():
    best = _FakeCandidate("I_SALESDOCUMENTITEM", 0.41)
    decision = suggest("VBAP", _FakeReport(best, [best]))

    assert decision.action == WRAP
    assert decision.base == "I_SALESDOCUMENTITEM"
    assert decision.target == "ZW_SALESDOCUMENTITEM"


def test_an_unanswerable_search_is_not_read_as_nothing_found():
    """"No view reads this table" and "nobody could be asked" are different
    sentences, and only one of them justifies building.

    On a release whose where-used indexes the tool cannot read, the second was
    silently becoming the first — advising a new view beside the perfectly good
    one that was already there.
    """
    report = _FakeReport(None)
    report.readers_unknown = True
    decision = suggest("VBAP", report)

    assert decision.action == SKIP
    assert decision.target == "", "nothing may be named on a guess"
    assert "candidate search unavailable" in decision.note


def test_a_table_the_generator_would_refuse_is_not_proposed_for_building():
    """Found by running twenty unseen tables.

    BSID and BSAK came back BUILD, and `apply` then refused both — they are
    S/4HANA compatibility views over ACDOCA, not tables, and CDC needs a
    database trigger on something real. The tool knew that at planning time and
    said it two steps and one wait later. Not rare either: BSID, BSAK, BSIK,
    BSAD, BSIS and BSAS are all views in S/4.
    """
    from cdcforge.metadata.types import TableClass, TableMeta

    compatibility_view = TableMeta(name="BSID", table_class=TableClass.VIEW)
    decision = suggest("BSID", _FakeReport(None), table=compatibility_view)

    assert decision.action == SKIP
    assert decision.target == "", "nothing is named for something unbuildable"
    assert "database triggers" in decision.why
    assert "cannot be built on" in decision.note


def test_a_real_table_is_still_proposed_for_building():
    """The gate must not blunt the honest answer."""
    from cdcforge.metadata.types import FieldMeta, TableClass, TableMeta

    real = TableMeta(
        name="VBKD",
        table_class=TableClass.TRANSPARENT,
        fields=[FieldMeta(name="VBELN", is_key=True), FieldMeta(name="BSTKD")],
    )
    decision = suggest("VBKD", _FakeReport(None), table=real)

    assert decision.action == BUILD
    assert decision.target == "ZI_VBKD"


def test_without_a_table_nothing_is_claimed():
    """`None` means the caller did not look, which is evidence of nothing."""
    decision = suggest("KNA1", _FakeReport(None))
    assert decision.action == BUILD


def test_no_usable_view_means_build_on_the_table():
    decision = suggest("KNA1", _FakeReport(None))
    assert decision.action == BUILD
    assert decision.target == "ZI_KNA1"
    assert decision.base == ""


def test_prefer_table_overrides_a_candidate():
    """F-09 says prefer the table when every view is row-filtered. A wrapper
    over one of them would replicate a slice and look complete."""
    best = _FakeCandidate("I_SALESORDERITEM", 0.41)
    decision = suggest("VBAP", _FakeReport(best, [best], prefer_table=True))
    assert decision.action == BUILD


def test_the_suggestion_is_recorded_separately_from_the_decision():
    """So a returned sheet still shows what the tool thought, and `changed`
    can find the rows where someone disagreed."""
    best = _FakeCandidate("I_X", 0.4)
    decision = suggest("VBAP", _FakeReport(best, [best]))
    assert decision.suggested_action == decision.action
    assert decision.suggested_base == decision.base


def test_candidates_are_listed_with_coverage_and_a_cdc_marker():
    choices = [
        _FakeCandidate("C_ITEMDEX", 0.38, cdc=True),
        _FakeCandidate("I_ITEM", 0.41),
    ]
    decision = suggest("VBAP", _FakeReport(choices[0], choices))
    assert "C_ITEMDEX 38%*" in decision.candidates
    assert "I_ITEM 41%" in decision.candidates


def test_a_row_filtered_candidate_is_marked_as_such():
    """Measured on EKPO: FNDEI_EKPO_FILTER shows 85% coverage against the
    chosen R_PURCHASINGDOCUMENTITEM's 54%, and is the worse answer because it
    is filtered. Printing coverage alone argues for the wrong choice.
    """
    choices = [
        _FakeCandidate("R_PURCHASINGDOCUMENTITEM", 0.54),
        _FakeCandidate("FNDEI_EKPO_FILTER", 0.85, filtered=True),
    ]
    decision = suggest("EKPO", _FakeReport(choices[0], choices))
    assert "FNDEI_EKPO_FILTER 85%~" in decision.candidates
    assert "R_PURCHASINGDOCUMENTITEM 54%" in decision.candidates
    assert "54%~" not in decision.candidates


def test_a_candidate_that_is_both_cdc_and_filtered_carries_both_marks():
    choice = _FakeCandidate("I_SALESORDERITEM", 0.41, cdc=True, filtered=True)
    decision = suggest("VBAP", _FakeReport(choice, [choice]))
    assert "I_SALESORDERITEM 41%*~" in decision.candidates


def test_an_unreleased_base_carries_the_caveat():
    """VC_INTEGRATION_VBAP is the right answer and is still not a released API.

    Both halves belong in the sheet, because the sheet is what gets mailed to
    someone who was not in the conversation.
    """
    best = _FakeCandidate(
        "VC_INTEGRATION_VBAP", 0.78, ready=True, cdc=True, api_state="NOT_RELEASED"
    )
    decision = suggest("VBAP", _FakeReport(best, [best]))

    assert decision.action == USE
    assert "not a released API" in decision.why
    assert "after every upgrade" in decision.why


@pytest.mark.parametrize("state", ["C1", "RELEASED"])
def test_a_released_base_gets_no_caveat(state):
    best = _FakeCandidate("I_ACCOUNTINGDOCUMENT", 0.26, api_state=state)
    decision = suggest("BKPF", _FakeReport(best, [best]))
    assert "not a released API" not in decision.why


def test_an_unreadable_release_state_is_treated_as_unreleased():
    """Unknown is never the optimistic reading."""
    best = _FakeCandidate("SOME_VIEW", 0.6, api_state="")
    decision = suggest("VBAP", _FakeReport(best, [best]))
    assert "treat it as unreleased" in decision.why


def test_an_object_already_built_over_the_same_base_suggests_skip():
    """The gap this closes, in one test.

    Planning BKPF suggested a wrapper over I_AccountingDocument while
    ZW_ACCTGDOC already existed over exactly that view. A second run over the
    same list otherwise rebuilds everything the first run built.
    """
    from cdcforge.estate import Estate, ExistingObject

    estate = Estate(
        objects=[
            ExistingObject(
                "ZW_ACCTGDOC", base="I_ACCOUNTINGDOCUMENT",
                root_table="BKPF", declares_cdc=True,
            )
        ],
        surveyed=1,
    )
    best = _FakeCandidate("I_ACCOUNTINGDOCUMENT", 0.26)
    decision = suggest("BKPF", _FakeReport(best, [best]), estate=estate)

    assert decision.action == SKIP
    assert "ZW_ACCTGDOC" in decision.existing
    assert decision.existing.startswith("ALREADY BUILT:")
    assert "already built" in decision.note


def test_an_existing_object_over_a_different_base_still_suggests_building():
    """Two wrappers over different views of one table are not duplicates, so
    the note informs rather than overriding."""
    from cdcforge.estate import Estate, ExistingObject

    estate = Estate(
        objects=[
            ExistingObject("ZW_OTHER", base="I_SOMETHINGELSE", root_table="BKPF")
        ],
        surveyed=1,
    )
    best = _FakeCandidate("I_ACCOUNTINGDOCUMENT", 0.26)
    decision = suggest("BKPF", _FakeReport(best, [best]), estate=estate)

    assert decision.action == WRAP
    assert "possibly not a duplicate" in decision.existing


def test_the_skip_is_recorded_as_the_suggestion_so_a_rebuild_shows_as_changed():
    """If the user overrides back to WRAP, `changed` must find it."""
    from cdcforge.estate import Estate, ExistingObject

    estate = Estate(
        objects=[ExistingObject("ZW_ACCTGDOC", base="I_X", root_table="BKPF")],
        surveyed=1,
    )
    best = _FakeCandidate("I_X", 0.26)
    decision = suggest("BKPF", _FakeReport(best, [best]), estate=estate)
    assert decision.suggested_action == SKIP
    assert decision.suggested_base == "I_X"


def test_no_estate_means_no_existing_column_and_no_override():
    """The parameter is optional, and its absence must not change anything."""
    best = _FakeCandidate("I_X", 0.4)
    decision = suggest("VBAP", _FakeReport(best, [best]))
    assert decision.existing == ""
    assert decision.action == WRAP


def test_the_existing_column_survives_the_round_trip(tmp_path):
    from cdcforge.estate import Estate, ExistingObject

    estate = Estate(
        objects=[ExistingObject("ZW_ACCTGDOC", base="I_X", root_table="BKPF")],
        surveyed=1,
    )
    best = _FakeCandidate("I_X", 0.26)
    path = write_plan(
        [suggest("BKPF", _FakeReport(best, [best]), estate=estate)],
        tmp_path / "plan.xlsx",
    )
    back = read_plan(path)
    assert "ZW_ACCTGDOC" in back[0].existing


def test_a_build_row_gets_no_release_caveat():
    """There is no base to caveat."""
    decision = suggest("KNA1", _FakeReport(None))
    assert "released API" not in decision.why


@pytest.mark.parametrize(
    ("source", "action", "expected"),
    [
        ("I_SALESDOCUMENTITEM", WRAP, "ZW_SALESDOCUMENTITEM"),
        ("C_SALESDOCUMENTITEMDEX", WRAP, "ZW_SALESDOCUMENTITEMDEX"),
        ("KNA1", BUILD, "ZI_KNA1"),
        ("I_VERYLONGNAMETHATGOESONANDONFOREVERANDEVER", WRAP,
         "ZW_VERYLONGNAMETHATGOESONANDON"),
    ],
)
def test_default_names_are_legal_and_readable(source, action, expected):
    """DDLS names cap at 30 characters. Truncating from the right keeps the
    part people recognise; a hash would not."""
    name = _default_target(source, action)
    assert name == expected
    assert len(name) <= 30


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def test_changed_rows_are_surfaced(tmp_path):
    """The most interesting thing in a returned sheet."""
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    workbook["Decisions"]["C2"] = BUILD  # was WRAP
    workbook["Decisions"]["D2"] = None
    workbook.save(path)

    summary = load(path)
    assert summary.ok, summary.problems
    assert [d.object_name for d in summary.changed] == ["VBAP"]
    assert "WRAP → BUILD" in summary.render()


def test_the_ui_builds_the_same_workbook_the_cli_reads(monkeypatch, tmp_path):
    """One format, one reader.

    An earlier UI offered a flat CSV with a free-text Decision column. It
    looked like the same thing and could be fed back to nothing — the user
    filled it in and then had nowhere to put it. This pins that the download
    button produces a workbook `cdcforge apply` accepts.
    """
    pytest.importorskip("streamlit")
    from cdcforge.model import Verdict
    from cdcforge.ui import app

    class _Assessment:
        verdict = Verdict.PASS
        problems = ()

    best = _FakeCandidate("I_SALESORDERITEM", 0.41)
    monkeypatch.setitem(app.S, "table_targets", ["VBAP"])
    monkeypatch.setitem(app.S, "successors", {"VBAP": _FakeReport(best, [best])})
    monkeypatch.setitem(app.S, "assessments", {"I_BUSINESSAREA": _Assessment()})

    monkeypatch.setitem(app.S, "metadata", None)
    data = app.build_decision_workbook()
    assert data, "the download button must produce a workbook"

    summary = load(data)
    assert summary.ok, summary.problems
    assert [d.object_name for d in summary.decisions] == ["VBAP", "I_BUSINESSAREA"]
    assert [d.kind for d in summary.decisions] == ["TABLE", "VIEW"]
    # A view that passes every rule is replicated, not rebuilt.
    assert summary.decisions[1].action == USE


def test_the_ui_workbook_carries_what_is_already_built(monkeypatch):
    """The Existing column must not be blank here and populated in the CLI.

    A re-run in the UI has exactly the same reason to stop proposing objects
    that already exist.
    """
    pytest.importorskip("streamlit")
    from cdcforge.ui import app
    from tests.test_estate import FakeSource, _WRAPPER

    metadata = FakeSource(
        views={
            "ZW_ACCTGDOC": _WRAPPER.format(
                name="ZW_ACCTGDOC", base="I_ACCOUNTINGDOCUMENT", label="x"
            ),
            "I_ACCOUNTINGDOCUMENT": _WRAPPER.format(
                name="I_ACCOUNTINGDOCUMENT", base="BKPF", label="y"
            ),
        },
        tables=("BKPF",),
    )
    best = _FakeCandidate("I_ACCOUNTINGDOCUMENT", 0.26)
    monkeypatch.setitem(app.S, "metadata", metadata)
    monkeypatch.setitem(app.S, "table_targets", ["BKPF"])
    monkeypatch.setitem(app.S, "successors", {"BKPF": _FakeReport(best, [best])})
    monkeypatch.setitem(app.S, "assessments", {})

    summary = load(app.build_decision_workbook())
    assert "ZW_ACCTGDOC" in summary.decisions[0].existing
    assert summary.decisions[0].action == SKIP


def test_the_plan_loop_survives_one_table_that_explodes(tmp_path, monkeypatch):
    """A fifty-table plan is an hour of running.

    Losing forty-nine good answers to one bad table is the worst way to spend
    it, so the row survives, says what happened, and the sheet still arrives.
    """
    import argparse

    from cdcforge import cli_connect

    def _boom(metadata, name, **kw):
        if name == "EXPLODES":
            raise RuntimeError("ADT said something unrepeatable")
        return _FakeReport(_FakeCandidate("I_X", 0.4), [_FakeCandidate("I_X", 0.4)])

    class _EverythingExists:
        def get_table(self, name):
            return object()

        def get_view_source(self, name):
            return None

        def delta_supported_views(self):
            return None

        def prefetch_sources(self, names):
            return None

    monkeypatch.setattr(cli_connect, "_open_session", lambda a: _NullSession())
    monkeypatch.setattr("cdcforge.successors.find_candidates", _boom)
    monkeypatch.setattr(
        "cdcforge.estate.survey", lambda md, **kw: _EmptyEstate()
    )
    monkeypatch.setattr(
        cli_connect, "CachedMetadataSource", lambda *a, **kw: _EverythingExists()
    )
    monkeypatch.setattr(cli_connect, "Store", lambda *a, **kw: None)

    out = tmp_path / "plan.xlsx"
    code = cli_connect.cmd_plan(
        argparse.Namespace(
            profile="T", profile_dir=None, name=["GOOD1", "EXPLODES", "GOOD2"],
            from_file=None, out=str(out), store=None, refresh=False,
            ignore_existing=True,
        )
    )

    assert code == 0
    assert out.exists(), "the sheet must still be written"
    back = read_plan(out)
    assert [d.object_name for d in back] == ["GOOD1", "EXPLODES", "GOOD2"]
    assert back[1].action == SKIP
    assert "unrepeatable" in back[1].why
    assert back[0].action == WRAP and back[2].action == WRAP


def test_a_name_that_is_not_in_the_system_is_not_proposed_for_building(
    tmp_path, monkeypatch
):
    """A misspelling has to look like a misspelling.

    Measured with a deliberate typo: NOSUCHTABLE produced a confident
    `BUILD ZI_NOSUCHTABLE` row, because a name nobody can find yields no
    candidates and no candidates reads as "nothing to build on".
    """
    import argparse

    from cdcforge import cli_connect

    from cdcforge.metadata.types import FieldMeta, TableClass, TableMeta

    real = TableMeta(
        name="REAL",
        table_class=TableClass.TRANSPARENT,
        fields=[FieldMeta(name="ID", is_key=True)],
    )

    class _Metadata:
        def get_table(self, name):
            return real if name == "REAL" else None

        def get_view_source(self, name):
            return None

        def delta_supported_views(self):
            return None

        def prefetch_sources(self, names):
            return None

    monkeypatch.setattr(cli_connect, "_open_session", lambda a: _NullSession())
    monkeypatch.setattr(cli_connect, "Store", lambda *a, **kw: None)
    monkeypatch.setattr(
        cli_connect, "CachedMetadataSource", lambda *a, **kw: _Metadata()
    )
    monkeypatch.setattr(
        "cdcforge.successors.find_candidates",
        lambda md, name, **kw: _FakeReport(None),
    )

    out = tmp_path / "plan.xlsx"
    cli_connect.cmd_plan(
        argparse.Namespace(
            profile="T", profile_dir=None, name=["REAL", "NOSUCHTABLE"],
            from_file=None, out=str(out), store=None, refresh=False,
            ignore_existing=True,
        )
    )

    back = read_plan(out)
    missing = next(d for d in back if d.object_name == "NOSUCHTABLE")
    assert missing.action == SKIP
    assert "Check the spelling" in missing.why
    assert not missing.target, "nothing is named for an object that is not there"

    real = next(d for d in back if d.object_name == "REAL")
    assert real.action == BUILD


class _NullSession:
    def connect(self):
        return None

    def logoff(self):
        return None


class _EmptyEstate:
    surveyed = 0
    objects: tuple = ()

    def note_for(self, table, base=""):
        return ""


def test_a_broken_sheet_reports_every_problem_not_the_first(tmp_path):
    path = write_plan(_sample(), tmp_path / "plan.xlsx")
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["Decisions"]
    sheet["D2"] = None       # WRAP with no base
    sheet["C3"] = "NONSENSE"  # unknown action
    workbook.save(path)

    summary = load(path)
    assert not summary.ok
    assert len(summary.problems) == 2
