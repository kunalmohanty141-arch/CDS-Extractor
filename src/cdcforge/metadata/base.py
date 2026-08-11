"""The ``MetadataSource`` interface.

Design note from §3.6 of the specification: the inventory module is written
against an abstract metadata source with three implementations — a server-side
companion view, ADT repository search, and direct HANA SQL — so the access path
is a config choice, not a rewrite.

Stage 1 ships two implementations: :class:`NullMetadataSource` (knows nothing,
answers honestly) and the fixture-backed mock (F-38). The ADT-backed ones land
in Stage 2 behind this same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from cdcforge.metadata.types import ObjectMeta, TableMeta


class MetadataSource(ABC):
    """Read-only access to system metadata.

    Every lookup may return ``None``. That is not an error condition — it is the
    honest answer for an object the source has never seen, and the rule engine
    turns it into INCONCLUSIVE rather than an assumption.
    """

    #: Short label shown in the UI and stamped on the audit record.
    name: str = "metadata"

    @abstractmethod
    def get_table(self, name: str) -> TableMeta | None:
        """DDIC table metadata: class, delivery class, fields, key flags."""

    @abstractmethod
    def get_view_source(self, name: str) -> str | None:
        """DDL source text for a CDS view, for stack resolution."""

    @abstractmethod
    def get_object(self, name: str) -> ObjectMeta | None:
        """Repository header: package, ownership, API release state."""

    def list_tables(self) -> list[str]:
        return []

    def list_views(self) -> list[str]:
        return []

    def extraction_enabled_views(self) -> set[str] | None:
        """Views the system reports as extraction-enabled, if it can say.

        Used to pre-rank candidates cheaply. ``None`` means "cannot answer".
        """
        return None

    def prefetch_sources(self, names: list[str]) -> None:
        """Warm the source cache for many objects at once.

        A hint, never a requirement: every caller still reads through
        :meth:`get_view_source`, and a source that does not implement this is
        simply slower. Nothing may depend on it having run, and it must never
        change *which* sources are read — only when.

        It exists because screening a table's readers is dominated by round
        trips, not by work. Measured on S/4HANA 2022: T001 has 248 readers,
        362 sources get read, and the whole thing takes 407 seconds — about
        1.1s each, essentially all of it latency.
        """
        return

    def prefetch_objects(self, names: list[str]) -> None:
        """Warm the repository-header cache for many objects at once.

        A hint like :meth:`prefetch_sources`, and the more important of the
        two: the per-object form is a freestyle query, and a freestyle query
        generates a program on the target system.
        """
        return

    def forget_extraction_enabled(self) -> None:
        """Drop any cached answer to :meth:`extraction_enabled_views`.

        A no-op for sources that do not cache. It exists because this list is
        the one the tool *itself* invalidates: creating or dropping a view
        changes it, and a stale copy makes the estate survey report that
        nothing feeds a table when something already does — which is exactly
        the duplicate the survey exists to prevent. Measured: a run reported 25
        custom extraction views where the system had 42, missing every object
        created that day.
        """
        return

    def delta_supported_views(self) -> set[str] | None:
        """Views the system reports as actually carrying delta, if it can say.

        Narrower than :meth:`extraction_enabled_views` — being enabled for
        extraction is not the same as carrying a change record. ``None`` means
        "cannot answer", never an empty set.
        """
        return None

    def released_views(self) -> set[str] | None:
        """Every DDL source SAP has released, if the source can say.

        ``None`` means "cannot answer" — never an empty set, which would read
        as "SAP has released nothing".
        """
        return None

    def views_reading_views(self, names: list[str]) -> list[str] | None:
        """Which CDS views read any of these views, if the source can answer.

        Lets a search look one level above a table's direct readers, where the
        consumption-ready views usually are. ``None`` means "cannot answer".
        """
        return None

    def views_reading_table(self, table: str) -> list[str] | None:
        """Which CDS views read this table, if the source can answer cheaply.

        ``None`` means "I cannot answer this" — distinct from ``[]``, which
        means "nothing reads it". The caller falls back to scanning, which is
        fine over fixtures and hopeless over a system with 7,000 views.
        """
        return None

    # -- convenience ------------------------------------------------------
    def is_table(self, name: str) -> bool:
        return self.get_table(name) is not None

    def is_view(self, name: str) -> bool:
        return self.get_view_source(name) is not None

    def describe(self) -> str:
        return self.name


class NullMetadataSource(MetadataSource):
    """Knows nothing about the system.

    Used when validating a bare DDL file with no fixtures and no connection.
    Every metadata-dependent rule comes back INCONCLUSIVE, which is the correct
    result: the tool genuinely cannot tell whether a key field is exposed if it
    has never seen the table's key.
    """

    name = "none"

    def get_table(self, name: str) -> TableMeta | None:
        return None

    def get_view_source(self, name: str) -> str | None:
        return None

    def get_object(self, name: str) -> ObjectMeta | None:
        return None
