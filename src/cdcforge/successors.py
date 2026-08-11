"""F-09 — given a table, what already exists, and what should we build on?

    Before offering to build a Z-wrapper for an SAP standard view, check
    whether an extraction-enabled, C1-released SAP view already covers the same
    base tables. Stops the customer building a duplicate of something SAP
    already ships. It sometimes tells the customer they need to build
    *nothing*, which builds more trust than a generator ever will.

A table like EKKO can have dozens of standard views over it. Choosing between
them is mostly **elimination, not scoring** — three or four of five are
usually disqualified outright, and the ranking only separates what survives.

Four gates, in order of how decisively they rule a view out:

1. **Does it expose the table's full key?** A CDC mapping addresses base
   tables, never intermediate views (Appendix A.5), so a wrapper over a
   standard view still has to map the table — and cannot, if the view never
   exposes its key.

2. **Is the table the view's root?** The one people miss. If the table is
   *joined* rather than the FROM, the view's row identity is something else
   entirely: a view rooted on EKPO yields one row per item, so replicating it
   to get header data duplicates every header field.

3. **Is it structurally CDC-capable?** Any HARD rule violation anywhere in its
   stack. This deliberately reuses the rule engine rather than re-implementing
   the checks, so a UNION four views down still disqualifies it.

4. **Is it a private (P_) view?** SAP-internal, changes without notice.

Join *count* is not a gate. Appendix A.3 is explicit that multiple joins are
fine and the shape is what matters; rejecting on count would discard most
viable candidates. It is a ranking penalty, because every mapped table is
another set of triggers on a live write path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdcforge.cardinality import structural_evidence
from cdcforge.cds import is_private_layer, vdm_layer
from cdcforge.inventory import describe_extraction
from cdcforge.lineage import element_origins
from cdcforge.metadata.base import MetadataSource, NullMetadataSource
from cdcforge.model import Severity, Verdict
from cdcforge.parsing.ddl import parse_ddl
from cdcforge.rules import RuleConfig, ValidationContext, validate_view
from cdcforge.rules.stack import NodeKind, resolve_stack

VDM_RANK = {"BASIC": 0, "COMPOSITE": 1, "CONSUMPTION": 2, "": 3, "PRIVATE": 9}

SCREENING_CONFIG = RuleConfig(max_stack_nodes=150)
"""A tighter dependency budget for screening candidates than for assessing one.

Assessing a single view the user named can afford to walk 400 objects. Doing it
for 25 candidates cannot: that is up to 10,000 object reads for one table, and
VBAP's first run took sixteen minutes almost entirely here.

150 is enough for what F-09 actually asks — the HARD blockers and key exposure
live in the first levels of a stack, not its far edges — and R-27 still reports
truncation, so a candidate whose stack could not be fully walked says so instead
of passing quietly. Assessment of a chosen view, where the answer has to be
complete, keeps the full budget.
"""


@dataclass
class Candidate:
    """An existing view over the table, and whether it can be built on."""

    view: str
    extraction_enabled: bool = False
    delta_method: str = "none"
    cdc_type: str = "none"
    owner: str = ""
    api_state: str = ""
    vdm: str = ""
    verdict: Verdict | None = None

    # -- gates -------------------------------------------------------------
    is_root: bool = False
    exposes_key: bool = False
    has_key_elements: bool = False
    blockers: list[str] = field(default_factory=list)
    private: bool = False

    # -- ranking -----------------------------------------------------------
    exposed_fields: int = 0
    table_fields: int = 0
    join_count: int = 0
    stack_depth: int = 0
    row_filtered: bool = False
    """A WHERE clause somewhere in the stack restricts which rows appear.

    Coverage counts columns, and columns are only half the question. VBAP has
    nine ready views — I_SalesOrderItem, I_CreditMemoRequestItem,
    I_SalesContractItem and so on — and each is VBAP restricted to one document
    category. At 23-41% column coverage they look like ordinary partial views,
    but replicating one gives every column for *some* of the rows rather than
    some columns for every row. Someone asking to replicate VBAP needs to know
    that before choosing, and no column count can tell them.
    """

    @property
    def exclusion_reasons(self) -> list[str]:
        """Every reason this view cannot be built on, most fundamental first.

        Order matters more than it looks. An aggregating view also fails the
        key gate — it groups, so the key is not in the projection — and
        reporting *that* first reads as "add the missing field", which is not a
        fix available to anyone. The GROUP BY is the cause; the missing key is
        a symptom.
        """
        reasons: list[str] = []
        if self.private:
            reasons.append(
                "private view (P_*) — SAP-internal, changes without notice"
            )
        if self.blockers:
            reasons.append(f"blocks delta — {self.blockers[0]}")
        if not self.is_root:
            reasons.append(
                "the table is joined into this view, not its root — its rows "
                "are not one per table row"
            )
        if not self.exposes_key:
            reasons.append(
                "does not expose the table's full key, so no CDC mapping to "
                "the table is possible"
            )
        if not self.has_key_elements:
            reasons.append(
                "declares no KEY elements, so a wrapper over it would have no "
                "row identity (R-17)"
            )
        return reasons

    @property
    def excluded_because(self) -> str:
        """The single most useful reason, or empty if the view is usable."""
        reasons = self.exclusion_reasons
        return reasons[0] if reasons else ""

    @property
    def usable(self) -> bool:
        return not self.excluded_because

    @property
    def is_annotated(self) -> bool:
        return self.extraction_enabled and self.delta_method == "CDC"

    @property
    def carries_cdc(self) -> bool:
        """Declares extraction and CDC delta, with nothing structural against it.

        This is what ranks a candidate first, and it is deliberately weaker
        than :attr:`is_ready`. Requiring a clean PASS put SAP's own purpose-built
        extraction views *below* unannotated ones, because two findings are
        near-universal and neither disqualifies anything:

        * R-26 is INCONCLUSIVE for every declared to-one join until it has been
          proven against real data, which needs a probe the tool has not run.
          Offline, that makes a clean verdict unreachable for any view with a
          join at all.
        * R-15 and its kind are FIXABLE — a missing element in the projection,
          not a reason delta cannot work.

        ``usable`` still carries the real disqualifiers: a UNION, a GROUP BY, a
        to-many join, a table that is joined in rather than the root. So this
        says "SAP ships this for extraction and nothing structural argues with
        it", and any findings ride along in :attr:`summary` rather than
        silently demoting the view.
        """
        return self.is_annotated and self.usable

    @property
    def is_ready(self) -> bool:
        """Annotated, usable, and every rule passes — the strict badge.

        A view can carry perfect-looking annotations and still contain an
        aggregation that makes delta impossible, so this stays strict. It is
        reported, not used to rank.
        """
        return self.is_annotated and self.verdict is Verdict.PASS and self.usable

    @property
    def coverage(self) -> float:
        return (self.exposed_fields / self.table_fields) if self.table_fields else 0.0

    @property
    def coverage_band(self) -> int:
        """Coverage rounded to 10% bands, best first.

        Banded so that 51% does not beat 49% on noise, leaving the structural
        signals to break genuine ties.
        """
        return -int(self.coverage * 10)

    @property
    def rank_key(self) -> tuple:
        """Best first. A view that already carries CDC delta wins outright.

        Readiness sits above coverage deliberately. A view SAP already ships
        with working extraction and CDC delta needs no wrapper, no transport
        and no maintenance, and that is worth more than columns — if the
        columns it carries are the ones you need. Whether they are is a
        question about *your* requirement, not about the table, and the tool
        cannot answer it. So it ranks the zero-work option first, states the
        coverage on every row, and lets the user overrule it.

        Coverage still sits above VDM layer and stack depth, which are
        tie-breakers between candidates of equal readiness.
        """
        return (
            # Anything SAP already ships with extraction and CDC delta comes
            # first, whether or not every rule passes — see carries_cdc.
            not self.carries_cdc,
            not self.is_ready,
            # Carrying every row beats carrying some. VBAP's nine ready views
            # are each one document category, and a column count cannot say so.
            self.row_filtered,
            # Coverage outranks bare extraction-enablement deliberately. A view
            # with extraction but no CDC delta needs exactly the same wrapper
            # as one with no annotations at all, so ranking it first claims a
            # saving that does not exist — and on VBAP it put a 1% view above
            # I_SalesDocumentItemBasic at 43%. Only CDC delta earns a tier of
            # its own, and it has two above.
            self.coverage_band,
            not self.extraction_enabled,
            VDM_RANK.get(self.vdm, 3),
            self.stack_depth,
            self.join_count,
            self.view,
        )

    @property
    def thin(self) -> bool:
        """Exposes so little of the table that reusing it may be pointless."""
        return bool(self.table_fields) and self.coverage < 0.25

    @property
    def summary(self) -> str:
        if self.is_ready:
            return f"ready — extraction and CDC delta ({self.cdc_type}), rules pass"
        if self.is_annotated and self.verdict is Verdict.FAIL_FIXABLE:
            return (
                f"already carries extraction and CDC delta ({self.cdc_type}) — "
                f"usable as it stands, with findings worth reading before you "
                f"rely on it"
            )
        if self.is_annotated and self.verdict is not Verdict.PASS:
            return (
                f"already carries extraction and CDC delta ({self.cdc_type}), "
                f"and some checks could not be settled offline — chiefly "
                f"whether its to-one joins really are to-one"
            )
        if self.extraction_enabled and self.delta_method == "byElement":
            return (
                "extraction enabled, timestamp delta only — a Replication Flow "
                "will not offer the delta option (KBA 3514600)"
            )
        if self.extraction_enabled:
            return "extraction enabled, no CDC delta — a wrapper would add it"
        return "no extraction annotations — a wrapper would add them"

    @property
    def detail(self) -> str:
        parts = [f"{self.vdm.title() or 'layer unknown'}"]
        if self.table_fields:
            parts.append(
                f"{self.exposed_fields}/{self.table_fields} table fields "
                f"({self.coverage:.0%})"
            )
        if self.row_filtered:
            parts.append("filtered rows")
        parts.append(f"depth {self.stack_depth}")
        if self.join_count:
            parts.append(f"{self.join_count} join(s)")
        if self.api_state not in ("", "UNKNOWN", "NOT_RELEASED"):
            parts.append(f"released {self.api_state}")
        return " · ".join(parts)


@dataclass
class SuccessorReport:
    table: str
    candidates: list[Candidate] = field(default_factory=list)
    searched: int = 0
    truncated: bool = False
    total_readers: int = 0
    """How many views read the table at all, before shortlisting."""

    examined: int = 0
    """How many went through the full stack walk and rule set."""

    prescreened: int = 0
    """How many were read and judged on their own DDL."""

    deferred: int = 0
    """Survived the prescreen but fell outside the full-validation budget."""

    deep_examined: int = 0
    """Released views read in the layers above the table's direct readers."""

    layers_climbed: int = 0
    """How many VDM layers above the direct readers were searched."""

    budget_exhausted: bool = False
    """The layer climb hit its budget, so higher layers may hold more."""

    from_delta_index: list[str] = field(default_factory=list)
    """Delta views contributed by the index rather than by the where-used walk.

    Worth reporting separately: every name here is one the climb could not
    have reached, so it is the measure of how much the index is earning.
    """

    readers_unknown: bool = False
    """Nobody could be asked what reads this table.

    Distinct from "nothing reads it", and the distinction is the whole
    recommendation: no readers means *build a view over the table*, while an
    unanswerable question means *do not know yet*. On a system whose where-used
    indexes this release does not expose, conflating them advises building a
    view beside the perfectly good one that was already there.
    """

    @property
    def partially_examined(self) -> bool:
        """True only when something was never *looked at* — not merely
        rejected cheaply. A view thrown out by the prescreen was judged on
        facts from its own DDL, so it counts as examined."""
        return (
            self.total_readers > self.prescreened - self.deep_examined
            or self.deferred > 0
            or self.budget_exhausted
        )

    @property
    def coverage_note(self) -> str:
        """Said out loud when only part of the list was checked.

        "None of these can carry delta" and "none of the ones I looked at can"
        are different claims, and the user has to be able to tell which they
        are being given.
        """
        if not self.partially_examined:
            return ""

        parts = [
            f"{self.total_readers} view(s) read {self.table} directly; "
            f"{self.prescreened} were read and screened"
        ]
        if self.deep_examined:
            parts.append(
                f", including {self.deep_examined} released view(s) found "
                f"across {self.layers_climbed} layer(s) above them"
            )
        parts.append(f", and {self.examined} went through the full rule set")
        if self.deferred:
            parts.append(
                f", widest coverage first — {self.deferred} were not fully "
                f"validated"
            )
        parts.append(".")
        if self.budget_exhausted:
            parts.append(
                " The layer search hit its budget, so higher layers may hold "
                "more."
            )
        return "".join(parts)

    @property
    def ready(self) -> list[Candidate]:
        return [c for c in self.candidates if c.is_ready]

    @property
    def wrappable(self) -> list[Candidate]:
        """Usable as the base of a delta wrapper, but not already there."""
        return [c for c in self.candidates if c.usable and not c.is_ready]

    @property
    def excluded(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.usable]

    @property
    def usable(self) -> list[Candidate]:
        """Every view that can carry delta, best first.

        Ready and wrappable candidates ranked against each other rather than
        as separate lists, because the choice between "use as-is" and "wrap
        it" is exactly the trade-off the user is being asked to make.
        """
        return sorted(self.ready + self.wrappable, key=lambda c: c.rank_key)

    @property
    def suggested(self) -> Candidate | None:
        """The best candidate overall — not the best *ready* one."""
        usable = self.usable
        return usable[0] if usable else None

    @property
    def has_ready(self) -> bool:
        """Is any candidate already carrying extraction and CDC delta?

        Uses ``carries_cdc``, not ``is_ready``: a view SAP ships for extraction
        counts even when a check could not be settled offline, which R-26 never
        can for a declared to-one join.
        """
        return any(c.carries_cdc for c in self.candidates)

    substantial_coverage: float = 0.15
    """Below this a candidate goes in the overflow rather than the main list."""

    def choices(self, limit: int = 10) -> list[Candidate]:
        """What to put in front of the user, best first.

        Every option that is genuinely worth weighing, and nothing else. Two
        earlier versions were both wrong in the same way, from opposite ends: a
        fixed three hid I_SalesDocumentItem at 49% behind a 32% view, and a
        flat top-eight padded the list with 1% and 7% views that nobody would
        pick. Neither length was the problem — the filter was.

        So the cut is on substance, not on position. Anything already carrying
        CDC delta is shown regardless of coverage, because it costs no work and
        the user may only need those columns. Anything else has to carry a
        meaningful share of the table to earn a place. The widest candidate is
        always shown, and everything omitted stays one click away rather than
        being discarded.
        """
        usable = self.usable
        if not usable:
            return []

        # Every view that already carries CDC delta is shown, and the limit
        # does not apply to them. They are the zero-work answers, so they are
        # the answer set rather than entries competing for room in it — VBAP
        # has twelve, and a flat cap of ten cut C_SalesDocumentItemDEX and its
        # sibling off the bottom. Everything else has to earn a place.
        cdc = [c for c in usable if c.carries_cdc]
        others = [
            c
            for c in usable
            if not c.carries_cdc and c.coverage >= self.substantial_coverage
        ]
        shown = cdc + others[: max(0, limit - len(cdc))]

        # Two guaranteed slots, both learned from real content. The widest
        # candidate, or a short list hides the 96% view of VBAK. And the widest
        # that carries *every* row, or VBAP's nine ready views — one per
        # document category — fill the list and push out I_SalesDocumentItem,
        # which at 49% and unfiltered is the better base for replicating the
        # table.
        must_show = [max(usable, key=lambda c: c.coverage)]
        unfiltered = [c for c in usable if not c.row_filtered]
        if unfiltered:
            must_show.append(max(unfiltered, key=lambda c: c.coverage))

        for candidate in must_show:
            if candidate not in shown:
                shown = [*shown, candidate]
        if not shown:
            shown = usable[:1]
        return sorted(shown, key=lambda c: c.rank_key)

    @property
    def prefer_table(self) -> bool:
        """Should the table be preferred over every candidate found?

        True when nothing usable carries a meaningful share of the table. MARA
        has five *ready* views exposing between 1% and 4% of its 367 columns:
        each is correct for CDC and none is what someone asking to replicate
        MARA wants. Reusing one is not reuse, it is replicating the wrong
        thing — and a single-table view over MARA costs nothing extra.
        """
        usable = self.usable
        return bool(usable) and all(c.thin for c in usable)

    @property
    def action(self) -> str:
        """What to do: ``USE``, ``WRAP``, ``BUILD`` or ``UNKNOWN``.

        The single place this ladder is decided. It used to be encoded twice —
        here in prose and again in the decision sheet — and the two drifted:
        the sheet demanded a *ready* view before saying USE while the prose
        needed only a CDC-carrying one, so EKET produced a row marked ``WRAP``
        whose own explanation read "so no wrapper is needed".

        The prose was right. A wrapper over a view that already carries delta
        inherits its findings — the stack depth, the trigger load — and fixes
        none of them, so there is nothing for a wrapper to add.
        """
        if self.readers_unknown:
            return "UNKNOWN"
        if self.prefer_table:
            return "BUILD"
        best = self.suggested
        if best is None:
            return "BUILD"
        if best.is_ready or best.carries_cdc:
            return "USE"
        return "WRAP"

    @property
    def recommendation(self) -> str:
        if self.readers_unknown:
            # Never "build directly on the table" here. That sentence is only
            # true when the search happened and found nothing; said after a
            # search that could not run, it advises building a second view
            # beside the one that was already there.
            return (
                f"Cannot tell what reads {self.table} on this system — the "
                f"where-used indexes did not answer, so no candidate search "
                f"was possible. This is not the same as finding nothing. "
                f"Check in SE11 or ADT before building anything, or run "
                f"'cdc-forge preflight' to see which metadata sources are "
                f"unavailable here."
            )
        if self.prefer_table:
            usable = self.usable
            widest = max(usable, key=lambda c: c.coverage)
            return (
                f"Build directly on {self.table}. {len(usable)} view(s) over it "
                f"can carry delta, but the widest ({widest.view}) exposes only "
                f"{widest.exposed_fields} of {widest.table_fields} columns "
                f"({widest.coverage:.0%}) — reusing one would replicate a small "
                f"slice of the table, not the table. They are listed below if "
                f"that slice is in fact all you need."
            )
        best = self.suggested
        if best is not None and best.is_ready:
            base = (
                f"Use {best.view} — extraction and CDC delta are already "
                f"declared and every rule passes. Building another view over "
                f"{self.table} would duplicate it."
            )
            return base + self._coverage_caveat(best)
        if best is not None and best.carries_cdc:
            base = (
                f"Use {best.view} — it already declares extraction and CDC "
                f"delta ({best.cdc_type}) and nothing structural argues with "
                f"it, so no wrapper is needed. Read its findings first: some "
                f"checks cannot be settled without probing real data."
            )
            return base + self._coverage_caveat(best)
        if best is not None:
            base = (
                f"{best.view} is the best base for a delta wrapper — "
                f"{best.detail}. It exposes the table's key, is rooted on "
                f"{self.table}, and contains nothing that blocks delta."
            )
            return base + self._coverage_caveat(best)
        if self.candidates:
            return (
                f"{len(self.candidates)} view(s) read {self.table} and none can "
                f"carry delta. Build directly on the table — a single-table view "
                f"qualifies for automatic CDC and has the shallowest stack."
            )
        return (
            f"No CDS view reads {self.table}. Build directly on the table."
        )

    def _coverage_caveat(self, candidate: Candidate) -> str:
        """Say so when the best candidate carries very little of the table.

        A view can be flawless for CDC and still be the wrong thing to
        replicate. MARA has ready views exposing 15 of its 367 columns —
        correct, and almost certainly not what someone asking to replicate MARA
        wants.
        """
        if not candidate.thin:
            return ""
        return (
            f" Note that it exposes only {candidate.exposed_fields} of "
            f"{candidate.table_fields} columns ({candidate.coverage:.0%}) — if "
            f"you need more than those, build directly on the table instead."
        )


def find_candidates(
    metadata: MetadataSource,
    table_name: str,
    *,
    scan_limit: int = 400,
    validate_limit: int = 25,
    prescreen_limit: int = 400,
    deep: bool = True,
    extra_names: list[str] | None = None,
) -> SuccessorReport:
    """Existing views over ``table_name``, gated and ranked.

    Two phases, because the thing that decides the answer must never be a
    guess. Phase one reads and parses *every* view that reads the table and
    rejects on facts from its own DDL. Phase two runs the full stack walk and
    rule set on what survives, ordered by measured coverage.

    The alternative — pre-ranking names before reading them — is what an
    earlier version did, and it cannot be made accurate. It ranked EKPO's 149
    readers on whether the name was short, put ``I_ARUNSTOITEM`` in the top
    fifteen and never looked at ``I_PurchasingDocumentItem``, which carries 41%
    of the table. The tool then advised building a custom view over a table
    that already had a perfectly good standard one.
    """
    report = SuccessorReport(table=table_name.upper())
    table = metadata.get_table(table_name)
    key_fields = {f.name.upper() for f in table.business_key_fields} if table else set()
    total_fields = len([f for f in table.fields if not f.is_client]) if table else 0

    names = metadata.views_reading_table(table_name)

    # Delta views the where-used indexes cannot see. CDSVIEWCROSSREF records
    # classic views under their SQL view name and a view entity has none, so
    # modern C_*DEX content is largely absent from it — and those are precisely
    # the views worth finding. The index comes at it from the other end: SAP
    # says which views carry delta, and their FROM chains resolve fine.
    #
    # Added to the candidate list, not short-circuited past it. They still get
    # prescreened, still get the full rule set, and can still be rejected —
    # being delta-supported is a reason to *look*, never a reason to trust.
    if extra_names:
        found = [n for n in extra_names if n]
        if found:
            report.from_delta_index = sorted(
                {n.upper() for n in found} - {n.upper() for n in (names or [])}
            )
            names = sorted({*(names or []), *found})

    if names is None:
        names = _scan_for_readers(metadata, table_name, scan_limit, report)
        # The index could not answer *and* the fallback had nothing to scan.
        # Zero readers then means "nobody asked", not "nobody reads it" — and
        # the difference decides whether this table gets a wrapper over an
        # existing view or a brand new one built beside the view that was
        # already there. Recording it is what stops the recommendation being
        # confident about a question that was never put.
        if not names and report.searched == 0:
            report.readers_unknown = True

    report.total_readers = len(names)

    # One column cache for the whole run. A table's readers sit on the same
    # handful of parent views, and without this each candidate re-parses them.
    columns: dict[str, frozenset[str] | None] = {}

    # -- phase one: read everything, reject only on certainties ------------
    order = _examination_order(metadata, names)
    if len(order) > prescreen_limit:
        report.truncated = True

    # Warm the cache for everything about to be read. A hint only — the loop
    # below still reads through get_view_source and behaves identically if this
    # does nothing. It matters because this phase is round trips, not work:
    # T001 has 248 readers, 362 sources get read, and serially that is 407
    # seconds of almost pure latency.
    metadata.prefetch_sources(order[:prescreen_limit])
    metadata.prefetch_objects(order[:prescreen_limit])

    screened: list[_Prescreen] = []
    for name in order[:prescreen_limit]:
        pre = _prescreen(
            metadata, name, report.table, key_fields, total_fields, columns
        )
        if pre is None:
            continue
        report.prescreened += 1
        if pre.rejected:
            report.candidates.append(pre.as_candidate())
        else:
            screened.append(pre)

    if deep:
        screened += _climb_layers(
            metadata, screened, report, key_fields, total_fields, columns=columns
        )

    # -- phase two: the expensive checks, on real coverage order ------------
    chosen = _to_validate(screened, validate_limit)
    for pre in chosen:
        candidate = _examine(metadata, pre, report.table, key_fields, total_fields)
        if candidate is not None:
            report.candidates.append(candidate)
    report.examined = len(chosen)
    report.deferred = len(screened) - len(chosen)

    report.candidates.sort(key=lambda c: c.rank_key)
    return report


def _climb_layers(
    metadata: MetadataSource,
    screened: list[_Prescreen],
    report: SuccessorReport,
    key_fields: set[str],
    total_fields: int,
    *,
    max_layers: int = 6,
    budget: int = 600,
    columns: dict[str, frozenset[str] | None] | None = None,
) -> list[_Prescreen]:
    """Every VDM layer above the table, not just the first one.

    ``I_SalesDocument`` selects from ``I_SalesDocumentBasic``, which selects
    from VBAK. A search that stops at direct readers of VBAK never sees it, yet
    it carries 56% of the table and is SAP-released. And the stack does not
    stop at two: BASIC feeds COMPOSITE feeds CONSUMPTION, and a released view
    worth building on can sit at any of them. So this keeps climbing until a
    layer yields nothing new.

    Three things keep it affordable, none of them a guess about a name:

    * Only *rooted* survivors are expanded. If a view is not rooted on the
      table, nothing selecting from it can be either — unless it joins the
      table directly, in which case it already arrived at layer one.
    * Only SAP-released readers are followed. The fan-out is large (296 views
      read I_SalesDocumentBasic alone) and release is a real statement that SAP
      means the object to be consumed. Custom Z-views are unaffected: they read
      their tables directly and arrive at layer one.
    * A budget, so a pathological graph cannot run away. Exhausting it is
      reported rather than swallowed.
    """
    found: list[_Prescreen] = []
    frontier = [p for p in screened if p.rooted]
    seen = {p.name for p in screened} | {c.view for c in report.candidates}
    released = metadata.released_views()

    for layer in range(max_layers):
        if not frontier or report.deep_examined >= budget:
            break

        # Both names, because crossref records a classic view under its SQL
        # view name — I_SalesDocument refers to ISDSALESDOCBSC, and looking up
        # the DDL name I_SALESDOCUMENTBASIC matches nothing.
        handles: list[str] = []
        for pre in frontier:
            handles.append(pre.name)
            if pre.sql_view_name:
                handles.append(pre.sql_view_name)

        above = metadata.views_reading_views(handles)
        if not above:
            break

        fresh = [n for n in above if n.upper() not in seen]
        if released is not None:
            fresh = [n for n in fresh if n.upper() in released]
        seen.update(n.upper() for n in above)
        if not fresh:
            break

        remaining = budget - report.deep_examined
        if len(fresh) > remaining:
            fresh = sorted(fresh)[:remaining]
            report.budget_exhausted = True

        next_frontier: list[_Prescreen] = []
        metadata.prefetch_sources(sorted(fresh))
        metadata.prefetch_objects(sorted(fresh))
        for name in sorted(fresh):
            pre = _prescreen(
                metadata, name, report.table, key_fields, total_fields, columns
            )
            if pre is None:
                continue
            report.prescreened += 1
            report.deep_examined += 1
            if pre.rejected:
                report.candidates.append(pre.as_candidate())
            else:
                found.append(pre)
                if pre.rooted:
                    next_frontier.append(pre)

        report.layers_climbed = layer + 1
        frontier = next_frontier

    return found


def _to_validate(screened: list[_Prescreen], limit: int) -> list[_Prescreen]:
    """Which survivors get the full stack walk, when not all of them can.

    Ordered by ``rank_key``, which puts already-enabled views first — they are
    the best possible outcome, since they need no wrapper at all. But that
    ordering alone can push the widest view out of the budget behind a queue of
    thin enabled ones, and dropping the widest candidate is the exact failure
    this whole search was rewritten to stop: the user picks something, then
    finds out a better view existed all along.

    So the widest few are seeded in first and hold their places regardless of
    rank. On VBAK that is what keeps ZDDL_VBAK (96%) in front of the user
    alongside VC_INTEGRATION_VBAK (84%, already enabled) — which of those two
    is right depends on the columns you need, and only the user knows that.
    """
    if len(screened) <= limit:
        return sorted(screened, key=lambda p: p.rank_key)

    widest = sorted(screened, key=lambda p: -p.coverage)[: max(1, limit // 5)]
    seeded = list(widest)
    seen = {id(p) for p in seeded}
    for pre in sorted(screened, key=lambda p: p.rank_key):
        if len(seeded) >= limit:
            break
        if id(pre) not in seen:
            seeded.append(pre)
            seen.add(id(pre))
    return sorted(seeded, key=lambda p: p.rank_key)


def _examination_order(metadata: MetadataSource, names: list[str]) -> list[str]:
    """The order to read views in, for when the budget cannot cover them all.

    This decides nothing on its own — every view in the list gets read and
    judged on its own DDL. It only decides *when*, so that a truncated run has
    looked at the most likely candidates first.

    Deliberately no name-shape heuristics beyond the VDM prefix, which is a
    documented SAP convention rather than a guess. An earlier version sorted by
    name length here and it silently decided the outcome.
    """
    enabled = metadata.extraction_enabled_views() or set()

    def order(name: str) -> tuple:
        upper = name.upper()
        return (
            upper not in enabled,
            VDM_RANK.get(vdm_layer(None, upper), 3),
            upper,
        )

    return sorted(names, key=order)


@dataclass
class _Prescreen:
    """What one view's own DDL says, before any stack walk.

    Everything here is derived from the view's source alone plus, at most, a
    walk of its FROM chain. No dependency resolution, no rule run against the
    full stack — those are phase two and cost an HTTP call per object.
    """

    name: str
    private: bool = False
    rooted: bool = False
    local_blockers: list[str] = field(default_factory=list)
    elements: int = 0
    exposed_fields: int = 0
    table_fields: int = 0
    coverage_measured: bool = False
    extraction_enabled: bool = False
    delta_method: str = "none"
    cdc_type: str = "none"
    owner: str = ""
    api_state: str = ""
    vdm: str = ""
    local_verdict: Verdict | None = None
    sql_view_name: str = ""
    """The classic view's generated SQL view, which is how crossref names it."""

    @property
    def rejected(self) -> bool:
        return self.private or bool(self.local_blockers) or not self.rooted

    @property
    def coverage(self) -> float:
        if self.coverage_measured and self.table_fields:
            return self.exposed_fields / self.table_fields
        return 0.0

    @property
    def rank_key(self) -> tuple:
        """Phase-two order. Coverage is measured by now, so this is evidence.

        Views whose coverage could not be measured cheaply — those sitting on
        other views, where the count needs full lineage — sort on element count
        instead, and after equally-covered direct readers rather than being
        dropped.
        """
        return (
            not self.extraction_enabled,
            -int(self.coverage * 10),
            not self.coverage_measured,
            -self.elements,
            VDM_RANK.get(self.vdm, 3),
            self.name,
        )

    def as_candidate(self) -> Candidate:
        """A rejected prescreen, as a candidate the report can explain.

        ``exposes_key`` and ``has_key_elements`` are asserted true because the
        prescreen never tested them — reporting them false would invent a
        reason the view was not rejected for. It already has a real one.

        The verdict is carried over only when the local run found a HARD
        violation, which is conclusive: a UNION or GROUP BY in this view's own
        DDL blocks delta no matter what the rest of the stack looks like. A
        locally-clean view that was rejected for its root or its layer keeps a
        ``None`` verdict, because "the parts I checked passed" must never be
        recorded as PASS.
        """
        return Candidate(
            view=self.name,
            extraction_enabled=self.extraction_enabled,
            delta_method=self.delta_method,
            cdc_type=self.cdc_type,
            owner=self.owner,
            api_state=self.api_state,
            vdm=self.vdm,
            verdict=self.local_verdict if self.local_blockers else None,
            private=self.private,
            blockers=list(self.local_blockers),
            is_root=self.rooted,
            exposes_key=True,
            has_key_elements=True,
            exposed_fields=self.exposed_fields,
            table_fields=self.table_fields,
        )


def _prescreen(
    metadata: MetadataSource,
    name: str,
    table: str,
    key_fields: set[str],
    total_fields: int,
    columns: dict[str, frozenset[str] | None] | None = None,
) -> _Prescreen | None:
    """Judge a view on its own DDL. Reject only what is certainly rejectable.

    Anything uncertain survives into phase two. A cheap phase that produces a
    false rejection is worse than no cheap phase at all — the view disappears
    and nothing downstream can recover it.
    """
    source = metadata.get_view_source(name)
    if source is None:
        return None
    view = parse_ddl(source, name_hint=name)
    if view.has_fatal_issue:
        return None

    enabled, delta, cdc = describe_extraction(view)
    obj = metadata.get_object(name)
    pre = _Prescreen(
        name=name.upper(),
        private=is_private_layer(view.annotations, name),
        elements=len(view.select_items),
        table_fields=total_fields,
        extraction_enabled=enabled,
        delta_method=delta,
        cdc_type=cdc,
        owner=obj.owner.value if obj else "",
        api_state=obj.api_state.value if obj else "",
        vdm=vdm_layer(view.annotations, name),
        sql_view_name=str(
            view.annotations.get("AbapCatalog.sqlViewName") or ""
        ).strip("'").upper(),
    )

    # Blockers visible without leaving this view: UNION, GROUP BY, aggregates,
    # inner and to-many joins. Run through the rule engine with no metadata so
    # it sees only the local DDL, rather than re-implementing the checks here.
    local = validate_view(view, metadata=NullMetadataSource())
    pre.local_verdict = local.verdict
    pre.local_blockers = [
        f"{r.rule_id}: {r.message}"
        for r in local.results
        if r.severity is Severity.HARD and r.outcome.value == "VIOLATED"
    ]

    root = view.from_source
    if root is not None:
        if root.name.upper() == table:
            pre.rooted = True
        else:
            # Follows the FROM chain only — a handful of fetches, all cached.
            pre.rooted = _roots_on(metadata, root.name, table, depth=6)

    if pre.rooted:
        # Measured for view-over-view too, not just direct readers. Restricting
        # this to a direct FROM left every deep-search candidate at coverage
        # zero — they sit on another view by definition — so they sorted last
        # and the full-validation budget never reached them. I_SalesDocument
        # passed every gate and was deferred out of the answer.
        #
        # Lineage does the walk, and the views it parses on the way are the
        # same handful shared by all of a table's readers, so the cache
        # absorbs most of the cost.
        origins = element_origins(metadata, view, cache=columns)
        exposed = {o.field for o in origins.values() if o.table.upper() == table}
        pre.exposed_fields = len(exposed)
        pre.coverage_measured = True
    return pre


def _examine(
    metadata: MetadataSource,
    pre: _Prescreen | str,
    table: str,
    key_fields: set[str],
    total_fields: int,
) -> Candidate | None:
    """The expensive half: resolve the stack and run the full rule set."""
    name = pre if isinstance(pre, str) else pre.name
    source = metadata.get_view_source(name)
    if source is None:
        return None
    view = parse_ddl(source, name_hint=name)
    if view.has_fatal_issue:
        return None

    enabled, delta, cdc = describe_extraction(view)
    obj = metadata.get_object(name)
    candidate = Candidate(
        view=name.upper(),
        extraction_enabled=enabled,
        delta_method=delta,
        cdc_type=cdc,
        owner=obj.owner.value if obj else "",
        api_state=obj.api_state.value if obj else "",
        vdm=vdm_layer(view.annotations, name),
        private=is_private_layer(view.annotations, name),
        join_count=len(view.joins),
        table_fields=total_fields,
    )

    # object_meta is passed explicitly: validate_view fetches it itself only
    # when it builds its own context, and this one is reused below.
    ctx = ValidationContext(
        view=view,
        metadata=metadata,
        config=SCREENING_CONFIG,
        object_meta=obj,
    )
    stack = ctx.stack

    # F-14's structural half. It reads no data — it proves a declared to-one
    # join by showing the ON condition covers the joined table's whole primary
    # key, so at most one row can match. Without it R-26 is INCONCLUSIVE for
    # every candidate with a join, which is most of them, and no candidate
    # could ever reach a clean verdict. That is the honest answer only when the
    # proof genuinely is unavailable, not when nobody looked.
    ctx.cardinality_evidence = structural_evidence(ctx)
    candidate.stack_depth = stack.max_depth_reached

    # A WHERE anywhere in the stack restricts the rows, not just this view's.
    candidate.row_filtered = view.where_ref is not None or any(
        node.view is not None and node.view.where_ref is not None
        for node in stack.nodes.values()
    )

    # Gate 2 — is the table this view's root, or merely joined in?
    root = view.from_source
    if root is not None:
        node = stack.node(root.name)
        if root.name.upper() == table:
            candidate.is_root = True
        elif node is not None and node.kind is NodeKind.VIEW:
            # A view over a view: root through the stack to the leaf it sits on.
            candidate.is_root = _roots_on(metadata, root.name, table, depth=6)

    # Gate 1 — does it expose the table's full key?
    #
    # Via lineage rather than the immediate-source index, so it holds for a
    # view sitting on other views as well as one reading the table directly,
    # and so a cast around the key does not hide it.
    origins = element_origins(metadata, view)
    exposed = {
        origin.field for origin in origins.values() if origin.table.upper() == table
    }
    candidate.exposed_fields = len(exposed)
    candidate.exposes_key = bool(key_fields) and key_fields <= exposed
    # A wrapper inherits the base's key markers, so a base with no KEY elements
    # produces a wrapper with no row identity. Cheap to check, and it is the
    # difference between suggesting a base and having the generator refuse it
    # one screen later.
    candidate.has_key_elements = bool(view.key_items)

    # Gate 3 — anything that blocks delta, anywhere in the stack.
    # Reuses the context built above, so the dependency stack is walked once
    # per candidate rather than twice.
    assessment = validate_view(view, context=ctx)
    candidate.verdict = assessment.verdict
    candidate.blockers = [
        f"{r.rule_id}: {r.message}"
        for r in assessment.results
        if r.severity is Severity.HARD and r.outcome.value == "VIOLATED"
    ]
    return candidate


def _roots_on(
    metadata: MetadataSource, view_name: str, table: str, depth: int
) -> bool:
    """Does this view's FROM chain terminate on ``table``?

    Follows only the FROM at each level, never a join: the question is what
    the view's rows *are*, and that is decided by the root alone.
    """
    if depth <= 0:
        return False
    source = metadata.get_view_source(view_name)
    if source is None:
        return view_name.upper() == table
    view = parse_ddl(source, name_hint=view_name)
    if view.has_fatal_issue or view.from_source is None:
        return False
    root = view.from_source.name.upper()
    if root == table:
        return True
    return _roots_on(metadata, root, table, depth - 1)


def _scan_for_readers(
    metadata: MetadataSource, table: str, limit: int, report: SuccessorReport
) -> list[str]:
    """Fallback when the source has no dependency index.

    Fine over fixtures, hopeless over a system with thousands of views — which
    is why ``views_reading_table`` exists.
    """
    target = table.upper()
    found: list[str] = []
    for index, name in enumerate(metadata.list_views()):
        if index >= limit:
            report.truncated = True
            break
        report.searched += 1
        source = metadata.get_view_source(name)
        if source is None:
            continue
        view = parse_ddl(source, name_hint=name)
        if view.has_fatal_issue:
            continue
        stack = resolve_stack(view, metadata, max_nodes=60)
        if target in {t.upper() for t in stack.table_names}:
            found.append(name)
    return found
