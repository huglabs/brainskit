"""`taxonomy_seed` must have a reader.

It was `required` by the policy schema, written by the onboarding wizard as
`sorted(branches)`, serialised on every save -- and read by nothing. Five write
and parse sites, zero readers. Simultaneously dead code and one of the nine
fields that blocked a hand-written config, which is the worst of both.

Decision: it biases filing. The filing proposal is shown which branches belong
to the vault's declared taxonomy, so a branch someone added for one stray
document is not treated as an equal candidate to the shape the operator
actually described.

These tests assert the value reaches the judgment call and changes what it is
asked. A test that only round-trips the key through serialisation is what let
it stay dead.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from brainskit.application.services import BrainskitService
from brainskit.domain.model import ValidationError
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine import policy


class RecordingRunner:
    """Captures what the filing job was asked, without calling a provider."""

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.calls: list[dict[str, Any]] = []

    def run(self, *, job: str, branches: list[str], variables: dict[str, Any],
            validator: Any = None) -> dict[str, Any]:
        self.calls.append({"job": job, "variables": variables})
        if job == "file-proposal":
            return {"branch": self.branch, "reason": "test", "confidence": 0.9}
        return {
            "proposal_id": "wiki-test",
            "operations": [],
        }

    def variables_for(self, job: str) -> dict[str, Any]:
        for call in self.calls:
            if call["job"] == job:
                return call["variables"]
        raise AssertionError(f"{job} was never run")


class TaxonomySeedBiasesFilingTest(unittest.TestCase):
    def build(self, seed: list[str]) -> tuple[BrainskitService, RecordingRunner]:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        document = policy()
        document["taxonomy_seed"] = seed
        vault = FileVault.initialize(Path(temporary.name).resolve() / "v", document)
        service = BrainskitService(vault, SqliteFtsIndex(vault.index_path))
        runner = RecordingRunner(branch="20-research")
        service.filing.judgment_runner = runner
        return service, runner

    def ingest_one(self, service: BrainskitService) -> None:
        captured = service.capture(None, text="Uma nota qualquer.", title="nota")
        try:
            service.ingest(captured["source"]["content_hash"])
        except ValidationError:
            # The stub returns no wiki operations, so the *apply* half is
            # rejected. That is downstream of the filing decision these tests
            # are about, and the recorded call has already happened.
            pass

    def branches_payload(self, runner: RecordingRunner) -> dict[str, Any]:
        raw = runner.variables_for("file-proposal")["branches"]
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    def test_seed_branches_are_marked_for_the_filing_decision(self) -> None:
        service, runner = self.build(["20-research"])
        self.ingest_one(service)
        payload = self.branches_payload(runner)
        self.assertTrue(payload["20-research"]["seed"])

    def test_non_seed_branches_are_marked_as_such(self) -> None:
        """Control: the flag must discriminate, not be True everywhere."""

        service, runner = self.build(["20-research"])
        self.ingest_one(service)
        payload = self.branches_payload(runner)
        self.assertFalse(payload["10-work"]["seed"])
        self.assertEqual(
            [name for name, entry in payload.items() if entry["seed"]],
            ["20-research"],
        )

    def test_changing_the_seed_changes_what_the_job_is_asked(self) -> None:
        """The observable effect. Without this the flag could be a constant."""

        first, first_runner = self.build(["20-research"])
        self.ingest_one(first)
        second, second_runner = self.build(["10-work"])
        self.ingest_one(second)
        self.assertNotEqual(
            self.branches_payload(first_runner),
            self.branches_payload(second_runner),
        )

    def test_the_seed_list_itself_reaches_the_job(self) -> None:
        service, runner = self.build(["20-research", "10-work"])
        self.ingest_one(service)
        seed = json.loads(runner.variables_for("file-proposal")["taxonomy_seed"])
        self.assertEqual(seed, ["10-work", "20-research"])

    def test_an_empty_seed_marks_nothing(self) -> None:
        """An empty list is a legitimate answer, not a reason to fail."""

        service, runner = self.build([])
        self.ingest_one(service)
        payload = self.branches_payload(runner)
        self.assertFalse(any(entry["seed"] for entry in payload.values()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
