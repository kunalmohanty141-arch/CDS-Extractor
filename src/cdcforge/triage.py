"""F-07 — the three lists.

The user-facing output of discovery:

1. **Ready** — extraction + CDC enabled, validated.
2. **Fixable** — a view exists but lacks extraction/delta; the tool can act.
3. **Bare** — a Z-table with no view, or an SAP view that cannot be modified.

Two buckets are added to the spec's three, because collapsing them would lie:
``REVIEW`` for objects the rules could not settle, and ``NOT_POSSIBLE`` for
structural hard failures. Filing an aggregating view under "Fixable" would
promise a fix the tool cannot deliver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cdcforge.metadata.base import MetadataSource
from cdcforge.model import Assessment, Verdict
from cdcforge.parsing.ddl import parse_ddl


class Bucket(str, Enum):
    READY = "READY"
    """Passes every deterministic rule."""

    FIXABLE = "FIXABLE"
    """Fails only on things the tool can generate its way out of."""

    REVIEW = "REVIEW"
    """Something could not be decided. A human has to look."""

    NOT_POSSIBLE = "NOT_POSSIBLE"
    """Structurally impossible — aggregation, union, to-many join, parameters."""

    UNPARSEABLE = "UNPARSEABLE"


_BUCKET_BY_VERDICT = {
    Verdict.PASS: Bucket.READY,
    Verdict.FAIL_FIXABLE: Bucket.FIXABLE,
    Verdict.MANUAL_REVIEW: Bucket.REVIEW,
    Verdict.FAIL_HARD: Bucket.NOT_POSSIBLE,
    Verdict.UNPARSEABLE: Bucket.UNPARSEABLE,
}


def classify(assessment: Assessment) -> Bucket:
    return _BUCKET_BY_VERDICT[assessment.verdict]


@dataclass
class TriageSummary:
    buckets: dict[Bucket, list[str]] = field(default_factory=dict)
    bare_tables: list[str] = field(default_factory=list)
    """Tables no CDS view reads — the "what has no view at all" list (F-06)."""

    def add(self, assessment: Assessment) -> None:
        self.buckets.setdefault(classify(assessment), []).append(assessment.object_name)

    def count(self, bucket: Bucket) -> int:
        return len(self.buckets.get(bucket, []))

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.buckets.values())

    def render(self) -> str:
        lines = [f"{self.total} view(s) assessed"]
        for bucket in Bucket:
            names = self.buckets.get(bucket, [])
            if not names:
                continue
            lines.append(f"  {bucket.value:<13} {len(names):>4}  {', '.join(sorted(names))}")
        if self.bare_tables:
            lines.append(
                f"  {'BARE TABLES':<13} {len(self.bare_tables):>4}  "
                f"{', '.join(sorted(self.bare_tables))}"
            )
        return "\n".join(lines)


def find_bare_tables(metadata: MetadataSource) -> list[str]:
    """Tables that no known CDS view reads.

    Only as complete as the view inventory behind it: with a partial inventory
    this over-reports, which is the safe direction — it suggests building a view
    that may already exist, rather than hiding a table that has none.
    """
    referenced: set[str] = set()
    for view_name in metadata.list_views():
        source = metadata.get_view_source(view_name)
        if source is None:
            continue
        parsed = parse_ddl(source, name_hint=view_name)
        for name in parsed.all_referenced_objects:
            referenced.add(name.upper())

    return [name for name in metadata.list_tables() if name.upper() not in referenced]


def triage(assessments: list[Assessment], metadata: MetadataSource | None = None) -> TriageSummary:
    summary = TriageSummary()
    for assessment in assessments:
        summary.add(assessment)
    if metadata is not None:
        summary.bare_tables = find_bare_tables(metadata)
    return summary
