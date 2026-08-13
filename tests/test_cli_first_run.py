"""What the first two commands of the quickstart actually show, and the guard
`--force` says it overrides.

`bk capture` is the second command in every quickstart and had no human
renderer, so it fell through to a raw JSON dump -- and the one field the user
needs next, the content hash, sat unlabelled inside it.

`--force`'s help was reported as promising a guard that is not implemented. It
is implemented (`FileVault._refuse_bad_site`), and these tests pin it so the
report cannot become true later.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brainskit.application.services import BrainskitService
from brainskit.domain.model import RefusalError
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine import policy


class CaptureRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.vault = FileVault.initialize(root / "v", policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path)
        )

    def rendered(self) -> str:
        payload = self.service.capture(None, text="Uma nota.", title="nota")
        return cli._render_capture(payload)

    def test_it_is_not_a_json_dump(self) -> None:
        output = self.rendered()
        self.assertNotIn('{"', output)
        self.assertNotIn("content_hash", output)

    def test_it_shows_the_hash_the_next_command_needs(self) -> None:
        payload = self.service.capture(None, text="Outra nota.", title="outra")
        self.assertIn(payload["source"]["content_hash"], cli._render_capture(payload))

    def test_it_names_the_next_command(self) -> None:
        self.assertIn("bk file", self.rendered())

    def test_capture_is_registered_as_a_renderer(self) -> None:
        """Control: the renderer must be reachable, not merely defined."""

        self.assertIn("capture", cli._RENDERERS)


class ForceGuardTest(unittest.TestCase):
    """`--force` overrides a guard that exists. Pinned, not added."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve() / "ws"
        for name in ("proj-a", "proj-b"):
            (self.workspace / name / ".git").mkdir(parents=True)

    def test_a_directory_holding_projects_is_refused(self) -> None:
        with self.assertRaises(RefusalError) as refused:
            FileVault.initialize(self.workspace, policy())
        self.assertEqual(
            sorted(refused.exception.details["repositories"]), ["proj-a", "proj-b"]
        )

    def test_the_refusal_names_force_as_the_override(self) -> None:
        with self.assertRaises(RefusalError) as refused:
            FileVault.initialize(self.workspace, policy())
        self.assertIn("--force", refused.exception.details["hint"])

    def test_force_creates_it_anyway(self) -> None:
        vault = FileVault.initialize(self.workspace, policy(), force=True)
        self.assertTrue((vault.root / ".brain" / "config.json").is_file())

    def test_a_single_project_directory_is_not_refused(self) -> None:
        """Control: the guard must fire on a workspace, not on every directory."""

        single = Path(self.temporary.name).resolve() / "one"
        (single / "proj" / ".git").mkdir(parents=True)
        vault = FileVault.initialize(single, policy())
        self.assertTrue((vault.root / ".brain" / "config.json").is_file())


class ApplyRefusalsNameTheNextStepTest(unittest.TestCase):
    """A refusal that hands back the answer must say it is the answer.

    `missing_base_hash` returns `observed` -- the page's current version, which
    is exactly the value the caller has to send back -- and never said so.
    """

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.vault = FileVault.initialize(root / "v", policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path)
        )
        captured = self.service.capture(None, text="Uma nota publica.", title="nota")
        self.source = captured["source"]["content_hash"]
        self.service.file(self.source, "20-research")
        self.apply("Primeira versao da pagina. ")

    def apply(self, body: str, base_hash: str | None = None) -> dict:
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": "pagina",
            "title": "Pagina",
            "aliases": [],
            "source_hashes": [self.source],
            "body": f"{body}[^source:{self.source}]",
            "links": [],
        }
        if base_hash is not None:
            operation["base_hash"] = base_hash
        return self.service.apply({"operations": [operation]})

    def failure(self, **kwargs: object) -> dict:
        from brainskit.domain.model import ValidationError

        with self.assertRaises(ValidationError) as refused:
            self.apply("Segunda versao, conteudo bem diferente daquele. ", **kwargs)
        return refused.exception.details["failures"][0]

    def test_missing_base_hash_says_observed_is_the_value_to_send(self) -> None:
        failure = self.failure()
        self.assertEqual(failure["code"], "missing_base_hash")
        self.assertIn("observed", failure["hint"])

    def test_missing_base_hash_still_returns_the_value(self) -> None:
        """Control: the hint is useless if the value stopped being there."""

        self.assertTrue(self.failure()["observed"])

    def test_stale_page_says_how_to_recover(self) -> None:
        failure = self.failure(base_hash="0" * 64)
        self.assertEqual(failure["code"], "stale_page")
        self.assertIn("bk context", failure["hint"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
