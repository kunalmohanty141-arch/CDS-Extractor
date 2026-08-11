"""CLI commands that talk to a system.

Every command here opens a read-only session except ``create`` and ``drop``,
which are the whole of Stage 4 and say so in their names. Those two turn
read-only off explicitly, run the preflight first so the production guard is
deciding on a known client role rather than an unknown one, and refuse outright
if the client turns out to be productive.

Nothing else in this module can write, and nothing here can write to an SAP
object: the namespace and package guards live in ``connect/writer.py``, below
this layer.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from cdcforge.cache import CachedMetadataSource
from cdcforge.store import Store

from cdcforge.connect import endpoints as ep
from cdcforge.connect import sql
from cdcforge.connect.audit import AuditLog
from cdcforge.connect.checkrun import compare, run_checkrun
from cdcforge.connect.preflight import run_preflight
from cdcforge.connect.profile import (
    DEFAULT_PROFILE_DIR,
    ConnectionProfile,
    CredentialError,
)
from cdcforge.connect.session import AdtError, AdtSession
from cdcforge.connect.source import AdtMetadataSource
from cdcforge.rules import validate_object, validate_view
from cdcforge.triage import triage

STORE_DIR = Path.home() / ".cdc-forge"


def _audit_for(profile: ConnectionProfile) -> AuditLog:
    return AuditLog(STORE_DIR / f"{profile.profile_id}.sqlite")


def _open_session(args: argparse.Namespace) -> AdtSession:
    profile = ConnectionProfile.load(args.profile, _profile_dir(args))
    session = AdtSession(profile, _audit_for(profile), read_only=True)
    return session


def _profile_dir(args: argparse.Namespace) -> Path:
    return Path(args.profile_dir) if getattr(args, "profile_dir", None) else DEFAULT_PROFILE_DIR


def _say(message: str = "") -> None:
    """Print something the user is waiting on, and make sure they see it.

    Python line-buffers stdout to a terminal and *block*-buffers it to a pipe,
    so every progress line in this module was invisible the moment anyone
    redirected the output — a twenty-table run printed nothing for twelve
    minutes and then everything at once, and an index build looked hung when
    it was working perfectly.

    Progress nobody sees is worse than no progress: it is the same silence,
    plus the belief that something is broken.
    """
    print(message, flush=True)


def _fail(exc: AdtError) -> int:
    print(exc.render(), file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Profiles and credentials
# ---------------------------------------------------------------------------


def cmd_profile_add(args: argparse.Namespace) -> int:
    reason = (args.tls_reason or "").strip()
    if not args.verify_ssl:
        # §3.3: support verify_ssl false, but force a typed confirmation and
        # stamp every audit record. Do not make insecure the silent default.
        print(
            f"TLS certificate verification will be DISABLED for this profile.\n"
            f"Authentication is HTTP Basic, so {args.user}'s password will be "
            f"sent to {args.host} on every request over a connection nobody "
            f"has authenticated. Anyone on the network path can read it.\n"
            f"Every audit record will be stamped ssl_verified=false."
        )
        typed = input("Type the host name to confirm: ").strip()
        if typed != args.host:
            print("Not confirmed — profile not created.", file=sys.stderr)
            return 1
        # The reason is written into the profile so it travels with it. A
        # profile without one refuses to connect, so asking here is kinder
        # than letting the refusal arrive later.
        while not reason:
            reason = input("Why is verification off? ").strip()
            if not reason:
                print("A reason is required. Ctrl-C to abandon.", file=sys.stderr)

    profile = ConnectionProfile(
        profile_id=args.profile,
        description=args.description or "",
        host=args.host,
        port=args.port,
        protocol="https" if not args.http else "http",
        client=args.client,
        username=args.user,
        verify_ssl=args.verify_ssl,
        ca_bundle_path=args.ca_bundle or "",
        tls_override_reason="" if args.verify_ssl else reason,
    )
    path = profile.save(_profile_dir(args))
    print(f"Wrote {path}")
    print(f"Now store the password:  cdc-forge login --profile {args.profile}")
    return 0


def cmd_profile_list(args: argparse.Namespace) -> int:
    directory = _profile_dir(args)
    names = ConnectionProfile.list_profiles(directory)
    if not names:
        print(f"No profiles in {directory}")
        return 0
    for name in names:
        profile = ConnectionProfile.load(name, directory)
        print(
            f"{profile.profile_id:<20} {profile.base_url}  client {profile.client}  "
            f"user {profile.username}  role {profile.role.label}"
        )
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Prompt for the password and store it in the OS keyring.

    The password is read from the terminal, never from an argument — a command
    line lands in shell history and in the process table.
    """
    profile = ConnectionProfile.load(args.profile, _profile_dir(args))
    password = getpass.getpass(
        f"Password for {profile.username}@{profile.host} client {profile.client}: "
    )
    if not password:
        print("No password entered.", file=sys.stderr)
        return 1
    try:
        profile.store_password(password)
    except CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Stored in the OS keyring under {profile.keyring_key!r}.")
    return 0


# ---------------------------------------------------------------------------
# Connect and inspect
# ---------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> int:
    try:
        session = _open_session(args)
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = run_preflight(session, probe_object=args.probe or "")
    print(report.render())
    print()

    if report.blocking_failures:
        print(f"{len(report.blocking_failures)} blocking failure(s).")
    else:
        print("No blocking failures.")
    if report.client_role.is_productive:
        print("Writes are blocked: this client counts as productive.")
    else:
        print(
            "Writes are possible on this client with `cdc-forge create`, into "
            "$TMP and the customer namespace only. This session is read-only."
        )

    if report.client_role.is_productive:
        print(
            f"\n!!  Client role is {report.client_role.label}. "
            "Treat everything about this system as production."
        )

    if report.system_id:
        session.audit.upsert_system(
            profile_id=session.profile.profile_id,
            host=session.profile.host,
            client=session.profile.client,
            system_id=report.system_id,
            role=report.client_role.value,
            release=report.release,
        )
    session.logoff()
    return 0 if report.ok else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    try:
        session = _open_session(args)
        session.connect()
        source = AdtMetadataSource(session).get_view_source(args.name)
    except AdtError as exc:
        return _fail(exc)
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if source is None:
        print(f"No DDL source returned for {args.name}", file=sys.stderr)
        session.logoff()
        return 1

    if args.out:
        Path(args.out).write_text(source, encoding="utf-8")
        print(f"Wrote {args.out} ({len(source)} characters)")
    else:
        print(source)
    session.logoff()
    return 0


def _cardinality_evidence(session, metadata, name: str, prove: bool):
    """F-14 evidence for one view, and the context it was worked out from.

    The structural half always runs: it costs nothing, needs no data-access
    permission, and proves the common join-on-the-key case outright. The
    empirical half only runs with --prove, because it is the one thing in the
    tool that reads a business table.

    The context comes back so the caller can validate against it. Planning the
    checks needs the dependency stack and so do the rules — returning only the
    evidence meant the stack was walked twice for every view assessed.
    """
    from cdcforge.cardinality import plan_cardinality_checks, summarise
    from cdcforge.connect.prober import evidence_from, run_probes
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext

    source = metadata.get_view_source(name)
    if source is None:
        return {}, None

    ctx = ValidationContext(
        view=parse_ddl(source, name_hint=name),
        metadata=metadata,
        object_meta=metadata.get_object(name),
    )
    plans = plan_cardinality_checks(ctx)
    if not plans:
        return {}, ctx

    if not prove:
        # Structural proofs only. Anything needing data stays unproven, which
        # R-26 reports honestly rather than assuming.
        return {
            p.join_alias: p.evidence()
            for p in plans
            if p.evidence() is not None and p.structural
        }, ctx

    outcomes = run_probes(session, plans)
    print(f"  cardinality — {summarise(plans)}")
    for outcome in outcomes:
        print(f"    {outcome.render()}")
    return evidence_from(outcomes), ctx


def cmd_assess(args: argparse.Namespace) -> int:
    """Run the full rule engine against live metadata.

    Nothing in the engine changes for this — the same thirty rules, driven by
    ADT instead of fixtures.
    """
    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    store = Store(
        args.store or (STORE_DIR / f"{args.profile}.cache.sqlite"),
        profile_id=args.profile,
    )
    metadata = CachedMetadataSource(
        AdtMetadataSource(session), store, refresh=args.refresh
    )

    names = args.name or []
    assessments = []

    if args.prove:
        print(
            "F-14: probing declared to-one joins. This reads COUNT aggregates "
            "from business tables — grouped key values and their row counts, "
            "never row contents.\n"
        )

    for name in names:
        started = time.monotonic()
        evidence, ctx = _cardinality_evidence(session, metadata, name, args.prove)
        if ctx is None:
            assessment = validate_object(
                name, metadata, cardinality_evidence=evidence
            )
        else:
            # Reuse the context the planner already built, so the dependency
            # stack is walked once per view rather than twice.
            ctx.cardinality_evidence = evidence
            assessment = validate_view(ctx.view, context=ctx)
        assessments.append(assessment)
        elapsed = time.monotonic() - started

        print(f"{assessment.verdict.value:<14} {assessment.object_name}  ({elapsed:.1f}s)")
        for result in assessment.problems:
            print(f"    {result.format_line()}")

        if args.checkrun:
            check = run_checkrun(session, name)
            agreement = compare(assessment, check)
            print(f"    SAP checkrun: {check.summary}")
            if agreement.false_pass:
                print(
                    "    !! DIVERGENCE: this tool passed the view and the system "
                    "rejected it. The system wins — log this for rule tuning."
                )
        print()

    if len(assessments) > 1:
        print(triage(assessments).render())
        print()

    print(f"metadata: {metadata.stats.render()}")
    session.logoff()
    return 0


def cmd_checkrun(args: argparse.Namespace) -> int:
    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    result = run_checkrun(session, args.name)
    print(f"{result.object_name}: {result.summary}")
    for message in result.messages:
        where = f" (line {message.line})" if message.line else ""
        print(f"  [{message.severity.value}]{where} {message.text}")
    if args.raw:
        print("\n--- raw ---")
        print(result.raw)
    session.logoff()
    return 0 if result.clean else 1


def cmd_extraction_list(args: argparse.Namespace) -> int:
    """List the views the system itself reports as extraction/CDC enabled.

    `DHCDCVCDSEXTRE` is the CDC/extraction-enabled view list (Appendix D). This
    is the authoritative answer to "which standard view is already enabled for
    <table>" — release-specific, and not something to take from memory or from
    a blog. Appendix D.6 applies: it is an internal table and may not exist on
    every release, so a failure here degrades to a clear message.
    """
    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    try:
        # Filter on the server. Paging first and filtering afterwards reported
        # "0 matching" for ZI_KNA1, which was in the table all along — 7,079
        # rows against a 5,000-row page, so it sorted past the cap.
        result = sql.run_query(
            session,
            sql.extraction_enabled_query(args.filter or ""),
            max_rows=args.limit,
        )
    except AdtError as exc:
        print(
            f"Could not read DHCDCVCDSEXTRE: {exc.message}\n"
            f"  → This is an internal SAP table and may not exist on this "
            f"release. Fall back to 'cdc-forge inventory' plus "
            f"'cdc-forge uses <TABLE>', which derive the same answer from the "
            f"annotations themselves.",
            file=sys.stderr,
        )
        session.logoff()
        return 1

    if not result.parsed:
        print(
            "The endpoint answered but the payload could not be parsed. Run "
            "'cdc-forge raw' to capture it.",
            file=sys.stderr,
        )
        session.logoff()
        return 1

    rows = result.rows
    needle = (args.filter or "").upper()

    print(f"columns: {', '.join(result.columns)}\n")
    for row in rows:
        print("  ".join(f"{k}={v}" for k, v in row.items() if v))
    print(f"\n{len(rows)} entr(ies)" + (f" matching {needle!r}" if needle else ""))
    if not needle and len(rows) >= args.limit:
        print(
            f"Stopped at the {args.limit}-row limit, so this is a page rather "
            f"than the whole table. Use --filter to search it, or raise --limit."
        )
    print(
        "\nExtraction-enabled is not the same as CDC-delta capable. Run "
        "'cdc-forge assess <VIEW> --profile "
        f"{args.profile}' to see which of R-01…R-30 each one actually satisfies."
    )
    session.logoff()
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    """Dump a raw ADT response.

    The wire contract is unpublished and differs between releases. When a
    parser here disagrees with reality, this is how you find out what reality
    actually sent.
    """
    try:
        session = _open_session(args)
        session.connect()
        response = session.request(
            args.path, args.method.upper(), body=args.body, action="raw"
        )
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    print(f"HTTP {response.status}  ({response.elapsed_ms} ms)")
    for key, value in sorted(response.headers.items()):
        print(f"  {key}: {value}")
    print()
    print(response.text[: args.limit])
    session.logoff()
    return 0


def cmd_endpoints(args: argparse.Namespace) -> int:
    for endpoint in ep.ALL_ENDPOINTS:
        print(
            f"{endpoint.access.value:<6} {endpoint.method:<5} {endpoint.path}\n"
            f"       {endpoint.description}"
        )
    print(
        "\nThese paths are reconstructed, not published by SAP. Verify against "
        "the target release."
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    profile = ConnectionProfile.load(args.profile, _profile_dir(args))
    log = _audit_for(profile)
    entries = log.entries(limit=args.limit)
    if not entries:
        print("No audit entries.")
        return 0
    for entry in reversed(entries):
        print(
            f"{entry['timestamp']}  {entry['action']:<18} "
            f"{entry['method'] or '':<5} {entry['http_status'] or '-':<4} "
            f"{entry['elapsed_ms'] or '-':>6}ms  "
            f"ssl={'y' if entry['ssl_verified'] else 'N'} "
            f"ro={'y' if entry['read_only'] else 'n'}  {entry['path'] or ''}"
        )
    print(f"\n{log.count()} record(s) total in {log.path}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    """Stage 4 — create a CDS view from a DDL file.

    Reads the source from a file rather than an argument on purpose: generated
    DDL is multi-line and quoting it through a shell is how a stray character
    ends up in an activated object.
    """
    from cdcforge.connect.preflight import run_preflight
    from cdcforge.connect.transport import check_transport, create_request
    from cdcforge.connect.writer import LOCAL_PACKAGE, WritePolicy, create_view

    ddl = Path(args.file).read_text(encoding="utf-8")

    try:
        profile = ConnectionProfile.load(args.profile, _profile_dir(args))
        # Writes need an explicit opt-out from read-only. It is not a default
        # anywhere, including here.
        session = AdtSession(profile, _audit_for(profile), read_only=False)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    # The production guard treats an unknown client role as productive, and the
    # preflight is what establishes the role. Running it here means the refusal
    # says "this client is productive" rather than "I never looked".
    report = run_preflight(session)
    print(
        f"{report.system_id or args.profile} client {profile.client} · "
        f"role {report.client_role.label}"
    )
    if report.client_role.is_productive:
        print(
            f"\nRefusing to write: the client role is "
            f"{report.client_role.label}.\n"
            f"Unknown counts as productive. Nothing was sent.",
            file=sys.stderr,
        )
        session.logoff()
        return 2

    # Never a policy built from --package. Deriving the allowlist from the
    # argument it is supposed to check makes the guard unable to refuse
    # anything: an earlier cut did exactly that, and `--package SD` sailed past
    # it and was only stopped by SAP answering 409.
    #
    # Writing outside $TMP therefore flips a flag rather than supplying a
    # value, and the namespace check stays between the argument and the system
    # either way.
    transportable = args.package.upper() != LOCAL_PACKAGE
    policy = WritePolicy(allow_transportable=transportable)

    transport = (args.transport or "").upper()
    if transport and args.new_transport:
        print(
            "--transport and --new-transport are mutually exclusive.",
            file=sys.stderr,
        )
        session.logoff()
        return 2

    if args.new_transport:
        # Ask before creating. A request made for a write that is then refused
        # — bad name, object already there — is an empty request nobody asked
        # for, sitting in the user's SE09 with the tool's description on it.
        need = check_transport(session, args.name, args.package)
        print(need.render())
        if need.unreadable:
            print("\nNothing was created.", file=sys.stderr)
            session.logoff()
            return 2
        if not need.required:
            print("No request needed here; --new-transport ignored.")
        else:
            transport, error = create_request(
                session, args.package, args.new_transport
            )
            if error:
                print(
                    f"\nCould not create a transport request: {error}",
                    file=sys.stderr,
                )
                session.logoff()
                return 2
            print(f"created transport request {transport}  {args.new_transport}")

    result = create_view(
        session,
        args.name,
        ddl,
        package=args.package,
        description=args.description,
        policy=policy,
        activate=not args.no_activate,
        transport=transport,
    )
    print()
    print(result.render())

    if result.check is not None and result.check.messages:
        print("\nSAP's check said:")
        for message in result.check.messages[:12]:
            where = f" (line {message.line})" if message.line else ""
            code = f" [{message.code}]" if message.code else ""
            print(f"  {message.severity.value} {message.text}{where}{code}")

    session.logoff()
    return 0 if result.ok else 1


def cmd_plan(args: argparse.Namespace) -> int:
    """Assess a list of tables and write a decision sheet.

    Read-only. The output is a spreadsheet with a suggestion in every row and
    two editable columns — the point being that the choice leaves the tool and
    comes back later, made by someone who knows things the system does not.
    """
    from cdcforge.decisions import SKIP, Decision, suggest, write_plan
    from cdcforge.deltaindex import load_or_build
    from cdcforge.estate import survey
    from cdcforge.successors import find_candidates

    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    store = Store(
        args.store or (STORE_DIR / f"{args.profile}.cache.sqlite"),
        profile_id=args.profile,
    )
    metadata = CachedMetadataSource(
        AdtMetadataSource(session), store, refresh=args.refresh
    )

    names = [n.upper() for n in (args.name or [])]
    if args.from_file:
        names += [
            line.strip().upper()
            for line in Path(args.from_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not names:
        print("Nothing to plan — name at least one table.", file=sys.stderr)
        session.logoff()
        return 2

    # What already exists, before anything is suggested. Without this a second
    # run over the same list proposes rebuilding everything the first built.
    estate = None
    if not args.ignore_existing:
        estate = survey(metadata)
        if estate.surveyed:
            print(
                f"{len(estate.objects)} custom extraction view(s) already here."
            )
        else:
            print(
                "The system did not report its extraction-enabled views, so "
                "nothing can be said about what already exists."
            )

    # SAP's own delta views, indexed by the table they feed. Built once per
    # system and cached: the where-used indexes cannot see view entities, so
    # without this the search misses C_*DEX content entirely.
    def _index_progress(done: int, total: int) -> None:
        if done == 0:
            _say(
                f"Indexing SAP's {total} delta view(s) down to the tables they "
                f"feed. One-off, then cached — the where-used indexes cannot "
                f"see view entities, so without this the search misses C_*DEX "
                f"content entirely."
            )
        else:
            _say(f"  {done}/{total} resolved…")

    delta_index = load_or_build(
        metadata, store, refresh=args.refresh, progress=_index_progress
    )
    if delta_index.available:
        print(delta_index.render())
    else:
        print(
            "The system did not report which views carry delta, so SAP's own "
            "delta views cannot be indexed — the search will see only what "
            "the where-used indexes report."
        )

    print(
        f"\nSearching every VDM layer over {len(names)} object(s). The first "
        f"look at a table reads its whole reader graph and is the slow one.\n"
    )

    decisions = []
    failed = 0
    for index, name in enumerate(names, 1):
        started = time.monotonic()
        try:
            # Does it exist at all? A name nobody can find yields no candidates,
            # and no candidates reads as "nothing to build on" — so the sheet
            # confidently proposed BUILD over a table that is not there.
            # Measured with a deliberate typo: NOSUCHTABLE came back BUILD.
            # A misspelling in a fifty-row list has to look like a misspelling.
            table = metadata.get_table(name)
            if table is None and metadata.get_view_source(name) is None:
                failed += 1
                decision = Decision(
                    object_name=name,
                    action=SKIP,
                    note="not found",
                    why=(
                        "No table and no CDS view of this name in this client. "
                        "Check the spelling, and that your user can read it."
                    ),
                    suggested_action=SKIP,
                )
                decisions.append(decision)
                _say(f"  [{index}/{len(names)}] {name:<22} NOT FOUND")
                continue

            report = find_candidates(
                metadata, name, extra_names=delta_index.covering(name)
            )
            decision = suggest(name, report, estate=estate, table=table)
        except Exception as exc:
            # One object's failure is that object's result, never the batch's.
            # A fifty-table plan is an hour of running, and losing forty-nine
            # good answers to one bad table is the worst way to spend it. The
            # row survives, says what happened, and the sheet still arrives.
            failed += 1
            decision = Decision(
                object_name=name,
                action=SKIP,
                note="could not be examined",
                why=f"{type(exc).__name__}: {exc}",
                suggested_action=SKIP,
            )
            decisions.append(decision)
            _say(f"  [{index}/{len(names)}] {name:<22} FAILED  {exc}")
            continue

        decisions.append(decision)
        elapsed = time.monotonic() - started
        detail = decision.base or name
        _say(
            f"  [{index}/{len(names)}] {name:<22} {decision.action:<6} "
            f"{detail:<34} ({elapsed:.1f}s)"
        )
        # Show the shortlist, not just the winner. The choice between them is
        # usually a real trade-off nobody but the reader can make: EKET's two
        # delta-carrying views cover 36% and 18% and are each filtered to one
        # document category, while the two that cover ~47% carry no delta at
        # all and would need wrapping. Printing one name hides that entirely.
        for rank, candidate in enumerate(report.choices(limit=4)[:4], 1):
            marks = []
            if candidate.carries_cdc:
                marks.append("delta")
            if candidate.row_filtered:
                marks.append("filtered rows")
            if candidate.api_state in ("C1", "RELEASED"):
                marks.append("released")
            chosen = "->" if candidate.view == decision.base else "  "
            _say(
                f"     {chosen} {rank}. {candidate.view:<34} "
                f"{candidate.coverage:>5.0%}"
                + (f"  [{', '.join(marks)}]" if marks else "")
            )
        if report.from_delta_index:
            _say(
                f"        found via the delta index, invisible to where-used: "
                f"{', '.join(report.from_delta_index[:4])}"
            )
        if decision.existing:
            _say(f"        {decision.existing}")

    target = write_plan(decisions, args.out)
    print(f"\nWrote {target}")
    if failed:
        print(
            f"{failed} object(s) were not found or could not be examined, and "
            f"are marked SKIP with the reason in the Why column."
        )
    print(
        "Edit the Action, Base, Target name and Note columns, then:\n"
        f"  cdcforge apply --profile {args.profile} --file {target} "
        f"--out-dir ddl/"
    )
    session.logoff()
    return 0


def cmd_estate(args: argparse.Namespace) -> int:
    """What custom extraction views already exist, and what they feed.

    Read-only, and worth having on its own: a consultant arriving at a system
    someone else has worked on needs this before deciding anything.
    """
    from cdcforge.estate import survey

    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    store = Store(
        args.store or (STORE_DIR / f"{args.profile}.cache.sqlite"),
        profile_id=args.profile,
    )
    metadata = CachedMetadataSource(
        AdtMetadataSource(session), store, refresh=args.refresh
    )

    found = survey(metadata)
    if not found.surveyed:
        print(
            "The system did not report its extraction-enabled views, so "
            "nothing can be said about what already exists. That is not the "
            "same as nothing being there."
        )
        session.logoff()
        return 1

    print(found.render())
    session.logoff()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Are these objects still feeding delta? Read-only.

    With no names, verifies everything the estate survey finds — which is the
    question worth asking on a landscape nobody has looked at for a while.
    """
    from cdcforge.estate import survey
    from cdcforge.verify import verify

    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    store = Store(
        args.store or (STORE_DIR / f"{args.profile}.cache.sqlite"),
        profile_id=args.profile,
    )
    metadata = CachedMetadataSource(
        AdtMetadataSource(session), store, refresh=args.refresh
    )

    names = [n.upper() for n in (args.name or [])]
    if not names:
        found = survey(metadata)
        names = [o.name for o in found.objects]
        if not names:
            print("Nothing to verify — no custom extraction views found.")
            session.logoff()
            return 0
        print(f"Verifying {len(names)} custom extraction view(s).\n")

    # One query for the batch. survey() has already refreshed this list, and
    # the whole point of verifying is to read what is true now.
    metadata.forget_extraction_enabled()
    delta = metadata.extraction_enabled_views()

    report = verify(metadata, names, delta_supported=delta, check_rules=not args.fast)
    print(report.render())
    session.logoff()
    return 1 if report.failed else 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Read a decision sheet back and do what it says.

    The sheet is validated whole before anything runs. A batch that generates
    eleven objects and stops on a typo in the twelfth leaves the user to work
    out which eleven, and that is a worse outcome than refusing the lot.
    """
    from cdcforge.decisions import BUILD, USE, PlanError, load
    from cdcforge.generator.wrapper import generate_wrapper
    from cdcforge.generator.ztable import generate_view_for_table
    from cdcforge.parsing.ddl import parse_ddl
    from cdcforge.rules import ValidationContext, validate_view

    try:
        summary = load(args.file)
    except PlanError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(summary.render())
    if not summary.ok:
        print(
            f"\n{len(summary.problems)} problem(s) — nothing was generated. "
            f"Fix the sheet and run again.",
            file=sys.stderr,
        )
        return 2

    todo = [d for d in summary.decisions if d.generates]
    using = summary.by_action(USE)
    if not todo and not using:
        print("\nNothing to generate.")
        return 0

    try:
        session = _open_session(args)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    store = Store(
        args.store or (STORE_DIR / f"{args.profile}.cache.sqlite"),
        profile_id=args.profile,
    )
    metadata = CachedMetadataSource(
        AdtMetadataSource(session), store, refresh=args.refresh
    )

    # A USE row is a claim: "replicate this, it already carries delta". Saying
    # that without checking is the one place this tool would hand over an
    # unverified promise, and it is the easiest of all the checks to make.
    if using:
        from cdcforge.verify import verify

        metadata.forget_extraction_enabled()
        delta = metadata.extraction_enabled_views()
        report = verify(
            metadata,
            [d.base for d in using if d.base],
            delta_supported=delta,
            check_rules=False,
        )
        print(f"\n{len(using)} object(s) marked USE — replicate these directly:")
        for result in report.results:
            source = next(
                (d.object_name for d in using if d.base == result.name), ""
            )
            print(f"  {result.status:<14} {result.name}  (for {source})")
            for note in result.notes:
                print(f"      {note}")
        if report.failed:
            print(
                f"  !! {len(report.failed)} of these do not currently carry "
                f"delta. Replicating them gives an initial load and no "
                f"changes after it."
            )

    if not todo:
        session.logoff()
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    generated: list[tuple[str, str]] = []
    refused = 0
    print(f"\nGenerating {len(todo)} object(s) into {out_dir}\n")

    for decision in todo:
        try:
            if decision.action == BUILD:
                table = metadata.get_table(decision.object_name)
                if table is None:
                    print(f"  REFUSED  {decision.object_name} — table not found")
                    refused += 1
                    continue
                result = generate_view_for_table(
                    table, name=decision.target or None
                )
            else:  # WRAP
                source = metadata.get_view_source(decision.base)
                if source is None:
                    print(
                        f"  REFUSED  {decision.object_name} — "
                        f"{decision.base} not found"
                    )
                    refused += 1
                    continue
                ctx = ValidationContext(
                    view=parse_ddl(source, name_hint=decision.base),
                    metadata=metadata,
                    object_meta=metadata.get_object(decision.base),
                )
                result = generate_wrapper(
                    ctx,
                    validate_view(ctx.view, context=ctx),
                    name=decision.target or None,
                )
        except Exception as exc:
            # Same rule as the plan loop: one row's failure is that row's
            # result. Everything already generated stays on disk and the rest
            # of the sheet still runs.
            print(f"  FAILED   {decision.object_name} — {type(exc).__name__}: {exc}")
            refused += 1
            continue

        if result.refused_because:
            print(f"  REFUSED  {decision.object_name} — {result.refused_because}")
            refused += 1
            continue

        path = out_dir / f"{result.name}.ddl"
        path.write_text(result.ddl, encoding="utf-8")
        generated.append((result.name, result.ddl))
        note = f"  ({len(result.warnings)} warning(s))" if result.warnings else ""
        print(f"  {result.name:<32} from {decision.object_name}{note}")
        for warning in result.warnings[:3]:
            print(f"      warning: {warning}")

    print(f"\n{len(generated)} generated, {refused} refused.")
    session.logoff()
    if not args.create or not generated:
        print(f"DDL is in {out_dir}. Add --create to create them in the system.")
        return 0 if not refused else 1

    return _create_batch(args, generated, refused)


def _create_batch(
    args: argparse.Namespace,
    generated: list[tuple[str, str]],
    refused: int,
) -> int:
    """Create everything the sheet generated, in one session and one request.

    A second, separate session. The one that read the metadata is read-only and
    stays that way — the same split the UI makes, so no amount of confusion
    upstream can turn an assessment into a write.
    """
    from cdcforge.connect.preflight import run_preflight
    from cdcforge.connect.transport import check_transport, create_request
    from cdcforge.connect.writer import LOCAL_PACKAGE, WritePolicy, create_view

    try:
        profile = ConnectionProfile.load(args.profile, _profile_dir(args))
        session = AdtSession(profile, _audit_for(profile), read_only=False)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    report = run_preflight(session)
    print(f"\n{report.system_id or args.profile} · role {report.client_role.label}")
    if report.client_role.is_productive:
        print(
            "Refusing to write: this client counts as productive.", file=sys.stderr
        )
        session.logoff()
        return 2

    package = (args.package or LOCAL_PACKAGE).upper()
    transport = (args.transport or "").upper()

    if args.new_transport:
        need = check_transport(session, generated[0][0], package)
        print(need.render())
        if need.unreadable:
            session.logoff()
            return 2
        if need.required:
            # One request for the whole batch. A request per object would be
            # a dozen entries in SE09 describing one piece of work.
            transport, error = create_request(session, package, args.new_transport)
            if error:
                print(f"Could not create a transport request: {error}", file=sys.stderr)
                session.logoff()
                return 2
            print(f"created transport request {transport}  {args.new_transport}")

    policy = WritePolicy(allow_transportable=package != LOCAL_PACKAGE)
    failures = 0
    print()
    for name, ddl in generated:
        result = create_view(
            session, name, ddl,
            package=package,
            description="Generated by CDC Forge",
            policy=policy,
            activate=not args.no_activate,
            transport=transport,
        )
        print(result.render())
        if not result.ok:
            failures += 1

    print(f"\n{len(generated) - failures} created, {failures} failed.")
    session.logoff()
    return 0 if not failures and not refused else 1


def cmd_transport(args: argparse.Namespace) -> int:
    """What would writing into this package require?

    Read-only, and it exists because the answer is worth having *before* a run
    rather than as a refusal in the middle of one — particularly the case where
    a request is needed and the user has none open, which is a trip to SE09 or
    a ``--new-transport`` and is better discovered early.
    """
    from cdcforge.connect.transport import check_transport

    try:
        profile = ConnectionProfile.load(args.profile, _profile_dir(args))
        session = AdtSession(profile, _audit_for(profile), read_only=True)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    need = check_transport(session, args.name, args.package)
    print(need.render())
    if need.unreadable and args.raw:
        print(f"\nraw:\n{need.raw}")
    session.logoff()
    return 0 if not need.unreadable else 1


def cmd_activate(args: argparse.Namespace) -> int:
    """Activate an object that already exists, and report what SAP said."""
    from cdcforge.connect.preflight import run_preflight
    from cdcforge.connect.writer import activate_view

    try:
        profile = ConnectionProfile.load(args.profile, _profile_dir(args))
        session = AdtSession(profile, _audit_for(profile), read_only=False)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    report = run_preflight(session)
    print(f"{report.system_id or args.profile} client {profile.client} · "
          f"role {report.client_role.label}\n")
    if report.client_role.is_productive:
        print("Refusing to activate: this client counts as productive.",
              file=sys.stderr)
        session.logoff()
        return 2

    failures = 0
    for name in args.name:
        result = activate_view(session, name)
        print(result.render())
        if result.check is not None and result.check.messages:
            for message in result.check.messages[:8]:
                where = f" (line {message.line})" if message.line else ""
                print(f"    {message.severity.value} {message.text}{where}")
        if not result.ok:
            failures += 1
        print()

    session.logoff()
    return 1 if failures else 0


def cmd_drop(args: argparse.Namespace) -> int:
    """Delete a view this tool created. Same guards as creating one."""
    from cdcforge.connect.preflight import run_preflight
    from cdcforge.connect.writer import WritePolicy, delete_view

    try:
        profile = ConnectionProfile.load(args.profile, _profile_dir(args))
        session = AdtSession(profile, _audit_for(profile), read_only=False)
        session.connect()
    except (FileNotFoundError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AdtError as exc:
        return _fail(exc)

    report = run_preflight(session)
    if report.client_role.is_productive:
        print(
            f"Refusing to delete: client role is {report.client_role.label}.",
            file=sys.stderr,
        )
        session.logoff()
        return 2

    if not args.yes:
        typed = input(f"Type {args.name.upper()} to confirm deletion: ").strip()
        if typed.upper() != args.name.upper():
            print("Not confirmed — nothing deleted.", file=sys.stderr)
            session.logoff()
            return 2

    # allow_transportable, because the object's package is whatever it already
    # is — refusing to delete something the tool created would be perverse.
    result = delete_view(
        session,
        args.name,
        policy=WritePolicy(allow_transportable=True),
        transport=(args.transport or "").upper(),
    )
    print(result.render())
    session.logoff()
    return 0 if result.deleted else 1


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--profile", required=True, help="connection profile id")
        parser.add_argument("--profile-dir", help="override the profile directory")

    profile = subparsers.add_parser("profile", help="manage connection profiles")
    profile_subs = profile.add_subparsers(dest="profile_command", required=True)

    add = profile_subs.add_parser("add", help="create a connection profile")
    add.add_argument("--profile", required=True)
    add.add_argument("--host", required=True)
    add.add_argument("--client", required=True)
    add.add_argument("--user", required=True)
    add.add_argument("--port", type=int, default=44300)
    add.add_argument("--description", default="")
    add.add_argument("--ca-bundle", default="")
    add.add_argument("--http", action="store_true", help="use http instead of https")
    add.add_argument(
        "--insecure",
        dest="verify_ssl",
        action="store_false",
        help="disable TLS verification (requires typed confirmation and a "
        "stated reason)",
    )
    add.add_argument(
        "--tls-reason",
        default="",
        help="why verification is off. Required with --insecure; prompted for "
        "if omitted, and written into the profile so it travels with it.",
    )
    add.add_argument("--profile-dir")
    add.set_defaults(func=cmd_profile_add, verify_ssl=True)

    listing = profile_subs.add_parser("list", help="list profiles")
    listing.add_argument("--profile-dir")
    listing.set_defaults(func=cmd_profile_list)

    login = subparsers.add_parser("login", help="store the password in the OS keyring")
    common(login)
    login.set_defaults(func=cmd_login)

    pre = subparsers.add_parser("preflight", help="connect and run the checklist (F-02)")
    common(pre)
    pre.add_argument("--probe", help="a CDS view name to test source reads with")
    pre.set_defaults(func=cmd_preflight)

    fetch = subparsers.add_parser("fetch", help="read a CDS DDL source")
    common(fetch)
    fetch.add_argument("name")
    fetch.add_argument("--out", help="write to a file instead of stdout")
    fetch.set_defaults(func=cmd_fetch)

    assess = subparsers.add_parser("assess", help="validate live objects (R-01…R-30)")
    common(assess)
    assess.add_argument("name", nargs="+")
    assess.add_argument(
        "--checkrun", action="store_true", help="also ask SAP's own check (F-15)"
    )
    assess.add_argument("--store", help="cache file (default: ~/.cdc-forge/<profile>.cache.sqlite)")
    assess.add_argument(
        "--refresh", action="store_true", help="ignore the cache and re-read from the system"
    )
    assess.add_argument(
        "--prove",
        action="store_true",
        help="F-14: prove declared to-one joins against real data. Reads COUNT "
        "aggregates from business tables — key values and row counts, never row "
        "contents. Structural proofs run regardless and read nothing.",
    )
    assess.set_defaults(func=cmd_assess)

    create = subparsers.add_parser(
        "create", help="create a CDS view in the system (Stage 4)"
    )
    common(create)
    create.add_argument("name", help="the object name — must be Z… or Y…")
    create.add_argument("--file", required=True, help="file holding the DDL source")
    create.add_argument(
        "--package",
        default="$TMP",
        help="target package, default $TMP — local, non-transportable, and the "
        "only one that needs no transport request. Any other target must be a "
        "customer (Z…/Y…) package and must supply --transport or "
        "--new-transport; an SAP package is refused before the request is "
        "built.",
    )
    create.add_argument(
        "--transport",
        default="",
        help="record the object in this existing transport request. Required "
        "for any package where CTS reports change recording.",
    )
    create.add_argument(
        "--new-transport",
        default="",
        metavar="DESCRIPTION",
        help="create a workbench request with this description and record the "
        "object in it. Only if CTS says one is needed.",
    )
    create.add_argument("--description", default="", help="object description")
    create.add_argument(
        "--no-activate",
        action="store_true",
        help="create and upload the source but leave the object inactive, to "
        "activate by hand. Useful when the user lacks the activation "
        "authorisation. SAP's syntax check still runs and a rejected source is "
        "still rolled back.",
    )
    create.set_defaults(func=cmd_create)

    plan = subparsers.add_parser(
        "plan",
        help="assess tables and write a decision sheet for a human to edit",
    )
    common(plan)
    plan.add_argument("name", nargs="*", help="table or view names")
    plan.add_argument(
        "--from-file", help="a text file of names, one per line (# comments ok)"
    )
    plan.add_argument("--out", default="plan.xlsx", help="the workbook to write")
    plan.add_argument("--store", help="cache file")
    plan.add_argument("--refresh", action="store_true", help="ignore the cache")
    plan.add_argument(
        "--ignore-existing",
        action="store_true",
        help="do not look at what is already built. Off by default: a plan "
        "that ignores the existing estate proposes rebuilding it.",
    )
    plan.set_defaults(func=cmd_plan)

    estate = subparsers.add_parser(
        "estate", help="list the custom extraction views already in the system"
    )
    common(estate)
    estate.add_argument("--store", help="cache file")
    estate.add_argument("--refresh", action="store_true", help="ignore the cache")
    estate.set_defaults(func=cmd_estate)

    verify = subparsers.add_parser(
        "verify",
        help="are these objects still carrying delta? (read-only)",
    )
    common(verify)
    verify.add_argument(
        "name",
        nargs="*",
        help="objects to check. With none, checks everything the estate holds.",
    )
    verify.add_argument("--store", help="cache file")
    verify.add_argument("--refresh", action="store_true", help="ignore the cache")
    verify.add_argument(
        "--fast",
        action="store_true",
        help="skip the rule set — existence and the system's delta flag only",
    )
    verify.set_defaults(func=cmd_verify)

    apply_ = subparsers.add_parser(
        "apply", help="read a decision sheet back and generate what it says"
    )
    common(apply_)
    apply_.add_argument("--file", required=True, help="the edited decision sheet")
    apply_.add_argument("--out-dir", default="ddl", help="where to write the DDL")
    apply_.add_argument("--store", help="cache file")
    apply_.add_argument("--refresh", action="store_true", help="ignore the cache")
    apply_.add_argument(
        "--create",
        action="store_true",
        help="also create the generated objects in the system. Without this "
        "the DDL is written to --out-dir and nothing is sent.",
    )
    apply_.add_argument("--package", default="$TMP", help="target package")
    apply_.add_argument("--transport", default="", help="existing request")
    apply_.add_argument(
        "--new-transport",
        default="",
        metavar="DESCRIPTION",
        help="create one request for the whole batch, if CTS says one is needed",
    )
    apply_.add_argument(
        "--no-activate", action="store_true", help="leave the objects inactive"
    )
    apply_.set_defaults(func=cmd_apply)

    transport = subparsers.add_parser(
        "transport",
        help="ask CTS what writing into a package would require (read-only)",
    )
    common(transport)
    transport.add_argument("package", help="the target package, e.g. ZDSP_EXTRACTION")
    transport.add_argument(
        "--name",
        default="ZCDCFORGE_PROBE",
        help="the object name to ask about — nothing is created",
    )
    transport.add_argument("--raw", action="store_true", help="dump the CTS response")
    transport.set_defaults(func=cmd_transport)

    activate = subparsers.add_parser(
        "activate", help="activate objects that already exist (Stage 4)"
    )
    common(activate)
    activate.add_argument("name", nargs="+")
    activate.set_defaults(func=cmd_activate)

    drop = subparsers.add_parser(
        "drop", help="delete a view this tool created (Stage 4)"
    )
    common(drop)
    drop.add_argument("name")
    drop.add_argument(
        "--yes", action="store_true", help="skip the typed confirmation"
    )
    drop.add_argument(
        "--transport",
        default="",
        help="record the deletion here. A deletion is a change like any other, "
        "so an object in a transportable package needs one — without it ADT "
        "answers HTTP 400. Not needed for $TMP.",
    )
    drop.set_defaults(func=cmd_drop)

    check = subparsers.add_parser("checkrun", help="run SAP's pre-check (F-15)")
    common(check)
    check.add_argument("name")
    check.add_argument("--raw", action="store_true")
    check.set_defaults(func=cmd_checkrun)

    extraction = subparsers.add_parser(
        "extraction-list",
        help="list the views the system reports as extraction/CDC enabled",
    )
    common(extraction)
    extraction.add_argument("--filter", help="substring to match, e.g. MATERIAL")
    extraction.add_argument("--limit", type=int, default=5000)
    extraction.set_defaults(func=cmd_extraction_list)

    raw = subparsers.add_parser("raw", help="dump a raw ADT response")
    common(raw)
    raw.add_argument("path")
    raw.add_argument("--method", default="GET")
    raw.add_argument("--body")
    raw.add_argument("--limit", type=int, default=4000)
    raw.set_defaults(func=cmd_raw)

    endpoints = subparsers.add_parser("endpoints", help="show the ADT endpoint map")
    endpoints.set_defaults(func=cmd_endpoints)

    audit = subparsers.add_parser("audit", help="show the audit log (F-32)")
    common(audit)
    audit.add_argument("--limit", type=int, default=40)
    audit.set_defaults(func=cmd_audit)
