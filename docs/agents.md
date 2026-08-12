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
happened under `enforcement.write_gate_probe`:

| `state` | Meaning |
|---|---|
| `enforcing` | a write to `wiki/` was refused and an ordinary write was not |
| `not_enforcing` | the hook is installed and let a gated write through |
| `over_blocking` | it refused an ordinary write outside the vault too |
| `unknown` | the hook could not be executed at all |
| `absent` | no gate is installed — a choice, not a fault |

Both probes are decisions only: `gate check-write` writes nothing, and no probe
file is ever created. When the hook fails open it explains itself on stderr, and
that sentence is repeated back as `hook_said` because it names the missing piece
better than an exit code can. `doctor` reports `healthy: false` for every state
except `enforcing` and `absent` — an installed gate that does not guard is worse
than none, because everything else goes on reporting success.

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
