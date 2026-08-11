"""``AdtMetadataSource`` — the real system behind the Stage 1 interface.

This is what the ``MetadataSource`` seam was for. The parser, the thirty rules,
the stack resolver and the generator are unchanged and unaware: swap the mock
for this and the same engine runs against a live S/4HANA system.

Reads only. Every method returns ``None`` rather than guessing, which the rule
engine turns into INCONCLUSIVE.
"""

from __future__ import annotations

from cdcforge.connect import endpoints as ep
from cdcforge.connect import sql
from cdcforge.connect.session import AdtError, AdtSession
from cdcforge.metadata.base import MetadataSource
from cdcforge.metadata.types import (
    ApiState,
    FieldMeta,
    ObjectMeta,
    Owner,
    TableClass,
    TableMeta,
    derive_owner,
)


class AdtMetadataSource(MetadataSource):
    """Metadata read live over ADT REST."""

    name = "adt"

    def __init__(
        self,
        session: AdtSession,
        *,
        use_queries: bool = True,
        fetch_workers: int = 6,
    ) -> None:
        self.session = session
        self.fetch_workers = fetch_workers
        """How many source reads to have in flight at once.

        Six rather than sixty. These are plain GETs — they do not generate
        programs the way the freestyle-query path does, which is what once
        filled a dev system's subpool directory — but the target is somebody's
        development box and this tool has no business saturating it. Set to 1
        to read strictly one at a time.
        """

        self.use_queries = use_queries
        """Whether the set-based data-preview path is available.

        The preflight establishes this. When it is off, table metadata is
        unavailable rather than wrong — object-by-object DDIC reads depend on an
        XML schema that is unpublished and varies by release, and inventing a
        parser for it would produce confident nonsense.
        """

        # In-memory for now. §6 specifies a SQLite cache keyed by source hash;
        # that lands with the inventory sweep, together with its invalidation.
        self._sources: dict[str, str | None] = {}
        self._tables: dict[str, TableMeta | None] = {}
        self._objects: dict[str, ObjectMeta | None] = {}
        self._states: dict[str, str] | None = None
        self._ddl_name_map: dict[str, str] | None = None
        self._released: set[str] | None = None
        self._directory: dict[str, str] | None = None
        self._delta: set[str] | None = None

    # -- MetadataSource ----------------------------------------------------
    def get_view_source(self, name: str) -> str | None:
        key = name.upper()
        if key in self._sources:
            return self._sources[key]

        source = self._read_source(key)
        if source is None:
            # A CDS view has three names and they need not agree: the view, the
            # database view, and the DDL *source object* — which is the one ADT
            # reads by. ZCDS_2RFLOWS is a real example: its source lives at
            # ZCDS_RFLOW1, so reading by view name answers 404 and the object
            # looks deleted. It is C1-released and delta-supported.
            #
            # Only tried on a miss, so the common path costs nothing.
            ddl_name = self._ddl_names().get(key)
            if ddl_name and ddl_name != key:
                source = self._read_source(ddl_name)
        self._sources[key] = source
        return source

    def prefetch_sources(self, names: list[str]) -> None:
        """Fetch many sources concurrently into the in-memory cache.

        Reads only, and the same reads that would happen anyway — the serial
        code that follows finds them already there. Nothing about *which*
        objects get examined changes, so the result is identical; only the
        waiting is shared.

        A failed read is deliberately **not** cached here. Caching the miss
        would skip the DDL-name fallback in :meth:`get_view_source`, and that
        fallback is the difference between finding ZCDS_2RFLOWS and declaring
        it deleted.
        """
        todo = [
            n.upper() for n in dict.fromkeys(names) if n and n.upper() not in self._sources
        ]
        if len(todo) < 2 or self.fetch_workers < 2:
            return

        from concurrent.futures import ThreadPoolExecutor

        workers = max(2, min(self.fetch_workers, len(todo)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, source in zip(
                todo, pool.map(self._read_source, todo), strict=True
            ):
                if source is not None:
                    self._sources[name] = source

    def prefetch_objects(self, names: list[str]) -> None:
        """Read TADIR for many objects in a handful of queries, not one each.

        Chunked at ten: an IN list of sixty answers a bare HTTP 400, and that
        failure was once swallowed silently and reported three readers where
        there were 287. A chunk that fails here is skipped rather than
        guessed at — the per-object path still runs for anything missing.
        """
        todo = [
            n.upper() for n in dict.fromkeys(names) if n and n.upper() not in self._objects
        ]
        if not self.use_queries or len(todo) < 2:
            return

        # One read of the whole directory answers these and every object the
        # stack walk goes on to discover — which is the larger half, and the
        # half that cannot be batched because its names are not known yet.
        if self._ddl_directory() is not None:
            return

        for start in range(0, len(todo), 10):
            chunk = todo[start : start + 10]
            try:
                result = sql.run_query(
                    self.session,
                    sql.object_directory_bulk_query(chunk),
                    max_rows=len(chunk) + 5,
                )
            except (AdtError, sql.QueryNotPermitted):
                continue

            found: dict[str, dict] = {
                (row.get("OBJ_NAME") or "").strip().upper(): row
                for row in result.rows
            }
            for name in chunk:
                row = found.get(name)
                if row is None:
                    # Absent from TADIR is a real answer — a generated or
                    # local object — and caching it saves asking again.
                    self._objects[name] = None
                    continue
                package = (row.get("DEVCLASS") or "").strip()
                self._objects[name] = ObjectMeta(
                    name=name,
                    kind=(row.get("OBJECT") or "DDLS").strip(),
                    package=package,
                    owner=(
                        Owner.CUSTOMER
                        if package.startswith(("Z", "Y", "$"))
                        else derive_owner(name)
                    ),
                    api_state=self._read_api_state(name),
                )

    def _read_source(self, name: str) -> str | None:
        try:
            response = self.session.get(
                ep.DDL_SOURCE.url(name=name), action="read-source", object_name=name
            )
        except AdtError:
            return None
        return response.text

    def get_table(self, name: str) -> TableMeta | None:
        key = name.upper()
        if key in self._tables:
            return self._tables[key]

        table = self._read_table(key) if self.use_queries else None
        self._tables[key] = table
        return table

    def get_object(self, name: str) -> ObjectMeta | None:
        key = name.upper()
        if key in self._objects:
            return self._objects[key]

        meta = self._read_object(key) if self.use_queries else None
        self._objects[key] = meta
        return meta

    def list_views(self) -> list[str]:
        if not self.use_queries:
            return []
        try:
            result = sql.run_query(self.session, sql.cds_inventory_query(), max_rows=20000)
        except AdtError:
            return []
        return sorted({row.get("DDLNAME", "").upper() for row in result.rows if row.get("DDLNAME")})

    def list_tables(self) -> list[str]:
        return sorted(self._tables)

    def extraction_enabled_views(self) -> set[str] | None:
        """Every extraction-enabled view, from the list already loaded for R-25.

        Free: the release-state map is DHCDCVCDSEXTRE, and being in it *is*
        being extraction-enabled.
        """
        states = self._release_states()
        return set(states) if states else None

    def delta_supported_views(self) -> set[str] | None:
        """Views the system itself reports as carrying delta.

        A strict subset of the extraction-enabled list — 904 of 7,095 on the
        reference system. These are the only views usable as they stand, which
        makes them the ones worth indexing by the table they feed.
        """
        self._load_extraction_rows()
        return self._delta or None

    def released_views(self) -> set[str] | None:
        """Released DDL sources, from one TADIR query (Appendix D.5).

        OBJ_NAME is the 40-character-padded name followed by the object type,
        so the type suffix is stripped and the padding trimmed here.
        """
        if not self.use_queries:
            return None
        if self._released is not None:
            return self._released
        try:
            result = sql.run_query(
                self.session, sql.released_ddls_query(), max_rows=40000
            )
        except (AdtError, sql.QueryNotPermitted):
            return None
        names = set()
        for row in result.rows:
            raw = (row.get("OBJ_NAME") or "").strip()
            if raw.endswith("DDLS"):
                raw = raw[: -len("DDLS")]
            if raw.strip():
                names.add(raw.strip().upper())
        self._released = names
        return names

    _IN_LIST_CHUNK = 10
    """Names per IN list.

    Measured, not guessed: the data-preview endpoint accepts a query of ~240
    characters and rejects ~390 with an unexplained HTTP 400, which lands
    between 10 and 20 forty-character names.
    """

    def views_reading_views(self, names: list[str]) -> list[str] | None:
        """Readers of any of these views, via CDSVIEWCROSSREF.

        Chunked, because the IN list goes into the query text and the endpoint
        rejects a long one. A failed chunk is split and retried rather than
        skipped — swallowing it returned 3 readers where there were 287 and
        reported that as the answer.
        """
        if not self.use_queries or not names:
            return None
        found: set[str] = set()
        answered = False
        for start in range(0, len(names), self._IN_LIST_CHUNK):
            batch = names[start : start + self._IN_LIST_CHUNK]
            rows = self._crossref_chunk(batch)
            if rows is None:
                continue
            answered = True
            found.update(rows)
        return sorted(found) if answered else None

    def _crossref_chunk(self, names: list[str]) -> set[str] | None:
        """One IN-list query, halving on failure down to single names.

        A rejected batch says nothing about the individual names in it, so the
        batch is split rather than abandoned. Only a name that fails on its own
        is genuinely unanswerable, and it costs one query to establish that.
        """
        if not names:
            return set()
        try:
            result = sql.run_query(
                self.session, sql.views_reading_views_query(names), max_rows=20000
            )
        except (AdtError, sql.QueryNotPermitted):
            if len(names) == 1:
                return None
            half = len(names) // 2
            left = self._crossref_chunk(names[:half])
            right = self._crossref_chunk(names[half:])
            if left is None and right is None:
                return None
            return (left or set()) | (right or set())

        return {
            (row.get("OBJECTDDLSOURCENAME") or "").upper()
            for row in result.rows
            if (row.get("OBJECTDDLSOURCENAME") or "").strip()
        }

    def views_reading_table(self, table: str) -> list[str] | None:
        """F-09 — which CDS views read this table.

        Two indexes, unioned, because each has gaps: CDSVIEWCROSSREF carries a
        SQLVIEWNAME and so indexes classic views, while DDLS_RIS_INDEX is the
        repository information system's own used-artifact index.

        Returns ``None`` when *both* fail — "I cannot answer" — rather than an
        empty list, which would mean "nothing reads it". That distinction
        matters: an earlier version returned [] when its query was simply
        wrong, and the UI reported "no CDS view reads EKKO" as fact.
        """
        if not self.use_queries:
            return None

        names: set[str] = set()
        answered = False

        try:
            result = sql.run_query(
                self.session, sql.views_reading_table_crossref_query(table),
                max_rows=2000,
            )
            answered = True
            names.update(
                (row.get("OBJECTDDLSOURCENAME") or "").upper()
                for row in result.rows
                if (row.get("OBJECTDDLSOURCENAME") or "").strip()
            )
        except (AdtError, sql.QueryNotPermitted):
            pass

        try:
            result = sql.run_query(
                self.session, sql.views_reading_table_ris_query(table), max_rows=2000
            )
            answered = True
            names.update(
                (row.get("DDLSRC_NAME") or "").upper()
                for row in result.rows
                if (row.get("DDLSRC_NAME") or "").strip()
            )
        except (AdtError, sql.QueryNotPermitted):
            pass

        return sorted(names) if answered else None

    def describe(self) -> str:
        return (
            f"ADT {self.session.profile.base_url} "
            f"client {self.session.profile.client} "
            f"as {self.session.profile.username}"
        )

    # -- internals ---------------------------------------------------------
    def _read_table(self, name: str) -> TableMeta | None:
        try:
            header = sql.run_query(self.session, sql.table_header_query(name), max_rows=1)
            fields = sql.run_query(self.session, sql.table_fields_query(name), max_rows=2000)
        except (AdtError, sql.QueryNotPermitted):
            return None

        if not header.rows and not fields.rows:
            return None

        head = header.first()
        meta = TableMeta(
            name=name,
            table_class=TableClass.parse(head.get("TABCLASS")),
            delivery_class=(head.get("CONTFLAG") or "").strip(),
            owner=derive_owner(name),
        )

        for row in fields.rows:
            field_name = (row.get("FIELDNAME") or "").strip()
            if not field_name or field_name.startswith("."):
                # '.INCLUDE' and friends are structure directives, not columns.
                continue
            meta.fields.append(
                FieldMeta(
                    name=field_name,
                    position=_as_int(row.get("POSITION")),
                    is_key=(row.get("KEYFLAG") or "").strip().upper() == "X",
                    data_element=(row.get("ROLLNAME") or "").strip(),
                    data_type=(row.get("DATATYPE") or "").strip(),
                    length=_as_int(row.get("LENG")),
                    decimals=_as_int(row.get("DECIMALS")),
                    ref_table=(row.get("REFTABLE") or "").strip(),
                    ref_field=(row.get("REFFIELD") or "").strip(),
                )
            )
        meta.fields.sort(key=lambda f: f.position)
        return meta if meta.fields else None

    def _read_object(self, name: str) -> ObjectMeta | None:
        directory = self._ddl_directory()
        if directory is not None:
            package = directory.get(name)
            if package is None:
                return None
            return self._object_from(name, package)

        try:
            result = sql.run_query(
                self.session, sql.object_directory_query(name), max_rows=1
            )
        except (AdtError, sql.QueryNotPermitted):
            return None
        if not result.rows:
            return None

        row = result.first()
        return self._object_from(name, (row.get("DEVCLASS") or "").strip())

    def _object_from(self, name: str, package: str) -> ObjectMeta:
        return ObjectMeta(
            name=name,
            kind="DDLS",
            package=package,
            owner=(
                Owner.CUSTOMER
                if package.startswith(("Z", "Y", "$"))
                else derive_owner(name)
            ),
            api_state=self._read_api_state(name),
        )

    def _ddl_directory(self) -> dict[str, str] | None:
        """Every DDL source's package, read once. ``None`` if it cannot be.

        The stack walk asks for objects it discovers as it goes, so it cannot
        batch them — screening one wide table asked TADIR 417 times that way.
        Reading the whole directory once removes the question rather than
        answering it faster, and a freestyle query generates a program on the
        target system, so the count matters more than the seconds.
        """
        if not self.use_queries:
            return None
        if self._directory is not None:
            return self._directory or None
        try:
            result = sql.run_query(
                self.session, sql.all_ddl_directory_query(), max_rows=100000
            )
        except (AdtError, sql.QueryNotPermitted):
            # Not fatal: the per-object path still works, just slowly.
            self._directory = {}
            return None

        directory: dict[str, str] = {}
        for row in result.rows:
            # OBJ_NAME is padded to 40 characters in TADIR.
            obj = (row.get("OBJ_NAME") or "").strip().upper()
            if obj:
                directory[obj] = (row.get("DEVCLASS") or "").strip()
        self._directory = directory
        return directory or None

    def _read_api_state(self, name: str) -> ApiState:
        """The release contract, from two independent sources.

        DHCDCVCDSEXTRE first: one query covers every extraction-enabled view
        and carries the actual level. The APIS transport object second, which
        establishes released-or-not for anything the first source has never
        heard of.

        Both were cross-checked against each other on a live system and agreed;
        neither is documented, so a failure of either leaves the state UNKNOWN,
        which R-25 treats as unmodifiable.
        """
        state = self._release_states().get(name.upper())
        if state:
            # 'NO' is the system's own way of saying not released.
            return ApiState.NOT_RELEASED if state == "NO" else ApiState.parse(state)

        # The released set is the same TADIR question, asked once for every
        # object instead of once per object. Screening T001's readers issued
        # 771 freestyle queries — 362 of them this one — and each of those
        # generates a program on the target system, which is what filled a
        # development box's subpool directory and closed its owner's Eclipse.
        # One bulk read answers all of them.
        #
        # It is also the more correct answer, which was not the reason for the
        # change but is the better one. The per-object query matches
        # `OBJ_NAME LIKE '<name>%DDLS'`, and `_` is a SQL single-character
        # wildcard as well as the commonest character in a CDS view name — so
        # `Z_I_CUSTBASIC` also matches `ZAIBCUSTBASIC…DDLS`. A view could be
        # reported as released because a *different* released view resembled
        # it, and "released" is what suppresses the upgrade caveat. Set
        # membership cannot do that.
        released = self.released_views()
        if released is not None:
            return (
                ApiState.RELEASED if name.upper() in released
                else ApiState.NOT_RELEASED
            )

        try:
            result = sql.run_query(self.session, sql.api_release_query(name), max_rows=1)
        except (AdtError, sql.QueryNotPermitted):
            return ApiState.UNKNOWN
        return ApiState.RELEASED if result.rows else ApiState.NOT_RELEASED

    def _release_states(self) -> dict[str, str]:
        self._load_extraction_rows()
        return self._states if self._states is not None else {}

    def _ddl_names(self) -> dict[str, str]:
        """View name → DDL source name, where the two differ."""
        self._load_extraction_rows()
        return self._ddl_name_map or {}

    def _load_extraction_rows(self) -> None:
        """One read of DHCDCVCDSEXTRE feeding both maps."""
        if self._states is not None:
            return
        try:
            result = sql.run_query(
                self.session, sql.extraction_release_states_query(), max_rows=50000
            )
        except (AdtError, sql.QueryNotPermitted):
            # An empty map is not the same as "not released" — the per-object
            # APIS lookup still runs, and a failure there leaves the state
            # UNKNOWN.
            self._states = {}
            self._ddl_name_map = {}
            return

        states: dict[str, str] = {}
        ddl_names: dict[str, str] = {}
        delta: set[str] = set()
        for row in result.rows:
            view = (row.get("VIEWNAME") or "").upper()
            if not view:
                continue
            states[view] = (row.get("RELEASE_STATE") or "").strip()
            ddl = (row.get("DDLNAME") or "").upper()
            if ddl and ddl != view:
                ddl_names[view] = ddl
            if (row.get("ISDELTASUPPORTED") or "").strip().lower() == "true":
                delta.add(view)
        self._states = states
        self._ddl_name_map = ddl_names
        self._delta = delta


def _as_int(value: str | None) -> int:
    try:
        return int((value or "0").strip() or 0)
    except ValueError:
        return 0
