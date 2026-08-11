"""F-15 — delegate to SAP's own check.

    Post candidate DDL to /sap/bc/adt/checkruns and surface SAP's own verdict
    alongside the tool's. Two columns in the UI: *my analysis* and *the
    system's answer*. Where they disagree, the system wins and the tool logs
    the divergence for rule tuning.

This is how the opaque "too complex for automatic CDC delta" rejection gets
handled — no static analysis can predict it, so the tool asks.

Scope note: this checks objects that already exist in the system. Checking
*unsaved* DDL requires the lock → PUT → check chain, which is Stage 4 and
which writes. Nothing here writes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum

from cdcforge.connect import endpoints as ep
from cdcforge.connect.session import AdtError, AdtSession
from cdcforge.model import Assessment, Verdict

_CHECK_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<chkrun:checkObjectList xmlns:chkrun="http://www.sap.com/adt/checkrun" '
    'xmlns:adtcore="http://www.sap.com/adt/core">'
    '<chkrun:checkObject adtcore:uri="{uri}" chkrun:version="{version}"/>'
    "</chkrun:checkObjectList>"
)


class Severity(str, Enum):
    ERROR = "E"
    WARNING = "W"
    INFO = "I"
    UNKNOWN = "?"


@dataclass
class CheckMessage:
    severity: Severity
    text: str
    line: int = 0
    uri: str = ""
    code: str = ""
    """The SAP message code, e.g. ``DDLS(349)``.

    Worth keeping: it is stable across languages and releases, so a divergence
    log keyed on the code stays useful when the short text is translated or
    reworded.
    """


@dataclass
class CheckRunResult:
    object_name: str
    messages: list[CheckMessage] = field(default_factory=list)
    raw: str = ""
    reachable: bool = True
    error: str = ""
    status: str = ""
    """``processed`` when the system actually ran the check."""

    status_text: str = ""
    reporter: str = ""

    @property
    def errors(self) -> list[CheckMessage]:
        return [m for m in self.messages if m.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[CheckMessage]:
        return [m for m in self.messages if m.severity is Severity.WARNING]

    @property
    def ran(self) -> bool:
        """Did the system actually check the object?

        Distinct from ``clean``. A check that never ran produces no messages,
        which looks exactly like a clean result — and treating "not checked" as
        "checked and fine" is the false PASS this tool exists to prevent,
        arriving from the delegation side.
        """
        return self.reachable and not self.error and self.status == "processed"

    @property
    def clean(self) -> bool:
        return self.ran and not self.errors

    @property
    def summary(self) -> str:
        if not self.reachable:
            return f"checkrun unavailable — {self.error}"
        if self.error:
            return f"checkrun failed — {self.error}"
        if not self.ran:
            return f"checkrun did not run (status {self.status or 'unknown'!r})"
        if self.errors:
            return f"{len(self.errors)} error(s): {self.errors[0].text}"
        if self.warnings:
            return f"clean, {len(self.warnings)} warning(s)"
        return "clean"


def object_uri(name: str) -> str:
    return ep.DDL_OBJECT.url(name=name.upper())


def run_checkrun(
    session: AdtSession, object_name: str, *, version: str = "active"
) -> CheckRunResult:
    """Ask the system to check a DDL source.

    ``version`` selects which copy to check, and it matters more than it looks.
    A source that has just been uploaded and never activated has **no active
    version**, so asking for ``active`` gives the system nothing to look at and
    the check quietly does not run — which reads exactly like a clean result if
    nobody is watching for it. Pass ``inactive`` to check what was just
    written.
    """
    result = CheckRunResult(object_name=object_name.upper())
    payload = _CHECK_PAYLOAD.format(uri=object_uri(object_name), version=version)

    try:
        response = session.post(
            ep.CHECK_RUNS.path,
            body=payload,
            headers={
                "Content-Type": "application/vnd.sap.adt.checkobjects+xml",
                "Accept": "application/vnd.sap.adt.checkmessages+xml",
            },
            action="checkrun",
            object_name=object_name,
        )
    except AdtError as exc:
        result.reachable = False
        result.error = exc.message
        return result

    result.raw = response.text
    result.messages = parse_check_messages(response.text)
    status, status_text, reporter = parse_report_status(response.text)
    result.status = status
    result.status_text = status_text
    result.reporter = reporter
    return result


def parse_check_messages(text: str) -> list[CheckMessage]:
    """Parse a checkrun response defensively.

    The schema is unpublished, so this looks for anything that resembles a
    message rather than walking a fixed path.
    """
    messages: list[CheckMessage] = []
    if not text.strip():
        return messages
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return messages

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if "message" not in tag:
            continue
        attrs = {k.rsplit("}", 1)[-1].lower(): v for k, v in element.attrib.items()}
        raw_type = (attrs.get("type") or attrs.get("severity") or "").upper()[:1]
        try:
            severity = Severity(raw_type)
        except ValueError:
            severity = Severity.UNKNOWN
        body = attrs.get("shorttext") or (element.text or "").strip()
        if not body:
            for child in element.iter():
                if child is element:
                    continue
                if child.text and child.text.strip():
                    body = child.text.strip()
                    break
        if not body:
            continue
        messages.append(
            CheckMessage(
                severity=severity,
                text=body,
                line=_line_from_uri(attrs.get("uri", "")),
                uri=attrs.get("uri", ""),
                code=attrs.get("code", ""),
            )
        )
    return messages


def parse_report_status(text: str) -> tuple[str, str, str]:
    """``(status, statusText, reporter)`` from the check report envelope.

    Read separately from the messages because their absence means opposite
    things: no messages is a clean result, while no *status* means the check
    never ran.
    """
    if not text.strip():
        return "", "", ""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return "", "", ""

    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() != "checkreport":
            continue
        attrs = {k.rsplit("}", 1)[-1].lower(): v for k, v in element.attrib.items()}
        return (
            attrs.get("status", ""),
            attrs.get("statustext", ""),
            attrs.get("reporter", ""),
        )
    return "", "", ""


def _line_from_uri(uri: str) -> int:
    if "start=" not in uri:
        return 0
    fragment = uri.split("start=", 1)[1].split(",")[0]
    try:
        return int(fragment)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Agreement — §10's benchmark
# ---------------------------------------------------------------------------


@dataclass
class Agreement:
    """The tool's verdict against the system's answer, side by side."""

    object_name: str
    my_verdict: Verdict
    sap_clean: bool | None
    """``None`` when the checkrun could not run."""

    sap_summary: str = ""

    @property
    def diverged(self) -> bool:
        """Do the two disagree in a way worth logging?

        The case that matters is a false PASS: the tool said fine, the system
        says no. The opposite direction — the tool flags something the system
        accepts — is not necessarily wrong, since checkruns does not evaluate
        the CDC constraint set at all. Both are recorded; only the first is
        alarming.
        """
        if self.sap_clean is None:
            return False
        return (self.my_verdict is Verdict.PASS) != self.sap_clean

    @property
    def false_pass(self) -> bool:
        return self.sap_clean is False and self.my_verdict is Verdict.PASS

    def render(self) -> str:
        system = (
            "unavailable" if self.sap_clean is None
            else ("clean" if self.sap_clean else "rejected")
        )
        flag = "  <-- DIVERGENCE" if self.diverged else ""
        return (
            f"{self.object_name:<32} mine={self.my_verdict.value:<14} "
            f"system={system:<12} {self.sap_summary}{flag}"
        )


def compare(assessment: Assessment, check: CheckRunResult) -> Agreement:
    return Agreement(
        object_name=assessment.object_name,
        my_verdict=assessment.verdict,
        sap_clean=None if not check.reachable else check.clean,
        sap_summary=check.summary,
    )
