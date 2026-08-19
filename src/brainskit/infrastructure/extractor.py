"""`CodeExtractorPort`, backed by the vendored Graphify extraction closure.

Every adaptation from Graphify's shape to Brainskit's belongs here, never inside
`infrastructure/codeanalysis/` — that directory stays byte-identical to
upstream (see its `NOTICE`). This module is the one place that:

- calls the vendored `extract`/`collect_files` in-process, scoping a scan to
  `root` (and optionally a narrower `paths`);
- keeps the tree-sitter grammars an *optional* dependency rather than
  something importing Brainskit always pays for — `available()` is a cheap
  check a caller can make before committing to a scan, and a grammar missing
  at extraction time surfaces as a `ValidationError` naming the fix, not a
  bare `ModuleNotFoundError` traceback;
- never hands a brainskit vault's own `.brain/` state files to the extractor —
  they are JSON, tree-sitter parses JSON, and a flat state dump mostly parses
  to zero nodes, which upstream reports as "please report the file(s)
  (#1666)" about files brainskit excluded from the graph on purpose (see
  `_is_vault_state_file`); and
- gives the extractor's own AST cache (`codeanalysis/cache.py`) a durable
  home when the caller has one to offer (`extract`'s `cache_root`), instead of
  the `tempfile.TemporaryDirectory` every call used before: that avoided
  littering the scanned repository (Graphify's cache otherwise defaults to
  living inside it, or wherever the process's cwd happens to be) but also
  discarded the cache's whole reason to exist — a rebuild reparsed every file
  every time, cold. `FileVault.code_cache_dir` is the persistent, vault-owned
  location a caller passes; see `_prepare_cache_dir` for how a broken cache
  degrades to a full re-extract rather than a wrong one, and
  `_cache_format_marker` for how a future re-vendor gets a fresh cache rather
  than reinterpreting an incompatible one.

Deliberately NOT reconciled with `CodeGraph.staleness` (`application/
codegraph.py`), even though both ultimately hash file content: staleness
answers "does this file still match what the graph recorded", which has to
re-read the file's real bytes every time — that recorded value is exactly
what could have gone stale, so nothing may be trusted to say otherwise. The
AST cache answers "have I already parsed this exact content before", where a
wrong guess (its optional mtime+size fastpath ahead of the real hash check)
only ever costs a redundant re-hash, never a wrong answer — a correctness bar
one notch lower than staleness needs, on top of a hash that is salted for the
extractor's own cache keys and is not the plain content hash citations,
the registry and freshness all key on elsewhere in the vault. Sharing the
mechanism would make staleness's correctness depend on a cache it does not
own; keeping them apart costs nothing the freshness model was not already
paying.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import stat
import sys
import tempfile
from contextlib import redirect_stdout
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from brainskit.domain.model import (
    GrammarNeed,
    NotConfiguredError,
    ScanSurvey,
)


def _install_hint() -> str:
    """Derived per call, never a constant.

    Which command installs a package is a property of how `bk` itself was
    installed, and the constant this replaces named `pip` — which a uv tool
    environment does not contain. See `pyenv`.
    """

    from brainskit.infrastructure.pyenv import grammar_install_hint

    return grammar_install_hint()


# Brainskit's own private state directory (`infrastructure/vault.py`). Wherever
# it appears in a scanned tree — nested at any depth, since a vault commonly
# sits inside the repository it documents (`repo/docs/brain`) — the files
# under it are the vault's config, registry, freshness and proposal JSON,
# never source a caller meant to index. `CodeGraph._vault_prefix` already
# drops any resulting node at import time, but that is after extraction has
# already spent time on them and after upstream has already printed the
# zero-nodes warning about them; filtering them out of the file list itself
# means neither happens.
_VAULT_STATE_DIR = ".brain"

# The vendored cache namespaces its own AST entries by
# `importlib.metadata.version("graphifyy")` (`codeanalysis/cache.py`), which
# always resolves to "unknown" here — the vendoring alias
# (`codeanalysis/__init__.py`) registers a synthetic module, never real
# package metadata, so that self-versioning never actually fires under
# vendoring. Without a marker of our own, a future re-vendor to a different
# revision would read and write the exact same on-disk cache directory as
# today's, and a changed extractor producing entries under an unchanged key
# is exactly the "stale cache, wrong graph" failure a persistent cache must
# not risk. `NOTICE` is the file a re-vendor is required to update (its own
# header says so); hashing it whole, rather than parsing out just the
# "Revision:"/"Version:" lines, means any edit that accompanies a re-vendor
# invalidates the cache, not only the two fields anticipated today.
_VENDORED_DIR = Path(__file__).resolve().parent / "codeanalysis"
_NOTICE_PATH = _VENDORED_DIR / "NOTICE"


@lru_cache(maxsize=1)
def _cache_format_marker() -> str:
    """A short token that changes whenever the vendored extractor does.

    Hashes the **extractor's own source**, not only `NOTICE`. Hashing the
    notice alone assumed the tree beside it never changes except at a
    re-vendor, and that assumption broke the first time it mattered: three
    files in that directory were edited -- one of them changing which results
    are written to the cache -- while `NOTICE` was untouched, so the marker
    stayed the same and entries written by the old extractor remained live
    under the new one. That is the exact "stale cache, wrong graph" failure
    this token exists to prevent.

    Reading the tree also makes the marker true by construction rather than by
    remembering to update a file, which is a weaker guarantee than the cache
    needs. It costs one pass over ~600 KB, once per process.

    An unreadable tree falls back to a fixed token rather than failing
    extraction: every entry under a marker's directory is still keyed by file
    content hash underneath, so a wrong guess can only cost a cold cache, never
    serve a wrong answer.
    """

    digest = hashlib.sha256()
    try:
        for path in sorted(_VENDORED_DIR.rglob("*.py")):
            digest.update(path.relative_to(_VENDORED_DIR).as_posix().encode())
            digest.update(path.read_bytes())
        digest.update(_NOTICE_PATH.read_bytes())
    except OSError:
        return "unversioned"
    return digest.hexdigest()[:16]


@lru_cache(maxsize=1)
def _enable_parallel_workers() -> bool:
    """Make `graphify` resolvable in a *freshly spawned* interpreter.

    The extractor parallelises above 20 files with a `ProcessPoolExecutor`, and
    a pool pickles work by qualified name -- `graphify.extract.extract_python`.
    On macOS (and anywhere the start method is `spawn`) the child is a brand
    new interpreter that re-imports `__main__` and inherits none of the
    parent's `sys.modules`. `graphify` exists only as a synthetic alias this
    package registers at runtime, so the child could not resolve it, every
    worker died, and its files produced **nothing** -- reported as coverage,
    not as an error. A polyglot fixture whose every file parses correctly in
    isolation measured 7.7%.

    It went unnoticed because `bk` happens to be immune: its console script
    imports the CLI, which imports this package, so a child re-importing
    `__main__` registers the alias on the way in. Anything else embedding the
    extractor -- pytest, a notebook, a plain script that imports brainskit
    inside a function -- got the silent version.

    The fix is to stop relying on that accident. `spawn` hands the child the
    parent's `sys.path`, so a real, importable `graphify` package on that path
    resolves in any interpreter. The package is three lines: a `__path__`
    pointing at the vendored tree, which is exactly what the in-process alias
    already does.

    **Prepended, and that reverses an earlier decision.** This used to append,
    so "a genuine Graphify installation earlier on the path keeps winning".
    Letting it win is precisely the fork: the parent binds `graphify` to the
    vendored tree -- `codeanalysis/__init__.py` now refuses to start if
    anything else holds that name -- while the child would bind it to the
    installed distribution, so a pool would extract with an implementation the
    parent never runs. Observed as `AttributeError: Can't get attribute
    '_extract_single_file' on module 'graphify.extract'`, raised while
    unpickling the work item, because that private helper is this tree's and
    not upstream's. The version that *does* have a same-named helper is the
    quiet one.

    A worker has to resolve the same `graphify` its parent did, so precedence
    is the requirement rather than mere presence. Prepending is safe on both
    counts that matter: this directory holds exactly one package -- the
    generated `graphify` -- so it can shadow nothing else, and `_shim_root`
    already refuses a directory that is not 0700 and ours, which is what stops
    a planted shim from being trusted on sight.

    Failure to write the shim is not fatal -- `_run` degrades to sequential
    extraction, which is slower and correct, and `CodeGraph` reports any file
    that went unexplained regardless.
    """

    directory = _shim_root()
    if directory is None:
        return False
    entry = str(directory)
    # Move it rather than skip it: an entry already on the path may be sitting
    # behind an installed `graphify`, which is the state this exists to fix.
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
    return True




@lru_cache(maxsize=1)
def _skipping_extensions() -> frozenset[str]:
    """Extensions whose extractor can decline a file on purpose.

    Detected rather than listed: an extractor with a deliberate-skip path
    returns a `skipped` marker. Today that is JSON alone (`extract_json`
    refuses data-shaped documents, upstream #1224), but a list would go stale
    the moment a second one learns to.

    Checked per *function*, not per file: `graphify.extract` is one 260 KB
    file holding dozens of extractors, and a whole-file substring search for
    `"skipped"` used to flag every extension dispatched from that file the
    moment any one of them mentioned the word -- 41 extensions instead of the
    one that actually implements a skip path. That mattered beyond noise:
    `_deliberate_skips`'s probe budget then went to confirming, one file at a
    time, that TypeScript and Python files never skip -- which they never do
    -- and ran out before reaching every `.json` file in a repository large
    enough to have more than a few hundred candidate files of any kind,
    undercounting the one extension it exists to count.
    """

    found: set[str] = set()
    try:
        importlib.import_module("brainskit.infrastructure.codeanalysis")
        dispatch = importlib.import_module("graphify.extract")._DISPATCH
    except Exception:
        return frozenset()
    for extension, function in dispatch.items():
        if _function_might_skip(function):
            found.add(extension.lower())
    return frozenset(found)


def _function_might_skip(function: Any) -> bool:
    """Whether `function`'s own bytecode can return a `"skipped"` result.

    A dict literal with constant keys -- `{"nodes": [], "skipped": reason}`,
    exactly the shape a skip path returns -- compiles to a *tuple* of key
    names plus `BUILD_CONST_KEY_MAP`, not a bare string, so both shapes are
    checked. Inner functions and comprehensions compile to nested code
    objects, and a skip path built inside a closure would otherwise look
    skip-free -- the same reason `_grammars_for_function` recurses.
    """

    code = getattr(function, "__code__", None)
    if code is None:
        return False

    def visit(block: Any) -> bool:
        for const in block.co_consts:
            if isinstance(const, str) and const == "skipped":
                return True
            if isinstance(const, tuple) and "skipped" in const:
                return True
        for const in block.co_consts:
            if hasattr(const, "co_consts") and visit(const):
                return True
        return False

    return visit(code)


def _deliberate_skips(paths: list[Path], cap: int = 400) -> int:
    """Files the extractor will decline to index, counted before the build.

    Without this the shortfall check accuses the extractor of losing files it
    never intended to keep. Six JSON Schema documents in brainskit's own tree
    were reported as "produced nothing, and no reason was given" -- the reason
    existed, it just had nowhere to be read from.

    Only extensions with a skip path are probed, and only up to `cap`, so the
    cost is bounded by how much data-shaped JSON a tree holds rather than by
    its size.
    """

    extensions = _skipping_extensions()
    if not extensions:
        return 0
    try:
        get_extractor = importlib.import_module("graphify.extract")._get_extractor
    except Exception:
        return 0
    skipped = 0
    probed = 0
    for path in paths:
        if path.suffix.lower() not in extensions:
            continue
        probed += 1
        if probed > cap:
            break
        extractor = get_extractor(path)
        if extractor is None:
            continue
        try:
            result = extractor(path)
        except Exception:  # noqa: S112 -- an extractor that raises is not a
            # deliberate skip; leaving it uncounted keeps it in the shortfall,
            # which is where a genuine failure belongs.
            continue
        if result.get("skipped") and not (result.get("nodes") or []):
            skipped += 1
    return skipped


def _present_on_disk(root: Path, targets: list[Path], cap: int = 64) -> int:
    """Files with a known extension that simply exist, ignoring every rule.

    The extractor's walk honours `.gitignore` and prunes conventionally noisy
    directory names, which is right almost always and catastrophic in the one
    case where the scan root *is* such a directory: it collects nothing, the
    graph comes out empty, and the only message anyone gets is "no code nodes"
    -- which reads as a broken extractor rather than an ignored path. Ten
    repositories cached under `benchmarks/.cache/` all measured zero this way,
    and working out why took longer than writing the benchmark did.

    Counting is capped because the answer is only ever compared against zero.
    """

    known = _extension_grammars()
    seen = 0
    for target in targets:
        base = target if target.is_absolute() else root / target
        if base.is_file():
            seen += 1 if base.suffix.lower() in known else 0
            continue
        for _, directories, filenames in os.walk(base):
            directories[:] = [d for d in directories if d != ".git"]
            for name in filenames:
                if Path(name).suffix.lower() in known:
                    seen += 1
                    if seen >= cap:
                        return seen
    return seen


def _parallel_allowed() -> bool:
    """Whether to use the worker pool at all.

    `BK_NO_PARALLEL=1` forces the sequential path. It exists because the
    difference between "a worker pool is broken" and "extraction is broken" is
    otherwise invisible from the outside, and that ambiguity cost real time:
    one escape hatch turns an hour of guessing into one run.
    """

    if os.environ.get("BK_NO_PARALLEL"):
        return False
    return _enable_parallel_workers()


def _shim_owner() -> str:
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    return os.environ.get("USERNAME") or "user"


def _private_directory(path: Path) -> Path | None:
    """`path`, but only if it is a real directory this user alone can write.

    `mkdir(mode=0o700)` does not chmod a directory that already exists, so the
    permission bits are checked rather than assumed, and `lstat` is used so a
    symlink pointing somewhere writable cannot pass as the directory itself.
    """

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        return None
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        return None
    return path


def _shim_root() -> Path | None:
    """Where the generated `graphify` package lives, in a directory we own.

    This path ends up on `sys.path` and spawned workers import from it, so
    whoever can create the file decides what runs inside a build. The name is a
    hash of the shipped tree and is therefore identical on every install, and
    `gettempdir()` is world-writable `/tmp` on Linux -- so a bare shared path
    could be pre-created by another local user and would then be trusted on
    sight. Namespacing by uid and refusing a directory that is not 0700 and
    ours means a planted shim is ignored; failing that check costs the worker
    pool, never correctness (`_parallel_allowed` degrades to sequential).
    """

    home = _private_directory(
        Path(tempfile.gettempdir()) / f"brainskit-graphify-{_shim_owner()}"
    )
    if home is None:
        return None
    package = home / _cache_format_marker()
    module = package / "graphify" / "__init__.py"
    if module.is_file():
        return package
    try:
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(
            '"""Generated by brainskit. Resolves the vendored Graphify tree in a\n'
            "spawned worker, which inherits sys.path but not sys.modules.\n"
            f'"""\n\n__path__ = [{str(_VENDORED_DIR)!r}]\n',
            encoding="utf-8",
        )
    except OSError:
        return None
    return package


@lru_cache(maxsize=4096)
def _encloses_a_vault(directory: Path) -> bool:
    """Whether this directory is the root of a brainskit vault.

    Cached because it is asked once per scanned directory on a walk that can
    reach thousands of files, and the answer cannot change during one call.
    """

    return (directory / _VAULT_STATE_DIR / "config.json").is_file()


def _is_vault_file(path: Path, root: Path) -> bool:
    """Whether a scanned file belongs to a vault rather than to the code.

    Every directory between the file and the scan root is tested, not just the
    file's own parents' names, because a vault is identified by containing
    `.brain/config.json` — the same marker `FileVault.discover` walks up for.
    Matching on the directory name alone would only find `.brain/` itself and
    leave the rest of the vault (`graph/`, `wiki/`, `views/`, `raw/`) to be
    parsed, warned about, and then dropped again at normalisation.

    Testing for the marker rather than for one known path also covers the case
    a name check cannot: a second vault nested somewhere in the scanned tree.
    """

    try:
        relative = path.relative_to(root)
    except ValueError:
        return _VAULT_STATE_DIR in path.parts

    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if _encloses_a_vault(current):
            return True
    return _VAULT_STATE_DIR in relative.parts


#: The tree-sitter runtime and a bundle nothing here uses -- neither is a
#: grammar anyone can be told to install. Same exclusions, for the same reason,
#: as `tests/test_code_grammars.py`.
_NOT_GRAMMARS = frozenset({"tree_sitter", "tree_sitter_version", "tree_sitter_languages"})


@lru_cache(maxsize=1)
def _extension_grammars() -> dict[str, tuple[str, ...]]:
    """Extension -> the grammar modules its extractor needs.

    Read off the *function*, not its module: `graphify.extract` is one 260 KB
    file holding many extractors, so attributing its imports by module told us
    that `.swift` needed `tree_sitter_python`. Per-function is exact, and two
    shapes have to be covered because the extractors use both:

    - **direct** — `extract_sql` does `import tree_sitter_sql` in its own body,
      which appears in `co_names`;
    - **engine-driven** — `extract_python` delegates to `_extract_generic(path,
      _PYTHON_CONFIG)`, and the grammar is `_PYTHON_CONFIG.ts_module`. That
      config is a live `LanguageConfig` reachable through the function's
      globals, so it is read rather than parsed.

    Reading bytecode names and a runtime attribute means no regex can drift
    against a refactor of the extractors, and nothing in the vendored directory
    has to be edited to expose the mapping.

    Extensions that resolve to nothing are extractors that do not use
    tree-sitter at all (`.md`, `.sln`, `.csproj`, `.dart` — regex and XML
    readers). Absent from the mapping is the correct answer for them: there is
    no grammar to be missing.
    """

    # Order matters and must survive an import sorter. `graphify` is a
    # synthetic alias that importing the vendored package registers, so it has
    # to be registered *before* anything asks for it -- and an `import`
    # statement here gets hoisted above the one it depends on the first time
    # `ruff --fix` runs, at which point `bk doctor` silently reports zero
    # grammars known instead of failing. `import_module` is a call, not an
    # import statement, so nothing may reorder it.
    importlib.import_module("brainskit.infrastructure.codeanalysis")
    dispatch = importlib.import_module("graphify.extract")._DISPATCH

    mapping: dict[str, tuple[str, ...]] = {}
    cache: dict[int, tuple[str, ...]] = {}
    for extension, function in dispatch.items():
        key = id(function)
        if key not in cache:
            cache[key] = _grammars_for_function(function)
        if cache[key]:
            mapping[extension.lower()] = cache[key]
    return mapping


def _grammars_for_function(function: Any) -> tuple[str, ...]:
    LanguageConfig = importlib.import_module(
        "graphify.extractors.models"
    ).LanguageConfig

    code = getattr(function, "__code__", None)
    if code is None:
        return ()
    globals_ = getattr(function, "__globals__", None) or {}
    found: set[str] = set()

    def visit(block: Any) -> None:
        for name in getattr(block, "co_names", ()):
            if name.startswith("tree_sitter_"):
                found.add(name)
            referenced = globals_.get(name)
            if isinstance(referenced, LanguageConfig):
                module = getattr(referenced, "ts_module", "")
                if module:
                    found.add(module)
        # Inner functions and comprehensions compile to nested code objects,
        # and an extractor that defers its import into a closure would
        # otherwise look grammar-free.
        for const in getattr(block, "co_consts", ()):
            if hasattr(const, "co_names"):
                visit(const)

    visit(code)
    return tuple(sorted(found - _NOT_GRAMMARS))


def _grammars_for_extension(extension: str) -> tuple[str, ...]:
    try:
        return _extension_grammars().get(extension.lower(), ())
    except Exception:
        # A survey is advisory. If the vendored dispatch table cannot be read,
        # the caller loses the install offer, not the ability to build.
        return ()


class _ExtractFn(Protocol):
    #: `parallel` was always part of the vendored signature; this Protocol
    #: simply had not declared it, because nothing here passed it. Brainskit
    #: does now -- a worker pool whose children cannot resolve the extractor
    #: has to be able to degrade to sequential rather than lose files silently.
    def __call__(
        self,
        paths: list[Path],
        cache_root: Path | None = None,
        *,
        root: Path | None = None,
        parallel: bool = True,
    ) -> dict[str, Any]: ...


class _CollectFn(Protocol):
    def __call__(self, target: Path, *, root: Path | None = None) -> list[Path]: ...


_extract: _ExtractFn | None = None
_collect_files: _CollectFn | None = None


def _load() -> tuple[_ExtractFn, _CollectFn]:
    """Import the vendored extractor, once, translating a missing grammar.

    Deferred rather than imported at module load: constructing
    `GraphifyExtractor` must not cost every caller of `BrainskitService` an
    import of the whole vendored closure, let alone fail one that never
    touches `code build` just because `tree-sitter` is not installed.
    """

    global _extract, _collect_files
    if _extract is None or _collect_files is None:
        # Side effect only: installs the `graphify` alias in `sys.modules`.
        import brainskit.infrastructure.codeanalysis  # noqa: F401

        try:
            from graphify.extract import (  # type: ignore[import-not-found]
                collect_files,
                extract,
            )
        except ImportError as exc:
            raise NotConfiguredError(
                "Code extraction requires the optional tree-sitter grammars",
                details={"hint": _install_hint()},
            ) from exc
        _extract, _collect_files = extract, collect_files
    return _extract, _collect_files


class GraphifyExtractor:
    """Extracts a repository's code graph in-process via vendored Graphify."""

    def survey(self, root: Path, paths: list[Path] | None = None) -> ScanSurvey:
        """Measure a scan without performing it: how many files, which grammars.

        Both answers come from one walk because both were missing for the same
        reason -- nothing looked at a scan before running it. The file count is
        what `CodeGraph.build` refuses on; the grammar list is what the CLI
        offers to install.

        The grammar half reads the vendored extractors rather than editing them
        (that directory has its own rules -- see its `NOTICE`). Every extractor
        defers `import tree_sitter_<lang>` into the function body, so the
        mapping extension -> grammar is recoverable statically: `_DISPATCH`
        gives extension -> extractor function, `__module__` gives the file, and
        the import statement in that file names the grammar. `find_spec` then
        answers whether it is installed, without importing a compiled parser
        this process may never need.

        Reading source rather than asking the extractor is not the shortest
        route, it is the only one: the extractor discovers a missing grammar
        per file, counts it into a local, prints a warning and returns
        `{"nodes", "edges"}` -- by the time a caller could observe the problem,
        the scan it should have prevented has already run.
        """

        _, collect_fn = _load()
        resolved_root = root.resolve()
        targets = paths or [resolved_root]
        seen: set[Path] = set()
        by_extension: dict[str, int] = {}
        for target in targets:
            candidate = target if target.is_absolute() else resolved_root / target
            for found in collect_fn(candidate, root=resolved_root):
                if found in seen or _is_vault_file(found, resolved_root):
                    continue
                seen.add(found)
                by_extension[found.suffix.lower()] = (
                    by_extension.get(found.suffix.lower(), 0) + 1
                )

        needs: dict[str, dict[str, Any]] = {}
        for extension, count in by_extension.items():
            for module in _grammars_for_extension(extension):
                entry = needs.setdefault(
                    module, {"extensions": set(), "files": 0}
                )
                entry["extensions"].add(extension)
                entry["files"] += count
        grammars = tuple(
            GrammarNeed(
                module=module,
                distribution=module.replace("_", "-"),
                installed=importlib.util.find_spec(module) is not None,
                extensions=tuple(sorted(entry["extensions"])),
                files=entry["files"],
            )
            for module, entry in sorted(needs.items())
        )
        return ScanSurvey(
            root=str(resolved_root),
            files=len(seen),
            grammars=grammars,
            present=_present_on_disk(resolved_root, targets),
            skipped=_deliberate_skips(sorted(seen)),
        )

    def available(self) -> bool:
        """Whether the optional tree-sitter grammars are installed.

        Checked directly rather than inferred from importing `graphify.extract`
        succeeding: that import is pure Python and always succeeds (every
        grammar import inside it is deferred to the language it parses), so it
        cannot tell a caller anything about whether extraction will work.

        `find_spec` rather than a real `import tree_sitter`: a static import
        needs `# type: ignore[import-not-found]` when the package is absent
        (as this repo's `neo4j`/`psycopg` imports already do) and mypy calls
        that same ignore unused the moment the package IS installed — and a
        contributor working on this exact file is more likely than most to
        have just run `uv sync --extra code`. A spec lookup by string is
        never a static import, so it gives mypy nothing to have an opinion on
        either way.
        """

        return importlib.util.find_spec("tree_sitter") is not None

    def extract(
        self,
        root: Path,
        paths: list[Path] | None = None,
        *,
        cache_root: Path | None = None,
    ) -> dict[str, Any]:
        """Extract nodes and edges for `root` (optionally scoped to `paths`).

        `cache_root`, when given, is a directory Brainskit owns — in practice
        `FileVault.code_cache_dir` — that the extractor's AST cache is allowed
        to persist in across calls, so a later extraction only reparses files
        that changed. Omitted (the default), extraction falls back to a
        throwaway directory as before: correct, just cold every time. The
        parameter is additive and keyword-only, so every existing call site
        that does not pass it keeps its current behaviour unchanged.
        """

        extract_fn, collect_fn = _load()
        resolved_root = root.resolve()
        targets = paths or [resolved_root]

        seen: set[Path] = set()
        files: list[Path] = []
        for target in targets:
            candidate = target if target.is_absolute() else resolved_root / target
            for found in collect_fn(candidate, root=resolved_root):
                if found in seen or _is_vault_file(found, resolved_root):
                    continue
                seen.add(found)
                files.append(found)

        cache_dir = self._prepare_cache_dir(cache_root)
        if cache_dir is not None:
            try:
                return self._run(extract_fn, files, cache_dir, resolved_root)
            except OSError:
                # `_prepare_cache_dir`'s write-probe only proves the top of the
                # directory is usable; a vendored subdirectory further down
                # replaced by a stray file (leftover from an interrupted run, a
                # manual `rm` that missed a level) would still surface here.
                # Per-file extraction failures are already caught inside the
                # vendored extractor and turned into a result with an `error`
                # field rather than an exception (`extract.py`'s
                # `_extract_single_file`), so an `OSError` escaping this call is
                # a cache-directory problem, not a source file one — falling
                # back to a fresh directory costs a cold extraction, which is
                # the same contract `_prepare_cache_dir` already promises: a
                # broken cache degrades to slow, never to wrong.
                pass
        with tempfile.TemporaryDirectory(prefix="brainskit-codeanalysis-") as fallback:
            return self._run(extract_fn, files, Path(fallback), resolved_root)

    def _run(
        self,
        extract_fn: _ExtractFn,
        files: list[Path],
        cache_dir: Path,
        root: Path,
    ) -> dict[str, Any]:
        _enable_parallel_workers()
        try:
            # The vendored extractor prints its own cold-cache progress lines
            # straight to stdout (no `file=sys.stderr`) once a scan crosses its
            # internal batch-size threshold. Brainskit's CLI also writes to
            # stdout -- the JSON result of `bk code build --json` among it --
            # so an unredirected call interleaves plain-text progress into that
            # payload and corrupts it. Redirecting here, at the one call site
            # that invokes vendored code, keeps the fix off `codeanalysis/`
            # (which stays byte-identical to upstream) while still surfacing
            # the progress lines on stderr instead of discarding them.
            with redirect_stdout(sys.stderr):
                result = extract_fn(
                    files, cache_dir, root=root, parallel=_parallel_allowed()
                )
        except ImportError as exc:
            raise NotConfiguredError(
                "Code extraction requires the optional tree-sitter grammars",
                details={"hint": _install_hint()},
            ) from exc
        return {"nodes": result.get("nodes", []), "edges": result.get("edges", [])}

    def _prepare_cache_dir(self, cache_root: Path | None) -> Path | None:
        """A durable, writable directory for this vendored revision, or None.

        `None` tells `extract` to fall back to a throwaway directory instead
        of raising: the persistent cache is an optimisation, so anything short
        of a writable directory — no `cache_root` given, a permissions
        problem, a stray file sitting where a directory belongs — degrades to
        the slower-but-always-correct path. `_cache_format_marker` scopes the
        directory to the vendored revision that will read and write it, so a
        re-vendor starts a fresh subtree rather than reinterpreting another
        revision's entries; the vendored cache's own per-entry and
        stat-index corruption handling (`cache.py`'s `load_cached`/
        `_ensure_stat_index`, both catching `json.JSONDecodeError`/`OSError`
        and treating the result as a cache miss) covers everything below this
        directory already existing and being usable.
        """

        if cache_root is None:
            return None
        candidate = cache_root / f"v{_cache_format_marker()}"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            descriptor, probe_path = tempfile.mkstemp(dir=candidate, prefix=".write-probe-")
            os.close(descriptor)
            os.unlink(probe_path)
        except OSError:
            return None
        return candidate


def grammar_inventory() -> dict[str, bool]:
    """Every grammar the shipped extractors can drive, and whether it is here.

    Derived from the extractors themselves rather than from `pyproject.toml`'s
    extras: the extras are what someone chose to pin, the dispatch table is
    what the code will actually reach for, and the gap between the two *was*
    the bug — 29 grammars imported, 13 pinned, the other 16 unreachable with no
    way to notice short of building a graph and spotting an absent language.

    An unreadable extractor tree (tree-sitter absent, so the vendored closure
    never imported) yields an empty inventory rather than raising: at that
    point "cannot say" is the honest answer, and `bk doctor` reports it as
    zero known instead of failing.
    """

    try:
        modules = {m for grammars in _extension_grammars().values() for m in grammars}
    except Exception:
        return {}
    return {
        module.replace("_", "-"): importlib.util.find_spec(module) is not None
        for module in sorted(modules)
    }
