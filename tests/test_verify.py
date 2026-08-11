"""Is it still good?

Everything else in this tool answers "can this work" before the fact. These
tests are about the state nobody looks at: an object built months ago, in a
system that has moved under it.

The recurring theme is that "could not be established" is its own answer.
Reporting a feed as healthy on the strength of a query that failed is the
failure mode that matters here, because nobody checks a green line twice.
"""

from __future__ import annotations

import pytest

from cdcforge.model import Verdict
from cdcforge.verify import Verification, verify
from tests.test_estate import FakeSource, _PLAIN, _WRAPPER


def _system(**extra) -> FakeSource:
    views = {
        "ZI_KNA1": _WRAPPER.format(
            name="ZI_KNA1", base="KNA1", label="Extraction view for KNA1"
        ),
        "ZW_ACCTGDOC": _WRAPPER.format(
            name="ZW_ACCTGDOC", base="I_ACCOUNTINGDOCUMENT", label="wrapper"
        ),
        "I_ACCOUNTINGDOCUMENT": _PLAIN.format(
            name="I_ACCOUNTINGDOCUMENT", base="BKPF"
        ),
    }
    views.update(extra)
    return FakeSource(views=views, tables=("KNA1", "BKPF"))


def test_an_object_carrying_delta_is_ok():
    report = verify(
        _system(), ["ZI_KNA1"], delta_supported={"ZI_KNA1"}, check_rules=False
    )
    result = report.results[0]
    assert result.ok
    assert result.status == "OK"
    assert report.failed == []


def test_an_object_that_is_gone_is_reported_not_skipped():
    """The cheapest failure, and the one nobody notices."""
    report = verify(_system(), ["ZI_DELETED"], delta_supported=set())
    result = report.results[0]
    assert result.exists is False
    assert result.status == "GONE"
    assert not result.ok
    assert "not found" in result.notes[0]


def test_an_object_that_lost_delta_says_so():
    report = verify(
        _system(), ["ZI_KNA1"], delta_supported={"SOMETHING_ELSE"}, check_rules=False
    )
    result = report.results[0]
    assert result.delta_supported is False
    assert result.status == "NO DELTA"
    assert not result.ok


def test_an_unaskable_system_gives_unconfirmed_not_failed():
    """"SAP says this lost delta" and "we could not ask" are the difference
    between an incident and a retry."""
    report = verify(_system(), ["ZI_KNA1"], delta_supported=None, check_rules=False)
    result = report.results[0]
    assert result.delta_supported is None
    assert result.status == "UNCONFIRMED"
    assert not result.ok, "unconfirmed is never a pass"
    assert "could not be confirmed" in result.notes[0]


def test_delta_but_failing_rules_is_its_own_state():
    """The system's flag is derived from what the view declares, so the two
    can disagree — and that disagreement is worth seeing."""
    result = Verification(
        name="ZX", exists=True, delta_supported=True, verdict=Verdict.FAIL_HARD
    )
    assert result.status == "DELTA, RULES FAIL"
    assert result.ok, "SAP still reports it as feeding; the rules are advisory here"


def test_a_wrapper_over_an_unreleased_sap_view_is_flagged():
    """Measured: 9 of the 17 objects this tool had built on the reference
    system are wrappers over unreleased SAP views. They work today."""
    from cdcforge.metadata.types import ApiState, ObjectMeta

    source = _system()
    source.objects = {
        "I_ACCOUNTINGDOCUMENT": ObjectMeta(
            name="I_ACCOUNTINGDOCUMENT", api_state=ApiState.NOT_RELEASED
        )
    }
    report = verify(
        source, ["ZW_ACCTGDOC"], delta_supported={"ZW_ACCTGDOC"}, check_rules=False
    )
    result = report.results[0]
    assert result.base == "I_ACCOUNTINGDOCUMENT"
    assert result.base_released is False
    assert "not a released API" in result.notes[0]
    assert result.ok, "a caveat, not a failure — it carries delta today"


def test_a_released_base_is_not_flagged():
    from cdcforge.metadata.types import ApiState, ObjectMeta

    source = _system()
    source.objects = {
        "I_ACCOUNTINGDOCUMENT": ObjectMeta(
            name="I_ACCOUNTINGDOCUMENT", api_state=ApiState.C1
        )
    }
    report = verify(
        source, ["ZW_ACCTGDOC"], delta_supported={"ZW_ACCTGDOC"}, check_rules=False
    )
    assert report.results[0].base_released is True
    assert report.results[0].notes == []


def test_a_customer_base_is_never_flagged_as_unreleased():
    """SAP will not change somebody's own view in a support pack.

    Measured as noise: the first run warned that ZAOH_I_GS_SALES_CUBE was built
    on ZAOH_I_GS_SALES_PROD, "which SAP may change in a support pack". Both are
    the same team's work. Warnings that are simply false are how people learn
    to skip warnings.
    """
    source = _system(
        ZW_OVER_CUSTOM=_WRAPPER.format(
            name="ZW_OVER_CUSTOM", base="ZI_KNA1", label="over our own"
        )
    )
    report = verify(
        source,
        ["ZW_OVER_CUSTOM"],
        delta_supported={"ZW_OVER_CUSTOM"},
        check_rules=False,
    )
    result = report.results[0]
    assert result.base == "ZI_KNA1"
    assert result.base_released is None
    assert result.notes == []


def test_a_view_built_straight_on_a_table_has_no_base_to_release():
    report = verify(
        _system(), ["ZI_KNA1"], delta_supported={"ZI_KNA1"}, check_rules=False
    )
    assert report.results[0].base == ""
    assert report.results[0].notes == []


def test_an_unknown_release_state_is_neither_released_nor_flagged():
    """Unknown is not evidence of a problem, and must not be reported as one."""
    report = verify(
        _system(), ["ZW_ACCTGDOC"], delta_supported={"ZW_ACCTGDOC"}, check_rules=False
    )
    result = report.results[0]
    assert result.base_released is None
    assert result.notes == []


def test_one_object_that_explodes_does_not_lose_the_rest():
    """Verifying forty objects and dying on the seventh tells you nothing
    about the other thirty-three."""

    class _Exploding(FakeSource):
        def get_view_source(self, name):
            if name.upper() == "ZI_BOOM":
                raise RuntimeError("ADT said something unrepeatable")
            return super().get_view_source(name)

    source = _Exploding(
        views=_system().views, tables=("KNA1", "BKPF")
    )
    report = verify(
        source,
        ["ZI_KNA1", "ZI_BOOM", "ZW_ACCTGDOC"],
        delta_supported={"ZI_KNA1", "ZW_ACCTGDOC"},
        check_rules=False,
    )

    assert [r.name for r in report.results] == ["ZI_KNA1", "ZI_BOOM", "ZW_ACCTGDOC"]
    assert report.results[0].ok and report.results[2].ok
    boom = report.results[1]
    assert not boom.ok, "a failure to check is never a pass"
    assert boom.status == "UNKNOWN"
    assert "unrepeatable" in boom.notes[0]


def test_the_report_counts_what_still_works():
    report = verify(
        _system(),
        ["ZI_KNA1", "ZI_GONE"],
        delta_supported={"ZI_KNA1"},
        check_rules=False,
    )
    rendered = report.render()
    assert "1 of 2 still carrying delta" in rendered
    assert "1 need attention" in rendered
    assert len(report.failed) == 1


@pytest.mark.parametrize(
    ("exists", "delta", "expected"),
    [
        (True, True, "OK"),
        (True, False, "NO DELTA"),
        (True, None, "UNCONFIRMED"),
        (False, None, "GONE"),
        (None, None, "UNKNOWN"),
    ],
)
def test_status_never_reads_a_gap_as_a_pass(exists, delta, expected):
    result = Verification(name="ZX", exists=exists, delta_supported=delta)
    assert result.status == expected
    assert result.ok is (expected == "OK")
