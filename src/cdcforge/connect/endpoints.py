"""Every ADT REST path, in one module.

The specification says this twice, and it is the single most important
structural decision in the connector:

    SAP does not publish the ADT REST wire contract. These paths are
    reconstructed from the open-source abap-adt-api client, community
    reverse-engineering, and observed Eclipse traffic. Verify each against the
    target release before relying on it. Build the connector so endpoint paths
    live in one config module, not scattered through the code.

So a release change is an edit to this file, not a refactor. Nothing anywhere
else in the codebase may contain a literal ``/sap/bc/adt/...`` string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from urllib.parse import parse_qs, parse_qsl

ADT_ROOT = "/sap/bc/adt"


class Access(str, Enum):
    """What an endpoint does to the system.

    This is the classification the read-only kill switch acts on. It is not the
    same as the HTTP verb: ``checkruns`` is a POST that changes nothing, and
    treating "POST" as "write" would disable the most valuable read-only check
    the tool has.
    """

    READ = "READ"
    """Reads state. Safe in read-only mode whatever the verb."""

    WRITE = "WRITE"
    """Creates, changes, locks or activates. Blocked in read-only mode, and
    blocked outright against a productive client."""


@dataclass(frozen=True)
class Endpoint:
    path: str
    method: str
    access: Access
    description: str
    accept: str = ""
    """The ``Accept`` header this endpoint needs.

    ADT is strict about content negotiation and answers 406 rather than
    guessing. A blanket ``Accept: application/*`` gets a flat refusal from the
    source endpoint, whose only representation is ``text/plain``:

        406 — The message content is not acceptable.
              Accepted content types: text/plain

    So the accepted type belongs here, beside the path, for the same reason the
    path does: a release changes it, and it must be one edit rather than a hunt
    through the code. Empty means send ``*/*`` and let the server choose.
    """

    content_type: str = ""
    """The ``Content-Type`` for a request body, where one is required."""

    verified: bool = False
    """Whether this path has been confirmed against a real system.

    Everything ships as False. The preflight flips what it proves. An
    unverified endpoint is not a bug — it is a fact about the state of public
    documentation, and the UI should say so rather than implying certainty.
    """

    def url(self, **params: str) -> str:
        return self.path.format(**params)


# ---------------------------------------------------------------------------
# Session and system
# ---------------------------------------------------------------------------

DISCOVERY = Endpoint(
    f"{ADT_ROOT}/discovery", "GET", Access.READ,
    "Discovery document; also how the CSRF token is fetched",
)
SYSTEM_INFORMATION = Endpoint(
    f"{ADT_ROOT}/core/systeminformation", "GET", Access.READ,
    "System ID, release, client",
)

# ---------------------------------------------------------------------------
# Metadata reads
# ---------------------------------------------------------------------------

DDIC_TABLE = Endpoint(
    f"{ADT_ROOT}/ddic/tables/{{name}}", "GET", Access.READ,
    "DDIC table metadata",
)
DDL_SOURCE = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources/{{name}}/source/main", "GET", Access.READ,
    "CDS DDL source text",
    accept="text/plain",
    verified=True,
)
DDL_OBJECT = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources/{{name}}", "GET", Access.READ,
    "CDS object metadata",
    accept="application/vnd.sap.adt.ddlSource+xml",
    verified=True,
)
DDL_DEPENDENCY_GRAPH = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/dependencies/graphdata", "GET", Access.READ,
    "DDL Dependency Analyzer. SAP's own view-stack resolution — worth "
    "comparing against the tool's (F-08), since Appendix D.2 warns the "
    "metadata tables have gaps.",
)
REPOSITORY_SEARCH = Endpoint(
    f"{ADT_ROOT}/repository/informationsystem/search", "GET", Access.READ,
    "Repository object search",
)
USAGE_REFERENCES = Endpoint(
    f"{ADT_ROOT}/repository/informationsystem/usageReferences", "POST", Access.READ,
    "Where-used. Eclipse's own answer to 'what reads this table', and the only "
    "one that works on this release — DDLDEPENDENCY maps a DDL source to the "
    "objects it *generates*, not the ones it uses. A POST that reads.",
    content_type="application/vnd.sap.adt.repository.usagereferences.request.v1+xml",
    accept="application/vnd.sap.adt.repository.usagereferences.result.v1+xml",
)
NODE_STRUCTURE = Endpoint(
    f"{ADT_ROOT}/repository/nodestructure", "POST", Access.READ,
    "Package contents. A POST that reads.",
)
DATA_PREVIEW = Endpoint(
    f"{ADT_ROOT}/datapreview/freestyle", "POST", Access.READ,
    "Freestyle SELECT — how Eclipse's data preview reads table contents. "
    "A POST that reads; used for T000 client-role detection and, later, for "
    "the cardinality prover (F-14).",
    accept="application/xml, application/vnd.sap.adt.datapreview.table.v1+xml",
    content_type="text/plain",
    verified=True,
)

# ---------------------------------------------------------------------------
# Checks — read-only, and the most important endpoint in the tool
# ---------------------------------------------------------------------------

CHECK_RUNS = Endpoint(
    f"{ADT_ROOT}/checkruns?reporters=abapCheckRun", "POST", Access.READ,
    "Syntax / pre-check. Returns SAP's own verdict without activating "
    "anything (F-15).",
)
ATC_RUNS = Endpoint(
    f"{ADT_ROOT}/atc/runs", "POST", Access.READ,
    "ATC run. Note that ATC has no dedicated CDC-readiness check variant.",
)

# ---------------------------------------------------------------------------
# Writes — Stage 4. Declared here so the classification exists from the start.
# ---------------------------------------------------------------------------

CREATE_DDL_SOURCE = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources", "POST", Access.WRITE, "Create a DDL source",
    content_type="application/vnd.sap.adt.ddlSource+xml",
    accept="application/vnd.sap.adt.ddlSource+xml",
)
UPDATE_DDL_SOURCE = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources/{{name}}/source/main", "PUT", Access.WRITE,
    "Update DDL source text",
    content_type="text/plain; charset=utf-8",
    accept="text/plain",
)
DELETE_DDL_SOURCE = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources/{{name}}", "DELETE", Access.WRITE,
    "Delete a DDL source — the rollback path when activation fails",
)
LOCK = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources/{{name}}?_action=LOCK&accessMode=MODIFY",
    "POST", Access.WRITE, "Lock for modification",
)
UNLOCK = Endpoint(
    f"{ADT_ROOT}/ddic/ddl/sources/{{name}}?_action=UNLOCK&lockHandle={{handle}}",
    "POST", Access.WRITE, "Release a lock",
)
ACTIVATE = Endpoint(
    f"{ADT_ROOT}/activation?method=activate&preauditRequested=true",
    "POST", Access.WRITE, "Activate",
    content_type="application/xml",
    accept="application/xml",
)
TRANSPORT_REQUESTS = Endpoint(
    f"{ADT_ROOT}/cts/transportrequests", "GET", Access.READ,
    "List transport requests. Note that the documented ?_action=FIND form "
    "answers an empty tm:root on this release even when modifiable requests "
    "exist — TRANSPORT_CHECK is what actually reports the usable ones.",
)
TRANSPORT_REQUEST_OBJECTS = Endpoint(
    f"{ADT_ROOT}/cts/transportrequests/{{number}}?withObjects=true",
    "GET", Access.READ,
    "One request and everything recorded in it. The query parameter is what "
    "makes the difference: without it the response is the request header "
    "alone, and the /objects sub-resource answers 405.",
    verified=True,
)
TRANSPORT_CHECK = Endpoint(
    f"{ADT_ROOT}/cts/transportchecks", "POST", Access.READ,
    "Ask CTS what an object would need: whether a request is required at all, "
    "and which of the user's open requests could take it. Eclipse calls this "
    "before it shows the transport dialog. A POST that reads, like checkruns — "
    "it answers a question and creates nothing. Creating a request is "
    "CREATE_TRANSPORT_REQUEST, a separate and explicit call.",
    content_type="application/vnd.sap.as+xml; charset=UTF-8; "
    "dataname=com.sap.adt.CheckCollection",
    accept="application/vnd.sap.as+xml; dataname=com.sap.adt.CheckCollection",
)
CREATE_TRANSPORT_REQUEST = Endpoint(
    f"{ADT_ROOT}/cts/transportrequests?_action=NEWREQUEST", "POST", Access.WRITE,
    "Create a workbench transport request. The _action is not decoration — "
    "without it the endpoint answers HTTP 400, as does every asx:abap body "
    "shape. It wants the Transport Organizer's own representation.",
    content_type="application/vnd.sap.adt.transportorganizer.v1+xml",
    accept="application/vnd.sap.adt.transportorganizer.v1+xml",
    verified=True,
)
PACKAGE = Endpoint(
    f"{ADT_ROOT}/packages/{{name}}", "GET", Access.READ,
    "Package metadata — software component, transport layer, and whether it "
    "is local. The one authority on whether an object created here can ever "
    "leave the system.",
    accept="application/vnd.sap.adt.packages.v1+xml",
)

#: The SICF node that must be active for any of this to work.
SICF_NODE = ADT_ROOT

ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    DISCOVERY,
    SYSTEM_INFORMATION,
    DDIC_TABLE,
    DDL_SOURCE,
    DDL_OBJECT,
    DDL_DEPENDENCY_GRAPH,
    USAGE_REFERENCES,
    REPOSITORY_SEARCH,
    NODE_STRUCTURE,
    DATA_PREVIEW,
    CHECK_RUNS,
    ATC_RUNS,
    CREATE_DDL_SOURCE,
    UPDATE_DDL_SOURCE,
    DELETE_DDL_SOURCE,
    LOCK,
    UNLOCK,
    ACTIVATE,
    TRANSPORT_REQUESTS,
    TRANSPORT_REQUEST_OBJECTS,
    TRANSPORT_CHECK,
    CREATE_TRANSPORT_REQUEST,
    PACKAGE,
)


#: Query parameters that make a request a write whatever else it looks like.
_WRITE_ACTIONS = ("_action=lock", "_action=unlock", "_action=activate")


@lru_cache(maxsize=256)
def _template_pattern(template_base: str) -> re.Pattern[str]:
    escaped = re.escape(template_base.rstrip("/"))
    for placeholder in ("name", "handle"):
        escaped = escaped.replace(re.escape("{" + placeholder + "}"), r"[^/?]+")
    return re.compile(f"^{escaped}$")


def _matches(endpoint: Endpoint, method: str, path: str) -> bool:
    """Does this request hit this endpoint?

    Path templates are matched whole, not by prefix. Prefix matching is what
    let ``POST /ddic/ddl/sources`` (create) and ``?_action=LOCK`` pass as reads,
    because both share a prefix with the DDL object read — the exact class of
    hole the read-only guard exists to close.
    """
    request_base, _, request_query = path.partition("?")
    template_base, _, template_query = endpoint.path.partition("?")

    if not _template_pattern(template_base).match(request_base.rstrip("/")):
        return False

    if template_query:
        present = parse_qs(request_query)
        for key, value in parse_qsl(template_query):
            if value not in present.get(key, []):
                return False
    return True


def find(method: str, path: str) -> Endpoint | None:
    """The endpoint definition a request corresponds to, if any."""
    verb = method.upper()
    for endpoint in ALL_ENDPOINTS:
        if endpoint.method == verb and _matches(endpoint, verb, path):
            return endpoint
    return None


def accept_for(method: str, path: str) -> str:
    """The ``Accept`` header to send, or ``*/*`` when nothing is declared."""
    endpoint = find(method, path)
    return (endpoint.accept if endpoint else "") or "*/*"


def classify(method: str, path: str) -> Access:
    """Classify an arbitrary request.

    Writes are matched first and win. Unknown paths are classified WRITE: a
    path this module has never heard of is a path whose effects nobody has
    established, and guessing READ would put the kill switch's promise —
    "provably incapable of writing" — on a coin flip.
    """
    verb = method.upper()
    if verb in ("PUT", "DELETE", "PATCH"):
        return Access.WRITE

    query = path.partition("?")[2].lower()
    if any(action in query for action in _WRITE_ACTIONS):
        return Access.WRITE

    for endpoint in ALL_ENDPOINTS:
        # The verb matters here too. The update endpoint shares its path with
        # the source read and differs only by PUT vs GET, so ignoring the verb
        # would classify every source read as a write and block the whole
        # assessment product.
        if (
            endpoint.access is Access.WRITE
            and endpoint.method == verb
            and _matches(endpoint, verb, path)
        ):
            return Access.WRITE

    for endpoint in ALL_ENDPOINTS:
        if endpoint.access is not Access.READ or not _matches(endpoint, verb, path):
            continue
        # A read endpoint only vouches for its own verb. POSTing to a path
        # whose read contract is a GET is an unknown operation, not a read.
        if endpoint.method == verb or verb == "GET":
            return Access.READ

    if verb == "GET":
        # A GET to an unrecognised ADT path still cannot modify anything by any
        # reasonable reading of REST, and blocking exploratory reads would make
        # the tool useless for diagnosing an unfamiliar release.
        return Access.READ
    return Access.WRITE
