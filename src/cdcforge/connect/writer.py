"""Stage 4 — creating a CDS view in the system.

This is the only module that changes anything, and it is written on the
assumption that it will one day be pointed at a system where a mistake matters.
Three things follow from that.

**It refuses far more than it accepts.** A write has to clear every gate in
:class:`WritePolicy` before a request is built: the name has to be in the
customer namespace, the package has to be one the caller explicitly allowed,
and the object must not already exist as something SAP owns. These are checked
here, below the UI and below the CLI, for the same reason the read-only switch
is: a bug one layer up must not be able to route around them.

**Every step is recorded, including the ones that did not run.** A pipeline
that says only "failed" leaves someone guessing whether a half-built object is
sitting in the system. :class:`WriteResult` carries the outcome of each step in
order, so the answer is always readable.

**It cleans up after itself.** Activation is the step that fails, and it fails
after the object exists. If anything goes wrong once the object has been
created, the object is deleted and the lock released — and if the rollback
itself fails, that is reported rather than swallowed, because then there really
is something left behind and the user needs to know its name.

The wire format is unpublished, like the rest of ADT. Every payload here is
reconstructed and every one is a candidate for a release-specific surprise.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from cdcforge.connect import endpoints as ep
from cdcforge.connect.checkrun import CheckRunResult, run_checkrun
from cdcforge.connect.session import AdtError, AdtSession
from cdcforge.connect.transport import (
    TransportNeed,
    check_transport,
    contains_object,
    request_status,
)

#: SAP's local, non-transportable package. Objects here belong to the user who
#: created them, need no transport request, and can be deleted without trace —
#: which makes it the only sane default for a pipeline like this one.
LOCAL_PACKAGE = "$TMP"

_CUSTOMER_NAME = re.compile(r"^[YZ][A-Z0-9_]{0,29}$")

#: A customer package. Same namespace rule as an object name, and deliberately
#: not "anything that is not SAP's" — an unknown namespace is refused.
_CUSTOMER_PACKAGE = re.compile(r"^[YZ][A-Z0-9_/]{0,29}$")

_CREATE_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources" '
    'xmlns:adtcore="http://www.sap.com/adt/core" '
    'adtcore:description="{description}" '
    'adtcore:name="{name}" '
    'adtcore:type="DDLS/DF">'
    '<adtcore:packageRef adtcore:name="{package}"/>'
    "</ddl:ddlSource>"
)

_ACTIVATE_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
    '<adtcore:objectReference adtcore:uri="{uri}" adtcore:name="{name}"/>'
    "</adtcore:objectReferences>"
)


class WriteRefused(Exception):
    """A guard said no. Raised before anything is sent."""

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy


@dataclass(frozen=True)
class WritePolicy:
    """What this pipeline is permitted to create, and where.

    Deliberately a value the caller has to construct rather than a set of
    defaults buried in a function. Widening it is then a visible edit at the
    call site.
    """

    packages: frozenset[str] = frozenset({LOCAL_PACKAGE})
    """Packages a new object may be created in. ``$TMP`` alone by default."""

    require_customer_namespace: bool = True
    """Names must start with Y or Z. There is no switch to turn this off in
    the UI, and it exists as a field only so a test can construct the refusal
    it guards against."""

    allow_transportable: bool = False
    """Permit any *customer-namespace* package, not just the listed ones.

    This exists because the obvious way to widen the allowlist is the wrong
    one. An earlier CLI built its policy as
    ``WritePolicy(packages={args.package})``, which passes the guard by
    construction — the check then only confirms that the user typed what the
    user typed, and an SAP package typed by mistake sails through.

    So widening is a flag, not a value: turning it on still leaves
    :data:`_CUSTOMER_PACKAGE` between the caller and the system, and there is
    no argument anywhere that can route around it. What the flag really says is
    "this run may write somewhere transportable", and everything that follows
    from that — the CTS check, the mandatory request — is then not optional.
    """

    def check_name(self, name: str) -> None:
        upper = (name or "").upper()
        if self.require_customer_namespace and not _CUSTOMER_NAME.match(upper):
            raise WriteRefused(
                f"{name!r} is not a customer-namespace name",
                remedy="A generated object must be named Z… or Y…. The tool "
                "never creates or modifies anything in SAP's namespace.",
            )

    def check_package(self, package: str) -> None:
        upper = (package or "").upper()
        if upper in {p.upper() for p in self.packages}:
            return
        if self.allow_transportable and _CUSTOMER_PACKAGE.match(upper):
            return
        if self.allow_transportable:
            raise WriteRefused(
                f"package {package!r} is not in the customer namespace",
                remedy="A transportable target must be a Z… or Y… package. "
                "The tool never creates anything in an SAP package, and there "
                "is no flag that allows it.",
            )
        raise WriteRefused(
            f"package {package!r} is not permitted by this policy",
            remedy=f"Allowed: {', '.join(sorted(self.packages))}. "
            f"{LOCAL_PACKAGE} is local and non-transportable, which is why it "
            f"is the default. Writing anywhere else is a deliberate choice and "
            f"needs a transport request.",
        )


@dataclass
class WriteStep:
    """One step of the pipeline, and whether it ran."""

    name: str
    ok: bool = False
    ran: bool = False
    detail: str = ""

    def render(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.ran else "-   ")
        return f"  {mark} {self.name}{f' — {self.detail}' if self.detail else ''}"


@dataclass
class WriteResult:
    """What happened, step by step."""

    object_name: str
    package: str
    steps: list[WriteStep] = field(default_factory=list)
    activated: bool = False
    left_inactive: bool = False
    """Created and filled, deliberately not activated.

    A real outcome, not a failure. Activating needs an authorisation that
    reading and writing the source do not, so on a system where the user cannot
    activate, the tool still does everything else and hands over an inactive
    object to activate in Eclipse.
    """

    deleted: bool = False
    """Removal was the goal and it succeeded.

    Kept apart from ``rolled_back``, which means a *create* failed and its
    half-built object was cleaned up. Both delete an object; only one of them
    is a success, and conflating the two reported a working ``drop`` as
    FAILED.
    """

    rolled_back: bool = False
    orphaned: bool = False
    """The object exists and could not be cleaned up. Needs a human."""

    error: str = ""
    remedy: str = ""
    check: CheckRunResult | None = None
    transport_need: TransportNeed | None = None
    """What CTS said about this package, kept even when the write was refused —
    that is exactly when the caller needs it, to show which requests exist."""

    transport: str = ""
    """The request this object was recorded in. Empty means none was needed,
    which for ``$TMP`` is the normal case rather than a gap."""

    @property
    def ok(self) -> bool:
        """Did the pipeline do what it was asked to do?

        Not activating is only a failure when activation was asked for. Judging
        it otherwise made a deliberate hand-over look like a broken run.
        """
        return (
            self.activated or self.left_inactive or self.deleted
        ) and not self.error

    def step(self, name: str) -> WriteStep:
        found = WriteStep(name=name)
        self.steps.append(found)
        return found

    def render(self) -> str:
        created = any(s.name == "create" and s.ok for s in self.steps)
        if not self.ok:
            headline = "FAILED"
        elif self.deleted:
            headline = "DELETED"
        elif self.left_inactive:
            headline = "CREATED, INACTIVE"
        elif not created:
            # activate_view on an object that already existed — saying CREATED
            # would claim something that did not happen.
            headline = "ACTIVATED"
        else:
            headline = "CREATED"
        lines = [f"{headline}  {self.object_name}"]
        lines += [s.render() for s in self.steps]
        if self.transport and (self.activated or self.left_inactive):
            lines.append(
                f"  moved: recorded in {self.transport} — release it in SE09 to "
                f"send {self.object_name} onward."
            )
        if self.left_inactive:
            lines.append(
                f"  next:  {self.object_name} exists in {self.package} with its "
                f"source in place, and is not active. Activate it in Eclipse "
                f"(or ADT) to make it usable."
            )
        if self.error:
            lines.append(f"  error: {self.error}")
        if self.remedy:
            lines.append(f"  fix:   {self.remedy}")
        if self.orphaned:
            lines.append(
                f"  WARNING: {self.object_name} still exists in {self.package} "
                f"and could not be removed. Delete it by hand."
            )
        return "\n".join(lines)


def create_view(
    session: AdtSession,
    name: str,
    ddl: str,
    *,
    package: str = LOCAL_PACKAGE,
    description: str = "",
    policy: WritePolicy | None = None,
    activate: bool = True,
    transport: str = "",
) -> WriteResult:
    """Create, fill, check, and activate one CDS view. Never raises.

    The order matters. The syntax check runs *after* the source is uploaded and
    *before* activation, because that is the only point where SAP will tell you
    what it thinks of the code without committing to it. A failed check aborts
    and rolls back, so a broken view is never left active.

    ``activate=False`` stops after the check and hands over an inactive object
    to activate by hand. That is a supported outcome, not a failure: activation
    needs an authorisation that creating and writing the source do not, and on
    a system where the user lacks it there is no reason to throw away the work.
    Nothing is rolled back in that case — but a source SAP's check *rejects* is
    still rolled back either way, because leaving broken DDL behind helps
    nobody.

    ``transport`` is the request to record the object in. Whether one is needed
    is not inferred from the package name — CTS is asked, and a check that does
    not clearly succeed refuses the write rather than proceeding without a
    change record.
    """
    policy = policy or WritePolicy()
    upper = (name or "").upper()
    ddl = _normalise(ddl)
    result = WriteResult(object_name=upper, package=package.upper())

    try:
        policy.check_name(upper)
        policy.check_package(package)
        _check_ddl_declares(upper, ddl)
        _refuse_if_not_ours(session, upper, result)
        transport = _resolve_transport(session, upper, package, transport, result)
    except WriteRefused as exc:
        result.error = exc.message
        result.remedy = exc.remedy
        return result

    uri = ep.DDL_OBJECT.url(name=upper.lower())
    created = False
    lock_handle = ""

    # A lock is only held by a stateful session; a stateless one silently loses
    # it between requests and the PUT then fails with an unhelpful 403.
    was_stateful = session.stateful
    session.stateful = True
    try:
        created = _create(session, upper, package, description, result, transport)
        if not created:
            return result

        lock_handle = _lock(session, upper, result)
        if not lock_handle:
            _rollback(session, upper, "", result)
            return result

        if not _put_source(session, upper, ddl, lock_handle, result, transport):
            _rollback(session, upper, lock_handle, result)
            return result

        if not _check(session, upper, result, activating=activate):
            _rollback(session, upper, lock_handle, result)
            return result

        # Unlock *before* activating. ADT answers 403 to an activation
        # attempted while the caller still holds the object's lock, and the
        # generic translation of that status — "authenticated but not
        # authorised" — reads exactly like a missing authorisation. It cost a
        # whole fallback path built on the wrong diagnosis. Measured: same
        # object, same user, same payload; locked 403, unlocked 200 with
        # activationExecuted="true".
        _unlock(session, upper, lock_handle, result)
        lock_handle = ""

        if not activate:
            # Deliberately stopping here. The object keeps its source; nothing
            # is rolled back, because an inactive object is the requested
            # outcome rather than a half-finished one.
            skipped = result.step("activate")
            skipped.detail = "not requested — left inactive for manual activation"
            result.left_inactive = True
            _confirm_recorded(session, upper, transport, result)
            return result

        if not _activate(session, upper, uri, result):
            _rollback(session, upper, "", result)
            return result

        result.activated = True
        _confirm_recorded(session, upper, transport, result)
        return result
    finally:
        session.stateful = was_stateful


def activate_view(
    session: AdtSession, name: str, *, policy: WritePolicy | None = None
) -> WriteResult:
    """Activate an object that already exists. Never raises.

    Separate from :func:`create_view` because the two failures are different:
    a create that fails leaves nothing, while an activation that fails leaves
    an object that is still there and still inactive — which is recoverable and
    should not be deleted.

    Nothing is locked. ADT refuses an activation attempted while the caller
    holds the object's lock, which is the whole reason the create pipeline
    unlocks first.
    """
    policy = policy or WritePolicy()
    upper = (name or "").upper()
    result = WriteResult(object_name=upper, package="")

    try:
        policy.check_name(upper)
        _refuse_if_not_ours(session, upper, result, must_exist=True)
    except WriteRefused as exc:
        result.error = exc.message
        result.remedy = exc.remedy
        return result

    # SAP's check first, against the inactive version — the one about to
    # become real. Activating something known to be broken helps nobody.
    if not _check(session, upper, result, activating=True):
        return result

    if _activate(session, upper, ep.DDL_OBJECT.url(name=upper.lower()), result):
        result.activated = True
    else:
        result.remedy = (
            f"{upper} still exists and is still inactive. Fix the source and "
            f"try again, or activate it in Eclipse to see the full message."
        )
    return result


def delete_view(
    session: AdtSession,
    name: str,
    *,
    policy: WritePolicy | None = None,
    package: str = "",
    transport: str = "",
) -> WriteResult:
    """Remove a view this tool created. Same guards as creating one.

    A deletion is a change like any other, so an object in a transportable
    package needs a request to record it — measured, ADT answers a bare HTTP
    400 without one, after the lock has already been taken. ``transport`` is
    therefore not optional there, and the package is discovered rather than
    asked for: the caller deleting an object usually does not know or care
    which package it is in.
    """
    policy = policy or WritePolicy()
    upper = (name or "").upper()
    result = WriteResult(object_name=upper, package="")

    try:
        policy.check_name(upper)
        _refuse_if_not_ours(session, upper, result, must_exist=True)
        package = package or _package_of(session, upper)
        result.package = package
        if package and package.upper() != LOCAL_PACKAGE:
            transport = _resolve_transport(
                session,
                upper,
                package,
                transport,
                result,
                operation="D",
                require_unless_local=True,
            )
    except WriteRefused as exc:
        result.error = exc.message
        result.remedy = exc.remedy
        return result

    was_stateful = session.stateful
    session.stateful = True
    try:
        handle = _lock(session, upper, result)
        if not handle:
            return result
        step = result.step("delete")
        step.ran = True
        params = {"lockHandle": handle}
        if transport:
            params["corrNr"] = transport
        try:
            session.delete(
                ep.DELETE_DDL_SOURCE.url(name=upper.lower()),
                params=params,
                action="delete-ddl-source",
                object_name=upper,
            )
            step.ok = True
            result.deleted = True
        except AdtError as exc:
            step.detail = exc.message
            result.error = exc.message
            # ADT puts the actual reason in the response body, which AdtError
            # carries as its remedy. Dropping it leaves "returned HTTP 400" and
            # nothing to act on.
            result.remedy = exc.remedy
            _unlock(session, upper, handle, result)
        return result
    finally:
        session.stateful = was_stateful


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _normalise(ddl: str) -> str:
    """Make the source safe to upload.

    A UTF-8 BOM makes SAP reject the whole file with *"Annotation could not be
    parsed"* pointing at line 1, which sends people hunting through their
    annotations for a syntax error that is not there. Editors and shells add
    one routinely — PowerShell's ``Out-File -Encoding utf8`` does — and this
    tool's own lexer strips it, so a file that parses perfectly here can still
    be refused by the system.

    Line endings are normalised for the same reason: to remove a difference
    between "worked on my machine" and "worked in the system".
    """
    if ddl.startswith("﻿"):
        ddl = ddl[1:]
    return ddl.replace("\r\n", "\n").replace("\r", "\n")


def _check_ddl_declares(name: str, ddl: str) -> None:
    """The DDL must define the object being created.

    ADT answers a mismatch with a bare HTTP 400 after the object has already
    been created and locked — so the run fails, rolls back, and tells the user
    nothing about why. Catching it here costs a parse and turns an opaque
    failure into the actual mistake, which is usually a copied file whose
    ``define view entity`` line was never renamed.
    """
    from cdcforge.parsing.ddl import parse_ddl

    view = parse_ddl(ddl, name_hint=name)
    if view.has_fatal_issue:
        issue = next((i for i in view.issues if i.fatal), None)
        raise WriteRefused(
            f"the DDL could not be parsed: {issue.message if issue else 'unknown'}",
            remedy="Fix the source before sending it. Nothing was created.",
        )

    declared = (view.name or "").upper()
    if declared and declared != name:
        raise WriteRefused(
            f"the DDL defines {declared}, not {name}",
            remedy=f"Rename the object in the source to {name}, or create it "
            f"under the name it declares. ADT reports this as a bare HTTP 400 "
            f"after the object already exists, which is why it is checked here.",
        )


def _refuse_if_not_ours(
    session: AdtSession, name: str, result: WriteResult, *, must_exist: bool = False
) -> None:
    """Never write over something that already exists and is not ours.

    The name guard already keeps us in the customer namespace, so this is
    belt-and-braces — but the one failure it catches is the expensive one: a
    customer object that happens to share a name, silently overwritten.
    """
    step = result.step("check target")
    step.ran = True
    try:
        session.get(
            ep.DDL_SOURCE.url(name=name.lower()),
            action="read-source",
            object_name=name,
        )
        exists = True
    except AdtError:
        exists = False

    if exists and not must_exist:
        step.detail = "already exists"
        raise WriteRefused(
            f"{name} already exists",
            remedy="Choose another name, or delete the existing object first. "
            "The tool will not overwrite an object it did not just create.",
        )
    if not exists and must_exist:
        step.detail = "not found"
        raise WriteRefused(f"{name} does not exist", remedy="Nothing to delete.")

    step.ok = True
    step.detail = "exists" if exists else "free"


def _resolve_transport(
    session: AdtSession,
    name: str,
    package: str,
    transport: str,
    result: WriteResult,
    operation: str = "I",
    require_unless_local: bool = False,
) -> str:
    """Settle whether this write needs a request, and that it has a usable one.

    The package name is not evidence. ``$TMP`` needs no request on every system
    anyone has looked at, but that is a convention rather than a rule, and a
    customer namespace can be configured either way. So CTS decides, and the
    three failure modes are kept apart because they need different things from
    the user: the check did not work, a request is needed and none was given,
    or one was given that CTS did not offer.
    """
    step = result.step("transport check")
    step.ran = True
    transport = (transport or "").upper().strip()
    need = check_transport(session, name, package, operation=operation)
    result.transport_need = need

    if need.unreadable:
        step.detail = "CTS did not answer clearly"
        raise WriteRefused(
            f"could not establish whether {package} requires a transport "
            f"request: {need.messages[0] if need.messages else 'unrecognised response'}",
            remedy="Nothing was created. An unreadable check is treated as a "
            "refusal rather than as 'no request needed' — writing without a "
            "change record because a response could not be parsed is the one "
            "outcome that cannot be undone.",
        )

    # For a deletion, CTS is not the authority its own answer implies. Asked
    # about DDLS ZCDCF_TRTEST in ZDSP_EXTRACTION with OPERATION=D it answers
    # RECORDING empty — no request needed — and the delete then fails with
    #
    #     ExceptionParameterNotFound: Parameter corrNr could not be found.
    #
    # after the lock has already been taken. So a deletion outside a local
    # package always needs a request, whatever the check says. Creations are
    # left to CTS, where its answer does match the endpoint's behaviour.
    if need.locked_in:
        # The object name is already held by a request — usually one this tool
        # used before, since a deletion stays recorded there. CTS reports
        # RECORDING empty in that case, which reads as "no request needed" and
        # means "already accounted for". Only that request will do, so it is
        # used rather than asked for, and a different one is refused before a
        # confusing 400 arrives.
        if transport and transport != need.locked_in:
            step.detail = f"{transport} conflicts with {need.locked_in}"
            raise WriteRefused(
                f"{name} is already held by {need.locked_in}, not {transport}",
                remedy=f"An object stays locked to the request it was last "
                f"recorded in, including after it was deleted. Use "
                f"{need.locked_in}, or release it and try again.",
            )
        step.ok = True
        step.detail = f"already held by {need.locked_in} — recording there"
        result.transport = need.locked_in
        return need.locked_in

    required = need.required or (require_unless_local and not need.local)

    if not required:
        step.ok = True
        step.detail = "local package — no request needed" if need.local else (
            "no change recording required"
        )
        if transport:
            # Passing one anyway is harmless but worth saying, because it
            # usually means the user thinks they are writing somewhere else.
            step.detail += f"; {transport} ignored"
        return ""

    if not transport:
        step.detail = "a request is required and none was given"
        if not need.required:
            raise WriteRefused(
                f"deleting from {package} needs a transport request",
                remedy="ADT refuses the delete without one — "
                "'Parameter corrNr could not be found' — even though the CTS "
                "check reports no recording required for a deletion. Pass "
                "--transport with a modifiable request.",
            )
        available = "\n".join(f"  {r.render()}" for r in need.requests)
        raise WriteRefused(
            f"{package} records changes, so a transport request is required",
            remedy=(
                f"Pass one of these:\n{available}"
                if need.requests
                else (
                    "You have no open request that can take this object. "
                    "Create one first — the tool will do it with "
                    "--new-transport, or you can make one in SE09 and pass it "
                    "with --transport."
                    if not need.existing_only
                    else "This package only accepts existing requests, and you "
                    "have none open. Ask whoever owns the package."
                )
            ),
        )

    known = {r.number for r in need.requests}
    note = ""
    if transport not in known:
        # Not one CTS offered — which is not the same as unusable. That list
        # holds requests CTS considers usable by this user for this object; a
        # colleague's request someone has been told to put their objects in is
        # a normal thing to be handed and will not appear in it. So the number
        # is looked up rather than refused on absence.
        status = request_status(session, transport)
        refusal = status.refusal()
        if refusal:
            step.detail = refusal
            raise WriteRefused(
                f"{transport} cannot take this object",
                remedy=refusal
                + (
                    "\nCTS offered: " + ", ".join(sorted(known))
                    if known
                    else ""
                ),
            )
        if status.owner and status.owner.upper() != _user_of(session):
            # Allowed, but worth saying: putting objects in someone else's
            # request means they decide when it is released.
            note = f" (owned by {status.owner})"

    step.ok = True
    step.detail = f"recording in {transport}{note}"
    result.transport = transport
    return transport


def _user_of(session: AdtSession) -> str:
    profile = getattr(session, "profile", None)
    return (getattr(profile, "username", "") or "").upper()


def _package_of(session: AdtSession, name: str) -> str:
    """Which package does this object live in?

    Needed because a deletion has to be recorded in the same place a creation
    was, and the caller saying ``drop ZFOO`` has no reason to know where ZFOO
    is. Returns empty when it cannot be read, which sends the caller down the
    local path — the right default, since a missing package reference is what
    a ``$TMP`` object looks like.
    """
    try:
        response = session.get(
            ep.DDL_OBJECT.url(name=name.lower()),
            action="read-object",
            object_name=name,
        )
    except AdtError:
        return ""
    try:
        root = ET.fromstring(response.text or "")
    except ET.ParseError:
        return ""
    for element in root.iter():
        if element.tag.rpartition("}")[2] != "packageRef":
            continue
        for key, value in element.attrib.items():
            if key.rpartition("}")[2] == "name":
                return (value or "").upper()
    return ""


def _confirm_recorded(
    session: AdtSession, name: str, transport: str, result: WriteResult
) -> None:
    """Check the object really is in the request, rather than assuming it.

    Never fails the run. The object exists and is activated by this point, so a
    missing change record is something to *say* loudly, not something to roll
    an activated object back over — deleting working work because a listing
    could not be read would be the worse mistake.
    """
    if not transport:
        return
    step = result.step("confirm recorded")
    step.ran = True
    listed = contains_object(session, transport, name)
    if listed is True:
        step.ok = True
        step.detail = f"{name} is in {transport}"
    elif listed is False:
        step.detail = (
            f"{name} is NOT in {transport} — the object exists but nothing is "
            f"recording it"
        )
        result.remedy = (
            f"{name} was created and is not in any transport request the tool "
            f"can see. Add it by hand in SE09 before it is lost to the next "
            f"system copy."
        )
    else:
        step.ok = True
        step.detail = f"could not read {transport}'s object list — unconfirmed"


def _create(
    session: AdtSession,
    name: str,
    package: str,
    description: str,
    result: WriteResult,
    transport: str = "",
) -> bool:
    step = result.step("create")
    step.ran = True
    payload = _CREATE_PAYLOAD.format(
        name=name,
        package=package.upper(),
        description=_xml_attr(description or "Generated by CDC Forge"),
    )
    try:
        session.post(
            ep.CREATE_DDL_SOURCE.path,
            body=payload,
            params={"corrNr": transport} if transport else None,
            action="create-ddl-source",
            object_name=name,
        )
    except AdtError as exc:
        step.detail = exc.message
        result.error = exc.message
        # Blaming the payload for an authorisation failure sends people to
        # read XML when what they need is S_DEVELOP. Name the likely cause.
        lowered = exc.message.lower()
        if "not authorised" in lowered or "not authorized" in lowered:
            result.remedy = (
                f"Either the user cannot create here — that needs S_DEVELOP "
                f"with object type DDLS and activity 01 — or {name} was "
                f"deleted very recently. SAP refuses to recreate a just-deleted "
                f"name for a while; a different name works immediately, and "
                f"the original comes back after a few minutes."
            )
        else:
            result.remedy = (
                "The create payload is reconstructed, not documented — a "
                "release may expect a different shape. Check the audit log "
                "for the body."
            )
        return False
    step.ok = True
    step.detail = f"in {package.upper()}"
    if transport:
        step.detail += f", recorded in {transport}"
    return True


def _lock(session: AdtSession, name: str, result: WriteResult) -> str:
    step = result.step("lock")
    step.ran = True
    try:
        response = session.post(
            ep.LOCK.url(name=name.lower()),
            action="lock",
            object_name=name,
            headers={"Accept": "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.Result"},
        )
    except AdtError as exc:
        step.detail = exc.message
        result.error = exc.message
        return ""

    handle = _lock_handle_from(response.text)
    if not handle:
        step.detail = "no lock handle in the response"
        result.error = "the lock succeeded but returned no handle"
        return ""
    step.ok = True
    return handle


def _put_source(
    session: AdtSession,
    name: str,
    ddl: str,
    handle: str,
    result: WriteResult,
    transport: str = "",
) -> bool:
    step = result.step("upload source")
    step.ran = True
    params = {"lockHandle": handle}
    if transport:
        params["corrNr"] = transport
    try:
        session.put(
            ep.UPDATE_DDL_SOURCE.url(name=name.lower()),
            body=ddl,
            params=params,
            action="update-ddl-source",
            object_name=name,
        )
    except AdtError as exc:
        step.detail = exc.message
        result.error = exc.message
        return False
    step.ok = True
    step.detail = f"{len(ddl)} characters"
    return True


def _check(
    session: AdtSession, name: str, result: WriteResult, *, activating: bool
) -> bool:
    """SAP's own syntax check, before activation commits anything.

    Checked against the *inactive* version, which is the one just uploaded. The
    first cut asked for ``active`` — the checkrun default — and a freshly
    created object has no active version, so the system had nothing to look at
    and the check silently did not run. That reads exactly like a clean result.
    """
    step = result.step("syntax check")
    step.ran = True
    check = run_checkrun(session, name, version="inactive")
    result.check = check

    if not check.ran:
        step.detail = f"could not run — {check.summary}"
        if activating:
            # Activation will refuse a broken object anyway, so this is a gap
            # in the reporting rather than in the safety.
            step.ok = True
            step.detail += "; activation will be the check"
            return True
        # Nothing else is going to look at this source. Say so loudly rather
        # than handing over an unverified object as though it were checked.
        step.ok = True
        step.detail += "; the source is UNVERIFIED"
        return True
    if check.clean:
        step.ok = True
        step.detail = "clean"
        return True

    # summary is a property, not a method. Calling it crashed with "'str' object
    # is not callable" — and only on this branch, so the happy path never saw it.
    step.detail = check.summary
    result.error = f"SAP's syntax check rejected the source: {check.summary}"
    result.remedy = "Fix the generated DDL and try again. Nothing was activated."
    return False


def _activate(
    session: AdtSession, name: str, uri: str, result: WriteResult
) -> bool:
    step = result.step("activate")
    step.ran = True
    payload = _ACTIVATE_PAYLOAD.format(uri=uri, name=name)
    try:
        response = session.post(
            ep.ACTIVATE.path, body=payload, action="activate", object_name=name
        )
    except AdtError as exc:
        step.detail = exc.message
        result.error = exc.message
        return False

    problems = _activation_errors(response.text)
    if problems:
        step.detail = problems[0]
        result.error = f"activation reported: {problems[0]}"
        result.remedy = (
            "The object was created but not activated, so it has been removed."
        )
        return False
    step.ok = True
    return True


def _unlock(
    session: AdtSession, name: str, handle: str, result: WriteResult
) -> None:
    if not handle:
        return
    step = result.step("unlock")
    step.ran = True
    try:
        session.post(
            ep.UNLOCK.url(name=name.lower(), handle=handle),
            action="unlock",
            object_name=name,
        )
        step.ok = True
    except AdtError as exc:
        # Not fatal: the lock dies with the session. Worth reporting, because a
        # stuck lock is confusing if the user goes looking in Eclipse.
        step.detail = f"{exc.message} (the lock expires with the session)"


def _rollback(
    session: AdtSession, name: str, handle: str, result: WriteResult
) -> None:
    """Remove the half-built object. Reports failure rather than hiding it.

    The handle may be empty: activation happens after the lock is released, so
    a failure there has nothing to delete with and must take a fresh lock.
    """
    step = result.step("roll back")
    step.ran = True

    if not handle:
        try:
            response = session.post(
                ep.LOCK.url(name=name.lower()),
                action="lock",
                object_name=name,
                headers={
                    "Accept": "application/vnd.sap.as+xml;charset=UTF-8;"
                    "dataname=com.sap.adt.lock.Result"
                },
            )
            handle = _lock_handle_from(response.text)
        except AdtError as exc:
            step.detail = f"could not re-lock to delete — {exc.message}"
            result.orphaned = True
            return

    try:
        session.delete(
            ep.DELETE_DDL_SOURCE.url(name=name.lower()),
            params={"lockHandle": handle} if handle else None,
            action="delete-ddl-source",
            object_name=name,
        )
        step.ok = True
        step.detail = "created object removed"
        result.rolled_back = True
    except AdtError as exc:
        step.detail = exc.message
        result.orphaned = True
    _unlock(session, name, handle, result)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def _xml_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _lock_handle_from(text: str) -> str:
    """Pull the lock handle out of the lock response.

    Two shapes are accepted because the response differs by release: an XML
    document with a ``LOCK_HANDLE`` element, and a bare data envelope. Anything
    else returns empty, which the caller treats as a failed lock rather than
    guessing.
    """
    if not text:
        return ""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        match = re.search(r"<LOCK_HANDLE>(.*?)</LOCK_HANDLE>", text, re.S)
        return match.group(1).strip() if match else ""

    for element in root.iter():
        if element.tag.rpartition("}")[2].upper() == "LOCK_HANDLE":
            return (element.text or "").strip()
    return ""


def _activation_errors(text: str) -> list[str]:
    """Error-severity messages from an activation response.

    An empty body means success. A body with messages may still be warnings,
    so severity is read rather than assumed — treating every message as fatal
    would roll back objects that activated perfectly well.
    """
    if not text or not text.strip():
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    problems: list[str] = []
    for element in root.iter():
        if element.tag.rpartition("}")[2] not in ("msg", "message"):
            continue
        severity = ""
        for key, value in element.attrib.items():
            if key.rpartition("}")[2] in ("type", "severity"):
                severity = value.upper()
        if severity and not severity.startswith(("E", "A")):
            continue
        text_element = next(
            (
                e.text
                for e in element.iter()
                if e.tag.rpartition("}")[2] == "shortText" and e.text
            ),
            None,
        )
        problems.append((text_element or element.attrib.get("text") or "").strip())
    return [p for p in problems if p]
