"""Column lineage — where does this element's value ultimately come from?

Needed because a CDC mapping addresses **base tables, never intermediate
views** (Appendix A.5), while almost every SAP view sits on another view. So
"which of my elements exposes EKKO's key" cannot be answered by looking at the
view in front of you — the answer is two or five levels down.

Without this the wrapper generator refuses every SAP view whose FROM is a view,
which is nearly all of them. That made the only route the tool offers for
standard content — build a wrapper — fail on the objects that need it most.

The walk follows one element at a time rather than resolving whole stacks: the
question is about a single column, and following its alias chain is both
cheaper and exact.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import TableMeta
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.parsing.nodes import ParsedView


@dataclass(frozen=True)
class Origin:
    """Where a view element's value ultimately comes from."""

    table: str
    field: str
    depth: int = 0
    """How many views were traversed. 0 means the view reads the table itself."""

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.field}"


ColumnCache = dict[str, frozenset[str] | None]
"""Object name → its column/element names. ``None`` means "could not read it"."""


#: How far down a FROM chain to follow before giving up. SAP's own stacks run
#: to six; ten leaves room without letting a cycle spin forever.
MAX_CHAIN_DEPTH = 10


def root_table_name(
    metadata: MetadataSource, name: str, *, max_depth: int = MAX_CHAIN_DEPTH
) -> str:
    """The table a view's rows ultimately come from, or ``""``.

    Follows only the ``FROM`` at each level, never a join: a join adds columns,
    but what a row *is* comes from the root. A view that joins EKET is not a
    feed for EKET.

    One implementation on purpose. There were four, subtly different, and when
    a defect turned up it got fixed in two of them — the estate survey and the
    delta index — while the mapping generator kept it. The defect was asking
    ``get_table`` before ``get_view_source``: on a name that is not a table
    that costs two freestyle DDIC queries returning nothing, and every element
    of a chain except the last is a view. Cheap here, thousands of wasted round
    trips when the caller is walking 900 chains.

    So: views first, and the table lookup only for the thing that has no DDL
    source.
    """
    current = (name or "").upper()
    for _ in range(max_depth):
        source = metadata.get_view_source(current)
        if source is None:
            # Not a view. The chain ends here — as a table, or nowhere.
            return current if metadata.get_table(current) is not None else ""
        parsed = parse_ddl(source, name_hint=current)
        if parsed.has_fatal_issue or parsed.from_source is None:
            return ""
        current = parsed.from_source.name.upper()
    return ""


def column_names(
    metadata: MetadataSource, name: str, cache: ColumnCache
) -> frozenset[str] | None:
    """What columns does this object expose, whether it is a table or a view?

    ``None`` means the object could not be read, which is different from a view
    that exposes nothing — callers must treat it as "cannot decide".
    """
    key = name.upper()
    if key in cache:
        return cache[key]

    result: frozenset[str] | None = None
    table = metadata.get_table(name)
    if table is not None:
        result = frozenset(f.name.upper() for f in table.fields)
    else:
        source = metadata.get_view_source(name)
        if source is not None:
            parsed = parse_ddl(source, name_hint=name)
            if not parsed.has_fatal_issue:
                result = frozenset(n.upper() for n in parsed.element_names if n)

    cache[key] = result
    return result


def owning_source(
    metadata: MetadataSource,
    view: ParsedView,
    field_name: str,
    cache: ColumnCache,
):
    """Which of the view's sources does this unqualified name come from?

    One source is the easy case. With several, ask each what columns it has and
    accept the answer only when exactly one owns the name — anything else is
    genuinely ambiguous and must resolve to ``None``.

    Views count, not just tables. An earlier version consulted DD03L only and
    returned ``None`` the moment any source was a view, which is the normal
    shape of SAP content: ``I_SalesDocument`` is a classic view selecting from
    ``I_SalesDocumentBasic`` with two joined tables, and its elements are
    written unqualified. Every one of them was untraceable, so its key looked
    unexposed and F-09 rejected the sales document view of VBAK.
    """
    sources = view.sources
    if len(sources) == 1:
        return sources[0]

    owners = []
    for source in sources:
        columns = column_names(metadata, source.name, cache)
        if columns is None:
            return None  # one unreadable source makes the whole question moot
        if field_name.upper() in columns:
            owners.append(source)
    return owners[0] if len(owners) == 1 else None


def trace_element(
    metadata: MetadataSource,
    view: ParsedView,
    element_name: str,
    *,
    max_depth: int = 8,
    cache: ColumnCache | None = None,
) -> Origin | None:
    """Follow ``element_name`` down to the base table column it reads.

    Returns ``None`` when the value is computed, ambiguous, or the chain runs
    into something unreadable — all cases where claiming an origin would be a
    guess. Callers treat that as "cannot establish", never as "no origin".
    """
    return _trace(metadata, view, element_name, max_depth, 0, cache or {})


def _trace(
    metadata: MetadataSource,
    view: ParsedView,
    element_name: str,
    max_depth: int,
    depth: int,
    cache: ColumnCache,
) -> Origin | None:
    if depth > max_depth:
        return None

    item = view.find_element(element_name)
    if item is None:
        return None

    # Sees through a CAST, which SAP's VDM applies to almost every key
    # ("cast(PurchasingDocument as vdm_purchaseorder preserving type)") and
    # which changes the value not at all. Anything genuinely computed still
    # returns None — a CASE expression has no single origin, and pretending
    # otherwise would put one into a CDC mapping.
    ref = item.lineage_ref
    if ref is None or ref.is_pseudo:
        return None

    # Which source does this element read from?
    if ref.is_qualified:
        source = view.find_source(ref.root)
        field_name = ref.path[1]
    else:
        source = owning_source(metadata, view, ref.leaf, cache)
        field_name = ref.leaf

    if source is None:
        return None

    table = metadata.get_table(source.name)
    if table is not None:
        return Origin(table=table.name, field=field_name.upper(), depth=depth)

    nested_source = metadata.get_view_source(source.name)
    if nested_source is None:
        return None
    nested = parse_ddl(nested_source, name_hint=source.name)
    if nested.has_fatal_issue:
        return None
    return _trace(metadata, nested, field_name, max_depth, depth + 1, cache)


def element_origins(
    metadata: MetadataSource,
    view: ParsedView,
    *,
    max_depth: int = 8,
    cache: ColumnCache | None = None,
) -> dict[str, Origin]:
    """Every traceable element of ``view``, mapped to its base table column.

    One column cache is shared across all the elements, because a 297-element
    view asks the same "which of my three sources owns this name" question 297
    times and each answer costs a parse of every source.

    Pass ``cache`` to share it across many views too. F-09 screens 180 views
    over one table and they sit on the same handful of parents, so a per-call
    cache re-parses I_SalesDocumentBasic once per candidate instead of once.
    """
    origins: dict[str, Origin] = {}
    if cache is None:
        cache = {}
    for item in view.select_items:
        if not item.name:
            continue
        origin = trace_element(
            metadata, view, item.name, max_depth=max_depth, cache=cache
        )
        if origin is not None:
            origins[item.name] = origin
    return origins


def exposed_key_elements(
    metadata: MetadataSource, view: ParsedView, table: TableMeta, **kwargs
) -> dict[str, str]:
    """``{view element: table key field}`` for the table's key.

    Empty when the view does not expose the whole key — the caller must not
    build a mapping from a partial one, because a mapping missing a key field
    activates cleanly and then fails at delta time.
    """
    wanted = {f.name.upper() for f in table.business_key_fields}
    if not wanted:
        return {}

    found: dict[str, str] = {}
    for element, origin in element_origins(metadata, view, **kwargs).items():
        if origin.table.upper() != table.name.upper():
            continue
        if origin.field in wanted and origin.field not in found.values():
            found[element] = origin.field

    return found if set(found.values()) == wanted else {}
