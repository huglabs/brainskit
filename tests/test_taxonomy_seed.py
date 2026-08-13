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
import re
import sys
import unittest
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from brainskit.application.services import BrainskitService
from brainskit.domain.model import ValidationError
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.llm import JobSpecs
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine import policy

#: Supplied by the machinery for every job rather than by the caller:
#: `wiki_language` by `PolicyJudgmentRouter.run`, `repair_feedback` by
#: `JudgmentRunner.run`. Everything else in the template must come from the
#: caller, which for `file-proposal` is `Filing._resolve_filing_destination`.
MACHINERY_VARIABLES = frozenset({"wiki_language", "repair_feedback"})


def file_proposal_template() -> str:
    resource = files("brainskit").joinpath("jobs", "file-proposal.md")
    return resource.read_text(encoding="utf-8")


def placeholders_in(template: str) -> set[str]:
    """The same expression `JobSpecs.prompt` substitutes with."""

    return set(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", template))


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


class FilingCallMixin:
    """Drives one real ingest in a throwaway vault and records the filing call."""

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


class TaxonomySeedBiasesFilingTest(FilingCallMixin, unittest.TestCase):
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

    def test_an_empty_seed_marks_nothing(self) -> None:
        """An empty list is a legitimate answer, not a reason to fail."""

        service, runner = self.build([])
        self.ingest_one(service)
        payload = self.branches_payload(runner)
        self.assertFalse(any(entry["seed"] for entry in payload.values()))


class FileProposalPromptContractTest(FilingCallMixin, unittest.TestCase):
    """What the caller passes and what the prompt reads must be one set.

    `JobSpecs.prompt` refuses a template whose placeholder has no variable, so
    the "referenced but never supplied" direction already fails loudly at
    runtime. The other direction is silent: an extra variable is simply never
    substituted, which is how `taxonomy_seed` was passed on every filing call
    and read by nothing -- the same shape of dead wiring as the policy key it
    came from. These tests make that direction fail here instead.
    """

    def passed_variables(self) -> set[str]:
        service, runner = self.build(["20-research"])
        self.ingest_one(service)
        return set(runner.variables_for("file-proposal"))

    def test_every_variable_passed_is_read_by_the_template(self) -> None:
        """The silent direction. A variable nothing substitutes is dead weight
        on every filing call."""

        unread = self.passed_variables() - placeholders_in(file_proposal_template())
        self.assertEqual(
            unread,
            set(),
            f"passed to file-proposal but never referenced by it: {sorted(unread)}",
        )

    def test_every_placeholder_is_supplied_by_someone(self) -> None:
        """Control for the direction that already fails loudly."""

        unsupplied = (
            placeholders_in(file_proposal_template())
            - self.passed_variables()
            - MACHINERY_VARIABLES
        )
        self.assertEqual(
            unsupplied,
            set(),
            f"referenced by file-proposal but supplied by nobody: {sorted(unsupplied)}",
        )

    def test_the_prompt_renders_with_nothing_left_unsubstituted(self) -> None:
        """End to end through the real renderer, which also proves
        `MACHINERY_VARIABLES` is the whole remainder: a third framework-supplied
        variable would make this raise `Job variables are incomplete`."""

        service, runner = self.build(["20-research"])
        self.ingest_one(service)
        prompt = JobSpecs().prompt(
            "file-proposal",
            {
                **runner.variables_for("file-proposal"),
                **dict.fromkeys(MACHINERY_VARIABLES, ""),
            },
        )
        self.assertNotIn("{{", prompt)

    def test_the_template_explains_the_seed_flag(self) -> None:
        """`seed` reaches the model inside `{{branches}}`. A boolean the prompt
        never mentions is inert: the model has no instruction for it, so the
        flag would be computed, serialised and ignored."""

        self.assertIn("seed", file_proposal_template())

    def test_no_packaged_template_carries_an_injected_tracking_footer(self) -> None:
        """Every byte of these files is prompt, sent on every judgment call.

        `.claude/hooks/md-timestamp-tracker.sh` appends a `<!-- doc-tracking -->`
        footer to any `.md` written in the project and grows it by one
        `- Updated:` line per edit. Its exclusion list covers `docs/brain/` and
        the root-level public docs but not these, so editing a template silently
        appends changelog lines to a prompt -- and ships them in the wheel.
        """

        jobs = files("brainskit").joinpath("jobs")
        polluted = [
            entry.name
            for entry in jobs.iterdir()
            if entry.name.endswith(".md")
            and "doc-tracking" in entry.read_text(encoding="utf-8")
        ]
        self.assertEqual(polluted, [], f"tracking footer inside prompts: {polluted}")

    def test_the_seed_list_is_not_passed_alongside_the_flags(self) -> None:
        """Records the decision, so re-adding it has to be argued for.

        A separate sorted list is derivable from the per-branch flags and
        costs tokens on every filing call. Worse, the two can disagree:
        `taxonomy_seed` is not constrained to be a subset of `branches`, so a
        seed naming an unconfigured branch would invite the model to propose a
        destination `_resolve_filing_destination` then rejects.
        """

        self.assertNotIn("taxonomy_seed", self.passed_variables())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
