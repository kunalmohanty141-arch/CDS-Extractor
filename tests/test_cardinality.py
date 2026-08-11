"""F-14 — the cardinality prover.

The planning half is pure logic and tested here against fixtures. The point
worth protecting is the split: a join on the whole key is *proven* by the key
constraint and needs no data, which is both stronger evidence than a sample and
free.
"""

from __future__ import annotations

import pytest

from cdcforge.cardinality import (
    plan_cardinality_checks,
    structural_evidence,
    summarise,
)
from cdcforge.connect.prober import COUNT_ALIAS, build_probe_query
from cdcforge.rules import CardinalityResult


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _plan_over(metadata, ddl: str, alias: str):
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    ctx = ValidationContext(view=parse_ddl(ddl, name_hint="ZI_PROBE"), metadata=metadata)
    return {p.join_alias: p for p in plan_cardinality_checks(ctx)}[alias]


def test_a_join_onto_a_view_is_proven_through_its_base_table(metadata):
    """Most of SAP's content joins views, not tables.

    Treating a view target as permanently unprovable left R-26 INCONCLUSIVE on
    the majority of real candidates, so no clean verdict was reachable — and
    C_SalesDocumentItemDEX, SAP's own extraction view, could never be READY.

    It reduces to the table case: the ON condition names elements of the joined
    view that trace, through lineage, to the whole primary key of the table it
    is rooted on. The proof rests on that real key constraint, not on the
    view's own key declaration, which CDS does not enforce.
    """
    plan = _plan_over(
        metadata,
        "define view entity ZI_PROBE as select from zorderitem as i\n"
        "  left outer to one join ZI_CustOrder as h on h.OrderId = i.orderid\n"
        "{ key i.orderid as A, key i.itemno as B, h.Amount as C }",
        "h",
    )
    assert plan.structural, plan.blocked
    assert plan.table == "ZCUSTORDER"
    assert plan.evidence().result is CardinalityResult.PROVEN_TO_ONE
    assert "whole primary key" in plan.reason


def test_a_join_onto_an_aggregating_view_is_not_proven(metadata):
    """ZI_AGGREGATE groups, so one base row is not one output row.

    No key constraint bounds the match, and claiming otherwise would be the
    silent false PASS the tool exists to prevent.
    """
    plan = _plan_over(
        metadata,
        "define view entity ZI_PROBE as select from zorderitem as i\n"
        "  left outer to one join ZI_AGGREGATE as g on g.OrderId = i.orderid\n"
        "{ key i.orderid as A, key i.itemno as B }",
        "g",
    )
    assert not plan.structural
    assert plan.blocked


def test_a_join_onto_a_view_that_misses_the_key_is_not_proven(metadata):
    """Joining on a non-key element proves nothing — several rows can match.

    Amount traces to ZCUSTORDER.AMOUNT, which is not the key, so the key
    constraint says nothing about how many rows share a value.
    """
    plan = _plan_over(
        metadata,
        "define view entity ZI_PROBE as select from zorderitem as i\n"
        "  left outer to one join ZI_CustOrder as h on h.Amount = i.netprice\n"
        "{ key i.orderid as A, key i.itemno as B }",
        "h",
    )
    assert not plan.structural, plan.reason
    assert plan.blocked
    assert "ORDERID" in plan.blocked, "should name the key field it never reached"


def test_a_join_on_the_whole_key_is_proven_without_data(context):
    """SNWD_PD's key is NODE_KEY, and the join is on NODE_KEY.

    At most one row can match — a guarantee from the key constraint, not an
    inference from a sample.
    """
    plans = {p.join_alias: p for p in plan_cardinality_checks(context("ZI_SALESORDER_CDC"))}

    product = plans["Product"]
    assert product.structural
    assert not product.needs_data
    assert product.evidence().result is CardinalityResult.PROVEN_TO_ONE
    assert "whole primary key" in product.reason


def test_a_join_on_a_non_key_column_needs_data(context):
    """Item joins on PARENT_KEY, which is not SNWD_SO_I's key (NODE_KEY).

    Nothing guarantees uniqueness, so this is exactly the case that can be
    declared to-one and silently be to-many.
    """
    plans = {p.join_alias: p for p in plan_cardinality_checks(context("ZI_SALESORDER_CDC"))}

    item = plans["Item"]
    assert not item.structural
    assert item.needs_data
    assert item.join_fields == ["parent_key"]
    assert "missing NODE_KEY" in item.reason
    assert item.evidence() is None  # nothing can be concluded yet


def test_undeclared_cardinality_is_not_planned(context):
    """R-10's business. There is no claim to test."""
    assert plan_cardinality_checks(context("ZI_UNSPECIFIED_JOIN")) == []


def test_views_without_joins_produce_no_plans(context):
    assert plan_cardinality_checks(context("ZI_BUSINESSAREA")) == []
    assert "no declared to-one joins" in summarise([])


def test_a_join_onto_a_view_cannot_be_probed(context):
    plans = plan_cardinality_checks(context("ZI_MAPPED_VIEW"))
    assert all(not p.needs_data for p in plans)


def test_structural_evidence_feeds_the_rule_engine(assess, context):
    """The free half alone should settle a join-on-the-key."""
    from cdcforge.model import Outcome

    evidence = structural_evidence(context("ZI_SALESORDER_CDC"))
    assert "Product" in evidence
    assert "Item" not in evidence  # that one genuinely needs data

    a = assess("ZI_SALESORDER_CDC", cardinality_evidence=evidence)
    product = [r for r in a.results_for("R-26") if r.node == "Product"]
    assert product and product[0].outcome is Outcome.SATISFIED


def test_summary_counts_the_three_categories(context):
    plans = plan_cardinality_checks(context("ZI_SALESORDER_CDC"))
    text = summarise(plans)
    assert "2 to-one join(s)" in text
    assert "1 proven by key constraint" in text
    assert "1 need data" in text


# ---------------------------------------------------------------------------
# The query — the boundary that keeps this to counts
# ---------------------------------------------------------------------------


def test_probe_query_selects_only_keys_and_a_count():
    query = build_probe_query("ZORDERITEM", ["ORDERID"], "MANDT", "800")
    assert query.startswith("SELECT ORDERID, COUNT(*)")
    assert COUNT_ALIAS in query
    assert "GROUP BY ORDERID" in query
    assert "HAVING COUNT(*) > 1" in query
    assert "WHERE MANDT = '800'" in query


def test_probe_query_can_never_select_row_contents():
    """The boundary is structural: the caller passes identifiers, not SQL.

    Every selected column is a grouping column or the count, so there is no
    shape of input that makes this return a row's contents.
    """
    query = build_probe_query("VBAP", ["VBELN", "POSNR"], "MANDT", "100")
    selected = query.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    columns = [c.strip() for c in selected.split(",")]
    assert columns[:2] == ["VBELN", "POSNR"]
    assert columns[2].startswith("COUNT(*)")
    assert len(columns) == 3


def test_probe_query_strips_injection_attempts():
    query = build_probe_query("ZTAB'; DROP TABLE X --", ["K'; DELETE"], "", "")
    assert "'" not in query.replace("COUNT(*)", "")
    assert "DROP" in query  # kept as inert characters, not as syntax
    assert ";" not in query


def test_probe_query_refuses_without_key_columns():
    with pytest.raises(ValueError):
        build_probe_query("ZTAB", [])


def test_client_filter_is_omitted_when_the_table_has_no_client():
    query = build_probe_query("ZTAB", ["K"], "", "800")
    assert "WHERE" not in query
