"""Set-based metadata reads via the ADT data preview service.

§3.6 of the specification: ADT REST is object-oriented and slow for bulk reads,
and the inventory sweep needs set-based access to metadata tables. Of the three
options offered, this is the zero-install one — it is how Eclipse's own Data
Preview reads a table, so it needs no server-side object and no database user.

It reads the metadata tables listed in Appendix D — DD02L, DD03L, TADIR, T000 —
whose columns *are* documented, rather than depending on the undocumented XML
schema of ``/sap/bc/adt/ddic/tables/{name}``. Appendix D.6 still applies: read
defensively, never assume a column exists.

Nothing here reads business data.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from cdcforge.connect import endpoints as ep
from cdcforge.connect.session import AdtSession

#: Tables this module is permitted to query. An allowlist, not a convenience:
#: a freestyle SELECT endpoint is exactly the thing that could quietly turn a
#: metadata tool into a data-extraction tool, and the product's central claim
#: is that it moves metadata and never business data.
ALLOWED_TABLES = frozenset(
    {
        "CVERS",
        "T000",
        "DD02L",
        "DD03L",
        "DD02T",
        "DD04T",
        "TADIR",
        "TDEVC",
        "DDLDEPENDENCY",
        "DDLS_RIS_INDEX",
        "DDHEADANNO",
        "CDSVIEWANNO",
        "CDSVIEWCROSSREF",
        "RSODPABAPCDSVIEW",
        "DHCDCVCDSEXTRE",
    }
)

_TABLE_IN_FROM = re.compile(r"\bfrom\s+([A-Za-z0-9_/]+)", re.IGNORECASE)


class QueryNotPermitted(Exception):
    """The query touches something outside the metadata allowlist."""


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    raw: str = ""
    """The unparsed response, kept so an unfamiliar release can be diagnosed."""

    @property
    def parsed(self) -> bool:
        return bool(self.columns)

    def column(self, name: str) -> list[str]:
        key = name.upper()
        return [r.get(key, "") for r in self.rows]

    def first(self) -> dict[str, str]:
        return self.rows[0] if self.rows else {}


def _assert_permitted(query: str) -> None:
    tables = {m.upper().lstrip("/") for m in _TABLE_IN_FROM.findall(query)}
    tables = {t.split("~")[0] for t in tables}
    forbidden = {t for t in tables if t not in ALLOWED_TABLES}
    if forbidden:
        raise QueryNotPermitted(
            f"query touches {sorted(forbidden)}, which is outside the metadata "
            f"allowlist. This tool reads metadata, never business data."
        )


def run_query(session: AdtSession, query: str, *, max_rows: int = 500) -> QueryResult:
    """Run a freestyle SELECT against a metadata table."""
    _assert_permitted(query)
    # Accept and Content-Type come from the endpoint map, so a release that
    # negotiates differently is one edit there rather than a change here.
    response = session.post(
        ep.DATA_PREVIEW.path,
        body=query,
        params={"rowNumber": str(max_rows)},
        action="metadata-query",
    )
    return parse_data_preview(response.text)


def parse_data_preview(text: str) -> QueryResult:
    """Parse an ADT data-preview response.

    The wire format is not published. This handles the shape the service is
    observed to return — a column block per column, each carrying a name and an
    ordered list of values — and, when it does not recognise the payload, says
    so by returning an unparsed result rather than an empty one. An empty result
    and an unreadable one mean very different things to the caller.
    """
    result = QueryResult(raw=text)
    if not text.strip():
        return result

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return result

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    columns: list[tuple[str, list[str]]] = []
    for element in root.iter():
        if local(element.tag) != "columns":
            continue
        name = ""
        values: list[str] = []
        for child in element.iter():
            tag = local(child.tag)
            if tag == "metadata":
                for key, value in child.attrib.items():
                    if local(key) == "name":
                        name = value
            elif tag == "data":
                values.append((child.text or "").strip())
        if name:
            columns.append((name.upper(), values))

    if not columns:
        return result

    result.columns = [name for name, _ in columns]
    height = max((len(v) for _, v in columns), default=0)
    result.rows = [
        {name: (values[i] if i < len(values) else "") for name, values in columns}
        for i in range(height)
    ]
    return result


# ---------------------------------------------------------------------------
# The specific queries the tool needs
# ---------------------------------------------------------------------------


def release_query() -> str:
    """CVERS — installed software component versions.

    Used because ``/sap/bc/adt/core/systeminformation`` does not exist on every
    release (S/4HANA 2025 answers 404). CVERS is stable, documented, and the
    release it reports for SAP_BASIS is exactly what the CDC and view-entity
    capability checks need.
    """
    return "SELECT COMPONENT, RELEASE, EXTRELEASE FROM CVERS"


def client_role_query(client: str) -> str:
    """T000 — production detection (F-03)."""
    safe = "".join(ch for ch in client if ch.isalnum())
    return f"SELECT MANDT, CCCATEGORY, MTEXT FROM T000 WHERE MANDT = '{safe}'"


def table_header_query(table: str) -> str:
    """DD02L — table class and delivery class."""
    safe = _safe_name(table)
    return (
        f"SELECT TABNAME, TABCLASS, CONTFLAG, AS4LOCAL FROM DD02L "
        f"WHERE TABNAME = '{safe}' AND AS4LOCAL = 'A'"
    )


def table_fields_query(table: str) -> str:
    """DD03L — field-level metadata, including the key flags."""
    safe = _safe_name(table)
    return (
        # REFTABLE/REFFIELD matter more than they look. A CURR or QUAN column
        # is meaningless without the currency or unit it is measured in, and
        # DDIC records where that lives. Without them the generator cannot tell
        # a locally-referenced amount (which SAP resolves by itself) from one
        # whose currency sits in another table (which SAP refuses outright),
        # so it emitted DDL that could not activate for most business tables.
        f"SELECT TABNAME, FIELDNAME, POSITION, KEYFLAG, ROLLNAME, DATATYPE, "
        f"LENG, DECIMALS, REFTABLE, REFFIELD FROM DD03L "
        f"WHERE TABNAME = '{safe}' AND AS4LOCAL = 'A'"
    )


def api_release_query(name: str, object_type: str = "DDLS") -> str:
    """Does an APIS transport object exist for this object?

    Appendix D.5: the release contract is the logical transport object
    ``R3TR APIS <name> <type>``. Its presence establishes that SAP has released
    the object; the C0…C3 level lives inside the object and is not reachable
    from here.

    ``OBJ_NAME`` is fixed-width — the object name padded to 40 characters and
    then the type — so this matches with LIKE rather than equality. An exact
    match on ``'I_CURRENCY DDLS'`` silently returns nothing, which would read
    as "not released" for every object in the system.
    """
    safe = _safe_name(name)
    safe_type = _safe_name(object_type)
    return (
        f"SELECT OBJ_NAME, DEVCLASS FROM TADIR WHERE OBJECT = 'APIS' "
        f"AND OBJ_NAME LIKE '{safe}%{safe_type}'"
    )


def extraction_release_states_query() -> str:
    """VIEWNAME → RELEASE_STATE, and → DDLNAME, for extraction-enabled views.

    One query covers thousands of objects, and it carries the actual contract
    level rather than mere existence. Preferred over the per-object APIS
    lookup wherever it has an answer.

    ``DDLNAME`` is here because a CDS view has **three** names and they need
    not agree::

        VIEWNAME      ZCDS_2RFLOWS   the view
        SQL_VIEWNAME  ZCDS2RFLWS     the database view
        DDLNAME       ZCDS_RFLOW1    the DDL source object

    ADT reads source by DDL source name. Asking it for ``ZCDS_2RFLOWS``
    answers 404, and a tool that takes that at face value reports a perfectly
    healthy C1-released view as deleted — which is exactly what this one did.
    """
    return (
        "SELECT VIEWNAME, RELEASE_STATE, DDLNAME, ISDELTASUPPORTED "
        "FROM DHCDCVCDSEXTRE"
    )


def object_directory_query(name: str, object_type: str = "DDLS") -> str:
    """TADIR — package and ownership."""
    safe = _safe_name(name)
    safe_type = _safe_name(object_type)
    return (
        f"SELECT PGMID, OBJECT, OBJ_NAME, DEVCLASS, AUTHOR FROM TADIR "
        f"WHERE OBJECT = '{safe_type}' AND OBJ_NAME = '{safe}'"
    )


def object_directory_bulk_query(names: list[str], object_type: str = "DDLS") -> str:
    """TADIR for many objects at once.

    The per-object form is the single most expensive thing this tool does at
    scale: screening T001's 248 readers issued 771 freestyle queries, and each
    one generates a program on the target system. That is not a throughput
    problem, it is how a development box's subpool directory fills up and its
    owner's Eclipse closes.

    Keep the chunks small. An IN list of 60 answered a bare HTTP 400 and the
    failure was silently swallowed, so the caller reported three readers where
    there were 287. Ten is known to work.
    """
    safe_type = _safe_name(object_type)
    values = ", ".join(f"'{_safe_name(n)}'" for n in names if n)
    return (
        f"SELECT PGMID, OBJECT, OBJ_NAME, DEVCLASS, AUTHOR FROM TADIR "
        f"WHERE OBJECT = '{safe_type}' AND OBJ_NAME IN ( {values} )"
    )


def views_reading_table_crossref_query(table: str) -> str:
    """CDSVIEWCROSSREF — CDS views that refer to this table.

    Not DDLDEPENDENCY, which was the obvious candidate and is wrong: on
    S/4HANA 2025 it maps a DDL source to the objects it *generates* (STOB and
    the generated SQL VIEW), never to the ones it uses. Querying it for a table
    returns nothing, which reads as "no view uses this table" — a confidently
    false answer.

    This table carries a SQLVIEWNAME column, so it indexes classic DDIC-based
    views; view entities generate no SQL view and may be under-represented.
    Hence the union with the RIS index below.
    """
    safe = _safe_name(table)
    return (
        f"SELECT OBJECTDDLSOURCENAME, REFERREDOBJECT, REFERREDOBJECTTYPE "
        f"FROM CDSVIEWCROSSREF WHERE REFERREDOBJECT = '{safe}'"
    )


def views_reading_table_ris_query(table: str) -> str:
    """DDLS_RIS_INDEX — the repository information system's used-artifact index.

    Artifacts are named ``\\TY:<NAME>``; a plain table name matches nothing.
    """
    safe = _safe_name(table)
    return (
        f"SELECT DDLSRC_NAME, USED_ARTIFACT_FULLNAME FROM DDLS_RIS_INDEX "
        f"WHERE USED_ARTIFACT_FULLNAME = '\\TY:{safe}'"
    )


def views_reading_views_query(names: list[str]) -> str:
    """CDSVIEWCROSSREF — which DDL sources refer to any of these objects.

    Used to look one level *above* a table's direct readers. The good
    consumption views usually live there: ``I_SalesDocument`` selects from
    ``I_SalesDocumentBasic``, which selects from VBAK, so a search that stops
    at direct readers of VBAK never sees it.

    Pass both the DDL name and the SQL view name of each object. Crossref
    records a classic view under its *SQL* view name — ``I_SalesDocument``
    refers to ``ISDSALESDOCBSC``, and looking up ``I_SALESDOCUMENTBASIC``
    returns nothing at all.
    """
    quoted = ", ".join(f"'{_safe_name(n)}'" for n in names if n)
    return (
        f"SELECT OBJECTDDLSOURCENAME, REFERREDOBJECT FROM CDSVIEWCROSSREF "
        f"WHERE REFERREDOBJECT IN ({quoted})"
    )


def released_ddls_query() -> str:
    """Every DDL source SAP has released, in one query.

    ``R3TR APIS <name> DDLS`` is the release contract (Appendix D.5), and
    ``OBJ_NAME`` is the name padded to 40 characters followed by the type — so
    this matches on the type suffix and strips the padding in the caller.

    A real signal, unlike a name shape: SAP releasing an object is a statement
    that it is meant to be consumed and will not change without notice.
    """
    return "SELECT OBJ_NAME FROM TADIR WHERE OBJECT = 'APIS' AND OBJ_NAME LIKE '%DDLS'"


def all_ddl_directory_query() -> str:
    """Package and author for every DDL source, in one query.

    The same shape as :func:`released_ddls_query`, and for the same reason.
    Screening one wide table asked TADIR 461 times — 417 of them one object at
    a time from the stack walk, which cannot know its object names in advance
    to batch them. Reading the whole directory once removes the question
    instead of answering it faster.

    Worth it because a freestyle query generates a program on the target
    system: this is 461 of them replaced by one, and it is 461 that filled a
    development box's subpool directory and closed its owner's Eclipse.
    """
    return "SELECT OBJ_NAME, DEVCLASS, AUTHOR FROM TADIR WHERE OBJECT = 'DDLS'"


def cds_inventory_query() -> str:
    """DDLDEPENDENCY — the dependency backbone (F-05)."""
    return (
        "SELECT DDLNAME, OBJECTNAME, OBJECTTYPE, STATE FROM DDLDEPENDENCY "
        "WHERE STATE = 'A'"
    )


def extraction_enabled_query(name_filter: str = "") -> str:
    """DHCDCVCDSEXTRE — the CDC/extraction-enabled view list.

    Appendix D warns that several of these are internal tables subject to change
    between NetWeaver versions, so a failure here is expected on some releases
    and must degrade rather than abort.

    ``name_filter`` restricts *server-side*, and that matters more than it
    sounds: this system has 7,079 rows, the caller pages at 5,000, and
    filtering the page afterwards reported "0 matching" for a view that was
    plainly there — it simply sorted past the cap. A false "not found" about an
    object the tool had just created.

    A ``%`` in the filter is honoured as a wildcard and the pattern is used as
    written; without one the filter is a substring search, which is what people
    expect from a search box. The first cut stripped ``%`` as an illegal DDIC
    character and always searched for a substring, so ``--filter Z%`` — asking
    for custom objects — returned ``C_CHEMICALGHSHAZARDCLASS``, because it
    contains a Z. An inventory of "what we have built" quietly seeded with
    SAP's own views is worse than no inventory.

    ``_`` is deliberately *not* treated as a wildcard even though SQL says it
    is one. It is a legal and very common DDIC name character, and everyone
    typing ``ZI_KNA1`` means the underscore literally.
    """
    if not name_filter:
        return "SELECT * FROM DHCDCVCDSEXTRE"
    safe = _safe_pattern(name_filter)
    pattern = safe if "%" in safe else f"%{safe}%"
    return (
        f"SELECT * FROM DHCDCVCDSEXTRE "
        f"WHERE VIEWNAME LIKE '{pattern}' ESCAPE '#'"
    )


def _safe_name(value: str) -> str:
    """Strip anything that is not a legal DDIC object character.

    Not defence against a hostile user — they already have the credentials —
    but against a stray quote turning a metadata read into a syntax error, or
    into a query nobody intended.
    """
    return "".join(ch for ch in value.upper() if ch.isalnum() or ch in "_/")


def _safe_pattern(value: str) -> str:
    """Like :func:`_safe_name`, but ``%`` survives as a wildcard.

    ``_`` is escaped rather than passed through: it is a SQL single-character
    wildcard and a legal DDIC character, and the second reading is the one
    every caller means.
    """
    kept = "".join(
        ch for ch in (value or "").upper() if ch.isalnum() or ch in "_/%"
    )
    return kept.replace("_", "#_")
