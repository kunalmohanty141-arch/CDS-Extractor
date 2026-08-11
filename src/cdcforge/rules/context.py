"""Everything a rule is allowed to look at.

A rule gets a :class:`ValidationContext` and returns results. It does not reach
out to a system, read a file, or hold state between runs — which is what makes
each of the thirty rules independently unit-testable against a DDL fixture, as
the specification requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property

from cdcforge.cds import CdcMappingEntry, read_cdc_mapping
from cdcforge.metadata.base import MetadataSource, NullMetadataSource
from cdcforge.metadata.types import ObjectMeta, TableMeta
from cdcforge.parsing.nodes import DataSource, FieldRef, ParsedView, SelectItem
from cdcforge.rules.stack import NodeKind, ViewStack, resolve_stack


@dataclass(frozen=True)
class RuleConfig:
    """Tunables. Every threshold here is a judgement call, and each one says so."""

    max_stack_depth: int = 15
    """Hard cap for the dependency walk — cycle protection, not a CDC limit."""

    max_stack_nodes: int = 400
    """Total objects the dependency walk may visit.

    Depth was never the binding constraint on real content; breadth is. A
    custom Z-view resolves into a handful of objects. SAP's own extraction
    views fan out into thousands, and against a live system each one is an HTTP
    round-trip — enough to spend hours on a single view.

    Exceeding the budget is reported by R-27, not swallowed. Raise it when you
    genuinely need to walk a large SAP stack and can afford the time.
    """

    manual_review_stack_depth: int = 5
    """Above this, R-29 asks for human review.

    SAP publishes no number. The framework rejects views it considers "too
    complex for automatic CDC delta" (KBA 3467820) without documenting the
    threshold, so this is the tool's own conservatism, and the rule message
    says exactly that rather than implying SAP set it.
    """

    row_count_review_threshold: int = 2_000_000_000
    """~2bn rows, the documented internal HANA row limit (Appendix E.4)."""

    require_cardinality_evidence: bool = True
    """Whether an unproven to-one join is a review item (R-26).

    Default on. A declared ``LEFT OUTER TO ONE JOIN`` that has never been
    checked against real data is the single most expensive silent failure in
    the CDC domain: the view activates, the initial load looks right, and delta
    produces duplicates weeks later. Calling that PASS would be exactly the
    false PASS the tool exists to prevent.

    Turn it off when the customer has knowingly accepted the risk — the finding
    then downgrades to a stated caveat rather than disappearing.
    """

    require_view_entity: bool = False
    """When true, a classic DEFINE VIEW is reported as a modernisation item.
    Off by default: classic views support CDC perfectly well."""


class CardinalityResult(str, Enum):
    """F-14 — the empirical cardinality prover's three outcomes."""

    PROVEN_TO_ONE = "PROVEN_TO_ONE"
    """Verified against real data."""

    VIOLATED = "VIOLATED"
    """Found N rows for a key. The actual key value is reported."""

    DECLARED_ONLY = "DECLARED_ONLY"
    """Could not probe — no data access, or the table is too large."""


@dataclass(frozen=True)
class CardinalityEvidence:
    """The result of probing one to-one join against real data."""

    join_alias: str
    table: str
    result: CardinalityResult
    max_count: int | None = None
    sample_key: str = ""


class SubscriptionState(str, Enum):
    """R-28 / F-16 — is there a live CDC subscription on this view?"""

    NONE = "NONE"
    ACTIVE = "ACTIVE"
    UNKNOWN = "UNKNOWN"
    """No system connected, or the check could not run. Writes stay blocked."""


@dataclass
class Origin:
    """Where a view element's value comes from."""

    source: DataSource
    field_name: str

    @property
    def object_name(self) -> str:
        return self.source.name


@dataclass
class ValidationContext:
    """The input to every rule."""

    view: ParsedView
    metadata: MetadataSource = field(default_factory=NullMetadataSource)
    config: RuleConfig = field(default_factory=RuleConfig)
    object_meta: ObjectMeta | None = None
    subscription_state: SubscriptionState = SubscriptionState.UNKNOWN
    cardinality_evidence: dict[str, CardinalityEvidence] = field(default_factory=dict)
    _stack: ViewStack | None = None
    _columns: dict[str, frozenset[str] | None] = field(default_factory=dict)
    """Shared column cache for unqualified-name resolution — see lineage."""

    # -- lazily derived views over the input -------------------------------
    @property
    def stack(self) -> ViewStack:
        if self._stack is None:
            self._stack = resolve_stack(
                self.view,
                self.metadata,
                max_depth=self.config.max_stack_depth,
                max_nodes=self.config.max_stack_nodes,
            )
        return self._stack

    @cached_property
    def mapping(self) -> list[CdcMappingEntry] | None:
        return read_cdc_mapping(self.view.annotations)

    @cached_property
    def main_entry(self) -> CdcMappingEntry | None:
        for entry in self.mapping or []:
            if entry.is_main:
                return entry
        return None

    @cached_property
    def element_names_upper(self) -> set[str]:
        return {name.upper() for name in self.view.element_names if name}

    @cached_property
    def origin_index(self) -> dict[tuple[str, str], list[str]]:
        """(source object, field) → the view elements that expose it.

        Only simple field elements are indexed. A computed element cannot be
        claimed to "expose" a base column, and pretending otherwise would let a
        key-exposure rule pass on a CASE expression that happens to mention the
        key.
        """
        index: dict[tuple[str, str], list[str]] = {}
        for item in self.view.select_items:
            origin = self.origin_of(item)
            if origin is None:
                continue
            key = (origin.object_name.upper(), origin.field_name.upper())
            index.setdefault(key, []).append(item.name or item.text())
        return index

    # -- helpers -----------------------------------------------------------
    def origin_of(self, item: SelectItem) -> Origin | None:
        """Which source column this element reads, seeing through a cast.

        SAP's VDM wraps almost every key in a type-only cast —
        ``cast(mara.matnr as productnumber preserving type)`` in I_Product.
        Matching only bare field references missed those, so R-13, R-15 and
        R-23 reported the key as unexposed on most SAP standard views, and
        F-09 excluded them from being wrapper bases for the same reason.
        """
        ref = item.lineage_ref
        return None if ref is None else self.resolve_ref(ref)

    def resolve_ref(self, ref: FieldRef) -> Origin | None:
        """Trace a field reference back to the source it reads.

        Returns ``None`` when the answer is not knowable from the DDL alone —
        an ambiguous unqualified name, a ``$projection`` self-reference, or a
        path through an association. Callers report that as INCONCLUSIVE.
        """
        if ref.is_pseudo:
            return None

        if ref.is_qualified:
            source = self.view.find_source(ref.root)
            if source is None:
                return None  # association path or unknown alias
            return Origin(source=source, field_name=ref.path[1])

        # More than one source and no qualifier: ask each source what columns
        # it has and accept the answer only when exactly one owns the name.
        #
        # Views count, not only tables. Consulting DD03L alone meant any view
        # among the sources made this bail out, which is the normal shape of
        # SAP content — and it left the elements of every classic view-over-view
        # with joins unresolved, so R-13/R-15/R-23 saw their keys as unexposed.
        from cdcforge.lineage import owning_source

        source = owning_source(self.metadata, self.view, ref.leaf, self._columns)
        return None if source is None else Origin(source=source, field_name=ref.leaf)

    def table_for_source(self, source: DataSource) -> TableMeta | None:
        return self.metadata.get_table(source.name)

    def table_meta(self, name: str) -> TableMeta | None:
        return self.metadata.get_table(name)

    def source_is_table(self, source: DataSource) -> bool:
        node = self.stack.node(source.name)
        return node is not None and node.kind is NodeKind.TABLE

    def elements_exposing(self, object_name: str, field_name: str) -> list[str]:
        return self.origin_index.get((object_name.upper(), field_name.upper()), [])

    def has_element(self, name: str) -> bool:
        return name.upper() in self.element_names_upper

    @cached_property
    def main_table_name(self) -> str | None:
        """The table playing the ``#MAIN`` role.

        Taken from the mapping when there is one. Otherwise inferred from the
        FROM clause, but only when FROM resolves to an actual table — inferring
        a main table through a view stack would be a guess.
        """
        if self.main_entry is not None and self.main_entry.table:
            return self.main_entry.table
        source = self.view.from_source
        if source is None:
            return None
        node = self.stack.node(source.name)
        if node is not None and node.kind is NodeKind.TABLE:
            return node.name
        return None

    @cached_property
    def main_table(self) -> TableMeta | None:
        name = self.main_table_name
        return self.metadata.get_table(name) if name else None
