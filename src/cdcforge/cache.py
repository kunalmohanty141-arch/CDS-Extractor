"""A caching layer over any ``MetadataSource``.

The expensive part of an assessment is not the rules — it is the reading. A
single deep SAP view resolves into dozens of nodes, and every node the stack
resolver visits was a fresh HTTPS round-trip: `I_MaterialDocumentRecord` alone
exceeded a five-minute timeout.

Wrapping the source rather than teaching each caller to cache means the stack
resolver, the rule engine and the generator all benefit without knowing this
exists. It also keeps the SAP-specific code free of caching concerns, which is
why this lives in the offline core and not in ``cdcforge.connect``.

Negative results are cached too. "No such table" costs the same round-trip as a
hit, and re-asking it on every run is most of what made the sweep slow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import ObjectMeta, TableMeta
from cdcforge.store import Store

KIND_SOURCE = "source"
KIND_TABLE = "table"
KIND_OBJECT = "object"
KIND_READERS = "readers"
"""Where-used answers: which views read a table, or a set of views."""

KIND_NAMESET = "nameset"
"""Whole-system name sets — the released and extraction-enabled lists."""


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    negative_hits: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return (self.hits / self.lookups) if self.lookups else 0.0

    def render(self) -> str:
        return (
            f"{self.lookups} lookup(s), {self.hits} from cache "
            f"({self.hit_rate:.0%}), {self.misses} fetched"
        )


class CachedMetadataSource(MetadataSource):
    """Read-through cache backed by the SQLite store."""

    def __init__(
        self,
        inner: MetadataSource,
        store: Store,
        *,
        refresh: bool = False,
    ) -> None:
        self.inner = inner
        self.store = store
        self.refresh = refresh
        """When true, ignore what is cached and re-read, updating the cache.

        The store is a cache, not a record of truth — this is the 'fresh'
        half of the cached-versus-fresh choice the UI is meant to offer.
        """

        self.stats = CacheStats()
        self._memory: dict[tuple[str, str], object] = {}

    name = "cached"

    # -- MetadataSource ----------------------------------------------------
    def _remembered(self, key: tuple[str, str]):
        """Serve from the in-process cache, counting it the same way the
        persistent path does — otherwise the stats under-report the negative
        hits that are most of the saving."""
        value = self._memory[key]
        self.stats.hits += 1
        if value is None:
            self.stats.negative_hits += 1
        return value

    def get_view_source(self, name: str) -> str | None:
        key = (KIND_SOURCE, name.upper())
        if key in self._memory:
            return self._remembered(key)  # type: ignore[return-value]

        if not self.refresh:
            source = self.store.get_source(name)
            if source is not None:
                self.stats.hits += 1
                self._memory[key] = source
                return source
            hit, payload = self.store.get_cached(KIND_SOURCE, name)
            if hit and payload is None:
                # A cached miss: the object genuinely has no DDL source.
                self.stats.hits += 1
                self.stats.negative_hits += 1
                self._memory[key] = None
                return None

        self.stats.misses += 1
        source = self.inner.get_view_source(name)
        if source is None:
            self.store.put_cached(KIND_SOURCE, name, None)
        else:
            self.store.put_source(name, source)
        self._memory[key] = source
        return source

    def prefetch_sources(self, names: list[str]) -> None:
        """Ask the inner source for the ones this layer cannot already answer.

        Anything already in memory or in the store is dropped from the request
        first: the point is to spend round trips only on genuine misses, and on
        a warm cache that is none of them.
        """
        wanted: list[str] = []
        for name in dict.fromkeys(n.upper() for n in names if n):
            if (KIND_SOURCE, name) in self._memory:
                continue
            if not self.refresh:
                if self.store.get_source(name) is not None:
                    continue
                hit, payload = self.store.get_cached(KIND_SOURCE, name)
                if hit and payload is None:
                    continue
            wanted.append(name)

        if len(wanted) < 2:
            return
        self.inner.prefetch_sources(wanted)

    def prefetch_objects(self, names: list[str]) -> None:
        wanted: list[str] = []
        for name in dict.fromkeys(n.upper() for n in names if n):
            if (KIND_OBJECT, name) in self._memory:
                continue
            if not self.refresh and self.store.get_cached(KIND_OBJECT, name)[0]:
                continue
            wanted.append(name)

        if len(wanted) < 2:
            return
        self.inner.prefetch_objects(wanted)
        # Pull what the inner source just learned through this layer, so it
        # lands in the store and survives the run.
        for name in wanted:
            self.get_object(name)

    def get_table(self, name: str) -> TableMeta | None:
        return self._cached(
            KIND_TABLE, name, self.inner.get_table, TableMeta.from_dict
        )

    def get_object(self, name: str) -> ObjectMeta | None:
        return self._cached(
            KIND_OBJECT, name, self.inner.get_object, ObjectMeta.from_dict
        )

    def list_views(self) -> list[str]:
        return self.inner.list_views()

    def list_tables(self) -> list[str]:
        return self.inner.list_tables()

    # -- the where-used queries -------------------------------------------
    #
    # These four used to pass straight through, which was invisible until F-09
    # started climbing VDM layers. Then every run re-queried 17,633 TADIR rows
    # for the released set and issued ~30 chunked crossref queries per table,
    # and a second look at VBAP cost as much as the first. Object metadata was
    # cached from the start; the set-based queries never were.

    def views_reading_table(self, table: str) -> list[str] | None:
        return self._cached_names(KIND_READERS, table.upper(), lambda: (
            self.inner.views_reading_table(table)
        ))

    def extraction_enabled_views(self) -> set[str] | None:
        names = self._cached_names(KIND_NAMESET, "extraction-enabled", lambda: (
            sorted(self.inner.extraction_enabled_views() or [])
            if self.inner.extraction_enabled_views() is not None
            else None
        ))
        return set(names) if names is not None else None

    def forget_extraction_enabled(self) -> None:
        self._memory.pop((KIND_NAMESET, "EXTRACTION-ENABLED"), None)
        self.store.forget(KIND_NAMESET, "extraction-enabled")
        forget = getattr(self.inner, "forget_extraction_enabled", None)
        if callable(forget):
            forget()

    def delta_supported_views(self) -> set[str] | None:
        names = self._cached_names(KIND_NAMESET, "delta-supported", lambda: (
            sorted(self.inner.delta_supported_views() or [])
            if self.inner.delta_supported_views() is not None
            else None
        ))
        return set(names) if names else None

    def released_views(self) -> set[str] | None:
        names = self._cached_names(KIND_NAMESET, "released", lambda: (
            sorted(self.inner.released_views() or [])
            if self.inner.released_views() is not None
            else None
        ))
        return set(names) if names is not None else None

    def views_reading_views(self, names: list[str]) -> list[str] | None:
        # Keyed on the request, since the answer is specific to this set of
        # names. Hashed because the list can hold hundreds of them.
        digest = hashlib.sha256(
            "\n".join(sorted(n.upper() for n in names)).encode()
        ).hexdigest()[:32]
        return self._cached_names(KIND_READERS, f"views:{digest}", lambda: (
            self.inner.views_reading_views(names)
        ))

    def describe(self) -> str:
        return f"{self.inner.describe()} (cached in {self.store.path})"

    # -- internals ---------------------------------------------------------
    def _cached_names(self, kind: str, key_name: str, fetch):
        """Cache a name list, keeping ``None`` distinct from ``[]``.

        ``None`` means the system could not answer and must be retried;
        ``[]`` means it answered "nothing". Collapsing the two would turn a
        failed query into a confident "no view reads this table", which is the
        exact defect that made the tool report that about EKKO.
        """
        key = (kind, key_name.upper())
        if key in self._memory:
            return self._remembered(key)

        if not self.refresh:
            hit, payload = self.store.get_cached(kind, key_name)
            if hit and payload is not None:
                self.stats.hits += 1
                value = list(payload.get("names", []))
                self._memory[key] = value
                return value

        self.stats.misses += 1
        value = fetch()
        # An unanswerable query is not cached — retrying it is the point.
        if value is not None:
            self.store.put_cached(kind, key_name, {"names": list(value)})
            self._memory[key] = list(value)
        return value

    def _cached(self, kind: str, name: str, fetch, revive):
        key = (kind, name.upper())
        if key in self._memory:
            return self._remembered(key)

        if not self.refresh:
            hit, payload = self.store.get_cached(kind, name)
            if hit:
                self.stats.hits += 1
                if payload is None:
                    self.stats.negative_hits += 1
                    self._memory[key] = None
                    return None
                value = revive(payload)
                self._memory[key] = value
                return value

        self.stats.misses += 1
        value = fetch(name)
        self.store.put_cached(kind, name, value.to_dict() if value else None)
        self._memory[key] = value
        return value
