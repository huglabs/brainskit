"""`CodeExtractorPort`, backed by the vendored Graphify extraction closure.

Every adaptation from Graphify's shape to Brainkit's belongs here, never inside
`infrastructure/codeanalysis/` — that directory stays byte-identical to
upstream (see its `NOTICE`). This module is the one place that:

- calls the vendored `extract`/`collect_files` in-process, scoping a scan to
  `root` (and optionally a narrower `paths`);
- keeps the tree-sitter grammars an *optional* dependency rather than
  something importing Brainkit always pays for — `available()` is a cheap
  check a caller can make before committing to a scan, and a grammar missing
  at extraction time surfaces as a `ValidationError` naming the fix, not a
  bare `ModuleNotFoundError` traceback;
- never hands a brainkit vault's own `.brain/` state files to the extractor —
  they are JSON, tree-sitter parses JSON, and a flat state dump mostly parses
  to zero nodes, which upstream reports as "please report the file(s)
  (#1666)" about files brainkit excluded from the graph on purpose (see
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
import importlib.util
import os
import sys
import tempfile
from contextlib import redirect_stdout
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from brainkit.domain.model import ValidationError

_INSTALL_HINT = "Install the optional grammars with: pip install brainkit[code]"

# Brainkit's own private state directory (`infrastructure/vault.py`). Wherever
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
_NOTICE_PATH = Path(__file__).resolve().parent / "codeanalysis" / "NOTICE"


def _cache_format_marker() -> str:
    """A short token that changes whenever the vendored extractor does.

    A missing or unreadable `NOTICE` (it ships beside the vendored source, so
    this should not normally happen) falls back to a fixed token rather than
    failing extraction: every entry under a marker's directory is still keyed
    by file content hash underneath, so the marker can only ever cost a cold
    cache on a wrong guess, never serve a wrong answer.
    """

    try:
        digest = hashlib.sha256(_NOTICE_PATH.read_bytes()).hexdigest()
    except OSError:
        return "unversioned"
    return digest[:16]


@lru_cache(maxsize=4096)
def _encloses_a_vault(directory: Path) -> bool:
    """Whether this directory is the root of a brainkit vault.

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


class _ExtractFn(Protocol):
    def __call__(
        self, paths: list[Path], cache_root: Path | None = None, *, root: Path | None = None
    ) -> dict[str, Any]: ...


class _CollectFn(Protocol):
    def __call__(self, target: Path, *, root: Path | None = None) -> list[Path]: ...


_extract: _ExtractFn | None = None
_collect_files: _CollectFn | None = None


def _load() -> tuple[_ExtractFn, _CollectFn]:
    """Import the vendored extractor, once, translating a missing grammar.

    Deferred rather than imported at module load: constructing
    `GraphifyExtractor` must not cost every caller of `BrainkitService` an
    import of the whole vendored closure, let alone fail one that never
    touches `code build` just because `tree-sitter` is not installed.
    """

    global _extract, _collect_files
    if _extract is None or _collect_files is None:
        # Side effect only: installs the `graphify` alias in `sys.modules`.
        import brainkit.infrastructure.codeanalysis  # noqa: F401

        try:
            from graphify.extract import (  # type: ignore[import-not-found]
                collect_files,
                extract,
            )
        except ImportError as exc:
            raise ValidationError(
                "Code extraction requires the optional tree-sitter grammars",
                details={"hint": _INSTALL_HINT},
            ) from exc
        _extract, _collect_files = extract, collect_files
    return _extract, _collect_files


class GraphifyExtractor:
    """Extracts a repository's code graph in-process via vendored Graphify."""

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

        `cache_root`, when given, is a directory Brainkit owns — in practice
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
        with tempfile.TemporaryDirectory(prefix="brainkit-codeanalysis-") as fallback:
            return self._run(extract_fn, files, Path(fallback), resolved_root)

    def _run(
        self,
        extract_fn: _ExtractFn,
        files: list[Path],
        cache_dir: Path,
        root: Path,
    ) -> dict[str, Any]:
        try:
            # The vendored extractor prints its own cold-cache progress lines
            # straight to stdout (no `file=sys.stderr`) once a scan crosses its
            # internal batch-size threshold. Brainkit's CLI also writes to
            # stdout -- the JSON result of `bk code build --json` among it --
            # so an unredirected call interleaves plain-text progress into that
            # payload and corrupts it. Redirecting here, at the one call site
            # that invokes vendored code, keeps the fix off `codeanalysis/`
            # (which stays byte-identical to upstream) while still surfacing
            # the progress lines on stderr instead of discarding them.
            with redirect_stdout(sys.stderr):
                result = extract_fn(files, cache_dir, root=root)
        except ImportError as exc:
            raise ValidationError(
                "Code extraction requires the optional tree-sitter grammars",
                details={"hint": _INSTALL_HINT},
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
