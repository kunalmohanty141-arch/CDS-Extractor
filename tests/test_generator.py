"""Generator tests — F-19 … F-22, F-25.

The most important assertions here are the refusals. A generator that emits DDL
which cannot work is worse than one that emits nothing.
"""

from __future__ import annotations

from cdcforge.generator import (
    NamingConvention,
    build_cdc_mapping,
    check_name,
    generate_view_for_table,
    generate_wrapper,
    preview_names,
)
from cdcforge.metadata import ApiState, ObjectMeta, Owner
from cdcforge.model import Verdict
from cdcforge.rules import validate_object, validate_source


# ---------------------------------------------------------------------------
# F-19 — Z-table → CDS view
# ---------------------------------------------------------------------------


def test_generated_view_is_valid_against_the_rule_engine(metadata):
    """The strongest test available: validate what the generator emits.

    The generated object does not exist in the system yet, so its repository
    header is supplied — exactly as the write pipeline would when it creates
    the object in a customer package.
    """
    table = metadata.get_table("ZORDERITEM")
    generated = generate_view_for_table(table)
    assert generated.ok

    assessment = validate_source(
        generated.ddl,
        name=generated.name,
        metadata=metadata,
        object_meta=ObjectMeta(
            name=generated.name,
            owner=Owner.CUSTOMER,
            api_state=ApiState.NOT_RELEASED,
            package="ZSALES",
        ),
    )
    assert assessment.verdict is Verdict.PASS, assessment.summary


def test_single_table_view_uses_the_automatic_annotation(metadata):
    generated = generate_view_for_table(metadata.get_table("ZCUSTORDER"))
    assert "changeDataCapture.automatic: true" in generated.ddl
    assert "mapping" not in generated.ddl


def test_all_key_fields_are_marked_key(metadata):
    generated = generate_view_for_table(metadata.get_table("ZORDERITEM"))
    assert "key ORDERID" in generated.ddl
    assert "key ITEMNO" in generated.ddl


def test_client_field_is_excluded_and_the_reason_is_stated(metadata):
    generated = generate_view_for_table(metadata.get_table("ZCUSTORDER"))
    assert "MANDT" not in generated.ddl
    assert any("client field" in w for w in generated.warnings)


def test_cluster_table_is_refused(metadata):
    generated = generate_view_for_table(metadata.get_table("RFBLG"))
    assert not generated.ok
    assert "CLUSTER" in generated.refused_because


def test_pool_table_is_refused(metadata):
    generated = generate_view_for_table(metadata.get_table("ZLEGACY_POOL"))
    assert not generated.ok


def test_table_without_a_primary_key_is_refused(metadata):
    generated = generate_view_for_table(metadata.get_table("ZNOKEY"))
    assert not generated.ok
    assert "primary key" in generated.refused_because


def test_field_selection_cannot_drop_a_key_field(metadata):
    table = metadata.get_table("ZORDERITEM")
    generated = generate_view_for_table(table, fields=["MATERIAL"])
    # F-22: keys are mandatory whatever the user selected.
    assert "key ORDERID" in generated.ddl
    assert "key ITEMNO" in generated.ddl
    assert any("included anyway" in w for w in generated.warnings)


def test_hot_table_carries_a_trigger_load_warning(metadata):
    generated = generate_view_for_table(metadata.get_table("VBAP"))
    assert any("trigger" in w for w in generated.warnings)


def test_camel_case_element_style(metadata):
    naming = NamingConvention(element_style="camel")
    generated = generate_view_for_table(metadata.get_table("SNWD_SO_I"), naming=naming)
    assert "as ParentKey" in generated.ddl
    assert "as SoItemPos" in generated.ddl


def test_preserve_element_style_is_the_default(metadata):
    # Element names identical to DD03L field names keep the CDC mapping
    # unambiguous and make a generated view reviewable against the table.
    generated = generate_view_for_table(metadata.get_table("SNWD_SO_I"))
    assert "PARENT_KEY" in generated.ddl
    assert "as ParentKey" not in generated.ddl


# ---------------------------------------------------------------------------
# F-21 — CDC mapping builder
# ---------------------------------------------------------------------------


def test_mapping_completes_what_the_fixture_left_out(context):
    ctx = context("ZI_MISSING_MAPPED_KEY")
    proposal = build_cdc_mapping(ctx)
    item = next(e for e in proposal.entries if e.table == "ZORDERITEM")
    assert item.table_elements == ["ORDERID", "ITEMNO"]
    assert proposal.ok


def test_from_table_is_always_main(context):
    proposal = build_cdc_mapping(context("ZI_SALESORDER_CDC"))
    mains = [e for e in proposal.entries if e.is_main]
    assert [e.table for e in mains] == ["SNWD_SO"]


def test_configuration_tables_are_omitted_by_default(assess_ddl, context, metadata):
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    source = (
        "define view entity ZI_X as select from vbak as H"
        " left outer to one join t001 as C on C.bukrs = H.bukrs_vf"
        " { key H.vbeln as SalesOrder, H.bukrs_vf as CompanyCode,"
        "   C.bukrs as CompanyCodeKey, C.butxt as CompanyName }"
    )
    ctx = ValidationContext(view=parse_ddl(source, name_hint="ZI_X"), metadata=metadata)
    proposal = build_cdc_mapping(ctx)

    decision = proposal.decision_for("T001")
    assert decision is not None and decision.included is False
    assert "configuration" in decision.reason
    assert [e.table for e in proposal.entries] == ["VBAK"]


def test_user_selection_overrides_the_smart_default(metadata):
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    source = (
        "define view entity ZI_X as select from vbak as H"
        " left outer to one join t001 as C on C.bukrs = H.bukrs_vf"
        " { key H.vbeln as SalesOrder, H.bukrs_vf as CompanyCode,"
        "   C.bukrs as CompanyCodeKey, C.butxt as CompanyName }"
    )
    ctx = ValidationContext(view=parse_ddl(source, name_hint="ZI_X"), metadata=metadata)
    proposal = build_cdc_mapping(ctx, include={"VBAK", "T001"})
    assert {e.table for e in proposal.entries} == {"VBAK", "T001"}


def test_mapping_refuses_when_a_key_is_not_exposed(metadata):
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    source = (
        "define view entity ZI_X as select from zorderitem"
        " { key orderid as OrderId, material as Material }"
    )
    ctx = ValidationContext(view=parse_ddl(source, name_hint="ZI_X"), metadata=metadata)
    proposal = build_cdc_mapping(ctx)
    assert not proposal.ok
    assert "ITEMNO" in " ".join(proposal.problems)


def test_mapping_over_a_view_stack_resolves_to_the_base_table(context):
    """ZI_MAPPED_VIEW reads ZI_CUSTORDER, which reads ZCUSTORDER.

    A CDC mapping must address base tables, and almost every SAP view sits on
    another view — so refusing here (the previous behaviour) made the wrapper
    generator useless on exactly the content it exists for. Column lineage
    follows the element down to the table it really reads.
    """
    proposal = build_cdc_mapping(context("ZI_MAPPED_VIEW"))
    assert proposal.ok, proposal.problems

    main = next(e for e in proposal.entries if e.is_main)
    assert main.table == "ZCUSTORDER"          # the base table, not ZI_CUSTORDER
    assert main.table_elements == ["ORDERID"]  # the table's column
    assert main.view_elements == ["OrderId"]   # this view's element name


def test_mapping_over_a_stack_refuses_when_the_key_is_not_exposed(metadata):
    """A partial key cannot be mapped — it activates and fails at delta time."""
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    source = (
        "define view entity ZI_NO_KEY as select from ZI_CUSTORDER as O"
        " { key O.Customer as Customer, O.Amount as Amount }"
    )
    ctx = ValidationContext(view=parse_ddl(source, name_hint="ZI_NO_KEY"), metadata=metadata)
    proposal = build_cdc_mapping(ctx)
    assert not proposal.ok
    assert "whole key of ZCUSTORDER" in " ".join(proposal.problems)


def test_mapping_pairs_refuse_to_truncate():
    """A mismatched mapping must not come back looking complete.

    ``zip`` silently drops the tail, so an entry that had lost a key field
    would pair cleanly and report success — the exact failure this tool exists
    to catch, arriving through its own back door.
    """
    from cdcforge.cds import CdcMappingEntry

    lopsided = CdcMappingEntry(
        table="ZORDERITEM", role="MAIN",
        view_elements=["OrderId", "ItemNo"], table_elements=["ORDERID"],
    )
    assert lopsided.pairs == []

    matched = CdcMappingEntry(
        table="ZORDERITEM", role="MAIN",
        view_elements=["OrderId", "ItemNo"], table_elements=["ORDERID", "ITEMNO"],
    )
    assert matched.pairs == [("OrderId", "ORDERID"), ("ItemNo", "ITEMNO")]


def test_mandatory_elements_include_on_condition_keys(context):
    proposal = build_cdc_mapping(context("ZI_SALESORDER_CDC"))
    assert "SalesOrderGuid" in proposal.mandatory_elements
    assert "ItemParentGuid" in proposal.mandatory_elements


# ---------------------------------------------------------------------------
# F-20 — wrapper
# ---------------------------------------------------------------------------


def test_wrapper_over_a_clean_sap_view(context, metadata):
    ctx = context("I_VENDOR_RELEASED")
    base = validate_object("I_VENDOR_RELEASED", metadata)
    generated = generate_wrapper(ctx, base)
    assert generated.ok
    assert "as select from I_VENDOR_RELEASED" in generated.ddl
    assert "dataExtraction: { enabled: true" in generated.ddl


def test_the_mapping_generator_agrees_with_R_11_about_inner_joins(context, metadata):
    """The two used to contradict each other about the same view.

    R-11 was rewritten today to accept an inner join whose table the mapping
    covers, after 38 C1-released SAP views were found doing exactly that. The
    mapping generator kept its blanket "only LEFT OUTER TO ONE JOIN is
    supported" refusal — so the rule engine called I_ProductValuation
    reviewable while the generator declined to build a mapping for it.

    Refusing was also self-defeating: mapping the joined table is precisely
    what answers R-11's objection.
    """
    from cdcforge.generator.mapping import build_cdc_mapping
    from cdcforge.model import Outcome, Severity

    ctx = context("ZI_INNER_JOIN")
    assessment = validate_object("ZI_INNER_JOIN", metadata)
    assert not [
        r for r in assessment.results
        if r.severity is Severity.HARD and r.outcome is Outcome.VIOLATED
    ], "the rule engine does not hard-fail this view"

    proposal = build_cdc_mapping(ctx)
    refusals = [p for p in proposal.problems if "not supported" in p]
    assert not refusals, f"the generator must not refuse what R-11 allows: {refusals}"

    # ZI_INNER_JOIN_UNMAPPED exposes the header's own key, so there is
    # something to key a VBAK entry on. Mapping it is what makes a change to
    # the header raise a delta — the answer R-11 asks for.
    mapped = build_cdc_mapping(context("ZI_INNER_JOIN_UNMAPPED"))
    assert not [p for p in mapped.problems if "not supported" in p]
    assert any(e.table.upper() == "VBAK" for e in mapped.entries), (
        f"the inner-joined table belongs in the mapping; got "
        f"{[e.table for e in mapped.entries]} and {mapped.problems}"
    )


def test_a_right_outer_join_is_still_refused_by_the_mapping_generator(metadata):
    """No mapping entry can rescue a row with no main-table row behind it."""
    from cdcforge.generator.mapping import build_cdc_mapping
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    source = metadata.get_view_source("ZI_RIGHT_OUTER_JOIN")
    ctx = ValidationContext(
        view=parse_ddl(source, name_hint="ZI_RIGHT_OUTER_JOIN"), metadata=metadata
    )
    proposal = build_cdc_mapping(ctx)
    assert any("no corresponding row" in p for p in proposal.problems)


def test_a_wrapper_is_never_a_projection_view(context, metadata):
    """`as projection on` makes it a RAP transactional projection.

    SAP refuses to activate one that is not part of a business object —
    "Transactional Projection View must be part of a business object",
    SD_CDS_PC_TQ(009) — and that rejected *every* wrapper the tool produced,
    which is its main answer for standard SAP content. Nothing caught it until
    the generated DDL was put in front of a real system.
    """
    ctx = context("I_VENDOR_RELEASED")
    generated = generate_wrapper(
        ctx, validate_object("I_VENDOR_RELEASED", metadata)
    )
    assert "projection on" not in generated.ddl.lower()


def test_generated_wrapper_validates_clean(context, metadata):
    ctx = context("I_VENDOR_RELEASED")
    base = validate_object("I_VENDOR_RELEASED", metadata)
    generated = generate_wrapper(ctx, base)
    assessment = validate_source(generated.ddl, name=generated.name, metadata=metadata)
    assert assessment.verdict is not Verdict.FAIL_HARD


def test_wrapper_is_refused_over_an_aggregating_base(context, metadata):
    # A wrapper inherits the base's structure — annotations on top cannot fix it.
    ctx = context("ZI_AGGREGATE")
    base = validate_object("ZI_AGGREGATE", metadata)
    generated = generate_wrapper(ctx, base)
    assert not generated.ok
    assert "inherits the structure" in generated.refused_because


def test_wrapper_is_refused_over_a_union_base(context, metadata):
    ctx = context("ZI_UNION")
    generated = generate_wrapper(ctx, validate_object("ZI_UNION", metadata))
    assert not generated.ok


def test_wrapper_is_refused_over_a_to_many_join_base(context, metadata):
    ctx = context("ZI_TOMANY_JOIN")
    generated = generate_wrapper(ctx, validate_object("ZI_TOMANY_JOIN", metadata))
    assert not generated.ok


def test_wrapper_is_refused_over_an_unparseable_base(context, metadata):
    ctx = context("ZI_UNPARSEABLE")
    generated = generate_wrapper(ctx, validate_object("ZI_UNPARSEABLE", metadata))
    assert not generated.ok
    assert "could not be parsed" in generated.refused_because


def test_wrapper_carries_unresolved_base_findings_forward_as_warnings(context, metadata):
    ctx = context("ZI_UNSPECIFIED_JOIN")
    generated = generate_wrapper(ctx, validate_object("ZI_UNSPECIFIED_JOIN", metadata))
    if generated.ok:
        assert any("unresolved in the base view" in w for w in generated.warnings)


# ---------------------------------------------------------------------------
# F-25 — naming
# ---------------------------------------------------------------------------


def test_name_templates():
    naming = NamingConvention()
    assert naming.for_table("ZCUSTORDER") == "ZI_ZCUSTORDER"
    assert naming.for_wrapper("I_VENDOR") == "ZC_I_VENDOR_EX"


def test_namespaced_source_yields_a_legal_name():
    assert NamingConvention().for_table("/ACME/ORDERS") == "ZI_ACME_ORDERS"


def test_collision_with_an_existing_object_is_detected(metadata):
    check = check_name("ZI_CUSTORDER", metadata)
    assert not check.ok
    assert "already exists" in check.problem


def test_non_customer_namespace_is_rejected(metadata):
    assert not check_name("I_SOMETHING", metadata).ok


def test_name_too_long_is_rejected(metadata):
    assert not check_name("Z" + "X" * 40, metadata).ok


def test_batch_detects_a_name_generated_twice(metadata):
    preview = preview_names({"T_ONE": "ZI_SAME", "T_TWO": "ZI_SAME"}, metadata)
    assert not preview.ok
    assert any("also generated for" in c.problem for c in preview.problems)


def test_batch_of_distinct_new_names_is_clean(metadata):
    preview = preview_names({"ZORDERITEM": "ZI_ORDER_ITEM_X"}, metadata)
    assert preview.ok
