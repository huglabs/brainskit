# ADR 0009 — Installation facts are inside the privacy boundary

Date: 2026-09-27 · Status: accepted · Closes TC1
([#12](https://github.com/huglabs/brainskit/issues/12)), raised by the twin
checks of 13 Aug and left open by the 0.6.2 close-out because it is a contract
decision, not a one-line redaction.

## Context

`Reader.reader_status` — the method behind the web viewer's `/api/status` —
returned `"vault": str(self.vault.root)` to every consumer. Every other key on
that response is consumer-scoped: `sources`, `wiki_pages`, `by_branch`,
`freshness` and the lint findings are all filtered, and `redacted_sources`
exists precisely to report a count instead of content. So a viewer bound at
`--consumer cloud` withheld a restricted source's filename and branch with some
care, and then named the directory the whole vault lives in, at the top of the
same object:

```json
{"vault": "/Users/<name>/Projects/<client>/docs/brain", "sources": 0,
 "consumer": "cloud", "redacted_sources": 1, ...}
```

The vault's doctrine is that a filename and a branch name are disclosure in
their own right. An absolute path is the same class of fact and usually a
richer one: it carries the operator's account name, the project or client the
vault belongs to, and the layout of a machine the `cloud` consumer is, by
definition, not on.

The roadmap row put the question the right way round: filtering this key means
deciding that installation facts are inside the boundary, and that decision has
to hold for every response that carries one — not for the one that happened to
be noticed.

## Decision

1. **Installation facts are inside the privacy boundary.** An installation fact
   is an absolute local path: the vault root, the home directory, the workspace
   an agent is installed into, the code root, the interpreter. A `cloud`
   consumer is never told one on any consumer-scoped response. `local` and
   `human` are on this machine, so there is nothing to withhold from them, and
   they keep every such key they have today.
2. **The rule is stated once, in the domain.** `Consumer.sees_installation()`
   in `domain/privacy.py` beside `Consumer.allows`, and bound to a request by
   `PrivacyBoundary.installation_facts(**facts)`, which returns the facts for
   a consumer that may see them and `{}` for one that may not. A response
   spreads it: `**boundary.installation_facts(vault=str(self.vault.root))`.
   No call site compares a consumer name to `"cloud"`.
3. **The key is omitted, not replaced.** A `cloud` `/api/status` has no `vault`
   key at all, the way a redacted source is counted rather than described.
   There is no `redacted_installation` flag either: the response already
   carries `"consumer": "cloud"`, and the omission is a property of that
   consumer rather than of this vault, so there is nothing per-vault to count.
   A value that *contains* a path rather than being one — the outdated-hook
   reinstall hint — keeps its key and loses only the path, replaced by `<path>`
   as `_safe_reason` already does for exception text: the command is the
   useful part, and dropping it would hide the remedy along with the fact.

## Classification

Every response found carrying an absolute path, by who can receive it. The
inventory started from `grep -rn '"vault": *str(' src/brainskit` and widened to
every key built from `str(<path>)` (`path`, `root`, `code_root`, `workspace`,
`registry`, `executable`), then error `details` that reach MCP or HTTP.
*Consumer-scoped* means the caller names a consumer and the response is filtered
for it; *operator-only* means a CLI command with no consumer, run by the person
who owns the machine.

| Site | Carries | Reached by | Class | `cloud` gets |
|---|---|---|---|---|
| `application/reader.py` `Reader.reader_status` | `vault` | `/api/status`, `BrainskitService.reader_status` | consumer-scoped | **key omitted** (this ADR) |
| `application/reader.py` `_reportable_enforcement` | a layer's reinstall `hint` (`bk hooks install … --root <workspace>`) | `/api/status` | consumer-scoped | the command, with each absolute path replaced by `<path>` |
| `application/health.py` `Health.status` | `vault` | `bk status`, MCP `status` tool, `reader_status(consumer="human")` | not consumer-scoped; human and the MCP transport's `local` scope | unreachable |
| `application/doctor.py` `doctor_report` | `vault`, `environment.executable`, `code.root` | `bk doctor` | operator-only | unreachable |
| `interfaces/cli.py` `bk init` result | `vault` | `bk init`, the `bk web` no-vault prompt | operator-only | unreachable |
| `interfaces/cli.py` `_sync_one_vault` | `vault` | `bk vaults sync` report | operator-only; the stores themselves get `vault_id` | unreachable |
| `infrastructure/vaults.py` register/forget/list | `vault`, `path`, `registry` | `bk vaults …` — CLI-only, not on MCP by design | operator-only | unreachable |
| `infrastructure/vault.py` open/init refusals | `vault` in `details` | raised opening or creating a vault, before any server serves | operator-only | unreachable |
| `application/codegraph.py` payload and `code_root` refusals | `code_root`, repository paths | `bk code …`, `/api/code-graph` | `local-only`: `CodeGraph._read` refuses `cloud` before the file is opened | refusal, no path |
| `interfaces/errors.py` `install_hint_for` | `uv` and interpreter paths in `hint` | any error with `needs`; raised only on code-graph paths | after the `cloud` refusal above — **except `CodeGraph.communities`**, which loads `networkx` before `_read` | refusal, no path (fixed by this ADR) |
| `application/installer.py` | `workspace`, `path` | `bk hooks install`, `bk agent install` | operator-only | unreachable |
| `application/capture.py`, `infrastructure/vault.py` capture | `path`, `source` | MCP `capture`, CLI; web capture is `human`-only | not a consumer-scoped read; the path is the caller's own input | unreachable |
| `infrastructure/integrations.py` via `integration_status` | `path`, container, DSN env names | `/api/integrations`, MCP `integration_status` | consumer-scoped since ADR 0001, and stricter: withheld from both machine consumers | withheld |
| `interfaces/mcp.py` unhandled exceptions | exception text | both MCP transports | `_safe_reason` replaces path-shaped text with `<path>` | no path |
| `Retrieval.search` / `context`, `browse_*`, `timeline`, `read_resource`, `graph_data`, `enrich_list`, `proposals_for_consumer` | vault-relative paths only | web viewer, MCP `search`/`context`, CLI `--consumer` | consumer-scoped | clean, now pinned by test |

Two rows reach `cloud`. The one the issue named was a single key; the other
was found by the inventory. `bk code communities --consumer cloud --json` on a
machine without the `code` extra answers `not_configured` with
`{"hint": "/…/bin/uv pip install --python '/Users/<name>/…/python3' …"}`,
because `communities` calls `_load_analysis()` one line before
`self._read(consumer)` — the refusal every other code-graph read reaches first.
Every other absolute path is unreachable by a `cloud` consumer or was already
filtered. `tests/test_fix_services.py::InstallationFactsStayInsideTheBoundaryTest`
scans every consumer-scoped read at `cloud` for the vault root and the home
directory; `tests/test_fix_interfaces.py::CloudTransportsNameNoLocalPathTest`
does the same over the bytes the web viewer and MCP send.

## Alternatives rejected

- **A stable opaque label in place of the path.** The two candidates both
  disclose. The registered label is chosen by the operator and is usually the
  project's name. `vault_id` is a truncated SHA-256 of the absolute path — a
  stable identifier that correlates every response from one vault, and
  dictionary-attackable for guessable layouts (`/Users/<name>/<project>`). The
  viewer renders no vault name, so nothing needs the label either.
- **Leaving it and saying so.** The roadmap allowed it. It would have made the
  response's one unfiltered key the one that identifies the operator, on the
  surface whose whole purpose is being safe to hand to `cloud`.
- **A path-shaped regex over the response.** It is what `_safe_reason` does
  for free-form exception text, and it is right there, where there is no
  structure to reason about. On a structured response it is guessing by shape
  where the key already says what the value is.

## Consequences

- **A `cloud` `/api/status` no longer has a `vault` key.** A client that read
  it unconditionally now has to treat it as optional. The bundled viewer never
  rendered it. `local` and `human` responses are byte-identical to before.
- `Reader._reportable_enforcement`'s docstring said this surface reports
  `vault` "on the same footing" as enforcement. It no longer does; the docstring
  now draws the line this ADR draws.
- `CodeGraph.communities` reads the graph (and so refuses `cloud`) before it
  loads `networkx`, the order `diff` and the rest already use. The install
  hint itself is unchanged: after that reorder it reaches only `local` and
  `human`, who may see where their own interpreter lives.
  `test_code_analysis.py` had pinned the old order with a comment calling
  either order defensible; this ADR is the decision it was waiting for, and
  the test now pins the refusal first and scans the error for local paths.
- The next response to grow a path-valued key fails the two scanning tests if
  it reaches `cloud`, instead of shipping. The way to add one is
  `**boundary.installation_facts(...)`.

## Out of scope

- **Vault-relative paths** (`raw/30-public/x.md`, `wiki/concepts/y.md`) are not
  installation facts. They identify evidence and are governed by the
  per-record filter that already runs on them.
- **The MCP `status` tool** answers with `Health.status`, whose counts are not
  consumer-scoped at all. Its `vault` key is visible to the transport's `local`
  scope, which this ADR keeps; whether its counts should be filtered to `local`
  is a separate question about counts, not paths. (Decided by ADR 0010: MCP
  `status` now answers through `reader_status` under the server's declared
  consumer, which defaults to `cloud`.)
- **Operator-only CLI output** (`doctor`, `init`, `vaults`, `hooks install`)
  names paths because the person reading it is the one who owns them. Piping it
  to a third party is a choice this boundary cannot see.
- **`integration_status`** keeps ADR 0001's stricter rule — machine consumers
  get no paths there — rather than being relaxed to match this one.
