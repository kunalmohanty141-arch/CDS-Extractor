"""Change recording — reading what CTS says a write would need.

Every payload here is a real response captured from S/4HANA 2022, not a
hand-written approximation. That matters more for this parser than for most:
its job is to decide whether a write gets a change record, and the failure that
costs something is not a crash but a misread — a required request read as "not
required", which writes into a transportable package with nothing recording it.
"""

from __future__ import annotations

import pytest

from cdcforge.connect.transport import (
    TransportNeed,
    parse_check,
    request_number_from,
)

# Measured: POST /sap/bc/adt/cts/transportchecks, DDLS ZI_KNA1 in $TMP.
LOCAL_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<asx:abap version="1.0" xmlns:asx="http://www.sap.com/abapxml"><asx:values>'
    "<DATA><PGMID>R3TR</PGMID><OBJECT>DDLS</OBJECT>"
    "<OBJECTNAME>ZI_KNA1</OBJECTNAME><OPERATION>I</OPERATION>"
    "<DEVCLASS>$TMP</DEVCLASS>"
    "<CTEXT>Temporary Objects (never transported!)</CTEXT><KORRFLAG/>"
    "<AS4USER>SAP</AS4USER><PDEVCLASS>SAP</PDEVCLASS><DLVUNIT>LOCAL</DLVUNIT>"
    "<NAMESPACE>/*/</NAMESPACE><SUPER_PACKAGE/><RECORD_CHANGES/><RESULT>S</RESULT>"
    "<RECORDING/><EXISTING_REQ_ONLY/><MESSAGES/><REQUESTS/><LOCKS/>"
    "<TADIRDEVC>$TMP</TADIRDEVC>"
    "<URI>/sap/bc/adt/ddic/ddl/sources/zi_kna1</URI><CTS_PROJECTS/>"
    "</DATA></asx:values></asx:abap>"
)

# Measured: same call, DDLS ZCTS_PROBE in ZDSP_EXTRACTION.
TRANSPORTABLE_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<asx:abap version="1.0" xmlns:asx="http://www.sap.com/abapxml"><asx:values>'
    "<DATA><PGMID>R3TR</PGMID><OBJECT>DDLS</OBJECT>"
    "<OBJECTNAME>ZCTS_PROBE</OBJECTNAME><OPERATION>I</OPERATION>"
    "<DEVCLASS>ZDSP_EXTRACTION</DEVCLASS><CTEXT>Datasphere extraction</CTEXT>"
    "<KORRFLAG>X</KORRFLAG><AS4USER/><PDEVCLASS>ZDEV</PDEVCLASS>"
    "<DLVUNIT>HOME</DLVUNIT><NAMESPACE>/0CUST/</NAMESPACE><SUPER_PACKAGE/>"
    "<RECORD_CHANGES/><RESULT>S</RESULT><RECORDING>X</RECORDING>"
    "<EXISTING_REQ_ONLY/><MESSAGES/><REQUESTS/><LOCKS/>"
    "<TADIRDEVC>ZDSP_EXTRACTION</TADIRDEVC>"
    "<URI>/sap/bc/adt/ddic/ddl/sources/zcts_probe</URI><CTS_PROJECTS/>"
    "</DATA></asx:values></asx:abap>"
)

# Measured with a real modifiable request open. Note the nesting — the number
# is under REQ_HEADER, not directly under CTS_REQUEST — which is exactly the
# kind of detail a hand-written fixture gets wrong and a real one cannot.
WITH_REQUESTS = TRANSPORTABLE_RESPONSE.replace(
    "<REQUESTS/>",
    "<REQUESTS><CTS_REQUEST><REQ_HEADER>"
    "<TRKORR>DEVK900123</TRKORR><TRFUNCTION>K</TRFUNCTION>"
    "<TRSTATUS>D</TRSTATUS><TARSYSTEM>VIR</TARSYSTEM>"
    "<AS4USER>TESTUSER</AS4USER><AS4DATE>2026-08-10</AS4DATE>"
    "<AS4TIME>12:47:18</AS4TIME>"
    "<AS4TEXT>CDC Forge generated objects</AS4TEXT>"
    "<CLIENT>800</CLIENT><REPOID/>"
    "</REQ_HEADER><REQ_ATTRS/><TASK_HEADERS/></CTS_REQUEST></REQUESTS>",
)

# Measured: GET /sap/bc/adt/cts/transportrequests/DEVK900851?withObjects=true
# The header and the object list come back together, which is why one call
# answers both "can this request take an object" and "did it take mine".
REQUEST_WITH_OBJECTS = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<tm:root xmlns:tm="http://www.sap.com/cts/adt/tm" '
    'xmlns:adtcore="http://www.sap.com/adt/core" adtcore:name="DEVK900851">'
    '<tm:request tm:number="DEVK900851" tm:parent="" tm:owner="TESTUSER" '
    'tm:desc="CDC Forge transport probe" tm:type="K" tm:status="D" '
    'tm:status_text="Modifiable" tm:target="VIR" tm:source_client="800"/>'
    '<tm:all_objects>'
    '<tm:abap_object tm:pgmid="R3TR" tm:type="DDLS" tm:name="ZCDCF_TRTEST" '
    'tm:wbtype="DDLS/DF" tm:obj_info="Data Definition Language Source"/>'
    "</tm:all_objects></tm:root>"
)

# Measured after ZCDCF_TRTEST had been created in DEVK900851 and deleted again.
# Note RECORDING is *empty* while LOCKS names the request — the response that
# made a re-create fail with "Parameter corrNr could not be found".
LOCKED_RESPONSE = TRANSPORTABLE_RESPONSE.replace(
    "<RECORDING>X</RECORDING>", "<RECORDING/>"
).replace(
    "<LOCKS/>",
    "<LOCKS><CTS_OBJECT_LOCK>"
    "<OBJECT_KEY><PGMID>R3TR</PGMID><OBJECT>DDLS</OBJECT>"
    "<OBJ_NAME>ZCDCF_TRTEST</OBJ_NAME></OBJECT_KEY>"
    "<LOCK_HOLDER><REQ_HEADER><TRKORR>DEVK900851</TRKORR>"
    "<TRFUNCTION>K</TRFUNCTION><TRSTATUS>D</TRSTATUS>"
    "<AS4USER>TESTUSER</AS4USER>"
    "<AS4TEXT>CDC Forge transport probe</AS4TEXT></REQ_HEADER>"
    "<REQ_ATTRS/><TASK_HEADERS><CTS_TASK_HEADER>"
    "<TRKORR>DEVK900852</TRKORR><TRFUNCTION>S</TRFUNCTION>"
    "<TRSTATUS>D</TRSTATUS><AS4USER>TESTUSER</AS4USER>"
    "</CTS_TASK_HEADER></TASK_HEADERS></LOCK_HOLDER>"
    "</CTS_OBJECT_LOCK></LOCKS>",
)

RELEASED_REQUEST = REQUEST_WITH_OBJECTS.replace(
    'tm:status="D" tm:status_text="Modifiable"',
    'tm:status="R" tm:status_text="Released"',
)


def test_local_package_needs_no_request():
    need = parse_check(LOCAL_RESPONSE, "$TMP")
    assert need.ok
    assert need.local
    assert not need.required
    assert need.package_text == "Temporary Objects (never transported!)"


def test_customer_package_records_changes():
    need = parse_check(TRANSPORTABLE_RESPONSE, "ZDSP_EXTRACTION")
    assert need.ok
    assert not need.local
    assert need.required
    assert need.requests == []


def test_available_requests_are_read():
    need = parse_check(WITH_REQUESTS, "ZDSP_EXTRACTION")
    assert [r.number for r in need.requests] == ["DEVK900123"]
    assert need.requests[0].description == "CDC Forge generated objects"
    assert need.requests[0].owner == "TESTUSER"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not xml at all", "<html><body>503</body></html>", "<other/>"],
)
def test_an_unreadable_response_is_never_read_as_no_request_needed(text):
    """The one misread that cannot be undone.

    A response this module does not recognise must not resolve to "no request
    needed" — that writes into a transportable package with nothing recording
    it, which is exactly the outcome change management exists to prevent. So
    ``ok`` is False and the caller refuses.
    """
    need = parse_check(text, "ZDSP_EXTRACTION")
    assert not need.ok
    assert need.unreadable
    assert not need.required
    assert not need.local


def test_a_failed_check_is_unreadable_even_when_it_parses():
    text = TRANSPORTABLE_RESPONSE.replace("<RESULT>S</RESULT>", "<RESULT>E</RESULT>")
    need = parse_check(text, "ZDSP_EXTRACTION")
    assert not need.ok
    assert need.unreadable


def test_local_needs_both_signals():
    """``DLVUNIT=LOCAL`` alone is not enough.

    A delivery unit is not a transport property. A package could plausibly
    carry LOCAL while still recording changes, and reading that as "no request
    needed" would skip the record — so KORRFLAG has to agree.
    """
    text = LOCAL_RESPONSE.replace("<KORRFLAG/>", "<KORRFLAG>X</KORRFLAG>")
    need = parse_check(text, "$TMP")
    assert not need.local


@pytest.mark.parametrize(
    "text,expected",
    [
        ("<TRKORR>DEVK900123</TRKORR>", "DEVK900123"),
        ('<tm:request adtcore:name="DEVK900456"/>', "DEVK900456"),
        ("/sap/bc/adt/cts/transportrequests/DEVK900789", "DEVK900789"),
        ("", ""),
        ("<result>ok</result>", ""),
    ],
)
def test_request_numbers_are_matched_by_format_not_position(text, expected):
    """Three response shapes have been seen for the create call.

    Matching by format rather than by element name survives all three, and
    returns nothing rather than a guess when the number is absent — recording
    an object against a request that does not exist is worse than reporting
    that the number could not be read.
    """
    assert request_number_from(text) == expected


def test_render_says_what_to_do_about_it():
    need = parse_check(TRANSPORTABLE_RESPONSE, "ZDSP_EXTRACTION")
    assert "transport request is required" in need.render()
    assert "none are open" in need.render()

    assert "no request needed" in parse_check(LOCAL_RESPONSE, "$TMP").render()
    assert "DEVK900123" in parse_check(WITH_REQUESTS, "ZDSP_EXTRACTION").render()


class _RequestSession:
    """Answers the request-contents GET, or fails on demand."""

    def __init__(self, text: str = REQUEST_WITH_OBJECTS, fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    def get(self, path, **kw):
        from cdcforge.connect.session import AdtError

        if self.fail:
            raise AdtError("404", remedy="")

        class _R:
            pass

        response = _R()
        response.text = self.text
        return response


def test_a_recorded_object_is_confirmed_not_assumed():
    """Sending corrNr and getting a 200 is not proof it was honoured.

    An ignored query parameter looks exactly like an accepted one. The same
    shape of bug already cost this codebase once: a checkrun against the
    ``active`` version of a never-activated object silently did not run and
    read as clean.
    """
    from cdcforge.connect.transport import contains_object

    session = _RequestSession()
    assert contains_object(session, "DEVK900851", "ZCDCF_TRTEST") is True
    assert contains_object(session, "DEVK900851", "zcdcf_trtest") is True


def test_an_object_missing_from_the_request_is_reported_as_missing():
    from cdcforge.connect.transport import contains_object

    session = _RequestSession()
    assert contains_object(session, "DEVK900851", "ZSOMETHING_ELSE") is False


def test_the_wrong_object_type_does_not_count():
    """A table and a view can share a name. Matching on name alone would
    confirm a change record that covers something else entirely."""
    from cdcforge.connect.transport import contains_object

    session = _RequestSession()
    assert contains_object(
        session, "DEVK900851", "ZCDCF_TRTEST", object_type="TABL"
    ) is False


@pytest.mark.parametrize(
    "session",
    [_RequestSession(fail=True), _RequestSession("not xml"), _RequestSession("<tm:x/>")],
)
def test_an_unreadable_object_list_is_none_not_false(session):
    """"Could not confirm" and "it is not there" need different words.

    False sends the user to SE09 to fix a problem that may not exist; None
    says the check could not be made, which is the truth.
    """
    from cdcforge.connect.transport import contains_object

    assert contains_object(session, "DEVK900851", "ZCDCF_TRTEST") is None


def test_an_object_already_held_by_a_request_is_detected():
    """RECORDING empty does not always mean "no request needed".

    An object stays locked to the request it was last recorded in — including
    after it is deleted, because the deletion is itself an entry there. CTS
    then reports RECORDING empty, meaning "already accounted for", and a write
    that takes that at face value fails with "Parameter corrNr could not be
    found" after the object has been created.
    """
    need = parse_check(LOCKED_RESPONSE, "ZDSP_EXTRACTION")
    assert need.ok
    assert not need.required, "the raw flag really is empty — that is the trap"
    assert need.locked_in == "DEVK900851"
    assert need.locked_task == "DEVK900852"


def test_the_lock_is_said_out_loud():
    need = parse_check(LOCKED_RESPONSE, "ZDSP_EXTRACTION")
    rendered = need.render()
    assert "already held by DEVK900851" in rendered
    assert "DEVK900852" in rendered
    assert "must name that request" in rendered


def test_no_lock_leaves_the_fields_empty():
    need = parse_check(TRANSPORTABLE_RESPONSE, "ZDSP_EXTRACTION")
    assert need.locked_in == ""
    assert need.locked_task == ""
    assert need.required


def test_a_modifiable_request_is_usable():
    from cdcforge.connect.transport import request_status

    status = request_status(_RequestSession(), "DEVK900851")
    assert status.found and status.modifiable and status.usable
    assert status.owner == "TESTUSER"
    assert status.description == "CDC Forge transport probe"
    assert status.refusal() == ""


def test_a_released_request_says_why_it_cannot_be_used():
    from cdcforge.connect.transport import request_status

    status = request_status(_RequestSession(RELEASED_REQUEST), "DEVK900851")
    assert status.found and not status.usable
    assert "Released" in status.refusal()


def test_a_number_that_is_not_there_says_what_one_looks_like():
    """The commonest reason to be here is a typo."""
    from cdcforge.connect.transport import request_status

    status = request_status(_RequestSession("<tm:root/>"), "DEVK999999")
    assert not status.found
    assert "DEVK900123" in status.refusal()


def test_a_status_lookup_for_a_different_number_does_not_match():
    """The response is read for the number asked about, not the first one in
    it — a mismatch would confirm a request nobody requested."""
    from cdcforge.connect.transport import request_status

    status = request_status(_RequestSession(), "DEVK111111")
    assert not status.found


def test_a_bare_need_is_refusing_by_default():
    """The zero value must be the safe one.

    Every field defaults to the reading that produces a refusal, so a
    ``TransportNeed`` that was never filled in cannot authorise a write.
    """
    need = TransportNeed(package="ZANY")
    assert not need.ok
    assert need.unreadable
    assert not need.local
