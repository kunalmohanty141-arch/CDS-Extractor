"""F-14 — the cardinality prover, execution half.

This is the only code in the tool that reads a business table, and it is
deliberately the narrowest thing that can answer the question.

The boundary is structural, not a promise. Callers pass a table name and a list
of key columns; they cannot pass SQL. The only query this module can construct
is::

    SELECT <key columns>, COUNT(*) FROM <table>
     WHERE <client> = '<client>'
     GROUP BY <key columns>
    HAVING COUNT(*) > 1

That returns duplicate *key values* and how many rows share them. It cannot
return a row's contents, because no non-key column is ever selected. The one
value that does leave the system is a key that is already duplicated — which is
precisely the evidence the user needs in order to fix the view, and is reported
so they can go and look at it.

An empirical pass is weaker than the structural proof in
``cdcforge.cardinality``: it establishes that no duplicate exists *in this
client, right now*, not that none can exist. The wording says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdcforge.cardinality import ProbePlan
from cdcforge.connect import endpoints as ep
from cdcforge.connect.session import AdtError, AdtSession
from cdcforge.connect.sql import _safe_name, parse_data_preview
from cdcforge.rules.context import CardinalityEvidence, CardinalityResult

COUNT_ALIAS = "CDCFORGE_ROWS"


@dataclass
class ProbeOutcome:
    plan: ProbePlan
    evidence: CardinalityEvidence
    query: str = ""
    error: str = ""

    def render(self) -> str:
        result = self.evidence.result.value
        detail = ""
        if self.evidence.result is CardinalityResult.VIOLATED:
            detail = (
                f" — {self.evidence.max_count} rows share key "
                f"{self.evidence.sample_key!r}"
            )
        elif self.error:
            detail = f" — {self.error}"
        return f"{self.plan.join_alias:<20} {self.plan.table:<22} {result}{detail}"


def build_probe_query(
    table: str, key_fields: list[str], client_field: str = "", client: str = ""
) -> str:
    """The only query this module can run.

    Built from identifiers, never from caller-supplied SQL. Every selected
    column is a grouping column or the count, so no row contents can be
    returned.
    """
    safe_table = _safe_name(table)
    columns = [_safe_name(f) for f in key_fields if f]
    if not columns:
        raise ValueError("a cardinality probe needs at least one key column")

    selected = ", ".join(columns)
    where = ""
    if client_field and client:
        where = f" WHERE {_safe_name(client_field)} = '{_safe_name(client)}'"

    return (
        f"SELECT {selected}, COUNT(*) AS {COUNT_ALIAS} FROM {safe_table}{where} "
        f"GROUP BY {selected} HAVING COUNT(*) > 1"
    )


def run_probe(session: AdtSession, plan: ProbePlan, *, max_rows: int = 5) -> ProbeOutcome:
    """Probe one join. Never raises."""
    already = plan.evidence()
    if already is not None:
        return ProbeOutcome(plan=plan, evidence=already)

    try:
        query = build_probe_query(
            plan.table, plan.join_fields, plan.client_field, session.profile.client
        )
    except ValueError as exc:
        return ProbeOutcome(
            plan=plan,
            evidence=CardinalityEvidence(
                plan.join_alias, plan.table, CardinalityResult.DECLARED_ONLY
            ),
            error=str(exc),
        )

    try:
        response = session.post(
            ep.DATA_PREVIEW.path,
            body=query,
            params={"rowNumber": str(max_rows)},
            action="cardinality-probe",
            object_name=plan.table,
        )
    except AdtError as exc:
        return ProbeOutcome(
            plan=plan,
            evidence=CardinalityEvidence(
                plan.join_alias, plan.table, CardinalityResult.DECLARED_ONLY
            ),
            query=query,
            error=exc.message,
        )

    result = parse_data_preview(response.text)
    if not result.parsed and result.raw.strip():
        return ProbeOutcome(
            plan=plan,
            evidence=CardinalityEvidence(
                plan.join_alias, plan.table, CardinalityResult.DECLARED_ONLY
            ),
            query=query,
            error="the probe ran but its response could not be parsed",
        )

    if not result.rows:
        # No key has more than one row — in this client, at this moment.
        return ProbeOutcome(
            plan=plan,
            evidence=CardinalityEvidence(
                join_alias=plan.join_alias,
                table=plan.table,
                result=CardinalityResult.PROVEN_TO_ONE,
                max_count=1,
            ),
            query=query,
        )

    worst = max(result.rows, key=lambda r: _as_int(r.get(COUNT_ALIAS)))
    sample = ", ".join(
        f"{field}={worst.get(field.upper(), '')}" for field in plan.join_fields
    )
    return ProbeOutcome(
        plan=plan,
        evidence=CardinalityEvidence(
            join_alias=plan.join_alias,
            table=plan.table,
            result=CardinalityResult.VIOLATED,
            max_count=_as_int(worst.get(COUNT_ALIAS)),
            sample_key=sample,
        ),
        query=query,
    )


def run_probes(
    session: AdtSession, plans: list[ProbePlan], *, max_rows: int = 5
) -> list[ProbeOutcome]:
    return [run_probe(session, plan, max_rows=max_rows) for plan in plans]


def evidence_from(outcomes: list[ProbeOutcome]) -> dict[str, CardinalityEvidence]:
    """Keyed by join alias, ready for ``validate_object(cardinality_evidence=…)``."""
    return {o.plan.join_alias: o.evidence for o in outcomes}


def _as_int(value: object) -> int:
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0
