"""Command-line front end for the offline core.

Everything here runs without a system. The Streamlit UI (§7) drives the same
functions; this exists so the core is usable and demoable from a terminal, and
so CI has something to run end to end.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from cdcforge import __version__
from cdcforge.generator import (
    NamingConvention,
    build_cdc_mapping,
    generate_view_for_table,
    generate_wrapper,
    preview_names,
)
from cdcforge.metadata import MetadataSource, MockMetadataSource, NullMetadataSource
from cdcforge.model import Assessment, Verdict
from cdcforge.rules import (
    RuleConfig,
    ValidationContext,
    all_rules,
    validate_all,
    validate_object,
    validate_source,
    validate_view,
)
from cdcforge.rules.stack import resolve_stack
from cdcforge.triage import triage

_MARKER = {
    Verdict.PASS: "PASS       ",
    Verdict.FAIL_FIXABLE: "FIXABLE    ",
    Verdict.MANUAL_REVIEW: "REVIEW     ",
    Verdict.FAIL_HARD: "HARD FAIL  ",
    Verdict.UNPARSEABLE: "UNPARSEABLE",
}


def _metadata(args: argparse.Namespace) -> MetadataSource:
    if getattr(args, "fixtures", None):
        return MockMetadataSource(args.fixtures)
    return NullMetadataSource()


def _print_assessment(assessment: Assessment, *, verbose: bool) -> None:
    print(f"{_MARKER[assessment.verdict]}  {assessment.object_name}")
    if assessment.write_blocked:
        for blocker in assessment.block_reasons:
            print(f"    WRITES BLOCKED  {blocker.rule_id}: {blocker.message}")
    for issue in assessment.parse_issues:
        tag = "FATAL" if issue.fatal else "note "
        where = f" (line {issue.ref.line})" if issue.ref.line else ""
        print(f"    {tag}  {issue.message}{where}")

    shown = assessment.results if verbose else assessment.problems
    for result in shown:
        if not verbose and result.severity.value == "BLOCKING":
            continue  # already printed above
        print(f"    {result.format_line()}")
        if verbose and result.sap_source:
            print(f"        source: {result.sap_source}")
        if result.remediation and result.is_problem:
            print(f"        fix: {result.remediation}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    metadata = _metadata(args)
    target = Path(args.target)

    if target.is_file():
        assessment = validate_source(
            target.read_text(encoding="utf-8"),
            name=target.stem.upper(),
            metadata=metadata,
            config=RuleConfig(),
            only_rules=args.rule,
        )
    else:
        assessment = validate_object(
            args.target, metadata, config=RuleConfig(), only_rules=args.rule
        )

    if args.json:
        print(json.dumps(assessment.to_dict(), indent=2))
    else:
        _print_assessment(assessment, verbose=args.verbose)
    return 0 if assessment.verdict is Verdict.PASS else 1


def cmd_scan(args: argparse.Namespace) -> int:
    metadata = _metadata(args)
    assessments = validate_all(metadata)
    summary = triage(assessments, metadata)

    if args.json:
        print(
            json.dumps(
                {
                    "summary": {b.value: names for b, names in summary.buckets.items()},
                    "bare_tables": summary.bare_tables,
                    "assessments": [a.to_dict() for a in assessments],
                },
                indent=2,
            )
        )
        return 0

    for assessment in sorted(assessments, key=lambda a: a.object_name):
        _print_assessment(assessment, verbose=args.verbose)
    print()
    print(summary.render())
    return 0


def _progress_printer() -> "object":
    """Print progress on one rewritten line, so an hours-long scan looks alive."""
    state = {"last": 0.0}

    def report(progress) -> None:
        now = time.monotonic()
        final = progress.done >= progress.total
        if not final and now - state["last"] < 0.2:
            return
        state["last"] = now
        sys.stdout.write("\r" + progress.render().ljust(110))
        if final:
            sys.stdout.write("\n")
        sys.stdout.flush()

    return report


def cmd_inventory(args: argparse.Namespace) -> int:
    from cdcforge.inventory import InventoryScanner
    from cdcforge.store import Store

    metadata = _metadata(args)
    store = Store(args.store, profile_id=args.profile_id)
    store.record_system(host=metadata.describe())

    scanner = InventoryScanner(
        metadata, store, progress=None if args.quiet else _progress_printer()
    )
    result = scanner.scan(
        refresh=args.refresh,
        use_cached_verdicts=not args.revalidate,
        limit=args.limit,
    )
    print(result.render())
    print(f"\nStore: {store.path}  {store.stats()}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from cdcforge.report import ReportData, write_all, write_excel, write_html, write_json
    from cdcforge.store import Store

    store = Store(args.store, profile_id=args.profile_id)
    if store.view_count() == 0:
        print(
            f"No inventory in {store.path}. Run 'cdc-forge inventory' first.",
            file=sys.stderr,
        )
        return 2

    data = ReportData.from_store(
        store,
        store.all_assessments(),
        system_id=args.system or "",
        source_label=args.source_label or str(store.path),
    )

    writers = {"json": write_json, "html": write_html, "xlsx": write_excel}
    out = Path(args.out)
    if args.format == "all":
        written = write_all(data, out, stem=args.stem)
    else:
        written = [writers[args.format](data, out / f"{args.stem}.{args.format}")]

    for path in written:
        print(f"Wrote {path}")
    print(
        f"\n{data.counts.get('TOTAL_VIEWS', 0)} views, "
        f"{data.total_effort_days} estimated person-days "
        f"(see the assumptions in the report before quoting anything)."
    )
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Launch the Streamlit UI.

    Shells out rather than importing: a Streamlit script is executed by
    Streamlit's own runtime, not called as a function.
    """
    import subprocess

    from cdcforge.ui import APP

    try:
        import streamlit  # noqa: F401
    except ImportError:
        print(
            "The UI needs Streamlit:  python -m pip install streamlit",
            file=sys.stderr,
        )
        return 2

    command = [
        sys.executable, "-m", "streamlit", "run", str(APP),
        "--server.port", str(args.port),
        "--server.headless", "true" if args.no_browser else "false",
        "--browser.gatherUsageStats", "false",
    ]
    print(f"Starting CDC Forge on http://localhost:{args.port}  (ctrl-c to stop)")
    return subprocess.call(command)


def cmd_uses(args: argparse.Namespace) -> int:
    """Which views read this table, and what state are they in?

    The practical question behind "is there already a standard view for
    <table>": not just which views touch it, but which of those are actually
    extraction- and CDC-enabled, and which of those the rules accept.
    """
    from cdcforge.store import Store

    store = Store(args.store, profile_id=args.profile_id)
    if store.view_count() == 0:
        print(
            f"No inventory in {store.path}. Run 'cdc-forge inventory' first.",
            file=sys.stderr,
        )
        return 2

    target = args.table.upper()
    names = set(store.dependents_of(target))
    rows = [v for v in store.views() if v["ddl_name"] in names or target in v["base_tables"]]

    if not rows:
        print(f"No view in the inventory reads {target}.")
        print(
            "That is the 'Bare' case: either no view exists, or the inventory "
            "does not cover it yet."
        )
        return 1

    print(f"{len(rows)} view(s) read {target}\n")
    header = f"{'View':<40} {'List':<13} {'Extraction':<11} {'Delta':<10} {'CDC':<10} Owner"
    print(header)
    print("-" * len(header))
    for row in sorted(rows, key=lambda r: (r["bucket"] or "", r["ddl_name"])):
        print(
            f"{row['ddl_name']:<40} {row['bucket'] or '':<13} "
            f"{'yes' if row['extraction_enabled'] else 'no':<11} "
            f"{row['delta_method'] or '':<10} {row['cdc_type'] or '':<10} "
            f"{row['owner'] or ''}"
        )

    usable = [r for r in rows if r["bucket"] == "READY"]
    print()
    if usable:
        print(f"Already CDC-ready: {', '.join(r['ddl_name'] for r in usable)}")
    else:
        print(
            "None of these are CDC-ready as they stand. Extraction-enabled is "
            "not the same as CDC-delta capable — check the Delta column."
        )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from cdcforge.store import Store

    store = Store(args.store, profile_id=args.profile_id)
    if args.compare:
        from cdcforge.inventory import compare_snapshots

        before = store.get_snapshot(args.compare)
        after = store.get_snapshot(args.name) if args.name else store.take_snapshot("__now")
        if before is None:
            print(f"No snapshot {args.compare!r}", file=sys.stderr)
            return 2
        print(compare_snapshots(before, after).render())
        return 0

    if not args.name:
        for row in store.snapshots():
            print(
                f"{row['snapshot_id']:<24} {row['taken_at']}  "
                f"{row['view_count']} views  {row['hash'][:12]}"
            )
        return 0

    payload = store.take_snapshot(args.name)
    print(f"Snapshot {args.name!r}: {len(payload)} views")
    return 0


def cmd_stack(args: argparse.Namespace) -> int:
    metadata = _metadata(args)
    source = metadata.get_view_source(args.name)
    if source is None:
        path = Path(args.name)
        if not path.is_file():
            print(f"no source found for {args.name}", file=sys.stderr)
            return 2
        source = path.read_text(encoding="utf-8")

    from cdcforge.parsing.ddl import parse_ddl

    view = parse_ddl(source, name_hint=args.name)
    stack = resolve_stack(view, metadata)
    print(stack.render_tree())
    print()
    print(stack.describe())
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    for rule_obj in all_rules():
        spec = rule_obj.spec
        print(f"{spec.id}  [{spec.severity.value:<13}] {spec.title}")
        if args.verbose:
            print(f"        tier: {spec.tier}")
            print(f"        source: {spec.sap_source}")
            if spec.rationale:
                print(f"        why: {spec.rationale}")
    print(f"\n{len(all_rules())} rules registered")
    return 0


def cmd_generate_table(args: argparse.Namespace) -> int:
    metadata = _metadata(args)
    table = metadata.get_table(args.table)
    if table is None:
        print(f"table {args.table} not found in {metadata.describe()}", file=sys.stderr)
        return 2

    naming = NamingConvention(element_style=args.element_style)
    generated = generate_view_for_table(table, naming=naming, name=args.name)

    if generated.refused_because:
        print(f"REFUSED  {generated.refused_because}")
        return 1

    preview = preview_names({table.name: generated.name}, metadata)
    for check in preview.problems:
        print(f"NAME PROBLEM  {check.problem}")

    for warning in generated.warnings:
        print(f"# warning: {warning}")
    print(generated.ddl)
    return 0 if preview.ok else 1


def cmd_generate_wrapper(args: argparse.Namespace) -> int:
    metadata = _metadata(args)
    source = metadata.get_view_source(args.view)
    if source is None:
        print(f"view {args.view} not found in {metadata.describe()}", file=sys.stderr)
        return 2

    from cdcforge.parsing.ddl import parse_ddl

    # One context for both the assessment and the generator; validate_source
    # would build a second and re-walk the same dependency stack.
    ctx = ValidationContext(
        view=parse_ddl(source, name_hint=args.view),
        metadata=metadata,
        object_meta=metadata.get_object(args.view),
    )
    assessment = validate_view(ctx.view, context=ctx)
    generated = generate_wrapper(ctx, assessment, name=args.name)

    if generated.refused_because:
        print(f"REFUSED  {generated.refused_because}")
        return 1
    for warning in generated.warnings:
        print(f"# warning: {warning}")
    print(generated.ddl)
    return 0


def cmd_generate_mapping(args: argparse.Namespace) -> int:
    metadata = _metadata(args)
    source = metadata.get_view_source(args.view)
    if source is None:
        print(f"view {args.view} not found in {metadata.describe()}", file=sys.stderr)
        return 2

    from cdcforge.parsing.ddl import parse_ddl

    ctx = ValidationContext(view=parse_ddl(source, name_hint=args.view), metadata=metadata)
    proposal = build_cdc_mapping(ctx)

    for decision in proposal.decisions:
        flag = "map" if decision.included else "omit"
        role = "#MAIN" if decision.is_main else "#LEFT_OUTER_TO_ONE_JOIN"
        print(f"[{flag}] {decision.table:<20} {role:<26} {decision.reason}")
        if decision.missing_keys:
            print(f"       missing key exposure: {', '.join(decision.missing_keys)}")
    print()
    for problem in proposal.problems:
        print(f"PROBLEM  {problem}")
    if proposal.entries:
        print("mapping: [")
        print("        " + proposal.render())
        print("      ]")
        print()
        print(f"mandatory elements: {', '.join(proposal.mandatory_elements)}")
    return 0 if proposal.ok else 1


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdc-forge",
        description="Assess and generate CDC-ready ABAP CDS views (offline core).",
    )
    parser.add_argument("--version", action="version", version=f"cdc-forge {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--fixtures", help="fixture directory for mock/demo mode (F-38)")
        sub.add_argument("-v", "--verbose", action="store_true")

    validate = subparsers.add_parser("validate", help="validate one DDL file or object")
    validate.add_argument("target", help="path to a .ddl file, or an object name")
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--rule", action="append", help="run only these rules")
    add_common(validate)
    validate.set_defaults(func=cmd_validate)

    scan = subparsers.add_parser("scan", help="validate every view in the fixtures")
    scan.add_argument("--json", action="store_true")
    add_common(scan)
    scan.set_defaults(func=cmd_scan)

    def add_store(sub: argparse.ArgumentParser, *, required: bool = False) -> None:
        sub.add_argument(
            "--store",
            default="cdc-forge.sqlite",
            help="SQLite store path (default: ./cdc-forge.sqlite)",
        )
        sub.add_argument("--profile-id", default="local", help="store partition key")

    inventory = subparsers.add_parser(
        "inventory", help="harvest and validate every view, cached (F-05/F-06)"
    )
    add_common(inventory)
    add_store(inventory)
    inventory.add_argument(
        "--refresh", action="store_true", help="re-read sources instead of using the cache"
    )
    inventory.add_argument(
        "--revalidate", action="store_true", help="re-run the rules even on unchanged source"
    )
    inventory.add_argument("--limit", type=int, help="stop after N views")
    inventory.add_argument("--quiet", action="store_true", help="no progress line")
    inventory.set_defaults(func=cmd_inventory)

    report = subparsers.add_parser("report", help="export the assessment report (F-34)")
    add_store(report)
    report.add_argument("--out", default=".", help="output directory")
    report.add_argument("--stem", default="cdc-assessment", help="output file stem")
    report.add_argument(
        "--format", choices=["all", "json", "html", "xlsx"], default="all"
    )
    report.add_argument("--system", help="system id to print on the report")
    report.add_argument("--source-label", help="what was assessed")
    report.add_argument("-v", "--verbose", action="store_true")
    report.set_defaults(func=cmd_report)

    ui = subparsers.add_parser("ui", help="launch the Streamlit UI")
    ui.add_argument("--port", type=int, default=8501)
    ui.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    ui.set_defaults(func=cmd_ui)

    uses = subparsers.add_parser(
        "uses", help="which views read a table, and are any CDC-ready?"
    )
    add_store(uses)
    uses.add_argument("table", help="table name, e.g. MATDOC")
    uses.add_argument("-v", "--verbose", action="store_true")
    uses.set_defaults(func=cmd_uses)

    snapshot = subparsers.add_parser(
        "snapshot", help="take or compare inventory snapshots (F-35)"
    )
    add_store(snapshot)
    snapshot.add_argument("name", nargs="?", help="snapshot id to take, or omit to list")
    snapshot.add_argument("--compare", help="compare this earlier snapshot against `name`")
    snapshot.add_argument("-v", "--verbose", action="store_true")
    snapshot.set_defaults(func=cmd_snapshot)

    stack = subparsers.add_parser("stack", help="show the dependency tree (F-08)")
    stack.add_argument("name")
    add_common(stack)
    stack.set_defaults(func=cmd_stack)

    rules = subparsers.add_parser("rules", help="list the rule catalogue")
    add_common(rules)
    rules.set_defaults(func=cmd_rules)

    generate = subparsers.add_parser("generate", help="generate DDL")
    generate_subparsers = generate.add_subparsers(dest="what", required=True)

    gen_table = generate_subparsers.add_parser("table", help="Z-table → CDS view (F-19)")
    gen_table.add_argument("table")
    gen_table.add_argument("--name", help="override the generated view name")
    gen_table.add_argument(
        "--element-style", choices=["preserve", "camel"], default="preserve"
    )
    add_common(gen_table)
    gen_table.set_defaults(func=cmd_generate_table)

    gen_wrapper = generate_subparsers.add_parser("wrapper", help="Z-wrapper (F-20)")
    gen_wrapper.add_argument("view")
    gen_wrapper.add_argument("--name")
    add_common(gen_wrapper)
    gen_wrapper.set_defaults(func=cmd_generate_wrapper)

    gen_mapping = generate_subparsers.add_parser("mapping", help="CDC mapping (F-21)")
    gen_mapping.add_argument("view")
    add_common(gen_mapping)
    gen_mapping.set_defaults(func=cmd_generate_mapping)

    _register_connected_commands(subparsers)
    return parser


def _register_connected_commands(subparsers: argparse._SubParsersAction) -> None:
    """Add the Stage 2 commands, if their dependencies are installed.

    The offline core must stay usable — and installable — without `requests`.
    A missing dependency hides the connected commands rather than breaking the
    whole CLI.
    """
    try:
        from cdcforge import cli_connect
    except ImportError:  # pragma: no cover - depends on the environment
        return
    cli_connect.register(subparsers)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
