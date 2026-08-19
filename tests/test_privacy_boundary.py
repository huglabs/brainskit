"""The privacy boundary is one bound object; these tests pin its contract.

ADR 0001: pure rules live in `domain/privacy.py`, the application exposes one
constructor -- `for_consumer(consumer, vault)` -- whose result answers every
"may this consumer see this?" question against snapshots taken at
construction. The snapshot test below is the request-scoped convention made
executable: a boundary built before a write answers from before the write.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import ClassVar

from brainskit.application.ports import IntegrationPort, SyncBoundaryPort
from brainskit.application.privacy import PrivacyBoundary, for_consumer
from brainskit.domain.model import (
    PolicyError,
    PrivacyMode,
    ValidationError,
    VaultConfig,
)
from brainskit.domain.privacy import (
    Consumer,
    branch_privacy,
    resolve_branch_policy,
    strictest_privacy,
)
from brainskit.infrastructure.integrations import NativeIntegrations
from brainskit.infrastructure.vault import FileVault


def policy() -> dict:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {
            "20-research": {"privacy": "local-only", "filing": "auto+digest-review"},
            "30-published": {"privacy": "cloud", "filing": "auto+digest-review"},
            "90-private": {"privacy": "never-ingest", "filing": "approve-each"},
        },
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
        "job_models": {
            job: {"provider": "ollama", "model": "test"}
            for job in (
                "ingest",
                "query",
                "digest",
                "lint-semantic",
                "file-proposal",
                "resurface",
            )
        },
        "sources": [],
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": ["research"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            "obsidian": {"enabled": False, "managed": False, "options": {}},
            "neo4j": {"enabled": False, "managed": False, "options": {}},
            "postgres": {"enabled": False, "managed": False, "options": {}},
            "web": {"enabled": False, "managed": True, "options": {}},
        },
    }


class ConsumerParseTest(unittest.TestCase):
    def test_known_consumers_parse(self) -> None:
        self.assertIs(Consumer.parse("human"), Consumer.HUMAN)
        self.assertIs(Consumer.parse("local"), Consumer.LOCAL)
        self.assertIs(Consumer.parse("cloud"), Consumer.CLOUD)

    def test_a_consumer_instance_passes_through(self) -> None:
        self.assertIs(Consumer.parse(Consumer.CLOUD), Consumer.CLOUD)

    def test_unknown_consumer_is_a_validation_error_naming_it(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            Consumer.parse("robot")
        self.assertEqual(
            "Consumer must be human, local, or cloud", str(caught.exception)
        )
        self.assertEqual({"consumer": "robot"}, caught.exception.details)


class ConsumerLatticeTest(unittest.TestCase):
    #: human sees everything; local everything but never-ingest; cloud only cloud.
    TABLE: ClassVar[dict[tuple[Consumer, PrivacyMode], bool]] = {
        (Consumer.HUMAN, PrivacyMode.CLOUD): True,
        (Consumer.HUMAN, PrivacyMode.LOCAL_ONLY): True,
        (Consumer.HUMAN, PrivacyMode.NEVER_INGEST): True,
        (Consumer.LOCAL, PrivacyMode.CLOUD): True,
        (Consumer.LOCAL, PrivacyMode.LOCAL_ONLY): True,
        (Consumer.LOCAL, PrivacyMode.NEVER_INGEST): False,
        (Consumer.CLOUD, PrivacyMode.CLOUD): True,
        (Consumer.CLOUD, PrivacyMode.LOCAL_ONLY): False,
        (Consumer.CLOUD, PrivacyMode.NEVER_INGEST): False,
    }

    def test_the_full_lattice(self) -> None:
        for (consumer, privacy), expected in self.TABLE.items():
            with self.subTest(consumer=consumer.value, privacy=privacy.value):
                self.assertIs(expected, consumer.allows(privacy))


class ResolveBranchPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = VaultConfig.from_dict(policy())

    def test_inbox_maps_to_the_inbox_policy(self) -> None:
        self.assertIs(
            self.config.inbox_policy, resolve_branch_policy(self.config, "_inbox")
        )

    def test_a_configured_branch_returns_its_policy(self) -> None:
        resolved = resolve_branch_policy(self.config, "90-private")
        self.assertEqual(PrivacyMode.NEVER_INGEST, resolved.privacy)

    def test_a_missing_branch_is_a_policy_error_naming_the_choices(self) -> None:
        with self.assertRaises(PolicyError) as caught:
            resolve_branch_policy(self.config, "55-unconfigured")
        self.assertEqual("55-unconfigured", caught.exception.details["branch"])
        self.assertEqual(
            sorted(self.config.branches), caught.exception.details["configured"]
        )

    def test_branch_privacy_is_the_policy_privacy_as_a_mode(self) -> None:
        self.assertIs(
            PrivacyMode.LOCAL_ONLY, branch_privacy(self.config, "20-research")
        )
        self.assertIs(PrivacyMode.LOCAL_ONLY, branch_privacy(self.config, "_inbox"))


class StrictestPrivacyTest(unittest.TestCase):
    def test_never_ingest_dominates(self) -> None:
        self.assertIs(
            PrivacyMode.NEVER_INGEST,
            strictest_privacy(
                [PrivacyMode.CLOUD, PrivacyMode.NEVER_INGEST, PrivacyMode.LOCAL_ONLY],
                on_empty=PrivacyMode.CLOUD,
            ),
        )

    def test_local_only_beats_cloud(self) -> None:
        self.assertIs(
            PrivacyMode.LOCAL_ONLY,
            strictest_privacy(
                [PrivacyMode.CLOUD, PrivacyMode.LOCAL_ONLY],
                on_empty=PrivacyMode.CLOUD,
            ),
        )

    def test_empty_answers_on_empty(self) -> None:
        self.assertIs(
            PrivacyMode.NEVER_INGEST,
            strictest_privacy([], on_empty=PrivacyMode.NEVER_INGEST),
        )


class BoundaryFixture(unittest.TestCase):
    """A real vault with one record per privacy mode plus an inbox straggler."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.cloud_hash = self.capture("Cloud fact.", "cloud-note", "30-published")
        self.local_hash = self.capture("Local fact.", "local-note", "20-research")
        self.never_hash = self.capture("Private fact.", "private-note", "90-private")
        self.inbox_hash = self.capture("Inbox fact.", "inbox-note", None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, text: str, title: str, branch: str | None) -> str:
        record, _ = self.vault.capture_text(text, title)
        if branch is not None:
            self.vault.file_source(record.content_hash, branch)
        return record.content_hash

    def write_wiki(self, relative: str, sources: list[str] | None) -> None:
        path = self.root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if sources is None:
            text = "---\ntype: concept\ntitle: Page\n---\n\nBody.\n"
        else:
            cited = "\n".join(f"  - {content_hash}" for content_hash in sources)
            text = f"---\ntype: concept\ntitle: Page\nsources:\n{cited}\n---\n\nBody.\n"
        path.write_text(text, encoding="utf-8")


class ForConsumerTest(BoundaryFixture):
    def test_an_unknown_consumer_fails_at_construction(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            for_consumer("robot", self.vault)
        self.assertEqual({"consumer": "robot"}, caught.exception.details)

    def test_the_boundary_snapshots_the_registry_at_construction(self) -> None:
        boundary = for_consumer("local", self.vault)
        before = dict(boundary.records)
        split_before = boundary.split_records()
        late_record, _ = self.vault.capture_text("Arrived later.", "late-note")
        self.assertIn(late_record.content_hash, self.vault.registry())
        self.assertNotIn(late_record.content_hash, boundary.records)
        self.assertEqual(before, dict(boundary.records))
        self.assertEqual(split_before, boundary.split_records())

    def test_records_is_a_read_only_view(self) -> None:
        boundary = for_consumer("local", self.vault)
        with self.assertRaises(TypeError):
            boundary.records["forged"] = object()  # type: ignore[index]


class BoundaryDecisionTest(BoundaryFixture):
    def test_allows_follows_the_consumer_lattice(self) -> None:
        boundary = for_consumer("cloud", self.vault)
        self.assertTrue(boundary.allows(PrivacyMode.CLOUD))
        self.assertFalse(boundary.allows(PrivacyMode.LOCAL_ONLY))

    def test_record_privacy_reads_the_branch_policy(self) -> None:
        boundary = for_consumer("local", self.vault)
        never = boundary.records[self.never_hash]
        self.assertIs(PrivacyMode.NEVER_INGEST, boundary.record_privacy(never))
        self.assertFalse(boundary.allows_record(never))
        self.assertTrue(boundary.allows_record(boundary.records[self.local_hash]))

    def test_branch_privacy_answers_for_the_inbox_too(self) -> None:
        boundary = for_consumer("local", self.vault)
        self.assertIs(PrivacyMode.LOCAL_ONLY, boundary.branch_privacy("_inbox"))
        self.assertIs(PrivacyMode.CLOUD, boundary.branch_privacy("30-published"))

    def test_split_records_agrees_with_the_lattice(self) -> None:
        expectations = {
            "human": (4, 0),
            "local": (3, 1),
            "cloud": (1, 3),
        }
        for consumer, (visible_count, redacted_count) in expectations.items():
            with self.subTest(consumer=consumer):
                visible, redacted = for_consumer(consumer, self.vault).split_records()
                self.assertEqual(visible_count, len(visible))
                self.assertEqual(redacted_count, redacted)
        visible, _ = for_consumer("cloud", self.vault).split_records()
        self.assertEqual({self.cloud_hash}, set(visible))


class EvidenceTest(BoundaryFixture):
    def test_evidence_privacy_short_circuits_on_a_registered_hash(self) -> None:
        boundary = for_consumer("local", self.vault)
        hit = {"content_hash": self.never_hash, "path": "ignored"}
        self.assertIs(PrivacyMode.NEVER_INGEST, boundary.evidence_privacy(hit, ""))
        self.assertFalse(boundary.allows_evidence(hit, ""))

    def test_evidence_privacy_reads_the_page_via_the_vault_when_content_is_none(
        self,
    ) -> None:
        self.write_wiki("wiki/concepts/secret.md", [self.never_hash])
        boundary = for_consumer("local", self.vault)
        hit = {"path": "wiki/concepts/secret.md"}
        self.assertIs(PrivacyMode.NEVER_INGEST, boundary.evidence_privacy(hit))
        self.assertFalse(boundary.allows_evidence(hit))
        self.assertTrue(for_consumer("human", self.vault).allows_evidence(hit))

    def test_evidence_branches_resolves_cited_sources(self) -> None:
        boundary = for_consumer("local", self.vault)
        hit = {"content_hash": self.local_hash, "path": "raw/20-research/x.md"}
        self.assertEqual(["20-research"], boundary.evidence_branches(hit, ""))
        page = (
            "---\ntitle: Page\nsources:\n"
            f"  - {self.cloud_hash}\n  - {self.never_hash}\n---\n\nBody.\n"
        )
        self.assertEqual(
            ["30-published", "90-private"],
            boundary.evidence_branches({"path": "wiki/p.md"}, page),
        )


class AllowsPathTest(BoundaryFixture):
    def boundary(self, consumer: str) -> PrivacyBoundary:
        return for_consumer(consumer, self.vault)

    def test_the_egress_table(self) -> None:
        self.write_wiki("wiki/concepts/secret.md", [self.never_hash])
        self.write_wiki("wiki/concepts/open.md", [self.cloud_hash])
        self.write_wiki("wiki/system/home.md", None)
        table = [
            # An unreconciled inbox file sits under a known policy: honoured,
            # never over-blocked.
            ("raw/_inbox/arrival.md", {"human": True, "local": True, "cloud": False}),
            # No configured branch means no policy says this may leave.
            (
                "raw/55-unconfigured/x.md",
                {"human": True, "local": False, "cloud": False},
            ),
            ("raw/90-private/private-note.md", {"human": True, "local": False}),
            # Wiki pages are judged by frontmatter provenance, read at decision
            # time.
            (
                "wiki/concepts/secret.md",
                {"human": True, "local": False, "cloud": False},
            ),
            ("wiki/concepts/open.md", {"human": True, "local": True, "cloud": True}),
            # No provenance declared at all: a system page, legitimately cloud.
            ("wiki/system/home.md", {"human": True, "local": True, "cloud": True}),
            # Everything outside wiki/ and raw/ is regenerated pre-filtered.
            ("views/home.md", {"human": True, "local": True, "cloud": True}),
            ("graph/graph.json", {"human": True, "local": True, "cloud": True}),
        ]
        for relative, expectations in table:
            for consumer, expected in expectations.items():
                with self.subTest(path=relative, consumer=consumer):
                    self.assertIs(
                        expected,
                        self.boundary(consumer).allows_path(PurePosixPath(relative)),
                    )


class SyncBoundaryPortTest(BoundaryFixture):
    def test_the_boundary_satisfies_the_port(self) -> None:
        boundary: SyncBoundaryPort = for_consumer("local", self.vault)
        self.assertIsInstance(boundary.consumer, str)
        self.assertEqual("local", boundary.consumer)
        self.assertTrue(boundary.allows_path(PurePosixPath("views/home.md")))

    def test_integration_sync_requires_a_boundary(self) -> None:
        """Required, not defaulted: an optional fallback would be a second,
        degraded decision path kept alive for test reachability (ADR 0001)."""

        for owner in (IntegrationPort, NativeIntegrations):
            with self.subTest(owner=owner.__name__):
                parameter = inspect.signature(owner.sync).parameters.get("boundary")
                self.assertIsNotNone(parameter, f"{owner.__name__}.sync lacks it")
                self.assertIs(parameter.default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
