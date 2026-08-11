"""F-02 — environment preflight.

    Fail loudly at startup with a checklist. Never discover a missing
    capability at write time.

Every check reports what it found and, when it fails, what to do about it. A
check that could not run is CHECK_FAILED, not a pass — the same principle as the
rule engine's INCONCLUSIVE.

Because the ADT wire contract is unpublished, several checks are also *probes*:
they establish whether an endpoint behaves as the reconstructed map says it
does on this particular release. That result is worth as much as the capability
answer itself.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum

from cdcforge.connect import endpoints as ep
from cdcforge.connect import sql
from cdcforge.connect.profile import SystemRole
from cdcforge.connect.session import AdtError, AdtSession


class Status(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    WARNING = "WARNING"
    UNKNOWN = "UNKNOWN"
    """The check could not be completed. Never treated as a pass."""


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    remedy: str = ""

    @property
    def marker(self) -> str:
        return {
            Status.OK: "[ ok ]",
            Status.FAILED: "[FAIL]",
            Status.WARNING: "[warn]",
            Status.UNKNOWN: "[ ?? ]",
        }[self.status]

    def render(self) -> str:
        line = f"{self.marker}  {self.name}"
        if self.detail:
            line += f"\n         {self.detail}"
        if self.remedy and self.status is not Status.OK:
            line += f"\n         → {self.remedy}"
        return line


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)
    system_id: str = ""
    release: str = ""
    service_pack: str = ""
    client_role: SystemRole = SystemRole.UNKNOWN
    can_read_sources: bool = False
    can_run_checkruns: bool = False
    can_query_metadata: bool = False

    capabilities: dict[str, bool] = field(default_factory=dict)
    """Which metadata tables answered, by name.

    Kept as data rather than a set of booleans so a caller can ask about the
    one it needs. A missing key means "not probed", which is not the same as
    "unavailable".
    """

    @property
    def can_find_candidates(self) -> bool:
        """F-09 needs at least one where-used source to find anything."""
        return self.can_query_metadata and (
            self.capabilities.get("CDSVIEWCROSSREF", True)
            or self.capabilities.get("DDLS_RIS_INDEX", True)
        )

    @property
    def can_confirm_delta(self) -> bool:
        """`estate`, `verify` and the USE check all rest on this one table."""
        return self.can_query_metadata and self.capabilities.get(
            "DHCDCVCDSEXTRE", True
        )

    @property
    def degraded(self) -> list[str]:
        """Features that will return less on this system, in plain words."""
        lost = []
        if not self.can_find_candidates:
            lost.append(
                "candidate search — no existing view can be found for a table"
            )
        if not self.can_confirm_delta:
            lost.append(
                "delta confirmation — 'estate' and 'verify' cannot say what "
                "the system reports as delta-supported"
            )
        return lost

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return all(c.status is not Status.FAILED for c in self.checks)

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAILED]

    def render(self) -> str:
        header = [
            f"System      {self.system_id or '(unknown)'}",
            f"Release     {self.release or '(unknown)'}"
            + (f" SP{self.service_pack}" if self.service_pack else ""),
            f"Client role {self.client_role.label}",
        ]
        body = [c.render() for c in self.checks]
        lines = [*header, "", *body]
        if self.degraded:
            lines.append("")
            lines.append("Reduced on this system:")
            lines += [f"  - {item}" for item in self.degraded]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Release helpers
# ---------------------------------------------------------------------------

#: Trigger-based CDC delta for CDS views arrived with S/4HANA 1909 FPS01
#: on-premise — SAP NetWeaver AS ABAP 7.54 (Appendix A.1).
MIN_CDC_ABAP_RELEASE = 754

#: View entities need ABAP 7.55 / S/4HANA 2020 (Appendix A.8).
MIN_VIEW_ENTITY_RELEASE = 755


def normalise_release(value: str) -> int | None:
    """'7.54' / '754' / 'SAP_BASIS 754' → 754."""
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) >= 3:
        return int(digits[:3])
    return None


def _flatten(text: str) -> dict[str, str]:
    """Collect every attribute and element text in an ADT XML document.

    The schemas are unpublished and differ between releases, so this reads
    everything and lets the caller look for what it needs by name rather than
    depending on an element path that may not exist here.
    """
    found: dict[str, str] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return found
    for element in root.iter():
        for key, value in element.attrib.items():
            found[key.rsplit("}", 1)[-1].upper()] = value
        tag = element.tag.rsplit("}", 1)[-1].upper()
        if element.text and element.text.strip():
            found.setdefault(tag, element.text.strip())
    return found


def _pick(values: dict[str, str], *needles: str) -> str:
    for needle in needles:
        for key, value in values.items():
            if needle.upper() in key:
                return value
    return ""


# ---------------------------------------------------------------------------
# The preflight itself
# ---------------------------------------------------------------------------


def run_preflight(session: AdtSession, *, probe_object: str = "") -> PreflightReport:
    """Run the full checklist against a connected session."""
    report = PreflightReport()

    _check_adt_reachable(session, report)
    if report.blocking_failures:
        # Nothing below can mean anything if the node is not answering.
        return report

    _check_system_information(session, report)
    _check_client_role(session, report)
    _check_metadata_queries(session, report)

    # The same object serves both checks: no point asking the system to check
    # something we could not even read.
    candidate = probe_object or "I_CURRENCY"
    _check_source_read(session, report, candidate)
    if report.can_read_sources:
        _check_checkruns(session, report, candidate)
    else:
        report.add(
            Check(
                "Pre-check endpoint (checkruns, F-15)",
                Status.UNKNOWN,
                f"skipped — {candidate} could not be read, so a check against it "
                f"would prove nothing",
                "Re-run with --probe <VIEWNAME> naming a view you can read.",
            )
        )

    _check_metadata_tables(session, report)
    _check_release_capabilities(report)
    _check_transport_layer(session, report)
    _check_ssl(session, report)

    return report


def _check_adt_reachable(session: AdtSession, report: PreflightReport) -> None:
    try:
        response = session.connect()
    except AdtError as exc:
        report.add(
            Check(
                f"ADT reachable ({ep.SICF_NODE})",
                Status.FAILED,
                exc.message,
                exc.remedy,
            )
        )
        return

    report.add(
        Check(
            f"ADT reachable ({ep.SICF_NODE})",
            Status.OK,
            f"HTTP {response.status} in {response.elapsed_ms} ms",
        )
    )
    report.add(
        Check(
            "CSRF token obtained",
            Status.OK if session.csrf_token else Status.WARNING,
            "token captured" if session.csrf_token else "no token in the response",
            "" if session.csrf_token else
            "Reads will still work. Writes need a token, so Stage 4 would fail here.",
        )
    )


def _check_system_information(session: AdtSession, report: PreflightReport) -> None:
    """Establish system ID, release and SP.

    Three sources, in order of preference. The ADT endpoint is tried first but
    does not exist on every release — S/4HANA 2025 answers 404 — so the release
    falls back to CVERS, which is stable and documented. The system ID is
    simpler still: the ICM stamps it on every response.
    """
    # 1. The SID is free — every ADT response carries it.
    report.system_id = session.system_id or ""

    # 2. The documented-but-not-universal ADT endpoint.
    try:
        response = session.get(ep.SYSTEM_INFORMATION.path, action="system-information")
        values = _flatten(response.text)
        report.system_id = report.system_id or _pick(values, "SYSTEMID", "SYSID", "SID", "NAME")
        report.release = _pick(values, "RELEASE", "VERSION")
        report.service_pack = _pick(values, "SUPPORTPACKAGE", "PATCHLEVEL", "SP")
    except AdtError:
        pass

    # 3. CVERS, via the metadata read path.
    components: dict[str, tuple[str, str]] = {}
    if not report.release:
        try:
            result = sql.run_query(session, sql.release_query(), max_rows=200)
            for row in result.rows:
                component = (row.get("COMPONENT") or "").strip().upper()
                if component:
                    components[component] = (
                        (row.get("RELEASE") or "").strip(),
                        (row.get("EXTRELEASE") or "").strip(),
                    )
        except (AdtError, sql.QueryNotPermitted):
            components = {}

        for component in ("SAP_BASIS", "SAP_ABA"):
            if component in components:
                report.release, report.service_pack = components[component]
                break

    if report.system_id:
        session.system_id = report.system_id

    detail = f"SID={report.system_id or '?'} release={report.release or '?'}"
    if "S4CORE" in components:
        detail += f" S4CORE={components['S4CORE'][0]}"

    report.add(
        Check(
            "System information",
            Status.OK if report.release else Status.UNKNOWN,
            detail,
            "" if report.release
            else "Neither the ADT endpoint nor CVERS yielded a release. The "
                 "release-dependent checks below will stay inconclusive.",
        )
    )


def _check_client_role(session: AdtSession, report: PreflightReport) -> None:
    """F-03 — production detection. The most consequential check here."""
    try:
        result = sql.run_query(
            session, sql.client_role_query(session.profile.client), max_rows=1
        )
    except (AdtError, sql.QueryNotPermitted) as exc:
        message = getattr(exc, "message", str(exc))
        report.client_role = SystemRole.UNKNOWN
        session.system_role = SystemRole.UNKNOWN
        report.add(
            Check(
                "Client role (T000)",
                Status.WARNING,
                f"could not be determined — {message}",
                "Unknown role is treated as PRODUCTION and all writes stay "
                "blocked. Set system_role in the profile once you have "
                "confirmed it manually (SE16 → T000 → CCCATEGORY).",
            )
        )
        return

    category = (result.first().get("CCCATEGORY") or "").strip()
    role = SystemRole.parse(category)
    report.client_role = role
    session.system_role = role

    status = Status.WARNING if role.is_productive else Status.OK
    report.add(
        Check(
            "Client role (T000)",
            status,
            f"client {session.profile.client} is {role.label}"
            + (f" — {result.first().get('MTEXT', '')}" if result.first().get("MTEXT") else ""),
            "Writes are blocked against this client. Overriding requires typing "
            "the system ID and client exactly."
            if role.is_productive else "",
        )
    )


def _check_metadata_queries(session: AdtSession, report: PreflightReport) -> None:
    """Probe the set-based read path the inventory sweep depends on."""
    try:
        result = sql.run_query(session, sql.table_header_query("T000"), max_rows=1)
    except AdtError as exc:
        report.can_query_metadata = False
        report.add(
            Check(
                "Set-based metadata read (data preview)",
                Status.WARNING,
                exc.message,
                "Without it the inventory falls back to object-by-object ADT "
                "reads, which work but are far slower on a large system.",
            )
        )
        return

    report.can_query_metadata = result.parsed
    report.add(
        Check(
            "Set-based metadata read (data preview)",
            Status.OK if result.parsed else Status.WARNING,
            f"DD02L returned {len(result.rows)} row(s), columns={result.columns}"
            if result.parsed
            else "the endpoint answered but the response could not be parsed",
            "" if result.parsed
            else "The data-preview wire format is unpublished and differs "
                 "between releases. Run 'cdc-forge raw' to capture the payload.",
        )
    )


#: The metadata tables the tool leans on, and the feature that stops working
#: without each. Every endpoint here is verified against one release only, and
#: these tables are the parts most likely to differ on another: DHCDCVCDSEXTRE
#: arrived with the CDC framework, and the where-used indexes are internal and
#: documented nowhere.
#:
#: Naming the *consequence* rather than the table is the point. "CDSVIEWCROSSREF
#: unavailable" means nothing to the person running this; "no existing view can
#: be found for a table" means everything.
_CAPABILITY_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "DHCDCVCDSEXTRE",
        "Delta registry",
        "Without it the tool cannot say which views the system already reports "
        "as delta-supported: 'estate', 'verify' and the USE check all go "
        "blind. Assessment and generation still work.",
    ),
    (
        "CDSVIEWCROSSREF",
        "Where-used (classic views)",
        "One of the two where-used sources. Losing both means no existing view "
        "can be found for a table, which is most of what 'plan' does.",
    ),
    (
        "DDLS_RIS_INDEX",
        "Where-used (view entities)",
        "The other where-used source. View entities generate no SQL view, so "
        "without this they are invisible to the candidate search.",
    ),
    (
        "TADIR",
        "Object directory",
        "Without it no object's package or release contract can be read, so "
        "every candidate's API state is UNKNOWN — which the rules treat as "
        "unreleased, so nothing is wrongly trusted, only less is known.",
    ),
)


def _check_metadata_tables(session: AdtSession, report: PreflightReport) -> None:
    """Probe each metadata table the tool depends on, and say what it costs.

    Runs one cheap query per table. An unavailable table is a *warning*, never
    a failure: the tool degrades to a smaller answer rather than a wrong one,
    and it should say which answer got smaller instead of quietly returning
    less. That is the difference between a tool that does not work here and a
    tool that lies here.
    """
    if not report.can_query_metadata:
        report.add(
            Check(
                "Metadata tables",
                Status.WARNING,
                "not probed — set-based reads are unavailable on this system",
                "Candidate search, the estate survey and delta confirmation "
                "all need them. Assessment of a named view still works.",
            )
        )
        return

    for table, feature, consequence in _CAPABILITY_PROBES:
        try:
            result = sql.run_query(
                session, f"SELECT * FROM {table}", max_rows=1
            )
            available = result.parsed or not result.raw.strip()
        except (AdtError, sql.QueryNotPermitted) as exc:
            report.capabilities[table] = False
            report.add(
                Check(
                    f"{feature} ({table})",
                    Status.WARNING,
                    f"unavailable — {getattr(exc, 'message', str(exc))}",
                    consequence,
                )
            )
            continue

        report.capabilities[table] = available
        report.add(
            Check(
                f"{feature} ({table})",
                Status.OK if available else Status.WARNING,
                "available" if available
                else "answered, but the response could not be read",
                "" if available else consequence,
            )
        )


def _check_source_read(
    session: AdtSession, report: PreflightReport, candidate: str
) -> None:
    """S_DEVELOP / DDLS activity 03 — can we read a CDS source at all?"""
    try:
        response = session.get(
            ep.DDL_SOURCE.url(name=candidate.upper()),
            action="read-source",
            object_name=candidate,
        )
    except AdtError as exc:
        status = Status.WARNING if exc.status == 404 else Status.FAILED
        report.add(
            Check(
                "Read a CDS DDL source (S_DEVELOP, DDLS, activity 03)",
                status,
                f"{candidate}: {exc.message}",
                exc.remedy
                or "Try again against a view you know exists: "
                   "cdc-forge preflight --probe <VIEWNAME>",
            )
        )
        return

    report.can_read_sources = True
    report.add(
        Check(
            "Read a CDS DDL source (S_DEVELOP, DDLS, activity 03)",
            Status.OK,
            f"{candidate}: {len(response.text)} characters",
        )
    )


def _check_checkruns(
    session: AdtSession, report: PreflightReport, probe_object: str
) -> None:
    """F-15 — SAP's own verdict without activating anything.

    Runs a *real* check against a real object. An earlier version posted an
    empty payload and reported the resulting 400 as a warning, which read as
    "checkruns is broken" when the endpoint was working perfectly — a probe
    that cannot succeed tells you nothing about whether the feature works, and
    worse, it tells you something false.
    """
    from cdcforge.connect.checkrun import run_checkrun

    result = run_checkrun(session, probe_object)

    if not result.reachable:
        report.can_run_checkruns = False
        report.add(
            Check(
                "Pre-check endpoint (checkruns, F-15)",
                Status.FAILED,
                f"{probe_object}: {result.error}",
                "Without checkruns the tool cannot corroborate its own verdicts "
                "against SAP's. That is the wedge feature.",
            )
        )
        return

    report.can_run_checkruns = result.ran
    report.add(
        Check(
            "Pre-check endpoint (checkruns, F-15)",
            Status.OK if result.ran else Status.WARNING,
            f"{probe_object}: {result.summary}"
            + (f" [{result.reporter}]" if result.reporter else ""),
            "" if result.ran
            else "The endpoint answered but did not report a processed check. "
                 "SAP's verdict cannot be trusted as corroboration until it does.",
        )
    )


def _check_release_capabilities(report: PreflightReport) -> None:
    release = normalise_release(report.release)
    if release is None:
        report.add(
            Check(
                "CDC framework available (ABAP 7.54+ / S/4HANA 1909 FPS01)",
                Status.UNKNOWN,
                "the release could not be read, so this cannot be established",
                "Confirm manually before relying on any CDC verdict.",
            )
        )
        report.add(
            Check(
                "View entity support (ABAP 7.55+ / S/4HANA 2020)",
                Status.UNKNOWN,
                "the release could not be read",
            )
        )
        return

    report.add(
        Check(
            "CDC framework available (ABAP 7.54+ / S/4HANA 1909 FPS01)",
            Status.OK if release >= MIN_CDC_ABAP_RELEASE else Status.FAILED,
            f"detected {release}",
            "" if release >= MIN_CDC_ABAP_RELEASE
            else "Trigger-based CDC delta for CDS views does not exist on this "
                 "release. Only generic timestamp-based delta is available, and "
                 "Replication Flows do not support it (KBA 3514600).",
        )
    )
    report.add(
        Check(
            "View entity support (ABAP 7.55+ / S/4HANA 2020)",
            Status.OK if release >= MIN_VIEW_ENTITY_RELEASE else Status.WARNING,
            f"detected {release}",
            "" if release >= MIN_VIEW_ENTITY_RELEASE
            else "Generated objects will use classic DEFINE VIEW with an "
                 "@AbapCatalog.sqlViewName.",
        )
    )


def _check_transport_layer(session: AdtSession, report: PreflightReport) -> None:
    try:
        session.get(ep.TRANSPORT_REQUESTS.path, action="transport-check")
    except AdtError as exc:
        report.add(
            Check(
                "Transport requests reachable (S_TRANSPRT)",
                Status.WARNING,
                exc.message,
                "Only needed for the write pipeline (Stage 4). Assessment is "
                "unaffected.",
            )
        )
        return
    report.add(Check("Transport requests reachable (S_TRANSPRT)", Status.OK))


def _check_ssl(session: AdtSession, report: PreflightReport) -> None:
    if session.profile.verify_ssl:
        report.add(
            Check(
                "TLS certificate verification",
                Status.OK,
                f"verified{' against ' + session.profile.ca_bundle_path if session.profile.ca_bundle_path else ''}",
            )
        )
    else:
        profile = session.profile
        reason = profile.tls_override_reason.strip() or "(none given)"
        # Named for what it costs, not for what is switched off. "Verification
        # disabled" reads like a setting; "the password is readable on the
        # path" reads like the fact it is.
        report.add(
            Check(
                "TLS certificate verification",
                Status.WARNING,
                f"DISABLED — Basic auth sends {profile.username}'s password to "
                f"{profile.host} on every request over a connection nobody has "
                f"authenticated. Anyone on the path can read it. Reason on "
                f"file: {reason}",
                "Point ca_bundle_path at the CA that issued this host's "
                "certificate. Every audit record from this session is stamped "
                "ssl_verified=false meanwhile.",
            )
        )
