"""CDC Forge — Streamlit UI.

Four steps: Connect, Analyse, Decide, Report.

Deliberately not the nine-step stepper of §7. The specification's nine steps are
a description of the work, not a demand for nine screens, and a consultant
showing this to a client's Basis team needs it to be obvious in ten seconds.
The three UI rules from Appendix H.3 are what actually matter and all hold
here: a verdict always shows *why*, the system role badge is on every screen,
and operational-consequence warnings are not footnotes.

Runs against local fixtures with no system at all (F-38), which is the point —
it can be demoed to a prospect who will never grant access.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from cdcforge.connect.writer import LOCAL_PACKAGE
from cdcforge.model import Assessment, Severity, Verdict
from cdcforge.triage import Bucket, classify
from cdcforge.ui import inputs, theme
from cdcforge.ui.inputs import split_names

st.set_page_config(
    page_title="CDC Forge", page_icon="◆", layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(theme.CSS, unsafe_allow_html=True)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "fixtures"

STEPS = ["1 · Connect", "2 · Analyse", "3 · Decide", "4 · Report"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def state() -> dict:
    defaults = {
        "metadata": None,
        "session": None,
        "profile_id": "",
        # Objects this session created, name → the WriteResult's rendering.
        "created": {},
        "connected": False,
        "mode": "",
        "system_label": "",
        "role_label": "Not connected",
        "productive": True,
        "preflight": None,
        "assessments": {},
        "table_targets": [],
        "decisions": {},
        "generated": {},
        "successors": {},
        "estate": None,
        "estate_checks": {},
        "step": STEPS[0],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    return st.session_state


S = state()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def sidebar() -> None:
    with st.sidebar:
        st.markdown(theme.brand(), unsafe_allow_html=True)
        st.caption("CDS readiness for Datasphere replication")

        st.markdown(
            theme.role_badge(S["role_label"], S["productive"]),
            unsafe_allow_html=True,
        )
        if S["connected"]:
            st.caption(S["system_label"])
            st.markdown(
                theme.chip("Read-only", theme.BLUE, "#e8f0fa", bold=False),
                unsafe_allow_html=True,
            )
            st.caption(
                "This session only reads. Creating objects opens a separate "
                "write session, needs a typed confirmation, and can only reach "
                "$TMP under a Z/Y name."
            )
        st.divider()

        S["step"] = st.radio(
            "Step", STEPS,
            index=STEPS.index(S["step"]),
            label_visibility="collapsed",
            disabled=not S["connected"] and S["step"] != STEPS[0],
        )

        if S["assessments"]:
            st.divider()
            counts = tally(list(S["assessments"].values()))
            for bucket, number in counts.items():
                if number:
                    label, colour, background = theme.BUCKET_STYLE[bucket]
                    st.markdown(
                        f'{theme.chip(f"{number}", colour, background)} '
                        f'<span style="font-size:.83rem;color:{theme.MUTED}">{label}</span>',
                        unsafe_allow_html=True,
                    )


def tally(assessments: list[Assessment]) -> dict[Bucket, int]:
    counts = dict.fromkeys(Bucket, 0)
    for assessment in assessments:
        counts[classify(assessment)] += 1
    return counts


# ---------------------------------------------------------------------------
# Step 1 — Connect
# ---------------------------------------------------------------------------


def step_connect() -> None:
    st.markdown("# Connect")
    st.markdown(
        '<div class="cdc-sub">Work against a live S/4HANA system, or explore '
        "the sample landscape with no connection at all.</div>",
        unsafe_allow_html=True,
    )

    demo, live = st.tabs(["Sample data", "S/4HANA system"])

    with demo:
        st.write(
            "A local set of CDS views and tables covering every rule. Nothing "
            "is read from any system."
        )
        if st.button("Open sample landscape", type="primary", key="demo"):
            from cdcforge.metadata import MockMetadataSource

            S["metadata"] = MockMetadataSource(FIXTURES)
            S.update(
                connected=True, mode="demo", system_label="Sample data · local files",
                role_label="SAMPLE DATA", productive=False, preflight=None,
                assessments={}, decisions={}, generated={}, successors={},
                step=STEPS[1],  # nothing left to do here — go straight on
            )
            st.rerun()

    with live:
        from cdcforge.connect.profile import ConnectionProfile

        profiles = ConnectionProfile.list_profiles()
        if not profiles:
            st.info(
                "No connection profiles yet. Create one from a terminal:\n\n"
                "`cdc-forge profile add --profile DEV --host <host> "
                "--port 44310 --client 100 --user <user>`\n\n"
                "then `cdc-forge login --profile DEV` to store the password in "
                "the OS keyring."
            )
        else:
            chosen = st.selectbox("Profile", profiles)
            if st.button("Connect and run preflight", type="primary", key="live"):
                connect_live(chosen)

    if S["preflight"] is not None:
        render_preflight(S["preflight"])

    if S["connected"]:
        st.success("Connected. Move to **2 · Analyse** in the sidebar.")


def connect_live(profile_id: str) -> None:
    from cdcforge.cache import CachedMetadataSource
    from cdcforge.connect.audit import AuditLog
    from cdcforge.connect.preflight import run_preflight
    from cdcforge.connect.profile import ConnectionProfile
    from cdcforge.connect.session import AdtError, AdtSession
    from cdcforge.connect.source import AdtMetadataSource
    from cdcforge.store import Store

    store_dir = Path.home() / ".cdc-forge"
    try:
        profile = ConnectionProfile.load(profile_id)
        session = AdtSession(
            profile, AuditLog(store_dir / f"{profile_id}.sqlite"), read_only=True
        )
        with st.spinner("Connecting and checking the environment…"):
            report = run_preflight(session)
    except AdtError as exc:
        st.error(f"**{exc.cause}** — {exc.message}")
        if exc.remedy:
            st.caption(exc.remedy)
        return
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")
        return

    store = Store(store_dir / f"{profile_id}.cache.sqlite", profile_id=profile_id)
    S["metadata"] = CachedMetadataSource(AdtMetadataSource(session), store)
    S["session"] = session
    S["profile_id"] = profile_id
    S.update(
        connected=True, mode="live", preflight=report,
        system_label=f"{report.system_id or profile_id} · client {profile.client} "
                     f"· release {report.release or '?'}",
        role_label=report.client_role.label.upper(),
        productive=report.client_role.is_productive,
        assessments={}, decisions={}, generated={}, successors={},
        # Move on by itself when the environment is sound. Stay put when it is
        # not, because a blocking failure is something the user must read.
        step=STEPS[0] if report.blocking_failures else STEPS[1],
    )
    st.rerun()


def render_preflight(report) -> None:
    from cdcforge.connect.preflight import Status

    st.markdown("## Environment")
    marks = {
        Status.OK: ("✓", theme.GREEN), Status.FAILED: ("✕", theme.RED),
        Status.WARNING: ("!", theme.AMBER), Status.UNKNOWN: ("?", theme.SLATE),
    }
    for check in report.checks:
        mark, colour = marks[check.status]
        st.markdown(
            f'<div class="cdc-finding" style="border-left-color:{colour}">'
            f'<b style="color:{colour}">{mark}</b> {check.name}'
            + (f'<div class="fix">{check.detail}</div>' if check.detail else "")
            + (f'<div class="fix">→ {check.remedy}</div>'
               if check.remedy and check.status is not Status.OK else "")
            + "</div>",
            unsafe_allow_html=True,
        )
    if report.client_role.is_productive:
        st.error(
            f"Client role is **{report.client_role.label}**. Treat everything "
            f"about this system as production."
        )


# ---------------------------------------------------------------------------
# Step 2 — Analyse
# ---------------------------------------------------------------------------


def step_analyse() -> None:
    st.markdown("# Analyse")
    st.markdown(
        '<div class="cdc-sub">Give it CDS views to check, and tables that need '
        "a view building. Paste names or upload a spreadsheet.</div>",
        unsafe_allow_html=True,
    )

    views_text, tables_text = "", ""

    upload = st.file_uploader(
        "Spreadsheet — a sheet named Views and one named Tables, or first sheet "
        "views and second tables (a CSV of names works too)",
        type=["xlsx", "csv"],
    )
    if upload is not None:
        views_text, tables_text = read_upload(upload)

        # Which sheet a name arrived on is the customer's opinion; what DDIC
        # actually holds is a fact. Checking costs one cached lookup per name
        # and stops a table listed under "views" being sent to the view
        # validator, which would report it UNPARSEABLE.
        read_views, read_tables = split_names(views_text), split_names(tables_text)
        sorted_views, sorted_tables, moved, unknown = inputs.sort_by_kind(
            S["metadata"], read_views, read_tables
        )
        views_text, tables_text = "\n".join(sorted_views), "\n".join(sorted_tables)

        st.success(
            f"Read {len(sorted_views)} view(s) and {len(sorted_tables)} table(s)."
        )
        if moved:
            to_tables = [n for n, d in moved if d == "tables"]
            to_views = [n for n, d in moved if d == "views"]
            lines = []
            if to_tables:
                lines.append(
                    f"**{', '.join(to_tables)}** — listed as views, but the "
                    f"system has them as tables."
                )
            if to_views:
                lines.append(
                    f"**{', '.join(to_views)}** — listed as tables, but the "
                    f"system has them as CDS views."
                )
            st.info("Re-sorted to match the system:\n\n" + "\n\n".join(lines))
        if unknown:
            st.warning(
                f"**Not found in this client:** {', '.join(unknown)}. Left out "
                f"rather than guessed at — check the spelling, or whether they "
                f"exist here and your user can read them."
            )

    left, right = st.columns(2)
    with left:
        st.markdown("### CDS views to check")
        views_text = st.text_area(
            "One name per line", value=views_text, height=190,
            placeholder="ZI_SALESORDER_CDC\nI_GoodsMovementDocumentDEX",
            label_visibility="collapsed",
        )
        if S["mode"] == "demo" and st.button("Use all sample views"):
            views_text = "\n".join(S["metadata"].list_views())
            st.session_state["_views_prefill"] = views_text

    with right:
        st.markdown("### Tables needing a view")
        tables_text = st.text_area(
            "One name per line", value=tables_text, height=190,
            placeholder="ZCUSTORDER\nZORDERITEM",
            label_visibility="collapsed", key="tables_input",
        )

    views = split_names(st.session_state.get("_views_prefill", views_text))
    tables = split_names(tables_text)

    st.caption(
        "Every declared to-one join is checked against the table's key where "
        "that proves it outright — no data is read."
    )

    if st.button(
        f"Analyse {len(views)} view(s) and {len(tables)} table(s)",
        type="primary", disabled=not (views or tables),
    ):
        run_analysis(views, tables)


def read_upload(upload) -> tuple[str, str]:
    try:
        return inputs.read_upload(upload.name, upload.getvalue())
    except ImportError:
        st.error("Reading .xlsx needs openpyxl — `pip install openpyxl`.")
        return "", ""
    except Exception as exc:
        st.error(f"Could not read {upload.name}: {exc}")
        return "", ""


def run_analysis(views: list[str], tables: list[str]) -> None:
    from cdcforge.cardinality import structural_evidence
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext, validate_object, validate_view
    from cdcforge.successors import find_candidates

    metadata = S["metadata"]
    results: dict[str, Assessment] = {}
    successors: dict[str, object] = {}
    total = len(views) + len(tables)
    done = 0
    progress = st.progress(0.0, text="Reading and checking…")

    for name in views:
        done += 1
        progress.progress(done / max(total, 1), text=f"Checking {name}…")
        source = metadata.get_view_source(name)
        if source is None:
            results[name] = validate_object(name, metadata)
            continue

        # One context, used twice. Working out the structural cardinality
        # evidence needs the dependency stack, and so do the rules — building a
        # second context walked the whole stack again for every view.
        ctx = ValidationContext(
            view=parse_ddl(source, name_hint=name),
            metadata=metadata,
            object_meta=metadata.get_object(name),
        )
        ctx.cardinality_evidence = structural_evidence(ctx)
        results[name] = validate_view(ctx.view, context=ctx)

    # F-09 — before offering to build anything for a table, find out whether an
    # extractor for it already exists. Sometimes the right answer is to build
    # nothing at all.
    for index, table in enumerate(tables, 1):
        done += 1
        progress.progress(
            done / max(total, 1),
            text=(
                f"Searching every VDM layer over {table} "
                f"({index} of {len(tables)}) — the first look at a table reads "
                f"its whole reader graph and is the slow one; later runs are "
                f"cached."
            ),
        )
        successors[table] = find_candidates(metadata, table)

    progress.empty()
    S["assessments"] = results
    S["table_targets"] = tables
    S["successors"] = successors
    S["decisions"] = {}
    S["generated"] = {}
    S["step"] = STEPS[2]
    st.rerun()


# ---------------------------------------------------------------------------
# Step 3 — Decide
# ---------------------------------------------------------------------------


def step_decide() -> None:
    st.markdown("# Decide")
    if not S["assessments"] and not S["table_targets"]:
        st.info("Nothing analysed yet — go back to **2 · Analyse**.")
        return

    assessments = list(S["assessments"].values())
    if assessments:
        counts = tally(assessments)
        st.markdown(
            theme.tiles([
                (theme.BUCKET_STYLE[b][0], counts[b], theme.BUCKET_STYLE[b][1])
                for b in Bucket if counts[b]
            ]),
            unsafe_allow_html=True,
        )

    if S["table_targets"]:
        st.markdown(
            f'<div class="cdc-note">{len(S["table_targets"])} table(s) queued for '
            f"a new CDS view: {', '.join(S['table_targets'][:8])}"
            f"{' …' if len(S['table_targets']) > 8 else ''}</div>",
            unsafe_allow_html=True,
        )

    for name, assessment in S["assessments"].items():
        render_view_decision(name, assessment)

    for table in S["table_targets"]:
        render_table_decision(table)

    st.divider()
    undecided = [
        name for name in list(S["assessments"]) + list(S["table_targets"])
        if not S["decisions"].get(name)
    ]
    if undecided:
        st.caption(
            f"{len(undecided)} object(s) still without a decision: "
            f"{', '.join(undecided[:5])}{' …' if len(undecided) > 5 else ''}"
        )
    if st.button("Continue to the report →", type="primary"):
        S["step"] = STEPS[3]
        st.rerun()


def render_view_decision(name: str, assessment: Assessment) -> None:
    verdict = assessment.verdict
    label = theme.verdict_label(verdict)
    with st.expander(f"{label}  ·  {name}", expanded=verdict is not Verdict.PASS):
        st.markdown(theme.verdict_chip(verdict), unsafe_allow_html=True)

        if verdict is Verdict.UNPARSEABLE:
            reason = next(
                (i.message for i in assessment.parse_issues if i.fatal),
                "no confident syntax tree could be built",
            )
            st.error(
                f"**The DDL could not be read: {reason}.** No verdict is given "
                f"because none would be trustworthy — a rule run against a "
                f"half-parsed view could pass something that does not work."
            )
        elif not assessment.results and not assessment.parse_issues:
            st.error(
                f"**{name} was not found.** No DDL source came back for it. "
                f"Check the spelling, or whether the view exists in this client "
                f"and your user can read it."
            )

        problems = [r for r in assessment.problems if r.severity is not Severity.BLOCKING]
        if not problems and assessment.results:
            st.success(
                "**Ready for replication.** Extraction and CDC delta are "
                "correctly declared, and every rule is satisfied. Nothing to do."
            )
        for result in problems:
            css = {
                Severity.HARD: "cdc-hard", Severity.FIXABLE: "cdc-fix",
                Severity.MANUAL_REVIEW: "cdc-rev", Severity.WARNING: "cdc-warn",
            }.get(result.severity, "cdc-warn")
            where = f" · line {result.ref.line}" if result.ref.line else ""
            st.markdown(
                f'<div class="cdc-finding {css}">'
                f'<span class="rule">{result.rule_id}{where}</span><br>{result.message}'
                + (f'<div class="fix">→ {result.remediation}</div>'
                   if result.remediation else "")
                + "</div>",
                unsafe_allow_html=True,
            )

        options = actions_for(assessment)
        choice = st.radio(
            "What should happen?", options, key=f"act_{name}", horizontal=False,
        )
        S["decisions"][name] = choice

        if choice.startswith("Build a Z-wrapper"):
            render_wrapper_preview(name, assessment)
        elif choice.startswith("Add the missing annotations"):
            st.info(
                "This is a customer object, so the annotations can be added to "
                "it directly. The tool does not edit it — take the findings "
                "above and make the change in Eclipse."
            )


def actions_for(assessment: Assessment) -> list[str]:
    """The actions genuinely available for this object.

    Modifying a standard SAP view is never offered — not as a disabled option,
    not with a warning. The route for an SAP view is a wrapper, always.
    """
    metadata = S["metadata"]
    meta = metadata.get_object(assessment.object_name)
    modifiable = bool(meta and meta.is_modifiable)

    if assessment.verdict is Verdict.PASS:
        return ["Use it as it is — nothing to do", "Skip for now"]
    if assessment.verdict is Verdict.UNPARSEABLE:
        return ["Look at the DDL by hand", "Skip for now"]
    if assessment.verdict is Verdict.FAIL_HARD:
        return [
            "Flag for redesign — annotations cannot fix this",
            "Skip for now",
        ]

    # A standard SAP view is never offered for in-place editing — not greyed
    # out, not with a warning. The change would survive until the next upgrade
    # overwrote it, and then the extraction would stop with nothing to connect
    # it to.
    options = ["Build a Z-wrapper over it", "Skip for now"]
    if modifiable:
        options.insert(0, "Add the missing annotations to this view")
    return options


def render_wrapper_preview(name: str, assessment: Assessment) -> None:
    from cdcforge.generator import generate_wrapper
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    metadata = S["metadata"]
    source = metadata.get_view_source(name)
    if source is None:
        st.warning("Source unavailable, so no wrapper can be generated.")
        return

    ctx = ValidationContext(view=parse_ddl(source, name_hint=name), metadata=metadata)
    generated = generate_wrapper(ctx, assessment)

    if generated.refused_because:
        st.error(f"**Refused.** {generated.refused_because}")
        return

    for warning in generated.warnings:
        st.markdown(
            f'<div class="cdc-note">⚠ {warning}</div>', unsafe_allow_html=True
        )
    st.code(generated.ddl, language="sql")
    S["generated"][generated.name] = generated.ddl


def render_table_decision(table_name: str) -> None:
    from cdcforge.generator import generate_view_for_table

    metadata = S["metadata"]
    table = metadata.get_table(table_name)
    report = S["successors"].get(table_name)

    header = f"Table  ·  {table_name}"
    if report is not None and report.ready:
        header = f"Already covered  ·  {table_name}"

    with st.expander(header, expanded=True):
        if table is None:
            st.warning(
                f"**{table_name} was not found.** It is not a table this system "
                f"knows about — check the spelling, or whether it exists in "
                f"client {S['system_label'].split('client ')[-1][:3] or '?'}."
            )
            S["decisions"][table_name] = "Not found"
            return

        st.caption(
            f"{table_name} has {len([f for f in table.fields if not f.is_client])} "
            f"field(s), {len(table.business_key_fields)} of them key."
        )

        # F-09 first. Building a second extractor over a table that already has
        # a working one leaves two objects to maintain forever.
        base = ""
        if report is not None:
            base = render_successors(table_name, report)

        if base:
            chosen = next((c for c in report.candidates if c.view == base), None)
            if chosen is not None and chosen.is_ready:
                st.success(
                    f"**{base} is already extraction- and CDC-enabled.** Point "
                    f"the Replication Flow at it — there is nothing to build."
                )
                S["decisions"][table_name] = f"Use {base} as it is"
                return
            render_wrapper_over(base, table_name)
            return

        generated = generate_view_for_table(table)
        if generated.refused_because:
            st.error(f"**No view can be generated.** {generated.refused_because}")
            S["decisions"][table_name] = "Cannot be generated"
            return

        st.caption(
            "One table, so the generated view uses "
            "`changeDataCapture.automatic` — the framework derives the key "
            "mapping itself, which is far less to get wrong than a hand-written "
            "mapping."
        )
        for warning in generated.warnings:
            st.markdown(
                f'<div class="cdc-note">⚠ {warning}</div>', unsafe_allow_html=True
            )
        st.code(generated.ddl, language="sql")

        S["decisions"][table_name] = st.radio(
            "What should happen?",
            ["Take this DDL and create the view yourself", "Skip for now"],
            key=f"tbl_{table_name}", horizontal=True,
        )
        S["generated"][generated.name] = generated.ddl


def render_wrapper_over(base_view: str, table_name: str) -> None:
    """Generate a delta wrapper over the chosen standard view."""
    from cdcforge.generator import generate_wrapper
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext, validate_view

    metadata = S["metadata"]
    source = metadata.get_view_source(base_view)
    if source is None:
        st.error(f"**{base_view} could not be read.**")
        return

    # One context for both the assessment and the generator — validate_object
    # would build a second and re-walk the same dependency stack.
    ctx = ValidationContext(
        view=parse_ddl(source, name_hint=base_view),
        metadata=metadata,
        object_meta=metadata.get_object(base_view),
    )
    generated = generate_wrapper(ctx, validate_view(ctx.view, context=ctx))

    if generated.refused_because:
        st.error(f"**No wrapper can be built over {base_view}.** "
                 f"{generated.refused_because}")
        S["decisions"][table_name] = f"Cannot wrap {base_view}"
        return

    for warning in generated.warnings:
        st.markdown(f'<div class="cdc-note">⚠ {warning}</div>', unsafe_allow_html=True)
    st.code(generated.ddl, language="sql")

    S["decisions"][table_name] = st.radio(
        "What should happen?",
        [f"Take this DDL and create the wrapper over {base_view} yourself",
         "Skip for now"],
        key=f"wrap_{table_name}", horizontal=False,
    )
    S["generated"][generated.name] = generated.ddl


def render_create() -> None:
    """Offer to create the generated objects in $TMP.

    Deliberately the least automatic thing in the UI. Everything else here can
    be undone by closing the tab; this cannot, so it asks, it says exactly what
    it will do and where, and it reports each object separately rather than a
    single "done".
    """
    generated = S["generated"]
    st.markdown("### Create in the system")

    if not generated:
        st.caption("Nothing generated yet, so there is nothing to create.")
        return

    if S["mode"] != "live":
        st.info(
            "Sample data is loaded, so there is no system to write to. "
            "Download the DDL below."
        )
        return

    if S["productive"]:
        st.error(
            f"**Client role is {S['role_label']}.** Writes are refused against "
            f"a productive client, and an unknown role counts as productive. "
            f"Download the DDL and create the objects through your normal "
            f"transport process."
        )
        return

    st.caption(
        "Objects are created under Z/Y names only. Nothing existing is ever "
        "overwritten, and SAP's syntax check runs before anything is kept. "
        "This tool never touches an SAP object."
    )

    LOCAL_CHOICE = f"Local — {LOCAL_PACKAGE}, stays on this system"
    TRANSPORT_CHOICE = "A package with a transport request"

    where = st.radio(
        "Where should these objects go?",
        [LOCAL_CHOICE, TRANSPORT_CHOICE],
        help=f"{LOCAL_PACKAGE} needs no transport request and can never reach "
        f"another system — good for trying things out, useless for promoting "
        f"them. A transportable package needs a request, which is what carries "
        f"the objects to QA and production.",
    )

    package = LOCAL_PACKAGE
    transport = ""
    new_transport = ""

    if where == TRANSPORT_CHOICE:
        package = (
            st.text_input(
                "Package",
                value="",
                placeholder="ZDSP_EXTRACTION",
                help="A customer (Z/Y) package. An SAP package is refused "
                "before anything is sent.",
            )
            .strip()
            .upper()
        )
        if not package:
            st.caption("Enter a package to continue.")
            return

        need = transport_need_for(package)
        if need is None:
            return
        if need.unreadable:
            st.error(
                f"Could not establish what **{package}** requires. "
                f"{need.messages[0] if need.messages else ''} Nothing will be "
                f"created — an unreadable answer is treated as a refusal, not "
                f"as 'no request needed'."
            )
            return

        if not need.required:
            st.info(f"**{package}** — {need.render().split(': ', 1)[-1]}")
        else:
            transport, new_transport = choose_transport(package, need)
            if not transport and not new_transport:
                return

    activate = st.checkbox(
        "Activate after creating",
        value=True,
        help="On by default: an inactive view cannot be previewed, cannot be "
        "consumed, and does not appear in the extraction registry, so leaving "
        "it inactive finishes nothing. Turn it off to create the source and "
        "activate it yourself in Eclipse. A source SAP's check rejects is "
        "removed again either way.",
    )
    confirmed = st.checkbox(
        f"Yes — create {len(generated)} object(s) in {package} on "
        f"{S['system_label']}"
    )

    if st.button(
        f"Create {len(generated)} object(s)",
        type="primary", disabled=not confirmed,
    ):
        run_create(
            list(generated.items()),
            activate=activate,
            package=package,
            transport=transport,
            new_transport=new_transport,
        )

    for name, rendering in S["created"].items():
        st.markdown(f"**{name}**")
        st.code(rendering, language="text")


def choose_transport(package: str, need) -> tuple[str, str]:
    """Pick a request, type one, or create one. Returns ``(number, new_desc)``.

    Typing a number is a first-class option rather than a fallback. CTS's list
    holds the requests it considers usable *by this user for this object*,
    which is not the set of requests that would work — being handed a
    colleague's request number and told to use it is an ordinary way to work,
    and it will not be in that list.
    """
    from cdcforge.connect.transport import request_status

    offered = [r.number for r in need.requests]
    PICK = "Use one of my open requests"
    TYPE = "Enter a request number"
    NEW = "Create a new request"

    modes = ([PICK] if offered else []) + [TYPE]
    if not need.existing_only:
        modes.append(NEW)

    if not offered:
        st.warning(
            f"**{package}** records changes and CTS offers no open request of "
            f"yours that can take these objects."
        )
    if need.existing_only:
        st.caption(
            f"{package} only accepts existing requests — a new one cannot be "
            f"created for it."
        )

    mode = st.radio("Transport request", modes, horizontal=len(modes) < 3)

    if mode == PICK:
        number = st.selectbox(
            "Request",
            offered,
            format_func=lambda n: next(
                (r.render() for r in need.requests if r.number == n), n
            ),
        )
        return number, ""

    if mode == TYPE:
        typed = (
            st.text_input(
                "Request number",
                value="",
                placeholder="DEVK900123",
                help="A modifiable workbench request. It does not have to be "
                "one of yours — if a colleague has told you to use theirs, "
                "that works, and they control when it is released.",
            )
            .strip()
            .upper()
        )
        if not typed:
            return "", ""

        # Check it before the user commits. The write would refuse a released
        # or missing request anyway, but not until after the confirmation and
        # a round of creates, which is a poor place to learn about a typo.
        session = S.get("session")
        status = request_status(session, typed) if session is not None else None
        if status is None:
            return typed, ""
        refusal = status.refusal()
        if refusal:
            st.error(refusal)
            return "", ""
        owner = f" · owned by {status.owner}" if status.owner else ""
        st.success(
            f"**{typed}** — {status.status_text or 'modifiable'}"
            f"{owner}{f' · {status.description}' if status.description else ''}"
        )
        return typed, ""

    description = st.text_input(
        "New request description", value="CDC Forge generated objects"
    )
    if not st.checkbox(
        f"Create a new workbench request in {package}",
        help="A real transport request in your SE09. It stays there until you "
        "release or delete it.",
    ):
        return "", ""
    return "", description


def transport_need_for(package: str):
    """Ask CTS about a package from the UI's read-only session.

    Read-only on purpose. The user is still deciding at this point, and the
    question "what would this need" must not itself create anything.
    """
    from cdcforge.connect.transport import check_transport

    session = S.get("session")
    if session is None:
        st.warning("Not connected, so the package cannot be checked.")
        return None
    try:
        return check_transport(session, "ZCDCFORGE_PROBE", package)
    except Exception as exc:  # a UI must not die on a probe
        st.error(f"Could not check {package}: {exc}")
        return None


def run_create(
    objects: list[tuple[str, str]],
    *,
    activate: bool,
    package: str = LOCAL_PACKAGE,
    transport: str = "",
    new_transport: str = "",
) -> None:
    """Open a write session, create each object, and report each separately."""
    from cdcforge.connect.audit import AuditLog
    from cdcforge.connect.profile import ConnectionProfile
    from cdcforge.connect.session import AdtError, AdtSession
    from cdcforge.connect.transport import create_request
    from cdcforge.connect.writer import WritePolicy, create_view

    profile_id = S["profile_id"]
    try:
        profile = ConnectionProfile.load(profile_id)
        # A separate session: the one the rest of the UI uses is read-only and
        # stays that way, so nothing else in the app can write by accident.
        session = AdtSession(
            profile,
            AuditLog(Path.home() / ".cdc-forge" / f"{profile_id}.sqlite"),
            read_only=False,
        )
        session.connect()
    except (AdtError, FileNotFoundError) as exc:
        st.error(f"Could not open a write session: {exc}")
        return

    # The production guard treats an unknown client role as productive, and the
    # role is established by the preflight. Carrying it over from the read-only
    # session avoids a second round trip and keeps the two consistent.
    preflight = S["preflight"]
    if preflight is not None:
        session.system_role = preflight.client_role

    if new_transport:
        # One request for the whole batch, not one each. Created here rather
        # than in the loop so a failure costs nothing and leaves nothing.
        transport, error = create_request(session, package, new_transport)
        if error:
            st.error(f"Could not create a transport request: {error}")
            session.logoff()
            return
        st.success(f"Created transport request **{transport}** — {new_transport}")

    policy = WritePolicy(allow_transportable=package.upper() != LOCAL_PACKAGE)

    progress = st.progress(0.0, text="Creating…")
    for index, (name, ddl) in enumerate(objects, 1):
        progress.progress(index / len(objects), text=f"Creating {name}…")
        result = create_view(
            session, name, ddl,
            description="Generated by CDC Forge",
            activate=activate,
            package=package,
            policy=policy,
            transport=transport,
        )
        S["created"][name] = result.render()
    progress.empty()
    session.logoff()
    st.rerun()


def render_successors(table_name: str, report) -> str:
    """F-09 — what already reads this table, ranked, with the rejects shown.

    Returns the base the user chose: a view name, or "" for build-on-table.

    The rejected views are shown, not hidden. A consultant learns more from
    seeing why three of five were ruled out than from being handed one name,
    and it is what lets them disagree with the tool.
    """
    if report.prefer_table:
        st.warning(f"**{report.recommendation}**")
    elif report.ready:
        st.success(f"**{report.recommendation}**")
    elif report.wrappable:
        st.info(f"**{report.recommendation}**")
    else:
        st.warning(f"**{report.recommendation}**")

    if report.coverage_note:
        st.caption(report.coverage_note)

    # Short list when something is already CDC-enabled, long list when nothing
    # is. With a ready view there is one obvious answer and a wall of
    # alternatives only invites second-guessing it; without one, every option
    # costs a wrapper and the trade-off between coverage, layer and stack depth
    # is a real decision that only the person who knows the requirement can
    # make.
    usable = report.usable
    shown = report.choices()

    if shown:
        st.markdown("###### Views that can carry delta")
        if report.has_ready:
            st.caption(
                "Ranked with already-enabled views first — those need no "
                "wrapper at all. Check the coverage before taking one: it may "
                "carry fewer columns than a view you would have to wrap."
            )
        else:
            st.caption(
                "None of these is CDC-enabled yet, so each costs one wrapper. "
                "Ranked by how much of the table they carry — pick on the "
                "columns you actually need."
            )
        if any(c.row_filtered for c in shown):
            st.caption(
                "**filtered rows** means a WHERE clause in the stack restricts "
                "which records appear — VBAP's ready views are each one "
                "document category. Coverage counts columns, so a filtered "
                "view gives every column for *some* rows, not some columns for "
                "every row."
            )
        for candidate in shown:
            colour = theme.GREEN if candidate.is_ready else theme.BLUE
            bar = round(candidate.coverage * 20)
            st.markdown(
                f'<div class="cdc-finding" style="border-left-color:{colour}">'
                f"<b>{candidate.view}</b> "
                f'<span class="rule">{candidate.detail}</span>'
                f'<div class="fix">{"█" * bar}{"░" * (20 - bar)} '
                f"{candidate.exposed_fields} of {candidate.table_fields} "
                f"columns &nbsp;·&nbsp; {candidate.summary}</div></div>",
                unsafe_allow_html=True,
            )
        if len(usable) > len(shown):
            with st.expander(
                f"{len(usable) - len(shown)} more that could also carry delta",
                expanded=False,
            ):
                for candidate in usable[len(shown):]:
                    st.markdown(
                        f'<div class="cdc-finding">'
                        f"<b>{candidate.view}</b> "
                        f'<span class="rule">{candidate.detail}</span></div>',
                        unsafe_allow_html=True,
                    )

    if report.excluded:
        with st.expander(
            f"{len(report.excluded)} view(s) ruled out — and why", expanded=False
        ):
            for candidate in report.excluded[:20]:
                st.markdown(
                    f'<div class="cdc-finding cdc-warn">'
                    f'<b style="color:{theme.MUTED}">{candidate.view}</b>'
                    f'<div class="fix">{candidate.excluded_because}</div></div>',
                    unsafe_allow_html=True,
                )

    if report.truncated:
        st.caption(
            "Only part of the view inventory was searched, so this list may be "
            "incomplete."
        )

    # --- choose the base -------------------------------------------------
    # Coverage goes in the option label itself. It is the number the choice
    # actually turns on, and putting it anywhere else means reading it in one
    # place and deciding in another.
    options = [
        f"{c.view}  —  {c.coverage:.0%} of {table_name}"
        f"  ({'already CDC-enabled' if c.is_ready else 'needs a wrapper'})"
        for c in usable
    ]
    options.append("Build directly on the table")
    options.append("Use a view I name myself")

    # When every candidate is thin, the table is the right default — a view
    # exposing 4% of the columns is correct for CDC and still the wrong object.
    default = len(options) - 2 if report.prefer_table else 0

    choice = st.radio(
        "What should the new object be built on?",
        options, index=default, key=f"base_{table_name}",
    )

    if choice.startswith("Build directly"):
        st.caption(
            "A single-table view qualifies for `changeDataCapture.automatic`, "
            "has the shallowest possible stack, and leaves the modelling to "
            "Datasphere — which is usually the right trade for replication."
        )
        return ""

    if choice.startswith("Use a view I name"):
        typed = st.text_input(
            "View name", key=f"own_{table_name}",
            placeholder="I_PurchaseOrderAPI01",
        ).strip().upper()
        if typed:
            return validate_chosen_base(table_name, typed)
        st.caption("Type a view name and it will be checked against the same gates.")
        return ""

    # By position rather than by parsing the label back apart, so the label
    # stays free to change without silently returning a truncated view name.
    return usable[options.index(choice)].view


def validate_chosen_base(table_name: str, view_name: str) -> str:
    """Check a view the user named against the same gates as the suggestions.

    Accepting it unchecked would be a foot-gun with extra steps.
    """
    from cdcforge.successors import find_candidates

    metadata = S["metadata"]
    if metadata.get_view_source(view_name) is None:
        st.error(
            f"**{view_name} was not found.** Check the spelling, or whether it "
            f"exists in this client and your user can read it."
        )
        return ""

    report = find_candidates(metadata, table_name)
    match = next((c for c in report.candidates if c.view == view_name), None)

    if match is None:
        st.warning(
            f"**{view_name} exists, but does not appear to read {table_name}.** "
            f"A CDC mapping has to address {table_name}, so a wrapper over this "
            f"view could not carry its delta."
        )
        return ""

    if not match.usable:
        st.error(f"**{view_name} cannot carry delta** — {match.excluded_because}")
        for reason in match.exclusion_reasons[1:]:
            st.caption(f"Also: {reason}")
        return ""

    st.success(f"**{view_name} passes the gates.** {match.detail}")
    return view_name


# ---------------------------------------------------------------------------
# Step 4 — Report
# ---------------------------------------------------------------------------


def step_report() -> None:
    st.markdown("# Report")
    if not S["assessments"] and not S["table_targets"]:
        st.info("Nothing analysed yet.")
        return

    rows = []
    for name, assessment in S["assessments"].items():
        reasons = "; ".join(
            r.message for r in assessment.problems
            if r.severity in (Severity.HARD, Severity.FIXABLE, Severity.MANUAL_REVIEW)
        )
        rows.append({
            "Object": name,
            "Kind": "CDS view",
            "Verdict": theme.verdict_label(assessment.verdict),
            "Decision": S["decisions"].get(name, ""),
            "Why": reasons or "Ready — nothing to do",
        })
    for table in S["table_targets"]:
        rows.append({
            "Object": table, "Kind": "Table",
            "Verdict": "New view required",
            "Decision": S["decisions"].get(table, ""),
            "Why": "No CDS view with extraction and delta",
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    render_create()

    left, right = st.columns(2)
    with left:
        workbook = build_decision_workbook()
        if workbook:
            st.download_button(
                "Download decision sheet (Excel)", workbook,
                file_name="cdc-plan.xlsx", type="primary",
                mime="application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet",
                help="Edit the Action, Base, Target name and Note columns "
                "offline, then bring it back below.",
            )
    with right:
        if S["generated"]:
            bundle = "\n\n".join(
                f"-- {name}\n{ddl}" for name, ddl in S["generated"].items()
            )
            st.download_button(
                f"Download generated DDL ({len(S['generated'])})", bundle,
                file_name="cdc-generated.ddl", mime="text/plain",
            )

    render_apply_plan()
    render_estate()


def render_estate() -> None:
    """What is already in the system, and whether it still works.

    Both were CLI-only, which is the same inconsistency the decision sheet had:
    a feature that exists but not where this user works is a feature they do
    not have. Read-only, and behind a button because the survey reads every
    custom extraction view's source.
    """
    if S["mode"] != "live" or S.get("metadata") is None:
        return

    st.markdown("### What is already built")
    st.caption(
        "Custom extraction views already in this system, and whether they "
        "still carry delta. Worth a look before building anything, and worth "
        "another after an upgrade."
    )

    left, right = st.columns(2)
    with left:
        if st.button("Survey what exists"):
            run_estate(check=False)
    with right:
        if st.button("Survey and verify each one"):
            run_estate(check=True)

    found = S.get("estate")
    if found is None:
        return

    if not found.surveyed:
        st.warning(
            "The system did not report its extraction-enabled views, so "
            "nothing can be said about what already exists. That is not the "
            "same as nothing being there."
        )
        return

    checks = S.get("estate_checks") or {}
    rows = []
    for obj in sorted(found.objects, key=lambda o: (o.root_table, o.name)):
        result = checks.get(obj.name)
        rows.append(
            {
                "Object": obj.name,
                "Feeds": obj.root_table or "(unresolved)",
                "Built on": obj.base,
                "Delta declared": "yes" if obj.declares_cdc else "no",
                "Still working": result.status if result else "",
                "Note": " · ".join(result.notes) if result and result.notes else "",
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    if checks:
        broken = [r for r in checks.values() if not r.ok]
        if broken:
            st.error(
                f"**{len(broken)} of {len(checks)} are not currently carrying "
                f"delta.** Replicating those gives an initial load and no "
                f"changes after it."
            )
        else:
            st.success(f"All {len(checks)} still carrying delta.")


def run_estate(*, check: bool) -> None:
    from cdcforge.estate import survey
    from cdcforge.verify import verify

    metadata = S["metadata"]
    with st.spinner("Reading every custom extraction view…"):
        found = survey(metadata)
    S["estate"] = found
    S["estate_checks"] = {}

    if check and found.objects:
        with st.spinner("Checking each one against the system…"):
            # forget first: the survey already refreshed this list, and
            # verifying against a stale one is the whole bug it exists to avoid.
            metadata.forget_extraction_enabled()
            delta = metadata.extraction_enabled_views()
            report = verify(
                metadata,
                [o.name for o in found.objects],
                delta_supported=delta,
                check_rules=False,
            )
        S["estate_checks"] = {r.name: r for r in report.results}
    st.rerun()


def build_decision_workbook() -> bytes:
    """The same workbook `cdcforge plan` writes, from what the UI already has.

    One format, one reader. An earlier cut offered a flat CSV with a free-text
    Decision column, which looked like the same thing and could not be fed back
    to anything — the user filled it in and then had nowhere to put it.
    """
    from cdcforge.decisions import USE, WRAP, Decision, suggest, write_plan
    from cdcforge.estate import survey

    # Same survey the CLI's `plan` runs. Without it the sheet's Existing
    # column is blank here and populated there, which is worse than either —
    # the whole point of the column is that a re-run stops proposing objects
    # that already exist.
    estate = None
    metadata = S.get("metadata")
    if metadata is not None:
        try:
            estate = survey(metadata)
        except Exception as exc:  # a download must not die on the survey
            st.caption(f"Could not check what already exists: {exc}")

    decisions: list[Decision] = []
    for table in S["table_targets"]:
        report = S["successors"].get(table)
        if report is None:
            decisions.append(Decision(object_name=table, kind="TABLE"))
        else:
            decisions.append(
                suggest(
                    table,
                    report,
                    estate=estate,
                    table=metadata.get_table(table) if metadata else None,
                )
            )

    for name, assessment in S["assessments"].items():
        # An existing view the user asked about. Ready means replicate it;
        # anything else means a wrapper over it, which is what the view
        # decision panel offers too.
        ready = assessment.verdict is Verdict.PASS
        decisions.append(
            Decision(
                object_name=name,
                kind="VIEW",
                action=USE if ready else WRAP,
                base=name,
                target="" if ready else f"ZW_{name.removeprefix('I_')}"[:30],
                why=(
                    "Passes every rule — replicate it as it is."
                    if ready
                    else "; ".join(r.message for r in assessment.problems[:2])
                ),
                suggested_action=USE if ready else WRAP,
                suggested_base=name,
            )
        )

    if not decisions:
        return b""

    try:
        with tempfile.TemporaryDirectory() as directory:
            path = write_plan(decisions, Path(directory) / "plan.xlsx")
            return path.read_bytes()
    except RuntimeError as exc:  # openpyxl missing
        st.caption(str(exc))
        return b""


def render_apply_plan() -> None:
    """Take an edited sheet back and generate what it asks for.

    Deliberately generates only. Creating is the panel above, which already
    asks for the package, the transport request and a typed confirmation —
    routing a file upload straight into a write would skip all three.
    """
    from cdcforge.decisions import PlanError, load

    st.markdown("### Bring an edited sheet back")
    upload = st.file_uploader(
        "Decision sheet (.xlsx)",
        type=["xlsx"],
        key="plan_upload",
        help="The workbook downloaded above, with your Action and Base "
        "choices. Generating puts the DDL in the panel above, ready to create.",
    )
    if upload is None:
        return

    try:
        summary = load(upload.getvalue())
    except PlanError as exc:
        st.error(str(exc))
        return

    st.code(summary.render(), language="text")
    if not summary.ok:
        st.error(
            f"{len(summary.problems)} problem(s) — nothing was generated. "
            f"Fix the sheet and upload it again."
        )
        return

    todo = [d for d in summary.decisions if d.generates]
    if not todo:
        st.info("Nothing in this sheet needs generating.")
        return

    if st.button(f"Generate {len(todo)} object(s) from this sheet"):
        run_plan_generation(todo)


def run_plan_generation(decisions) -> None:
    """Generate every row the sheet asks for, into the create panel."""
    from cdcforge.decisions import BUILD
    from cdcforge.generator.wrapper import generate_wrapper
    from cdcforge.generator.ztable import generate_view_for_table
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext, validate_view

    metadata = S["metadata"]
    made = 0
    progress = st.progress(0.0, text="Generating…")
    for index, decision in enumerate(decisions, 1):
        progress.progress(index / len(decisions), text=f"{decision.object_name}…")
        if decision.action == BUILD:
            table = metadata.get_table(decision.object_name)
            if table is None:
                st.error(f"{decision.object_name}: table not found")
                continue
            result = generate_view_for_table(table, name=decision.target or None)
        else:
            source = metadata.get_view_source(decision.base)
            if source is None:
                st.error(f"{decision.object_name}: {decision.base} not found")
                continue
            ctx = ValidationContext(
                view=parse_ddl(source, name_hint=decision.base),
                metadata=metadata,
                object_meta=metadata.get_object(decision.base),
            )
            result = generate_wrapper(
                ctx, validate_view(ctx.view, context=ctx),
                name=decision.target or None,
            )

        if result.refused_because:
            st.warning(f"**{decision.object_name}** — {result.refused_because}")
            continue
        S["generated"][result.name] = result.ddl
        made += 1
    progress.empty()

    if made:
        st.success(
            f"Generated {made} object(s). They are in **Create in the system** "
            f"above, which is where the package and transport request are set."
        )
    st.rerun()


# ---------------------------------------------------------------------------


sidebar()

# Where you are, above every screen. The sidebar radio is how you *move*
# between steps; this is how you see the shape of the whole job without
# looking away from the page you are on.
st.markdown(theme.stepper(STEPS, S["step"]), unsafe_allow_html=True)

if not S["connected"] or S["step"] == STEPS[0]:
    step_connect()
elif S["step"] == STEPS[1]:
    step_analyse()
elif S["step"] == STEPS[2]:
    step_decide()
else:
    step_report()
