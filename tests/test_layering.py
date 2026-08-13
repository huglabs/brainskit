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
    "infrastructure": frozenset({"infrastructure", "application", "domain"}),
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
    {("application.codegraph", "infrastructure.codeanalysis")}
)


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
            if node.value.startswith("brainskit.") and "codeanalysis" in node.value:
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
