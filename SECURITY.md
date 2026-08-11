# Security

This tool holds credentials for an SAP system and can create objects in it.
This document is what you should read before pointing it at a system you care
about, and what to hand a colleague along with the code.

---

## What it can do, and what it cannot

| | |
|---|---|
| **Reads** | DDL sources, DDIC metadata, repository headers, transport information, and `COUNT(*)` aggregates over business tables when you ask for a cardinality proof (`--prove`) |
| **Writes** | CDS view sources it generated, in a package you name, under a `Z`/`Y` name — create, activate, delete |
| **Never** | Modifies an SAP standard object. Not with a flag, not with an override. There is no code path that writes to a non-customer namespace |

It reads business data in exactly one place: F-14's cardinality probe, which
issues `SELECT key, COUNT(*) … GROUP BY key` to prove a join is to-one. It
retrieves key values and row counts, never row contents, and only when you pass
`--prove`. Everything else reads metadata.

### The guards, and where they live

They are all in the connector, below the UI and below the CLI, because a bug
one layer up must not be able to route around them.

- **Read-only by default.** Every session starts read-only. Writing is an
  explicit argument at the call site, never the consequence of a missing one.
- **Requests are classified, not trusted.** An unrecognised ADT path is
  classified `WRITE`, because a path nobody has established the effects of is
  not a read. `checkruns` is a `POST` that reads and is classified `READ` — the
  verb is not the classification.
- **Productive clients are refused.** The client role comes from `T000`, and an
  **unknown role counts as productive**. If the tool cannot establish what it is
  connected to, the expensive mistake is writing to production.
- **Customer namespace only.** Object names must match `[YZ][A-Z0-9_]{0,29}`.
  Packages likewise. This is checked *before* the system is asked anything —
  necessary, because CTS answers "no change recording required" for `SD`, and a
  tool that asked CTS whether an SAP package was safe to write into would be
  trusting the wrong authority.
- **A query allowlist.** Freestyle `SELECT` is restricted to fifteen named
  metadata tables. Anything else raises before a request is built.
- **Names are whitelisted, not escaped.** Everything that reaches a SQL literal
  is stripped to `[A-Z0-9_/]`, so a stray quote cannot terminate one.

---

## Credentials

- Stored in the **OS keyring** — Windows Credential Manager, macOS Keychain,
  Secret Service on Linux. Never in a profile, never in the SQLite stores,
  never in the audit log.
- A password written into a profile file **by hand is discarded on load**.
- `CDC_FORGE_PASSWORD` is honoured last, for CI and headless runs. It is last
  because an environment variable is visible to every process you run and the
  keyring is not.
- Authentication is **HTTP Basic**, which is what ADT supports. The password is
  sent on *every request*.

That last point is why TLS matters more here than it might elsewhere.

### TLS

Certificate verification is **on by default**. Turning it off needs two keys,
not one:

```yaml
verify_ssl: false
tls_override_reason: "self-signed certificate on an isolated sandbox"
```

Without the reason the tool refuses to connect — and it refuses at construction,
before a transport exists, so no password is ever sent. Self-signed
certificates really are the norm on sandboxes, so this is not a ban. It is
there because `verify_ssl: false` on its own is one word in a file that gets
copied from colleague to colleague, and it copies silently. Requiring a stated
reason means nobody turns it off without saying so, and the reason travels with
the profile to whoever reads it next.

The honest fix is `ca_bundle_path`, pointing at the CA that issued the host's
certificate. Every audit record is stamped `ssl_verified` either way.

---

## The audit log

One SQLite file per profile, at `~/.cdc-forge/<profile>.sqlite`. Every request
is recorded with its method, path, object, status, elapsed time, whether TLS was
verified, and whether the session was read-only.

**Bodies are stored as hashes, not content.** Request and response bodies are
SHA-256 digests, so the log records that a call happened and what it was for
without accumulating a copy of the metadata — or, for a data preview, of
anything it returned.

It is a local file with no integrity protection. It is evidence of what this
tool did, on the assumption that the machine it ran on is trusted. It is not
tamper-proof and does not claim to be.

---

## Dependencies

The offline core — parsing, all thirty rules, the generators — has **zero**
runtime dependencies and runs on the standard library alone. You can validate
DDL files without installing anything.

Everything else is opt-in:

| extra | packages | needed for |
|---|---|---|
| `sap` | `requests`, `keyring`, `PyYAML` | talking to a system |
| `report` | `openpyxl` | Excel decision sheets and reports |
| `ui` | `streamlit` | the web UI |
| `all` | the above | everything |

`lxml` and `pydantic` were declared here for a long time and imported nowhere.
They are gone: a dependency that does nothing still has to be trusted,
installed into a corporate environment, and patched when it has a CVE.

---

## Handing this to somebody else

They need their own profile and their own keyring entry. Nothing about a
profile is transferable except the host, port and client — the password is not
in it, and should not be shared.

```bash
python -m pip install -e ".[all]"

cdc-forge profile add --profile QAS --host sap-qas.corp --port 44300 \
                      --client 200 --user YOURUSER
cdc-forge login     --profile QAS      # password → OS keyring
cdc-forge preflight --profile QAS      # can we, and may we?
```

If the host has a self-signed certificate, prefer `--ca-bundle /path/to/ca.pem`
over `--insecure`. `--insecure` demands a typed confirmation *and* a stated
reason, and writes the reason into the profile so it travels with it.

`preflight` is the honest first step: it establishes the release, the client
role, which authorisations the user actually has, and whether TLS is verified —
and it says which of those it could not establish rather than assuming.

### Authorisations the user needs

| | |
|---|---|
| Read a CDS source | `S_DEVELOP` DDLS activity 03 |
| Metadata queries | the ADT data-preview service |
| Create and activate | `S_DEVELOP` DDLS activities 01/02, plus `S_TRANSPRT` for anything outside `$TMP` |

Give the least of these that does the job. Assessment and planning are entirely
read-only; the write authorisations are only needed for `create`, `activate`
and `drop`.

---

## Things worth knowing before you trust it

- **The ADT REST contract is unpublished.** Every endpoint is reconstructed and
  verified against S/4HANA 2025 (release 816). A different release may answer
  differently. `preflight` tests the ones it can; the rest live in one module,
  `connect/endpoints.py`, so a release change is an edit rather than a hunt.
- **A generated view is not verified by passing this tool's own rules.** The
  only judge that counts is activation on a real system, which is why the write
  pipeline runs SAP's own syntax check before it keeps anything.
- **The delta flag is SAP's, and it is derived.** `ISDELTASUPPORTED` reflects
  what a view *declares*. It is strong evidence and not a runtime proof that
  delta works end to end.
