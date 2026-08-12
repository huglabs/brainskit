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
the paths it was given.

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
