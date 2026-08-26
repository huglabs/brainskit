"""Regression tests for the node-generation defects the stress test found.

Every case here descends from a reproduced failure, not a hypothesis:

- **B-1**: a file and a symlink to it raced through the extraction pool and
  the *symlink* sometimes took the credit -- the real file contributed
  nothing while the graph pointed at an alias. The collapse is now done in
  the adapter, deterministically, before any worker sees a path.
- **B-1's ghost**: that misattributed payload survived its own fix, because
  the AST cache salts by the *resolved* path -- alias and target share one
  key -- so the old entry was served back for the canonical file. The cache
  marker now hashes the adapter too (asserted indirectly: the marker must
  change when the adapter changes).
- **B-2**: `bk code build <scope>` re-hashed every node path from disk,
  blessing edits it never extracted; `status` answered `fresh` over nodes
  describing code that no longer exists.
- **B-3**: `unexplained_files` reached only the command output, so the gap
  expired when the build output scrolled away. It lives in the artefact now,
  and `staleness` answers `partial` while it lasts.
- **B-4**: `_resolve` matched function labels exactly, and they are minted
  as `name()` -- so every bare function name failed with "No such symbol".
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_projections import policy as _base_policy

from brainskit.application.codegraph import CODE_PROJECTION, CodeGraph
from brainskit.application.doctor import (
    _grammar_requirements,
    _satisfies,
    grammar_update_check,
)
from brainskit.domain.model import (
    GrammarNeed,
    NotFoundError,
    ScanSurvey,
    ValidationError,
)
from brainskit.infrastructure.extractor import (
    _cache_format_marker,
    _canonical_files,
)
from brainskit.infrastructure.vault import FileVault


def policy(**overrides: object) -> dict:
    return {**_base_policy(), **overrides}


def _git(directory: Path) -> Path:
    (directory / ".git").mkdir(parents=True, exist_ok=True)
    return directory


class FakeExtractor:
    """Returns canned payloads for the files it is asked about."""

    def __init__(self, payloads: dict[str, dict] | None = None):
        self.payloads = payloads or {}

    def available(self) -> bool:
        return True

    def extract(self, root, paths=None, *, cache_root=None):
        targets = [Path(p).name for p in (paths or [])] or None
        nodes: list[dict] = []
        links: list[dict] = []
        for name, payload in self.payloads.items():
            if targets is not None and name not in targets:
                continue
            nodes.extend(payload.get("nodes", []))
            links.extend(payload.get("links", []))
        return {"nodes": nodes, "edges": [], "links": links}


def _node(node_id: str, label: str, path: str) -> dict:
    return {
        "id": node_id,
        "label": label,
        "file_type": "code",
        "source_file": path,
        "source_location": "L1",
    }


class CanonicalFilesTest(unittest.TestCase):
    """One entry per real file: symlinks collapse onto their target."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def test_a_real_file_beats_a_symlink_to_it_regardless_of_order(self) -> None:
        target = self.root / "real.py"
        target.write_text("x = 1\n")
        alias = self.root / "alias.py"
        os.symlink(target, alias)

        for collected in ([alias, target], [target, alias]):
            files = _canonical_files(collected)
            self.assertEqual([p.name for p in files], ["real.py"])

    def test_two_symlinks_to_one_file_yield_one_deterministic_entry(self) -> None:
        target = self.root / "real.py"
        target.write_text("x = 1\n")
        a = self.root / "a.py"
        b = self.root / "b.py"
        os.symlink(target, a)
        os.symlink(target, b)

        # No real file present at all: the lexicographic winner is stable, so
        # repeated builds attribute identically no matter the walk order.
        self.assertEqual([p.name for p in _canonical_files([b, a])], ["a.py"])

    def test_identical_content_at_distinct_paths_keeps_both_entries(self) -> None:
        # Same bytes are still two sources; only the same *file* collapses.
        (self.root / "one.py").write_text("x = 1\n")
        (self.root / "two.py").write_text("x = 1\n")

        files = _canonical_files([self.root / "two.py", self.root / "one.py"])
        self.assertEqual(sorted(p.name for p in files), ["one.py", "two.py"])

    def test_the_cache_marker_covers_the_adapter_itself(self) -> None:
        # B-1's ghost: the cache salts entries by resolved path, so a payload
        # extracted under an alias was served back for the canonical file once
        # extraction stopped visiting the alias. The marker must therefore
        # change when the adapter changes -- proved here by hashing a modified
        # copy of this file into a different marker.
        import unittest.mock

        from brainskit.infrastructure import extractor as adapter

        original = _cache_format_marker()
        self.assertTrue(original)
        self.assertEqual(original, _cache_format_marker())  # stable in-process

        altered = Path(self._temp.name) / "extractor.py"
        altered.write_text(Path(adapter.__file__).read_text() + "\n# drift\n")
        with unittest.mock.patch.object(adapter, "__file__", str(altered)):
            _cache_format_marker.cache_clear()
            drifted = _cache_format_marker()
        _cache_format_marker.cache_clear()
        self.assertNotEqual(original, drifted)


class ScopedFreshnessTest(unittest.TestCase):
    """A scoped build must not bless edits outside its scope."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        repo = _git(Path(self._temp.name) / "repo")
        self.vault = FileVault.initialize(repo / ".brainskit", policy())
        self.keep = self.vault.code_root() / "keep"
        self.drop = self.vault.code_root() / "drop"
        self.keep.mkdir()
        self.drop.mkdir()
        (self.keep / "x.py").write_text("def kx():\n    pass\n")
        (self.drop / "y.py").write_text("def dy():\n    pass\n")
        self.pre_edit_digest = hashlib.sha256(b"def dy():\n    pass\n").hexdigest()
        self.graph = CodeGraph(
            self.vault,
            FakeExtractor(
                payloads={
                    "x.py": {"nodes": [_node("kx", "kx()", "keep/x.py")]},
                    "y.py": {"nodes": [_node("dy", "dy()", "drop/y.py")]},
                }
            ),
        )
        self.graph.build()

    def _stored(self) -> dict:
        return json.loads((self.vault.root / CODE_PROJECTION).read_text())

    def test_an_edit_outside_the_scope_stays_stale_after_a_scoped_build(self) -> None:
        # The incident: after `bk code build keep`, status said `fresh` over
        # nodes still describing the old dy(), because the scoped write had
        # re-hashed drop/y.py from disk without ever extracting it.
        (self.drop / "y.py").write_text("def dy_new():\n    pass\n")
        self.graph.build([self.keep])

        verdict = CodeGraph(self.vault).staleness()
        self.assertEqual(verdict["state"], "stale")
        self.assertIn("drop/y.py", verdict["changed"])

        labels = {node["label"] for node in self._stored()["nodes"]}
        # The out-of-scope edit is reported, not absorbed: the old nodes stay
        # until something actually reads the file again.
        self.assertIn("dy()", labels)
        self.assertNotIn("dy_new()", labels)
        # And the artefact records what the graph last *saw*, not what disk
        # happens to hold now.
        self.assertEqual(self._stored()["files"]["drop/y.py"], self.pre_edit_digest)

    def test_a_full_rebuild_after_the_scoped_one_clears_the_staleness(self) -> None:
        (self.drop / "y.py").write_text("def dy_new():\n    pass\n")
        self.graph.build([self.keep])
        # The fake now "sees" the edited bytes too -- it serves whatever its
        # payloads hold, so update them to match the file it claims to read.
        self.graph.extractor.payloads["y.py"]["nodes"][0] = _node(
            "dy_new", "dy_new()", "drop/y.py"
        )
        self.graph.build()

        verdict = CodeGraph(self.vault).staleness()
        self.assertEqual(verdict["state"], "fresh")
        labels = {node["label"] for node in self._stored()["nodes"]}
        self.assertIn("dy_new()", labels)
        self.assertNotIn("dy()", labels)


class UnexplainedCoverageTest(unittest.TestCase):
    """The unexplained gap is persisted, and `partial` while it lasts."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        repo = _git(Path(self._temp.name) / "repo")
        self.vault = FileVault.initialize(repo / ".brainskit", policy())
        source = self.vault.code_root() / "app.py"
        source.write_text("def app():\n    pass\n")

    def _survey(self, *, parseable: int) -> ScanSurvey:
        return ScanSurvey(
            root=str(self.vault.code_root()),
            files=parseable,
            grammars=(
                GrammarNeed(
                    module="tree_sitter_python",
                    distribution="tree-sitter-python",
                    installed=True,
                    extensions=(".py",),
                    files=parseable,
                ),
            ),
        )

    def _store_coverage(self, coverage: dict) -> dict:
        stored = {
            "version": 1,
            "built_at": "2026-08-24T00:00:00+00:00",
            "privacy": "local-only",
            "code_root": self.vault.code_root().resolve().as_posix(),
            "nodes": [
                {
                    "id": "app",
                    "label": "app.py",
                    "path": "app.py",
                    "line": 1,
                }
            ],
            "edges": [],
            "files": {"app.py": self.vault.code_hash("app.py")},
            "coverage": coverage,
        }
        self.vault.write_generated(
            CODE_PROJECTION, json.dumps(stored, indent=2) + "\n"
        )
        return stored

    def test_a_measured_gap_is_written_into_the_artefact_and_reported(self) -> None:
        # Two parseable files surveyed, one file's worth of nodes written:
        # the exact shape of the symlink race before it was fixed.
        survey = self._survey(parseable=2)
        coverage = CodeGraph(self.vault)._coverage(survey, scoped=False, covered=1)
        self._store_coverage(coverage)

        self.assertEqual(coverage["unexplained_files"], 1)
        verdict = CodeGraph(self.vault).staleness()
        self.assertEqual(verdict["state"], "partial")
        self.assertFalse(verdict["stale"])
        self.assertEqual(verdict["unexplained_files"], 1)

    def test_a_whole_root_build_records_zero_when_nothing_was_lost(self) -> None:
        # An explicit zero, not an absent key: "measured complete" must be
        # distinguishable from "never measured".
        coverage = CodeGraph(self.vault)._coverage(
            self._survey(parseable=1), scoped=False, covered=1
        )
        self._store_coverage(coverage)

        self.assertEqual(coverage["unexplained_files"], 0)
        self.assertEqual(CodeGraph(self.vault).staleness()["state"], "fresh")

    def test_an_artefact_from_before_the_field_existed_stays_fresh(self) -> None:
        # Backward compatibility: a graph written before `unexplained_files`
        # existed carries no claim either way, and must not read as partial.
        self._store_coverage({"root": str(self.vault.code_root()), "files": 1})
        self.assertEqual(CodeGraph(self.vault).staleness()["state"], "fresh")


class SymbolResolveTest(unittest.TestCase):
    """`_resolve` tolerates how people spell symbol names."""

    def setUp(self) -> None:
        self.two_helpers = {
            "nodes": [
                {"id": "app_app", "label": "App", "path": "app.py", "line": 3},
                {"id": "lib_helper", "label": "helper()", "path": "lib.py", "line": 1},
                {
                    "id": "other_helper",
                    "label": "helper()",
                    "path": "other.py",
                    "line": 1,
                },
            ],
            "edges": [],
        }
        self.one_helper = {
            "nodes": [n for n in self.two_helpers["nodes"] if n["id"] == "lib_helper"],
            "edges": [],
        }

    def resolve(self, graph: dict, symbol: str) -> str:
        return CodeGraph._resolve(None, graph, symbol)

    def test_a_bare_function_name_resolves_despite_the_paren_suffix(self) -> None:
        # B-4 verbatim: `bk code path App helper` answered "No such symbol".
        self.assertEqual(self.resolve(self.one_helper, "helper"), "lib_helper")

    def test_the_paren_spelling_still_works(self) -> None:
        self.assertEqual(self.resolve(self.one_helper, "helper()"), "lib_helper")

    def test_matching_is_case_insensitive_after_normalisation(self) -> None:
        self.assertEqual(self.resolve(self.one_helper, "HELPER"), "lib_helper")

    def test_a_qualified_name_disambiguates_same_named_symbols(self) -> None:
        self.assertEqual(self.resolve(self.two_helpers, "lib.helper"), "lib_helper")
        self.assertEqual(
            self.resolve(self.two_helpers, "other.helper"), "other_helper"
        )

    def test_an_unqualified_ambiguous_name_is_refused_with_candidates(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.resolve(self.two_helpers, "helper")
        self.assertIn("candidates", caught.exception.details)

    def test_an_unknown_name_still_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.resolve(self.two_helpers, "missing")


class GrammarUpdateCheckTest(unittest.TestCase):
    """`bk doctor` flags grammars that are missing *or* older than the pins."""

    class _Env:
        kind = "uv-tool"
        label = "uv tool"
        executable = "/tools/bk/bin/python"
        installable = True

        def install_hint(self, packages):
            return f"uv pip install {' '.join(packages)}"

    def test_the_pins_come_from_brainskits_own_metadata(self) -> None:
        pins = _grammar_requirements()
        self.assertTrue(pins, "no grammar pins found in dist metadata")
        self.assertIn("tree-sitter-python", pins)

    def test_a_version_outside_the_pinned_range_counts_as_outdated(self) -> None:
        report = grammar_update_check(
            {"tree-sitter-python": {"installed": True, "version": "0.22.0"}},
            environment=self._Env(),
        )
        outdated = report["grammars_outdated"]
        self.assertEqual(outdated[0]["distribution"], "tree-sitter-python")
        self.assertEqual(outdated[0]["installed"], "0.22.0")
        self.assertEqual(outdated[0]["required"], _grammar_requirements()["tree-sitter-python"])
        self.assertIn("upgrade", report)

    def test_a_version_inside_the_range_is_not_flagged(self) -> None:
        report = grammar_update_check(
            {"tree-sitter-python": {"installed": True, "version": "0.24.4"}},
            environment=self._Env(),
        )
        self.assertEqual(report, {})

    def test_missing_and_outdated_share_one_upgrade_command(self) -> None:
        report = grammar_update_check(
            {
                "tree-sitter-python": {"installed": False, "version": None},
                "tree-sitter-swift": {"installed": True, "version": "0.1.0"},
            },
            environment=self._Env(),
        )
        command = report["upgrade"]
        self.assertIn("tree-sitter-python", command)
        self.assertIn("tree-sitter-swift", command)

    def test_no_metadata_degrades_to_no_update_check(self) -> None:
        # No dist metadata (an exotic install): no update verdict at all --
        # degrading to absent, never to a false "outdated".
        with unittest.mock.patch(
            "brainskit.application.doctor._grammar_requirements",
            return_value={},
        ):
            report = grammar_update_check(
                {"tree-sitter-python": {"installed": True, "version": "0.1.0"}},
                environment=self._Env(),
            )
        self.assertEqual(report, {})

    def test_the_version_comparator_handles_every_declared_shape(self) -> None:
        cases = [
            ("0.23.0", ">=0.23,<0.26", True),
            ("0.25.9", ">=0.23,<0.26", True),
            ("0.26.0", ">=0.23,<0.26", False),
            ("0.22.9", ">=0.23,<0.26", False),
            ("0.23", ">=0.23,<0.26", True),   # padded comparison
            ("1.4.1", "~=1.2", True),
            ("2.0", "~=1.2", False),
            ("0.24", "==0.*", True),          # wildcard: unverifiable, passes
            ("0.7.2", ">=0.7,<1", True),
            ("0.6.9", ">=0.7,<1", False),
        ]
        for version, specifiers, expected in cases:
            with self.subTest(version=version, specifiers=specifiers):
                self.assertEqual(_satisfies(version, specifiers), expected)


if __name__ == "__main__":
    unittest.main()
