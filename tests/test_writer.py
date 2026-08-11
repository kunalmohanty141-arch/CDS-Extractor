"""Stage 4 — the write pipeline.

The tests that matter here are the refusals. Everything this module can do is
reversible except the thing it must never do: touch an object it did not
create. So the guards are tested before the happy path, and they are tested at
the connector layer, because that is where they are enforced.
"""

from __future__ import annotations

import pytest

from cdcforge.connect import endpoints as ep
from tests.test_transport import (
    LOCAL_RESPONSE,
    LOCKED_RESPONSE,
    RELEASED_REQUEST,
    REQUEST_WITH_OBJECTS,
    TRANSPORTABLE_RESPONSE,
    WITH_REQUESTS,
)
from cdcforge.connect.writer import (
    LOCAL_PACKAGE,
    WritePolicy,
    WriteRefused,
    WriteResult,
    _activation_errors,
    _lock_handle_from,
    _xml_attr,
    create_view,
)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["I_SALESORDER", "MARA", "C_SALESDOCUMENTITEMDEX", "/DMBE/THING", "", "1ZED"],
)
def test_only_customer_namespace_names_are_accepted(name):
    """The tool must never create or modify anything in SAP's namespace.

    A standing instruction, and the reason this is a connector-level check
    rather than a UI one: a bug in the UI must not be able to route around it.
    """
    with pytest.raises(WriteRefused):
        WritePolicy().check_name(name)


@pytest.mark.parametrize("name", ["ZC_THING", "Y_THING", "Z", "ZI_SALESORDER_EX"])
def test_customer_names_pass(name):
    WritePolicy().check_name(name)


@pytest.mark.parametrize("package", ["SD", "$SAP", "ZPACKAGE", ""])
def test_only_allowed_packages_are_accepted(package):
    with pytest.raises(WriteRefused):
        WritePolicy().check_package(package)


def test_the_cli_does_not_build_its_policy_from_the_argument_it_checks():
    """The regression that made the package guard unable to refuse anything.

    An earlier cut of ``cmd_create`` did::

        policy = WritePolicy(packages=frozenset({args.package.upper()}))

    which permits whatever was typed. ``--package SD`` sailed straight through
    and was stopped only by SAP answering 409 — the guard never fired.

    What is pinned is the *allowlist*, not the whole call. ``allow_transportable``
    was added so a run can target a real package, and it is safe in a way
    ``packages=`` is not: it widens to a namespace the code fixes, rather than
    to a value the caller supplies. So the assertion is narrow and exact —
    ``packages`` is never passed here, positionally or by keyword.
    """
    import ast
    import inspect

    from cdcforge import cli_connect

    tree = ast.parse(inspect.getsource(cli_connect.cmd_create))
    policies = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "WritePolicy"
    ]
    assert policies, "cmd_create must construct a WritePolicy"
    for call in policies:
        assert not call.args, (
            "WritePolicy's first field is `packages` — a positional argument "
            "here is the allowlist being set from the command line"
        )
        assert not any(kw.arg == "packages" for kw in call.keywords), (
            "cmd_create must not set `packages` — deriving the allowlist from "
            "--package makes the guard unable to refuse anything"
        )


def test_the_local_package_is_the_default():
    """$TMP is local and non-transportable, so a mistake leaves no trace in a
    transport and cannot travel to another system."""
    assert WritePolicy().packages == frozenset({LOCAL_PACKAGE})
    WritePolicy().check_package("$TMP")
    WritePolicy().check_package("$tmp")


def test_widening_the_policy_is_explicit():
    policy = WritePolicy(packages=frozenset({"$TMP", "ZCDC"}))
    policy.check_package("ZCDC")
    with pytest.raises(WriteRefused):
        policy.check_package("SD")


@pytest.mark.parametrize("package", ["ZCDC", "ZDSP_EXTRACTION", "YDSC", "Z001"])
def test_transportable_packages_need_the_flag(package):
    with pytest.raises(WriteRefused):
        WritePolicy().check_package(package)
    WritePolicy(allow_transportable=True).check_package(package)


@pytest.mark.parametrize("package", ["SD", "SAP", "$SAP", "ABC", "", "/DMBE/SD"])
def test_the_transportable_flag_still_refuses_sap_packages(package):
    """The flag widens to a namespace, not to whatever was typed.

    This is the whole reason it is a flag rather than a package list: there is
    no argument anywhere that lets a write reach an SAP package.

    And the namespace check cannot be delegated to CTS, because CTS does not
    make it. Measured on S/4HANA 2022::

        $ cdcforge transport --profile DEV SD
        SD: no change recording required

    A tool that asked CTS whether ``SD`` was safe to write into would be told
    yes. The order in ``create_view`` is what matters — ``check_package`` runs
    before the CTS call, so an SAP package is refused before the system is even
    asked about it.
    """
    with pytest.raises(WriteRefused):
        WritePolicy(allow_transportable=True).check_package(package)


def test_the_namespace_guard_runs_before_cts_is_asked(monkeypatch):
    """Ordering, pinned — see the measurement above."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_SAPPKG",
        _ddl("ZC_SAPPKG"),
        package="SD",
        policy=WritePolicy(allow_transportable=True),
    )

    assert not result.ok
    assert "customer namespace" in result.error
    assert "transport-check" not in session.calls, (
        "CTS answers 'no change recording required' for SD — asking it at all "
        "would be trusting the wrong authority"
    )


# ---------------------------------------------------------------------------
# The pipeline refuses before it sends
# ---------------------------------------------------------------------------


class RefusingSession:
    """Fails the test if the pipeline sends anything at all."""

    stateful = False

    def _boom(self, *a, **k):  # pragma: no cover - only runs on failure
        raise AssertionError("a guard should have stopped this before the network")

    get = post = put = delete = _boom


def test_a_refused_name_never_reaches_the_network():
    result = create_view(RefusingSession(), "I_SALESORDER", _ddl("I_SALESORDER"))
    assert not result.ok
    assert "customer-namespace" in result.error
    assert not result.activated


def test_a_ddl_that_defines_a_different_object_is_refused():
    """A copied file whose `define view entity` line was never renamed.

    ADT answers this with a bare HTTP 400 *after* the object has been created
    and locked, so the run fails, rolls back, and explains nothing. Catching it
    before anything is sent turns it into the actual mistake.
    """
    result = create_view(
        RefusingSession(),
        "ZC_WANTED",
        "define view entity ZC_SOMETHING_ELSE as select from t001 "
        "{ key bukrs as A }",
    )
    assert not result.ok
    assert "defines ZC_SOMETHING_ELSE, not ZC_WANTED" in result.error


def test_generated_ddl_declares_the_name_it_is_filed_under(metadata):
    """The writer refuses a DDL that defines a different object.

    The UI and CLI both feed it generator output keyed by `generated.name`, so
    if the generator ever emitted a `define view entity` line that disagreed
    with that name, every create would be refused — and the failure would show
    up in front of a customer, not here.
    """
    from cdcforge.connect.writer import _check_ddl_declares
    from cdcforge.generator import generate_view_for_table, generate_wrapper
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext, validate_object

    table = metadata.get_table("ZCUSTORDER")
    built = generate_view_for_table(table)
    assert built.ddl
    _check_ddl_declares(built.name.upper(), built.ddl)

    source = metadata.get_view_source("ZI_BUSINESSAREA")
    ctx = ValidationContext(
        view=parse_ddl(source, name_hint="ZI_BUSINESSAREA"), metadata=metadata
    )
    wrapper = generate_wrapper(ctx, validate_object("ZI_BUSINESSAREA", metadata))
    assert wrapper.ddl, wrapper.refused_because
    _check_ddl_declares(wrapper.name.upper(), wrapper.ddl)


def test_a_client_column_is_found_by_type_not_by_name():
    """ACDOCA calls its client column RCLNT, not MANDT.

    A name-only check let it through as an ordinary key field, and SAP refused
    the generated view: "Client field is not allowed in the entity view". The
    DDIC data type CLNT is what actually makes a column the client.
    """
    from cdcforge.metadata.types import FieldMeta

    assert FieldMeta(name="RCLNT", data_type="CLNT").is_client
    assert FieldMeta(name="MANDT", data_type="CLNT").is_client
    assert FieldMeta(name="MANDT").is_client, "the name check remains a fallback"
    assert not FieldMeta(name="RLDNR", data_type="CHAR").is_client
    assert not FieldMeta(name="RBUKRS", data_type="CHAR").is_client


def test_field_metadata_survives_the_cache_intact():
    """to_dict is what the store persists.

    Leaving the currency and unit references out of it meant a cached table
    generated different DDL from a freshly-read one — the amounts would lose
    their casts and the view would stop activating.
    """
    from cdcforge.metadata.types import FieldMeta

    original = FieldMeta(
        name="NETPR", data_type="CURR", length=11, decimals=2,
        ref_table="EKKO", ref_field="WAERS",
    )
    revived = FieldMeta.from_dict(original.to_dict())
    assert revived == original
    assert revived.ref_table == "EKKO" and revived.ref_field == "WAERS"
    assert revived.is_amount_or_quantity
    assert not revived.reference_is_local("EKPO")


def test_a_matching_name_is_accepted_whatever_its_case():
    from cdcforge.connect.writer import _check_ddl_declares

    _check_ddl_declares(
        "ZC_THING",
        "define view entity zc_thing as select from t001 { key bukrs as A }",
    )


def test_unparseable_ddl_is_refused_before_anything_is_created():
    result = create_view(RefusingSession(), "ZC_BROKEN", "define view entity ((((")
    assert not result.ok
    assert "could not be parsed" in result.error


def test_a_refused_package_never_reaches_the_network():
    result = create_view(
        RefusingSession(), "ZC_THING", _ddl("ZC_THING"), package="SD"
    )
    assert not result.ok
    assert "not permitted" in result.error


# ---------------------------------------------------------------------------
# Endpoint classification — the write paths must be classified as writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/sap/bc/adt/ddic/ddl/sources"),
        ("PUT", "/sap/bc/adt/ddic/ddl/sources/zc_x/source/main"),
        ("DELETE", "/sap/bc/adt/ddic/ddl/sources/zc_x"),
        ("POST", "/sap/bc/adt/ddic/ddl/sources/zc_x?_action=LOCK&accessMode=MODIFY"),
        ("POST", "/sap/bc/adt/ddic/ddl/sources/zc_x?_action=UNLOCK&lockHandle=abc"),
        ("POST", "/sap/bc/adt/activation?method=activate&preauditRequested=true"),
    ],
)
def test_every_write_endpoint_is_classified_as_a_write(method, path):
    """Read-only mode blocks on this classification, so a misclassified write
    endpoint would be a hole straight through the kill switch."""
    assert ep.classify(method, path) is ep.Access.WRITE


def test_reading_a_source_is_still_a_read():
    # The update endpoint shares this path and differs only by the verb.
    assert ep.classify(
        "GET", "/sap/bc/adt/ddic/ddl/sources/zc_x/source/main"
    ) is ep.Access.READ


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def test_lock_handle_is_read_from_xml():
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<asx:abap xmlns:asx="http://www.sap.com/abapxml"><asx:values><DATA>'
        "<LOCK_HANDLE>20260810XYZ</LOCK_HANDLE></DATA></asx:values></asx:abap>"
    )
    assert _lock_handle_from(body) == "20260810XYZ"


def test_lock_handle_is_read_from_a_non_xml_body():
    assert _lock_handle_from("junk <LOCK_HANDLE>ABC123</LOCK_HANDLE> junk") == "ABC123"


def test_a_missing_lock_handle_is_empty_not_a_guess():
    assert _lock_handle_from("<foo/>") == ""
    assert _lock_handle_from("") == ""


def test_an_empty_activation_response_means_success():
    assert _activation_errors("") == []
    assert _activation_errors("   ") == []


def test_activation_warnings_are_not_treated_as_failures():
    """Rolling back on a warning would delete objects that activated fine."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<chkl:messages xmlns:chkl="http://www.sap.com/abapxml/checklist">'
        '<msg type="W"><shortText>just a warning</shortText></msg>'
        "</chkl:messages>"
    )
    assert _activation_errors(body) == []


def test_activation_errors_are_reported():
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<chkl:messages xmlns:chkl="http://www.sap.com/abapxml/checklist">'
        '<msg type="E"><shortText>field UNKNOWN is not defined</shortText></msg>'
        "</chkl:messages>"
    )
    assert _activation_errors(body) == ["field UNKNOWN is not defined"]


def test_a_bom_is_stripped_before_upload():
    """SAP rejects a BOM with "Annotation could not be parsed" at line 1.

    Which sends people hunting through their annotations for a syntax error
    that is not there. Editors and shells add one routinely — PowerShell's
    `Out-File -Encoding utf8` does — and this tool's own lexer strips it, so a
    file that parses perfectly here is still refused by the system.
    """
    from cdcforge.connect.writer import _normalise

    assert _normalise("﻿@EndUserText.label: 'x'").startswith("@")


def test_line_endings_are_normalised():
    from cdcforge.connect.writer import _normalise

    assert _normalise("a\r\nb\rc") == "a\nb\nc"


def test_a_bom_does_not_defeat_the_name_check(monkeypatch):
    """The parse that checks the declared name must see the cleaned source."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session, "ZC_BOM", "﻿" + _ddl("ZC_BOM"), activate=False
    )
    assert result.ok, result.error


def test_description_is_xml_escaped():
    assert _xml_attr('a "quoted" <tag> & more') == (
        "a &quot;quoted&quot; &lt;tag&gt; &amp; more"
    )


# ---------------------------------------------------------------------------
# The unhappy paths, which the live smoke test never reaches
# ---------------------------------------------------------------------------


def _ddl(name: str) -> str:
    """Valid DDL declaring exactly ``name``.

    Has to be real now that the pipeline parses the source and checks the
    object it defines — a placeholder is refused, which is the point.
    """
    return f"define view entity {name} as select from t001 {{ key bukrs as A }}"


class FakeResponse:
    def __init__(self, text: str = "") -> None:
        self.text = text


class ScriptedSession:
    """Answers each action from a script, and records what was called.

    Enough to drive the pipeline through failures that a real system only
    produces occasionally — a rejected syntax check, a failed activation — so
    the rollback and reporting code is exercised rather than assumed.
    """

    stateful = False

    def __init__(
        self,
        fail_on: str = "",
        lock_handle: str = "LOCK1",
        transport_response: str = LOCAL_RESPONSE,
        request_objects: str = "",
    ) -> None:
        self.fail_on = fail_on
        self.lock_handle = lock_handle
        self.calls: list[str] = []
        self.exists = False
        self.transport_response = transport_response
        self.request_objects = request_objects

    def _run(self, action: str, text: str = "") -> FakeResponse:
        from cdcforge.connect.session import AdtError

        self.calls.append(action)
        if action == self.fail_on:
            raise AdtError(f"{action} refused", remedy="")
        return FakeResponse(text)

    def get(self, path, **kw):
        from cdcforge.connect.session import AdtError

        if "/cts/transportrequests/" in path:
            self.calls.append("read-transport-request")
            return FakeResponse(self.request_objects)

        self.calls.append("read-source")
        if not self.exists:
            raise AdtError("404", remedy="")
        return FakeResponse("define view ...")

    def post(self, path, **kw):
        action = kw.get("action", "")
        if action == "lock":
            return self._run(action, f"<DATA><LOCK_HANDLE>{self.lock_handle}</LOCK_HANDLE></DATA>")
        if action == "transport-check":
            # The real $TMP answer, captured from S/4HANA 2022. The pipeline
            # refuses a check it cannot read, so a fake that stays silent here
            # would fail every test for the wrong reason.
            return self._run(action, self.transport_response)
        return self._run(action)

    def put(self, path, **kw):
        return self._run(kw.get("action", ""))

    def delete(self, path, **kw):
        return self._run(kw.get("action", ""))


def test_a_transportable_package_refuses_without_a_request(monkeypatch):
    """The point of the whole feature: no silent unrecorded write.

    CTS says the package records changes and none was supplied, so nothing is
    sent — not created-then-rolled-back, but never created at all.
    """
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(transport_response=TRANSPORTABLE_RESPONSE)
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_NOREQ",
        _ddl("ZC_NOREQ"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
    )

    assert not result.ok
    assert "transport request is required" in result.error
    assert "create-ddl-source" not in session.calls, "nothing may be sent"


def test_a_supplied_request_is_recorded_on_create_and_upload(monkeypatch):
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(
        transport_response=WITH_REQUESTS,
        request_objects=REQUEST_WITH_OBJECTS.replace("ZCDCF_TRTEST", "ZC_TR"),
    )
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())
    sent: list[dict] = []

    original_post, original_put = session.post, session.put
    session.post = lambda path, **kw: (sent.append(kw), original_post(path, **kw))[1]
    session.put = lambda path, **kw: (sent.append(kw), original_put(path, **kw))[1]

    result = create_view(
        session,
        "ZC_TR",
        _ddl("ZC_TR"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900123",
    )

    assert result.ok, result.error
    assert result.transport == "DEVK900123"
    recorded = [
        kw for kw in sent if (kw.get("params") or {}).get("corrNr") == "DEVK900123"
    ]
    assert len(recorded) == 2, "both the create and the source upload carry corrNr"
    assert "DEVK900123" in result.render()
    confirm = next(s for s in result.steps if s.name == "confirm recorded")
    assert confirm.ok and "is in DEVK900123" in confirm.detail


def test_an_object_that_did_not_land_in_the_request_is_reported_loudly(monkeypatch):
    """corrNr silently ignored is the failure this step exists to catch.

    The object is real and activated, so it is not rolled back — but the run
    must not claim a change record that does not exist.
    """
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(
        transport_response=WITH_REQUESTS,
        request_objects=REQUEST_WITH_OBJECTS,  # lists ZCDCF_TRTEST, not ours
    )
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_LOST",
        _ddl("ZC_LOST"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900123",
    )

    assert result.activated, "an activated object is not thrown away over this"
    confirm = next(s for s in result.steps if s.name == "confirm recorded")
    assert not confirm.ok
    assert "NOT in DEVK900123" in confirm.detail
    assert "SE09" in result.remedy


def test_a_request_number_that_does_not_exist_is_refused(monkeypatch):
    """A typo caught before the object is created rather than after."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(
        transport_response=WITH_REQUESTS, request_objects="<tm:root/>"
    )
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_WRONGTR",
        _ddl("ZC_WRONGTR"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK999999",
    )

    assert not result.ok
    assert "cannot take this object" in result.error
    assert "was not found" in result.remedy
    assert "create-ddl-source" not in session.calls


def test_a_released_request_is_refused_with_the_reason(monkeypatch):
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(
        transport_response=WITH_REQUESTS, request_objects=RELEASED_REQUEST
    )
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_RELEASED",
        _ddl("ZC_RELEASED"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900851",
    )

    assert not result.ok
    assert "Released" in result.remedy
    assert "create-ddl-source" not in session.calls


def test_a_modifiable_request_cts_did_not_offer_is_accepted(monkeypatch):
    """Being handed a colleague's request number is an ordinary way to work.

    CTS's list holds requests it considers usable by *this* user for *this*
    object, which is not the set of requests that would work. Refusing on
    absence from that list made a normal instruction — "put it in mine" —
    impossible to follow.
    """
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(
        transport_response=WITH_REQUESTS, request_objects=REQUEST_WITH_OBJECTS
    )
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_COLLEAGUE",
        _ddl("ZC_COLLEAGUE"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900851",  # not in WITH_REQUESTS, which offers …123
    )

    assert result.ok, result.error
    assert result.transport == "DEVK900851"
    step = next(s for s in result.steps if s.name == "transport check")
    assert "owned by TESTUSER" in step.detail, (
        "putting objects in someone else's request means they decide when it "
        "is released — worth saying"
    )


def test_an_object_held_by_a_request_is_recorded_there_without_being_asked(
    monkeypatch,
):
    """Only that request will do, so it is used rather than requested.

    Asking the user which request to use, when exactly one can work, is a
    question with one right answer — and getting it wrong produces an opaque
    HTTP 400 after the object already exists.
    """
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(
        transport_response=LOCKED_RESPONSE, request_objects=REQUEST_WITH_OBJECTS
    )
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_HELD",
        _ddl("ZC_HELD"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
    )

    assert result.ok, result.error
    assert result.transport == "DEVK900851"
    step = next(s for s in result.steps if s.name == "transport check")
    assert "already held by DEVK900851" in step.detail


def test_a_different_request_than_the_holder_is_refused(monkeypatch):
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(transport_response=LOCKED_RESPONSE)
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_HELD",
        _ddl("ZC_HELD"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900999",
    )

    assert not result.ok
    assert "already held by DEVK900851" in result.error
    assert "including after it was deleted" in result.remedy
    assert "create-ddl-source" not in session.calls


def test_an_unreadable_cts_answer_stops_the_write(monkeypatch):
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(transport_response="<html>503</html>")
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session,
        "ZC_UNREADABLE",
        _ddl("ZC_UNREADABLE"),
        package="ZDSP_EXTRACTION",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900123",
    )

    assert not result.ok
    assert "could not establish" in result.error
    assert "create-ddl-source" not in session.calls


def test_the_local_package_still_needs_nothing(monkeypatch):
    """The default path must not have acquired a new way to fail."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(session, "ZC_LOCAL", _ddl("ZC_LOCAL"), activate=False)

    assert result.ok, result.error
    assert result.transport == ""
    assert "no request needed" in result.render()


def _pkg_response(package: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources" '
        'xmlns:adtcore="http://www.sap.com/adt/core" adtcore:name="ZC_X">'
        f'<adtcore:packageRef adtcore:name="{package}"/></ddl:ddlSource>'
    )


class _DeleteSession(ScriptedSession):
    """A ScriptedSession whose object already exists, in a named package."""

    def __init__(self, package: str, **kw) -> None:
        super().__init__(**kw)
        self.package = package
        self.exists = True

    def get(self, path, **kw):
        if kw.get("action") == "read-object":
            self.calls.append("read-object")
            return FakeResponse(_pkg_response(self.package))
        return super().get(path, **kw)


def test_deleting_from_a_transportable_package_needs_a_request(monkeypatch):
    """CTS says no request is needed for a deletion. CTS is wrong.

    Measured: asked about DDLS ZCDCF_TRTEST in ZDSP_EXTRACTION with
    OPERATION=D, the check answers RECORDING empty — and the delete then fails
    with ``ExceptionParameterNotFound: Parameter corrNr could not be found``
    *after* the lock has been taken. So a deletion outside a local package
    always needs one, whatever the check reports.
    """
    from cdcforge.connect.writer import delete_view

    # RECORDING empty, exactly as CTS answers for a deletion.
    deletion_check = TRANSPORTABLE_RESPONSE.replace(
        "<RECORDING>X</RECORDING>", "<RECORDING/>"
    )
    session = _DeleteSession("ZDSP_EXTRACTION", transport_response=deletion_check)

    result = delete_view(
        session, "ZC_X", policy=WritePolicy(allow_transportable=True)
    )

    assert not result.deleted
    assert "needs a transport request" in result.error
    assert "corrNr" in result.remedy
    assert "delete-ddl-source" not in session.calls, "nothing may be locked or sent"


def test_deleting_from_a_transportable_package_records_the_deletion(monkeypatch):
    from cdcforge.connect.writer import delete_view

    deletion_check = TRANSPORTABLE_RESPONSE.replace(
        "<RECORDING>X</RECORDING>", "<RECORDING/>"
    )
    session = _DeleteSession(
        "ZDSP_EXTRACTION",
        transport_response=deletion_check,
        request_objects=REQUEST_WITH_OBJECTS,
    )
    sent: list[dict] = []
    original = session.delete
    session.delete = lambda path, **kw: (sent.append(kw), original(path, **kw))[1]

    result = delete_view(
        session,
        "ZC_X",
        policy=WritePolicy(allow_transportable=True),
        transport="DEVK900851",
    )

    assert result.deleted, result.error
    assert sent[0]["params"]["corrNr"] == "DEVK900851"


def test_deleting_from_the_local_package_still_needs_nothing():
    from cdcforge.connect.writer import delete_view

    session = _DeleteSession("$TMP")
    result = delete_view(session, "ZC_X")

    assert result.deleted, result.error
    assert "transport-check" not in session.calls


def test_a_failed_delete_reports_what_sap_said():
    """"returned HTTP 400" is not something anyone can act on.

    ADT puts the reason in the response body, which AdtError carries as its
    remedy — dropping it is how "Parameter corrNr could not be found" stayed
    invisible through two failed runs.
    """
    from cdcforge.connect.session import AdtError
    from cdcforge.connect.writer import delete_view

    session = _DeleteSession("$TMP", fail_on="delete-ddl-source")

    def _boom(path, **kw):
        session.calls.append("delete-ddl-source")
        raise AdtError(
            "returned HTTP 400", remedy="Parameter corrNr could not be found."
        )

    session.delete = _boom
    result = delete_view(session, "ZC_X")

    assert not result.deleted
    assert "corrNr" in result.remedy


def test_a_failed_activation_rolls_the_object_back(monkeypatch):
    """The object exists by the time activation runs, so failing there must not
    leave it behind."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(fail_on="activate")
    monkeypatch.setattr(
        writer_module, "run_checkrun", lambda *a, **k: _CleanCheck()
    )

    result = create_view(session, "ZC_ROLLBACK", _ddl("ZC_ROLLBACK"))

    assert not result.ok
    assert result.rolled_back, "the created object must be deleted again"
    assert not result.orphaned
    assert "delete-ddl-source" in session.calls
    assert "unlock" in session.calls


def test_a_rejected_syntax_check_aborts_before_activation(monkeypatch):
    """Nothing should be activated once SAP has said the source is wrong."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(
        writer_module, "run_checkrun", lambda *a, **k: _RejectingCheck()
    )

    result = create_view(session, "ZC_BADSYNTAX", _ddl("ZC_BADSYNTAX"))

    assert not result.ok
    assert "syntax check rejected" in result.error
    assert "activate" not in session.calls, "must not activate a rejected source"
    assert result.rolled_back


def test_the_lock_is_released_before_activation(monkeypatch):
    """ADT answers 403 to an activation attempted while the caller holds the
    lock, and the generic translation of that status reads as a missing
    authorisation. Measured on a real system: same object, same user, same
    payload — locked 403, unlocked 200 with activationExecuted="true".
    """
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(session, "ZC_ORDER", _ddl("ZC_ORDER"))

    assert result.activated, result.error
    assert "unlock" in session.calls and "activate" in session.calls
    assert session.calls.index("unlock") < session.calls.index("activate"), (
        "activation must come after the unlock, or ADT refuses it"
    )


def test_a_failed_activation_relocks_to_delete(monkeypatch):
    """By the time activation runs the lock has been released, so the rollback
    has nothing to delete with and has to take a fresh one."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(fail_on="activate")
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(session, "ZC_RELOCK", _ddl("ZC_RELOCK"))

    assert not result.ok
    assert result.rolled_back, "the object must not be left behind"
    assert not result.orphaned
    assert session.calls.count("lock") == 2, "one to write, one to delete"


def test_leaving_the_object_inactive_is_a_success_not_a_failure(monkeypatch):
    """Activation needs an authorisation that writing the source does not.

    On a system where the user lacks it there is no reason to throw the work
    away — but judging the run by `activated` alone made a deliberate hand-over
    read as a broken pipeline.
    """
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = create_view(
        session, "ZC_INACTIVE", _ddl("ZC_INACTIVE"), activate=False
    )

    assert result.ok
    assert result.left_inactive
    assert not result.activated
    assert not result.rolled_back, "an inactive object is the requested outcome"
    assert "activate" not in session.calls
    assert "delete-ddl-source" not in session.calls
    assert "unlock" in session.calls

    rendered = result.render()
    assert "CREATED, INACTIVE" in rendered
    assert "Activate it in Eclipse" in rendered


def test_a_rejected_source_is_rolled_back_even_when_not_activating(monkeypatch):
    """Leaving broken DDL behind helps nobody, whatever the mode."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession()
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _RejectingCheck())

    result = create_view(
        session, "ZC_BADINACTIVE", _ddl("ZC_BADINACTIVE"), activate=False
    )

    assert not result.ok
    assert result.rolled_back
    assert not result.left_inactive


def test_the_check_runs_against_the_inactive_version(monkeypatch):
    """A freshly created object has no active version.

    Asking for `active` gave the system nothing to look at, so the check
    silently did not run — which reads exactly like a clean result.
    """
    import cdcforge.connect.writer as writer_module

    asked: dict = {}

    def spy(session, name, *, version="active"):
        asked["version"] = version
        return _CleanCheck()

    monkeypatch.setattr(writer_module, "run_checkrun", spy)
    create_view(ScriptedSession(), "ZC_VERSION", _ddl("ZC_VERSION"))
    assert asked["version"] == "inactive"


def test_an_unverified_source_says_so_when_nothing_will_activate_it(monkeypatch):
    import cdcforge.connect.writer as writer_module

    class _CannotRun:
        ran = False
        clean = False
        summary = "checkrun did not run"

    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CannotRun())
    result = create_view(
        ScriptedSession(), "ZC_UNVERIFIED", _ddl("ZC_UNVERIFIED"), activate=False
    )

    assert result.ok, "an unrunnable check is not a reason to throw the work away"
    assert "UNVERIFIED" in result.render()


def test_a_failed_rollback_is_reported_as_an_orphan(monkeypatch):
    """Then something really is left in the system and the user needs its name."""
    import cdcforge.connect.writer as writer_module

    session = ScriptedSession(fail_on="delete-ddl-source")
    monkeypatch.setattr(
        writer_module, "run_checkrun", lambda *a, **k: _RejectingCheck()
    )

    result = create_view(session, "ZC_ORPHAN", _ddl("ZC_ORPHAN"))

    assert result.orphaned
    assert "ZC_ORPHAN" in result.render()
    assert "still exists" in result.render()


class _CleanCheck:
    ran = True
    clean = True
    summary = "clean"


class _RejectingCheck:
    ran = True
    clean = False
    summary = "1 error: field UNKNOWN is not defined"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_activating_an_existing_object_does_not_claim_to_have_created_it(monkeypatch):
    """`cdc-forge activate ZI_KNA1` printed CREATED, which it had not."""
    import cdcforge.connect.writer as writer_module
    from cdcforge.connect.writer import activate_view

    session = ScriptedSession()
    session.exists = True
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = activate_view(session, "ZC_EXISTING")

    assert result.ok and result.activated
    assert "ACTIVATED" in result.render()
    assert "CREATED" not in result.render()
    assert "create-ddl-source" not in session.calls


def test_a_failed_activation_of_an_existing_object_leaves_it_alone(monkeypatch):
    """Unlike a failed create, there is nothing to roll back — the object was
    already there and is still recoverable."""
    import cdcforge.connect.writer as writer_module
    from cdcforge.connect.writer import activate_view

    session = ScriptedSession(fail_on="activate")
    session.exists = True
    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _CleanCheck())

    result = activate_view(session, "ZC_STILL_THERE")

    assert not result.ok
    assert not result.deleted and not result.rolled_back
    assert "delete-ddl-source" not in session.calls
    assert "still exists and is still inactive" in result.remedy


def test_a_successful_delete_reports_success():
    """A working `drop` printed FAILED.

    `ok` only knew about activation, and delete_view set `rolled_back` — which
    means a *create* failed and was cleaned up. Both remove an object; only one
    of them is a success.
    """
    from cdcforge.connect.writer import delete_view

    session = ScriptedSession()
    session.exists = True
    result = delete_view(session, "ZC_GONE")

    assert result.deleted
    assert result.ok
    assert not result.rolled_back, "a deliberate delete is not a rollback"
    assert "DELETED" in result.render()
    assert "FAILED" not in result.render()


def test_a_rollback_is_not_reported_as_a_successful_delete(monkeypatch):
    import cdcforge.connect.writer as writer_module

    monkeypatch.setattr(writer_module, "run_checkrun", lambda *a, **k: _RejectingCheck())
    result = create_view(ScriptedSession(), "ZC_BAD", _ddl("ZC_BAD"))

    assert result.rolled_back
    assert not result.deleted
    assert not result.ok
    assert "FAILED" in result.render()


def test_an_orphan_is_named_loudly():
    """If rollback fails there really is something left behind, and the user
    needs its name — not a generic failure."""
    result = WriteResult(object_name="ZC_X", package="$TMP", orphaned=True)
    rendered = result.render()
    assert "ZC_X" in rendered
    assert "still exists" in rendered
    assert "$TMP" in rendered


def test_steps_that_never_ran_are_distinguishable_from_failures():
    result = WriteResult(object_name="ZC_X", package="$TMP")
    created = result.step("create")
    created.ran, created.ok = True, True
    result.step("activate")  # never ran

    steps = [line for line in result.render().splitlines() if line.startswith("  ")]
    assert any("ok   create" in line for line in steps)
    assert any("-    activate" in line for line in steps)
    assert not any("FAIL" in line for line in steps), (
        "a step that never ran must not read as a failed one"
    )
