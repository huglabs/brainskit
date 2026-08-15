# ADR 0008 — The vendored extractor is tested at the port, and reached through one module object

Date: 2026-08-14 · Status: accepted · Decided while auditing
`infrastructure/codeanalysis/` — the one directory in this repository that
nobody is allowed to fix.

## Context

`src/brainskit/infrastructure/codeanalysis/` is 27,618 lines of vendored
Graphify against 22,393 lines of first-party source: **fifty-seven per cent of
the Python in this repository is code we may not edit.** Its `NOTICE` states
the rule that makes a re-vendor "a copy rather than a merge":

> The rule is that adaptation lives OUTSIDE this directory, in the adapter that
> implements Brainskit's `CodeExtractorPort`.

`tests/test_vendoring.py` enforces the provenance half of that contract with
unusual rigour — 43 sha256 digests, the MODIFIED/REMOVED declaration lists
checked against `NOTICE` in both directions, the packaged attribution, the
cache marker. It is the best-guarded thing in the repository.

It guards the wrong axis on its own, and the audit found two consequences.

### Nothing checked that any of it extracts the right thing

The dispatch table maps 95 extensions onto 46 distinct extractor
implementations, and **not one of them had a behavioural test.** There was no
`tests/fixtures/` directory at all. The whole coverage story was: the bytes are
pinned, and the bytes are unreadable.

That pairing — un-editable code, no behavioural tests — is worse than either
half. A per-language bug is *undetectable*, because nothing would notice, and
*unfixable in place*, because the fix may not live in that directory. It also
quietly undermines the operation the `NOTICE` is written around. On a
re-vendor, the sha pin proves every byte that moved; nothing whatsoever says
whether the graph those bytes produce still means the same thing. The pin
turns a silent change into a loud one and then stops, at exactly the point
where someone has to decide whether to accept it.

### The tests were exercising a second copy of the extractor

`codeanalysis/__init__.py` registers a synthetic top-level `graphify` package
whose `__path__` points at the vendored directory, so the vendored files can
keep importing each other by upstream's absolute names without being edited.
Its docstring states the condition that makes the alias safe, and states it as
a rule:

> nothing outside this file should import a sibling module here by its real
> dotted path.

Because import one file both ways and Python imports it twice, under two names,
producing two module objects with two sets of module-level state. The rule had
no enforcement, and the suite was breaking it: nine imports in
`tests/test_code_grammars.py` reached the tree by its brainskit path while
production (`infrastructure/extractor.py`) reaches it through `graphify.*`.

Measured on this tree rather than reasoned about:

```
graphify.extract is brainskit…codeanalysis.extract   False
same `extract` function object                        False
same `collect_files` function object                  False
same `_DISPATCH`                                      False
same `_XAML_CSHARP_CLASS_CACHE`                       False
two resolver registries, 9 resolvers each, no shared object
7 vendored modules loaded under the brainskit path, 39 under graphify
```

Both registries held resolvers of the same nine names, which is what makes this
the quiet kind of fault: nothing looks wrong until state written through one
path is read through the other. The root cause is inside the vendored file —
`extract.py` mixes `from .cache` and `from .resolver_registry` with absolute
`graphify.*` imports — so the fork is always one import statement away and is
invisible in any diff.

The tests were therefore validating an instance production never runs.

## Decision

**1. The alias discipline is enforced as source text, in `test_vendoring.py`.**
`VendoredModulesAreReachedOnlyThroughTheAliasTest` sweeps every `*.py` under
`src/`, `tests/`, `benchmarks/` and `scripts/` for the vendored package name
followed by a submodule, and fails naming file, line and match. Exactly one
file is exempt: `codeanalysis/__init__.py`, which necessarily spells out the
path it is telling everyone else not to use. Checked as text rather than at
runtime because an import is a property of the file — a runtime check would
only catch the modules a given test run happened to load. Three controls
accompany it: the sweep really reads the repository, the exemption is still
earning its keep, and first-party code *does* reach the tree through the alias
(so the rule cannot be satisfied by importing nothing).

**2. The nine offending imports are routed through `graphify.*`**, with one
module-level `import brainskit.infrastructure.codeanalysis` for the alias's
side effect — the idiom `tests/test_benchmarks.py` and `benchmarks/run.py`
already use. Import paths only; no assertion was touched. After a full run of
that suite, `sys.modules` now holds **0** vendored modules under the brainskit
path and 41 under `graphify`.

**3. Behavioural tests bind to `CodeExtractorPort`, never to a vendored
internal.** `tests/test_language_extraction.py` calls `extract` and `survey`
and nothing else. That seam is the one brainskit owns and the one production
calls, so a re-vendor that moves a vendored internal cannot leave the corpus
exercising something no caller reaches. It also makes the skip honest:
`survey()` reports which grammars a scan needs and whether they are installed,
so a machine with `code` but not `code-all` skips the languages it genuinely
cannot parse and runs the rest, instead of the file vanishing behind one import
guard.

**4. A golden corpus, where adding a language is one directory.**

```
tests/fixtures/<language>/
    source/…            the files handed to the extractor (the scan root)
    expected.json       the golden — deliberately outside the scan root,
                        since `.json` is an extension the dispatch table
                        would otherwise collect as a fixture file
```

`discover()` walks `tests/fixtures/`; nothing holds a list of languages. Twelve
are covered, chosen for *spread across extractor paradigms* rather than count,
because twelve config-driven languages would exercise one code path twelve
times and leave `extractors/sql.py` — which shares nothing with it — as
untested as before:

| paradigm | what it is | fixtures |
|---|---|---|
| `config-engine` | three-line functions delegating to `_extract_generic(path, _X_CONFIG)` | python, ruby, typescript, java, csharp |
| `standalone-tree-sitter` | 100–400 lines of hand-rolled extractor, own grammar, own node vocabulary | go, rust, sql, terraform, bash, json |
| `no-grammar` | a reader that never touches tree-sitter, so it stays exercised where no grammar is installed | markdown |

`python` and `ruby` are two-file fixtures, so the resolution layer
(`resolver_registry`, `ruby_resolution`, `symbol_resolution`) is exercised at
all: their goldens carry 10 and 1 edges respectively whose endpoints live in
different files. The paradigm is declared in the golden and asserted against a
fixed vocabulary; a new fixture is written `UNDECLARED` and fails until a human
classifies it, because nothing can derive a paradigm from a file extension.

**5. Nodes and edges are compared in full, as a normalised projection.**
Sorted (nodes by id, edges by source/relation/target/file/line, each with its
own canonical JSON as tie-break), path-relative, posix-separated, JSON-native.
Nothing is dropped — labels, line numbers and metadata all stay. Determinism
was verified before committing to that: across three separate interpreters and
with the parallel path both enabled and forced off (`BK_NO_PARALLEL`), all
twelve fixtures produced byte-identical output.

**6. Regeneration classifies before it writes, and refuses the case that
matters.** A golden is a claim about *inputs → outputs*, so `--regenerate`
compares the sha256 of every fixture source recorded in the golden against the
files on disk:

| sources | graph | verdict |
|---|---|---|
| changed | — | **written** — the input moved; this is the intended workflow |
| unchanged | unchanged | no-op |
| unchanged | **changed** | **REFUSED**, with the diff printed |

The third row is the regression case by definition: same input, different
output, therefore the code under test changed behaviour. Overwriting the golden
there deletes the only evidence, which is precisely how a corpus stops being a
test. Unlocking it takes a second, differently named flag
(`--accept-extractor-change`), so "regenerate the goldens" — the thing someone
types when a test is red and they are in a hurry — can never be the thing that
erases the finding.

## Alternatives rejected

- **Fix `extract.py`'s mixed imports.** It is the actual root cause, and it is
  forbidden: the `NOTICE` says adaptation lives outside this directory, and
  `extract.py` is already one of three declared MODIFIED files. A fourth edit
  would widen an exception whose whole value is being narrow, and would have to
  be argued for in a section that exists to keep such edits visible. The fork
  is a property of *how it is imported*, and importing is done from outside, so
  the fix belongs outside.

- **Alias the package to itself** (`sys.modules["graphify"] = sys.modules[__name__]`).
  Already rejected, at length, in `codeanalysis/__init__.py`'s own docstring:
  it makes every vendored module's `__name__` lie, and it does not remove the
  double-import hazard, it generalises it.

- **Assert the import discipline at runtime** — walk `sys.modules` after
  extraction. It would catch only what a given run happened to import, and the
  discipline is about what is written, not about what today's tests reach.

- **Test against `graphify.extract` directly.** Shorter, and it binds the whole
  corpus to a vendored internal — the thing a re-vendor is most likely to move,
  and the thing the port exists so that callers do not depend on.

- **Assert node counts, or spot-check that a few names appear.** Cheap, stable,
  and it passes through exactly the drift the corpus exists to catch: a
  relation renamed, an edge lost, a symbol attributed to the wrong parent all
  leave the count intact.

- **Cover all forty-odd languages.** Breadth here is a poor trade against a
  re-runnable harness and a guarded regenerator; twelve fixtures spanning three
  paradigms exercise more distinct code than thirty spanning one. The loader
  makes the thirteenth a directory, which is the property that matters.

- **Let `--regenerate` always rewrite.** This is the default everywhere and it
  is what makes most golden suites decorative: the first red build is resolved
  by refreshing the goldens, and the corpus has then certified the regression
  it was built to catch.

- **Drop line numbers and metadata to keep the goldens quiet.** They are
  deterministic, so they are not flake — they are signal. A re-vendor that
  shifts a symbol's line or renames a metadata key *is* a change worth being
  shown. The corpus exists to make that visible, not comfortable.

## Consequences

- The vendored tree now has behavioural coverage for the first time: 12
  languages, 14 fixture source files, **107 nodes and 139 edges** pinned in
  136 KB of committed goldens, 29 tests in `test_language_extraction.py` and 4
  added to `test_vendoring.py`.

- **A re-vendor is now a decision with evidence behind it.** Copy the new tree,
  regenerate the pin, run the suite: the sha digests say which bytes moved and
  the corpus says what that did to twelve languages' graphs — as a unified diff
  the reviewer reads, not a count. Refusing to auto-accept it is the point.

- `tests/test_code_grammars.py` gained one module-level import and had nine
  import paths rewritten. Its assertions are unchanged, and it now exercises
  the same module objects production does — so a cache or resolver-registry
  bug it observes is one a `bk code build` could observe too, which was not
  previously true.

- The corpus is a fixture of *this* extractor's output, not of a specification.
  It cannot tell a correct graph from an incorrect one that has been stable
  since it was generated; what it can do is refuse to let either change without
  a human reading the diff. Where a fixture encodes something that looks wrong,
  that is a finding to raise upstream, and the golden is the artefact to raise
  it with.

- Still uncovered, deliberately: **34 of the 46 extractor implementations**,
  including every Pascal/Delphi form reader, the DM family, apex, razor,
  verilog, powershell, fortran, julia, elixir, objc, zig, the `.sln`/`.csproj`
  solution readers and the XAML/C# cross-file class cache — the one piece of
  vendored module-level state the import fork demonstrably duplicated. Adding
  any of them is one directory and one paradigm declaration.
