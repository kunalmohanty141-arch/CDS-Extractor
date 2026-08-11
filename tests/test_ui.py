"""The UI, actually rendered.

Every other test in this suite calls the app's functions directly, which proves
the logic and proves nothing about the page. A Streamlit script fails at
*render* time — a bad widget argument, a key that is not in session state, a
`format_func` over an empty list — and none of that shows up until someone
opens the tab.

That gap is not hypothetical. The transport panel, the plan upload and the
package radio were all written and "tested" by calling their functions, which
cannot execute a single `st.radio`.

`AppTest` runs the real script. `at.exception` is the assertion that matters:
anything raised during a render lands there instead of a traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "src" / "cdcforge" / "ui" / "app.py")

STEPS = ["1 · Connect", "2 · Analyse", "3 · Decide", "4 · Report"]


def _app(**state) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    for key, value in state.items():
        at.session_state[key] = value
    return at


def _assert_clean(at: AppTest) -> None:
    assert not at.exception, "\n".join(str(e) for e in at.exception)


def test_the_app_renders_before_anything_is_connected():
    _assert_clean(_app().run())


def test_every_step_renders_without_a_connection():
    """A user can click any step in the sidebar at any time.

    Steps that need data must say so rather than raising — the state they read
    is empty on a fresh session, and half of it is empty again after a reset.
    """
    for step in STEPS:
        at = _app(step=step, connected=True, mode="live").run()
        _assert_clean(at)


# ---------------------------------------------------------------------------
# The create panel — the newest code, and the code that writes
# ---------------------------------------------------------------------------


def _ready_to_create(**extra):
    # The create panel lives on the Report step, *after* the results table —
    # and that step returns early when nothing has been analysed, so there has
    # to be a result for the panel to be reached at all.
    from cdcforge.model import Assessment
    from tests.test_estate import FakeSource

    state = {
        "step": STEPS[3],
        "connected": True,
        "mode": "live",
        "productive": False,
        "system_label": "DEV · 800",
        "generated": {"ZI_THING": "define view entity ZI_THING as select from t000 {}"},
        "assessments": {"I_BUSINESSAREA": Assessment(object_name="I_BUSINESSAREA")},
        "table_targets": [],
        "successors": {},
        # The estate panel needs a metadata source to be worth rendering, and
        # the workbook builder surveys it.
        "metadata": FakeSource(views={}, tables=()),
    }
    state.update(extra)
    return _app(**state)


def test_the_create_panel_renders_with_something_to_create():
    at = _ready_to_create().run()
    _assert_clean(at)


def test_the_target_choice_is_offered_and_defaults_to_local():
    """$TMP is the default because a mistake there leaves no trace and cannot
    travel to another system."""
    at = _ready_to_create().run()
    _assert_clean(at)
    targets = [r for r in at.radio if "Where should these objects go" in r.label]
    assert targets, "the user must be asked where the objects go"
    assert "$TMP" in targets[0].value


def test_choosing_a_transportable_package_asks_for_one():
    """And must not fall over before a package has been typed — the panel
    re-renders on every keystroke, most of them with the box still empty."""
    at = _ready_to_create().run()
    target = next(r for r in at.radio if "Where should these objects go" in r.label)
    at = target.set_value("A package with a transport request").run()
    _assert_clean(at)
    assert any("Package" in i.label for i in at.text_input)


def test_a_productive_client_is_refused_in_the_panel_not_only_in_the_writer():
    at = _ready_to_create(productive=True, role_label="Production").run()
    _assert_clean(at)
    assert any("productive" in e.value.lower() for e in at.error)


def test_sample_mode_says_there_is_nothing_to_write_to():
    at = _ready_to_create(mode="sample").run()
    _assert_clean(at)
    assert any("no system to write to" in i.value for i in at.info)


def test_nothing_generated_means_nothing_to_create():
    at = _ready_to_create(generated={}).run()
    _assert_clean(at)


# ---------------------------------------------------------------------------
# Picking a transport request — the panel with the most new widgets in it
# ---------------------------------------------------------------------------


class _FakeSession:
    """Answers the two reads the transport panel makes, and nothing else."""

    def __init__(self, check: str, request: str = "") -> None:
        self.check = check
        self.request = request

    def post(self, path, **kw):
        return type("R", (), {"text": self.check})()

    def get(self, path, **kw):
        from cdcforge.connect.session import AdtError

        if not self.request:
            raise AdtError("404", remedy="")
        return type("R", (), {"text": self.request})()


def _transport_panel(check: str, request: str = "", package: str = "ZDSP_EXTRACTION"):
    """Render the create panel with a transportable package chosen."""
    at = _ready_to_create(session=_FakeSession(check, request)).run()
    target = next(r for r in at.radio if "Where should these objects go" in r.label)
    at = target.set_value("A package with a transport request").run()
    box = next(i for i in at.text_input if i.label == "Package")
    return box.set_value(package).run()


def test_a_package_needing_a_request_offers_all_three_ways_to_name_one():
    from tests.test_transport import TRANSPORTABLE_RESPONSE

    at = _transport_panel(TRANSPORTABLE_RESPONSE)
    _assert_clean(at)
    picker = next(r for r in at.radio if r.label == "Transport request")
    # No open requests in this response, so picking one is not offered.
    assert "Enter a request number" in picker.options
    assert "Create a new request" in picker.options
    assert "Use one of my open requests" not in picker.options


def test_an_offered_request_can_be_picked():
    from tests.test_transport import WITH_REQUESTS

    at = _transport_panel(WITH_REQUESTS)
    _assert_clean(at)
    picker = next(r for r in at.radio if r.label == "Transport request")
    assert picker.options[0] == "Use one of my open requests"
    assert any("DEVK900123" in str(s.value) for s in at.selectbox)


def test_typing_a_request_number_checks_it_and_says_it_is_good():
    from tests.test_transport import REQUEST_WITH_OBJECTS, TRANSPORTABLE_RESPONSE

    at = _transport_panel(TRANSPORTABLE_RESPONSE, REQUEST_WITH_OBJECTS)
    picker = next(r for r in at.radio if r.label == "Transport request")
    at = picker.set_value("Enter a request number").run()
    _assert_clean(at)

    box = next(i for i in at.text_input if i.label == "Request number")
    at = box.set_value("DEVK900851").run()
    _assert_clean(at)
    assert any("DEVK900851" in s.value for s in at.success)
    assert any("TESTUSER" in s.value for s in at.success)


def test_typing_a_request_number_that_does_not_exist_says_so_before_committing():
    from tests.test_transport import TRANSPORTABLE_RESPONSE

    at = _transport_panel(TRANSPORTABLE_RESPONSE)  # no request response = 404
    picker = next(r for r in at.radio if r.label == "Transport request")
    at = picker.set_value("Enter a request number").run()
    box = next(i for i in at.text_input if i.label == "Request number")
    at = box.set_value("DEVK999999").run()

    _assert_clean(at)
    assert any("was not found" in e.value for e in at.error)


def test_an_empty_request_box_does_not_error_on_every_keystroke():
    """The panel re-renders as the user types, most often with nothing in it."""
    from tests.test_transport import TRANSPORTABLE_RESPONSE

    at = _transport_panel(TRANSPORTABLE_RESPONSE)
    picker = next(r for r in at.radio if r.label == "Transport request")
    _assert_clean(picker.set_value("Enter a request number").run())


def test_an_object_already_held_by_a_request_needs_no_choice_at_all():
    """CTS reports no recording required and names the holder; the panel has
    nothing to ask and must not ask it."""
    from tests.test_transport import LOCKED_RESPONSE

    at = _transport_panel(LOCKED_RESPONSE)
    _assert_clean(at)
    assert not [r for r in at.radio if r.label == "Transport request"]


def test_an_unreadable_cts_answer_refuses_in_the_panel():
    at = _transport_panel("<html>503</html>")
    _assert_clean(at)
    assert any("Could not establish" in e.value for e in at.error)


# ---------------------------------------------------------------------------
# The report step — the decision sheet download and upload
# ---------------------------------------------------------------------------


def test_the_report_step_renders_with_results():
    from cdcforge.model import Assessment

    at = _app(
        step=STEPS[3],
        connected=True,
        mode="live",
        assessments={"I_BUSINESSAREA": Assessment(object_name="I_BUSINESSAREA")},
        table_targets=["VBAP"],
        successors={},
    ).run()
    _assert_clean(at)


def test_the_decision_sheet_download_is_offered():
    from cdcforge.model import Assessment

    at = _app(
        step=STEPS[3],
        connected=True,
        mode="live",
        assessments={"I_BUSINESSAREA": Assessment(object_name="I_BUSINESSAREA")},
        table_targets=[],
        successors={},
    ).run()
    _assert_clean(at)
    labels = [b.label for b in at.button] + [
        getattr(b, "label", "") for b in at.get("download_button")
    ]
    assert any("decision sheet" in label.lower() for label in labels)


def test_the_estate_panel_offers_both_survey_buttons():
    """Both were CLI-only. A feature that exists but not where the user works
    is a feature they do not have."""
    at = _ready_to_create().run()
    _assert_clean(at)
    labels = [b.label for b in at.button]
    assert "Survey what exists" in labels
    assert "Survey and verify each one" in labels


def test_the_estate_panel_renders_results():
    from cdcforge.estate import Estate, ExistingObject
    from cdcforge.verify import Verification

    at = _ready_to_create(
        estate=Estate(
            objects=[
                ExistingObject(
                    "ZW_ACCTGDOC", base="I_ACCOUNTINGDOCUMENT",
                    root_table="BKPF", declares_cdc=True,
                )
            ],
            surveyed=1,
        ),
        estate_checks={
            "ZW_ACCTGDOC": Verification(
                name="ZW_ACCTGDOC", exists=True, delta_supported=True
            )
        },
    ).run()
    _assert_clean(at)
    assert any("still carrying delta" in s.value for s in at.success)


def test_the_estate_panel_says_loudly_when_something_stopped_working():
    from cdcforge.estate import Estate, ExistingObject
    from cdcforge.verify import Verification

    at = _ready_to_create(
        estate=Estate(
            objects=[ExistingObject("ZW_DEAD", root_table="BKPF")], surveyed=1
        ),
        estate_checks={
            "ZW_DEAD": Verification(name="ZW_DEAD", exists=False)
        },
    ).run()
    _assert_clean(at)
    assert any("not currently carrying delta" in e.value for e in at.error)


def test_an_unanswerable_survey_is_not_read_as_nothing_built():
    from cdcforge.estate import Estate

    at = _ready_to_create(estate=Estate(surveyed=0)).run()
    _assert_clean(at)
    assert any("not the same as nothing being there" in w.value for w in at.warning)


def test_the_stylesheet_defines_a_dark_palette_and_fetches_nothing():
    """Two things a client's security reviewer would ask about.

    The first cut hard-coded light colours, so anyone running the dark theme
    got dark chrome around white panels. And a webfont import would be an
    outbound request from a tool that is otherwise entirely local — blocked in
    plenty of corporate networks, and a needless thing to have to explain.
    """
    from cdcforge.ui import theme

    assert "prefers-color-scheme: dark" in theme.CSS
    assert "--ink:" in theme.CSS, "colours come from tokens, not literals"
    assert "@import" not in theme.CSS
    assert "http://" not in theme.CSS and "https://" not in theme.CSS


def test_the_stepper_marks_where_you_are():
    from cdcforge.ui import theme

    html = theme.stepper(STEPS, STEPS[2])
    assert html.count("cdc-step") == len(STEPS) + 1  # +1 for the container
    assert "done" in html and "on" in html


def test_the_report_step_renders_with_nothing_analysed():
    at = _app(step=STEPS[3], connected=True, mode="live").run()
    _assert_clean(at)
