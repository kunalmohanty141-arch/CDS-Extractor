"""Is it still good?

Everything else in this tool answers "can this work" before the fact. Nothing
answered "does it still work" after it — and the objects this tool builds sit
in a system that keeps moving under them. Support packs change SAP's views. A
colleague deletes a package. An upgrade withdraws a base view that carried no
release contract in the first place, which the decision sheet has to warn about
precisely because nothing was going to check later.

So this asks four questions of an object that is supposed to be feeding
Datasphere, and they are deliberately independent:

1. **Is it there?** The cheapest failure, and the one nobody notices.
2. **Does the system report it as delta-supported?** ``DHCDCVCDSEXTRE`` is
   SAP's own answer, and it is the closest thing to an acceptance test
   available on the ABAP side.
3. **Does it still pass the rules?** The source can drift under a view — a
   base changes, a field disappears from a projection.
4. **Is its base still released?** Only asked when it has one.

A verification that could not be made is never reported as a pass. Each answer
is a tri-state for that reason: ``True``, ``False``, or ``None`` for "could not
be established", and :attr:`Verification.ok` requires the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.metadata.base import MetadataSource
from cdcforge.model import Verdict


@dataclass
class Verification:
    """What is still true about one object."""

    name: str
    exists: bool | None = None
    delta_supported: bool | None = None
    verdict: Verdict | None = None
    base: str = ""
    base_released: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def rules_pass(self) -> bool | None:
        if self.verdict is None:
            return None
        return self.verdict is Verdict.PASS

    @property
    def ok(self) -> bool:
        """Everything checkable checked out.

        ``None`` never counts as a pass. A verification that could not be made
        is the state most likely to be glossed over, and glossing over it here
        would report a feed as healthy on the strength of a failed query.
        """
        return bool(self.exists) and self.delta_supported is True

    @property
    def status(self) -> str:
        if self.exists is False:
            return "GONE"
        if self.exists is None:
            return "UNKNOWN"
        if self.delta_supported is False:
            return "NO DELTA"
        if self.delta_supported is None:
            return "UNCONFIRMED"
        if self.rules_pass is False:
            return "DELTA, RULES FAIL"
        return "OK"

    def render(self) -> str:
        line = f"{self.status:<18} {self.name}"
        if self.verdict is not None and self.rules_pass is False:
            line += f"  ({self.verdict.value})"
        for note in self.notes:
            line += f"\n    {note}"
        return line


@dataclass
class VerifyReport:
    results: list[Verification] = field(default_factory=list)

    @property
    def failed(self) -> list[Verification]:
        return [r for r in self.results if not r.ok]

    def render(self) -> str:
        lines = [r.render() for r in self.results]
        good = sum(1 for r in self.results if r.ok)
        lines.append(
            f"\n{good} of {len(self.results)} still carrying delta"
            + (f", {len(self.failed)} need attention" if self.failed else "")
        )
        return "\n".join(lines)


def verify(
    metadata: MetadataSource,
    names: list[str],
    *,
    delta_supported: set[str] | None = None,
    check_rules: bool = True,
) -> VerifyReport:
    """Check each name. Reads only.

    ``delta_supported`` is the system's own set, passed in so one query serves
    a whole batch rather than one per object. ``None`` means it could not be
    read, and every object's answer is then ``None`` rather than ``False`` —
    the difference between "SAP says this lost delta" and "we could not ask" is
    the difference between an incident and a retry.
    """
    report = VerifyReport()
    for name in names:
        try:
            report.results.append(
                _verify_one(metadata, name, delta_supported, check_rules)
            )
        except Exception as exc:
            # Verifying forty objects and dying on the seventh tells you
            # nothing about the other thirty-three. The failure becomes this
            # object's answer — UNKNOWN, which never counts as a pass.
            report.results.append(
                Verification(
                    name=name.upper(),
                    notes=[f"could not be checked — {type(exc).__name__}: {exc}"],
                )
            )
    return report


def _verify_one(
    metadata: MetadataSource,
    name: str,
    delta_supported: set[str] | None,
    check_rules: bool,
) -> Verification:
    from cdcforge.rules import validate_object

    upper = name.upper()
    result = Verification(name=upper)

    source = metadata.get_view_source(upper)
    result.exists = source is not None
    if source is None:
        result.notes.append(
            "not found — deleted, or in a client this user cannot read"
        )
        return result

    if delta_supported is None:
        result.notes.append(
            "the system did not report its delta-enabled views, so this "
            "could not be confirmed"
        )
    else:
        result.delta_supported = upper in delta_supported
        if not result.delta_supported:
            result.notes.append(
                "the system no longer reports this as delta-supported — "
                "check its annotations and that it is active"
            )

    if check_rules:
        assessment = validate_object(upper, metadata)
        result.verdict = assessment.verdict
        if assessment.verdict is not Verdict.PASS:
            for problem in assessment.problems[:3]:
                result.notes.append(problem.format_line().strip())

    _check_base(metadata, result, source)
    return result


def _check_base(metadata: MetadataSource, result: Verification, source: str) -> None:
    """Is what this view is built on still a released API?

    Only meaningful for a wrapper. The decision sheet warns when a base carries
    no release contract — ``VC_INTEGRATION_*`` and its kind — and that warning
    is worth nothing if nobody ever looks again.
    """
    from cdcforge.parsing.ddl import parse_ddl

    view = parse_ddl(source, name_hint=result.name)
    if view.has_fatal_issue or view.from_source is None:
        return
    base = view.from_source.name.upper()
    if metadata.get_table(base) is not None:
        return  # built straight on a table; nothing to release
    result.base = base

    if base.startswith(("Z", "Y")):
        # A customer view. It is not released and never will be, and saying
        # "SAP may change it in a support pack" about somebody's own object is
        # simply false — it warned about ZAOH_I_GS_SALES_CUBE being built on
        # ZAOH_I_GS_SALES_PROD, which is the same team's work. Keeping a base
        # you own stable is your business, and the tool has nothing to add.
        return

    meta = metadata.get_object(base)
    state = (getattr(meta, "api_state", None) if meta else None)
    label = getattr(state, "value", "") or str(state or "")
    if not label or label.upper() in ("UNKNOWN", "NONE"):
        result.base_released = None
        return
    result.base_released = label.upper() in ("C1", "RELEASED")
    if not result.base_released:
        result.notes.append(
            f"built on {base}, which is not a released API ({label}) — SAP may "
            f"change it in a support pack, so re-check after every upgrade"
        )
