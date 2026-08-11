"""F-08 — the view dependency graph.

Resolve a view down to its leaf tables. Diamonds are handled with a visited
set, cycles with an in-progress set and a depth cap, and every node is
memoised.

The important part is what happens at the edges. Appendix D.3 is blunt: the
metadata objects do not reliably represent table functions, some generated
providers, or calculated columns, and dependency resolution through a table
function is the biggest blind spot. So any branch that cannot be walked
terminates as UNRESOLVED and propagates MANUAL_REVIEW up the tree (R-27).
It does not quietly terminate as "leaf table".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import TableClass, TableMeta
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.parsing.nodes import EntityKind, ParsedView


class NodeKind(str, Enum):
    VIEW = "VIEW"
    """A CDS view whose source was found and parsed."""

    TABLE = "TABLE"
    """A transparent DDIC table — a genuine leaf."""

    TABLE_FUNCTION = "TABLE_FUNCTION"
    """AMDP-implemented. Opaque by construction (KBA 2884410)."""

    UNRESOLVED = "UNRESOLVED"
    """Could not be walked. The reason is recorded and surfaced to the user."""


@dataclass
class StackNode:
    name: str
    kind: NodeKind
    depth: int
    view: ParsedView | None = None
    table: TableMeta | None = None
    parents: list[str] = field(default_factory=list)
    reason: str = ""
    """Why this node is UNRESOLVED, in plain English."""


@dataclass
class ViewStack:
    """The resolved dependency tree for one root view."""

    root_name: str
    nodes: dict[str, StackNode] = field(default_factory=dict)
    max_depth_reached: int = 0
    depth_cap_hit: bool = False
    node_budget_hit: bool = False
    """The walk stopped because it had visited too many objects.

    Depth was never the binding constraint on real content — breadth is. A
    custom Z-view resolves into a handful of objects; an SAP standard
    extraction view fans out into thousands, and walking it over ADT means
    thousands of round-trips. Truncating and saying so beats running for hours,
    and beats the alternative of quietly walking a partial tree and reporting
    the result as if it were complete.
    """

    cycles: list[tuple[str, str]] = field(default_factory=list)

    # -- accessors --------------------------------------------------------
    @property
    def root(self) -> StackNode | None:
        return self.nodes.get(self.root_name.upper())

    @property
    def views(self) -> list[StackNode]:
        return [n for n in self.nodes.values() if n.kind is NodeKind.VIEW]

    @property
    def leaf_tables(self) -> list[StackNode]:
        return [n for n in self.nodes.values() if n.kind is NodeKind.TABLE]

    @property
    def table_names(self) -> list[str]:
        return sorted(n.name for n in self.leaf_tables)

    @property
    def table_functions(self) -> list[StackNode]:
        return [n for n in self.nodes.values() if n.kind is NodeKind.TABLE_FUNCTION]

    @property
    def unresolved(self) -> list[StackNode]:
        return [
            n
            for n in self.nodes.values()
            if n.kind in (NodeKind.UNRESOLVED, NodeKind.TABLE_FUNCTION)
        ]

    @property
    def is_fully_resolved(self) -> bool:
        return (
            not self.unresolved
            and not self.depth_cap_hit
            and not self.node_budget_hit
            and not self.cycles
        )

    def node(self, name: str) -> StackNode | None:
        return self.nodes.get(name.upper())

    def describe(self) -> str:
        parts = [
            f"{len(self.views)} view(s)",
            f"{len(self.leaf_tables)} table(s)",
            f"depth {self.max_depth_reached}",
        ]
        if self.node_budget_hit:
            parts.append("TRUNCATED — node budget exhausted")
        if self.unresolved:
            parts.append(f"{len(self.unresolved)} unresolved")
        if self.cycles:
            parts.append(f"{len(self.cycles)} cycle(s)")
        return ", ".join(parts)

    def render_tree(self) -> str:
        """An indented rendering for the CLI and the dependency viewer."""
        lines: list[str] = []
        seen: set[str] = set()

        def walk(name: str, indent: int) -> None:
            node = self.nodes.get(name.upper())
            if node is None:
                return
            marker = {
                NodeKind.VIEW: "view",
                NodeKind.TABLE: "table",
                NodeKind.TABLE_FUNCTION: "table function",
                NodeKind.UNRESOLVED: "UNRESOLVED",
            }[node.kind]
            suffix = f" — {node.reason}" if node.reason else ""
            repeat = " (already shown)" if name.upper() in seen else ""
            lines.append(f"{'  ' * indent}{node.name} [{marker}]{suffix}{repeat}")
            if name.upper() in seen:
                return
            seen.add(name.upper())
            if node.view is not None:
                for child in node.view.data_path_objects:
                    walk(child, indent + 1)

        walk(self.root_name, 0)
        return "\n".join(lines)


def resolve_stack(
    root: ParsedView,
    metadata: MetadataSource,
    *,
    max_depth: int = 15,
    max_nodes: int = 400,
) -> ViewStack:
    """Walk ``root`` down to leaf tables.

    ``max_nodes`` bounds the total number of objects visited. Against a live
    system every unvisited node is an HTTP round-trip, and SAP's own extraction
    views resolve into thousands of them — enough to run for hours on a single
    view. When the budget runs out the walk stops and the stack is marked
    truncated, which R-27 reports as MANUAL_REVIEW: an unwalked branch is one
    whose constructs were never checked, and no rule above it can be trusted.
    """
    stack = ViewStack(root_name=(root.name or "<anonymous>").upper())
    in_progress: set[str] = set()

    def add(name: str, depth: int, parent: str | None) -> StackNode:
        key = name.upper()
        stack.max_depth_reached = max(stack.max_depth_reached, depth)

        existing = stack.nodes.get(key)
        if existing is not None:
            if parent and parent not in existing.parents:
                existing.parents.append(parent)
            return existing

        node = StackNode(name=name.upper(), kind=NodeKind.UNRESOLVED, depth=depth)
        if parent:
            node.parents.append(parent)
        stack.nodes[key] = node
        return node

    def walk(name: str, depth: int, parent: str | None, known: ParsedView | None = None) -> None:
        key = name.upper()

        if key in in_progress:
            stack.cycles.append((parent or "", key))
            return

        node = add(name, depth, parent)
        if node.kind is not NodeKind.UNRESOLVED or node.reason:
            # Already classified on an earlier visit — memoised, do not re-walk.
            return

        if depth > max_depth:
            stack.depth_cap_hit = True
            node.reason = f"depth cap of {max_depth} reached"
            return

        if len(stack.nodes) > max_nodes:
            stack.node_budget_hit = True
            node.reason = (
                f"node budget of {max_nodes} objects exhausted before this "
                f"branch was walked"
            )
            return

        parsed = known
        if parsed is None:
            source = metadata.get_view_source(name)
            if source is not None:
                parsed = parse_ddl(source, name_hint=name)

        if parsed is not None:
            if parsed.entity_kind is EntityKind.TABLE_FUNCTION:
                node.kind = NodeKind.TABLE_FUNCTION
                node.view = parsed
                node.reason = (
                    "table function (AMDP) — contents are opaque to static "
                    "analysis and unsupported by CDC"
                )
                return
            if parsed.has_fatal_issue:
                node.reason = "source found but could not be parsed"
                node.view = parsed
                return

            node.kind = NodeKind.VIEW
            node.view = parsed
            in_progress.add(key)
            # Only the data path. An association that is declared but never
            # followed contributes no columns, so nothing inside it can affect
            # the result — and walking it anyway pulled entire unrelated
            # subject areas into the stack.
            for child in parsed.data_path_objects:
                walk(child, depth + 1, node.name)
            in_progress.discard(key)
            return

        table = metadata.get_table(name)
        if table is not None:
            if table.table_class is TableClass.VIEW:
                node.reason = (
                    "DDIC view — no CDS source available, contents not analysable"
                )
                return
            node.kind = NodeKind.TABLE
            node.table = table
            return

        node.reason = "not found in metadata — neither a known view nor a known table"

    walk(stack.root_name, 0, None, known=root)
    return stack
