# Coding agents

`bk hooks install` teaches an agent the vault contract instead of hoping it
infers one:

```bash
bk --vault ./my-vault hooks install --agent claude
```

It installs `.claude/skills/brainskit/SKILL.md`, appends a managed block to the
agent's instruction file (`CLAUDE.md`, or `AGENTS.md`/`GEMINI.md` for the other
agents) covering how the graph is formed, where the privacy boundary applies and
which commands may write, and installs a `pre-commit` hook running `bk lint`
when the workspace is a git repository.

It also runs the first `bk code build` itself, in-process, on the same run.
Without that, `bk code status` would keep reporting `missing` until an agent
happened to notice the `code build` row in the skill's own command table and
ran it unprompted — nothing else on this path ever asks it to. The build is
best-effort: a vault that never installed the `code` extra gets the same
install hint `bk code build` would already give on its own, reported in
`code_graph` and on stderr, not a failed onboarding. Pass `--skip-code-build`
to leave the graph exactly as `bk code status` finds it — for a vault that
documents something other than a code repository, or when a slow first
extraction should not block onboarding.

Everything it writes is safe to re-run. The instruction block is fenced by
`<!-- brainskit:start -->` / `<!-- brainskit:end -->` and replaced in place, so
your own instructions keep their content and their position. An existing skill
or a pre-existing `pre-commit` hook is reported rather than overwritten; pass
`--force` to replace them. A workspace without git still installs everything
else.

## Checking that the gate really guards

`bk status` reports that each enforcement layer is installed and registered.
That is not the same claim as "a direct write to `wiki/` is refused", and the
two come apart quietly: the hook script fails open on purpose — no `python3`,
no `bk` on `PATH`, an unreachable vault, a lost executable bit — and each of
those makes it exit 0 on a write it was installed to deny, while `status` keeps
reporting the layer active.

So `bk doctor` runs it. It sends the gate one path it must refuse and one it
must allow, using the same payload Claude Code sends, and reports what actually
happened under `enforcement.write_gate_probe`. What it runs is the command
`.claude/settings.json` registers, the way Claude Code runs it — through
`sh -c`, from the workspace, with `CLAUDE_PROJECT_DIR` set — so a registration
that does not survive the shell (an unquoted path with a space exits 127, and
Claude Code lets the write through) is caught even though the script it names
works. `command` names what was run and `exercised` says whether it was the
registered command or, when nothing registers the gate, the script itself.

| `state` | Meaning |
|---|---|
| `enforcing` | a write to `wiki/` was refused and an ordinary write was not |
| `not_enforcing` | the hook is installed and let a gated write through, or its script is gone while `settings.json` still registers it |
| `over_blocking` | it refused an ordinary write outside the vault too |
| `unknown` | the hook could not be executed at all, or its recorded workspace is gone |
| `absent` | no gate is registered and no script is installed — a choice, not a fault |

A deleted gate script whose registration remains is not `absent`. Claude Code
still runs the registered command, `sh -c` exits 127, and Claude Code does not
treat that as a block, so every write goes through. `doctor` runs the command,
quotes the shell's error in `detail` and `hook_said`, and gives the reinstall
command as `hint`; `bk status` reads the layer inactive and says the same.

Both probes are decisions only: `gate check-write` writes nothing, and no probe
file is ever created. When the hook fails open it explains itself on stderr, and
that sentence is repeated back as `hook_said` because it names the missing piece
better than an exit code can. `doctor` reports `healthy: false` for every state
except `enforcing` and `absent` — an installed gate that does not guard is worse
than none, because everything else goes on reporting success. `enforcing` also
needs `status` to agree that the gate is live (`gated: true`): the probe can
pass for an unregistered script (run directly) or for an outdated copy.

The git pre-commit hook is exercised the same way, under
`enforcement.commit_lint_probe`: the hook git will run (`core.hooksPath`
honoured) is executed from the workspace with nothing on stdin. `bk lint` exits
0 when clean and 1 when it finds errors — both mean the hook linted this vault,
`enforcing`. Anything else is `not_enforcing` and quotes the first stderr line:
126/127 from the shell, or 2 from a `bk` error such as `Not a brainskit vault`.
Such a hook refuses every commit without checking one. A hook naming another
vault is reported without being run, and one without its executable bit is
`not_enforcing` because git skips it, as is a brainskit hook left in
`.git/hooks` while `core.hooksPath` points at a directory with no pre-commit;
the hint says to add `bk lint --changed` to the redirected hook, as the
installer does. A hook brainskit did not write is
`not_judged` and never executed. `not_enforcing` and `unknown` make `healthy`
false, as they do for the gate; `gated` is unaffected, because commit-time lint
catches a bypass after the fact. The lint refreshes page ages in the freshness
ledger, the same bookkeeping every commit does.

`bk status` makes the cheap half of that check without running anything: it
reads the `--vault` a generated hook passes, as sh will read it, and reports
`commit_lint` inactive when that is not this vault — a hook from before 0.8.0
that JSON-quoted a non-ASCII path, or one naming where the repository used to
be.

If the workspace recorded in `.brain/agent-<agent>.json` no longer exists —
the repository was moved — every layer says so, carries
`workspace_missing: true`, and gives the reinstall command with
`--root <project>` (the repository the vault now sits in, when there is one).
The skill file an earlier install wrote for the old vault path is replaced by
that reinstall without `--force`, as long as it was not edited.

`healthy` does not depend on the optional `[code]` extra. A default install with
no tree-sitter grammar can report `healthy: true`, so CI can gate on it. Only a
*broken* grammar install counts against it. See
[the code graph](code-graph.md#what-needs-the-extra).

### Outdated hook scripts

Every hook script the installer writes carries a `# brainskit:generated` marker.
`bk status` and `bk doctor` compare each marked script against what
`bk hooks install` would write now, using the installer's own renderer. A copy
left by an older brainskit is reported with `outdated: true` on its layer, in
`enforcement.outdated`, and with the fix in `hint`: `bk hooks install --agent
<agent>`, plus `--root` when the install recorded a workspace other than the
vault.

| Layer | When outdated | Why |
|---|---|---|
| `write_gate` | `active: false`, so `gated: false` and `healthy: false` | a stale gate may enforce rules that have since changed |
| `session_status` | stays active, reported as a warning; `healthy` is unaffected | it is observability: it can misreport the vault, but it lets no write through |

A script without the marker belongs to you. It is not judged, and a reinstall
leaves it in place unless you pass `--force`.

A repository whose `core.hooksPath` points somewhere other than `.git/hooks` —
which is what Husky sets, and what any repository may set globally — gets no
hook written at all. Git would never read it, so installing one there would
leave a file that looks installed and never runs. The refusal names the
directory git actually uses and the line to add to its own `pre-commit`, and
`commit_lint` is reported inactive by both `bk hooks install` and `bk status`
until you wire it up. `--force` does not override this: it decides whether to
replace an existing hook, not which directory git executes.

A `.claude/settings.json` carried over from somewhere else — a previous
install at a different `--root`, or a `.claude/` copied wholesale from another
project — has its stale `brainskit-gate`/`brainskit-status` entries replaced,
not left running alongside the new ones: the idempotency key is the hook's
*identity* (its template name), not the literal command path, so a command
pointing at a different vault or workspace is recognised as superseded and
pruned. Reported in `settings.pruned` and on stderr. Unrelated tooling
registered on the same event is never touched — only a command whose name
matches `brainskit-gate.sh`/`brainskit-status.sh` is ever considered stale.

## The vault is not always the workspace

An agent reads `.claude/` and its instruction file from the **project it was
opened on**. When the vault is a directory inside that project, those are two
different places, so name the project with `--root`:

```bash
bk --vault ./docs/brain hooks install --agent claude --root .
```

`--root` receives the agent configuration — `.claude/`, the instruction file
and the git `pre-commit` hook — while the vault keeps `.brain/` and the vault
path baked into the hook scripts. The default is the vault itself, which is
what a standalone vault wants.

Getting this wrong used to be silent, and silence is the expensive part: every
file lands, the summary reads like success, and not one hook is ever loaded.
So an install that would repeat that mistake — a vault with no agent
configuration of its own, nested inside a directory that has some — says so on
stderr and names the flag that fixes it:

```text
bk: WORKSPACE - everything installed, nothing will load:
      The vault is not a project root, so an agent opened on /path/to/project
      will never load what was just installed here.
      Reinstall with --root /path/to/project
```

The resolved workspace is recorded in `.brain/agent-<agent>.json`, because
nothing else on disk remembers it and `bk status` has to look in the same place
the installer wrote to. An adapter written before that field existed falls back
to the vault, so an existing install keeps reporting exactly as it did.

## What a watch will not capture

`bk watch` walks every configured source folder and captures what it finds, and
a capture cannot be taken back: a source is identified by the hash of its bytes
and `raw/` is immutable. So the walk is filtered by `ignore` in
`.brain/config.json`, a list of shell globs matched against each path segment:

```json
{
  "ignore": ["node_modules", ".git", "__pycache__", "dist", "*.log", "docs/build"]
}
```

A pattern without a separator prunes that directory anywhere it appears, so
`node_modules` costs one comparison rather than a stat per file inside it. A
pattern with one is anchored to the source folder, so `docs/build` excludes
that tree and leaves every other `build` alone. Matching is case-insensitive,
because the primary target is a case-insensitive filesystem.

`bk init` offers the defaults — version-control metadata, dependency and build
directories, editor and OS droppings — prefilled, so they can be edited rather
than discovered later. A vault created before this field existed inherits those
defaults; a vault that stores `[]` has said "ignore nothing" and gets it.
`watch --json` reports `ignored` alongside `created`, counting pruned trees
once rather than per file inside them.

The vault's own directory is always excluded, so a source folder that contains
the vault cannot re-capture `raw/` into itself.

Nor does it bring back a source you `bk forget`: the forget leaves a tombstone
keyed by content hash, a sweep that meets that content again counts it under
`forgotten` instead of capturing it, and only an explicit `bk capture` re-adds
it.
