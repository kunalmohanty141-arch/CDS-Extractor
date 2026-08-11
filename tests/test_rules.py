"""One test per rule, positive and negative — §10 of the specification.

Every rule is exercised against a DDL fixture, and the two rules that depend on
runtime state (R-26 cardinality evidence, R-28 subscription state) get that
state injected here, since it cannot come from a file.
"""

from __future__ import annotations

import pytest

from cdcforge.model import Outcome, Severity, Verdict
from cdcforge.rules import (
    CardinalityEvidence,
    CardinalityResult,
    RuleConfig,
    SubscriptionState,
    all_rules,
    rule_ids,
)


def outcome(assessment, rule_id: str) -> Outcome | None:
    return assessment.outcome_of(rule_id)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_all_thirty_rules_are_registered():
    expected = {f"R-{n:02d}" for n in range(1, 31)}
    assert expected.issubset(set(rule_ids()))


def test_every_rule_declares_where_its_authority_comes_from():
    # F-12 requires each rule to name the SAP rule or KBA it derives from.
    for rule in all_rules():
        assert rule.spec.sap_source, f"{rule.id} has no SAP source"
        assert rule.spec.title


# ---------------------------------------------------------------------------
# R-01 … R-09 — annotations and forbidden constructs
# ---------------------------------------------------------------------------


def test_r01_missing_extraction_annotation(assess):
    assert outcome(assess("ZI_NO_EXTRACTION"), "R-01") is Outcome.VIOLATED


def test_r01_satisfied_when_present(assess):
    assert outcome(assess("ZI_BUSINESSAREA"), "R-01") is Outcome.SATISFIED


def test_r01_false_value_is_still_a_violation(assess_ddl):
    a = assess_ddl(
        "@Analytics.dataExtraction.enabled: false\n"
        "define view entity ZI_X as select from tgsb { key gsber as A }"
    )
    assert outcome(a, "R-01") is Outcome.VIOLATED


def test_r02_no_cdc_annotation(assess):
    assert outcome(assess("ZI_NO_CDC"), "R-02") is Outcome.VIOLATED


def test_r02_automatic_and_mapping_both_satisfy(assess):
    assert outcome(assess("ZI_BUSINESSAREA"), "R-02") is Outcome.SATISFIED
    assert outcome(assess("ZI_SALESORDER_CDC"), "R-02") is Outcome.SATISFIED


def test_r02_malformed_mapping_entry_is_reported(assess_ddl):
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.mapping: [ {role: #MAIN, viewElement: ['A']} ] }\n"
        "define view entity ZI_X as select from tgsb { key gsber as A }"
    )
    messages = " ".join(r.message for r in a.results_for("R-02"))
    assert "malformed" in messages and "'table'" in messages


def test_r03_aggregates(assess):
    a = assess("ZI_AGGREGATE")
    assert outcome(a, "R-03") is Outcome.VIOLATED
    assert {r.node for r in a.results_for("R-03")}


def test_r03_aggregate_deep_in_the_stack_is_found(assess_ddl, metadata):
    # ZI_AGGREGATE aggregates; a view reading it inherits the failure.
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true }\n"
        "define view entity ZI_ON_TOP as select from ZI_AGGREGATE as Agg"
        " { key Agg.Customer as Customer, Agg.TotalAmount as Total }",
        name="ZI_ON_TOP",
    )
    assert outcome(a, "R-03") is Outcome.VIOLATED
    assert a.verdict is Verdict.FAIL_HARD


def test_r04_group_by(assess):
    assert outcome(assess("ZI_AGGREGATE"), "R-04") is Outcome.VIOLATED


def test_r05_having(assess):
    assert outcome(assess("ZI_HAVING"), "R-05") is Outcome.VIOLATED


def test_r06_union(assess):
    assert outcome(assess("ZI_UNION"), "R-06") is Outcome.VIOLATED


def test_r07_distinct(assess):
    assert outcome(assess("ZI_DISTINCT"), "R-07") is Outcome.VIOLATED


def test_r08_parameters(assess):
    a = assess("ZI_PARAMETERS")
    assert outcome(a, "R-08") is Outcome.VIOLATED
    assert len(a.results_for("R-08")) == 2


def test_r09_table_function_in_the_stack(assess):
    a = assess("ZI_USES_TABLE_FUNC")
    assert outcome(a, "R-09") is Outcome.VIOLATED
    assert a.verdict is Verdict.FAIL_HARD


def test_r09_the_table_function_itself(assess):
    assert outcome(assess("ZI_TABLE_FUNC"), "R-09") is Outcome.VIOLATED


# ---------------------------------------------------------------------------
# R-10 … R-12 — join and association shape
# ---------------------------------------------------------------------------


def test_r10_to_many_join_is_a_hard_failure(assess):
    a = assess("ZI_TOMANY_JOIN")
    assert outcome(a, "R-10") is Outcome.VIOLATED
    assert a.verdict is Verdict.FAIL_HARD


def test_r10_unspecified_cardinality_is_review_not_pass_and_not_fail(assess):
    a = assess("ZI_UNSPECIFIED_JOIN")
    assert outcome(a, "R-10") is Outcome.INCONCLUSIVE
    assert a.verdict is Verdict.MANUAL_REVIEW


def test_r10_declared_to_one_is_accepted(assess):
    assert outcome(assess("ZI_SALESORDER_CDC"), "R-10") is Outcome.SATISFIED


def test_r11_accepts_an_inner_join_the_mapping_covers(assess):
    """Measured against SAP's own content, not read off the documentation.

    Of the 887 views S/4HANA ships with CDC delta declared, 38 use inner joins
    — every one C1-released — and 50 of those 57 joins target a table the
    view's own mapping covers. A mapped table carries its own trigger, so a
    change there raises its own delta and the join keyword decides nothing.
    Failing these outright hid good wrapper bases from F-09.
    """
    assert outcome(assess("ZI_INNER_JOIN"), "R-11") is Outcome.SATISFIED


def test_r11_refers_an_unmapped_inner_join_for_review(assess):
    """Not a PASS: an unmapped joined table raises no delta when it changes.

    Not a hard failure either — SAP does exactly this for customizing tables
    that never change in production. Ambiguity resolves to review, as always.
    """
    assert outcome(assess("ZI_INNER_JOIN_UNMAPPED"), "R-11") is Outcome.INCONCLUSIVE


def test_r11_still_fails_a_right_outer_join(assess):
    """No mapping can rescue this one, and no SAP delta view uses it.

    A right outer join emits rows for headers with no item, so the output
    contains records with no corresponding main-table row at all.
    """
    assert outcome(assess("ZI_RIGHT_OUTER_JOIN"), "R-11") is Outcome.VIOLATED


def test_r11_does_not_double_report_left_outer_joins(assess):
    # R-10 owns cardinality, R-11 owns join type. One defect, one finding.
    a = assess("ZI_TOMANY_JOIN")
    assert outcome(a, "R-11") is Outcome.SATISFIED


def test_r12_to_many_association_followed_in_projection(assess):
    assert outcome(assess("ZI_TOMANY_ASSOC"), "R-12") is Outcome.VIOLATED


def test_r12_exposed_to_many_association_is_not_a_violation(assess):
    """Regression: exposing a to-many association is not following it.

    Found by running against a real system. Conflating the two hard-failed
    almost every SAP standard view, including I_BusinessArea — the
    specification's own example of a *working* automatic-CDC view.
    """
    a = assess("ZI_EXPOSED_ASSOC")
    assert outcome(a, "R-12") is Outcome.SATISFIED
    assert a.verdict is Verdict.PASS


def test_r12_still_catches_a_followed_to_many_association(assess):
    # The fix must not blunt the rule: a path expression through a to-many
    # association is still a hard failure.
    assert outcome(assess("ZI_TOMANY_ASSOC"), "R-12") is Outcome.VIOLATED


def test_r12_unused_to_many_association_is_harmless(assess_ddl):
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true }\n"
        "define view entity ZI_X as select from zcustorder as H"
        " association [0..*] to zorderitem as _Item on _Item.orderid = H.orderid"
        " { key H.orderid as OrderId, H.customer as Customer }"
    )
    assert outcome(a, "R-12") is Outcome.SATISFIED


def test_r12_filtered_to_many_association_is_inconclusive(assess_ddl):
    """Measured against SAP's own delta content, 904 views.

    I_DFS_EquipmentBasicDEX and I_DFS_EquipmentDEX are both C1-released and
    both ship with CDC delta, and both follow an association declared [1..*]
    whose ON condition pins one status, one type and a validity window. CDS
    cannot say "to-many, but this filter makes it to-one", so SAP declares
    the pessimistic cardinality. A hard failure here would be wrong; a PASS
    would be a promise we cannot keep. Only data settles it.
    """
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true }\n"
        "define view entity ZI_X as select from zcustorder as H"
        " association [1..*] to zorderitem as _Item on _Item.orderid = H.orderid"
        "   and _Item.status = 'A'"
        " { key H.orderid as OrderId, _Item.descr as Descr }"
    )
    assert outcome(a, "R-12") is Outcome.INCONCLUSIVE
    assert a.verdict is Verdict.MANUAL_REVIEW


def test_r12_unfiltered_to_many_association_still_fails_hard(assess_ddl):
    # Same shape as above with the filter removed — nothing restricts the
    # target, so following it genuinely multiplies rows.
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true }\n"
        "define view entity ZI_X as select from zcustorder as H"
        " association [1..*] to zorderitem as _Item on _Item.orderid = H.orderid"
        " { key H.orderid as OrderId, _Item.descr as Descr }"
    )
    assert outcome(a, "R-12") is Outcome.VIOLATED


# ---------------------------------------------------------------------------
# R-13 … R-15 — key and foreign-key exposure
# ---------------------------------------------------------------------------


def test_r13_incomplete_main_key_mapping(assess):
    a = assess("ZI_MISSING_MAIN_KEY")
    assert outcome(a, "R-13") is Outcome.VIOLATED
    assert "ITEMNO" in " ".join(r.message for r in a.results_for("R-13"))


def test_r13_sees_a_key_exposed_through_a_cast(assess_ddl):
    """SAP's VDM wraps almost every key in a type-only cast.

    I_Product exposes MARA's key as
    `cast(mara.matnr as productnumber preserving type)`. Matching only bare
    field references reported the key as unexposed on most SAP standard views —
    a false finding on the single most important rule the tool has.
    """
    a = assess_ddl(
        "@Analytics: { dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true } }\n"
        "define view entity ZI_CASTKEY as select from zcustorder\n"
        "{ key cast(orderid as abap.char(12) preserving type) as OrderId,\n"
        "      customer as Customer }"
    )
    assert outcome(a, "R-13") is Outcome.SATISFIED


def test_r23_sees_a_client_field_hidden_behind_a_cast(assess_ddl):
    a = assess_ddl(
        "@Analytics: { dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true } }\n"
        "define view entity ZI_CASTCLIENT as select from zcustorder\n"
        "{ key cast(mandt as abap.clnt preserving type) as Client,\n"
        "  key orderid as OrderId }"
    )
    assert outcome(a, "R-23") is Outcome.VIOLATED


def test_r13_satisfied_on_a_complete_mapping(assess):
    assert outcome(assess("ZI_SALESORDER_CDC"), "R-13") is Outcome.SATISFIED


def test_r13_traces_exposure_when_there_is_no_mapping(assess_ddl):
    # Automatic path: exposure has to be traced through the projection.
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true }\n"
        "define view entity ZI_X as select from zorderitem"
        " { key orderid as OrderId, material as Material }"
    )
    assert outcome(a, "R-13") is Outcome.VIOLATED


def test_r14_incomplete_joined_table_key(assess):
    a = assess("ZI_MISSING_MAPPED_KEY")
    assert outcome(a, "R-14") is Outcome.VIOLATED
    assert "ITEMNO" in " ".join(r.message for r in a.results_for("R-14"))


def test_r14_not_applicable_without_a_mapping(assess):
    assert outcome(assess("ZI_BUSINESSAREA"), "R-14") is Outcome.NOT_APPLICABLE


def test_r07_allows_a_distinct_that_can_merge_nothing(assess):
    """The whole key is projected, so DISTINCT provably removes nothing.

    Rows differing in the key are never collapsed, and rows identical in every
    column including the key are ones CDC could not tell apart anyway. Banning
    the keyword outright flagged four of the six DISTINCT views S/4HANA ships
    with CDC delta declared, three of them C1-released.
    """
    assert outcome(assess("ZI_DISTINCT_SAFE"), "R-07") is Outcome.SATISFIED


def test_r07_still_fails_a_distinct_that_merges_base_records(assess):
    """ZORDERITEM's key is ORDERID + ITEMNO and only ORDERID is exposed.

    Two items of the same order collapse into one row — the real defect, and
    the shape I_CustSalesAreaTax has on TVKWZ, where WERKS is left out.
    """
    a = assess("ZI_DISTINCT")
    assert outcome(a, "R-07") is Outcome.VIOLATED
    assert "ITEMNO" in " ".join(r.message for r in a.results_for("R-07"))


def test_a_reused_context_gives_the_same_answer(metadata):
    """Passing a prebuilt context must be an optimisation, not a change.

    F-09 and the UI both need the dependency stack before validating — to know
    whether a candidate is rooted on the table, and to work out structural
    cardinality evidence. Letting validate_view build its own context walked
    that stack a second time for every object. The saving is only legitimate if
    the verdict is identical.
    """
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext, validate_view

    for name in ("ZI_SALESORDER_CDC", "ZI_INNER_JOIN", "ZI_MISSING_FK"):
        view = parse_ddl(metadata.get_view_source(name), name_hint=name)
        own = validate_view(
            view, metadata=metadata, object_meta=metadata.get_object(name)
        )
        ctx = ValidationContext(
            view=view, metadata=metadata, object_meta=metadata.get_object(name)
        )
        reused = validate_view(view, context=ctx)

        assert reused.verdict is own.verdict, name
        assert [(r.rule_id, r.outcome) for r in reused.results] == [
            (r.rule_id, r.outcome) for r in own.results
        ], name


def test_r15_one_exposed_side_is_enough(assess):
    """ZI_MISSING_FK exposes node_key but not parent_key, and they are equated.

    After the join the two hold the same value in every row, so the key CDC
    needs is in the output. Demanding both sides flagged 117 of the 123 joined
    views S/4HANA ships with CDC delta declared — a rule rejecting 95% of the
    vendor's own content is measuring the wrong thing.
    """
    assert outcome(assess("ZI_MISSING_FK"), "R-15") is Outcome.SATISFIED


def test_r15_violated_when_neither_side_is_exposed(assess):
    """The real defect: the join key is absent from the output entirely.

    No mapping can then work out which output row a change to the joined table
    belongs to.
    """
    a = assess("ZI_NO_JOIN_KEY")
    assert outcome(a, "R-15") is Outcome.VIOLATED
    message = " ".join(r.message for r in a.results_for("R-15"))
    assert "parent_key" in message and "node_key" in message


def test_r15_accepts_an_inner_join_that_exposes_its_key(assess):
    # ZI_INNER_JOIN equates VBAP.VBELN with VBAK.VBELN and exposes the first.
    assert outcome(assess("ZI_INNER_JOIN"), "R-15") is Outcome.SATISFIED


def test_r15_satisfied_when_both_sides_are_exposed(assess):
    assert outcome(assess("ZI_SALESORDER_CDC"), "R-15") is Outcome.SATISFIED


# ---------------------------------------------------------------------------
# R-16 … R-20 — mapping integrity
# ---------------------------------------------------------------------------


def test_r16_two_main_entries(assess):
    assert outcome(assess("ZI_TWO_MAIN"), "R-16") is Outcome.VIOLATED


def test_r16_no_main_entry(assess_ddl):
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.mapping: ["
        "  {table: 'ZCUSTORDER', role: #LEFT_OUTER_TO_ONE_JOIN,"
        "   viewElement: ['OrderId'], tableElement: ['ORDERID']} ] }\n"
        "define view entity ZI_X as select from zcustorder"
        " { key orderid as OrderId }"
    )
    assert outcome(a, "R-16") is Outcome.VIOLATED


def test_r17_view_key_does_not_match_main_key(assess):
    assert outcome(assess("ZI_KEY_MISMATCH"), "R-17") is Outcome.VIOLATED


def test_r17_no_key_elements_at_all(assess):
    a = assess("ZI_NO_PRIMARY_KEY")
    assert outcome(a, "R-17") is Outcome.VIOLATED


def test_r18_table_element_missing_and_non_key(assess):
    a = assess("ZI_BAD_TABLEELEMENT")
    messages = " ".join(r.message for r in a.results_for("R-18"))
    assert "does not exist" in messages
    assert "not a key field" in messages


def test_r18_inconclusive_without_table_metadata(assess_ddl):
    a = assess_ddl(
        "@Analytics.dataExtraction: { enabled: true,"
        " delta.changeDataCapture.mapping: ["
        "  {table: 'UNKNOWN_TAB', role: #MAIN,"
        "   viewElement: ['K'], tableElement: ['KF']} ] }\n"
        "define view entity ZI_X as select from tgsb { key gsber as K }"
    )
    assert outcome(a, "R-18") is Outcome.INCONCLUSIVE


def test_r19_view_element_does_not_exist(assess):
    assert outcome(assess("ZI_BAD_VIEWELEMENT"), "R-19") is Outcome.VIOLATED


def test_r20_mapping_names_a_view_not_a_table(assess):
    a = assess("ZI_MAPPED_VIEW")
    assert outcome(a, "R-20") is Outcome.VIOLATED
    assert "CDS view" in " ".join(r.message for r in a.results_for("R-20"))


# ---------------------------------------------------------------------------
# R-21 … R-25 — base tables, delta method, modifiability
# ---------------------------------------------------------------------------


def test_r21_cluster_table(assess):
    a = assess("ZI_CLUSTER_TABLE")
    assert outcome(a, "R-21") is Outcome.VIOLATED
    assert a.verdict is Verdict.FAIL_HARD


def test_r22_no_primary_key(assess):
    assert outcome(assess("ZI_NO_PRIMARY_KEY"), "R-22") is Outcome.VIOLATED


def test_r23_client_exposed_as_key(assess):
    assert outcome(assess("ZI_CLIENT_KEY"), "R-23") is Outcome.VIOLATED


def test_r23_satisfied_when_client_is_left_out(assess):
    assert outcome(assess("ZI_CUSTORDER"), "R-23") is Outcome.SATISFIED


def test_r24_timestamp_delta_only(assess):
    assert outcome(assess("ZI_BYELEMENT_DELTA"), "R-24") is Outcome.VIOLATED


def test_r25_released_sap_view_must_not_be_modified(assess):
    a = assess("I_VENDOR_RELEASED")
    assert outcome(a, "R-25") is Outcome.VIOLATED
    assert "C1" in " ".join(r.detail.get("api_state", "") for r in a.results_for("R-25"))


def test_r25_customer_object_is_modifiable(assess_ddl):
    from cdcforge.metadata import ApiState, ObjectMeta, Owner

    a = assess_ddl(
        "define view entity ZI_NEEDS_FIXING as select from zcustorder"
        " { key orderid as OrderId }",
        name="ZI_NEEDS_FIXING",
        object_meta=ObjectMeta(
            name="ZI_NEEDS_FIXING", owner=Owner.CUSTOMER,
            api_state=ApiState.NOT_RELEASED,
        ),
    )
    assert outcome(a, "R-25") is Outcome.SATISFIED


def test_an_unreleased_sap_view_is_still_never_modifiable():
    """Policy, stricter than the specification: the tool never edits, and never
    offers to edit, a standard SAP object — released or not.

    "Technically modifiable" is a trap: the change survives until the next
    upgrade overwrites it, the extraction stops, and nobody connects the two.
    """
    from cdcforge.metadata import ApiState, ObjectMeta, Owner

    for state in (ApiState.NOT_RELEASED, ApiState.C0, ApiState.C3, ApiState.UNKNOWN):
        meta = ObjectMeta(name="I_SAP_VIEW", owner=Owner.SAP, api_state=state)
        assert not meta.is_modifiable, f"{state} must not be modifiable"
        assert "wrapper only" in meta.modifiability_reason


def test_r25_is_silent_when_nothing_needs_changing(assess_ddl):
    """Modifiability decides *which fix to offer*, not whether CDC works.

    A released SAP view that already declares extraction and CDC needs no edit,
    so whether it may be edited is beside the point. Reporting 'must not be
    modified' about a view nobody was going to modify buries the real findings —
    it was 7 of 8 findings on a real sample.
    """
    from cdcforge.metadata import ApiState, ObjectMeta, Owner

    released_and_complete = (
        "@Analytics: { dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true } }\n"
        "define view entity I_ALREADY_FINE as select from tgsb"
        " { key gsber as BusinessArea }"
    )
    a = assess_ddl(
        released_and_complete,
        name="I_ALREADY_FINE",
        object_meta=ObjectMeta(
            name="I_ALREADY_FINE", owner=Owner.SAP, api_state=ApiState.C1
        ),
    )
    assert outcome(a, "R-25") is Outcome.NOT_APPLICABLE
    assert a.verdict is Verdict.PASS


def test_r25_still_fires_when_an_in_place_fix_would_be_needed(assess_ddl):
    from cdcforge.metadata import ApiState, ObjectMeta, Owner

    missing_extraction = (
        "define view entity I_NEEDS_FIXING as select from tgsb"
        " { key gsber as BusinessArea }"
    )
    a = assess_ddl(
        missing_extraction,
        name="I_NEEDS_FIXING",
        object_meta=ObjectMeta(
            name="I_NEEDS_FIXING", owner=Owner.SAP, api_state=ApiState.C1
        ),
    )
    assert outcome(a, "R-25") is Outcome.VIOLATED
    assert "wrapper" in " ".join(r.remediation for r in a.results_for("R-25"))


def test_released_state_without_a_level_still_forbids_modification():
    """An APIS object proves release; the C0…C3 level is not reachable.

    The action is the same either way, so the missing level must not become an
    excuse to modify.
    """
    from cdcforge.metadata import ApiState, ObjectMeta, Owner

    meta = ObjectMeta(name="I_X", owner=Owner.SAP, api_state=ApiState.RELEASED)
    assert ApiState.RELEASED.forbids_modification
    assert not meta.is_modifiable


def test_r25_unknown_state_is_inconclusive_not_permissive(assess_ddl):
    # Fail safe: where the release state cannot be determined, assume
    # unmodifiable rather than assuming it is fine to edit.
    a = assess_ddl(
        "define view entity I_SOMETHING_UNKNOWN as select from tgsb"
        " { key gsber as A }",
        name="I_SOMETHING_UNKNOWN",
    )
    assert outcome(a, "R-25") is Outcome.INCONCLUSIVE


# ---------------------------------------------------------------------------
# R-26 / R-28 — rules that need injected runtime state
# ---------------------------------------------------------------------------


def test_r26_declared_only_is_review_by_default(assess):
    a = assess("ZI_SALESORDER_CDC")
    assert outcome(a, "R-26") is Outcome.INCONCLUSIVE
    assert a.verdict is Verdict.MANUAL_REVIEW


def test_r26_proven_cardinality_lets_the_view_pass(assess):
    evidence = {
        "Item": CardinalityEvidence("Item", "SNWD_SO_I", CardinalityResult.PROVEN_TO_ONE, 1),
        "Product": CardinalityEvidence("Product", "SNWD_PD", CardinalityResult.PROVEN_TO_ONE, 1),
    }
    a = assess("ZI_SALESORDER_CDC", cardinality_evidence=evidence)
    assert outcome(a, "R-26") is Outcome.SATISFIED
    assert a.verdict is Verdict.PASS


def test_r26_disproven_cardinality_is_a_hard_failure_with_the_key(assess):
    evidence = {
        "Item": CardinalityEvidence(
            "Item", "SNWD_SO_I", CardinalityResult.VIOLATED, 3, "4711"
        )
    }
    a = assess("ZI_SALESORDER_CDC", cardinality_evidence=evidence)
    result = next(r for r in a.results_for("R-26") if r.outcome is Outcome.VIOLATED)
    assert result.severity is Severity.HARD
    assert "3 rows" in result.message and "4711" in result.message
    assert a.verdict is Verdict.FAIL_HARD


def test_r26_can_be_waived_by_configuration_but_still_states_the_risk(assess):
    a = assess(
        "ZI_SALESORDER_CDC",
        config=RuleConfig(require_cardinality_evidence=False),
    )
    assert outcome(a, "R-26") is Outcome.SATISFIED
    assert a.verdict is Verdict.PASS
    assert "not been proven" in " ".join(r.message for r in a.results_for("R-26"))


def test_r28_unknown_subscription_blocks_writes_without_changing_the_verdict(assess):
    a = assess("ZI_BUSINESSAREA")
    assert a.verdict is Verdict.PASS
    assert a.write_blocked is True
    assert outcome(a, "R-28") is Outcome.INCONCLUSIVE


def test_r28_active_subscription_blocks(assess):
    a = assess("ZI_BUSINESSAREA", subscription_state=SubscriptionState.ACTIVE)
    assert outcome(a, "R-28") is Outcome.VIOLATED
    assert a.write_blocked is True
    assert "re-initialisation" in " ".join(r.message for r in a.results_for("R-28"))


def test_r28_no_subscription_releases_the_block(assess):
    a = assess("ZI_BUSINESSAREA", subscription_state=SubscriptionState.NONE)
    assert a.write_blocked is False
    assert a.verdict is Verdict.PASS


# ---------------------------------------------------------------------------
# R-27 / R-29 / R-30
# ---------------------------------------------------------------------------


def test_r27_unresolved_branch(assess):
    assert outcome(assess("ZI_USES_TABLE_FUNC"), "R-27") is Outcome.INCONCLUSIVE


def test_r27_clean_stack(assess):
    assert outcome(assess("ZI_BUSINESSAREA"), "R-27") is Outcome.SATISFIED


def test_r29_deep_stack(assess):
    a = assess("ZI_DEEP_L1")
    assert outcome(a, "R-29") is Outcome.VIOLATED
    # The message must be honest that the threshold is the tool's, not SAP's.
    assert "not an SAP number" in " ".join(r.message for r in a.results_for("R-29"))


def test_r29_threshold_is_configurable(assess):
    a = assess("ZI_DEEP_L1", config=RuleConfig(manual_review_stack_depth=10))
    assert outcome(a, "R-29") is Outcome.SATISFIED


def test_r30_row_count_warns_without_failing(assess):
    a = assess("ZI_BIG_TABLE")
    assert outcome(a, "R-30") is Outcome.VIOLATED
    assert a.verdict is Verdict.PASS  # a warning must never move the verdict


# ---------------------------------------------------------------------------
# Engine behaviour
# ---------------------------------------------------------------------------


def test_unparseable_runs_no_rules_at_all(assess):
    a = assess("ZI_UNPARSEABLE")
    assert a.verdict is Verdict.UNPARSEABLE
    assert a.results == []
    assert any(i.fatal for i in a.parse_issues)


def test_a_rule_that_raises_becomes_inconclusive_not_a_crash(context):
    # A skipped rule reads as a passed rule, so a crashing rule must surface as
    # INCONCLUSIVE rather than vanishing or taking the assessment down.
    from cdcforge.rules.base import Rule, get_rule

    def boom(ctx, spec):
        raise RuntimeError("synthetic failure")

    broken = Rule(spec=get_rule("R-01").spec, fn=boom)
    results = broken.run(context("ZI_BUSINESSAREA"))
    assert results[0].outcome is Outcome.INCONCLUSIVE
    assert "synthetic failure" in results[0].message
    assert results[0].verdict_contribution() is not Verdict.PASS


@pytest.mark.parametrize("name", ["ZI_BUSINESSAREA", "ZI_SALESORDER_CDC", "ZI_CUSTORDER"])
def test_valid_fixtures_never_produce_a_hard_failure(assess, name):
    a = assess(name)
    assert a.verdict is not Verdict.FAIL_HARD
