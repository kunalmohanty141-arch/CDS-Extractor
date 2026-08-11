"""Change recording — asking CTS what a write would need, and recording it.

`$TMP` needs none of this, which is why it stayed the default for so long: an
object there belongs to the user who made it, transports nowhere, and can be
deleted without trace. That is also its limitation. Nothing built in `$TMP` can
ever reach QA or production, so a tool that only writes there produces work that
cannot leave the machine it was made on.

Everything else needs a transport request, and the question of *whether* it does
is not ours to answer. Guessing from the package name — ``$`` means local, ``Z``
means transportable — is the kind of rule that holds until it doesn't: a
namespace can be configured either way, and a package can forbid new requests
entirely. So this module asks the system, every time, through the same check
Eclipse runs before it shows its transport dialog.

Measured against S/4HANA 2022, the answer comes back as a flat ``DATA`` envelope
and three fields decide everything:

    ``$TMP``     KORRFLAG=      DLVUNIT=LOCAL  RECORDING=
    ``ZDSP_…``   KORRFLAG=X     DLVUNIT=HOME   RECORDING=X

``RECORDING=X`` means a request is required. ``REQUESTS`` lists the ones this
user could put the object in — empty means there are none open and one has to be
created. ``EXISTING_REQ_ONLY=X`` means creating one is not allowed either, which
is a refusal rather than a problem to solve.

The wire format is unpublished like the rest of ADT, so every field name here is
reconstructed and every one is a candidate for a release-specific surprise. What
protects against that is not confidence in the shape but :data:`TransportNeed.ok`
— a check that did not clearly succeed is never read as "no request needed".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from cdcforge.connect import endpoints as ep
from cdcforge.connect.session import AdtError, AdtSession

_CHECK_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">'
    "<asx:values><DATA>"
    "<PGMID>R3TR</PGMID>"
    "<OBJECT>{object_type}</OBJECT>"
    "<OBJECTNAME>{name}</OBJECTNAME>"
    "<DEVCLASS>{package}</DEVCLASS>"
    "<SUPER_PACKAGE/>"
    "<OPERATION>{operation}</OPERATION>"
    "<URI>{uri}</URI>"
    "</DATA></asx:values></asx:abap>"
)

#: Creating a request does not use the ``DATA`` envelope the *check* does.
#:
#: Three ``asx:abap`` shapes were tried against S/4HANA 2022 — with and without
#: an object ``REF``, as ``text/plain`` and as a typed ``as+xml`` — and all
#: three answered a bare HTTP 400. The Transport Organizer's own representation
#: is what the endpoint accepts, and it needs ``?_action=NEWREQUEST`` on the
#: path as well as the matching content type.
#:
#: Note what is *not* here: the package. A request is created free-standing and
#: bound to nothing; ``corrNr`` on the object write is what puts an object in
#: it.
_CREATE_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<tm:root xmlns:tm="http://www.sap.com/cts/adt/tm" '
    'tm:useraction="newrequest">'
    '<tm:request tm:desc="{description}" tm:type="K" tm:target="" '
    'tm:cts_project=""/>'
    "</tm:root>"
)

#: A workbench request number: system ID, K, six digits — ``DEVK900123``.
_REQUEST_NUMBER = re.compile(r"\b([A-Z][A-Z0-9]{2}K[0-9]{6})\b")


@dataclass(frozen=True)
class TransportRequest:
    """One request the object could be recorded in."""

    number: str
    description: str = ""
    owner: str = ""

    def render(self) -> str:
        text = f"{self.number}"
        if self.description:
            text += f"  {self.description}"
        if self.owner:
            text += f"  ({self.owner})"
        return text


@dataclass
class TransportNeed:
    """What CTS says about writing one object into one package."""

    package: str
    package_text: str = ""
    ok: bool = False
    """The check itself succeeded (``RESULT=S``).

    Separate from every other field on purpose. A check that failed, or came
    back in a shape this module does not recognise, must never be read as "no
    request needed" — that reading turns an unparsed response into a silent
    write with no change record.
    """

    local: bool = False
    """The package is local and non-transportable — ``$TMP`` and its kind.

    Objects here are usable on this system and nowhere else.
    """

    required: bool = False
    """A transport request must be supplied (``RECORDING=X``)."""

    existing_only: bool = False
    """New requests are not allowed here; one of :attr:`requests` must be used."""

    locked_in: str = ""
    """The request that already holds this object name, if any.

    An object that has ever been recorded in a request stays locked to it —
    including after it is deleted, because the deletion is itself an entry in
    that request. CTS then answers ``RECORDING`` *empty*, which reads as "no
    request needed" and is not what it means: it means "already accounted
    for", and the write still has to name that request or ADT refuses it with
    ``Parameter corrNr could not be found``.

    Measured the hard way. ZCDCF_TRTEST was created in DEVK900851 and deleted
    again; re-creating the same name then failed, with the check cheerfully
    reporting that no recording was required.
    """

    locked_task: str = ""
    """The task under :attr:`locked_in` that actually holds the object."""

    requests: list[TransportRequest] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def unreadable(self) -> bool:
        return not self.ok

    def render(self) -> str:
        if self.unreadable:
            detail = f" — {self.messages[0]}" if self.messages else ""
            return f"{self.package}: CTS check did not succeed{detail}"
        label = f"{self.package}"
        if self.package_text:
            label += f" ({self.package_text})"
        if self.local:
            return f"{label}: local, non-transportable — no request needed"
        if self.locked_in:
            return (
                f"{label}: this object is already held by {self.locked_in}"
                + (f" (task {self.locked_task})" if self.locked_task else "")
                + " — the write must name that request"
            )
        if not self.required:
            return f"{label}: no change recording required"
        if not self.requests:
            note = "and none are open" if not self.existing_only else (
                "and this package forbids creating one"
            )
            return f"{label}: a transport request is required, {note}"
        lines = [f"{label}: a transport request is required. Available:"]
        lines += [f"    {r.render()}" for r in self.requests]
        return "\n".join(lines)


def check_transport(
    session: AdtSession,
    name: str,
    package: str,
    *,
    object_type: str = "DDLS",
    uri: str = "",
    operation: str = "I",
) -> TransportNeed:
    """Ask CTS what creating ``name`` in ``package`` would require.

    Reads. Creates nothing, including transport requests — that is
    :func:`create_request`, and keeping the two apart is what lets this run in
    a read-only session.
    """
    upper = (name or "").upper()
    package = (package or "").upper()
    uri = uri or ep.DDL_OBJECT.url(name=upper.lower())

    payload = _CHECK_PAYLOAD.format(
        object_type=object_type,
        name=_xml_text(upper),
        package=_xml_text(package),
        operation=operation,
        uri=_xml_text(uri),
    )
    try:
        response = session.post(
            ep.TRANSPORT_CHECK.path,
            body=payload,
            action="transport-check",
            object_name=upper,
            headers={
                "Content-Type": ep.TRANSPORT_CHECK.content_type,
                "Accept": ep.TRANSPORT_CHECK.accept,
            },
        )
    except AdtError as exc:
        return TransportNeed(package=package, messages=[exc.message])

    return parse_check(response.text, package)


def parse_check(text: str, package: str) -> TransportNeed:
    """Read the ``DATA`` envelope CTS answers with.

    Split out from the request so the shape can be tested without a system —
    which matters more here than usual, because this is the parser that decides
    whether a write gets recorded.
    """
    need = TransportNeed(package=package, raw=text or "")
    data = _data_element(text)
    if data is None:
        need.messages.append("the CTS response could not be parsed")
        return need

    def value(tag: str) -> str:
        found = data.find(tag)
        return (found.text or "").strip() if found is not None else ""

    need.ok = value("RESULT").upper() == "S"
    need.package_text = value("CTEXT")
    need.required = value("RECORDING").upper() == "X"
    need.existing_only = value("EXISTING_REQ_ONLY").upper() == "X"
    # Two independent signals for the same fact, and they are read together
    # because either alone has a hole: DLVUNIT is a delivery unit rather than a
    # transport property, and KORRFLAG is empty both for a local package and
    # for a field this release does not send.
    need.local = value("DLVUNIT").upper() == "LOCAL" and not value("KORRFLAG")

    locks = data.find("LOCKS")
    if locks is not None:
        holder = next(
            (e for e in locks.iter() if e.tag.rpartition("}")[2] == "LOCK_HOLDER"),
            None,
        )
        if holder is not None:
            header = next(
                (e for e in holder if e.tag.rpartition("}")[2] == "REQ_HEADER"),
                None,
            )
            if header is not None:
                number = header.find("TRKORR")
                need.locked_in = (number.text or "").strip() if number is not None else ""
            task = next(
                (
                    e
                    for e in holder.iter()
                    if e.tag.rpartition("}")[2] == "CTS_TASK_HEADER"
                ),
                None,
            )
            if task is not None:
                number = task.find("TRKORR")
                need.locked_task = (
                    (number.text or "").strip() if number is not None else ""
                )

    requests = data.find("REQUESTS")
    if requests is not None:
        for element in requests.iter():
            number = ""
            description = ""
            owner = ""
            for child in element:
                tag = child.tag.rpartition("}")[2].upper()
                content = (child.text or "").strip()
                if tag in ("TRKORR", "NUMBER"):
                    number = content
                elif tag in ("AS4TEXT", "DESCRIPTION", "TEXT"):
                    description = content
                elif tag in ("AS4USER", "OWNER", "USER"):
                    owner = content
            if number:
                need.requests.append(
                    TransportRequest(number, description=description, owner=owner)
                )

    for element in data.iter():
        if element.tag.rpartition("}")[2].upper() in ("MESSAGE", "MSG"):
            for child in element.iter():
                if child.tag.rpartition("}")[2].upper() in (
                    "TEXT", "MESSAGE_TEXT", "SHORTTEXT"
                ) and (child.text or "").strip():
                    need.messages.append(child.text.strip())
    return need


def create_request(
    session: AdtSession, package: str, description: str
) -> tuple[str, str]:
    """Create a workbench request. Returns ``(number, error)``.

    Writes. The one call in this module that does, which is why it is not
    folded into :func:`check_transport` however convenient that would be — a
    read-only run must be able to report what a write *would* need without the
    asking itself leaving a request behind.
    """
    package = (package or "").upper()
    text = (description or "CDC Forge generated objects").strip()[:60]
    payload = _CREATE_PAYLOAD.format(description=_xml_attr(text))
    try:
        response = session.post(
            ep.CREATE_TRANSPORT_REQUEST.url(),
            body=payload,
            action="create-transport-request",
            object_name=package,
            headers={
                "Content-Type": ep.CREATE_TRANSPORT_REQUEST.content_type,
                "Accept": ep.CREATE_TRANSPORT_REQUEST.accept,
            },
        )
    except AdtError as exc:
        return "", exc.message

    number = request_number_from(response.text)
    if not number:
        return "", (
            "the request was created but its number could not be read from the "
            "response — check SE09 before running again, or a second run will "
            "create another one"
        )
    return number, ""


@dataclass
class RequestStatus:
    """One transport request, looked up by number.

    For the case where someone types a number rather than picking one. CTS's
    own list only offers requests it considers usable *by this user for this
    object*, which is not the same as the set of requests that would work — a
    colleague's request the user has been told to put their objects in is a
    normal thing to be handed, and it will not be in that list.
    """

    number: str
    found: bool = False
    modifiable: bool = False
    owner: str = ""
    description: str = ""
    status_text: str = ""

    @property
    def usable(self) -> bool:
        return self.found and self.modifiable

    def refusal(self) -> str:
        """Why this request cannot take an object, or empty if it can."""
        if not self.number:
            return "No request number was given."
        if not self.found:
            return (
                f"{self.number} was not found. Check the number — a transport "
                f"request looks like DEVK900123."
            )
        if not self.modifiable:
            return (
                f"{self.number} is {self.status_text or 'not modifiable'}, so "
                f"nothing more can be added to it. Use a modifiable request, "
                f"or create one."
            )
        return ""


def request_status(session: AdtSession, number: str) -> RequestStatus:
    """Look one request up. Reads."""
    number = (number or "").upper().strip()
    status = RequestStatus(number=number)
    if not number:
        return status
    try:
        response = session.get(
            ep.TRANSPORT_REQUEST_OBJECTS.url(number=number),
            action="read-transport-request",
            object_name=number,
        )
    except AdtError:
        return status

    try:
        root = ET.fromstring(response.text or "")
    except ET.ParseError:
        return status

    for element in root.iter():
        if element.tag.rpartition("}")[2] != "request":
            continue
        attrs = {k.rpartition("}")[2]: v for k, v in element.attrib.items()}
        if attrs.get("number", "").upper() != number:
            continue
        status.found = True
        status.owner = attrs.get("owner", "")
        status.description = attrs.get("desc", "")
        status.status_text = attrs.get("status_text", "")
        # 'D' is the system's code for modifiable; released requests are 'R'
        # or 'O' and will not take another object.
        status.modifiable = attrs.get("status", "").upper() == "D"
        return status
    return status


def contains_object(
    session: AdtSession, request: str, name: str, object_type: str = "DDLS"
) -> bool | None:
    """Is ``name`` actually listed in ``request``? ``None`` if unreadable.

    Sending ``corrNr`` and having the create succeed is not proof that the
    object was recorded — an ignored query parameter looks exactly like an
    honoured one, and the pipeline would go on to report a change record that
    does not exist. That failure has a precedent in this codebase: a checkrun
    asking for the ``active`` version of a never-activated object silently did
    not run, and read as clean.

    So the claim is checked. One GET, and the answer is the request's own
    object list::

        <tm:all_objects>
          <tm:abap_object tm:pgmid="R3TR" tm:type="DDLS" tm:name="ZCDCF_TRTEST"/>

    ``None`` rather than ``False`` when the list cannot be read, because "we
    could not confirm" and "it is not there" call for different words.
    """
    request = (request or "").upper()
    name = (name or "").upper()
    if not request or not name:
        return None
    try:
        response = session.get(
            ep.TRANSPORT_REQUEST_OBJECTS.url(number=request),
            action="read-transport-request",
            object_name=request,
        )
    except AdtError:
        return None

    try:
        root = ET.fromstring(response.text or "")
    except ET.ParseError:
        return None

    found_any = False
    for element in root.iter():
        if element.tag.rpartition("}")[2] != "abap_object":
            continue
        found_any = True
        attrs = {k.rpartition("}")[2]: v for k, v in element.attrib.items()}
        if (attrs.get("name", "").upper() == name
                and attrs.get("type", "").upper() == object_type.upper()):
            return True
    return False if found_any else None


def request_number_from(text: str) -> str:
    """Pull the request number out of a create response.

    Three shapes have been seen in the wild for this call — a ``TRKORR``
    element, an ``adtcore:name`` attribute, and a bare URI ending in the number
    — so the number is matched by its own format rather than by position. It is
    a distinctive format, and a wrong answer here is worse than no answer: the
    caller would record the object against a request that does not exist.
    """
    if not text:
        return ""
    match = _REQUEST_NUMBER.search(text)
    return match.group(1) if match else ""


def _data_element(text: str) -> ET.Element | None:
    if not text or not text.strip():
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag.rpartition("}")[2].upper() == "DATA":
        return root
    for element in root.iter():
        if element.tag.rpartition("}")[2].upper() == "DATA":
            return element
    return None


def _xml_text(value: str) -> str:
    return (
        (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _xml_attr(value: str) -> str:
    """The description goes in an attribute, so quotes have to go too."""
    return _xml_text(value).replace('"', "&quot;")
