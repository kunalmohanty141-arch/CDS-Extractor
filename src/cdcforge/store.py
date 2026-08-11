"""§6 — the local store.

SQLite, one file per profile. Everything the tool learns about a system lands
here so the second run is instant: the first inventory sweep can take hours on
a large landscape, and a tool that re-reads four thousand views every time will
not be run twice.

    Cache invalidation by source hash. Never trust a cached verdict against
    changed source.

That rule is enforced here rather than left to callers — :meth:`cached_verdicts`
returns nothing unless the stored hash matches the source it is asked about.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cdcforge.model import (
    Assessment,
    Outcome,
    ParseIssue,
    RuleResult,
    Severity,
    SourceRef,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS systems (
    profile_id TEXT PRIMARY KEY,
    host       TEXT,
    client     TEXT,
    system_id  TEXT,
    role       TEXT,
    release    TEXT,
    sp         TEXT,
    first_seen TEXT,
    last_scan  TEXT
);

CREATE TABLE IF NOT EXISTS views (
    profile_id         TEXT NOT NULL,
    ddl_name           TEXT NOT NULL,
    sql_view_name      TEXT,
    entity_type        TEXT,
    package            TEXT,
    software_component TEXT,
    owner              TEXT,
    api_state          TEXT,
    extraction_enabled INTEGER,
    delta_method       TEXT,
    cdc_type           TEXT,
    base_tables        TEXT,
    verdict            TEXT,
    bucket             TEXT,
    scanned_at         TEXT,
    PRIMARY KEY (profile_id, ddl_name)
);

CREATE TABLE IF NOT EXISTS view_sources (
    profile_id  TEXT NOT NULL,
    ddl_name    TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    source_text TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (profile_id, ddl_name)
);

CREATE TABLE IF NOT EXISTS tables (
    profile_id      TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    table_class     TEXT,
    delivery_class  TEXT,
    package         TEXT,
    owner           TEXT,
    key_field_count INTEGER,
    field_count     INTEGER,
    est_rows        INTEGER,
    is_hot          INTEGER,
    has_view        INTEGER,
    scanned_at      TEXT,
    PRIMARY KEY (profile_id, table_name)
);

CREATE TABLE IF NOT EXISTS dependencies (
    profile_id TEXT NOT NULL,
    parent     TEXT NOT NULL,
    child      TEXT NOT NULL,
    child_type TEXT,
    depth      INTEGER,
    resolved   INTEGER,
    PRIMARY KEY (profile_id, parent, child)
);

CREATE TABLE IF NOT EXISTS verdicts (
    profile_id  TEXT NOT NULL,
    ddl_name    TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    outcome     TEXT,
    severity    TEXT,
    node        TEXT,
    line        INTEGER,
    explanation TEXT,
    remediation TEXT,
    sap_source  TEXT,
    detail      TEXT,
    evaluated_at TEXT,
    PRIMARY KEY (profile_id, ddl_name, rule_id, seq)
);

-- The header for one assessment. Separate from `verdicts` because an
-- assessment is not the same thing as its findings, and the store used to
-- conflate them: `verdicts` holds one row per rule result, so an assessment
-- with *no* results wrote no rows and vanished. UNPARSEABLE is exactly that
-- shape — the parser gives up, no rule runs — and it is the verdict the
-- specification is most insistent about being a correct output. It went in and
-- never came back, from either cached_assessment or all_assessments.
--
-- `unparseable` and `parse_issues` live here too. Without them a revived
-- assessment has no results and no fatal flag, which computes to PASS: a
-- stored "I could not read this" would come back as "this is fine".
CREATE TABLE IF NOT EXISTS assessments (
    profile_id   TEXT NOT NULL,
    ddl_name     TEXT NOT NULL,
    source_hash  TEXT NOT NULL,
    verdict      TEXT,
    unparseable  INTEGER,
    parse_issues TEXT,
    evaluated_at TEXT,
    PRIMARY KEY (profile_id, ddl_name)
);

CREATE TABLE IF NOT EXISTS cardinality (
    profile_id TEXT NOT NULL,
    ddl_name   TEXT NOT NULL,
    join_alias TEXT NOT NULL,
    table_name TEXT,
    result     TEXT,
    max_count  INTEGER,
    sample_key TEXT,
    probed_at  TEXT,
    PRIMARY KEY (profile_id, ddl_name, join_alias)
);

CREATE TABLE IF NOT EXISTS generated (
    profile_id  TEXT NOT NULL,
    object_name TEXT NOT NULL,
    ddl_text    TEXT,
    created_at  TEXT,
    status      TEXT,
    tr          TEXT,
    PRIMARY KEY (profile_id, object_name)
);

CREATE TABLE IF NOT EXISTS rollback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   TEXT NOT NULL,
    object_name  TEXT NOT NULL,
    prior_source TEXT,
    tr           TEXT,
    timestamp    TEXT,
    outcome      TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    profile_id  TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    taken_at    TEXT,
    view_count  INTEGER,
    hash        TEXT,
    payload     TEXT,
    PRIMARY KEY (profile_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    profile_id    TEXT    NOT NULL,
    system_id     TEXT,
    client        TEXT,
    user          TEXT,
    action        TEXT    NOT NULL,
    method        TEXT,
    path          TEXT,
    object        TEXT,
    http_status   INTEGER,
    elapsed_ms    INTEGER,
    request_hash  TEXT,
    response_hash TEXT,
    ssl_verified  INTEGER NOT NULL,
    read_only     INTEGER NOT NULL,
    outcome       TEXT,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS metadata_cache (
    profile_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    payload    TEXT,
    fetched_at TEXT,
    PRIMARY KEY (profile_id, kind, name)
);

CREATE INDEX IF NOT EXISTS idx_views_bucket   ON views (profile_id, bucket);
CREATE INDEX IF NOT EXISTS idx_verdicts_view  ON verdicts (profile_id, ddl_name);
CREATE INDEX IF NOT EXISTS idx_deps_parent    ON dependencies (profile_id, parent);
"""


def source_hash(text: str) -> str:
    """The cache key. Normalised for line endings so a CRLF round-trip through
    Windows does not invalidate every cached verdict in the store."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ViewRecord:
    ddl_name: str
    entity_type: str = ""
    sql_view_name: str = ""
    package: str = ""
    software_component: str = ""
    owner: str = ""
    api_state: str = ""
    extraction_enabled: bool = False
    delta_method: str = ""
    cdc_type: str = ""
    base_tables: list[str] = None  # type: ignore[assignment]
    verdict: str = ""
    bucket: str = ""
    scanned_at: str = ""

    def __post_init__(self) -> None:
        if self.base_tables is None:
            self.base_tables = []


class Store:
    """The per-profile SQLite store."""

    def __init__(self, path: str | Path, profile_id: str = "local") -> None:
        self.path = Path(path)
        self.profile_id = profile_id
        if self.path.name != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection for the life of the store, not one per operation.
        #
        # Per-call connections cost roughly 0.7s per view in bookkeeping alone,
        # almost all of it fsync. That is invisible on a 38-view fixture corpus
        # and an extra hour on a 4,000-view landscape — on top of the network
        # time the sweep is already apologising for.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if str(self.path) != ":memory:":
            # WAL plus NORMAL trades a fsync per commit for a fsync per
            # checkpoint. The store is a cache rebuilt by re-scanning, so
            # losing the last few writes to a power cut costs a re-read, not
            # data — and the audit log, which must not lose records, is a
            # separate database.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        yield self._conn

    # -- system ------------------------------------------------------------
    def record_system(self, **fields: str) -> None:
        fields.setdefault("profile_id", self.profile_id)
        fields.setdefault("first_seen", _now())
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(
            f"{k}=excluded.{k}" for k in fields if k not in ("profile_id", "first_seen")
        )
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO systems ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(profile_id) DO UPDATE SET {updates}",
                tuple(fields.values()),
            )
            conn.commit()

    def mark_scanned(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE systems SET last_scan = ? WHERE profile_id = ?",
                (_now(), self.profile_id),
            )
            conn.commit()

    def last_scan(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_scan FROM systems WHERE profile_id = ?", (self.profile_id,)
            ).fetchone()
        return (row["last_scan"] if row else "") or ""

    # -- sources -----------------------------------------------------------
    def put_source(self, ddl_name: str, text: str) -> str:
        digest = source_hash(text)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO view_sources (profile_id, ddl_name, source_hash, "
                "source_text, fetched_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(profile_id, ddl_name) DO UPDATE SET "
                "source_hash=excluded.source_hash, source_text=excluded.source_text, "
                "fetched_at=excluded.fetched_at",
                (self.profile_id, ddl_name.upper(), digest, text, _now()),
            )
            conn.commit()
        return digest

    def get_source(self, ddl_name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_text FROM view_sources WHERE profile_id=? AND ddl_name=?",
                (self.profile_id, ddl_name.upper()),
            ).fetchone()
        return row["source_text"] if row else None

    def get_source_hash(self, ddl_name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_hash FROM view_sources WHERE profile_id=? AND ddl_name=?",
                (self.profile_id, ddl_name.upper()),
            ).fetchone()
        return row["source_hash"] if row else None

    # -- generic metadata cache --------------------------------------------
    def put_cached(self, kind: str, name: str, payload: object) -> None:
        """Cache a metadata lookup, including a negative one.

        ``None`` is stored as JSON null rather than skipped. "This object does
        not exist" is an expensive answer to obtain over ADT and a perfectly
        good one to remember; dropping it would make every miss re-query on
        every run, which is exactly the case that made deep view stacks
        unusable.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metadata_cache (profile_id, kind, name, payload, "
                "fetched_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(profile_id, kind, name) DO UPDATE SET "
                "payload=excluded.payload, fetched_at=excluded.fetched_at",
                (self.profile_id, kind, name.upper(), json.dumps(payload), _now()),
            )
            conn.commit()

    def forget(self, kind: str, name: str) -> None:
        """Drop one cached entry, so the next read goes to the system.

        Needed because a few cached answers are invalidated by this tool's own
        writes rather than by time. Nothing here expires on a clock, which is
        right for DDIC metadata and wrong for "the list of views that exist".
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM metadata_cache WHERE profile_id=? AND kind=? "
                "AND name=?",
                (self.profile_id, kind, name.upper()),
            )

    def get_cached(self, kind: str, name: str) -> tuple[bool, object]:
        """``(hit, payload)``. A hit with payload ``None`` is a cached miss."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM metadata_cache WHERE profile_id=? AND "
                "kind=? AND name=?",
                (self.profile_id, kind, name.upper()),
            ).fetchone()
        if row is None:
            return False, None
        return True, json.loads(row["payload"])

    # -- views -------------------------------------------------------------
    def put_view(self, record: ViewRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO views (
                    profile_id, ddl_name, sql_view_name, entity_type, package,
                    software_component, owner, api_state, extraction_enabled,
                    delta_method, cdc_type, base_tables, verdict, bucket, scanned_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id, ddl_name) DO UPDATE SET
                    sql_view_name=excluded.sql_view_name,
                    entity_type=excluded.entity_type,
                    package=excluded.package,
                    software_component=excluded.software_component,
                    owner=excluded.owner,
                    api_state=excluded.api_state,
                    extraction_enabled=excluded.extraction_enabled,
                    delta_method=excluded.delta_method,
                    cdc_type=excluded.cdc_type,
                    base_tables=excluded.base_tables,
                    verdict=excluded.verdict,
                    bucket=excluded.bucket,
                    scanned_at=excluded.scanned_at
                """,
                (
                    self.profile_id,
                    record.ddl_name.upper(),
                    record.sql_view_name,
                    record.entity_type,
                    record.package,
                    record.software_component,
                    record.owner,
                    record.api_state,
                    int(record.extraction_enabled),
                    record.delta_method,
                    record.cdc_type,
                    json.dumps(sorted(record.base_tables)),
                    record.verdict,
                    record.bucket,
                    record.scanned_at or _now(),
                ),
            )
            conn.commit()

    def views(self, bucket: str = "") -> list[dict]:
        query = "SELECT * FROM views WHERE profile_id=?"
        params: list = [self.profile_id]
        if bucket:
            query += " AND bucket=?"
            params.append(bucket)
        query += " ORDER BY ddl_name"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["base_tables"] = json.loads(item.get("base_tables") or "[]")
            out.append(item)
        return out

    def view_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM views WHERE profile_id=?", (self.profile_id,)
            ).fetchone()[0]

    # -- tables ------------------------------------------------------------
    def put_table(self, **fields) -> None:
        fields.setdefault("profile_id", self.profile_id)
        fields.setdefault("scanned_at", _now())
        fields["table_name"] = str(fields["table_name"]).upper()
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(
            f"{k}=excluded.{k}" for k in fields if k not in ("profile_id", "table_name")
        )
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO tables ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(profile_id, table_name) DO UPDATE SET {updates}",
                tuple(fields.values()),
            )
            conn.commit()

    def tables(self, *, bare_only: bool = False) -> list[dict]:
        query = "SELECT * FROM tables WHERE profile_id=?"
        if bare_only:
            query += " AND (has_view IS NULL OR has_view = 0)"
        query += " ORDER BY table_name"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query, (self.profile_id,)).fetchall()]

    # -- dependencies ------------------------------------------------------
    def put_dependencies(self, parent: str, edges: list[tuple[str, str, int, bool]]) -> None:
        with self._connect() as conn:
            for child, child_type, depth, resolved in edges:
                conn.execute(
                    "INSERT INTO dependencies (profile_id, parent, child, child_type, "
                    "depth, resolved) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(profile_id, parent, child) DO UPDATE SET "
                    "child_type=excluded.child_type, depth=excluded.depth, "
                    "resolved=excluded.resolved",
                    (self.profile_id, parent.upper(), child.upper(), child_type, depth, int(resolved)),
                )
            conn.commit()

    def dependents_of(self, table: str) -> list[str]:
        """Which views read this table — the 'where used' the three lists need."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT parent FROM dependencies WHERE profile_id=? AND child=?",
                (self.profile_id, table.upper()),
            ).fetchall()
        return sorted(r["parent"] for r in rows)

    # -- verdicts ----------------------------------------------------------
    @staticmethod
    def _revive_header(ddl_name: str, header) -> Assessment:
        """Rebuild the assessment shell, findings aside.

        Restoring ``unparseable`` matters: without it an assessment with no
        results looks clean, so a stored UNPARSEABLE would revive as PASS —
        the exact false pass the whole tool is built to prevent, produced by
        its own cache.
        """
        issues = []
        for raw in json.loads(header["parse_issues"] or "[]"):
            issues.append(
                ParseIssue(
                    message=raw.get("message", ""),
                    ref=SourceRef(line=raw.get("line") or 0),
                    fatal=bool(raw.get("fatal")),
                )
            )
        return Assessment(
            object_name=ddl_name.upper(),
            parse_issues=issues,
            unparseable=bool(header["unparseable"]),
        )

    def put_verdicts(self, assessment: Assessment, digest: str) -> None:
        """Store an assessment: the header always, the findings if it has any.

        The header is written unconditionally, which is the whole point. An
        UNPARSEABLE assessment has no rule results at all, so a design that
        recorded only findings recorded nothing and lost the object.
        """
        name = assessment.object_name.upper()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO assessments (profile_id, ddl_name, source_hash, "
                "verdict, unparseable, parse_issues, evaluated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id, ddl_name) DO UPDATE SET "
                "source_hash=excluded.source_hash, verdict=excluded.verdict, "
                "unparseable=excluded.unparseable, "
                "parse_issues=excluded.parse_issues, "
                "evaluated_at=excluded.evaluated_at",
                (
                    self.profile_id,
                    name,
                    digest,
                    assessment.verdict.value,
                    1 if assessment.unparseable else 0,
                    json.dumps(
                        [
                            {
                                "message": issue.message,
                                "line": issue.ref.line,
                                "fatal": issue.fatal,
                            }
                            for issue in assessment.parse_issues
                        ]
                    ),
                    _now(),
                ),
            )
            conn.execute(
                "DELETE FROM verdicts WHERE profile_id=? AND ddl_name=?",
                (self.profile_id, name),
            )
            for seq, result in enumerate(assessment.results):
                conn.execute(
                    """
                    INSERT INTO verdicts (
                        profile_id, ddl_name, source_hash, rule_id, seq, outcome,
                        severity, node, line, explanation, remediation, sap_source,
                        detail, evaluated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.profile_id,
                        assessment.object_name.upper(),
                        digest,
                        result.rule_id,
                        seq,
                        result.outcome.value,
                        result.severity.value,
                        result.node,
                        result.ref.line,
                        result.message,
                        result.remediation,
                        result.sap_source,
                        json.dumps(result.detail),
                        _now(),
                    ),
                )
            conn.commit()

    def cached_assessment(self, ddl_name: str, digest: str) -> Assessment | None:
        """Rebuild a stored assessment, but only if the source has not changed.

        Returning a verdict computed against different source would be worse
        than having no cache at all: it would be confidently wrong, and nothing
        downstream could tell.
        """
        with self._connect() as conn:
            # The header decides whether we have this assessment at all. Using
            # the presence of findings for that lost every UNPARSEABLE object,
            # which has none by construction.
            header = conn.execute(
                "SELECT * FROM assessments WHERE profile_id=? AND ddl_name=? "
                "AND source_hash=?",
                (self.profile_id, ddl_name.upper(), digest),
            ).fetchone()
            if header is None:
                return None
            rows = conn.execute(
                "SELECT * FROM verdicts WHERE profile_id=? AND ddl_name=? AND "
                "source_hash=? ORDER BY seq",
                (self.profile_id, ddl_name.upper(), digest),
            ).fetchall()

        assessment = self._revive_header(ddl_name, header)
        for row in rows:
            assessment.results.append(
                RuleResult(
                    rule_id=row["rule_id"],
                    outcome=Outcome(row["outcome"]),
                    severity=Severity(row["severity"]),
                    message=row["explanation"] or "",
                    ref=SourceRef(line=row["line"] or 0),
                    node=row["node"] or "",
                    sap_source=row["sap_source"] or "",
                    remediation=row["remediation"] or "",
                    detail=json.loads(row["detail"] or "{}"),
                )
            )
        return assessment

    def all_assessments(self) -> list[Assessment]:
        """Rebuild every stored assessment, for reporting.

        Deliberately not hash-checked. The report describes what the last scan
        found, and silently dropping objects whose source has since changed
        would make the totals disagree with the view list beside them. Freshness
        is the scanner's business; ``cached_assessment`` is the one that
        refuses stale data.
        """
        with self._connect() as conn:
            # From the header table, not from `verdicts`. Listing objects by
            # the findings they produced omitted every object that produced
            # none — an UNPARSEABLE view is invisible to that query, and the
            # report's totals then quietly disagreed with reality.
            names = [
                row["ddl_name"]
                for row in conn.execute(
                    "SELECT ddl_name FROM assessments WHERE profile_id=? "
                    "ORDER BY ddl_name",
                    (self.profile_id,),
                ).fetchall()
            ]
        out: list[Assessment] = []
        for name in names:
            digest = self.get_source_hash(name) or ""
            assessment = self.cached_assessment(name, digest)
            if assessment is None:
                assessment = self._assessment_any_hash(name)
            if assessment is not None:
                out.append(assessment)
        return out

    def _assessment_any_hash(self, ddl_name: str) -> Assessment | None:
        """The stored assessment whatever hash it was computed against.

        Used by the report, which describes the last scan rather than the
        current source. Keyed on the header for the same reason
        ``cached_assessment`` is: an object with no findings still exists.
        """
        with self._connect() as conn:
            header = conn.execute(
                "SELECT * FROM assessments WHERE profile_id=? AND ddl_name=?",
                (self.profile_id, ddl_name.upper()),
            ).fetchone()
            if header is None:
                return None
            rows = conn.execute(
                "SELECT * FROM verdicts WHERE profile_id=? AND ddl_name=? ORDER BY seq",
                (self.profile_id, ddl_name.upper()),
            ).fetchall()
        assessment = self._revive_header(ddl_name, header)
        for row in rows:
            assessment.results.append(
                RuleResult(
                    rule_id=row["rule_id"],
                    outcome=Outcome(row["outcome"]),
                    severity=Severity(row["severity"]),
                    message=row["explanation"] or "",
                    ref=SourceRef(line=row["line"] or 0),
                    node=row["node"] or "",
                    sap_source=row["sap_source"] or "",
                    remediation=row["remediation"] or "",
                    detail=json.loads(row["detail"] or "{}"),
                )
            )
        return assessment

    # -- cardinality (F-14) -------------------------------------------------

    # -- snapshots (F-35) ---------------------------------------------------
    def take_snapshot(self, snapshot_id: str) -> dict:
        views = self.views()
        payload = {
            v["ddl_name"]: {
                "verdict": v["verdict"],
                "extraction_enabled": v["extraction_enabled"],
                "cdc_type": v["cdc_type"],
                "package": v["package"],
            }
            for v in views
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO snapshots (profile_id, snapshot_id, taken_at, "
                "view_count, hash, payload) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(profile_id, snapshot_id) DO UPDATE SET "
                "taken_at=excluded.taken_at, view_count=excluded.view_count, "
                "hash=excluded.hash, payload=excluded.payload",
                (self.profile_id, snapshot_id, _now(), len(views), digest,
                 json.dumps(payload, sort_keys=True)),
            )
            conn.commit()
        return payload

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                (self.profile_id, snapshot_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def snapshots(self) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT snapshot_id, taken_at, view_count, hash FROM snapshots "
                    "WHERE profile_id=? ORDER BY taken_at DESC",
                    (self.profile_id,),
                ).fetchall()
            ]

    # -- housekeeping ------------------------------------------------------
    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE profile_id=?",
                    (self.profile_id,),
                ).fetchone()[0]
                for table in ("views", "view_sources", "tables", "dependencies", "verdicts")
            }

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
