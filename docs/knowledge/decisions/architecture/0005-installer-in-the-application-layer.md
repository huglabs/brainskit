# ADR 0005 — The installer is application code, and the CLI is what says so out loud

Date: 2026-08-14 · Status: accepted · The second half of the seam ADR 0004
opened. 0004 gave the writer and the readers of an install one table to iterate;
this moves the writer to the layer the readers were already in.

A file of its own rather than a second decision section in 0004 because this
repository's ADRs are one decision per file, and the two have different
subjects. 0004's context is a divergence *between two sides of a table*; this
one's is a layer boundary drawn in the wrong place. Appending it would have left
0004 with two Contexts, two lists of rejected alternatives, and a title that no
longer described its own contents — and would have buried the rejection below
under a heading about layer registries, when it is the sentence in this change
most worth finding.

## Context

`interfaces/cli.py` was 3,847 lines, and about 1,100 of them were not a command
line. `bk hooks install` resolved a workspace, wrote `.brain/agent-<agent>.json`,
rewrote a managed block inside the operator's instruction file, rendered two
shipped hook scripts, registered them in `.claude/settings.json` without
clobbering the operator's own entries, installed a git `pre-commit` hook,
retired the artefacts a former brand had left, and summarised every enforcement
layer it did or did not manage to create. `bk doctor` then ran that gate on two
paths to find out whether any of it enforced anything.

None of that talks to a terminal. All of it decides and writes, which is what
the application layer is for — and the *reader* half of the same pair,
`Health.enforcement`, has been in `application/health.py` all along. That
asymmetry is the one ADR 0004 records: writer and readers restating each other's
constants until a `--agent codex` install reported four false rows, because the
two sides sat on opposite sides of a layering rule and each answered it with a
copy.

It also had a cost 0004 did not address. With the CLI as its only entry point,
the installer could be exercised only through `tests/test_hooks_install.py`'s
end-to-end scaffolding: a real `FileVault` over a complete policy, a real
SQLite index, a real `BrainskitService`, and a real install. That is the right
apparatus for "does an install work". It is a strange one for "is this
registered hook command superseded by the one being installed", which is a
question about two strings and had no way to be asked directly.

## Decision

1. **`application/installer.py` owns writing an install.** `install_agent(vault,
   agent, *, root, force)` and every helper beneath it: workspace resolution and
   its advisory, the adapter policy, template and script rendering, the skill,
   the managed instruction block, the pre-commit hook, `settings.json`
   registration and stale-entry pruning, former-brand retirement, and the
   enforcement summary.

   A module of its own rather than more of `install.py`, for a mechanical reason
   on top of the conceptual one. `gate.py` imports `adapter_path` from
   `install.py` on the path of every Write an agent attempts; the writer needs
   `gate.py`'s managed-block sentinels and deny prefixes. Putting the writer in
   `install.py` would close an import cycle `tests/test_layering.py` forbids.
   The registry stays what its docstring promises — stdlib-only and gate-free —
   and the writer depends on both.

2. **`application/doctor.py` owns exercising one.** `probe_write_gate` runs the
   installed hook on one path it must deny and one it must allow;
   `doctor_report` assembles the four sections and decides `healthy`.

   Separate from the installer because they distrust each other: the installer's
   remit ends when the files are on disk, and this begins by refusing to believe
   they do anything. That is the "exercised, not believed" idea already named in
   CONTEXT.md, and it is why the probe did not simply join `Health` either —
   every other answer `Health` gives is "the artefact is installed and
   registered", which is the claim this exists to go behind.

3. **The dividing line is the terminal.** What stayed in `interfaces/cli.py` is
   everything that speaks: `_warn_about_inactive_enforcement`,
   `_note_pruned_stale_hooks`, `_note_legacy_migration`,
   `_note_code_graph_bootstrap`, `_offer_missing_grammars`, `_run_install`, the
   argparse wiring and the renderers. `_install_hooks` survives as the four-line
   wrapper that runs the install and then says what it did — the split is
   exactly "decide" versus "say".

4. **Where the line is not the terminal, it is `infrastructure`.** `bk doctor`
   reports which interpreter `bk` was installed into and which tree-sitter
   grammars that interpreter can reach. Both are `infrastructure`'s to know, and
   `application` may not import it. So they cross as parameters —
   `doctor(environment=…, grammars=…)`, typed by a new `EnvironmentPort` in
   `application/ports.py` — the same shape `IntegrationPort.sync` uses for
   `SyncBoundaryPort`: a capability the caller holds and this layer questions,
   never one it reaches for. The verdict stays on this side, because what counts
   as a healthy installation is not a fact about the interpreter.

   The code-graph bootstrap stayed in the CLI for the same reason: it turns a
   missing optional extra into the command that installs it *on this machine*,
   which is `_install_hint_for`'s job and `pyenv`'s knowledge.

5. **`BrainskitService` gains `install_agent` and `doctor`**, as one-line
   delegations like every other method on the facade. `interfaces/cli.py` calls
   them instead of holding the implementation, which dissolved four of the eight
   places it reached through the facade into a collaborator (`service.vault.…`,
   `service.health.…`).

6. **The seam is tested as a seam.** `tests/test_installer_seam.py` drives both
   modules with a temporary directory and a vault stub two members wide. The
   stub is the point: anything it has to grow is the installer reaching further
   into the vault than it claims to.

## Alternatives rejected

- **Expose the installer over MCP.** Putting it on the facade is a layering
  decision; handing it to a model is a different one, and the answer is no.
  `install_agent` rewrites `.claude/settings.json`, writes hook scripts and
  installs a git hook — that is, it writes the write gate. A tool that lets an
  agent reinstall, redirect or `--force` its way through those artefacts hands
  the agent the switch on the mechanism that constrains it, and it would do so
  through the one interface with no operator watching the terminal. The gate
  fails open in eight places already (`bk doctor` exists because of it); the one
  thing it must not also be is *reconfigurable by its subject*. `interfaces/mcp.py`
  is unchanged and stays that way.
- **Keep the installer in `install.py`.** One module named for the concept reads
  better right up until the import graph is checked: see Decision 1.
- **Put the probe in `health.py`.** It consumes `Health.enforcement`'s own output
  and would have needed no new module. But `Health` answers whether an artefact
  is installed, and this exists precisely because that is not the same claim; a
  probe filed under the reader it distrusts is a probe that will eventually be
  read as one of its rows.
- **Move `_doctor` whole, importing `pyenv` and `grammar_inventory`.** It is what
  the function did as an interface function, and it is a layering violation the
  moment it is not: `tests/test_layering.py` allows `application → {application,
  domain}` and nothing else. The docstring `_doctor` already carried said as
  much; Decision 4 keeps the reasoning and moves the part that is not about the
  interpreter.
- **Re-export the moved names from `interfaces/cli.py`** so no test had to
  change. That is the `INSTRUCTION_FILES` alternative 0004 already rejected, one
  layer over: a second name for the same thing, living in the module that used
  to own it, teaching the next reader that this is where it lives. The 52
  references moved to the module that owns each name instead, and not one
  assertion changed.
- **Move `bk vaults` while the file was open.** `_vaults`,
  `_sync_registered_vaults`, `_sync_one_vault`, `_vault_to_register` and
  `_schedule` are the same shape of filesystem work and were deliberately left.
  The vaults group is CLI-only on purpose — an MCP server answers under one
  vault's declared boundary, and reaching into unrelated vaults would widen it.
  Moving it into the shared application layer is the first step toward exposing
  it; the same reasoning as the first rejection above.

## Consequences

- `interfaces/cli.py` is 2,953 lines, down from 3,847. `application/installer.py`
  is 808 and `application/doctor.py` is 206.
- The full suite is unchanged at 1,339 passing before the new tests and 1,363
  after, and the 209-test enforcement net passed at 209 both before and after
  the move. Twenty-four of the new tests are the unit seam; nothing that existed
  was weakened, and only import targets changed.
- Moved code is byte-identical, which is why `installer.py` and `doctor.py`
  inherit five `ruff format` hunks from the file they came out of. `cli.py`
  fails `ruff format --check` at HEAD with 53 hunks and now fails with 47, all
  of them pre-existing; reformatting on the way past would have made the move
  unverifiable to a reviewer, which is a worse trade than a formatter debt this
  repository already carries everywhere.
- `application/ports.py` declares a fifth kind of collaborator. `EnvironmentPort`
  is read-only throughout — the describer is a frozen value object, and nothing
  on this side of the boundary has business writing to it.
- Four facade bypasses remain in `interfaces/cli.py`, all of them in the
  CLI-only group above: three `service.vault.config()` reads (`bk web`'s
  integration policy, `bk vaults sync`'s per-vault enablement, `bk schedule`'s
  crontab) and one `service.vault.root` interpolated into a printed command.
  Each is a config read in a command that does not exist on the facade, not an
  implementation living on the wrong side.
