"""The two structural invariants AGENTS.md states but nothing checked.

Both are properties of the import graph, so neither Ruff nor mypy can see them
and no behavioural test can either: a cycle and a wrong-direction import both
compile, typecheck and pass every existing test. They were documented as
intentions -- "preserve the DDD dependency direction" and, after the service
split, "a cycle check on the import graph is worth keeping ... it is the
property that makes the split real rather than cosmetic" -- and an intention
nothing enforces is one refactor away from being false.

Reading the graph with `ast` rather than by importing keeps this free of import
side effects and lets it run without the optional extras installed.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import ClassVar

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "brainskit"

#: Byte-identical vendored third-party source. It is excluded from Ruff and
#: mypy for the same reason it is excluded here: it was never written against
#: this repository's contracts, and a re-vendor must stay a copy.
VENDORED = "codeanalysis"

#: What each layer may import from. `interfaces` composes the application with
#: concrete adapters, so it alone sees infrastructure.
ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "application": frozenset({"application", "domain"}),
    # Narrowed from the whole of `application`. The port pattern is the
    # architecture -- an adapter implements an interface the application
    # defines -- so `application.ports` is allowed structurally below. Anything
    # else an adapter reaches for is a concrete service, and has to be argued
    # for in DOCUMENTED_EXCEPTIONS rather than waved through by a rule broad
    # enough to hide it.
    "infrastructure": frozenset({"infrastructure", "domain"}),
    "interfaces": frozenset({"interfaces", "application", "infrastructure", "domain"}),
}

#: The one reach across the layering that is deliberate, reasoned at its call
#: site, and load-bearing: `application/codegraph.py` imports the vendored
#: `normalize_id` rather than copying it, because node identity has to agree
#: with what the extractor minted and the vendoring rule forbids a hand-kept
#: duplicate that would drift on the next re-vendor. It is a pure function and
#: answers to no port.
#:
#: Listing it here is the point. The rule is enforced for everything else, so a
#: *second* such import has to be argued for in this file rather than appearing
#: unnoticed -- which is exactly what happened to the first one.
DOCUMENTED_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("application.codegraph", "infrastructure.codeanalysis"),
        # An adapter reaching *up* for a concrete application function rather
        # than a port. It was invisible while `ALLOWED` let infrastructure
        # import all of `application`; naming it is what turns a silent
        # allowance into a decision someone has to defend.
        #
        # `parse_frontmatter` is a pure parser over bytes the adapter already
        # holds, with no port to answer to -- the same argument as
        # `normalize_id` above.
        ("infrastructure.graph", "application.pages"),
    }
)

#: The one application module every adapter may import: the port pattern is the
#: architecture, not an exception to it.
STRUCTURAL_PORTS: frozenset[str] = frozenset({"application.ports"})


def _module_name(path: Path) -> str:
    parts = path.relative_to(SOURCE_ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path, module: str) -> set[str]:
    """Every first-party module `path` imports, absolute and relative alike."""

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # `from .sibling import x`
                package = module.rsplit(".", node.level)[0] if "." in module else ""
                target = f"{package}.{node.module}" if node.module else package
                found.add(target.strip("."))
            elif node.module and node.module.startswith("brainskit."):
                found.add(node.module[len("brainskit.") :])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("brainskit."):
                    found.add(alias.name[len("brainskit.") :])
    # `importlib.import_module("brainskit.…")` is an import too. The codegraph
    # module uses one deliberately, and a static reader that ignored it would
    # report a layering violation that the exception list already covers as
    # clean -- an enforcement gap disguised as a pass.
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Any `brainskit.` module string, not only the one dynamic import
            # that was already known. Filtering on `"codeanalysis" in ...`
            # identified the hole and then closed exactly the instance already
            # covered by the exception list -- so a *second* dynamic import,
            # anywhere else, would have passed unseen. That is the shape of an
            # enforcement gap disguised as a pass, which is the thing this file
            # exists to prevent.
            if node.value.startswith("brainskit."):
                found.add(node.value[len("brainskit.") :])
    return {name for name in found if name}


def _graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if VENDORED in path.parts:
            continue
        module = _module_name(path)
        graph[module] = _imports(path, module)
    return graph


class LayeringTests(unittest.TestCase):
    def test_the_source_tree_is_actually_scanned(self) -> None:
        """A guard that never sees a file passes for the wrong reason."""

        graph = _graph()
        self.assertGreater(len(graph), 20, "module graph is implausibly small")
        self.assertIn("application.services", graph)

    def test_imports_respect_the_ddd_direction(self) -> None:
        violations = []
        for module, targets in _graph().items():
            source_layer = module.split(".")[0]
            permitted = ALLOWED.get(source_layer)
            if permitted is None:
                continue
            for target in targets:
                target_layer = target.split(".")[0]
                if target_layer in permitted or target_layer not in ALLOWED:
                    continue
                trimmed = ".".join(target.split(".")[:2])
                if trimmed in STRUCTURAL_PORTS:
                    continue
                if (module, trimmed) in DOCUMENTED_EXCEPTIONS:
                    continue
                violations.append(f"{module} -> {target}")
        self.assertEqual(
            [],
            sorted(violations),
            "an import crosses the layering; add a reasoned entry to "
            "DOCUMENTED_EXCEPTIONS or route it through a port",
        )

    def test_the_module_graph_is_acyclic(self) -> None:
        """The property that makes the service split real rather than cosmetic."""

        graph = _graph()
        state: dict[str, int] = {}
        cycles: list[str] = []

        def walk(node: str, stack: list[str]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                cycles.append(" -> ".join([*stack[stack.index(node) :], node]))
                return
            state[node] = 1
            for target in sorted(graph.get(node, ())):
                if target in graph:
                    walk(target, [*stack, node])
            state[node] = 2

        for module in sorted(graph):
            walk(module, [])
        self.assertEqual([], cycles, "import cycle(s) in brainskit")


if __name__ == "__main__":
    unittest.main()


class ConstantsHaveOneOwnerTest(unittest.TestCase):
    """A shared constant must be imported, never copied across the boundary.

    The layering rule -- application must not import interfaces -- is right, and
    the response to it was to copy the managed-block sentinel and the gate's
    deny prefixes into both sides. Writer (`bk hooks install`) and readers
    (`bk status`, `bk doctor`) could then disagree in silence about what
    "installed" means, and per this repository's history that divergence has
    already shipped twice.
    """

    LITERALS: ClassVar[dict[str, str]] = {
        "<!-- brainskit:start -->": "INSTRUCTION_START",
        "<!-- brainskit:end -->": "INSTRUCTION_END",
        "# brainskit:generated": "HOOK_SENTINEL",
    }

    def sources(self) -> dict[Path, str]:
        root = Path(__file__).resolve().parents[1] / "src" / "brainskit"
        return {
            path: path.read_text(encoding="utf-8")
            for path in root.rglob("*.py")
            if "codeanalysis" not in path.parts
        }

    def test_each_sentinel_is_written_once(self) -> None:
        for literal, name in self.LITERALS.items():
            with self.subTest(constant=name):
                owners = [
                    path.name
                    for path, text in self.sources().items()
                    if f'"{literal}"' in text
                ]
                self.assertEqual(
                    owners,
                    ["gate.py"],
                    msg=f"{name} is spelled out in {owners}; import it instead",
                )

    def test_the_deny_prefixes_are_defined_once(self) -> None:
        owners = [
            path.name
            for path, text in self.sources().items()
            if '("wiki/", "raw/")' in text
        ]
        self.assertEqual(owners, ["gate.py"], msg=f"copied into {owners}")


class EnforcementLayersHaveOneOwnerTest(unittest.TestCase):
    """The install table must be imported too, for exactly the same reason.

    `ConstantsHaveOneOwnerTest` above guards the strings a copy of which had
    already shipped twice. It did not cover the rest of what writer and reader
    have to agree on -- what each enforcement layer is called, what mechanism it
    names, which instruction file an agent reads, which state file records where
    its configuration went, and what this brand's hook scripts are called -- and
    that was the third instance: `bk hooks install` knew all four agents while
    `Health` knew `claude`, in six hardcoded strings. An install for any other
    agent was reported against a workspace nobody chose, an instruction file
    nobody wrote and two hooks brainskit does not install for it.

    So the same assertion, over the same boundary, one class of constant wider.
    """

    #: Spelled with their quotes, so prose naming a file is not read as a copy
    #: of it. `"instructions"` is here as the layer name *and* as the key the
    #: installer's result carries for it -- they are the same layer, and using
    #: the constant for both is what keeps this assertion whole.
    LITERALS: ClassVar[dict[str, str]] = {
        "write_gate": "WRITE_GATE",
        "session_status": "SESSION_STATUS",
        "commit_lint": "COMMIT_LINT",
        "instructions": "INSTRUCTIONS",
        ".git/hooks/pre-commit running bk lint --changed": "COMMIT_LINT_MECHANISM",
        "Claude Code PreToolUse hook on Write|Edit|MultiEdit": "the gate hook's mechanism",
        "Claude Code SessionStart hook reporting vault state": "the status hook's mechanism",
        "CLAUDE.md": "AGENTS['claude'].instructions",
        "AGENTS.md": "AGENTS['codex'].instructions",
        "GEMINI.md": "AGENTS['gemini'].instructions",
    }

    #: Written inside f-strings, so there is no opening quote to match on. The
    #: mechanism suffix keeps its *closing* quote, because "managed block" is
    #: also how three docstrings describe the thing in prose and prose is not a
    #: copy -- only a string literal ending in it is.
    FRAGMENTS: ClassVar[dict[str, str]] = {
        "agent-{agent}.json": "adapter_path",
        ' managed block"': "AgentInstall.instructions_mechanism",
    }

    def sources(self) -> dict[Path, str]:
        return {
            path: path.read_text(encoding="utf-8")
            for path in SOURCE_ROOT.rglob("*.py")
            if VENDORED not in path.parts
        }

    def test_each_layer_string_is_written_once(self) -> None:
        for literal, name in self.LITERALS.items():
            with self.subTest(constant=name):
                owners = [
                    path.name
                    for path, text in self.sources().items()
                    if f'"{literal}"' in text
                ]
                self.assertEqual(
                    owners,
                    ["install.py"],
                    msg=f"{name} is spelled out in {owners}; import it instead",
                )

    def test_each_derived_fragment_is_written_once(self) -> None:
        for fragment, name in self.FRAGMENTS.items():
            with self.subTest(constant=name):
                owners = [
                    path.name
                    for path, text in self.sources().items()
                    if fragment in text
                ]
                self.assertEqual(
                    owners,
                    ["install.py"],
                    msg=f"{name} is spelled out in {owners}; import it instead",
                )

    def test_no_module_spells_a_brand_derived_artefact_name(self) -> None:
        """`brainskit-gate.sh` is derived from `BRAND`, so writing it is a copy.

        This is the shape the reader was in: two script names typed out beside
        an installer that builds them from the brand, which is why a rename
        would have moved one and left the other. Nothing may spell either --
        including the module that owns them, which builds them from `BRAND`.
        """

        from brainskit.application.install import AGENTS

        names = {
            spelling
            for shape in AGENTS.values()
            for hook in shape.hooks
            for spelling in (hook.template, hook.script)
        }
        self.assertTrue(names, "fixture guard: the registry declares no hooks")
        offenders = [
            f"{path.name}: {spelling}"
            for path, text in self.sources().items()
            for spelling in sorted(names)
            if f'"{spelling}"' in text
        ]
        self.assertEqual(offenders, [], msg="derive it from BRAND instead")


class DomainHasNoThirdPartyImportsTest(unittest.TestCase):
    """The domain layer must depend on the standard library and nothing else.

    `jsonschema` was its only third-party dependency, imported for one
    validation engine whose every caller already lived in `application/`. A
    domain layer that reaches for a vendor library to answer a question its own
    callers ask is a boundary that exists on paper only.

    This is the assertion that keeps the move from being quietly undone.
    """

    def test_no_domain_module_imports_a_third_party_package(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "brainskit"
        stdlib = set(sys.stdlib_module_names)
        offenders: list[str] = []
        for path in (root / "domain").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] if node.level == 0 else []
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    if not top or top in stdlib or top == "brainskit":
                        continue
                    offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], msg="domain reaches outside the stdlib")
