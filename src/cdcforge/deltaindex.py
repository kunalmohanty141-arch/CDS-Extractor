"""Which of SAP's delta views feeds which table.

The candidate search climbs *up* from a table: find its readers, then the
readers of those, and so on. That works while the where-used indexes answer,
and on this release they stop answering exactly where it matters.

Measured on EKET. Fifty-two views read it, none carry delta, and the tool
recommended building a wrapper. Two C1-released delta views feed EKET all
along::

    C_PurOrdScheduleLineDEX      C1            <- delta, released, never found
      I_PurOrdScheduleLineAPI01  RELEASED
        I_PurOrdScheduleLineBasic  NOT_RELEASED
          I_PurgDocScheduleLineBasic NOT_RELEASED   <- found, ranked, wrapped
            EKET

Two things defeated the climb. It follows only SAP-released readers, and the
chain runs through two unreleased views; and — the real problem —
``CDSVIEWCROSSREF`` and ``DDLS_RIS_INDEX`` both return *nothing* for those
views. Crossref records classic views under their SQL view name, and a view
entity generates no SQL view, so modern content is largely absent. ``C_*DEX``
views are exactly that content.

So this goes the other way. The system will say which views carry delta —
904 of 7,095 on the reference system — and resolving a view's ``FROM`` chain
works perfectly well. Walk those once, record the table each is rooted on, and
a delta view can never be missed again however many layers up it sits or
whatever its release state.

The cost is one pass, and it is paid once per system: the answer only changes
when SAP ships new content, so it is cached and rebuilt on demand.

Rooted, not merely mentioned — the same rule as everywhere else. A delta view
that *joins* EKET is not a feed for EKET, because its rows are not one per EKET
row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.metadata.base import MetadataSource
from cdcforge.parsing.ddl import parse_ddl

#: Cache identity. Bump when the shape of what is stored changes, so an old
#: entry is rebuilt rather than misread.
INDEX_VERSION = 1

_MAX_DEPTH = 10


@dataclass
class DeltaIndex:
    """Delta-carrying views, by the table each one is rooted on."""

    by_table: dict[str, list[str]] = field(default_factory=dict)
    examined: int = 0
    unresolved: int = 0
    """Views whose FROM chain could not be followed to a table.

    Counted rather than hidden: a large number here means the index is
    covering less than it appears to.
    """

    @property
    def available(self) -> bool:
        """Was the index actually built? An empty one must not read as
        "no delta views feed anything"."""
        return self.examined > 0

    def covering(self, table: str) -> list[str]:
        return self.by_table.get((table or "").upper(), [])

    def to_dict(self) -> dict:
        return {
            "version": INDEX_VERSION,
            "by_table": self.by_table,
            "examined": self.examined,
            "unresolved": self.unresolved,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "DeltaIndex | None":
        if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
            return None
        by_table = payload.get("by_table")
        if not isinstance(by_table, dict):
            return None
        return cls(
            by_table={k.upper(): list(v) for k, v in by_table.items()},
            examined=int(payload.get("examined") or 0),
            unresolved=int(payload.get("unresolved") or 0),
        )

    def render(self) -> str:
        lines = [
            f"{sum(len(v) for v in self.by_table.values())} delta view(s) over "
            f"{len(self.by_table)} table(s), from {self.examined} examined"
        ]
        if self.unresolved:
            lines.append(f"  {self.unresolved} could not be traced to a table")
        return "\n".join(lines)


def build(
    metadata: MetadataSource, *, limit: int = 5000, progress=None
) -> DeltaIndex:
    """Walk every delta-carrying view down to the table it is rooted on.

    Reads only. Returns an empty, ``available``-false index when the system
    cannot say which views carry delta — which must never be read as "none
    do".

    ``progress`` is called with ``(done, total)`` as it goes. This is a
    minutes-long one-off on a real system, and a command that prints nothing
    for minutes looks hung rather than busy.
    """
    index = DeltaIndex()
    names = metadata.delta_supported_views()
    if not names:
        return index

    ordered = sorted(names)[:limit]
    index.examined = len(ordered)
    if progress:
        progress(0, len(ordered))

    # Resolve every chain in lockstep, one *level* at a time, rather than one
    # view at a time. Both do the same reads; only this one can batch them.
    #
    # Depth-first per view walks four or five levels serially, 900 times over,
    # which is thousands of sequential round trips — the first attempt was
    # still running after ten minutes. Level-wise turns that into a handful of
    # concurrent batches, and the levels share heavily: hundreds of these
    # chains pass through the same basic views, so after the first level most
    # reads are cache hits.
    pending = {name.upper(): name.upper() for name in ordered}

    for _level in range(_MAX_DEPTH):
        if not pending:
            break
        metadata.prefetch_sources(sorted(set(pending.values())))

        advanced: dict[str, str] = {}
        for view, current in pending.items():
            try:
                nxt = _next_in_chain(metadata, current)
            except Exception:
                nxt = None

            if nxt is None:
                index.unresolved += 1
            elif nxt.is_table:
                index.by_table.setdefault(nxt.name, []).append(view)
            else:
                advanced[view] = nxt.name

        pending = advanced
        if progress:
            progress(len(ordered) - len(pending), len(ordered))

    # Anything still moving when the depth limit ran out is unresolved, not
    # silently dropped.
    index.unresolved += len(pending)

    for views in index.by_table.values():
        views.sort()
    return index


@dataclass(frozen=True)
class _Step:
    name: str
    is_table: bool


def _next_in_chain(metadata: MetadataSource, name: str) -> "_Step | None":
    """One level down a FROM chain. ``None`` when it cannot be followed.

    Ask "is this a view?" before "is this a table?", and the order is the whole
    performance of this module.

    ``get_table`` on a name that is not a table costs two freestyle DDIC
    queries — DD02L and DD03L — that return nothing. Every element of every
    chain except the last one is a view, so testing for a table first spends
    two round trips per view to learn what the source cache already knows.
    Across 904 chains four levels deep that is thousands of queries and forty
    minutes of asking things that are obviously views whether they are tables.

    ``get_view_source`` is prefetched in bulk one level at a time, so the view
    case is answered from memory and only the final table reaches DDIC.
    """
    source = metadata.get_view_source(name)
    if source is not None:
        view = parse_ddl(source, name_hint=name)
        if view.has_fatal_issue or view.from_source is None:
            return None
        return _Step(view.from_source.name.upper(), False)

    # No DDL source: the chain has reached something that is not a view.
    if metadata.get_table(name) is not None:
        return _Step(name, True)
    return None


def load_or_build(
    metadata: MetadataSource, store, *, refresh: bool = False, progress=None
) -> DeltaIndex:
    """The cached index, built on first use.

    SAP's delta content changes when SAP ships, not while you work, so this is
    cached without expiry and rebuilt on request. ``store`` may be ``None``,
    in which case it is simply built every time.
    """
    key = "delta-index"
    if store is not None and not refresh:
        hit, payload = store.get_cached("index", key)
        if hit:
            cached = DeltaIndex.from_dict(payload)
            if cached is not None and cached.available:
                return cached

    index = build(metadata, progress=progress)
    if store is not None and index.available:
        store.put_cached("index", key, index.to_dict())
    return index


