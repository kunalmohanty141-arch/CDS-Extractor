# Design notes

**Why every part of CDC Forge is the way it is** — including the parts that were
wrong and got corrected by measuring them against a real system.

For what the tool *does*, start at [README.md](README.md). To use it, see
[GETTING_STARTED.md](GETTING_STARTED.md). For the security model,
[SECURITY.md](SECURITY.md). This file is the reasoning, and it is deliberately
long: on a wire contract SAP does not publish, the argument for a decision is
worth more than the decision.

Built from `CDC-Forge-Complete.md` (Build Specification v1.0).

**Status.** Offline core (parser, rule engine, verdict model, generator,
fixtures), read-only ADT connector, store and reporting, the F-09 successor
search, the F-14 cardinality prover, a Streamlit UI, a write pipeline with
transport handling, the decision-sheet round trip, the estate survey, the delta
index, and `verify`. The rules are calibrated against the **904 views** S/4HANA
itself ships with CDC delta declared — see *Judgement calls*, where four of them
had to be reversed. The Datasphere side is not built; see *What is not here*.

---

## The principle everything else is built on

> A verdict of `MANUAL_REVIEW` or `UNPARSEABLE` is a *correct output*, not a
> failure. Never let ambiguity resolve to `PASS`.

This is mechanical, not a matter of discipline. `RuleResult.verdict_contribution`
maps any `INCONCLUSIVE` outcome to `MANUAL_REVIEW` regardless of the rule's
severity, and `Assessment.verdict` derives the verdict from the results rather
than trusting a caller to set one. A rule that crashes returns `INCONCLUSIVE`
rather than disappearing, because a skipped rule reads as a passed rule.

The same idea runs through everything added since. An unreadable CTS answer
refuses a write rather than reading as "no transport request needed". A
where-used index that cannot answer is recorded as *unknown* rather than as *no
readers*. A verification that could not be made never counts as a pass.

## Quick start

```bash
cd cdc-forge
python -m pip install -e ".[dev]"       # or just: set PYTHONPATH=src
python -m pytest -q                     # 716 tests
```

Everything below runs against the local fixtures — no system, no network.

```bash
# Validate one view, with the reason for every finding
python -m cdcforge.cli validate ZI_MISSING_MAPPED_KEY --fixtures fixtures

# The three lists (F-07) across the whole corpus
python -m cdcforge.cli scan --fixtures fixtures

# Dependency tree down to leaf tables (F-08)
python -m cdcforge.cli stack ZI_DEEP_L1 --fixtures fixtures

# The rule catalogue with its SAP sources
python -m cdcforge.cli rules -v

# Generation
python -m cdcforge.cli generate table ZORDERITEM --fixtures fixtures
python -m cdcforge.cli generate mapping ZI_MISSING_MAPPED_KEY --fixtures fixtures
python -m cdcforge.cli generate wrapper I_VENDOR_RELEASED --fixtures fixtures
```

Refusals are part of the output:

```
$ python -m cdcforge.cli generate wrapper ZI_AGGREGATE --fixtures fixtures
REFUSED  a wrapper inherits the structure of ZI_AGGREGATE, and the base view
         fails hard — R-03: aggregate SUM() used; R-04: GROUP BY present.
         Adding annotations on top cannot fix this.
```

### Against a real system

The whole job, in the order you would actually do it. Everything up to `create`
is read-only.

```bash
python -m cdcforge.cli login     --profile DEV        # password → OS keyring
python -m cdcforge.cli preflight --profile DEV        # can we, and may we?

python -m cdcforge.cli estate    --profile DEV        # what is already built
python -m cdcforge.cli plan      --profile DEV VBAP EKPO KNA1 --out plan.xlsx
#   … edit Action / Base / Target name / Note in Excel, offline …
python -m cdcforge.cli apply     --profile DEV --file plan.xlsx --out-dir ddl/

# Only this one writes, and only where you say
python -m cdcforge.cli apply     --profile DEV --file plan.xlsx --out-dir ddl/ \
                                 --create --package ZDSP_EXTRACTION \
                                 --new-transport "CDC Forge generated views"

python -m cdcforge.cli verify    --profile DEV        # and again after an upgrade
```

Or `python -m cdcforge.cli ui` for the same thing with the sheet as a download
and an upload.

---

## Layout

| Module | Responsibility | Talks to SAP? |
|---|---|---|
| `cdcforge.parsing` | Tokenizer, annotation normaliser, DDL parser → AST (F-11) | No |
| `cdcforge.cds` | The CDC annotation vocabulary, shared by rules and generator | No |
| `cdcforge.metadata` | `MetadataSource` interface + fixture-backed mock (F-38) | No |
| `cdcforge.rules` | Stack resolution (F-08), R-01…R-30 (F-12), advisories, engine | No |
| `cdcforge.generator` | Z-table view, wrapper, CDC mapping, naming (F-19…F-22, F-25) | No |
| `cdcforge.triage` | The three lists (F-07) | No |
| `cdcforge.cli` | Terminal front end | No |
| `cdcforge.model` | Verdict model (F-13) | No |

`parsing`, `rules` and `generator` have no SAP dependency and must not acquire
one. That is what makes the hard logic testable offline and demoable without a
system — and it is where the intellectual property lives.

### Feature coverage

Implemented: **F-07, F-08, F-09, F-11, F-12, F-13, F-14, F-15, F-19, F-20,
F-21, F-22, F-24, F-25, F-32, F-34, F-38**, plus advisories for **F-17** and
**F-18**, and the interface for **F-16** (`SubscriptionState`) which the
offline core consumes but cannot populate.

All thirty rules **R-01 … R-30** are implemented and individually tested, and
calibrated against SAP's own delta content — see *Judgement calls*.

Beyond the specification, because running it on a real system asked for them:
the **decision sheet** round trip, **change recording** through CTS, the
**estate** survey of what is already built, and **verify** for whether it still
works.

---

## Judgement calls

Where the specification was silent or where two readings were possible, here is
what was decided and why. These are the places to push back if you disagree.

**R-10 / R-11 split.** R-11 owns the join *type*; R-10 owns the *cardinality* of a
LEFT OUTER join. The spec lists both, but reporting one defect twice under two
rule IDs would bury the real reason, so each join produces one finding from one
rule.

**R-26 is reported, not used to rank.** A declared `TO ONE` join that has never
been probed is `INCONCLUSIVE`, which offline means *every* view with a join. That
is the honest answer, and it made "every rule passes" unreachable for any joined
view — so ranking candidates on it demoted SAP's own purpose-built extraction
views below unannotated ones. F-09 ranks on `carries_cdc` instead: declares
extraction and CDC delta, and nothing *structural* argues with it. `usable` still
holds the real disqualifiers — UNION, GROUP BY, aggregates, to-many joins, a
table joined in rather than rooted on. Findings ride along in the summary rather
than silently demoting a view.

**Coverage counts columns, so row filtering is tracked separately.** VBAP has nine
ready views — `I_SalesOrderItem`, `I_CreditMemoRequestItem`, `I_SalesContractItem`
and so on — each of which is VBAP restricted to one document category by a WHERE
clause. At 23–41% column coverage they look like ordinary partial views. They are
not: replicating one gives every column for *some* rows rather than some columns
for every row, and no column count can say so. A WHERE anywhere in the stack marks
a candidate `filtered rows`, it ranks below an unfiltered peer, and the widest
candidate that carries every row always has a place in the shown list.

**A bare `LEFT OUTER JOIN` is `MANUAL_REVIEW`.** Not PASS, not FAIL. The framework
needs a to-one shape; plain `LEFT OUTER JOIN` neither declares nor denies one, and
ABAP does not validate cardinality at runtime. Passing it would be the false PASS
the tool exists to prevent; failing it would discard most viable candidates.

**R-26 defaults to requiring evidence.** A declared `LEFT OUTER TO ONE JOIN` that
has never been probed is `INCONCLUSIVE`, so any multi-join view is `MANUAL_REVIEW`
until F-14 runs. That is deliberate — it is the single most expensive silent
failure in the domain, and making it visible is the product's wedge. It is
configurable (`RuleConfig(require_cardinality_evidence=False)`), and when waived
the finding is downgraded, not hidden. When evidence *disproves* a declaration,
R-26 reports at `HARD` severity even though its declared severity is
`MANUAL_REVIEW`: a proven defect is no longer a matter for review.

**`BLOCKING` and `WARNING` never move the verdict.** R-28 is inconclusive by
construction whenever no system is connected. If that fed the verdict, every
offline assessment would be `MANUAL_REVIEW` and the findings that matter would
drown. Instead R-28 gates `Assessment.write_blocked`, which defaults to blocked.

**`MANUAL_REVIEW` outranks `FAIL_FIXABLE`.** `FAIL_FIXABLE` is a promise that the
tool can generate a working alternative. With anything unresolved, that promise
cannot honestly be made.

**R-15 requires one exposed side per ON equality, not both — reversed after
measuring it.** This rule used to demand both sides, and the justification
written here said the values are equal by construction "but SAP's requirement is
written about the fields, and a false PASS costs more than a false FAIL". That
sentence contained its own refutation, and the numbers settled it:

| | |
|---|---|
| delta views with joins | 123 |
| flagged by the strict reading | **117 (95%)** |
| C1-released among them | 109 |

A rule rejecting 95% of the vendor's own shipped delta content is not finding
defects. Narrowing its scope was not the answer either: 58 of the flagged fields
sat on a table the view's own mapping addresses, which is exactly the case the
strict reading existed for.

`ON a.vbeln = b.vbeln` guarantees both sides hold the same value in every output
row. CDC needs that *value* to work out which output row a base-table change
belongs to, so one exposed side supplies it and the second is redundant — which
is why SAP consistently omits it. The genuine defect is **neither** side exposed:
then the join key is absent from the output entirely and no mapping can locate
the row. That is what R-15 now checks, which required the parser to keep ON
tokens so equalities can be *paired* — a flat list of field references cannot
express which field is equated with which.

**R-29's threshold is the tool's, not SAP's.** SAP publishes no maximum stack
depth. The default of 5 is this tool's conservatism and the rule message says so
in as many words, rather than implying an SAP number exists.

**F-09 reads every view before ranking any of them.** Choosing a wrapper base is
elimination, and elimination has to run on facts. Earlier versions pre-ranked
candidate *names* to keep the cost down — first alphabetically, then by whether
the name was short. Both silently decided the outcome. On EKPO the length
tie-breaker put `I_ARUNSTOITEM` in the examined set and left
`I_PurchasingDocumentItem` out of it, and the tool recommended building a custom
view over a table that already had a standard one carrying 41% of its columns.

There is no version of that heuristic that is safe, so it is gone. The search is
two-phase: read and parse every view that reads the table, reject on what its
own DDL proves (private, UNION, GROUP BY, not rooted on the table), then run the
full stack walk and rule set on the survivors, ordered by *measured* coverage.
Phase one never rejects on anything uncertain: a cheap phase that produces a
false rejection is worse than no cheap phase, because the view disappears and
nothing downstream can recover it.

**And it climbs every VDM layer, not just the direct readers.** The dependency
indexes list views that read a table *directly*, and that is not where the good
ones live. `I_SalesDocument` selects from `I_SalesDocumentBasic`, which selects
from VBAK — and VBAK's released document views (`I_SalesOrder`,
`I_CustomerReturn`, `I_SalesContract`, `I_SalesQuotation`) sit three layers up.
None of them was reachable until the search started climbing.

Three things keep the fan-out affordable, none of them a guess about a name:

- Only *rooted* survivors are expanded. If a view is not rooted on the table,
  nothing selecting from it can be either — unless it joins the table directly,
  in which case it already arrived at layer one.
- Only SAP-**released** readers are followed. 296 views read
  `I_SalesDocumentBasic` alone, and release is a real statement that SAP means
  the object to be consumed. Custom Z-views are unaffected: they read their
  tables directly and arrive at layer one.
- A budget, reported when hit rather than swallowed.

One wire-format detail worth knowing if you touch this: `CDSVIEWCROSSREF`
records a classic view under its *generated SQL view name*, so `I_SalesDocument`
refers to `ISDSALESDOCBSC` and looking up `I_SALESDOCUMENTBASIC` matches nothing
at all. Both names are passed. The data-preview endpoint also rejects an `IN`
list beyond roughly ten names with an unexplained HTTP 400 — measured, not
guessed — and a failed batch is split and retried rather than skipped, because
skipping it once returned 3 readers where there were 287 and reported that as
the answer.

### Why it is fast enough

The first look at a table reads its whole reader graph; later runs are cached.
Two things brought a VBAP search from **967 seconds to about 9**:

- **Parsing is memoized.** F-09 screens ~200 views over a table, every one
  resolves a dependency stack, and those stacks overlap heavily. Measured: 96%
  of all parse calls were repeats (VBAK 10,180 hits / 385 misses). A
  `ParsedView` is written only while being built — every mutation lives in
  `parsing/ddl.py` — so caching on the source text is safe. *A caller that
  mutates a parsed view would corrupt every later reader of the same source.*
- **Each dependency stack is walked once.** Callers that need the stack *and*
  then validate used to build two contexts and walk it twice. `validate_view`
  now accepts a caller's context, and a test pins the invariant that made this
  legitimate: reusing a context must produce an identical verdict and an
  identical list of rule outcomes.

**Count the queries, not the seconds.** A cold 12-table backlog took 866s, and
one table — `T001`, read by 248 views — took 407 of them. Profiling by action
rather than guessing:

```
read-source      914 calls
metadata-query   771 calls, 326s serial — 57% of the wall clock
```

771 freestyle queries to screen one table, and **a freestyle query generates a
program on the target system**. That is not a throughput problem. It is how a
development box's subpool directory fills up and its owner's Eclipse closes,
which happened once on the reference system and was caused by this tool.

Two reads replaced them. The API-release check now answers from the bulk
released set already in memory; the repository headers come from **one read of
the whole DDL directory** instead of one query per object. The second matters
more, and is the less obvious of the two: 417 of the 461 TADIR queries came
from the *stack walk*, which cannot batch because it does not know an object's
name until it gets there. Reading everything once removes the question rather
than answering it faster.

| | before | after |
|---|---|---|
| queries to screen `T001` | 771 | **100** |
| of those, TADIR | 461 | **2** |
| wall clock, cold | 609s | **320s** |

Same candidate, same order. What remains is 76 table-metadata reads and 20
where-used chunks — worth batching next, and no longer the thing that puts a
system at risk.

**Trust the query count, not the stopwatch.** The same cold 12-table backlog
went 866s → 715s, which is a much smaller improvement than the single-table
measurement, and the honest reading is that the end-to-end number is too noisy
to quantify: `LIPS` took 4.4s, 55.7s and 46.6s on three runs of the same code
against the same system. A shared development box has other people on it. The
query count is deterministic and does not care, which is the other reason to
count queries.

Concurrent source prefetch is in too, and honest about its worth: it bought
13%, not the 5× the round-trip latency suggested, because the queries were the
real cost all along. It also broke the audit log — two threads each opening
their own SQLite connection collide, one gets `database is locked`, and the
request it was recording dies with it, intermittently. Writes are serialised
now. *An audit entry must never be the reason a request fails.*

Screening also uses a tighter node budget than assessing a single named view —
the blockers and key exposure live in the first levels of a stack, and R-27
still reports truncation, so a candidate whose stack could not be fully walked
says so rather than passing quietly.

**A CDC-enabled view outranks a wider one that needs a wrapper.** Reversed from
an earlier build, which put coverage first. A view SAP already ships with working
extraction and CDC delta needs no wrapper, no transport and no maintenance, and
that is worth more than columns — *if* the columns it carries are the ones you
need. Whether they are is a question about the requirement, not about the table,
and the tool cannot answer it. So it ranks the zero-work option first, prints the
coverage on every row and in every radio label, and lets the user overrule it.

Bare `dataExtraction.enabled` earns no such priority: without CDC delta it needs
exactly the same wrapper an unannotated view needs, so claiming a saving there is
false. On VBAP that had put a 1% view above `I_SalesDocumentItemBasic` at 43%.

**What gets shown is cut on substance, not on position.** Two earlier versions
were wrong from opposite ends — a fixed three hid `I_SalesDocumentItem` at 49%
behind a 32% view, and a flat top-eight padded the list with 1% and 7% views
nobody would pick. Neither length was the problem. Now:

- **every** view already carrying CDC delta is shown, whatever the limit — those
  are the zero-work answers, not entries competing for room. VBAP has twelve, and
  a cap of ten cut `C_SalesDocumentItemDEX` off the bottom;
- everything else must carry a meaningful share of the table to earn a place;
- the widest candidate, and the widest that carries *every* row, always have a
  place;
- anything omitted is one click away rather than discarded.

**Two buckets added to F-07's three.** `REVIEW` and `NOT_POSSIBLE` alongside
`READY` / `FIXABLE` / bare tables. Filing an aggregating view under "Fixable"
would promise a fix that cannot be delivered.

**Element naming defaults to `preserve`.** Generated element names equal the DDIC
field names, which keeps the CDC mapping unambiguous and makes a generated view
reviewable line-by-line against DD03L. `camel` is available.

**A wrapper is `as select from`, never `as projection on`.** A projection view
entity is a RAP *transactional projection*, and SAP refuses to activate one that
is not part of a business object:

```
E  Transactional Projection View must be part of a business object.
   SD_CDS_PC_TQ(009)
```

That rejected **every wrapper the tool produced** — its main answer for standard
SAP content — and nothing caught it, because the wrapper passed the tool's own
thirty rules, passed the parser, and read perfectly well. It only surfaced when
the generated DDL was put in front of the real system. An extraction wrapper
needs no behaviour definition and the element list is identical either way.
Measured on S/4HANA 816: projection rejected, `select` activated clean, and
`provider contract analytical_query` rejected too.

The lesson generalises past this one bug: **generated DDL is not verified by
passing our own rules.** The only judge that counts is an activation on a real
system, which is why `tests/` cannot be the whole story here.

**R-11 asks about the mapping, not the join keyword — and this reverses an
earlier decision made on a bad sample.** An earlier build kept R-11 as a blanket
HARD failure on any non-left-outer join, recording
`C_SalesDocItmPrcgElmntDEX_1` as a lone outlier where SAP contradicted its own
documentation. That conclusion came from sampling 20 views, and it was wrong.

`DHCDCVCDSEXTRE` lists every extraction-enabled view with an `ISDELTASUPPORTED`
flag — 887 of them on this system. Running the local rules across all 887 found
**38 views using inner joins, every one C1-released**, spanning production,
valuation, plant maintenance and supplier. That is not an outlier, it is a
pattern.

Checking the mechanism rather than the count settles it: of those 57 inner and
cross joins, **50 target a table the view's own CDC mapping covers**. The seven
that do not are customizing tables (`TCMS_SEC_AST`, `CRMC_PROC_TYPE`) plus two
whose base tables lineage could not resolve. A mapped table carries its own
trigger, so a change there raises its own delta — which is what actually decides
delta correctness. The documented "only Left-Outer-to-One joins are supported"
is about the mapping `role`.

So R-11 now asks the question that matters:

| shape | outcome |
|---|---|
| right outer / full outer | `VIOLATED`, still HARD — emits rows with no main-table row, and no SAP delta view uses one |
| inner / cross, table is mapped | `SATISFIED` |
| inner / cross, `changeDataCapture.automatic` | `SATISFIED` — the framework derives the mapping |
| inner / cross, table not mapped | `INCONCLUSIVE` → `MANUAL_REVIEW` |

Disagreement with SAP's own flag fell from 46 views to 10 — R-07 (6), R-12 (4)
and R-10 (2) — and R-07 and R-12 were then taken the same way, by looking at the
mechanism behind each remaining view rather than at the count.

**R-12 distinguishes a filtered to-many association from an unfiltered one.**
Three views disagreed. Two of them, `I_DFS_EquipmentBasicDEX` and
`I_DFS_EquipmentDEX`, are C1-released and ship with CDC delta while following an
association declared `[1..*]`:

```
association [1..*] to I_DFS_UniversalAssignmentBasic as _DFS_UniversalAssignmentBasic on …
  and _DFS_UniversalAssignmentBasic.DfsAssgmtStatusCode     = 'IDFS4'
  and _DFS_UniversalAssignmentBasic.DfsAssgmtValdtyStrtDate <= $session.user_date
  and _DFS_UniversalAssignmentBasic.DfsAssgmtType           = 'TOB'
```

One status, one type, one validity window — effectively to-one. CDS has no way
to say *to-many, but this filter makes it to-one*, so SAP declares the
pessimistic cardinality and ships it anyway. The third,
`I_PlantMaintObjectListData` (not released), follows a genuinely **unfiltered**
to-many association — a real violation. So a followed to-many whose ON condition
carries filter conditions is now `INCONCLUSIVE` → `MANUAL_REVIEW`, and an
unfiltered one stays `VIOLATED`. The declaration stops being the last word only
where SAP itself stopped treating it that way.

**R-10 stands.** One view disagrees, `C_RDPBATCHDEX`, with two
`LEFT OUTER TO MANY JOIN`s against a `#MAIN` of `MCHA`. A to-many join really
does multiply rows, and one view out of 123 joined ones is not evidence against
the mechanism. Left as a finding.

Across all 904 extraction-enabled views the local rules now block exactly one
that SAP flags as delta-supported — the R-10 view above — with none
unparseable.

The caveat that keeps this honest: every one of the 887 already carries CDC
annotations, so `ISDELTASUPPORTED` is derived from what the view declares. It is
evidence of what SAP *ships and stands behind*, not an independent runtime proof
that delta works.

**Unknown is a first-class state.** No metadata means `INCONCLUSIVE`, never an
assumption. Unknown API state means *unmodifiable* (fail safe). An unknown object
in the dependency walk is `UNRESOLVED`, never assumed to be a leaf table.

**A tool that degrades must say what got smaller.** Every ADT endpoint here is
verified against one release, and the metadata tables are the parts most likely
to differ on another — `DHCDCVCDSEXTRE` arrived with the CDC framework, and the
where-used indexes are internal and documented nowhere. `preflight` probes each
one and names the *consequence* rather than the table, because "CDSVIEWCROSSREF
unavailable" means nothing to the person running this and "no existing view can
be found for a table" means everything.

The failure this guards against was live and found by looking for it.
`views_reading_table` returns `None` when the index cannot answer; the fallback
then scans `list_views()`, which is empty when set-based reads are unavailable.
Zero readers came back looking exactly like *nothing reads this table*, and the
recommendation was **"build directly on it"** — which on such a system advises
building a second view beside the perfectly good one already there. A search
that could not run is now `readers_unknown`, the recommendation says so, and the
decision sheet marks the row `SKIP` instead of naming a Z object on a guess.

Not the same sentence, not the same advice:

| | says |
|---|---|
| searched, found nothing | *Build directly on the table.* |
| could not search | *Cannot tell what reads it — this is not the same as finding nothing.* |

---

## The corpus

`fixtures/` holds 38 DDL views, 16 tables and an `expected.json` that pins both
the verdict and *the rule that must have produced it* — verdict alone is a weak
assertion, because a fixture written for R-14 could fail for an unrelated reason
and still show `MANUAL_REVIEW`. `test_corpus.py` also asserts that the corpus
between them exercises every rule, so a rule cannot rot unnoticed.

`ZI_COMMENT_TRAPS.ddl` is the fixture that justifies building a real tokenizer:
commented-out `define view`, `group by` and `union all`; a block comment
containing `inner join` and `sum()`; string literals containing `group by`,
`inner join` and `//`; and an escaped quote. It validates clean.

**Against the spec's target of ≥100 views, this is a starter corpus.** It covers
every rule positively and negatively, which is the useful floor. Growing it with
real customer DDL is the highest-value next task for validation quality, and the
`expected.json` format is designed to make that a one-line addition per view.

---

## Stage 2 — the read-only connector

`cdcforge.connect` is the only package that talks to SAP. The offline core does
not import it and does not need it installed.

```bash
python -m pip install requests keyring

cdc-forge profile add --profile DEV --host s4dev.corp.local --port 44300 \
                      --client 100 --user KUNAL
cdc-forge login --profile DEV        # prompts; stored in Windows Credential Manager
cdc-forge preflight --profile DEV    # the checklist (F-02)

cdc-forge fetch  ZI_SOMETHING --profile DEV
cdc-forge assess ZI_SOMETHING --profile DEV --checkrun
cdc-forge audit  --profile DEV
```

**Writing is possible, and deliberately hard to do by accident.** Sessions are
read-only by default — turning that off is an explicit argument, never a
default — and two independent guards sit in front of the socket:

- **F-04 read-only mode** — enforced in the connector, not the UI, so a UI bug
  cannot route around it. Blocked calls never reach the network.
- **F-03 production guard** — writes are refused against a productive client,
  and an *unknown* client role counts as productive. Overriding needs the system
  ID and client typed exactly.

The spec says read-only mode should block "every non-GET". Taken literally that
disables `checkruns`, a POST that changes nothing and which the same document
calls the most important endpoint in the tool. The switch therefore blocks
anything not classified READ in `endpoints.py`, and anything `endpoints.py` has
never heard of. Two bugs found by the tests are worth knowing about, because
both would have permitted writes in read-only mode: prefix matching let
`POST /ddic/ddl/sources` (create) pass as a read because it prefixes the object
read, and `?_action=LOCK` passed for the same reason. Classification now matches
whole endpoint templates, verb included, writes first.

Every request is audited (F-32) with **body hashes, never bodies** — the tool
moves metadata, and the audit log is the one place a careless implementation
would start retaining source code. Passwords live in the OS keyring; a password
written by hand into a profile file is ignored.

Set-based metadata reads go through the ADT data-preview service against an
**allowlist** of the metadata tables in Appendix D (DD02L, DD03L, TADIR, T000…).
A freestyle SELECT endpoint is exactly what could quietly turn a metadata tool
into an extraction tool, and the product's central claim is that it never moves
business data.

Because the ADT wire contract is unpublished, the preflight *probes* as well as
checks — it reports whether each endpoint behaves as the reconstructed map says
on your release. `cdc-forge raw` dumps a response when a parser disagrees with
reality, and `cdc-forge endpoints` prints the map.

Not yet built in Stage 2: the full inventory sweep with its SQLite cache (§6),
the APIS release-state lookup (so R-25 reports UNKNOWN → unmodifiable, fail
safe), and the three-way `MetadataSource` selection of §3.6 — only the
zero-install path is implemented.

## Stage 3 — inventory, store and the report

```bash
# harvest and validate everything, cached to SQLite (F-05/F-06)
cdc-forge inventory --fixtures fixtures --store demo.sqlite
cdc-forge inventory --profile DEV --store dev.sqlite        # against a system

# "is there already a view for this table, and is any of it usable?"
cdc-forge uses MATDOC --store dev.sqlite

# the deliverable (F-34): Excel + JSON + self-contained HTML
cdc-forge report --store dev.sqlite --out ./out --system "S4D/100"

# drift across time or environments (F-35)
cdc-forge snapshot baseline --store dev.sqlite
cdc-forge snapshot latest --compare baseline --store dev.sqlite
```

The store is the §6 schema, one SQLite file per profile, with **cache
invalidation by source hash** — `cached_assessment` refuses to return a verdict
computed against different source, because a stale verdict is worse than none:
it is confidently wrong and nothing downstream can tell.

The first sweep is slow, the second is not. Watch for one thing if you touch
this code: the store originally opened a connection per operation, which cost
~0.7s per view in fsync alone — invisible on a 38-view corpus and an extra hour
on a 4,000-view landscape. It now holds one connection in WAL mode. The audit
log is a *separate* database and keeps its durability, because losing a cache
entry costs a re-read and losing an audit record costs the client's trust.

The progress line withholds its ETA until three objects are done. An estimate
from one sample swings wildly and teaches the user to distrust the number.

**The PDF path is the HTML export**, printed from a browser. That is a better
document than reportlab would produce and avoids a dependency whose only job
would be layout.

**On the effort estimate.** F-34 asks for one, and an estimate built from
invented per-object numbers is exactly the false precision this tool refuses
everywhere else. So `EffortModel` is explicit, configurable, and its assumptions
are printed in the report next to the total they produced. A reader can disagree
with them because they can see them.

## Stage 4 — creating a view

`connect/writer.py` is the only code that changes anything, and it refuses far
more than it accepts. Four gates run at the connector layer, below the UI and
below the CLI:

- the name must be **customer namespace** (Y/Z) — SAP objects are refused
  outright, which has been a standing rule of this project from the start;
- the package must be **explicitly allowed**, and the default allowlist is
  `$TMP` alone: local, non-transportable, owned by the creating user, deletable
  without trace. Widening it is a visible edit at the call site;
- the object **must not already exist** — the tool never overwrites something it
  did not just create;
- read-only mode must be off, and the production guard must be satisfied.

The pipeline is *check target → create → lock → upload → syntax check →
activate → unlock*, and every step is recorded including the ones that never
ran, because "failed" alone leaves someone guessing whether a half-built object
is sitting in the system. A rejected syntax check or a failed activation rolls
the object back and releases the lock; if the rollback itself fails the result
is marked **orphaned** and the object is named, because then something really is
left behind.

**The syntax check is checked against the `inactive` version.** This matters
more than it looks: a source that has just been uploaded and never activated has
no *active* version, so the first cut — which asked for `active`, the checkrun
default — gave the system nothing to look at and quietly did not run. That reads
exactly like a clean result. Pointed at the right version it earns its place:
against a deliberately broken view it returns *"The column
this_field_does_not_exist is unknown"* and the object is rolled back.

`activate=False` stops after the check and hands over an inactive object to
activate by hand. That is a supported outcome, not a failure — activation needs
an authorisation that creating and writing the source do not, and on a system
where the user lacks it there is no reason to throw the work away. Nothing is
rolled back in that case, but a source SAP *rejects* is still rolled back either
way.

## The decision sheet

The tool ranks views, measures coverage and says what it would do. It cannot
know that VBAP is wanted for one document category only, that EKPO is already
covered by a feed built last year, or that this quarter only finance matters.
Those are the customer's facts, and they arrive in a spreadsheet because that
is where this industry keeps its lists.

So a batch is a round trip:

```
cdcforge plan  --profile DEV VBAP EKPO KNA1 MARA BKPF --out plan.xlsx
#   … edit Action / Base / Target name / Note, offline, with colleagues …
cdcforge apply --profile DEV --file plan.xlsx --out-dir ddl/ [--create]
```

Four actions: `USE` (replicate the view as-is, generate nothing), `WRAP`
(generate a Z wrapper over `Base`), `BUILD` (generate over the table), `SKIP`.

Three decisions in the format earned their place:

- **The suggestion and the decision are different columns.** `Action` is what
  will happen; `Suggested action` is read-only provenance. Merging them loses
  the record of what the tool thought, and the first question anyone asks about
  a batch six weeks later is *why did we do that one*. It also makes the
  interesting rows findable — `apply` prints what changed:
  `EKPO: WRAP → BUILD`, `KNA1: USE → SKIP`.
- **Columns are found by name, not position.** The first thing anyone does to a
  spreadsheet is insert a column. Reading by position would shift every value
  one place and build the wrong objects without a word.
- **A sheet is validated whole before any of it runs.** A batch that creates
  eleven objects and stops on a typo in the twelfth leaves the user to work out
  which eleven. Duplicate target names are caught here too — the second create
  would fail with *"already exists"* and read like a stale object from a
  previous run.

**Coverage on its own argues for the wrong choice**, which the first real run
demonstrated. `FNDEI_EKPO_FILTER` shows 85% against the chosen
`R_PURCHASINGDOCUMENTITEM` at 54%, and is the worse answer: it is row-filtered,
so it gives every column of *some* rows. Candidates are therefore marked `*`
for CDC-carrying and `~` for row-filtered, and the marks are explained on a
sheet inside the workbook — the file gets mailed to someone who was not in the
conversation where it was generated.

The same run turned up something worth knowing about S/4HANA itself. For VBAP,
KNA1 and MARA the best candidate is `VC_INTEGRATION_*` — SAP's own extraction
views, unfiltered, `changeDataCapture.automatic`, 78% coverage against 41% for
the next best. Their source says why they carry no contract:

```
// Unfortunately only views with a prefix 'I_' and 'C_' are allowed
// to be released for C1 contract
```

Still the right answer, still something SAP can change in a support pack. Both
halves go in the sheet: a `USE` row whose base is not a released API carries
the caveat in its `Why`.

The UI downloads and re-reads **the same workbook** — one format, one reader.
An earlier cut offered a flat CSV with a free-text `Decision` column, which
looked like the same thing and could be fed back to nothing: the user filled it
in and then had nowhere to put it. Uploading an edited sheet generates into the
create panel rather than writing directly, because that panel is where the
package, the transport request and the typed confirmation live.

## The views the where-used indexes cannot see

Asked whether EKET really had no delta view anywhere. It had two, and the tool
could not see either:

```
C_PurOrdScheduleLineDEX      C1             delta, released, never found
  I_PurOrdScheduleLineAPI01  RELEASED
    I_PurOrdScheduleLineBasic  NOT_RELEASED
      I_PurgDocScheduleLineBasic NOT_RELEASED   found, ranked, wrapped
        EKET
```

Two causes, and the second is the serious one. The layer climb follows only
SAP-released readers, and that chain runs through two unreleased views — but
more fundamentally, **neither where-used index records view-to-view usage for
view entities**. `CDSVIEWCROSSREF` stores classic views under their SQL view
name and a view entity has none; `DDLS_RIS_INDEX` returned nothing either.
Measured: both return zero rows for all three views in that chain. So `C_*DEX`
content — precisely what this tool exists to find — was largely invisible.

So index from the other end. The system says which views carry delta (904 of
7,095 extraction-enabled), and `FROM` chains resolve perfectly well. Walk them
once, record the table each is rooted on, cache it: **885 views over 626
tables**. Names found this way join the candidate list and go through the same
prescreen and the same thirty rules — *being delta-supported is a reason to
look, never a reason to trust*.

It earns its place on every table tried. `EKPO` finds `C_PurchaseOrderItemDEX`
and `C_ScheduleAgreementItemDEX` and switches from WRAP to USE; `KNA1` finds
`I_BusinessPartnerCustomerDEX` and `C_SustCustomerAddressDEX`. All previously
invisible.

**The shortlist is shown, not just the winner**, because the choice is usually
a trade-off nobody else can make:

```
[1/3] EKET      USE    C_PURORDSCHEDULELINEDEX
   -> 1. C_PURORDSCHEDULELINEDEX      36%  [delta, filtered rows, released]
      2. C_SCHEDAGRMTSCHEDLINEDEX     18%  [delta, filtered rows, released]
      3. I_PURGDOCSCHEDULELINE        46%
      4. R_PURCHASINGDOCSCHEDULELINE  47%
```

Those top two are complementary slices — purchase orders and scheduling
agreements — so together they may be the answer and neither alone is. The two
that cover ~47% carry no delta at all. One name hides all of that.

Three bugs were found building it, all self-inflicted and all worth recording.
`CachedMetadataSource` did not forward `delta_supported_views`, so it fell
through to the base class's `None` and the index reported "the system did not
report which views carry delta" on a system that reports it fine — a `None`
default makes forgetting *silent*, so a reflective test now fails if any reader
goes unforwarded. The chain walk asked "is this a table?" before "is this a
view?", and `get_table` on a non-table costs two freestyle DDIC queries that
return nothing — thousands of them, and the build was still running after ten
minutes. And the progress output was invisible, because Python block-buffers
stdout to a pipe: *progress nobody sees is worse than none — the same silence,
plus the belief that something is broken.*

## What is already built

The tool was good at answering *what should we build for VBAP* and had no
memory at all. Run it twice over the same list and it proposed the same objects
the second time.

Not theoretical. Planning `BKPF` suggested a wrapper over
`I_AccountingDocument` named `ZW_ACCOUNTINGDOCUMENT` — while `ZW_ACCTGDOC`
already existed, was already delta-supported, and was already a wrapper over
exactly that view. The sheet said nothing, and a near-duplicate got built.

So `plan` now surveys first, and the sheet carries an `Existing` column:

```
[1/4] BKPF   SKIP   I_ACCOUNTINGDOCUMENT
      ALREADY BUILT: ZW_ACCTGDOC is already built over I_ACCOUNTINGDOCUMENT.
[2/4] EKPO   WRAP   R_PURCHASINGDOCUMENTITEM
      ALREADY BUILT over EKPO: ZDS2_EKPO over EKPO (delta), ZW_PURDOCITEM over
      EKPO (delta). A different base, so possibly not a duplicate.
```

The two cases are deliberately different. Something over the **same base** is
almost certainly a duplicate, so the suggestion flips to `SKIP` — while
recording `SKIP` as the *suggested* action too, so overriding it back shows up
in `changed`. Something over the **same table but a different base** may be a
genuinely different feed, so the note informs and the suggestion stands.
Whether an existing object makes a new one unnecessary depends on which columns
it exposes and who already consumes it — facts the reader has and the tool does
not.

Attribution follows the **FROM chain only, never a join**, the same rule the
mapping generator and the cardinality prover use: what a row *is* comes from
the root, and a view that merely joins VBAP is not a feed for VBAP.

**This one list is never served from cache.** It is the list the tool
invalidates itself — every view it creates or drops changes it. Measured: a
cached run found 25 custom extraction views where the system had 42, missing
every object created that day, and duly reported that nothing fed `BKPF` while
`ZW_ACCTGDOC` had fed it for hours. Caching had reintroduced the precise defect
the survey exists to prevent.

`cdcforge estate` runs the survey on its own, which is what someone arriving at
a system another consultant has worked on needs before deciding anything.

## Is it still good?

Everything above answers *can this work* before the fact. Nothing answered
*does it still work* after it, and the objects this tool builds sit in a system
that keeps moving under them.

```
cdcforge verify --profile DEV            # everything the estate holds
cdcforge verify --profile DEV ZW_ACCTGDOC
```

Four independent questions: is it there, does `DHCDCVCDSEXTRE` still report it
as delta-supported, does it still pass the rules, and is its base still a
released API. Each answer is a tri-state — `True`, `False`, or `None` for
*could not be established* — and `None` never counts as a pass. Reporting a
feed as healthy on the strength of a query that failed is the failure that
matters here, because nobody checks a green line twice.

Run against the reference system's 42 custom extraction views: **41 still
carrying delta, one gone**. And a fact worth knowing about the other 41 — nine
are wrappers over SAP views that carry **no release contract**
(`I_SalesDocumentBasic`, `I_SalesDocumentItemBasic`, `R_PurchasingDocument`,
`I_JVALineItemData`, several `SEPM_*`). They work today. SAP has not promised
they will work after the next support pack, which is exactly what `verify` is
for.

The first run also produced a warning that was simply false — that
`ZAOH_I_GS_SALES_CUBE` was built on `ZAOH_I_GS_SALES_PROD`, "which SAP may
change in a support pack". Both are the same team's work. A customer base is
never flagged now: keeping your own view stable is your business, and warnings
that are false are how people learn to skip warnings.

`apply` uses the same check on `USE` rows. Telling someone *replicate this, it
already carries delta* without confirming it was the one place the tool handed
over an unverified promise, and it was the easiest of all the checks to make.

## Change recording

`$TMP` needs no transport request, which is why it stayed the default — and is
also its limit: nothing built there reaches QA or production. Any other package
needs a request, and *whether* it does is never inferred from the name. CTS is
asked, through the same check Eclipse runs before it shows its transport dialog.
Measured on S/4HANA 2022, three fields decide it:

| package | `KORRFLAG` | `DLVUNIT` | `RECORDING` |
|---|---|---|---|
| `$TMP` | empty | `LOCAL` | empty |
| every Z/Y package | `X` | `HOME` | `X` |

`RECORDING=X` means a request is required, `REQUESTS` lists the ones that could
take the object, `EXISTING_REQ_ONLY` means creating one is not allowed either. A
response that does not clearly succeed **refuses the write** rather than reading
as "no request needed" — writing unrecorded because a body could not be parsed
is the one outcome nobody can undo.

Four things the wire only told us when asked:

- **Creating a request rejects every `asx:abap` shape.** With and without an
  object `REF`, as `text/plain` and as typed `as+xml` — all three answer a bare
  HTTP 400. It wants the Transport Organizer's own `tm:root` representation and
  `?_action=NEWREQUEST` on the path. The request is created free-standing; the
  package is not in the payload at all.
- **`corrNr` is checked, not assumed.** Sending it and getting a 200 is not
  proof it was honoured — an ignored query parameter looks exactly like an
  accepted one, and this codebase has already been bitten by that shape once
  (a checkrun against the `active` version of a never-activated object silently
  did not run, and read as clean). So the pipeline reads the request's object
  list back and reports `confirm recorded — ZC_X is in DEVK900851`, or says
  loudly that it is not.
- **CTS is not the authority for deletions.** Asked about a `DDLS` in a
  transportable package with `OPERATION=D` it answers `RECORDING` empty — no
  request needed — and the delete then fails with *"Parameter corrNr could not
  be found"* after the lock has been taken. A deletion outside a local package
  always needs a request, whatever the check says.
- **CTS would not stop a write into an SAP package.** `cdcforge transport SD`
  answers *"no change recording required"*. What stops it is the namespace
  guard, and the ordering is pinned by a test: `check_package` runs before the
  system is asked.
- **`RECORDING` empty does not always mean "no request needed".** An object
  stays locked to the request it was last recorded in — including after it is
  *deleted*, because the deletion is itself an entry there. CTS then reports
  `RECORDING` empty and names the holder under `LOCKS` instead:

  ```
  <RECORDING/>
  <LOCKS><CTS_OBJECT_LOCK><OBJECT_KEY>…ZCDCF_TRTEST…</OBJECT_KEY>
    <LOCK_HOLDER><REQ_HEADER><TRKORR>DEVK900851</TRKORR>…
  ```

  Empty means *already accounted for*, not *not needed*, and a write that takes
  it at face value fails with `Parameter corrNr could not be found` after the
  object exists. Only that request will do, so the tool uses it rather than
  asking — one right answer is not a question — and refuses a different one up
  front.

Where objects go is an **explicit choice**, not something inferred from a
package name someone typed: local `$TMP`, or a package with a transport
request. For the request itself, typing a number is a first-class option
alongside picking from CTS's list and creating a new one. CTS offers the
requests it considers usable *by this user for this object*, which is not the
set that would work — being handed a colleague's number and told to use it is
an ordinary way to work. A typed number is looked up before the user commits
(missing, released, or fine, and who owns it) rather than refused for being
absent from a list.

Widening the write scope is therefore a **flag, not a value**. Building the
policy as `WritePolicy(packages={args.package})` is what made the guard unable
to refuse anything the first time — the check then only confirms that the user
typed what the user typed. `allow_transportable` widens to a namespace the code
fixes, so nothing a caller passes can reach an SAP package.

```
cdcforge transport ZDSP_EXTRACTION          # read-only: what would this need?
cdcforge create ZI_X --file x.ddl --package ZDSP_EXTRACTION \
                     --new-transport "CDC Forge generated views"
cdcforge drop ZI_X --transport DEVK900851
```

## What is not here
- **Stage 5 — differentiators.** The cardinality prover's empirical half (F-14)
  is wired and works, but on real content the *structural* proof has answered
  every case so far, so the data probe has yet to fire in anger. Multi-system
  compare and drift detection are absent.
- **Stage 6 — Datasphere side.** Absent.

Two things worth flagging before Stage 2 starts, both from the spec's own
appendices: the ADT REST wire contract is unpublished and reconstructed, so every
endpoint path belongs in one config module and must be verified against the target
release; and there is no mature Python ADT client, so the connector is the
highest-risk component in the whole build and should be budgeted as such.
