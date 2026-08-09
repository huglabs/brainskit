"""Where a vault may be created, and how much a build is allowed to scan.

Every test here descends from one incident. `bk init` was run in
`~/Projetos/tools` — a directory that holds three checkouts rather than being
one — and the resulting vault indexed **55,295 files** from the whole
`~/Projetos` tree into a **683 MB** `graph/code.json`, plus 1.6 GB of AST
cache. Nothing refused, nothing asked, and nothing on screen said what was
being scanned until it was done.

Three independent defects had to line up, so there are three independent
locks and each is tested on its own:

1. `FileVault.code_root()` fell back to `self.root.parent` when no `.git` was
   found anywhere above the vault. That is the origin: a vault one level below
   a directory of projects claimed that directory.
2. Nothing refused to *site* a vault in a directory of projects in the first
   place.
3. Nothing bounded the scan, so neither of the above had a backstop.

A fourth failure rides along because it shares a cause — nobody looked at a
scan before running it. A grammar that is not installed does not fail a build;
it removes a language from it. `bk code build` reported success on a four-file
repository with a two-node graph, and `bk code status` then called that graph
`fresh`.
"""

from __future__ import annotations

import json
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from test_projections import policy as _base_policy

from brainkit.application.codegraph import CodeGraph
from brainkit.domain.model import (
    DEFAULT_CODE_SCAN_LIMIT,
    GrammarNeed,
    ScanSurvey,
    ValidationError,
    VaultConfig,
)
from brainkit.infrastructure import extractor, pyenv
from brainkit.infrastructure.vault import (
    DERIVED_DIRECTORIES,
    GENERATED_DIRECTORIES,
    FileVault,
    gitignore_block,
    merge_gitignore,
    workspace_repos,
)


def policy(**overrides: object) -> dict:
    """`test_projections.policy` with per-test overrides.

    Local rather than a change to the shared fixture: every other test file
    depends on that dict's exact shape, and these tests need to vary two keys
    (`code_root`, `code_scan_limit`) that most of them must not carry.
    """

    return {**_base_policy(), **overrides}


def _git(directory: Path) -> Path:
    """A directory that looks like a repository, without needing git itself."""

    (directory / ".git").mkdir(parents=True, exist_ok=True)
    return directory


class CodeRootBoundsTest(unittest.TestCase):
    """`code_root()` must never name a directory larger than one project."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_a_vault_with_no_repository_above_it_indexes_only_itself(self) -> None:
        # The incident, reduced. `workspace/` holds projects and has no `.git`,
        # so the old fallback -- `self.root.parent` -- handed the extractor
        # every sibling. The vault itself is the only safe answer available.
        workspace = self.root / "workspace"
        _git(workspace / "repoA")
        _git(workspace / "repoB")
        vault = FileVault.initialize(workspace / "solo", policy())

        self.assertEqual(vault.code_root(), workspace / "solo")
        self.assertNotEqual(vault.code_root(), workspace)
        self.assertIn("no repository", vault.code_root_reason()[1])

    def test_a_vault_inside_a_repository_names_that_repository(self) -> None:
        repo = _git(self.root / "project")
        vault = FileVault.initialize(repo / ".brainkit", policy())

        self.assertEqual(vault.code_root(), repo)
        self.assertIn("git repository", vault.code_root_reason()[1])

    def test_the_upward_walk_never_returns_home_or_above(self) -> None:
        # A `.git` in the home directory is not a project boundary anyone
        # means, and returning it would scan everything the operator owns.
        home = self.root / "home"
        _git(home)
        vault_root = home / "notes"
        vault_root.mkdir(parents=True)
        vault = FileVault.initialize(vault_root, policy())

        with unittest.mock.patch.object(Path, "home", staticmethod(lambda: home)):
            resolved, reason = vault.code_root_reason()

        self.assertEqual(resolved, vault_root)
        self.assertIn("no repository", reason)

    def test_an_explicit_code_root_is_honoured(self) -> None:
        # `..` is the *normal* setting for the `<repo>/.brainkit` layout, so it
        # must not be mistaken for an escape attempt.
        repo = _git(self.root / "project")
        vault = FileVault.initialize(repo / ".brainkit", policy(code_root=".."))

        self.assertEqual(vault.code_root(), repo)
        self.assertIn("configured", vault.code_root_reason()[1])


class VaultSitingTest(unittest.TestCase):
    """`bk init` refuses the three locations that cannot be what anyone meant."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_a_directory_holding_projects_is_refused(self) -> None:
        workspace = self.root / "Projetos"
        _git(workspace / "brainkit")
        _git(workspace / "gitlab-mcp")

        with self.assertRaises(ValidationError) as caught:
            FileVault.initialize(workspace, policy())

        details = caught.exception.details
        self.assertEqual(details["repositories"], ["brainkit", "gitlab-mcp"])
        # The message has to name a way forward, not only a refusal.
        self.assertIn(".brainkit", details["hint"])

    def test_a_repository_of_its_own_is_not_a_workspace(self) -> None:
        # A monorepo whose sub-projects each carry a `.git` is one project.
        # Refusing it would break a legitimate layout.
        repo = _git(self.root / "monorepo")
        _git(repo / "packages" / "a")
        _git(repo / "packages" / "b")

        vault = FileVault.initialize(repo / ".brainkit", policy())
        self.assertEqual(vault.code_root(), repo)

    def test_force_overrides_the_workspace_refusal(self) -> None:
        workspace = self.root / "Projetos"
        _git(workspace / "one")
        _git(workspace / "two")

        vault = FileVault.initialize(workspace, policy(), force=True)
        # Overriding the siting must not also unbound the scan: the code root
        # still refuses to climb out of the vault.
        self.assertEqual(vault.code_root(), workspace)

    def test_the_home_directory_is_refused(self) -> None:
        home = self.root / "home"
        home.mkdir()
        with unittest.mock.patch.object(Path, "home", staticmethod(lambda: home)):
            with self.assertRaises(ValidationError) as caught:
                FileVault.initialize(home, policy())
        self.assertIn("home directory", caught.exception.details["reason"])

    def test_a_vault_inside_a_vault_is_refused(self) -> None:
        outer = FileVault.initialize(self.root / "outer", policy())
        with self.assertRaises(ValidationError) as caught:
            FileVault.initialize(outer.root / "wiki" / "inner", policy())
        self.assertEqual(
            caught.exception.details["enclosing_vault"], str(outer.root)
        )

    def test_workspace_repos_ignores_files_and_symlinks(self) -> None:
        directory = self.root / "mixed"
        _git(directory / "real")
        (directory / "loose.txt").parent.mkdir(parents=True, exist_ok=True)
        (directory / "loose.txt").write_text("x")
        self.assertEqual([p.name for p in workspace_repos(directory)], ["real"])


class VaultGitignoreTest(unittest.TestCase):
    """The generated set is ignored, and an existing file is never destroyed."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_every_fully_derived_directory_is_ignored(self) -> None:
        # `graph/`, `views/` and `output/` were generated from the beginning
        # and never ignored, so a vault inside a repository committed its own
        # derived output -- here a 683 MB `graph/code.json`.
        vault = FileVault.initialize(self.root / "vault", policy())
        ignored = (vault.root / ".gitignore").read_text()
        for name in DERIVED_DIRECTORIES:
            self.assertIn(f"{name}/", ignored, f"{name}/ is generated but tracked")

    def test_the_two_directory_lists_account_for_each_other(self) -> None:
        # A fourth generated directory must force a decision about whether it
        # is committable, rather than silently defaulting to "tracked".
        self.assertEqual(
            set(GENERATED_DIRECTORIES) - set(DERIVED_DIRECTORIES),
            {".brain"},
            "a generated directory is neither ignored nor deliberately kept",
        )

    def test_an_existing_gitignore_survives(self) -> None:
        target = self.root / "project"
        target.mkdir()
        (target / ".gitignore").write_text("# mine\n*.log\nnode_modules/\n")

        FileVault.initialize(target, policy())

        content = (target / ".gitignore").read_text()
        self.assertIn("# mine", content)
        self.assertIn("node_modules/", content)
        self.assertIn("graph/", content)

    def test_merging_twice_leaves_exactly_one_block(self) -> None:
        once = merge_gitignore("*.log\n", gitignore_block())
        twice = merge_gitignore(once, gitignore_block())
        self.assertEqual(twice.count(">>> brainkit >>>"), 1)
        self.assertEqual(once, twice)


class ScanCeilingTest(unittest.TestCase):
    """A scan larger than a project refuses rather than running."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def _graph(self, limit: int, files: int) -> CodeGraph:
        vault = FileVault.initialize(self.root / "vault", policy(code_scan_limit=limit))
        survey = ScanSurvey(root=str(vault.root), files=files)

        class _Extractor:
            def available(self) -> bool:
                return True

            def survey(self, root: Path, paths: list[Path] | None = None) -> ScanSurvey:
                return survey

            def extract(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("extraction must not start once refused")

        return CodeGraph(vault, _Extractor())

    def test_a_scan_over_the_limit_is_refused_before_extracting(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self._graph(limit=100, files=55_295).build()

        details = caught.exception.details
        self.assertEqual(details["files"], 55_295)
        self.assertEqual(details["code_scan_limit"], 100)
        # Naming the root and *why* it was chosen is the point: the operator
        # could not previously find out either.
        self.assertIn("code_root", details)
        self.assertIn("why_this_root", details)

    def test_a_scan_at_the_limit_proceeds(self) -> None:
        # Boundary, and the negative control for the test above: the ceiling
        # must not refuse ordinary work.
        with self.assertRaises(AssertionError):
            self._graph(limit=100, files=100).build()

    def test_the_default_limit_is_between_the_incident_and_real_repositories(
        self,
    ) -> None:
        self.assertLess(DEFAULT_CODE_SCAN_LIMIT, 55_295)
        self.assertGreater(DEFAULT_CODE_SCAN_LIMIT, 5_000)

    def test_a_nonsense_limit_is_rejected_at_load(self) -> None:
        for value in (0, -1, "many", True):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    VaultConfig.from_dict(policy(code_scan_limit=value))

    def test_the_limit_round_trips_only_when_it_differs(self) -> None:
        default = VaultConfig.from_dict(policy()).to_dict()
        self.assertNotIn("code_scan_limit", default)
        custom = VaultConfig.from_dict(policy(code_scan_limit=42)).to_dict()
        self.assertEqual(custom["code_scan_limit"], 42)


class InstallCommandTest(unittest.TestCase):
    """The install hint has to work in the environment that is actually running.

    Every previous hint said `pip install …`. A `uv tool` environment ships no
    `pip`, so following the instruction either failed or installed the package
    into an unrelated interpreter and produced the identical message on the
    retry.
    """

    def test_a_uv_tool_environment_is_recognised(self) -> None:
        with TemporaryDirectory() as name:
            prefix = Path(name)
            (prefix / "uv-receipt.toml").write_text("[tool]\n")
            with unittest.mock.patch.object(pyenv.sys, "prefix", str(prefix)):
                self.assertEqual(pyenv.describe_environment().kind, "uv-tool")

    def test_a_command_never_assumes_pip_when_uv_is_present(self) -> None:
        environment = pyenv.Environment(
            kind="uv-tool", executable="/tools/bk/bin/python3", prefix="/tools/bk"
        )
        with unittest.mock.patch.object(pyenv.shutil, "which", lambda _: "/bin/uv"):
            command = environment.install_command(["tree-sitter-sql"])
        self.assertEqual(command[:2], ["/bin/uv", "pip"])
        # The interpreter is named explicitly; that is the whole fix.
        self.assertIn("/tools/bk/bin/python3", command)
        self.assertNotIn("-m", command)

    def test_an_environment_that_can_install_nothing_says_so(self) -> None:
        environment = pyenv.Environment(
            kind="system", executable="/usr/bin/python3", prefix="/usr",
            installable=False,
        )
        hint = environment.install_hint(["tree-sitter-sql"])
        self.assertIn("neither uv nor pip", hint)

    def test_pipx_injects_into_the_package(self) -> None:
        environment = pyenv.Environment(
            kind="pipx", executable="/pipx/venvs/brainkit/bin/python", prefix="/pipx"
        )
        with unittest.mock.patch.object(pyenv.shutil, "which", lambda _: "/bin/pipx"):
            command = environment.install_command(["tree-sitter-sql"])
        self.assertEqual(command[:3], ["pipx", "inject", "brainkit"])


class CoverageReportingTest(unittest.TestCase):
    """A graph missing a language must not report itself complete."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.vault = FileVault.initialize(
            Path(self._temp.name).resolve() / "vault", policy()
        )

    def _store(self, coverage: dict[str, object] | None) -> CodeGraph:
        source = self.vault.code_root() / "app.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("x = 1\n")
        graph: dict[str, object] = {
            "version": 1,
            "built_at": "2026-08-08T00:00:00+00:00",
            "privacy": "local-only",
            "nodes": [],
            "edges": [],
            "files": {"app.py": self.vault.code_hash("app.py")},
        }
        if coverage is not None:
            graph["coverage"] = coverage
        self.vault.write_generated(
            "graph/code.json", json.dumps(graph, indent=2) + "\n"
        )
        return CodeGraph(self.vault)

    def test_a_complete_graph_is_fresh(self) -> None:
        state = self._store({"grammars_missing": [], "unreachable_files": 0}).staleness()
        self.assertEqual(state["state"], "fresh")
        self.assertNotIn("missing_grammars", state)

    def test_a_graph_built_without_a_grammar_is_partial_not_fresh(self) -> None:
        # The exact lie: three of four source files were never parsed, and
        # `bk code status` answered `fresh` because every file it *did* record
        # still matched.
        state = self._store(
            {
                "grammars_missing": [
                    {"distribution": "tree-sitter-sql"},
                    {"distribution": "tree-sitter-swift"},
                ],
                "unreachable_files": 3,
            }
        ).staleness()

        self.assertEqual(state["state"], "partial")
        self.assertFalse(state["stale"])
        self.assertEqual(
            state["missing_grammars"], ["tree-sitter-sql", "tree-sitter-swift"]
        )
        self.assertEqual(state["unreachable_files"], 3)

    def test_a_graph_predating_coverage_still_reports_fresh(self) -> None:
        # Backward compatibility: an older graph carries no `coverage` key and
        # must not be reported as partial on that account alone.
        self.assertEqual(self._store(None).staleness()["state"], "fresh")

    def test_a_scoped_build_does_not_overwrite_the_graphs_coverage(self) -> None:
        """Rebuilding one file must not relabel the whole graph as complete.

        `build(paths)` merges its nodes into the stored graph but surveyed only
        its scope, so writing that survey whole recorded `files: 1` and an
        empty `grammars_missing` over a graph of a thousand — and `staleness`
        then called `fresh` a graph still blind to a language. Measured on this
        repository before the fix: a one-file survey against a 112-file graph.
        """

        graph = self._store(
            {
                "root": "/repo",
                "files": 112,
                "grammars_missing": [{"distribution": "tree-sitter-sql", "files": 3}],
                "unreachable_files": 3,
            }
        )
        scoped = ScanSurvey(root="/repo", files=1, grammars=(), present=1)

        carried = graph._coverage(scoped, scoped=True)

        assert carried is not None
        self.assertEqual(carried["files"], 112)
        self.assertEqual(
            [entry["distribution"] for entry in carried["grammars_missing"]],
            ["tree-sitter-sql"],
        )
        self.assertEqual(carried["unreachable_files"], 3)

    def test_a_scoped_build_adds_a_grammar_its_own_scope_found_missing(self) -> None:
        graph = self._store(
            {
                "root": "/repo",
                "files": 112,
                "grammars_missing": [{"distribution": "tree-sitter-sql", "files": 3}],
                "unreachable_files": 3,
            }
        )
        scoped = ScanSurvey(
            root="/repo",
            files=1,
            grammars=(
                GrammarNeed(
                    module="tree_sitter_swift",
                    distribution="tree-sitter-swift",
                    installed=False,
                    files=2,
                ),
            ),
        )

        carried = graph._coverage(scoped, scoped=True)

        assert carried is not None
        self.assertEqual(
            [entry["distribution"] for entry in carried["grammars_missing"]],
            ["tree-sitter-sql", "tree-sitter-swift"],
        )
        self.assertEqual(carried["unreachable_files"], 5)

    def test_the_written_artefact_keeps_the_coverage_on_a_scoped_write(self) -> None:
        # Through `_write`, because that is where the bug lived: `_coverage`
        # can be perfectly correct and still never be consulted.
        graph = self._store(
            {
                "root": "/repo",
                "files": 112,
                "grammars_missing": [{"distribution": "tree-sitter-sql", "files": 3}],
                "unreachable_files": 3,
            }
        )
        scoped = ScanSurvey(root="/repo", files=1, grammars=(), present=1)

        graph._write(
            {"n": {"id": "n", "path": "app.py", "kind": "function"}},
            [],
            0,
            survey=scoped,
            scoped=True,
        )

        written = json.loads(
            (self.vault.root / "graph" / "code.json").read_text(encoding="utf-8")
        )
        self.assertEqual(written["coverage"]["files"], 112)
        self.assertEqual(written["coverage"]["unreachable_files"], 3)
        self.assertEqual(CodeGraph(self.vault).staleness()["state"], "partial")

    def test_a_whole_root_build_replaces_the_coverage_outright(self) -> None:
        # The other half of the rule: a full build measured everything, so its
        # answer supersedes whatever a previous partial one recorded.
        graph = self._store(
            {"grammars_missing": [{"distribution": "tree-sitter-sql", "files": 3}]}
        )
        full = ScanSurvey(root="/repo", files=112, grammars=(), present=112)

        fresh = graph._coverage(full, scoped=False)

        assert fresh is not None
        self.assertEqual(fresh["grammars_missing"], [])
        self.assertEqual(fresh["files"], 112)


class IgnoredScanRootTest(unittest.TestCase):
    """A root the extractor refuses to walk must say so, not report zero.

    The second harness failure. Ten pinned repositories cached under
    `benchmarks/.cache/` every one measured zero nodes, because the extractor
    prunes `.cache` as a noise directory — and the only message that reached
    anyone was "No code nodes in the imported graph", which reads as a defect
    in the extractor rather than a path it declined to enter.
    """

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def _graph(self, *, collected: int, present: int) -> CodeGraph:
        vault = FileVault.initialize(self.root / "vault", policy())
        survey = ScanSurvey(root=str(vault.root), files=collected, present=present)

        class _Extractor:
            def available(self) -> bool:
                return True

            def survey(self, root: Path, paths: list[Path] | None = None) -> ScanSurvey:
                return survey

            def extract(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("extraction must not start once refused")

        return CodeGraph(vault, _Extractor())

    def test_a_root_that_is_ignored_is_named_as_such(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self._graph(collected=0, present=5).build()

        details = caught.exception.details
        self.assertEqual(details["files_on_disk"], 5)
        self.assertEqual(details["files_collected"], 0)
        # The message has to name the mechanism, or the next person repeats the
        # afternoon this cost.
        self.assertIn(".cache", details["likely_cause"])

    def test_a_genuinely_empty_root_is_not_reported_as_ignored(self) -> None:
        # The negative case that keeps the check honest: nothing on disk is not
        # the same failure, and must fall through to the ordinary empty-graph
        # path rather than accusing the walker.
        with self.assertRaises(AssertionError):
            self._graph(collected=0, present=0).build()

    def test_a_partially_collected_root_is_not_reported_as_ignored(self) -> None:
        with self.assertRaises(AssertionError):
            self._graph(collected=3, present=5).build()


class UnexplainedCoverageTest(unittest.TestCase):
    """Files with an installed grammar that produced nothing, for no reason.

    Every other outcome names itself — a missing grammar is reported, a
    deliberate skip carries a marker, an unreadable file is listed. What was
    left was a silent fourth category, and both harness failures landed in it.
    """

    def test_a_shortfall_is_counted_and_proportioned(self) -> None:
        survey = ScanSurvey(
            root="/tmp/x",
            files=100,
            grammars=(
                GrammarNeed("tree_sitter_python", "tree-sitter-python", True, (".py",), 80),
                GrammarNeed("tree_sitter_sql", "tree-sitter-sql", False, (".sql",), 20),
            ),
        )
        # 80 files have an installed grammar; only 6 reached the graph.
        result = CodeGraph._unexplained(survey, covered=6)
        self.assertEqual(result["unexplained_files"], 74)
        self.assertEqual(result["unexplained_ratio"], 0.925)

    def test_a_missing_grammar_alone_is_not_unexplained(self) -> None:
        # The distinction that matters: 20 SQL files contributing nothing is
        # explained, and must not be reported as a mystery.
        survey = ScanSurvey(
            root="/tmp/x",
            files=100,
            grammars=(
                GrammarNeed("tree_sitter_python", "tree-sitter-python", True, (".py",), 80),
                GrammarNeed("tree_sitter_sql", "tree-sitter-sql", False, (".sql",), 20),
            ),
        )
        self.assertEqual(CodeGraph._unexplained(survey, covered=80), {})


class CommandDeadEndTest(unittest.TestCase):
    """Every command must have a way to be given what it needs.

    Twenty of the forty-six invocable commands cannot run without an argument,
    and each was a place an operator could arrive with no way forward: the
    browser ended at `--help` and exited, and a bare invocation printed usage.
    A third group — `capture`, `ingest` — is worse, because argparse does not
    even record the requirement: it lives in a runtime error string.
    """

    def setUp(self) -> None:
        from brainkit.interfaces import cli

        self.cli = cli
        self.parser = cli.build_parser()

    def test_every_required_argument_is_discoverable_from_the_parser(self) -> None:
        needed = self.cli.requirements(self.parser, ["search"])
        self.assertEqual([item.dest for item in needed], ["query"])

        needed = self.cli.requirements(self.parser, ["code", "path"])
        self.assertEqual([item.dest for item in needed], ["source", "target"])

        # A required *flag* counts too, or `bk export` stays a dead end.
        needed = self.cli.requirements(self.parser, ["export"])
        self.assertEqual([item.flag for item in needed], ["--target"])

    def test_a_command_with_no_requirements_reports_none(self) -> None:
        self.assertEqual(self.cli.requirements(self.parser, ["status"]), [])
        # A group is not a command; it has nothing to ask for.
        self.assertEqual(self.cli.requirements(self.parser, ["code"]), [])

    def test_choices_are_offered_rather_than_typed(self) -> None:
        needed = self.cli.requirements(self.parser, ["hooks", "install"])
        agent = next(item for item in needed if item.flag == "--agent")
        self.assertIn("claude", agent.choices)

    def test_commands_whose_requirement_argparse_cannot_see_are_declared(self) -> None:
        # The exact gap: argparse reports `capture` as needing nothing, because
        # `source` is optional and `--text` is an alternative to it.
        self.assertEqual(self.cli.requirements(self.parser, ["capture"]), [])
        self.assertIn("capture", self.cli._ALTERNATIVES)
        question, options = self.cli._ALTERNATIVES["capture"]
        self.assertTrue(question.endswith("?"))
        # One form takes a value, the other is a switch — a prompt that assumed
        # every choice needs typing would ask for a value `--all` cannot take.
        self.assertTrue(any(o.takes_value for o in options))
        _, ingest_options = self.cli._ALTERNATIVES["ingest"]
        self.assertTrue(any(not o.takes_value for o in ingest_options))

    def test_declared_alternatives_name_real_flags(self) -> None:
        for command, (_, options) in self.cli._ALTERNATIVES.items():
            rendered = self.cli._subcommand_help(self.parser, command)
            for option in options:
                if option.flag:
                    self.assertIn(
                        option.flag,
                        rendered,
                        f"{command} has no {option.flag}",
                    )


class PastedValueTest(unittest.TestCase):
    """A path arrives at the prompt carrying whatever quoting produced it.

    Reported from a real session: dragging a PDF into the terminal and pasting
    it at `bk capture`'s prompt produced `'/Users/…/file.pdf'` — quotes and
    all — which brainkit then treated as a literal filename and rejected with
    "Capture source is not a file", echoing a command line with the quotes
    still in it. The failure reads as brainkit being broken when it is one
    layer of shell quoting nobody removed.
    """

    def setUp(self) -> None:
        from brainkit.interfaces import cli

        self.cli = cli
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_a_quoted_paste_becomes_the_path_it_names(self) -> None:
        target = self.root / "Vant · relatório de negócio.pdf"
        target.write_text("x")
        self.assertEqual(self.cli._typed_value(f"'{target}'"), str(target))
        self.assertEqual(self.cli._typed_value(f'"{target}"'), str(target))

    def test_drag_and_drop_backslash_escaping_is_undone(self) -> None:
        target = self.root / "a file with spaces.pdf"
        target.write_text("x")
        escaped = str(target).replace(" ", r"\ ")
        self.assertEqual(self.cli._typed_value(escaped), str(target))

    def test_a_tilde_is_expanded(self) -> None:
        self.assertEqual(
            self.cli._typed_value("~/x.pdf"), str(Path.home() / "x.pdf")
        )

    def test_ordinary_prose_is_left_exactly_as_typed(self) -> None:
        # The rule has to be safe for `search`, `ask` and `--text`, where a
        # multi-word value is the point and stripping anything would corrupt it.
        for text in ("retrieval memory notes", "why did the build fail", "a, b, c"):
            self.assertEqual(self.cli._typed_value(text), text)

    def test_an_unbalanced_quote_is_not_guessed_at(self) -> None:
        # `shlex` raises here; returning the raw value is the only safe answer.
        self.assertEqual(self.cli._typed_value("it's fine"), "it's fine")

    def test_a_missing_file_is_refused_at_the_prompt(self) -> None:
        problem = self.cli._looks_like_source(str(self.root / "nope.pdf"))
        self.assertIsNotNone(problem)
        self.assertIn("nope.pdf", problem)

    def test_a_url_and_an_existing_file_are_both_accepted(self) -> None:
        target = self.root / "real.md"
        target.write_text("x")
        self.assertIsNone(self.cli._looks_like_source(str(target)))
        self.assertIsNone(self.cli._looks_like_source("https://example.com/a"))

    def test_the_capture_alternative_carries_that_validator(self) -> None:
        # Wiring, not logic: a validator defined and never attached is the
        # shape of bug this whole class exists to catch.
        _, options = self.cli._ALTERNATIVES["capture"]
        by_file = next(o for o in options if o.flag is None)
        self.assertIs(by_file.validate, self.cli._looks_like_source)


class ShimIsolationTest(unittest.TestCase):
    """The generated `graphify` package must live where only this user writes.

    Its path reaches `sys.path` and spawned workers import from it, so whoever
    can create the file decides what runs inside a build. The name is a hash of
    the shipped tree — identical on every install — and `gettempdir()` is
    world-writable `/tmp` on Linux, so a bare shared path could be planted in
    advance and would then be trusted on sight.
    """

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_a_fresh_directory_is_created_private(self) -> None:
        target = extractor._private_directory(self.root / "new")
        assert target is not None
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_a_world_writable_directory_is_refused(self) -> None:
        planted = self.root / "planted"
        planted.mkdir(mode=0o777)
        planted.chmod(0o777)
        self.assertIsNone(extractor._private_directory(planted))

    def test_a_symlink_is_not_mistaken_for_the_directory(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(elsewhere)
        self.assertIsNone(extractor._private_directory(link))

    def test_the_shim_path_is_namespaced_per_user(self) -> None:
        # Two users on one machine must not resolve to the same directory, or
        # the ownership check is the only thing standing between them.
        root = extractor._shim_root()
        assert root is not None
        self.assertIn(f"brainkit-graphify-{extractor._shim_owner()}", str(root))
