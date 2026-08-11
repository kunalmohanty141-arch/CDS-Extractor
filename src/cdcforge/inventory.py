"""F-05 / F-06 — the inventory sweep.

Harvest every CDS view and every table, validate what is found, and cache the
lot. The first run against a large landscape can take hours; every run after it
should be instant.

Two things the specification is specific about, both about not lying to the
user during a long wait:

    Show a progress bar with a realistic ETA — this run can take hours on a
    large system and users will assume it has hung.

and the cached-versus-fresh choice, which is offered here as ``refresh`` rather
than being decided silently.

Works against any ``MetadataSource``: fixtures for a demo, ADT for a real
system. The scanner does not know or care which.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from cdcforge.cds import (
    ANN_CDC_AUTOMATIC,
    ANN_CDC_MAPPING,
    ANN_DELTA_BY_ELEMENT,
    ANN_EXTRACTION_ENABLED,
)
from cdcforge.metadata.base import MetadataSource
from cdcforge.model import Assessment
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.parsing.nodes import ParsedView
from cdcforge.rules import RuleConfig, SubscriptionState, validate_view
from cdcforge.rules.stack import NodeKind, ViewStack, resolve_stack
from cdcforge.store import Store, ViewRecord
from cdcforge.triage import TriageSummary, classify


@dataclass
class ScanProgress:
    done: int
    total: int
    current: str
    elapsed_s: float
    eta_s: float | None
    cached: int
    fetched: int

    @property
    def percent(self) -> float:
        return (self.done / self.total * 100) if self.total else 0.0

    def render(self) -> str:
        eta = "—" if self.eta_s is None else _duration(self.eta_s)
        return (
            f"[{self.done:>5}/{self.total:<5}] {self.percent:5.1f}%  "
            f"elapsed {_duration(self.elapsed_s)}  eta {eta}  {self.current}"
        )


ProgressCallback = Callable[[ScanProgress], None]


@dataclass
class ScanResult:
    assessments: list[Assessment] = field(default_factory=list)
    summary: TriageSummary = field(default_factory=TriageSummary)
    cached: int = 0
    fetched: int = 0
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    """Objects whose source could not be read, with the reason.

    Kept separate from the verdicts. "I could not read this" is not a verdict
    about the view, and folding it in with UNPARSEABLE would blur a system or
    authorization problem into a statement about the DDL.
    """

    tables_scanned: int = 0
    elapsed_s: float = 0.0

    @property
    def total(self) -> int:
        return len(self.assessments)

    def render(self) -> str:
        lines = [
            f"Scanned {self.total} view(s) in {_duration(self.elapsed_s)} "
            f"({self.fetched} fetched, {self.cached} from cache)",
            f"Tables recorded: {self.tables_scanned}",
        ]
        if self.unreadable:
            lines.append(f"Unreadable: {len(self.unreadable)}")
            for name, reason in self.unreadable[:10]:
                lines.append(f"    {name}: {reason}")
            if len(self.unreadable) > 10:
                lines.append(f"    (+{len(self.unreadable) - 10} more)")
        lines.append("")
        lines.append(self.summary.render())
        return "\n".join(lines)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
# Annotation state, for the inventory columns
# ---------------------------------------------------------------------------


def describe_extraction(view: ParsedView) -> tuple[bool, str, str]:
    """(extraction enabled, delta method, CDC annotation type)."""
    annotations = view.annotations
    if annotations is None:
        return False, "none", "none"

    enabled = annotations.is_true(ANN_EXTRACTION_ENABLED)

    if annotations.is_true(ANN_CDC_AUTOMATIC):
        cdc_type = "automatic"
    elif isinstance(annotations.get(ANN_CDC_MAPPING), list):
        cdc_type = "mapping"
    else:
        cdc_type = "none"

    if cdc_type != "none":
        delta = "CDC"
    elif annotations.has(ANN_DELTA_BY_ELEMENT):
        delta = "byElement"
    else:
        delta = "none"

    return enabled, delta, cdc_type


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class InventoryScanner:
    """Harvest, validate and cache."""

    def __init__(
        self,
        metadata: MetadataSource,
        store: Store,
        *,
        config: RuleConfig | None = None,
        progress: ProgressCallback | None = None,
        subscription_state: SubscriptionState = SubscriptionState.UNKNOWN,
    ) -> None:
        self.metadata = metadata
        self.store = store
        self.config = config or RuleConfig()
        self.progress = progress
        self.subscription_state = subscription_state

    def _report(self, progress: ScanProgress) -> None:
        if self.progress is not None:
            self.progress(progress)

    # -- views -------------------------------------------------------------
    def scan(
        self,
        names: Iterable[str] | None = None,
        *,
        refresh: bool = False,
        use_cached_verdicts: bool = True,
        limit: int | None = None,
        include_tables: bool = True,
    ) -> ScanResult:
        """Scan the named views, or everything the source knows about."""
        targets = list(names) if names is not None else self.metadata.list_views()
        if limit is not None:
            targets = targets[:limit]

        result = ScanResult()
        started = time.monotonic()
        total = len(targets)

        for index, name in enumerate(targets, start=1):
            elapsed = time.monotonic() - started
            self._report(
                ScanProgress(
                    done=index - 1, total=total, current=name, elapsed_s=elapsed,
                    eta_s=_estimate(elapsed, index - 1, total),
                    cached=result.cached, fetched=result.fetched,
                )
            )
            self._scan_one(name, result, refresh, use_cached_verdicts)

        result.elapsed_s = time.monotonic() - started
        self._report(
            ScanProgress(
                done=total, total=total, current="done", elapsed_s=result.elapsed_s,
                eta_s=0, cached=result.cached, fetched=result.fetched,
            )
        )

        for assessment in result.assessments:
            result.summary.add(assessment)

        if include_tables:
            result.tables_scanned = self.scan_tables()
            result.summary.bare_tables = [
                row["table_name"] for row in self.store.tables(bare_only=True)
            ]

        self.store.mark_scanned()
        return result

    def _scan_one(
        self, name: str, result: ScanResult, refresh: bool, use_cached_verdicts: bool
    ) -> None:
        source, from_cache = self._read_source(name, refresh)
        if source is None:
            result.unreadable.append((name.upper(), "no DDL source returned"))
            return

        if from_cache:
            result.cached += 1
        else:
            result.fetched += 1

        digest = self.store.put_source(name, source)
        view = parse_ddl(source, name_hint=name)

        assessment = (
            self.store.cached_assessment(name, digest) if use_cached_verdicts else None
        )
        if assessment is None:
            assessment = validate_view(
                view,
                metadata=self.metadata,
                config=self.config,
                subscription_state=self.subscription_state,
            )
            self.store.put_verdicts(assessment, digest)
        else:
            assessment.source_text = source
            assessment.unparseable = view.has_fatal_issue
            assessment.parse_issues = list(view.issues)

        result.assessments.append(assessment)
        self._record_view(name, view, assessment)

    def _read_source(self, name: str, refresh: bool) -> tuple[str | None, bool]:
        if not refresh:
            cached = self.store.get_source(name)
            if cached is not None:
                return cached, True
        return self.metadata.get_view_source(name), False

    def _record_view(self, name: str, view: ParsedView, assessment: Assessment) -> None:
        enabled, delta, cdc_type = describe_extraction(view)
        stack = (
            assessment.stack
            if isinstance(assessment.stack, ViewStack)
            else resolve_stack(view, self.metadata, max_depth=self.config.max_stack_depth)
        )
        obj = self.metadata.get_object(name)

        self.store.put_view(
            ViewRecord(
                ddl_name=name.upper(),
                entity_type=view.entity_kind.value,
                sql_view_name=view.sql_view_name or "",
                package=obj.package if obj else "",
                software_component=obj.software_component if obj else "",
                owner=obj.owner.value if obj else "",
                api_state=obj.api_state.value if obj else "",
                extraction_enabled=enabled,
                delta_method=delta,
                cdc_type=cdc_type,
                base_tables=stack.table_names,
                verdict=assessment.verdict.value,
                bucket=classify(assessment).value,
            )
        )

        edges = [
            (node.name, node.kind.value, node.depth, node.kind is not NodeKind.UNRESOLVED)
            for node in stack.nodes.values()
            if node.name != name.upper()
        ]
        if edges:
            self.store.put_dependencies(name, edges)

    # -- tables (F-06) ------------------------------------------------------
    def scan_tables(self, names: Iterable[str] | None = None) -> int:
        """Record every table, and whether any CDS view already reads it.

        The 'has a view' flag comes from the dependency edges collected during
        the view scan, so it is only as complete as the view inventory behind
        it. With a partial inventory this over-reports bare tables, which is
        the safe direction: it suggests building a view that may already exist,
        rather than hiding a table that has none.
        """
        targets = set(names or [])
        if not targets:
            targets.update(self.metadata.list_tables())
            for row in self.store.views():
                targets.update(row["base_tables"])

        count = 0
        for table_name in sorted(targets):
            table = self.metadata.get_table(table_name)
            if table is None:
                continue
            self.store.put_table(
                table_name=table.name,
                table_class=table.table_class.value,
                delivery_class=table.delivery_class,
                package=table.package,
                owner=table.owner.value,
                key_field_count=len(table.business_key_fields),
                field_count=len(table.fields),
                est_rows=table.estimated_rows,
                is_hot=int(table.is_hot),
                has_view=int(bool(self.store.dependents_of(table.name))),
            )
            count += 1
        return count


def _estimate(elapsed: float, done: int, total: int) -> float | None:
    """Seconds remaining, or None while there is not enough data to be honest.

    Showing an ETA derived from a single sample is worse than showing none —
    it swings wildly and teaches the user to distrust the number.
    """
    if done < 3 or total <= done:
        return None
    return (elapsed / done) * (total - done)


# ---------------------------------------------------------------------------
# Drift (F-35), built on the snapshots the store already keeps
# ---------------------------------------------------------------------------


@dataclass
class Drift:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[str, str, str]] = field(default_factory=list)
    """(view, what changed, description)."""

    lost_extraction: list[str] = field(default_factory=list)
    """Views that were extraction-enabled and no longer are — the classic
    post-upgrade regression."""

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def render(self) -> str:
        if not self.any:
            return "No drift."
        lines = []
        if self.lost_extraction:
            lines.append("LOST EXTRACTION (check after an upgrade):")
            lines += [f"    {n}" for n in self.lost_extraction]
        if self.added:
            lines.append(f"Added ({len(self.added)}): {', '.join(self.added[:20])}")
        if self.removed:
            lines.append(f"Removed ({len(self.removed)}): {', '.join(self.removed[:20])}")
        for name, what, detail in self.changed[:40]:
            lines.append(f"    {name}: {what} {detail}")
        return "\n".join(lines)


def compare_snapshots(before: dict, after: dict) -> Drift:
    drift = Drift()
    drift.added = sorted(set(after) - set(before))
    drift.removed = sorted(set(before) - set(after))

    for name in sorted(set(before) & set(after)):
        old, new = before[name], after[name]
        if old.get("extraction_enabled") and not new.get("extraction_enabled"):
            drift.lost_extraction.append(name)
        for key in ("verdict", "cdc_type", "extraction_enabled", "package"):
            if old.get(key) != new.get(key):
                drift.changed.append(
                    (name, key, f"{old.get(key)!r} → {new.get(key)!r}")
                )
    return drift
