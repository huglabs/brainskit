# ADR 0004 — What an install consists of has one owner, and both the writer and the readers iterate it

Date: 2026-08-14 · Status: accepted · Decided during the same architecture
review that produced 0001–0003, applied to the seam those three did not touch:
the one between the code that *installs* enforcement and the code that *reports*
it.

## Context

`bk hooks install` is agent-aware. `interfaces/cli.py` mapped each of the four
agents to its instruction file (`claude`→`CLAUDE.md`, `codex`→`AGENTS.md`,
`gemini`→`GEMINI.md`, `opencode`→`AGENTS.md`), wrote the managed block into
`root / INSTRUCTION_FILES[agent]`, recorded the resolved workspace in
`.brain/agent-<agent>.json`, installed Claude Code hooks for `claude` and
nothing equivalent for the others, and reported an enforcement summary naming
each layer it had written.

`Health` — the reader behind `bk status`, `bk doctor` and `/api/status` — was
not agent-aware in any of those respects:

- `_agent_workspace(self, agent: str = "claude")` was called with no argument,
  so it read `.brain/agent-claude.json` whatever had been installed;
- the instruction file was `root / "CLAUDE.md"`, hardcoded, and its mechanism
  string `"CLAUDE.md managed block"`, hardcoded beside it;
- the two hook scripts were `"brainskit-gate.sh"` and `"brainskit-status.sh"`,
  typed out, while every one of the installer's artefact names is derived from
  `BRAND`;
- the four layer names and their mechanism prose were written out on both sides.

So `bk hooks install --agent codex` produced this. The reproduction is a vault
at `<project>/docs/brain` with the install pointed at `<project>`:

| | the installer said | `bk status` said |
|---|---|---|
| `commit_lint` | created, active | *"…/docs/brain is not a git repository"* |
| `instructions` | `AGENTS.md`, active | `CLAUDE.md managed block`, *"no managed block found"* |
| `write_gate` | *(not a layer codex has)* | *"brainskit-gate.sh is not installed"* |
| `session_status` | *(not a layer codex has)* | *"brainskit-status.sh is not installed"* |
| overall | `inactive: []` | `inactive: [all four]`, `healthy: false` |

Every cell is wrong, and none of them raises. The workspace resolution **falls
back silently**: with no `agent-claude.json` to read, `_agent_workspace` returns
the enclosing-project guess, and no field in the report says an adapter was
missing. So a live pre-commit hook read as a missing git repository, a managed
block that had just been written read as absent because the reader opened a
different file, and two layers brainskit does not install for codex read as
guards that had fallen off. `bk doctor` was affected too, and in the opposite
direction: its write-gate probe reported `absent` (there is no script to run)
and `absent` is on its healthy allowlist, so `doctor` announced `healthy: true`
over the same four red rows `status` was calling `healthy: false`.

This is the third time this repository has shipped a divergence of exactly this
shape. `ConstantsHaveOneOwnerTest` says so in its own docstring — it was written
after the managed-block sentinel and the gate's deny prefixes were copied across
the same boundary, "and per this repository's history that divergence has
already shipped twice". It did not catch this one because it guards *sentinel
strings*: the literal bytes that appear inside a file. The layer names, the
mechanism prose, the per-agent filenames and the brand-derived script names are
the same kind of shared knowledge and were not covered.

The reason for the copies is the one 0001 and 0002 both name: the layering rule
is that `application` must not import `interfaces`, and the response to it was
to restate the constant on the application side. `gate.py` already refuses that
answer for `INSTRUCTION_START`, `INSTRUCTION_END` and `HOOK_SENTINEL`, and
`health.py` already imports the first of them. The direction was established;
only the rest of the table had not followed.

## Decision

1. **`application/install.py` owns what an install consists of.** `AGENTS` maps
   each agent to an `AgentInstall` — its instruction file, its hooks, whether it
   gets the skill — and each `AgentHook` carries the layer it provides, its
   template, its event, its matcher, its timeout and its mechanism. The four
   layer names, `COMMIT_LINT_MECHANISM`, `DEFAULT_AGENT`, `BRAND` and
   `adapter_path` live there too.

   A module of its own rather than more of `gate.py`. The gate answers one
   question — may this write land — on the path of every Write the agent
   attempts, and nothing in this table participates in that decision; a hook
   event, a timeout and a paragraph of mechanism prose would be data the gate
   imports and never reads. What the gate *does* need is the adapter's filename,
   which it built by hand as a third spelling of the same path, so it imports
   `adapter_path` from here and the two modules stay what they are: one decides,
   one describes.

2. **`Health` reports for the agents that were installed**, discovered from the
   adapters under `.brain/`, each against the workspace *its own* adapter
   records. Not a parameter: `bk status` has no `--agent` flag and never will
   have a useful default for one, and a default is what produced the defect.

   A vault with no adapter at all is reported as `DEFAULT_AGENT`. Nothing has
   been installed yet, and the useful answer is where the layers would land —
   an empty report would read as a vault with no rules rather than a vault with
   no install.

   `agent` is stamped on a layer only when more than one is installed, because
   the field exists to disambiguate and in the overwhelmingly common case there
   is nothing to disambiguate. That is what keeps the single-`claude` report
   byte-identical, which is what most of the suite pins. `inactive` deduplicates
   names — two agents sharing one repository see the same missing `commit_lint`,
   and listing it twice would read as two faults.

3. **An agent reports the layers it has, not the layers `claude` has.** The
   installer has always said that a codex install is `commit_lint` plus
   `instructions`; the reader now says the same, because both read `hooks` off
   the registry. A `write_gate` row for an agent brainskit ships no gate for is
   not a stricter report, it is a different claim — "the guard fell off" rather
   than "there is no guard here" — and it is the one an operator would act on.

4. **The installer's summary iterates the same table.** `_enforcement_summary`
   no longer carries its own copy of the layer names, the mechanism strings or
   the instruction filename, and `_install_hooks` decides what an agent gets
   from `install.skill` / `install.hooks` rather than from `agent == "claude"`.

5. **`EnforcementLayersHaveOneOwnerTest` extends the assertion that already
   exists** for the sentinels, one class of constant wider: the layer names,
   the mechanism strings, the per-agent instruction filenames, the adapter path
   and the mechanism suffix must each be spelled in `install.py` and nowhere
   else, and *no* module may spell a brand-derived artefact name at all — those
   are built from `BRAND`, so writing `brainskit-gate.sh` anywhere is by
   definition a copy. Both halves were confirmed by putting the removed strings
   back and watching the build fail.

## Alternatives rejected

- **Give `Health.enforcement` an `agent` parameter.** Every caller would have to
  supply one, none of them has an agent to supply — `bk status`, `bk doctor` and
  `/api/status` are all asking about the vault, not about a session — and the
  parameter's default would be the hardcoded `claude` this ADR removes, wearing
  a signature.
- **Report per agent, as `{"agents": {...}}`.** A cleaner shape for the rare
  case, at the cost of changing the report for every existing caller and every
  test that reads `enforcement["layers"]`, to express something a single-agent
  vault has nothing to say about.
- **Report `write_gate` as inactive for agents that have none**, so the headline
  stays red. It restates the false claim more politely: nothing fell off, and an
  operator who reinstalls to fix it gets the same report back. If brainskit
  should refuse to call a gateless install healthy, that is one decision in the
  registry for both sides — not a string in the reader.
- **Extend `gate.py`.** See 1: the gate's remit is the write decision, and a
  layer registry is not consulted by it.
- **Keep `INSTRUCTION_FILES` in `cli.py` as a projection of `AGENTS`.** Derived,
  so it cannot drift — but it is a second name for the same table sitting in the
  layer that used to own it, and the next reader to reach for it re-establishes
  the habit. One test helper referenced it and now reads the registry.

## Consequences

- **A codex, gemini or opencode vault can now report `healthy: true` where it
  reported `false`.** Its layers are `commit_lint` and `instructions`, and
  `enforcement_ok` reads exactly the layers reported. This is the installer's
  claim being told consistently rather than a guarantee being weakened —
  brainskit has never installed a write gate for those agents — and `gated:
  false` and `bk doctor`'s `write_gate_probe: absent` both still say so in the
  same report. The place to change that verdict, if it should change, is
  `AGENTS`.
- `bk doctor`'s probe now takes the first `write_gate` layer **that names a
  script**, not simply the first. With two agents installed the rows are ordered
  by the registry and only one has a file behind it; stopping at the first would
  report `absent` for an installation whose gate is live.
- `_reportable_enforcement` passes `agent` through to `/api/status`, on the
  other side of the line it draws for `detail` and `script`: it names an agent,
  not a machine, and two identically named rows would otherwise render as one
  layer reported twice. `_enforcement_rows` labels the name the same way it
  already labels `(advisory)`.
- `gate.py` now imports one name from a sibling application module. Its
  docstring said "depends on nothing but the standard library"; it says what it
  depends on and why instead. `install.py` is stdlib-only, so the hook path
  gains nothing it has to load.
- The single-`claude` report — layers, order, keys, details — is unchanged, and
  `tests/test_enforcement_status.py`, `tests/test_hooks_install.py` and
  `tests/test_session_status_hook.py` pass untouched.
- The writer named throughout this ADR still lived in `interfaces/cli.py` when
  it was written. **ADR 0005** moves it to `application/installer.py`, beside
  the readers that were already here; the table this one introduced is what both
  sides go on iterating.
