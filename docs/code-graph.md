# The code graph

`bk code` describes the repository a vault documents, so a question about the
code is answered from its structure rather than from a grep. It is a second
graph, alongside the vault's own: `bk graph` regenerates `graph/graph.json`
from the wiki, while `bk code build` extracts `graph/code.json` from the source
tree.

Every code-graph read defaults to `--consumer local`, because it carries
repository paths and is not meant to leave the machine.

## What needs the extra

Only two things need the `[code]` extra, and they need different halves of it:
`build` needs the tree-sitter grammars to extract, and `communities`, `cycles`
and `diff` need networkx for the vendored analysis. Everything else — `import`,
`status`, `affected`, `path`, `hubs` — reads the stored graph and runs on the
base install, so a graph extracted elsewhere can be imported and queried with
no extra at all. A command that does need it fails with the install hint rather
than a stack trace.

`[code]` pins grammars for the thirteen most common languages — Python, JS/TS,
Go, Rust, Java, C/C++, C#, Ruby, PHP, Bash and JSON. The vendored extractors can
drive sixteen more (SQL, Swift, Terraform/HCL, Kotlin, Objective-C, Groovy,
PowerShell, Fortran, Lua, Elixir, Julia, Scala, Zig, Verilog, Pascal, DM), and
without its grammar an extractor is unreachable. `bk code build` surveys the
scan before running it, names the grammars this repository actually needs, and
offers to install them — with the command that works for *this* interpreter,
which is not always `pip`: a uv tool environment does not contain one. Decline
and the build proceeds, reporting what could not be parsed rather than
succeeding quietly. Install one grammar, or all of them up front:

```bash
uv tool install 'brainskit[code-all]'   # every language, a much larger download
```

The split is deliberate — grammars are compiled wheels, and `code-all` roughly
triples an already large install for languages most repositories do not contain.

## Commands

```bash
bk code build                     # extract in-process and store graph/code.json
bk code import GRAPH.json         # or take one an external extractor produced
bk code status                    # does the stored graph still describe the tree?
bk code affected SYMBOL           # what breaks if this changes
bk code path FROM TO              # shortest chain of edges between two symbols
bk code hubs                      # the most connected symbols
bk code communities               # structurally cohesive clusters
bk code cycles                    # import cycles among files
bk code diff                      # what changed structurally since the stored graph
```

## What `bk code status` reports

Five states, and only three of them are about freshness:

| state | meaning | remedy |
|---|---|---|
| `fresh` | the recorded file digests all still match | — |
| `stale` | a file the graph indexed changed or was removed | `bk code import <graph.json>` |
| `missing` | there is no graph on disk, or what is there is not a JSON object | `bk code import <graph.json>` |
| `partial` | fresh, but a grammar was absent so a whole language went unindexed | install the named grammar, then rebuild |
| `malformed` | the graph parses but cannot be traversed | `bk code build` |

`partial` and `malformed` are the two that are not freshness verdicts at all.
Both exist for the same reason: a graph can match every file it recorded and
still answer nothing useful — `partial` because it never recorded the files it
could not parse, `malformed` because it cannot be traversed whatever it
recorded.

`status` answers freshness from the `files` fingerprint alone and never indexes
a node or an edge — which is why it cannot traceback, and equally why it used to
report `fresh` on a graph an edge missing `type` made unreadable,
while every other `bk code` command refused that same graph. The states are now
tied to the readers by construction: `status` says `malformed` exactly when a
reader would refuse it, and `missing` exactly when a reader would say there is
no graph, from the same two functions.

It is a report, not a failure. `bk code status` exits 0 and its `--json`
envelope is `{"ok": true, ...}` in every state, the same as `stale` and
`missing` — `ok` says the command answered, and the answer is in `state`. Only
`bk lint` and `bk vaults sync` turn a finding into a non-zero exit.

The remedy for `malformed` is a **full** `bk code build`, never a scoped one: a
scoped build merges into the stored graph and would carry the fault forward,
which is why it refuses a malformed graph rather than laundering it back out. A
full build never merges, so it recovers from any stored state.

## Where it scans

`code_root` is read from `.brain/config.json`, is relative to the vault, and is
discovered upward when absent. An explicitly empty string means the vault root,
for a vault that sits at the top of the repository it documents — the same
absent-versus-empty rule the `ignore` patterns follow. The vault's own
directories are excluded from the graph: an extractor pointed at the repository
has no idea one of those folders is the vault asking the question, and left in
they arrive as the most connected nodes in it.

Given explicit `PATH`s, `build` merges that subset into the stored graph
instead of replacing it, so a scoped re-extraction does not shrink the graph to
the paths it was given. Scoping to the code root itself — `bk code build .`, or
the root's absolute path — is the whole repository, and behaves exactly like a
bare `bk code build`.

## What a scoped build prunes

A scoped build replaces its own scope and keeps the rest. "The rest" would
otherwise accumulate phantoms — symbols at line numbers in files deleted months
ago — so a stored node whose file is gone is dropped even though it lies outside
the scope.

Deleting a node is not recoverable, so pruning takes evidence rather than
inference, and keeping is the default. Every graph records the `code_root` its
node paths are relative to, beside them in `graph/code.json`, and a stored node
is pruned only when **both** of these hold:

- the graph was written under the same root that is resolving paths now, and
- the graph's own `files` map holds a real digest for that path — the artefact
  saying that path once resolved to a readable file under that root.

What is left after both is a path that provably resolved and provably does not
now, which is a deletion. Anything else is kept and reported as stale.

That matters because `code_root` is re-resolved on every command, from a config
key you may edit and an upward walk for `.git`. Change it, or import a graph an
external extractor emitted relative to its own working directory, and the stored
paths stop resolving — through no fault of the files they name. Pruning then
stands down, `bk code build` says so in `prune_skipped`, and `bk code status`
reports every unresolvable path as `removed` until a whole-root rebuild restates
the graph. A graph written before this field existed records no root at all, and
is treated the same way: nothing is pruned until some build restates it.

## One importer, two sources

`build` and `import` share one importer, so a graph from an external extractor
is normalised on the way in rather than trusted as given. `communities`,
`cycles` and `diff` are the three questions brainskit does not answer itself and
are delegated to the vendored analysis; `affected`, `path` and `hubs` use
brainskit's own traversal, which needs no dependency and already answers under a
`--consumer`.

The vendored analysis subset, its upstream revision and the changes made to it
are recorded in [`NOTICE`](../NOTICE) and in that directory's own `NOTICE`.
Coverage numbers are in [Benchmarks](./benchmarks.md).

---
<!-- doc-tracking -->
- Created: 2026-08-13 11:32
- Updated: 2026-08-13 13:04
- Updated: 2026-08-13 13:05
