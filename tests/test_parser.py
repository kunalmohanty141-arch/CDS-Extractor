"""Parser tests — F-11.

Both view syntaxes, joins with and without declared cardinality, associations,
parameters, set operations, and the UNPARSEABLE outcome.
"""

from __future__ import annotations

from cdcforge.parsing.annotations import EnumValue
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.parsing.nodes import EntityKind, JoinCardinality, JoinType


def test_view_entity_is_recognised():
    view = parse_ddl("define view entity ZI_X as select from tgsb { key gsber as A }")
    assert view.entity_kind is EntityKind.VIEW_ENTITY
    assert view.name == "ZI_X"
    assert view.from_source.name == "tgsb"
    assert not view.has_fatal_issue


def test_left_outer_to_exact_one_join():
    """``TO EXACT ONE`` is a to-one shape, and SAP writes it.

    For CDC the only question is whether the join can multiply main-table rows,
    and this cannot — it additionally promises a match exists. Not handling the
    keyword made three of SAP's own delta views UNPARSEABLE.
    """
    view = parse_ddl(
        "define view entity ZI_X as select from vbap as i\n"
        "  left outer to exact one join vbak as h on h.vbeln = i.vbeln\n"
        "{ key i.vbeln as A, h.erdat as B }"
    )
    assert not view.has_fatal_issue
    assert view.joins[0].join_type is JoinType.LEFT_OUTER
    assert view.joins[0].join_cardinality is JoinCardinality.TO_ONE
    assert view.joins[0].name == "vbak"


def test_association_cardinality_written_as_a_keyword():
    """``association to one X as _Y`` rather than ``association [0..1] to X``.

    Reading "one" as the target entity left the real target unconsumed and the
    projection list unreachable.
    """
    view = parse_ddl(
        "define view entity ZI_X as select from vbak\n"
        "  association to one I_Currency as _Currency on $projection.C = _Currency.C\n"
        "{ key vbeln as A, waerk as C }"
    )
    assert not view.has_fatal_issue
    assoc = view.associations[0]
    assert assoc.name == "_Currency"
    assert assoc.target == "I_Currency"
    assert assoc.cardinality.is_specified
    assert not assoc.cardinality.is_to_many


def test_association_with_both_textual_cardinality_forms():
    """SAP writes ``association of one to one X`` — both forms at once.

    The keyword after TO must be consumed even when a cardinality was already
    read, or it is taken as the target entity.
    """
    view = parse_ddl(
        "define view entity ZI_X as select from vbak\n"
        "  association of one to one E_Agreement as _Ext on $projection.A = _Ext.A\n"
        "{ key vbeln as A }"
    )
    assert not view.has_fatal_issue
    assoc = view.associations[0]
    assert assoc.name == "_Ext"
    assert assoc.target == "E_Agreement"
    assert not assoc.cardinality.is_to_many


def test_a_to_many_association_keyword_is_still_to_many():
    view = parse_ddl(
        "define view entity ZI_X as select from vbak\n"
        "  association to many I_Item as _Item on $projection.A = _Item.A\n"
        "{ key vbeln as A }"
    )
    assert not view.has_fatal_issue
    assert view.associations[0].cardinality.is_to_many


def test_classic_view_and_sql_view_name():
    view = parse_ddl(
        "@AbapCatalog.sqlViewName: 'ZVX'\n"
        "define view ZI_X as select from tgsb { key gsber as A }"
    )
    assert view.entity_kind is EntityKind.CLASSIC_VIEW
    assert view.sql_view_name == "ZVX"


def test_projection_view():
    view = parse_ddl("define view entity ZC_X as projection on ZI_X { key A }")
    assert view.entity_kind is EntityKind.PROJECTION_VIEW
    assert view.from_source.name == "ZI_X"


def test_table_function_is_recognised_without_a_select():
    view = parse_ddl(
        "define table function ZI_TF returns { a : abap.char(1); }"
        " implemented by method cl=>m;"
    )
    assert view.entity_kind is EntityKind.TABLE_FUNCTION
    assert not view.has_fatal_issue


def test_join_kinds_and_cardinality():
    view = parse_ddl(
        """
        define view entity ZI_X as select from a
          left outer to one join b on b.k = a.k
          left outer to many join c on c.k = a.k
          left outer join d on d.k = a.k
          inner join e on e.k = a.k
          cross join f
        { key a.k as K }
        """
    )
    kinds = [(j.name, j.join_type, j.join_cardinality) for j in view.joins]
    assert kinds == [
        ("b", JoinType.LEFT_OUTER, JoinCardinality.TO_ONE),
        ("c", JoinType.LEFT_OUTER, JoinCardinality.TO_MANY),
        ("d", JoinType.LEFT_OUTER, JoinCardinality.UNSPECIFIED),
        ("e", JoinType.INNER, JoinCardinality.UNSPECIFIED),
        ("f", JoinType.CROSS, JoinCardinality.UNSPECIFIED),
    ]


def test_on_condition_field_references_are_captured():
    view = parse_ddl(
        "define view entity ZI_X as select from a"
        " left outer to one join b on b.parent = a.id and b.mandt = a.mandt"
        " { key a.id as Id }"
    )
    refs = {str(r) for r in view.joins[0].on_refs}
    assert refs == {"b.parent", "a.id", "b.mandt", "a.mandt"}


def test_association_cardinality_and_usage():
    view = parse_ddl(
        """
        define view entity ZI_X as select from a
          association [0..*] to b as _B on _B.k = a.k
          association [0..1] to c as _C on _C.k = a.k
        { key a.k as K, _B.name as BName }
        """
    )
    assoc = view.association_map
    assert assoc["_B"].cardinality.is_to_many
    assert assoc["_C"].cardinality.is_to_one
    # _C is declared but never followed — declaring a to-many is harmless.
    assert view.used_association_names == {"_B"}


def test_parameters_are_parsed_and_do_not_break_the_projection():
    view = parse_ddl(
        "define view entity ZI_X with parameters p1 : abap.dats, p2 : abap.cuky(5)"
        " as select from a { key a.k as K, a.v as V }"
    )
    assert [p.name for p in view.parameters] == ["p1", "p2"]
    assert [i.name for i in view.select_items] == ["K", "V"]


def test_aggregates_are_found_only_when_called():
    view = parse_ddl(
        "define view entity ZI_X as select from a"
        " { key a.k as K, sum(a.v) as Total, a.max_value as MaxValue }"
    )
    assert [name for name, _ in view.aggregates] == ["SUM"]


def test_case_expression_does_not_confuse_the_element_split():
    view = parse_ddl(
        """
        define view entity ZI_X as select from a
        {
          key a.k as K,
              case when a.v = 'x' then 'one, two' else 'three' end as Label,
              a.w as W
        }
        """
    )
    assert [i.name for i in view.select_items] == ["K", "Label", "W"]


def test_set_operation_is_recorded_and_branch_sources_kept():
    view = parse_ddl(
        "define view entity ZI_X as select from a { key a.k as K }"
        " union all select from b { key b.k as K }"
    )
    assert [label for label, _ in view.set_operations] == ["UNION ALL"]
    assert [s.name for s in view.union_sources] == ["b"]
    # Union branches stay out of the join list so R-10/R-11 do not double-report.
    assert view.joins == []


def test_annotations_normalise_across_writing_styles():
    dotted = parse_ddl(
        "@Analytics.dataExtraction.enabled: true\n"
        "define view entity ZI_A as select from t { key t.k as K }"
    )
    nested = parse_ddl(
        "@Analytics: { dataExtraction: { enabled: true } }\n"
        "define view entity ZI_B as select from t { key t.k as K }"
    )
    mixed = parse_ddl(
        "@Analytics.dataExtraction: { enabled: true }\n"
        "define view entity ZI_C as select from t { key t.k as K }"
    )
    for view in (dotted, nested, mixed):
        assert view.annotations.is_true("analytics.dataextraction.enabled")


def test_annotation_enum_and_array_values():
    view = parse_ddl(
        "@Analytics: { dataCategory: #FACT,"
        " dataExtraction.delta.changeDataCapture.mapping: ["
        "  {table: 'T1', role: #MAIN, viewElement: ['A'], tableElement: ['B']} ] }\n"
        "define view entity ZI_X as select from t { key t.k as A }"
    )
    assert view.annotations.get("analytics.datacategory") == EnumValue("FACT")
    mapping = view.annotations.get(
        "analytics.dataextraction.delta.changedatacapture.mapping"
    )
    assert mapping[0]["table"] == "T1"
    assert mapping[0]["role"] == EnumValue("MAIN")


def test_enum_collection_annotation_value():
    """``#('A','B')`` — found in SAP's delivered C_SalesDocItmPrcgElmntDEX_1.

    An unfamiliar-but-valid construct must not make a whole view UNPARSEABLE.
    """
    view = parse_ddl(
        "@AccessControl:{ authorizationCheck: #CHECK,\n"
        "  personalData.blocking: #('TRANSACTIONAL_DATA') }\n"
        "define view entity ZI_X as select from t { key t.k as K }"
    )
    assert not view.has_fatal_issue
    assert view.annotations.get("accesscontrol.personaldata.blocking") == [
        "TRANSACTIONAL_DATA"
    ]
    assert view.annotations.get("accesscontrol.authorizationcheck") == EnumValue("CHECK")


def test_empty_and_multi_member_enum_collections():
    view = parse_ddl(
        "@A.b: #('ONE', 'TWO')\n@A.c: #()\n"
        "define view entity ZI_X as select from t { key t.k as K }"
    )
    assert not view.has_fatal_issue
    assert view.annotations.get("a.b") == ["ONE", "TWO"]
    assert view.annotations.get("a.c") == []


def test_a_bare_hash_is_still_an_error():
    # The tolerance is for '#(' specifically, not for '#' in general.
    view = parse_ddl(
        "@A.b: # \ndefine view entity ZI_X as select from t { key t.k as K }"
    )
    assert view.has_fatal_issue


def test_annotation_names_are_case_insensitive():
    view = parse_ddl(
        "@ANALYTICS.DATAEXTRACTION.ENABLED: true\n"
        "define view entity ZI_X as select from t { key t.k as K }"
    )
    assert view.annotations.is_true("analytics.dataextraction.enabled")


def test_key_marking_and_aliases():
    view = parse_ddl(
        "define view entity ZI_X as select from a"
        " { key a.k as K, a.v as V, a.w }"
    )
    assert [i.name for i in view.key_items] == ["K"]
    assert view.element_names == ["K", "V", "w"]


def test_unparseable_returns_a_fatal_issue_not_an_exception():
    view = parse_ddl("define view entity ZI_X as select from a { key 'unterminated }")
    assert view.has_fatal_issue
    assert "unterminated string literal" in view.issues[0].message


def test_missing_projection_is_fatal():
    view = parse_ddl("define view entity ZI_X as select from a")
    assert view.has_fatal_issue


def test_comments_and_literals_do_not_create_phantom_clauses():
    view = parse_ddl(
        """
        // group by customer
        /* union all */
        define view entity ZI_X as select from a
        { key a.k as K, 'group by' as Note }
        """
    )
    assert view.group_by_ref is None
    assert view.set_operations == []
    assert not view.has_distinct
