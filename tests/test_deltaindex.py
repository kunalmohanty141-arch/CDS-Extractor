"""Which of SAP's delta views feeds which table.

The shape here is taken from the real chain that defeated the candidate
search — EKET, four layers below a C1-released delta view, with two unreleased
views in between and no where-used index able to see any of it.
"""

from __future__ import annotations

import pytest

from cdcforge.deltaindex import DeltaIndex, build, load_or_build
from tests.test_estate import FakeSource, _PLAIN, _WRAPPER


def _chain() -> FakeSource:
    """C_PURORDSCHEDULELINEDEX -> ... -> EKET, as measured."""
    return FakeSource(
        views={
            "C_PURORDSCHEDULELINEDEX": _WRAPPER.format(
                name="C_PurOrdScheduleLineDEX",
                base="I_PurOrdScheduleLineAPI01",
                label="Extraction for PO schedule lines",
            ),
            "I_PURORDSCHEDULELINEAPI01": _PLAIN.format(
                name="I_PurOrdScheduleLineAPI01",
                base="I_PurOrdScheduleLineBasic",
            ),
            "I_PURORDSCHEDULELINEBASIC": _PLAIN.format(
                name="I_PurOrdScheduleLineBasic",
                base="I_PurgDocScheduleLineBasic",
            ),
            "I_PURGDOCSCHEDULELINEBASIC": _PLAIN.format(
                name="I_PurgDocScheduleLineBasic", base="EKET"
            ),
            # A delta view that only *joins* EKET is not a feed for it.
            "C_SOMETHINGELSEDEX": _WRAPPER.format(
                name="C_SomethingElseDEX", base="MARA", label="elsewhere"
            ),
        },
        tables=("EKET", "MARA"),
    )


class _WithDelta(FakeSource):
    def __init__(self, inner: FakeSource, delta: set[str]):
        super().__init__(views=inner.views, tables=inner.tables)
        self._delta = delta

    def delta_supported_views(self):
        return self._delta or None


def test_a_delta_view_four_layers_up_is_found():
    """The case the climb could not reach.

    Two unreleased views sit between the C1 delta view and the table, and
    neither where-used index records any of it.
    """
    source = _WithDelta(_chain(), {"C_PURORDSCHEDULELINEDEX", "C_SOMETHINGELSEDEX"})
    index = build(source)

    assert index.available
    assert index.covering("EKET") == ["C_PURORDSCHEDULELINEDEX"]
    assert index.covering("MARA") == ["C_SOMETHINGELSEDEX"]


def test_resolving_a_chain_does_not_ask_whether_each_view_is_a_table():
    """The order of two questions, and the whole performance of the module.

    `get_table` on a name that is not a table costs two freestyle DDIC queries
    that return nothing. Every element of every chain except the last is a
    view, so testing for a table first spends two round trips per view to
    learn what the prefetched source cache already knows — thousands of
    queries across 904 chains, and forty minutes of asking things that are
    obviously views whether they are tables.
    """
    asked: list[str] = []

    class _Counting(_WithDelta):
        def get_table(self, name):
            asked.append(name.upper())
            return super().get_table(name)

    source = _Counting(_chain(), {"C_PURORDSCHEDULELINEDEX"})
    index = build(source)

    assert index.covering("EKET") == ["C_PURORDSCHEDULELINEDEX"]
    assert asked == ["EKET"], (
        f"only the thing with no DDL source should be tested as a table, "
        f"not every view on the way down — asked about {asked}"
    )


def test_a_table_with_no_delta_view_gets_an_empty_list():
    source = _WithDelta(_chain(), {"C_PURORDSCHEDULELINEDEX"})
    assert build(source).covering("VBAP") == []


def test_a_system_that_cannot_say_yields_an_unavailable_index():
    """An empty index must never read as "no delta view feeds anything"."""
    index = build(_WithDelta(_chain(), set()))
    assert not index.available
    assert index.covering("EKET") == []


def test_a_view_whose_chain_cannot_be_followed_is_counted():
    source = _WithDelta(
        FakeSource(
            views={
                "C_MYSTERYDEX": _WRAPPER.format(
                    name="C_MysteryDEX", base="I_NOBODYKNOWS", label="x"
                )
            },
            tables=(),
        ),
        {"C_MYSTERYDEX"},
    )
    index = build(source)
    assert index.examined == 1
    assert index.unresolved == 1
    assert index.by_table == {}


def test_one_view_that_explodes_does_not_lose_the_index():
    class _Exploding(FakeSource):
        def get_view_source(self, name):
            if name.upper() == "C_BOOMDEX":
                raise RuntimeError("gone")
            return super().get_view_source(name)

    inner = _chain()
    inner.views["C_BOOMDEX"] = "not ddl"
    source = _WithDelta(
        _Exploding(views=inner.views, tables=inner.tables),
        {"C_PURORDSCHEDULELINEDEX", "C_BOOMDEX"},
    )
    index = build(source)
    assert index.covering("EKET") == ["C_PURORDSCHEDULELINEDEX"]
    assert index.unresolved == 1


# ---------------------------------------------------------------------------
# Caching — the answer changes when SAP ships, not while you work
# ---------------------------------------------------------------------------


def test_the_index_is_cached_and_not_rebuilt(tmp_path):
    from cdcforge.store import Store

    source = _WithDelta(_chain(), {"C_PURORDSCHEDULELINEDEX"})
    store = Store(tmp_path / "c.sqlite", profile_id="T")

    first = load_or_build(source, store)
    assert first.covering("EKET") == ["C_PURORDSCHEDULELINEDEX"]

    # A source that would now answer nothing. The cached index must survive.
    silent = _WithDelta(_chain(), set())
    second = load_or_build(silent, store)
    assert second.covering("EKET") == ["C_PURORDSCHEDULELINEDEX"]


def test_refresh_rebuilds(tmp_path):
    from cdcforge.store import Store

    store = Store(tmp_path / "c.sqlite", profile_id="T")
    load_or_build(_WithDelta(_chain(), {"C_PURORDSCHEDULELINEDEX"}), store)

    rebuilt = load_or_build(
        _WithDelta(_chain(), {"C_SOMETHINGELSEDEX"}), store, refresh=True
    )
    assert rebuilt.covering("EKET") == []
    assert rebuilt.covering("MARA") == ["C_SOMETHINGELSEDEX"]


def test_an_index_from_an_older_version_is_not_believed(tmp_path):
    """A stored shape this code does not recognise is rebuilt, not misread."""
    from cdcforge.store import Store

    store = Store(tmp_path / "c.sqlite", profile_id="T")
    store.put_cached("index", "delta-index", {"version": 0, "by_table": {"EKET": ["X"]}})

    index = load_or_build(_WithDelta(_chain(), {"C_PURORDSCHEDULELINEDEX"}), store)
    assert index.covering("EKET") == ["C_PURORDSCHEDULELINEDEX"]


@pytest.mark.parametrize("payload", [None, "junk", {"version": 1}, {"by_table": 3}])
def test_unreadable_cached_payloads_are_rebuilt(payload):
    assert DeltaIndex.from_dict(payload) is None


def test_a_bare_index_answers_nothing_rather_than_erroring():
    index = DeltaIndex()
    assert not index.available
    assert index.covering("EKET") == []
