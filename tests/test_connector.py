"""Connector tests — F-01, F-03, F-04, F-15, F-32.

No system required. The transport is faked, which is the point: the guards that
decide whether a request may leave, the CSRF dance and the retry policy are all
verifiable before anyone connects to anything.

The read-only and production guards get the most attention here, because they
are the two places where a bug writes to a customer's production system.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cdcforge.connect import endpoints as ep
from cdcforge.connect import sql
from cdcforge.connect.audit import AuditLog, NullAuditLog, body_hash, safe_headers
from cdcforge.connect.checkrun import Severity, compare, parse_check_messages
from cdcforge.connect.preflight import normalise_release
from cdcforge.connect.profile import (
    ConnectionProfile,
    CredentialError,
    SystemRole,
)
from cdcforge.connect.session import (
    AdtHttpError,
    AdtSession,
    AuthenticationFailed,
    AuthorizationFailed,
    HostUnreachable,
    ProductionGuardViolation,
    ReadOnlyViolation,
    SicfNodeInactive,
    TlsProblem,
)
from cdcforge.connect.source import AdtMetadataSource
from cdcforge.model import Verdict


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    headers: dict = field(default_factory=dict)


@dataclass
class FakeCall:
    method: str
    url: str
    headers: dict
    body: bytes | None


class FakeTransport:
    def __init__(self, responses=None, raises=None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls: list[FakeCall] = []
        self.closed = False

    def request(self, method, url, data=None, headers=None, params=None, timeout=None):
        self.calls.append(FakeCall(method, url, dict(headers or {}), data))
        if self.raises is not None:
            raise self.raises
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, "ok", {"x-csrf-token": "TOKEN123"})

    def close(self):
        self.closed = True


def make_profile(**overrides) -> ConnectionProfile:
    defaults = {
        "profile_id": "TEST",
        "host": "s4dev.corp.local",
        "client": "100",
        "username": "TESTER",
        "max_retries": 3,
    }
    defaults.update(overrides)
    return ConnectionProfile(**defaults)


def make_session(transport=None, *, read_only=True, role=None, **profile_kwargs):
    profile = make_profile(**profile_kwargs)
    session = AdtSession(
        profile,
        NullAuditLog(),
        read_only=read_only,
        transport=transport or FakeTransport(),
        password="secret",
        sleep=lambda _: None,
    )
    if role is not None:
        session.system_role = role
    return session


# ---------------------------------------------------------------------------
# Endpoint classification
# ---------------------------------------------------------------------------


def test_checkruns_is_a_read_despite_being_a_post():
    # The spec's literal wording ("blocks every non-GET") would disable the
    # single most important endpoint in the tool. Access, not verb, is the
    # correct boundary.
    assert ep.classify("POST", ep.CHECK_RUNS.path) is ep.Access.READ


def test_data_preview_is_a_read():
    assert ep.classify("POST", ep.DATA_PREVIEW.path) is ep.Access.READ


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", ep.CREATE_DDL_SOURCE.path),
        ("PUT", ep.UPDATE_DDL_SOURCE.url(name="ZI_X")),
        ("POST", ep.ACTIVATE.path),
        ("POST", ep.LOCK.url(name="ZI_X")),
        ("DELETE", "/sap/bc/adt/ddic/ddl/sources/ZI_X"),
    ],
)
def test_modifying_calls_are_classified_write(method, path):
    assert ep.classify(method, path) is ep.Access.WRITE


def test_unknown_post_path_is_write_not_read():
    # Guessing READ for an unrecognised path would put the kill switch's
    # promise on a coin flip.
    assert ep.classify("POST", "/sap/bc/adt/something/nobody/documented") is ep.Access.WRITE


def test_lock_cannot_smuggle_past_a_shared_read_prefix():
    # LOCK shares its path with the DDL object read and differs only in the
    # query string. Prefix matching classified it READ.
    assert ep.classify("POST", ep.LOCK.url(name="ZI_X")) is ep.Access.WRITE


def test_create_is_not_a_read_because_it_prefixes_the_object_read():
    # POST /ddic/ddl/sources creates; GET /ddic/ddl/sources/{name} reads.
    assert ep.classify("POST", ep.CREATE_DDL_SOURCE.path) is ep.Access.WRITE
    assert ep.classify("GET", ep.DDL_OBJECT.url(name="ZI_X")) is ep.Access.READ


def test_the_same_path_classifies_by_verb():
    # The source read and the source update are the same path, GET vs PUT.
    # Ignoring the verb in either direction breaks something important:
    # one way blocks every read, the other way permits every write.
    path = ep.DDL_SOURCE.url(name="ZI_X")
    assert ep.classify("GET", path) is ep.Access.READ
    assert ep.classify("PUT", path) is ep.Access.WRITE


def test_an_unexpected_verb_on_a_known_path_is_a_write():
    assert ep.classify("POST", ep.DDL_SOURCE.url(name="ZI_X")) is ep.Access.WRITE


# ---------------------------------------------------------------------------
# F-04 — read-only mode
# ---------------------------------------------------------------------------


def test_read_only_blocks_a_write_before_the_network():
    transport = FakeTransport()
    session = make_session(transport, read_only=True)
    with pytest.raises(ReadOnlyViolation):
        session.request(ep.ACTIVATE.path, "POST")
    assert transport.calls == []  # blocked at the connector, not by the server


def test_read_only_allows_checkruns():
    transport = FakeTransport([FakeResponse(200, "<messages/>")])
    session = make_session(transport, read_only=True)
    session.post(ep.CHECK_RUNS.path, body="<x/>")
    assert len(transport.calls) == 1


def test_read_only_is_the_default():
    assert make_session().read_only is True


# ---------------------------------------------------------------------------
# F-03 — production guard
# ---------------------------------------------------------------------------


def test_production_guard_blocks_writes_even_with_read_only_off():
    transport = FakeTransport()
    session = make_session(transport, read_only=False, role=SystemRole.PRODUCTION)
    with pytest.raises(ProductionGuardViolation):
        session.request(ep.ACTIVATE.path, "POST")
    assert transport.calls == []


def test_unknown_client_role_counts_as_production():
    session = make_session(read_only=False, role=SystemRole.UNKNOWN)
    assert session.system_role.is_productive
    with pytest.raises(ProductionGuardViolation):
        session.request(ep.ACTIVATE.path, "POST")


def test_development_role_permits_writes_when_read_only_is_off():
    transport = FakeTransport([FakeResponse(200, "activated")])
    session = make_session(transport, read_only=False, role=SystemRole.DEVELOPMENT)
    session.request(ep.ACTIVATE.path, "POST")
    assert len(transport.calls) == 1


def test_production_override_requires_the_exact_system_and_client():
    session = make_session(read_only=False, role=SystemRole.PRODUCTION)
    session.system_id = "PRD"

    assert session.authorise_production_writes("wrong", "100") is False
    assert session.authorise_production_writes("PRD", "200") is False
    with pytest.raises(ProductionGuardViolation):
        session.request(ep.ACTIVATE.path, "POST")

    assert session.authorise_production_writes("prd", "100") is True
    session._transport = FakeTransport([FakeResponse(200, "ok")])
    session.request(ep.ACTIVATE.path, "POST")


def test_reads_are_never_blocked_by_the_production_guard():
    transport = FakeTransport([FakeResponse(200, "source")])
    session = make_session(transport, read_only=True, role=SystemRole.PRODUCTION)
    session.get(ep.DDL_SOURCE.url(name="I_CURRENCY"))
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Session and CSRF
# ---------------------------------------------------------------------------


def test_connect_fetches_a_csrf_token():
    transport = FakeTransport([FakeResponse(200, "", {"x-csrf-token": "ABC"})])
    session = make_session(transport)
    session.connect()
    assert session.csrf_token == "ABC"
    assert transport.calls[0].headers["X-CSRF-Token"] == "Fetch"


def test_token_is_attached_to_writes_but_not_to_gets():
    transport = FakeTransport(
        [
            FakeResponse(200, "", {"x-csrf-token": "ABC"}),
            FakeResponse(200, "ok"),
            FakeResponse(200, "ok"),
        ]
    )
    session = make_session(transport)
    session.connect()
    session.post(ep.CHECK_RUNS.path, body="<x/>")
    session.get(ep.DDL_SOURCE.url(name="X"))
    assert transport.calls[1].headers["X-CSRF-Token"] == "ABC"
    assert "X-CSRF-Token" not in transport.calls[2].headers


def test_csrf_403_refreshes_once_and_retries():
    transport = FakeTransport(
        [
            FakeResponse(200, "", {"x-csrf-token": "OLD"}),          # connect
            FakeResponse(403, "CSRF token validation failed"),        # first try
            FakeResponse(200, "", {"x-csrf-token": "NEW"}),           # re-connect
            FakeResponse(200, "done"),                                # retry
        ]
    )
    session = make_session(transport)
    session.connect()
    response = session.post(ep.CHECK_RUNS.path, body="<x/>")
    assert response.text == "done"
    assert session.csrf_token == "NEW"


def test_csrf_retry_happens_only_once():
    transport = FakeTransport(
        [
            FakeResponse(200, "", {"x-csrf-token": "OLD"}),
            FakeResponse(403, "CSRF token validation failed"),
            FakeResponse(200, "", {"x-csrf-token": "NEW"}),
            FakeResponse(403, "CSRF token validation failed"),
        ]
    )
    session = make_session(transport)
    session.connect()
    with pytest.raises(AuthorizationFailed):
        session.post(ep.CHECK_RUNS.path, body="<x/>")


def test_5xx_is_retried_with_backoff():
    transport = FakeTransport(
        [FakeResponse(503, "busy"), FakeResponse(500, "busy"), FakeResponse(200, "ok")]
    )
    session = make_session(transport)
    assert session.get(ep.DDL_SOURCE.url(name="X")).text == "ok"
    assert len(transport.calls) == 3


def test_4xx_is_not_retried():
    # Asserting the specific type matters: `raises(Exception)` would also pass
    # if the session threw a TypeError on a refactor, so the test would keep
    # reporting green while the retry policy was broken.
    transport = FakeTransport([FakeResponse(404, "not found")])
    session = make_session(transport)
    with pytest.raises(AdtHttpError) as exc:
        session.get(ep.DDL_SOURCE.url(name="MISSING"))
    assert exc.value.status == 404
    assert len(transport.calls) == 1


def test_retries_give_up_after_max_retries():
    transport = FakeTransport([FakeResponse(500, "boom")] * 5)
    session = make_session(transport, max_retries=3)
    with pytest.raises(AdtHttpError) as exc:
        session.get(ep.DDL_SOURCE.url(name="X"))
    assert exc.value.status == 500
    assert len(transport.calls) == 3


def test_logoff_closes_the_transport():
    transport = FakeTransport()
    session = make_session(transport)
    session.logoff()
    assert transport.closed is True


# ---------------------------------------------------------------------------
# F-01 — failures name their cause, never a stack trace
# ---------------------------------------------------------------------------


def test_401_is_reported_as_authentication():
    session = make_session(FakeTransport([FakeResponse(401, "unauthorized")]))
    with pytest.raises(AuthenticationFailed) as exc:
        session.get(ep.DDL_SOURCE.url(name="X"))
    assert "credentials" in exc.value.message
    assert exc.value.remedy


def test_403_is_reported_as_authorization_with_the_right_object():
    session = make_session(FakeTransport([FakeResponse(403, "no auth")]))
    with pytest.raises(AuthorizationFailed) as exc:
        session.get(ep.DDL_SOURCE.url(name="X"))
    assert "S_DEVELOP" in exc.value.remedy


def test_404_on_discovery_is_diagnosed_as_an_inactive_sicf_node():
    session = make_session(FakeTransport([FakeResponse(404, "")]))
    with pytest.raises(SicfNodeInactive) as exc:
        session.connect()
    assert "SICF" in exc.value.remedy


def test_dns_failure_is_named():
    session = make_session(FakeTransport(raises=OSError("getaddrinfo failed")))
    with pytest.raises(HostUnreachable) as exc:
        session.connect()
    assert "resolved" in exc.value.message
    assert "VPN" in exc.value.remedy


def test_tls_failure_is_named_and_suggests_the_ca_bundle():
    class SSLError(Exception):
        pass

    session = make_session(FakeTransport(raises=SSLError("certificate verify failed")))
    with pytest.raises(TlsProblem) as exc:
        session.connect()
    assert "ca_bundle_path" in exc.value.remedy


def test_connection_refused_is_named():
    session = make_session(FakeTransport(raises=OSError("connection refused")))
    with pytest.raises(HostUnreachable) as exc:
        session.connect()
    assert "SMICM" in exc.value.remedy


# ---------------------------------------------------------------------------
# F-32 — audit
# ---------------------------------------------------------------------------


def test_every_request_is_audited(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    profile = make_profile()
    session = AdtSession(
        profile, log, transport=FakeTransport([FakeResponse(200, "body")]),
        password="secret", sleep=lambda _: None,
    )
    session.get(ep.DDL_SOURCE.url(name="I_CURRENCY"), object_name="I_CURRENCY")
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0]["object"] == "I_CURRENCY"
    assert entries[0]["http_status"] == 200
    assert entries[0]["read_only"] == 1


def test_audit_stores_body_hashes_not_bodies(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    session = AdtSession(
        make_profile(), log,
        transport=FakeTransport([FakeResponse(200, "SECRET RESPONSE BODY")]),
        password="secret", sleep=lambda _: None,
    )
    session.post(ep.CHECK_RUNS.path, body="SECRET REQUEST BODY")
    entry = log.entries()[0]
    assert "SECRET" not in str(entry)
    assert entry["response_hash"] == body_hash("SECRET RESPONSE BODY")


def test_audit_redacts_authenticating_headers():
    redacted = safe_headers(
        {"Authorization": "Basic abc", "Cookie": "SAP_SESSIONID=x", "Accept": "application/*"}
    )
    assert redacted["Authorization"] == "<redacted>"
    assert redacted["Cookie"] == "<redacted>"
    assert redacted["Accept"] == "application/*"


def test_insecure_sessions_are_stamped(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    session = AdtSession(
        make_profile(verify_ssl=False), log,
        transport=FakeTransport([FakeResponse(200, "x")]),
        password="secret", sleep=lambda _: None,
    )
    session.get(ep.DDL_SOURCE.url(name="X"))
    assert log.entries()[0]["ssl_verified"] == 0


def test_a_blocked_write_still_never_reaches_the_network():
    transport = FakeTransport()
    session = make_session(transport)
    for path, method in [
        (ep.ACTIVATE.path, "POST"),
        (ep.UPDATE_DDL_SOURCE.url(name="X"), "PUT"),
        (ep.CREATE_DDL_SOURCE.path, "POST"),
    ]:
        with pytest.raises(ReadOnlyViolation):
            session.request(path, method)
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Metadata queries
# ---------------------------------------------------------------------------


def test_query_allowlist_refuses_business_tables():
    with pytest.raises(sql.QueryNotPermitted):
        sql.run_query(make_session(), "SELECT * FROM VBAK")


def test_query_allowlist_permits_metadata_tables():
    transport = FakeTransport([FakeResponse(200, "<empty/>")])
    session = make_session(transport)
    sql.run_query(session, sql.table_header_query("ZCUSTORDER"))
    assert len(transport.calls) == 1


def test_query_names_cannot_escape_their_literal():
    query = sql.table_fields_query("ZTAB'; DROP TABLE X --")
    literal = query.split("TABNAME = '", 1)[1].split("'", 1)[0]
    assert "'" not in literal
    assert literal == "ZTABDROPTABLEX"  # everything unsafe is stripped, not escaped


class _NamedTransport:
    """Serves a DDL source at one name only, plus the extraction-view table."""

    EXTRACTION_ROWS = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<dataPreview:tableData xmlns:dataPreview="http://www.sap.com/adt/dataPreview">'
        "<dataPreview:columns>"
        '<dataPreview:metadata dataPreview:name="VIEWNAME" dataPreview:type="C"/>'
        "<dataPreview:dataSet><dataPreview:data>ZCDS_2RFLOWS</dataPreview:data>"
        "</dataPreview:dataSet></dataPreview:columns>"
        "<dataPreview:columns>"
        '<dataPreview:metadata dataPreview:name="RELEASE_STATE" dataPreview:type="C"/>'
        "<dataPreview:dataSet><dataPreview:data>C1</dataPreview:data>"
        "</dataPreview:dataSet></dataPreview:columns>"
        "<dataPreview:columns>"
        '<dataPreview:metadata dataPreview:name="DDLNAME" dataPreview:type="C"/>'
        "<dataPreview:dataSet><dataPreview:data>ZCDS_RFLOW1</dataPreview:data>"
        "</dataPreview:dataSet></dataPreview:columns>"
        "</dataPreview:tableData>"
    )

    def __init__(self, serves: str) -> None:
        self.serves = serves.lower()
        self.reads: list[str] = []

    def request(self, method, url, data=None, headers=None, params=None, timeout=None):
        if "/datapreview/" in url:
            return FakeResponse(200, self.EXTRACTION_ROWS)
        if "/ddic/ddl/sources/" in url:
            # ADT is case-insensitive here; the connector sends upper case.
            name = url.split("/ddic/ddl/sources/")[1].split("/")[0].lower()
            self.reads.append(name)
            if name != self.serves:
                return FakeResponse(404, "not found")
            return FakeResponse(200, "define view ZCDS_2RFLOWS as select from x {}")
        return FakeResponse(200, "ok", {"x-csrf-token": "T"})

    def close(self):
        pass


def test_a_source_is_found_by_its_ddl_name_when_the_view_name_differs():
    """A CDS view has three names and they need not agree.

    Measured on a real system::

        VIEWNAME      ZCDS_2RFLOWS   the view
        SQL_VIEWNAME  ZCDS2RFLWS     the database view
        DDLNAME       ZCDS_RFLOW1    the DDL source object

    ADT reads source by DDL source name, so asking for ZCDS_2RFLOWS answers
    404 — and the tool reported a C1-released, delta-supported view as
    deleted. The user was told one of their objects was broken. It was not.
    """
    transport = _NamedTransport(serves="zcds_rflow1")
    source = AdtMetadataSource(make_session(transport))

    assert source.get_view_source("ZCDS_2RFLOWS") is not None
    assert transport.reads == ["zcds_2rflows", "zcds_rflow1"], (
        "the view name is tried first; the DDL name is a fallback on a miss"
    )


def test_the_ddl_name_fallback_costs_nothing_when_the_name_matches():
    transport = _NamedTransport(serves="zcds_2rflows")
    source = AdtMetadataSource(make_session(transport))

    assert source.get_view_source("ZCDS_2RFLOWS") is not None
    assert transport.reads == ["zcds_2rflows"], "no second read on the happy path"


def test_an_object_that_really_is_missing_is_still_missing():
    transport = _NamedTransport(serves="something_else")
    source = AdtMetadataSource(make_session(transport))
    assert source.get_view_source("ZI_GONE") is None


def test_prefetch_warms_the_cache_without_changing_what_is_read():
    """A hint, not a mechanism. The reads are the same reads."""
    transport = _NamedTransport(serves="zi_a")
    source = AdtMetadataSource(make_session(transport), fetch_workers=4)

    source.prefetch_sources(["ZI_A", "ZI_B", "ZI_C"])
    assert sorted(transport.reads) == ["zi_a", "zi_b", "zi_c"]

    before = len(transport.reads)
    assert source.get_view_source("ZI_A") is not None
    assert len(transport.reads) == before, "a warmed source is not read again"


def test_a_prefetch_miss_is_not_cached():
    """Caching the miss would skip the DDL-name fallback, and that fallback is
    the difference between finding ZCDS_2RFLOWS and declaring it deleted."""
    transport = _NamedTransport(serves="zcds_rflow1")
    source = AdtMetadataSource(make_session(transport), fetch_workers=4)

    source.prefetch_sources(["ZCDS_2RFLOWS"])  # single name: no pool, no cache
    transport.reads.clear()

    assert source.get_view_source("ZCDS_2RFLOWS") is not None
    assert "zcds_rflow1" in transport.reads, "the fallback must still run"


def test_a_prefetch_miss_in_a_batch_is_not_cached_either():
    transport = _NamedTransport(serves="zcds_rflow1")
    source = AdtMetadataSource(make_session(transport), fetch_workers=4)

    source.prefetch_sources(["ZCDS_2RFLOWS", "ZI_OTHER"])
    transport.reads.clear()

    assert source.get_view_source("ZCDS_2RFLOWS") is not None
    assert "zcds_rflow1" in transport.reads


def test_one_worker_reads_serially_and_still_works():
    """The escape hatch for a system that should not be asked for six at once."""
    transport = _NamedTransport(serves="zi_a")
    source = AdtMetadataSource(make_session(transport), fetch_workers=1)

    source.prefetch_sources(["ZI_A", "ZI_B"])
    assert transport.reads == [], "no pool, and nothing read speculatively"
    assert source.get_view_source("ZI_A") is not None


def test_the_cache_layer_only_asks_for_genuine_misses(tmp_path):
    from cdcforge.cache import CachedMetadataSource
    from cdcforge.store import Store

    class _Counting(AdtMetadataSource):
        def __init__(self):
            self.asked: list[list[str]] = []
            self.sources = {"ZI_A": "define view entity ZI_A as select from t {}"}

        def prefetch_sources(self, names):
            self.asked.append(list(names))

        def get_view_source(self, name):
            return self.sources.get(name.upper())

    inner = _Counting()
    cached = CachedMetadataSource(
        inner, Store(tmp_path / "c.sqlite", profile_id="T")
    )
    cached.get_view_source("ZI_A")          # now in the store
    cached.prefetch_sources(["ZI_A", "ZI_B", "ZI_C"])

    assert inner.asked == [["ZI_B", "ZI_C"]], (
        "round trips belong to misses; a warm cache should ask for nothing"
    )


class _QueryCountingTransport:
    """Answers every freestyle query with one TADIR row, and counts them."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def request(self, method, url, data=None, headers=None, params=None, timeout=None):
        if "/datapreview/" in url:
            body = data.decode() if isinstance(data, bytes) else str(data or "")
            self.queries.append(body)
            return FakeResponse(200, self._rows())
        return FakeResponse(200, "ok", {"x-csrf-token": "T"})

    @staticmethod
    def _rows() -> str:
        def column(name, *values):
            data = "".join(f"<dataPreview:data>{v}</dataPreview:data>" for v in values)
            return (
                "<dataPreview:columns>"
                f'<dataPreview:metadata dataPreview:name="{name}" '
                'dataPreview:type="C"/>'
                f"<dataPreview:dataSet>{data}</dataPreview:dataSet>"
                "</dataPreview:columns>"
            )

        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<dataPreview:tableData '
            'xmlns:dataPreview="http://www.sap.com/adt/dataPreview">'
            + column("OBJ_NAME", "ZI_A", "ZI_B", "ZI_C")
            + column("DEVCLASS", "$TMP", "$TMP", "ZPKG")
            + column("AUTHOR", "X", "X", "X")
            + "</dataPreview:tableData>"
        )

    def close(self):
        pass


def test_object_headers_come_from_one_directory_read_not_one_query_each():
    """The single most expensive thing this tool did at scale.

    Screening T001's readers asked TADIR 461 times — 417 of them one object at
    a time from the stack walk, which cannot batch because it does not know
    its object names until it gets there. Each freestyle query generates a
    program on the target system, and that is how a development box's subpool
    directory fills up.

    Measured after this change: 461 TADIR queries became 2, and the whole
    screen went from 559 queries to 100.
    """
    transport = _QueryCountingTransport()
    source = AdtMetadataSource(make_session(transport))

    for name in ("ZI_A", "ZI_B", "ZI_C", "ZI_A"):
        source.get_object(name)

    directory_reads = [q for q in transport.queries if "OBJECT = 'DDLS'" in q]
    per_object = [q for q in transport.queries if "OBJ_NAME = '" in q]
    assert len(directory_reads) == 1, "the directory is read once for the run"
    assert per_object == [], "and never one object at a time"


def test_release_state_comes_from_set_membership_not_a_like_pattern():
    """The bulk path is also the more correct one.

    The per-object query matched ``OBJ_NAME LIKE '<name>%DDLS'``, and ``_`` is
    a SQL single-character wildcard as well as the commonest character in a CDS
    view name. ``Z_I_CUSTBASIC`` also matches ``ZAIBCUSTBASIC…DDLS``, so a view
    could be reported released because a *different* released view resembled
    it — and "released" is what suppresses the upgrade caveat.
    """
    from cdcforge.metadata.types import ApiState

    class _Source(AdtMetadataSource):
        def __init__(self):
            self._states = {}
            self._ddl_name_map = {}
            self._released = {"ZAIBCUSTBASIC"}   # a lookalike, not our object
            self._directory = {"Z_I_CUSTBASIC": "$TMP"}
            self._objects = {}
            self.use_queries = True

    assert _Source()._read_api_state("Z_I_CUSTBASIC") is ApiState.NOT_RELEASED


def test_an_object_absent_from_the_directory_is_absent():
    transport = _QueryCountingTransport()
    source = AdtMetadataSource(make_session(transport))
    assert source.get_object("ZI_NOWHERE") is None


def test_the_per_object_path_still_works_when_the_directory_cannot_be_read():
    """A bulk read that fails must degrade, not disable."""

    class _NoDirectory(_QueryCountingTransport):
        def request(self, method, url, data=None, headers=None, params=None, timeout=None):
            body = data.decode() if isinstance(data, bytes) else str(data or "")
            if "OBJECT = 'DDLS'" in body and "OBJ_NAME" not in body.split("WHERE")[-1]:
                return FakeResponse(500, "no")
            return super().request(method, url, data, headers, params, timeout)

    transport = _NoDirectory()
    source = AdtMetadataSource(make_session(transport))
    assert source.get_object("ZI_A") is not None


def test_the_cache_layer_forwards_delta_supported_views(tmp_path):
    """The bug that made the delta index silently unavailable.

    `delta_supported_views` was implemented on AdtMetadataSource and not on the
    cache that wraps it, so the call fell through to the base class default of
    `None` — and the index reported "the system did not report which views
    carry delta" on a system that reports it perfectly well. Every method the
    connector grows has to be forwarded, and a `None` default makes forgetting
    silent.
    """
    from cdcforge.cache import CachedMetadataSource
    from cdcforge.store import Store

    class _Inner(AdtMetadataSource):
        def __init__(self):
            self.calls = 0

        def delta_supported_views(self):
            self.calls += 1
            return {"C_THINGDEX"}

    inner = _Inner()
    cached = CachedMetadataSource(inner, Store(tmp_path / "c.sqlite", profile_id="T"))

    assert cached.delta_supported_views() == {"C_THINGDEX"}
    cached.delta_supported_views()
    assert inner.calls <= 2, "the answer is cached, not re-asked every time"


def test_every_metadata_reader_is_forwarded_by_the_cache():
    """Pins the class of bug above rather than the one instance.

    A reader present on the ADT source but missing from the cache returns the
    base class's `None`, which every caller is built to treat as "cannot
    answer" — so the feature disappears without an error.
    """
    import inspect

    from cdcforge.cache import CachedMetadataSource
    from cdcforge.metadata.base import MetadataSource

    optional = {
        name
        for name, member in inspect.getmembers(MetadataSource, inspect.isfunction)
        if not name.startswith("_")
        and name in vars(AdtMetadataSource)
        and name in vars(MetadataSource)
    }
    missing = sorted(optional - set(vars(CachedMetadataSource)))
    assert not missing, (
        f"CachedMetadataSource does not forward {missing} — callers will get "
        f"the base class default and quietly lose the feature"
    )


def test_the_audit_log_survives_concurrent_writers(tmp_path):
    """An audit entry must never be the reason a request fails.

    Every record opens its own SQLite connection, which is fine until two
    threads do it at once — then one gets "database is locked" and the request
    it was recording dies with it. Found the moment the first concurrent read
    path landed, and intermittently.
    """
    from concurrent.futures import ThreadPoolExecutor

    from cdcforge.connect.audit import AuditLog, AuditRecord

    log = AuditLog(tmp_path / "audit.sqlite")

    def write(index: int) -> None:
        log.record(AuditRecord(profile_id="T", action=f"probe-{index}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(120)))

    assert log.count() == 120, "every write must land, none may raise"


def test_a_wildcard_filter_is_honoured_as_a_prefix():
    """`--filter Z%` means "custom objects", not "contains a Z".

    Measured: the first cut stripped % as an illegal DDIC character and always
    searched for a substring, so an inventory of custom extraction views came
    back holding C_CHEMICALGHSHAZARDCLASS — which contains a Z. A list of
    "what we have built" seeded with SAP's own views is worse than no list.
    """
    query = sql.extraction_enabled_query("Z%")
    assert "LIKE 'Z%'" in query
    assert "'%Z%'" not in query


def test_a_filter_without_a_wildcard_is_still_a_substring_search():
    # What anyone expects from a search box.
    assert "LIKE '%ACDOCA%'" in sql.extraction_enabled_query("ACDOCA")


def test_an_underscore_in_a_filter_is_literal_not_a_wildcard():
    """`_` is a SQL single-character wildcard and a legal DDIC character.

    Everyone typing ZI_KNA1 means the underscore, so it is escaped rather than
    passed through — otherwise the filter also matches ZIXKNA1.
    """
    query = sql.extraction_enabled_query("ZI_KNA1")
    assert "ZI#_KNA1" in query
    assert "ESCAPE '#'" in query


def test_a_filter_still_cannot_escape_its_literal():
    query = sql.extraction_enabled_query("Z%'; DROP TABLE X --")
    literal = query.split("LIKE '", 1)[1].split("'", 1)[0]
    assert "'" not in literal


DATA_PREVIEW_XML = """<?xml version="1.0" encoding="utf-8"?>
<dataPreview:tableData xmlns:dataPreview="http://www.sap.com/adt/dataPreview">
  <dataPreview:columns>
    <dataPreview:metadata dataPreview:name="TABNAME" dataPreview:type="C"/>
    <dataPreview:dataSet>
      <dataPreview:data>ZCUSTORDER</dataPreview:data>
      <dataPreview:data>ZORDERITEM</dataPreview:data>
    </dataPreview:dataSet>
  </dataPreview:columns>
  <dataPreview:columns>
    <dataPreview:metadata dataPreview:name="TABCLASS" dataPreview:type="C"/>
    <dataPreview:dataSet>
      <dataPreview:data>TRANSP</dataPreview:data>
      <dataPreview:data>TRANSP</dataPreview:data>
    </dataPreview:dataSet>
  </dataPreview:columns>
</dataPreview:tableData>
"""


def test_data_preview_is_parsed_into_rows():
    result = sql.parse_data_preview(DATA_PREVIEW_XML)
    assert result.columns == ["TABNAME", "TABCLASS"]
    assert result.rows[0] == {"TABNAME": "ZCUSTORDER", "TABCLASS": "TRANSP"}
    assert len(result.rows) == 2


def test_unparseable_response_is_distinguishable_from_an_empty_one():
    unreadable = sql.parse_data_preview("<html>login page</html>")
    empty = sql.parse_data_preview(DATA_PREVIEW_XML.replace("ZCUSTORDER", ""))
    assert unreadable.parsed is False
    assert empty.parsed is True
    assert unreadable.raw  # kept for diagnosis


# ---------------------------------------------------------------------------
# AdtMetadataSource — the Stage 1 seam
# ---------------------------------------------------------------------------


def test_metadata_source_returns_none_rather_than_guessing():
    session = make_session(FakeTransport([FakeResponse(404, "")]))
    source = AdtMetadataSource(session)
    assert source.get_view_source("ZI_MISSING") is None


def test_metadata_source_drives_the_unchanged_rule_engine():
    """The payoff of the Stage 1 interface: same rules, live source."""
    ddl = (
        "@Analytics: { dataExtraction: { enabled: true,"
        " delta.changeDataCapture.automatic: true } }\n"
        "define view entity ZI_LIVE as select from zcustorder"
        " { key orderid as OrderId, sum(amount) as Total }"
    )
    session = make_session(FakeTransport([FakeResponse(200, ddl)]))
    source = AdtMetadataSource(session, use_queries=False)

    from cdcforge.rules import validate_object

    assessment = validate_object("ZI_LIVE", source)
    assert assessment.verdict is Verdict.FAIL_HARD
    assert assessment.outcome_of("R-03") is not None


def test_source_is_cached_so_a_scan_does_not_refetch():
    transport = FakeTransport([FakeResponse(200, "define view entity X as select from t { key t.k as K }")])
    source = AdtMetadataSource(make_session(transport))
    source.get_view_source("X")
    source.get_view_source("X")
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# F-15 — checkrun parsing and agreement
# ---------------------------------------------------------------------------


CHECK_XML = """<?xml version="1.0" encoding="utf-8"?>
<chkrun:checkRunReports xmlns:chkrun="http://www.sap.com/adt/checkrun">
  <chkrun:checkReport chkrun:reporter="abapCheckRun"
                      chkrun:triggeringUri="/sap/bc/adt/ddic/ddl/sources/ZI_X"
                      chkrun:status="processed"
                      chkrun:statusText="Object ZI_X has been checked">
    <chkrun:checkMessageList>
      <chkrun:checkMessage chkrun:uri="/sap/bc/adt/ddic/ddl/sources/ZI_X#start=22,3"
                           chkrun:type="E"
                           chkrun:code="RSODP_ABAP_CDS(201)"
                           chkrun:shortText="Change data capture annotations are incompatible"/>
      <chkrun:checkMessage chkrun:type="W" chkrun:shortText="Cardinality may be wrong"/>
    </chkrun:checkMessageList>
  </chkrun:checkReport>
</chkrun:checkRunReports>
"""

# The real shape returned by S/4HANA for a view with nothing wrong.
CLEAN_CHECK_XML = """<?xml version="1.0" encoding="utf-8"?>
<chkrun:checkRunReports xmlns:chkrun="http://www.sap.com/adt/checkrun">
  <chkrun:checkReport chkrun:reporter="abapCheckRun" chkrun:status="processed"
                      chkrun:statusText="Object I_CURRENCY has been checked">
    <chkrun:checkMessageList/>
  </chkrun:checkReport>
</chkrun:checkRunReports>
"""


def test_check_messages_are_parsed_with_severity_and_line():
    messages = parse_check_messages(CHECK_XML)
    assert len(messages) == 2
    assert messages[0].severity is Severity.ERROR
    assert messages[0].line == 22
    assert "incompatible" in messages[0].text
    assert messages[1].severity is Severity.WARNING


def test_message_code_is_captured():
    # The code is stable across languages and releases; the short text is not,
    # so a divergence log keyed on the code stays useful after translation.
    assert parse_check_messages(CHECK_XML)[0].code == "RSODP_ABAP_CDS(201)"


def test_report_status_is_read_separately_from_messages():
    from cdcforge.connect.checkrun import parse_report_status

    status, status_text, reporter = parse_report_status(CLEAN_CHECK_XML)
    assert status == "processed"
    assert reporter == "abapCheckRun"
    assert "has been checked" in status_text


def test_a_check_that_never_ran_is_not_treated_as_clean():
    """No messages and no status look identical, and mean opposite things.

    Treating 'not checked' as 'checked and fine' would be the false PASS this
    tool exists to prevent, arriving from the delegation side.
    """
    from cdcforge.connect.checkrun import CheckRunResult

    never_ran = CheckRunResult(object_name="ZI_X", status="")
    assert never_ran.messages == []
    assert never_ran.ran is False
    assert never_ran.clean is False
    assert "did not run" in never_ran.summary

    genuinely_clean = CheckRunResult(object_name="ZI_X", status="processed")
    assert genuinely_clean.clean is True
    assert genuinely_clean.summary == "clean"


def test_agreement_flags_a_false_pass(assess):
    from cdcforge.connect.checkrun import CheckRunResult

    assessment = assess("ZI_BUSINESSAREA")
    check = CheckRunResult(object_name="ZI_BUSINESSAREA", status="processed")
    check.messages = parse_check_messages(CHECK_XML)

    agreement = compare(assessment, check)
    assert agreement.my_verdict is Verdict.PASS
    assert agreement.sap_clean is False
    assert agreement.false_pass is True
    assert "DIVERGENCE" in agreement.render()


def test_agreement_is_silent_when_the_checkrun_could_not_run(assess):
    from cdcforge.connect.checkrun import CheckRunResult

    check = CheckRunResult(object_name="X", reachable=False, error="404")
    agreement = compare(assess("ZI_BUSINESSAREA"), check)
    assert agreement.sap_clean is None
    assert agreement.diverged is False


# ---------------------------------------------------------------------------
# Profile and release helpers
# ---------------------------------------------------------------------------


def test_profile_round_trips_without_the_password(tmp_path):
    profile = make_profile()
    profile._password = "should-not-be-written"
    path = profile.save(tmp_path)
    assert "should-not-be-written" not in path.read_text(encoding="utf-8")
    assert "password" not in path.read_text(encoding="utf-8").lower()

    loaded = ConnectionProfile.load("TEST", tmp_path)
    assert loaded.host == profile.host
    assert loaded._password is None


def test_a_password_written_into_the_profile_file_is_ignored(tmp_path):
    path = tmp_path / "P.yaml"
    path.write_text(
        "profile_id: P\nhost: h\nclient: '100'\nusername: U\npassword: hunter2\n",
        encoding="utf-8",
    )
    loaded = ConnectionProfile.from_file(path)
    assert loaded._password is None


def test_unverified_tls_without_a_stated_reason_is_refused():
    """The password crosses the wire on every request.

    Authentication is HTTP Basic, so an unverified connection is not a
    cosmetic warning — it is *anyone on the path can read the password*.
    Self-signed certificates are genuinely normal on sandboxes, so this is not
    a ban; it is a second key. `verify_ssl: false` alone is one word in a file
    that gets copied from colleague to colleague, and it copies silently.
    """
    profile = make_profile(verify_ssl=False)
    with pytest.raises(CredentialError, match="no reason"):
        profile.check_tls()


def test_a_stated_reason_permits_it():
    profile = make_profile(
        verify_ssl=False, tls_override_reason="self-signed cert, isolated lab"
    )
    profile.check_tls()  # must not raise
    assert "isolated lab" in profile.tls_warning()
    assert "TLS NOT VERIFIED" in profile.tls_warning()


def test_verified_tls_needs_no_reason_and_warns_about_nothing():
    profile = make_profile(verify_ssl=True)
    profile.check_tls()
    assert profile.tls_warning() == ""


def test_the_check_runs_before_the_password_can_be_sent():
    """Refused at construction, so no request is ever built.

    Warning after the fact would be warning after the password had already
    gone out, which is the one moment that matters.
    """
    from cdcforge.connect.audit import NullAuditLog

    profile = make_profile(verify_ssl=False)
    with pytest.raises(CredentialError):
        AdtSession(profile, NullAuditLog(), password="secret")


def test_a_profile_is_written_owner_readable_where_the_os_allows(tmp_path):
    """It holds no password — but it names a host, a client and a user with
    development authorisation on it. That is a starting point for somebody."""
    import os
    import stat

    path = make_profile().save(tmp_path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_profile_keys_are_rejected(tmp_path):
    path = tmp_path / "P.yaml"
    path.write_text(
        "profile_id: P\nhost: h\nclient: '100'\nusername: U\ntypo_key: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ConnectionProfile.from_file(path)


@pytest.mark.parametrize(
    "raw,expected",
    [("7.54", 754), ("754", 754), ("SAP_BASIS 755", 755), ("", None), ("x", None)],
)
def test_release_normalisation(raw, expected):
    assert normalise_release(raw) == expected


@pytest.mark.parametrize(
    "category,productive",
    [("P", True), ("", True), ("D", False), ("T", False), ("C", False)],
)
def test_client_role_parsing_fails_safe(category, productive):
    assert SystemRole.parse(category).is_productive is productive
