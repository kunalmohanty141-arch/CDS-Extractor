"""Dependency resolution — F-08.

Diamonds, cycles, the depth cap, and the table-function blind spot.
"""

from __future__ import annotations

from cdcforge.metadata.base import MetadataSource, NullMetadataSource
from cdcforge.metadata.types import FieldMeta, TableClass, TableMeta
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.rules import RuleConfig, resolve_stack
from cdcforge.rules.stack import NodeKind


class DictMetadata(MetadataSource):
    """A metadata source assembled inline, for graph-shape tests."""

    name = "inline"

    def __init__(self, views: dict[str, str], tables: list[str] | None = None):
        self.views = {k.upper(): v for k, v in views.items()}
        self.tables = {
            t.upper(): TableMeta(
                name=t.upper(),
                table_class=TableClass.TRANSPARENT,
                fields=[FieldMeta("K", is_key=True)],
            )
            for t in (tables or [])
        }

    def get_table(self, name):
        return self.tables.get(name.upper())

    def get_view_source(self, name):
        return self.views.get(name.upper())

    def get_object(self, name):
        return None

    def list_views(self):
        return sorted(self.views)

    def list_tables(self):
        return sorted(self.tables)


def view(name: str, source: str) -> str:
    return f"define view entity {name} as select from {source} {{ key K }}"


def test_leaf_tables_are_resolved(metadata):
    root = parse_ddl(metadata.get_view_source("ZI_SALESORDER_CDC"), name_hint="ZI_SALESORDER_CDC")
    stack = resolve_stack(root, metadata)
    assert set(stack.table_names) == {"SNWD_SO", "SNWD_SO_I", "SNWD_PD"}
    assert stack.is_fully_resolved


def test_diamond_is_visited_once():
    md = DictMetadata(
        {
            "V_TOP": "define view entity V_TOP as select from V_LEFT"
            " left outer to one join V_RIGHT on V_RIGHT.K = V_LEFT.K { key V_LEFT.K as K }",
            "V_LEFT": view("V_LEFT", "T_BASE"),
            "V_RIGHT": view("V_RIGHT", "T_BASE"),
        },
        tables=["T_BASE"],
    )
    root = parse_ddl(md.get_view_source("V_TOP"), name_hint="V_TOP")
    stack = resolve_stack(root, md)

    base = stack.node("T_BASE")
    assert base is not None and base.kind is NodeKind.TABLE
    assert sorted(base.parents) == ["V_LEFT", "V_RIGHT"]
    assert stack.table_names == ["T_BASE"]


def test_cycle_is_recorded_and_does_not_recurse_forever():
    md = DictMetadata(
        {"V_A": view("V_A", "V_B"), "V_B": view("V_B", "V_A")}
    )
    root = parse_ddl(md.get_view_source("V_A"), name_hint="V_A")
    stack = resolve_stack(root, md)
    assert stack.cycles
    assert not stack.is_fully_resolved


def test_depth_cap_is_hit_and_reported():
    sources = {f"V_{i}": view(f"V_{i}", f"V_{i + 1}") for i in range(12)}
    sources["V_12"] = view("V_12", "T_BASE")
    md = DictMetadata(sources, tables=["T_BASE"])
    root = parse_ddl(md.get_view_source("V_0"), name_hint="V_0")
    stack = resolve_stack(root, md, max_depth=4)
    assert stack.depth_cap_hit
    assert not stack.is_fully_resolved


def test_node_budget_truncates_a_wide_stack_and_says_so():
    """Breadth, not depth, is what makes real SAP stacks unwalkable.

    A custom Z-view resolves into a handful of objects. An SAP extraction view
    fans out into thousands, and against a live system each one is an HTTP
    round-trip — a single view ran to 9,480 calls without finishing.
    """
    # One view reading fifty others, each reading a table: wide, not deep.
    sources = {
        "V_WIDE": "define view entity V_WIDE as select from V_0 "
        + " ".join(
            f"left outer to one join V_{i} as A{i} on A{i}.K = V_0.K"
            for i in range(1, 50)
        )
        + " { key V_0.K as K }"
    }
    for i in range(50):
        sources[f"V_{i}"] = view(f"V_{i}", f"T_{i}")
    md = DictMetadata(sources, tables=[f"T_{i}" for i in range(50)])

    root = parse_ddl(md.get_view_source("V_WIDE"), name_hint="V_WIDE")

    full = resolve_stack(root, md, max_nodes=1000)
    assert not full.node_budget_hit
    assert full.is_fully_resolved

    truncated = resolve_stack(root, md, max_nodes=20)
    assert truncated.node_budget_hit
    assert not truncated.is_fully_resolved
    assert "TRUNCATED" in truncated.describe()

    # The budget bounds the *walking*, not the node count exactly. A node that
    # trips the budget is still recorded — so the user can see the branch
    # exists — but its source is never fetched, which is where the cost is.
    # Overshoot is therefore bounded by the fan-out of the level that tripped
    # it, and costs nothing.
    assert len(truncated.views) < len(full.views)
    assert len(truncated.views) <= 25
    unwalked = [n for n in truncated.unresolved if "budget" in n.reason]
    assert unwalked, "branches skipped for budget must be recorded, not dropped"


def test_a_truncated_stack_is_never_reported_as_clean(assess):
    """R-27 must object. An unwalked branch is one whose constructs were never
    checked, so no rule above it can be trusted."""
    from cdcforge.model import Outcome, Verdict

    a = assess("ZI_SALESORDER_CDC", config=RuleConfig(max_stack_nodes=1))
    assert a.outcome_of("R-27") is Outcome.INCONCLUSIVE
    assert a.verdict is not Verdict.PASS
    assert "never examined" in " ".join(r.message for r in a.results_for("R-27"))


def test_table_function_branch_terminates_as_unresolved(metadata):
    source = metadata.get_view_source("ZI_USES_TABLE_FUNC")
    stack = resolve_stack(parse_ddl(source, name_hint="ZI_USES_TABLE_FUNC"), metadata)
    node = stack.node("ZI_TABLE_FUNC")
    assert node is not None and node.kind is NodeKind.TABLE_FUNCTION
    assert "opaque" in node.reason
    assert not stack.is_fully_resolved


def test_unknown_object_is_unresolved_not_assumed_to_be_a_table():
    md = DictMetadata({"V_A": view("V_A", "T_MYSTERY")})
    stack = resolve_stack(parse_ddl(md.get_view_source("V_A"), name_hint="V_A"), md)
    node = stack.node("T_MYSTERY")
    assert node.kind is NodeKind.UNRESOLVED
    assert stack.leaf_tables == []


def test_with_no_metadata_at_all_nothing_is_invented():
    root = parse_ddl("define view entity V as select from tgsb { key gsber as A }")
    stack = resolve_stack(root, NullMetadataSource())
    assert stack.leaf_tables == []
    assert len(stack.unresolved) == 1


def test_followed_association_targets_are_walked():
    md = DictMetadata(
        {
            "V_A": "define view entity V_A as select from T_MAIN"
            " association [0..1] to V_SIDE as _S on _S.K = T_MAIN.K"
            " { key T_MAIN.K as K, _S.K as SK }",
            "V_SIDE": view("V_SIDE", "T_SIDE"),
        },
        tables=["T_MAIN", "T_SIDE"],
    )
    stack = resolve_stack(parse_ddl(md.get_view_source("V_A"), name_hint="V_A"), md)
    assert set(stack.table_names) == {"T_MAIN", "T_SIDE"}


def test_unfollowed_association_targets_are_not_walked():
    """A declared-but-unfollowed association contributes no columns.

    Walking it anyway pulled SAP's whole business-partner and address graph
    into a sales-pricing extraction view's stack, and the forbidden-construct
    rules then hard-failed it on an aggregation four views away that it never
    reads — a view SAP itself ships as delta-capable.
    """
    md = DictMetadata(
        {
            "V_A": "define view entity V_A as select from T_MAIN"
            " association [0..1] to V_AGG as _Agg on _Agg.K = T_MAIN.K"
            " { key T_MAIN.K as K }",  # _Agg declared, never followed
            "V_AGG": "define view entity V_AGG as select from T_OTHER"
            " { key T_OTHER.K as K, sum(T_OTHER.V) as Total } group by T_OTHER.K",
        },
        tables=["T_MAIN", "T_OTHER"],
    )
    root = parse_ddl(md.get_view_source("V_A"), name_hint="V_A")
    stack = resolve_stack(root, md)

    assert stack.node("V_AGG") is None, "an unfollowed association was walked"
    assert set(stack.table_names) == {"T_MAIN"}

    # The relationship is still visible for the where-used graph.
    assert "V_AGG" in root.all_referenced_objects
    assert "V_AGG" not in root.data_path_objects


def test_an_unfollowed_aggregating_association_does_not_fail_the_view(assess):
    from cdcforge.model import Outcome, Verdict

    a = assess("ZI_EXPOSED_ASSOC")
    assert a.outcome_of("R-03") is Outcome.SATISFIED
    assert a.verdict is Verdict.PASS


def test_render_tree_marks_repeats_rather_than_expanding_them():
    md = DictMetadata(
        {
            "V_TOP": "define view entity V_TOP as select from V_LEFT"
            " left outer to one join V_RIGHT on V_RIGHT.K = V_LEFT.K { key V_LEFT.K as K }",
            "V_LEFT": view("V_LEFT", "T_BASE"),
            "V_RIGHT": view("V_RIGHT", "T_BASE"),
        },
        tables=["T_BASE"],
    )
    tree = resolve_stack(parse_ddl(md.get_view_source("V_TOP"), name_hint="V_TOP"), md)
    rendered = tree.render_tree()
    assert "already shown" in rendered


def test_config_depth_cap_is_used_by_the_engine(assess):
    a = assess("ZI_DEEP_L1", config=RuleConfig(max_stack_depth=2))
    assert a.stack.depth_cap_hit
