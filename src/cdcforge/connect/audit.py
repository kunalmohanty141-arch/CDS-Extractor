"""F-32 — the audit log.

Immutable, append-only. Every read and write: timestamp, user, system, client,
object, action, HTTP status, TR, verdict, outcome.

The specification is direct about why this is not a nice-to-have:

    It is what makes the tool acceptable to a client's Basis and audit teams,
    and that acceptance — not the code — is the real barrier to adoption.

Request and response *bodies* are never stored, only their SHA-256 hashes. The
tool moves metadata, not business data, and the audit log is the one place a
careless implementation would quietly start retaining source code and row
contents.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
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
"""

#: Header names whose values must never reach the log.
_REDACT = frozenset({"authorization", "cookie", "set-cookie", "x-csrf-token"})


def body_hash(body: bytes | str | None) -> str:
    if body is None:
        return ""
    data = body.encode("utf-8") if isinstance(body, str) else body
    return hashlib.sha256(data).hexdigest()[:32]


def safe_headers(headers: dict) -> dict:
    """Headers minus anything that authenticates."""
    return {
        k: ("<redacted>" if k.lower() in _REDACT else v) for k, v in headers.items()
    }


@dataclass
class AuditRecord:
    profile_id: str
    action: str
    method: str = ""
    path: str = ""
    object: str = ""
    http_status: int | None = None
    elapsed_ms: int | None = None
    request_hash: str = ""
    response_hash: str = ""
    ssl_verified: bool = True
    read_only: bool = True
    outcome: str = ""
    detail: str = ""
    system_id: str = ""
    client: str = ""
    user: str = ""


class AuditLog:
    """Append-only audit store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        #: Writes are serialised here rather than left to SQLite's own locking.
        #:
        #: Every record opens its own connection, which is fine until two
        #: threads do it at once — then one of them gets
        #: ``sqlite3.OperationalError: database is locked`` and the request it
        #: was recording dies with it. Measured the moment the first concurrent
        #: read path landed, and intermittently, which is the worst way to find
        #: out.
        #:
        #: An audit entry must never be the reason a request fails. This lock
        #: is cheap — the writes are tiny — and it makes the log safe for any
        #: concurrency the rest of the tool grows into.
        self._lock = threading.Lock()

        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def record(self, entry: AuditRecord) -> None:
        with self._lock, closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.execute(
                """
                INSERT INTO audit (
                    timestamp, profile_id, system_id, client, user, action,
                    method, path, object, http_status, elapsed_ms,
                    request_hash, response_hash, ssl_verified, read_only,
                    outcome, detail
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    entry.profile_id,
                    entry.system_id,
                    entry.client,
                    entry.user,
                    entry.action,
                    entry.method,
                    entry.path,
                    entry.object,
                    entry.http_status,
                    entry.elapsed_ms,
                    entry.request_hash,
                    entry.response_hash,
                    int(entry.ssl_verified),
                    int(entry.read_only),
                    entry.outcome,
                    entry.detail,
                ),
            )
            conn.commit()

    def upsert_system(self, **fields: str) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "profile_id")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                f"INSERT INTO systems ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(profile_id) DO UPDATE SET {updates}",
                tuple(fields.values()),
            )
            conn.commit()

    def entries(self, limit: int = 100) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]


class NullAuditLog(AuditLog):
    """Discards records. For unit tests only — never for a real session."""

    def __init__(self) -> None:
        self.path = Path(":memory:")
        self.records: list[AuditRecord] = []

    def record(self, entry: AuditRecord) -> None:
        self.records.append(entry)

    def upsert_system(self, **fields: str) -> None:
        return None

    def entries(self, limit: int = 100) -> list[dict]:
        return [vars(r) for r in self.records[-limit:]]

    def count(self) -> int:
        return len(self.records)
