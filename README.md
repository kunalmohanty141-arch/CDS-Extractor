<h1 align="center">CDC Forge</h1>

<p align="center">
  <strong>Get ABAP CDS views ready for SAP Datasphere replication — with delta.</strong><br>
  Finds what already works, generates what is missing, and refuses what cannot work.
</p>

<p align="center">
  <a href="https://github.com/kunalmohanty141-arch/CDS-Extractor/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/kunalmohanty141-arch/CDS-Extractor/actions/workflows/tests.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-1f5fa9">
  <img alt="Read-only by default" src="https://img.shields.io/badge/writes-off%20by%20default-6b46c1">
</p>

---

## The problem

A SAP Datasphere Replication Flow will only offer you **delta** if the CDS view
it reads is correctly enabled for Change Data Capture. Getting that wrong is
quiet and expensive: the view activates, the flow runs, the initial load looks
perfect — and then nothing ever changes again, or duplicates appear that nobody
notices for weeks.

Whether a view qualifies depends on roughly thirty separate conditions spread
across its own DDL, every view beneath it, and the tables at the bottom. Reading
that by hand, for fifty tables, is not realistic.

**CDC Forge answers three questions per table:**

1. Is there already a view that does the job? *(often yes — and it is not always obvious which)*
2. If not, can one be built, and what exactly would it look like?
3. Will it still work in six months?

---

## What it does

| | |
|---|---|
| **Assess** | Thirty rules (R-01…R-30) over a view and its whole dependency stack. Every finding says which rule, why, and what to do about it |
| **Find** | Searches every VDM layer for existing views worth reusing, ranked by column coverage — plus an index of SAP's own delta views that the standard where-used tables cannot see |
| **Decide** | Exports an Excel sheet. You set `USE` / `WRAP` / `BUILD` / `SKIP` offline, feed it back, and it executes |
| **Generate** | Z-views over tables, wrappers over existing views, and the CDC mapping — or a refusal explaining why it cannot work |
| **Create** | Creates, syntax-checks with SAP's own checker, activates, and records the object in a transport request |
| **Verify** | Re-checks later that objects still exist, still carry delta, and still pass — the check worth running after every upgrade |

It runs as a **CLI** for batches and a **web UI** for working alongside someone.

---

## Quick start

```bash
git clone https://github.com/kunalmohanty141-arch/CDS-Extractor.git cdc-forge
cd cdc-forge
python -m pip install -e ".[all]"

python -m pytest -q                  # 718 tests, no SAP system needed
```

Point it at a system — everything here is **read-only**:

```bash
cdc-forge profile add --profile DEV --host sap-dev.corp --client 100 --user YOURUSER
cdc-forge login     --profile DEV    # prompts for the password (hidden)
cdc-forge preflight --profile DEV    # can we, and may we?

cdc-forge estate --profile DEV                        # what already exists
cdc-forge plan   --profile DEV VBAP EKPO KNA1 --out plan.xlsx
```

The password is typed at a hidden prompt and stored in your **OS keyring** —
never in a file, never on a command line. The profile holds only host, client
and username.

You get a shortlist per table, not a single answer:

```
[1/3] EKET      USE    C_PURORDSCHEDULELINEDEX
   -> 1. C_PURORDSCHEDULELINEDEX      36%  [delta, filtered rows, released]
      2. C_SCHEDAGRMTSCHEDLINEDEX     18%  [delta, filtered rows, released]
      3. I_PURGDOCSCHEDULELINE        46%
      4. R_PURCHASINGDOCSCHEDULELINE  47%
```

Those top two are complementary slices — purchase orders and scheduling
agreements — so together they may be the answer and neither alone is. The two
covering ~47% carry no delta at all. That choice is yours to make, which is why
all four are shown.

Then edit `plan.xlsx`, and:

```bash
cdc-forge apply  --profile DEV --file plan.xlsx --out-dir ddl/    # generate only
cdc-forge apply  --profile DEV --file plan.xlsx --out-dir ddl/ --create
cdc-forge verify --profile DEV                                    # and after every upgrade
```

Or `cdc-forge ui` for the same workflow in a browser.

**→ [GETTING_STARTED.md](GETTING_STARTED.md)** covers prerequisites,
authorisations and troubleshooting in full.

---

## Security

This tool holds SAP credentials and can create objects. The full model is in
**[SECURITY.md](SECURITY.md)**; the short version:

- **Read-only by default.** Writing is an explicit argument at the call site, never a default
- **Never touches an SAP object.** There is no flag, no override, and no code path that writes outside the customer (`Z…`/`Y…`) namespace
- **Refuses productive clients** — and treats an *unknown* client role as productive
- **Credentials live in the OS keyring**, never in a file. A password written into a profile by hand is discarded on load
- **TLS verification is on by default.** Turning it off needs both the switch and a written reason, because authentication is HTTP Basic — the password crosses the wire on every request
- **Every request is audited**, with bodies stored as hashes rather than content
- **A query allowlist** restricts metadata reads to fifteen named DDIC tables

Business data is read in exactly one place: an opt-in `--prove` flag that runs
`COUNT(*)` aggregates to prove a join really is to-one. Key values and row
counts, never row contents.

---

## Requirements

| | |
|---|---|
| Python | 3.11+ — the offline core has **zero** dependencies |
| SAP | S/4HANA 1909 FPS01 or newer (ABAP 7.54+), ADT service active, a non-production client |
| Auth | `S_DEVELOP` DDLS activity 03 to assess; 01/02 to create; `S_TRANSPRT` to write outside `$TMP` |

Windows, macOS and Linux. **S/4HANA Cloud public edition is not supported** —
the platform does not allow reading base tables like `VBAP`, which a CDC mapping
requires. Private edition / RISE is the same stack as on-premise.

---

## Documentation

| | |
|---|---|
| **[GETTING_STARTED.md](GETTING_STARTED.md)** | From nothing to a decision sheet in ten minutes |
| **[SECURITY.md](SECURITY.md)** | What it can and cannot do, where credentials live, which authorisations to ask for |
| **[DESIGN.md](DESIGN.md)** | Why every rule is what it is — including the ones that were wrong and got corrected by measurement |

---

## A note on how the rules were settled

The rules are not derived from documentation alone. They were checked against
SAP's own content: **904 views the reference system reports as delta-supported**.
Where a rule disagreed with SAP, the rule was investigated and usually changed —
R-07, R-11, R-12 and R-15 were all rewritten this way, and disagreement fell
from 46 views to one.

That one remaining disagreement is documented rather than hidden, along with the
reasoning for keeping it. The same applies throughout `DESIGN.md`: the mistakes
are recorded next to the fixes, because on a system nobody has documented, being
wrong in a traceable way is the only way to end up right.

---

## License

MIT — see [LICENSE](LICENSE).
