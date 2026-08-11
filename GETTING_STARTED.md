# Getting started

From nothing to a decision sheet in about ten minutes. Everything up to the
last section is **read-only** — it cannot change your SAP system.

---

## 1. Prerequisites

**On your machine**

| | |
|---|---|
| Python | **3.11 or newer** — `python --version` |
| Disk | ~50 MB, plus a cache of a few MB per system |
| Network | HTTPS to your SAP system's ADT port (usually `443xx`, e.g. `44300`) |

Windows, macOS and Linux all work. There is no platform-specific code.
*(Developed and proven on Windows; the others are expected to work but have not
been run — see the portability note in the README.)*

**On the SAP system**

| | |
|---|---|
| Release | S/4HANA **1909 FPS01 or newer** (ABAP 7.54+). Earlier releases have no CDC framework |
| Service | The ADT node `/sap/bc/adt` active in SICF |
| Client | A **non-production** client. The tool refuses to write to a productive one, and treats *unknown* as productive |
| User | A dialog or system user with the authorisations below |

**Authorisations — ask for the least that does the job**

| To do this | You need |
|---|---|
| Assess, plan, verify *(read-only — start here)* | `S_DEVELOP` DDLS activity **03**, plus the ADT data-preview service |
| Create and activate objects | `S_DEVELOP` DDLS activities **01/02** |
| Write outside `$TMP` | `S_TRANSPRT` as well |

S/4HANA Cloud **public** edition is not supported — the platform does not allow
reading base tables like `VBAP`, which a CDC mapping requires. **Private
edition / RISE** is the same stack as on-premise and is expected to work.

---

## 2. Install

```bash
git clone <your-repo-url> cdc-forge
cd cdc-forge
python -m pip install -e ".[all]"
```

Check it works — this needs no SAP system at all:

```bash
python -m pytest -q                      # 716 tests
cdc-forge rules -v                       # the rule catalogue
cdc-forge validate ZI_MISSING_MAPPED_KEY --fixtures fixtures
```

If `cdc-forge` is not found, use `python -m cdcforge.cli` instead — same thing.

---

## 3. Connect

```bash
cdc-forge profile add --profile DEV --host sap-dev.corp --port 44300 \
                      --client 100 --user YOURUSER
cdc-forge login     --profile DEV        # password → OS keyring, never a file
cdc-forge preflight --profile DEV
```

### Where the password actually goes

This trips people up, so plainly:

| Step | What it asks for | Where it goes |
|---|---|---|
| `profile add` | host, port, client, **username** | `~/.cdc-forge/profiles/DEV.yaml` — a plain file, and **never the password** |
| `login` | **password**, typed at a prompt | The OS keyring: Windows Credential Manager, macOS Keychain, Linux Secret Service |

```
$ cdc-forge login --profile DEV
Password for YOURUSER@sap-dev.corp client 100: ▒▒▒▒▒▒▒▒
Stored in the OS keyring under 'DEV:100:YOURUSER'.
```

The prompt is hidden — the password does not echo, and it is deliberately not a
command-line option, because a command line lands in your shell history and in
the process table where any other user can read it. If a profile file has a
`password:` line typed in by hand, it is **discarded on load**.

For CI or a headless box with no keyring, `CDC_FORGE_PASSWORD` is honoured — last
in the order, because an environment variable is visible to every process you
run and a keyring is not.

`preflight` is the honest first step. It reports the release, the client role,
which authorisations you actually have, whether TLS is verified, and — this is
the part worth reading — **which metadata sources are unavailable and what
stops working without them**.

> **Self-signed certificate?** Prefer `--ca-bundle /path/to/ca.pem`.
> `--insecure` works but demands a typed confirmation *and* a written reason,
> because authentication is HTTP Basic: your password crosses the wire on every
> request, and without verification anyone on the path can read it.

---

## 4. Plan — the main event, still read-only

```bash
cdc-forge estate --profile DEV                       # what already exists here
cdc-forge plan   --profile DEV VBAP EKPO KNA1 --out plan.xlsx
```

Or feed a list: `--from-file tables.txt` (one name per line, `#` for comments).

The first `plan` on a system builds a one-off index of SAP's delta views, which
takes a few minutes and is then cached. Expect roughly **a minute per table**
cold, seconds afterwards.

You get a shortlist per table, not a single answer:

```
[1/3] EKET      USE    C_PURORDSCHEDULELINEDEX
   -> 1. C_PURORDSCHEDULELINEDEX      36%  [delta, filtered rows, released]
      2. C_SCHEDAGRMTSCHEDLINEDEX     18%  [delta, filtered rows, released]
      3. I_PURGDOCSCHEDULELINE        46%
      4. R_PURCHASINGDOCSCHEDULELINE  47%
```

---

## 5. Decide — in Excel, offline

Open `plan.xlsx`. Four columns are yours; everything else is what the tool
found, kept so the reasoning survives.

| Column | |
|---|---|
| **Action** | `USE`, `WRAP`, `BUILD` or `SKIP` (a dropdown) |
| **Base** | the existing view to use or wrap — pick from `Candidates` |
| **Target name** | the `Z…`/`Y…` object to generate |
| **Note** | yours; carried through, never interpreted |

In `Candidates`, `*` means the view already carries delta and `~` means it is
**row-filtered** — so a filtered view at 85% is usually a worse answer than an
unfiltered one at 54%. Read `Existing` first on a re-run: it is what stops you
building something twice.

The four actions:

- **USE** — replicate this view as it is. Nothing is generated.
- **WRAP** — generate a `Z` wrapper over `Base`, adding the annotations it lacks.
- **BUILD** — generate a `Z` view directly over the table.
- **SKIP** — do nothing.

---

## 6. Generate

```bash
cdc-forge apply --profile DEV --file plan.xlsx --out-dir ddl/
```

Still writes nothing to SAP. You get `.ddl` files to read, and any `USE` rows
are checked against the system to confirm they really do carry delta.

---

## 7. Create — the only step that changes anything

```bash
# Local sandbox: no transport request, never leaves this system
cdc-forge apply --profile DEV --file plan.xlsx --out-dir ddl/ --create

# A real package, recorded in a transport request
cdc-forge apply --profile DEV --file plan.xlsx --out-dir ddl/ --create \
                --package ZDSP_EXTRACTION \
                --new-transport "CDC Forge generated views"
```

`$TMP` is the default: local, non-transportable, and it needs no request.
Anything else must be a customer (`Z…`/`Y…`) package and needs a transport
request — the tool asks CTS rather than guessing, and refuses if it cannot get
a clear answer.

Each object is created, filled, checked by **SAP's own syntax check**,
activated, and confirmed to be in the transport request. Anything that fails is
rolled back and reported. Undo one with `cdc-forge drop <NAME>`.

---

## 8. Verify — now, and after every upgrade

```bash
cdc-forge verify --profile DEV
```

Checks each object still exists, still carries delta according to the system,
still passes the rules, and whether its base is still a released API. Worth
re-running after a support pack: several standard views that make good bases
carry no release contract, and the sheet says so at the time.

---

## The UI

```bash
cdc-forge ui
```

The same workflow in a browser, with the decision sheet as a download and an
upload. Useful for working alongside someone; the CLI is better for batches.

---

## If something goes wrong

| Symptom | Look at |
|---|---|
| `no password stored` | `cdc-forge login --profile DEV` |
| `returned HTTP 403` on activation | Expected while the object is locked — the tool handles it. If it persists, check `S_DEVELOP` activity 02 |
| `Parameter corrNr could not be found` | The object is already tied to a transport request; pass that one |
| Everything is `INCONCLUSIVE` | `cdc-forge preflight` — a metadata source is probably unavailable, and it will say which |
| A run seems to hang | `plan` builds its delta index on first use. Progress prints as it goes |

Every request is recorded: `cdc-forge audit --profile DEV`. Bodies are stored as
hashes, not content.

For the security model — what is stored where, and every guard and why —
see [SECURITY.md](SECURITY.md). For the reasoning behind the rules and the
measurements behind them, see [DESIGN.md](DESIGN.md).
