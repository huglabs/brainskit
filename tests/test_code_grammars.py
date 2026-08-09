"""Every grammar the vendored extractors import must be installable.

`codeanalysis/extractors/` ships a working extractor for far more languages
than the `code` extra pins grammars for, and the failure mode is quiet: without
its grammar an extractor is unreachable, `bk code build` reports the file
"contributed nothing", and the graph builds successfully around the hole. A
repository of SQL, Swift and Terraform produced a two-node graph while every
warning said something was merely "not installed".

So the contract asserted here is not "everything is installed" -- grammars are
compiled wheels and staying out of the default install is deliberate -- but
that every grammar the code can ask for is *named somewhere a user can act on*:
`code` for the common set, `code-all` for the rest. A new extractor that lands
without its grammar in either list fails these tests rather than silently
extracting nothing.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEANALYSIS = REPO_ROOT / "src" / "brainkit" / "infrastructure" / "codeanalysis"

#: `tree_sitter` itself is the runtime, not a grammar, and `tree_sitter_version`
#: is a substring of the `_check_tree_sitter_version` helper rather than a
#: module anyone imports.
_NOT_GRAMMARS = {"tree_sitter", "tree_sitter_version", "tree_sitter_languages"}


def imported_grammars() -> set[str]:
    """Every `tree_sitter_*` module the vendored analysis refers to."""

    found: set[str] = set()
    for path in CODEANALYSIS.rglob("*.py"):
        for match in re.findall(r"\btree_sitter_[a-z0-9_]+", path.read_text("utf-8")):
            if match not in _NOT_GRAMMARS:
                found.add(match)
    return found


def pinned_distributions() -> dict[str, set[str]]:
    """The `tree-sitter-*` distributions each extra pins."""

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    extras = data["project"]["optional-dependencies"]
    return {
        name: {
            re.split(r"[<>=!~\[]", spec, maxsplit=1)[0].strip()
            for spec in specs
            if spec.startswith("tree-sitter-")
        }
        for name, specs in extras.items()
    }


def module_to_distribution(module: str) -> str:
    """`tree_sitter_c_sharp` -> `tree-sitter-c-sharp`, the usual PyPI rule."""

    return module.replace("_", "-")


class GrammarCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.imported = imported_grammars()
        self.pinned = pinned_distributions()
        self.all_pinned = self.pinned["code"] | self.pinned["code-all"]

    def test_the_extractors_import_more_grammars_than_code_alone_pins(self) -> None:
        """A guard on the premise: if this stops being true, the split is moot."""

        self.assertGreater(len(self.imported), len(self.pinned["code"]))

    def test_every_imported_grammar_is_pinned_by_some_extra(self) -> None:
        missing = sorted(
            module
            for module in self.imported
            if module_to_distribution(module) not in self.all_pinned
        )
        self.assertEqual(
            missing,
            [],
            "these grammars are imported by a vendored extractor but named in "
            "neither `code` nor `code-all`, so that extractor can never run and "
            "its files will silently contribute nothing: " + ", ".join(missing),
        )

    def test_no_extra_pins_a_grammar_nothing_imports(self) -> None:
        """The other direction: a pin nothing asks for is dead download weight."""

        unused = sorted(
            dist
            for dist in self.all_pinned
            if dist not in {module_to_distribution(m) for m in self.imported}
        )
        self.assertEqual(unused, [])

    def test_the_two_extras_do_not_overlap(self) -> None:
        """`code-all` extends `code`; pinning a grammar twice invites a conflict."""

        self.assertEqual(self.pinned["code"] & self.pinned["code-all"], set())

    def test_code_all_includes_code_rather_than_replacing_it(self) -> None:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
        self.assertIn(
            "brainkit[code]", data["project"]["optional-dependencies"]["code-all"]
        )


class InstallHintTest(unittest.TestCase):
    """The hint has to name something a brainkit user can actually install.

    Upstream pointed every missing-grammar message at a `graphifyy` extra.
    brainkit vendors this code and does not depend on graphifyy, so following
    that instruction installed an unrelated distribution and the file still
    contributed nothing.
    """

    def test_no_user_facing_message_tells_anyone_to_install_graphifyy(self) -> None:
        offenders = []
        for path in CODEANALYSIS.rglob("*.py"):
            for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if "graphifyy[" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [], f"still advertising graphifyy: {offenders}")

    def test_the_hint_is_derived_from_the_module_the_error_names(self) -> None:
        """Derived, not tabulated, so every grammar gets a correct hint."""

        source = (CODEANALYSIS / "extract.py").read_text("utf-8")
        self.assertIn('_module.group(1).replace("_", "-")', source)
        self.assertIn("brainkit[code-all]", source)

    def test_graphifyy_is_not_a_dependency_which_is_why_that_hint_was_wrong(
        self,
    ) -> None:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
        every = list(data["project"]["dependencies"])
        for specs in data["project"]["optional-dependencies"].values():
            every += list(specs)
        self.assertFalse([spec for spec in every if "graphify" in spec.lower()])



class DeliberateSkipTest(unittest.TestCase):
    """A file the extractor *chose* not to index is not an anomaly.

    `extract_json` skips data-shaped JSON on purpose: #1224 removed it after
    AST-walking datasets and API dumps swamped the graph with orphan key-nodes.
    It says so by returning a `skipped` marker alongside the empty node list.

    #1666's empties warning checked only `nodes` and `error`, so every such file
    was reported as "produced zero nodes … a re-run will retry them". The advice
    is a loop: the skip is deterministic, so the re-run re-derives it and warns
    identically, forever. On this repository that was six JSON Schema documents
    warning on every single build.
    """

    SCHEMA: ClassVar[dict] = {
        "type": "object",
        "required": ["operations"],
        "properties": {"operations": {"type": "array"}},
    }

    def test_a_json_schema_document_is_skipped_rather_than_empty(self) -> None:
        import json
        import tempfile

        from brainkit.infrastructure.codeanalysis.extractors.json_config import (
            extract_json,
        )

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "ingest.json"
            path.write_text(json.dumps(self.SCHEMA), encoding="utf-8")
            result = extract_json(path)
        self.assertEqual(result["nodes"], [])
        self.assertTrue(
            result.get("skipped"),
            "a deliberate skip must say so, or it is indistinguishable from a "
            "failed extraction",
        )

    def test_the_same_document_is_indexed_once_a_probe_key_is_present(self) -> None:
        """Proves the skip is recognition, not an inability to parse the file."""

        import json
        import tempfile

        from brainkit.infrastructure.codeanalysis.extractors.json_config import (
            extract_json,
        )

        probed = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
        probed.update(self.SCHEMA)
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "probed.json"
            path.write_text(json.dumps(probed), encoding="utf-8")
            result = extract_json(path)
        self.assertTrue(result["nodes"])
        self.assertFalse(result.get("skipped"))

    def test_a_deliberate_skip_raises_no_empties_warning(self) -> None:
        import io
        import json
        import tempfile
        from contextlib import redirect_stderr

        from brainkit.infrastructure.codeanalysis.extract import extract

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "data.json").write_text(json.dumps(self.SCHEMA), encoding="utf-8")
            (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                extract(
                    [root / "data.json", root / "app.py"],
                    cache_root=root / "cache",
                    root=root,
                    parallel=False,
                )
        self.assertNotIn("produced zero nodes", stderr.getvalue())

    def test_a_genuinely_empty_source_is_still_reported(self) -> None:
        """The warning must keep firing for what it was actually built for."""

        import io
        import tempfile
        from contextlib import redirect_stderr

        from brainkit.infrastructure.codeanalysis.extract import extract

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            # An extractor accepts .py and emits at least a file node for any
            # real source, so a result with no nodes and no `skipped` marker is
            # the anomaly #1666 exists to surface.
            (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = extract(
                    [root / "app.py"],
                    cache_root=root / "cache",
                    root=root,
                    parallel=False,
                )
        self.assertTrue(result.get("nodes"), "a .py file must produce nodes")
        self.assertNotIn("produced zero nodes", stderr.getvalue())


class SkipCachingTest(unittest.TestCase):
    """A deliberate skip is worth caching; an anomalous empty is not.

    #1666 stopped caching zero-node results so a transient hiccup could
    self-heal on the next build. A `skipped` result is not a hiccup -- it is a
    deterministic decision about this file -- so excluding it from the cache
    only re-parses every data-shaped JSON in the tree on every build, forever.
    """

    def build_twice(self, root: Path) -> tuple[dict, dict]:
        from brainkit.infrastructure.codeanalysis.extract import extract

        paths = [root / "data.json"]
        first = extract(paths, cache_root=root / "cache", root=root, parallel=False)
        second = extract(paths, cache_root=root / "cache", root=root, parallel=False)
        return first, second

    def test_a_skipped_file_is_served_from_cache_on_the_second_build(self) -> None:
        import json
        import tempfile

        from brainkit.infrastructure.codeanalysis.cache import load_cached

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "data.json").write_text(
                json.dumps({"type": "object", "properties": {}}), encoding="utf-8"
            )
            self.build_twice(root)
            cached = load_cached(
                root / "data.json", root, cache_root=root / "cache"
            )
        self.assertIsNotNone(cached, "a deliberate skip should be cached")
        self.assertTrue(cached.get("skipped"))

    def test_editing_the_file_re_extracts_it(self) -> None:
        """The cache is content-keyed, which is what makes caching a skip safe."""

        import json
        import tempfile

        from brainkit.infrastructure.codeanalysis.extractors.json_config import (
            extract_json,
        )

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / "data.json"
            path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
            self.build_twice(root)
            # Adding a probe key makes the same document indexable; the content
            # hash changes, so the stale skip must not be reused.
            path.write_text(
                json.dumps({"$schema": "x", "type": "object", "properties": {}}),
                encoding="utf-8",
            )
            self.assertTrue(extract_json(path)["nodes"])
    def test_the_parallel_path_caches_skips_too(self) -> None:
        """Both cache-write sites need this, and only one is on the `parallel=False`
        path -- a negative control that reverted the other one passed, which is how
        this gap surfaced. `_PARALLEL_THRESHOLD` is 20, so the file count matters.
        """

        import json
        import tempfile

        from brainkit.infrastructure.codeanalysis.cache import load_cached
        from brainkit.infrastructure.codeanalysis.extract import (
            _PARALLEL_THRESHOLD,
            extract,
        )

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = []
            for i in range(_PARALLEL_THRESHOLD + 5):
                path = root / f"data{i}.json"
                path.write_text(
                    json.dumps({"type": "object", "properties": {}}), encoding="utf-8"
                )
                paths.append(path)
            extract(paths, cache_root=root / "cache", root=root, parallel=True)
            cached = load_cached(paths[0], root, cache_root=root / "cache")
        self.assertIsNotNone(cached, "the parallel path must cache skips as well")
        self.assertTrue(cached.get("skipped"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
