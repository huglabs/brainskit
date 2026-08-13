"""The vendoring contract for `infrastructure/codeanalysis/`.

That directory is Graphify's source, copied in. Its `NOTICE` used to claim
"nothing inside this directory" was edited — and by the time anyone checked,
three files had been. The claim mattered for two separate reasons:

- **Legally**, Apache-2.0 section 4(b) requires modified files to carry
  prominent notice of the change.
- **Operationally**, the extractor's AST cache is namespaced by a hash of this
  tree. When the hash covered only `NOTICE`, an edit to `extract.py` — one that
  changed *which results get cached* — left the namespace unchanged, so entries
  written by the previous extractor stayed live under the new one. A cache that
  cannot tell those apart is the "stale cache, wrong graph" failure the
  namespace exists to prevent.

So this file enforces what the notice states, rather than trusting it:

1. the tree contains exactly the files pinned in `VENDORED_SOURCE`, with exactly
   those contents — a new file, a deletion, or a silent edit to any of the 43
   fails here;
2. `NOTICE` files every departure under the right heading: MODIFIED for files
   that are present and changed, REMOVED for files that are gone, with no
   overlap in either direction;
3. `NOTICE` makes no blanket claim that the tree is unedited;
4. the cache marker changes when the vendored source changes;
5. the attribution files ship inside the distribution, not only in the
   repository; and
6. no user-facing string in that tree still points at `graphifyy`, which
   brainskit does not depend on.

Point 1 is what the previous version of this file was missing. It pinned only
the *names* of the three declared edits, so an undeclared new file, and an edit
to any of the other forty — `security.py` among them, which is the tree's
path-traversal and graph-size guard, and which `ruff` and `mypy` are both
configured to skip — passed unnoticed. Point 3 was worse than missing: it
searched for a sentence that appears nowhere in the file, inside a section that
excluded the region where the live false claim actually sat.

Regenerating the pin after a legitimate re-vendor::

    python3 -c '
    import hashlib, pathlib
    root = pathlib.Path("src/brainskit/infrastructure/codeanalysis")
    for p in sorted(root.rglob("*.py")):
        d = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        print(f"    \"{p.relative_to(root).as_posix()}\": \"{d}\",")
    '

Regenerating it is not a rubber stamp: whatever moved has to be argued for in
`NOTICE` under MODIFIED or REMOVED first, which is the whole point of pinning.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import tomllib
import unittest
from collections.abc import Callable
from pathlib import Path

from brainskit.infrastructure import extractor

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "brainskit"
VENDORED = Path(extractor.__file__).resolve().parent / "codeanalysis"
NOTICE = VENDORED / "NOTICE"
LICENSE_MIT = VENDORED / "LICENSE-MIT"

#: The files that knowingly depart from upstream. Kept here *and* in `NOTICE`
#: on purpose: the notice is the human-readable declaration Apache-2.0 asks
#: for, and this is the machine-checked one. They are asserted to agree.
DECLARED_MODIFICATIONS = frozenset({"extract.py", "detect.py", "google_workspace.py"})

#: Files removed from the vendored tree rather than edited. Declared for the
#: same reason and pinned the same way: a removal is a departure from
#: byte-identity too, and an undeclared one would let the tree drift from
#: upstream silently in the direction nobody checks.
DECLARED_REMOVALS = frozenset({"analyze.py", "build.py", "validate.py"})

#: sha256 (first 16 hex digits) of every vendored module, as vendored. This is
#: the byte-identity claim in machine-readable form. Truncated because this is
#: a change detector, not a security control — a collision here costs a missed
#: notice, not an exploit.
VENDORED_SOURCE = {
    "__init__.py": "6bf057553cbdbcd9",
    "cache.py": "0955b389fb0a57e0",
    "cluster.py": "a3920a33cb72c013",
    "detect.py": "5e936f8be887b27d",
    "extract.py": "ad15a901e2b23bf2",
    "extractors/__init__.py": "a7bb47af2815e551",
    "extractors/apex.py": "4881d0e160d98d40",
    "extractors/base.py": "cd11628d3d4c05dc",
    "extractors/bash.py": "41af5e1a45c1b1f9",
    "extractors/blade.py": "64e388ddd010ddf0",
    "extractors/csharp.py": "f3c6d3901eeb7840",
    "extractors/dart.py": "0c2d82b24f7510ef",
    "extractors/dm.py": "f3da427b09102805",
    "extractors/elixir.py": "221c90f161456f32",
    "extractors/engine.py": "2c3f5920503c6ab9",
    "extractors/fortran.py": "8caf869d542e5c1f",
    "extractors/go.py": "222513ed1fe1d9e6",
    "extractors/json_config.py": "d15ea6d9b48cc71e",
    "extractors/julia.py": "acf0097f7c008695",
    "extractors/markdown.py": "2dd93c3158b55a37",
    "extractors/models.py": "0a3465bbef7f7254",
    "extractors/objc.py": "c22feb15ec373a76",
    "extractors/pascal.py": "3eae81396a536251",
    "extractors/pascal_forms.py": "ad68d27b5d4680c1",
    "extractors/powershell.py": "d403e5c56fcfe8d4",
    "extractors/razor.py": "e79739ae271f5990",
    "extractors/resolution.py": "16b697b07a66e599",
    "extractors/rust.py": "f5252845da883494",
    "extractors/sln.py": "9b5987dacc793277",
    "extractors/sql.py": "ae2c98229b0b923a",
    "extractors/terraform.py": "507c8bcf03ddd59b",
    "extractors/verilog.py": "ca3c851d784c3ae6",
    "extractors/zig.py": "b5fff63f95afdb18",
    "google_workspace.py": "5403e9ad850b01f1",
    "ids.py": "ac9138fccaa369ad",
    "manifest_ingest.py": "1f79a52f3c7f7a47",
    "mcp_ingest.py": "7553845a7cae7c31",
    "pascal_resolution.py": "e4e9400ee20e6b41",
    "paths.py": "98b99e4de7290c6e",
    "resolver_registry.py": "bd5904412417ead6",
    "ruby_resolution.py": "10e799b360512584",
    "security.py": "26effec8aec27935",
    "symbol_resolution.py": "1da5a99cf41c5111",
}

#: Sentences asserting the tree carries no edits at all. Any of these is false
#: while `DECLARED_MODIFICATIONS` is non-empty, and the whole `NOTICE` is
#: scanned for them — the previous version of this check partitioned the file
#: first and so could not see the paragraph the live false claim was in.
FALSE_BYTE_IDENTITY_CLAIMS = (
    r"nothing\s+(?:in|inside)\s+this\s+(?:directory|tree)\s+"
    r"(?:is|was|has\s+been)\s+(?:edited|modified|changed)",
    r"no\s+files?\s+(?:in|inside)\s+this\s+(?:directory|tree)\s+"
    r"(?:is|are|was|were|has\s+been|have\s+been)\s+(?:edited|modified|changed)",
    r"nothing\s+here\s+(?:is|was|has\s+been)\s+(?:edited|modified|changed)",
    r"this\s+(?:directory|tree)\s+is\s+byte[-\s]identical",
)

#: Files that live beside the package source but are macOS debris rather than
#: payload. Excluded from the "everything non-Python must be packaged" sweep
#: deliberately, so that sweep does not train anyone to add junk to the globs.
NOT_PAYLOAD = frozenset({".DS_Store"})


def _digests() -> dict[str, str]:
    return {
        path.relative_to(VENDORED).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()[:16]
        for path in sorted(VENDORED.rglob("*.py"))
    }


def _notice_section(heading: str) -> str:
    """The body under a bare `HEADING` line, up to the next heading of any kind.

    `NOTICE` uses two heading styles: bare capitals (`REMOVED`, `MODIFIED`) and
    a title underlined with dashes. Both terminate a section, which is what
    keeps the MODIFIED body from swallowing everything after it.
    """

    lines = NOTICE.read_text(encoding="utf-8").splitlines()
    start = next(
        (
            index + 1
            for index, line in enumerate(lines)
            if line.startswith(heading) and not line[:1].isspace()
        ),
        None,
    )
    if start is None:
        return ""
    body: list[str] = []
    for index in range(start, len(lines)):
        line = lines[index]
        if line and not line[0].isspace():
            following = lines[index + 1] if index + 1 < len(lines) else ""
            underlined = bool(following) and set(following) <= {"-", "="}
            if underlined or re.match(r"^[A-Z]{3,}\b", line):
                break
        body.append(line)
    return "\n".join(body)


def _module_names(section: str) -> set[str]:
    """Bare module names in a section, ignoring anything with a path prefix.

    `application/codegraph.py` is first-party and is named in prose here; the
    negative lookbehind on `/` keeps it out. A bare name cannot be filtered by
    "does the file exist", because a removal is exactly a name that does not.
    """

    return set(re.findall(r"(?<![\w/])([a-z_]+\.py)\b", section))


class VendoredTreeIsPinnedTest(unittest.TestCase):
    """Byte-identity, checked rather than asserted."""

    def test_the_tree_contains_exactly_the_pinned_files(self) -> None:
        found = set(_digests())
        pinned = set(VENDORED_SOURCE)
        self.assertEqual(
            found,
            pinned,
            "the vendored tree gained or lost a file. Undeclared additions are "
            f"{sorted(found - pinned)}; missing are {sorted(pinned - found)}. "
            "Declare the change in NOTICE, then regenerate VENDORED_SOURCE.",
        )

    def test_no_vendored_file_changed_without_declaration(self) -> None:
        found = _digests()
        drifted = sorted(
            name
            for name, digest in VENDORED_SOURCE.items()
            if name in found and found[name] != digest
        )
        self.assertEqual(
            drifted,
            [],
            "vendored source was edited without updating the pin: "
            f"{drifted}. An edit here is a departure from upstream and has to "
            "be argued for in NOTICE under MODIFIED first. This directory is "
            "excluded from ruff and mypy, so this test is the only gate on it.",
        )

    def test_the_pin_covers_every_declared_modification(self) -> None:
        """Control: the pin must not be trivially satisfiable.

        If a declared edit were somehow outside the pinned set, the drift check
        above would pass while the file it exists for went unwatched.
        """

        for name in sorted(DECLARED_MODIFICATIONS):
            with self.subTest(file=name):
                self.assertIn(name, VENDORED_SOURCE)


class NoticeDeclarationTest(unittest.TestCase):
    """MODIFIED and REMOVED are different claims and must not be conflated.

    Before this, `REMOVED` was the only heading in the file, so `extract.py`,
    `detect.py` and `google_workspace.py` — all three present on disk, all
    three reachable — were filed as removals. A present-and-edited file read as
    removed loses exactly the section 4(b) notice the file exists to give.
    """

    def test_the_modified_section_names_exactly_the_modified_files(self) -> None:
        section = _notice_section("MODIFIED")
        self.assertTrue(section, "NOTICE has no MODIFIED heading")
        self.assertEqual(_module_names(section), set(DECLARED_MODIFICATIONS))

    def test_the_removed_section_names_exactly_the_removals(self) -> None:
        section = _notice_section("REMOVED")
        self.assertTrue(section, "NOTICE has no REMOVED heading")
        self.assertEqual(_module_names(section), set(DECLARED_REMOVALS))

    def test_the_two_sections_do_not_overlap(self) -> None:
        modified = _module_names(_notice_section("MODIFIED"))
        removed = _module_names(_notice_section("REMOVED"))
        self.assertEqual(
            modified & removed,
            set(),
            "a file is filed as both modified and removed",
        )

    def test_the_notice_makes_no_false_byte_identity_claim(self) -> None:
        text = NOTICE.read_text(encoding="utf-8")
        offenders = [
            pattern
            for pattern in FALSE_BYTE_IDENTITY_CLAIMS
            if re.search(pattern, text, re.IGNORECASE)
        ]
        self.assertEqual(
            offenders,
            [],
            "NOTICE claims the tree carries no edits while "
            f"{sorted(DECLARED_MODIFICATIONS)} are declared modifications",
        )

    def test_every_declared_modification_is_present(self) -> None:
        for name in sorted(DECLARED_MODIFICATIONS):
            with self.subTest(file=name):
                self.assertTrue((VENDORED / name).is_file(), f"{name} is gone")

    def test_the_declared_removals_are_actually_gone(self) -> None:
        for name in sorted(DECLARED_REMOVALS):
            with self.subTest(file=name):
                self.assertFalse((VENDORED / name).exists())

    def test_nothing_in_the_tree_imports_a_removed_module(self) -> None:
        """Control: a removal that broke the tree would fail here, not at runtime."""

        stems = {name.removesuffix(".py") for name in DECLARED_REMOVALS}
        offenders = []
        for path in VENDORED.rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                for stem in stems:
                    if f"graphify.{stem}" in stripped:
                        offenders.append(f"{path.name}: {stripped}")
        self.assertEqual(offenders, [])


class PackagedAttributionTest(unittest.TestCase):
    """The licence has to travel with the code it licenses.

    Through 0.6.0 it did not: `package-data` globbed `jobs/*` and `templates/*`
    and there is no `MANIFEST.in`, so 43 MIT/Apache source files shipped in
    every wheel and sdist while `LICENSE-MIT` and `NOTICE` stayed behind in the
    repository. MIT requires its permission notice to accompany all copies;
    Apache-2.0 §4(c)/(d) requires the NOTICE content to be conveyed.

    `scripts/verify-wheel.sh` proves it against a real built artifact. This
    proves it against the configuration, at unit-test speed, so the regression
    is caught before anyone builds.
    """

    def globs(self) -> dict[str, list[str]]:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
        package_data = data["tool"]["setuptools"]["package-data"]
        return {
            str(package): [str(pattern) for pattern in patterns]
            for package, patterns in package_data.items()
        }

    def test_the_vendored_attribution_files_are_globbed(self) -> None:
        patterns = self.globs().get("brainskit.infrastructure.codeanalysis", [])
        for name in ("LICENSE-MIT", "NOTICE"):
            with self.subTest(file=name):
                self.assertTrue(
                    any(fnmatch.fnmatch(name, pattern) for pattern in patterns),
                    f"{name} ships nowhere: no package-data glob matches it",
                )

    def test_every_non_python_payload_file_is_globbed(self) -> None:
        """The general form: anything in the package that is not source must ship.

        Stated as a sweep rather than a list because the specific miss was not
        interesting — the class was. `three.min.js`, the schemas and the agent
        templates are all in the same position, and the next added resource
        will be too.
        """

        patterns = self.globs()
        unpackaged = []
        for path in sorted(PACKAGE.rglob("*")):
            if not path.is_file() or path.suffix == ".py":
                continue
            if "__pycache__" in path.parts or path.name in NOT_PAYLOAD:
                continue
            for package, globs in patterns.items():
                base = PACKAGE.joinpath(*package.split(".")[1:])
                if not path.is_relative_to(base):
                    continue
                relative = path.relative_to(base).as_posix()
                if any(fnmatch.fnmatch(relative, pattern) for pattern in globs):
                    break
            else:
                unpackaged.append(str(path.relative_to(PACKAGE)))
        self.assertEqual(
            unpackaged,
            [],
            f"packaged resources no glob matches, so they ship nowhere: {unpackaged}",
        )

    def test_the_mit_licence_still_carries_its_permission_notice(self) -> None:
        text = LICENSE_MIT.read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026 Safi Shamsi", text)
        self.assertIn(
            "shall be included in all\ncopies or substantial portions", text
        )


class CacheMarkerTest(unittest.TestCase):
    """The AST cache namespace must move when the extractor does."""

    def setUp(self) -> None:
        extractor._cache_format_marker.cache_clear()
        self.addCleanup(extractor._cache_format_marker.cache_clear)

    def test_the_marker_is_stable_for_unchanged_source(self) -> None:
        first = extractor._cache_format_marker()
        extractor._cache_format_marker.cache_clear()
        self.assertEqual(first, extractor._cache_format_marker())

    def test_the_marker_is_live_rather_than_the_fallback(self) -> None:
        """`unversioned` is what an unreadable tree yields.

        It is also what every *installed* copy yielded through 0.6.0, because
        the wheel shipped the vendored source without the `NOTICE` this hashes
        — so the namespace the cache relies on was constant in exactly the
        environments the cache runs in. The packaging fix is what closes it;
        this asserts the mechanism is not silently inert here.
        """

        self.assertNotEqual(extractor._cache_format_marker(), "unversioned")

    def test_editing_vendored_source_changes_the_marker(self) -> None:
        # The regression. Editing `extract.py` while leaving `NOTICE` alone used
        # to leave the marker -- and therefore the cache namespace -- untouched.
        target = VENDORED / "extract.py"
        original = target.read_bytes()
        before = extractor._cache_format_marker()
        try:
            target.write_bytes(original + b"\n# cache-marker probe\n")
            extractor._cache_format_marker.cache_clear()
            after = extractor._cache_format_marker()
        finally:
            target.write_bytes(original)
            extractor._cache_format_marker.cache_clear()

        self.assertNotEqual(
            before,
            after,
            "a changed extractor reuses the cache written by the old one",
        )
        self.assertEqual(before, extractor._cache_format_marker())

    def test_editing_the_notice_still_changes_the_marker(self) -> None:
        # The original guarantee, kept: a re-vendor updates NOTICE, and that
        # alone must still invalidate the cache.
        original = NOTICE.read_bytes()
        before = extractor._cache_format_marker()
        try:
            NOTICE.write_bytes(original + b"\n")
            extractor._cache_format_marker.cache_clear()
            after = extractor._cache_format_marker()
        finally:
            NOTICE.write_bytes(original)
            extractor._cache_format_marker.cache_clear()
        self.assertNotEqual(before, after)


class InstallHintTest(unittest.TestCase):
    def test_no_vendored_string_points_at_graphifyy(self) -> None:
        offenders = []
        for path in sorted(VENDORED.rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "graphifyy[" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], f"still advertising graphifyy: {offenders}")


class VendoredAnalysisIsUnreachableTest(unittest.TestCase):
    """`graphify.analyze` and `graphify.build` are no longer imported.

    `analyze.py` did a module-level `from graphify.build import edge_data`, so
    reaching `find_import_cycles` or `graph_diff` loaded 1,643 lines of builder
    plus `validate.py` plus networkx, for a thirteen-line helper. Both analyses
    operate on brainskit's own `code.json` and are now implemented against it,
    which puts 2,487 lines of vendored source out of the import graph.

    `graphify.cluster` is deliberately still reachable: community detection is
    genuine graph-theory work and imports only networkx.
    """

    def first_party(self) -> dict[str, str]:
        return {
            str(path.relative_to(PACKAGE)): path.read_text(encoding="utf-8")
            for path in PACKAGE.rglob("*.py")
            if "codeanalysis" not in path.parts
        }

    def test_no_first_party_module_imports_the_vendored_analysis(self) -> None:
        offenders = [
            name
            for name, text in self.first_party().items()
            if "from graphify import analyze" in text
            or 'graphify.analyze"' in text
            or "import graphify.analyze" in text
        ]
        self.assertEqual(offenders, [], msg="the build.py chain is reachable again")

    def test_cluster_is_still_the_one_delegation(self) -> None:
        """Control: the test must not pass by nothing importing graphify at all."""

        importers = [
            name
            for name, text in self.first_party().items()
            if "from graphify import cluster" in text
        ]
        self.assertEqual(importers, ["application/codegraph.py"])


class ReleaseGateIsolationTest(unittest.TestCase):
    """The gate must not modify the machine it is run on.

    `scripts/verify-wheel.sh` drives the real CLI contract, which ends in
    `bk init "$WORK/vault"` -- and `bk init` registers the vault it creates in
    the machine-level registry. `$WORK` is a `mktemp -d` the script deletes on
    exit, so every run left one more entry pointing at a directory that no
    longer exists. A maintainer's registry had accumulated eleven of them, and
    both `ci.yml` and `release.yml` run this script.

    Pinned here for the same reason `RegistryIsolationTest` pins
    `tests/conftest.py`: the isolation is one line, and one line is exactly what
    a later edit removes without noticing. Asserted statically rather than by
    running the gate, because the gate builds a wheel and this file is unit
    speed -- what can regress is the *placement* of that line, and placement is
    readable from the text.

    (`test_vendoring.py` is its home because this file already owns what
    `verify-wheel.sh` is expected to prove; move it beside the other registry
    tests if that reads better.)
    """

    SCRIPT = ROOT / "scripts" / "verify-wheel.sh"

    def lines(self) -> list[str]:
        return self.SCRIPT.read_text(encoding="utf-8").splitlines()

    def index_of(self, predicate: Callable[[str], bool]) -> int:
        for number, line in enumerate(self.lines()):
            if predicate(line):
                return number
        return -1

    def test_the_gate_redirects_the_machine_registry_into_its_own_workspace(self) -> None:
        exports = [
            line
            for line in self.lines()
            if line.startswith("export XDG_CONFIG_HOME=")
        ]
        self.assertEqual(
            len(exports), 1, "the gate no longer isolates the machine registry"
        )
        self.assertIn(
            '"$WORK', exports[0], "isolated somewhere the gate does not clean up"
        )

    def test_the_isolation_precedes_every_cli_invocation(self) -> None:
        """Placement is the whole fix: an export after `bk init` isolates nothing."""

        export_at = self.index_of(lambda line: line.startswith("export XDG_CONFIG_HOME="))
        first_cli = self.index_of(lambda line: '"$BK"' in line)
        self.assertGreater(export_at, -1, "no XDG_CONFIG_HOME export at all")
        self.assertGreater(first_cli, -1, "the gate no longer drives the CLI")
        self.assertLess(
            export_at, first_cli, "the registry is isolated after the CLI has used it"
        )

    def test_no_other_repository_script_drives_the_cli_unisolated(self) -> None:
        """The general form, since `scripts/` is where the next gate will land."""

        offenders = []
        for path in sorted((ROOT / "scripts").glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            if "/bin/bk" in text and "export XDG_CONFIG_HOME=" not in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"scripts touch the real registry: {offenders}")


if __name__ == "__main__":
    unittest.main()
