"""Column lineage.

A CDC mapping addresses base tables, never intermediate views, while almost
every SAP view sits on another view. Without lineage the wrapper generator
refused every such view — which is nearly all of the content it exists for.
"""

from __future__ import annotations

from cdcforge.lineage import element_origins, exposed_key_elements, trace_element
from cdcforge.parsing.ddl import parse_ddl


def parse(metadata, name):
    return parse_ddl(metadata.get_view_source(name), name_hint=name)


def test_an_unqualified_element_resolves_against_a_view_source(metadata):
    """The I_SalesDocument case.

    A classic view selecting from a *view* with joined tables, whose elements
    are written unqualified — the normal shape of SAP content. Disambiguation
    used to consult DD03L only, so a view among the sources made it give up
    entirely: 58 of I_SalesDocument's 297 elements traced, its key resolved to
    nothing, and F-09 rejected the sales document view of VBAK for "does not
    expose the table's full key".

    The view source has to be asked what elements it has, exactly as a table is
    asked what fields it has.
    """
    source = (
        "define view ZI_OVER_VIEW as select from ZI_CustOrder as h\n"
        "  left outer to one join zorderitem as i on i.OrderId = h.OrderId\n"
        "{ key h.OrderId, Amount, i.ItemNo }"
    )
    view = parse_ddl(source, name_hint="ZI_OVER_VIEW")

    # Amount belongs to the view source alone, so it is unambiguous — and it
    # resolves only if a view can be asked what elements it exposes.
    origin = trace_element(metadata, view, "Amount")
    assert origin is not None, "unqualified name must resolve through the view"
    assert (origin.table, origin.field) == ("ZCUSTORDER", "AMOUNT")


def test_an_unqualified_element_owned_by_two_sources_stays_ambiguous(metadata):
    """Resolving is only allowed when exactly one source owns the name.

    Both sources carrying the column is genuine ambiguity, and guessing would
    put the wrong table into a CDC mapping. OrderId is the join column here, so
    it exists on both sides — the common case, and the one that must stay
    unresolved rather than defaulting to the FROM.
    """
    source = (
        "define view ZI_AMBIG as select from ZI_CustOrder as h\n"
        "  left outer to one join zorderitem as i on i.OrderId = h.OrderId\n"
        "{ key OrderId }"
    )
    view = parse_ddl(source, name_hint="ZI_AMBIG")
    assert trace_element(metadata, view, "OrderId") is None


def test_an_element_on_a_table_traces_at_depth_zero(metadata):
    view = parse(metadata, "ZI_CUSTORDER")
    origin = trace_element(metadata, view, "OrderId")
    assert origin is not None
    assert (origin.table, origin.field, origin.depth) == ("ZCUSTORDER", "ORDERID", 0)
    assert origin.qualified == "ZCUSTORDER.ORDERID"


def test_an_element_through_a_view_traces_to_the_base_table(metadata):
    """ZI_MAPPED_VIEW → ZI_CUSTORDER → ZCUSTORDER, following the alias chain."""
    view = parse(metadata, "ZI_MAPPED_VIEW")
    origin = trace_element(metadata, view, "OrderId")
    assert origin is not None
    assert (origin.table, origin.field) == ("ZCUSTORDER", "ORDERID")
    assert origin.depth == 1


def test_lineage_sees_through_a_preserving_type_cast(metadata):
    """SAP's VDM wraps almost every key in a type-only cast.

    R_PurchaseOrder exposes EKKO's key as
    `cast(PurchasingDocument as vdm_purchaseorder preserving type)`. The value
    is unchanged, so lineage must follow it — stopping there made the key
    untraceable and refused a wrapper over every such view.
    """
    source = (
        "define view entity ZI_CAST as select from zcustorder\n"
        "{ key cast(orderid as abap.char(12) preserving type) as OrderId,\n"
        "      customer as Customer }"
    )
    view = parse_ddl(source, name_hint="ZI_CAST")
    origin = trace_element(metadata, view, "OrderId")
    assert origin is not None
    assert (origin.table, origin.field) == ("ZCUSTORDER", "ORDERID")


def test_a_cast_is_seen_through_across_a_stack(metadata):
    table = metadata.get_table("ZCUSTORDER")
    source = (
        "define view entity ZI_CASTED as select from ZI_CUSTORDER as O\n"
        "{ key cast(O.OrderId as abap.char(12) preserving type) as PurchaseOrder }"
    )
    view = parse_ddl(source, name_hint="ZI_CASTED")
    assert exposed_key_elements(metadata, view, table) == {"PurchaseOrder": "ORDERID"}


def test_arithmetic_is_not_mistaken_for_a_cast(metadata):
    """`a + 1` also has exactly one field reference and does not preserve the
    value. Treating it as lineage would put arithmetic into a key mapping."""
    source = (
        "define view entity ZI_MATH as select from zcustorder"
        " { key orderid as OrderId, amount + 1 as Bumped }"
    )
    view = parse_ddl(source, name_hint="ZI_MATH")
    assert trace_element(metadata, view, "Bumped") is None


def test_a_computed_element_has_no_single_origin(metadata):
    """Claiming one would put a CASE expression into a CDC mapping."""
    view = parse(metadata, "ZI_COMMENT_TRAPS")
    assert trace_element(metadata, view, "CaseResult") is None
    assert trace_element(metadata, view, "Note") is None


def test_an_unknown_element_traces_to_nothing(metadata):
    view = parse(metadata, "ZI_CUSTORDER")
    assert trace_element(metadata, view, "NoSuchElement") is None


def test_a_broken_chain_gives_up_rather_than_guessing(metadata):
    source = (
        "define view entity ZI_ON_MISSING as select from I_DoesNotExist as X"
        " { key X.SomeField as Key }"
    )
    view = parse_ddl(source, name_hint="ZI_ON_MISSING")
    assert trace_element(metadata, view, "Key") is None


def test_recursion_is_bounded(metadata):
    view = parse(metadata, "ZI_DEEP_L1")
    # The chain is six views deep; a depth cap below that must give up rather
    # than loop or lie.
    assert trace_element(metadata, view, "OrderId", max_depth=2) is None
    assert trace_element(metadata, view, "OrderId", max_depth=8) is not None


def test_element_origins_covers_the_traceable_elements(metadata):
    view = parse(metadata, "ZI_CUSTORDER")
    origins = element_origins(metadata, view)
    assert set(origins) >= {"OrderId", "Customer", "Amount"}
    assert all(o.table == "ZCUSTORDER" for o in origins.values())


def test_key_exposure_is_all_or_nothing(metadata):
    """A partial key is not a mapping — it activates and fails at delta time."""
    table = metadata.get_table("ZORDERITEM")

    complete = parse_ddl(
        "define view entity ZI_BOTH as select from zorderitem"
        " { key orderid as OrderId, key itemno as ItemNo }",
        name_hint="ZI_BOTH",
    )
    assert exposed_key_elements(metadata, complete, table) == {
        "OrderId": "ORDERID", "ItemNo": "ITEMNO",
    }

    partial = parse_ddl(
        "define view entity ZI_HALF as select from zorderitem"
        " { key orderid as OrderId, material as Material }",
        name_hint="ZI_HALF",
    )
    assert exposed_key_elements(metadata, partial, table) == {}


def test_key_exposure_works_through_a_stack(metadata):
    table = metadata.get_table("ZCUSTORDER")
    view = parse(metadata, "ZI_MAPPED_VIEW")
    assert exposed_key_elements(metadata, view, table) == {"OrderId": "ORDERID"}
