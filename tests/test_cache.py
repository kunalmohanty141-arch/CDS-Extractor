"""Caching layer tests.

The point of this layer is that a second read costs nothing, including a second
read of something that does not exist.
"""

from __future__ import annotations

import pytest

from cdcforge.cache import CachedMetadataSource
from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import (
    ApiState,
    FieldMeta,
    ObjectMeta,
    Owner,
    TableClass,
    TableMeta,
)
from cdcforge.store import Store


class CountingSource(MetadataSource):
    """Records every call, so cache misses are visible."""

    name = "counting"

    def __init__(self) -> None:
        self.source_calls: list[str] = []
        self.table_calls: list[str] = []
        self.object_calls: list[str] = []

    def get_view_source(self, name):
        self.source_calls.append(name.upper())
        if name.upper() == "ZI_KNOWN":
            return "define view entity ZI_KNOWN as select from t { key t.k as K }"
        return None

    def get_table(self, name):
        self.table_calls.append(name.upper())
        if name.upper() != "ZCUSTORDER":
            return None
        return TableMeta(
            name="ZCUSTORDER",
            table_class=TableClass.TRANSPARENT,
            delivery_class="A",
            owner=Owner.CUSTOMER,
            estimated_rows=1234,
            fields=[
                FieldMeta("MANDT", 1, is_key=True, data_type="CLNT", length=3),
                FieldMeta("ORDERID", 2, is_key=True, data_type="CHAR", length=12),
                FieldMeta("AMOUNT", 3, data_type="CURR", length=15, decimals=2),
            ],
        )

    def get_object(self, name):
        self.object_calls.append(name.upper())
        if name.upper() != "ZI_KNOWN":
            return None
        return ObjectMeta(
            name="ZI_KNOWN", package="ZSALES", owner=Owner.CUSTOMER,
            api_state=ApiState.NOT_RELEASED,
        )

    def list_views(self):
        return ["ZI_KNOWN"]

    def list_tables(self):
        return ["ZCUSTORDER"]


@pytest.fixture
def inner() -> CountingSource:
    return CountingSource()


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "cache.sqlite", profile_id="TEST")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_source_is_read_once(inner, store):
    cached = CachedMetadataSource(inner, store)
    assert cached.get_view_source("ZI_KNOWN").startswith("define")
    assert cached.get_view_source("ZI_KNOWN").startswith("define")
    assert inner.source_calls == ["ZI_KNOWN"]
    assert cached.stats.hits == 1


def test_cache_survives_a_new_process(inner, store, tmp_path):
    CachedMetadataSource(inner, store).get_view_source("ZI_KNOWN")

    reopened = Store(tmp_path / "cache.sqlite", profile_id="TEST")
    fresh = CachedMetadataSource(inner, reopened)
    assert fresh.get_view_source("ZI_KNOWN").startswith("define")
    assert inner.source_calls == ["ZI_KNOWN"]  # still only one network read


def test_a_missing_object_is_only_asked_about_once(inner, store):
    """Negative caching.

    A miss costs the same round-trip as a hit, and the stack resolver asks
    about every source name it meets — most of which are tables, not views.
    Re-asking on each run is most of what made deep stacks unusable.
    """
    cached = CachedMetadataSource(inner, store)
    assert cached.get_view_source("ZCUSTORDER") is None
    assert cached.get_view_source("ZCUSTORDER") is None
    assert inner.source_calls == ["ZCUSTORDER"]
    assert cached.stats.negative_hits == 1


def test_refresh_bypasses_the_cache(inner, store):
    CachedMetadataSource(inner, store).get_view_source("ZI_KNOWN")
    refreshing = CachedMetadataSource(inner, store, refresh=True)
    refreshing.get_view_source("ZI_KNOWN")
    assert inner.source_calls == ["ZI_KNOWN", "ZI_KNOWN"]


# ---------------------------------------------------------------------------
# Tables and objects
# ---------------------------------------------------------------------------


def test_table_round_trips_through_the_cache_intact(inner, store):
    cached = CachedMetadataSource(inner, store)
    first = cached.get_table("ZCUSTORDER")

    reopened = CachedMetadataSource(inner, store)
    reopened._memory.clear()
    second = reopened.get_table("ZCUSTORDER")

    assert inner.table_calls == ["ZCUSTORDER"]
    assert second is not None
    assert second.name == first.name
    assert second.table_class is TableClass.TRANSPARENT
    assert second.owner is Owner.CUSTOMER
    assert second.estimated_rows == 1234
    assert [f.name for f in second.fields] == ["MANDT", "ORDERID", "AMOUNT"]
    assert [f.name for f in second.business_key_fields] == ["ORDERID"]
    assert second.field_by_name("AMOUNT").decimals == 2


def test_object_round_trips_with_its_api_state(inner, store):
    cached = CachedMetadataSource(inner, store)
    cached.get_object("ZI_KNOWN")

    fresh = CachedMetadataSource(inner, store)
    revived = fresh.get_object("ZI_KNOWN")
    assert inner.object_calls == ["ZI_KNOWN"]
    assert revived.api_state is ApiState.NOT_RELEASED
    assert revived.owner is Owner.CUSTOMER
    assert revived.is_modifiable


def test_missing_table_is_cached_as_a_miss(inner, store):
    cached = CachedMetadataSource(inner, store)
    assert cached.get_table("NOPE") is None
    assert CachedMetadataSource(inner, store).get_table("NOPE") is None
    assert inner.table_calls == ["NOPE"]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_the_rule_engine_reads_each_object_once(store):
    """The reason this layer exists.

    The stack resolver visits every referenced name, so an uncached source is
    re-read once per reference. This asserts the whole assessment costs one
    read per distinct object.
    """
    from cdcforge.rules import validate_object

    class Stack(CountingSource):
        def get_view_source(self, name):
            self.source_calls.append(name.upper())
            sources = {
                "ZI_TOP": "define view entity ZI_TOP as select from ZI_MID"
                          " left outer to one join ZI_MID as B on B.K = ZI_MID.K"
                          " { key ZI_MID.K as K }",
                "ZI_MID": "define view entity ZI_MID as select from zcustorder"
                          " { key orderid as K }",
            }
            return sources.get(name.upper())

    inner = Stack()
    cached = CachedMetadataSource(inner, store)
    validate_object("ZI_TOP", cached)

    assert sorted(set(inner.source_calls)) == sorted(inner.source_calls), (
        f"an object was read more than once: {inner.source_calls}"
    )


def test_stats_report_the_hit_rate(inner, store):
    cached = CachedMetadataSource(inner, store)
    cached.get_view_source("ZI_KNOWN")
    cached.get_view_source("ZI_KNOWN")
    cached.get_view_source("ZI_KNOWN")
    assert cached.stats.misses == 1
    assert cached.stats.hits == 2
    assert "3 lookup(s)" in cached.stats.render()
