from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from brainskit.application.gate import (
    DEFAULT_DENY_PREFIXES,
    GateDecision,
    check_write,
    load_gate_policy,
)
from brainskit.application.services import BrainskitService

DECISION_KEYS = {"allowed", "path", "reason", "remediation", "rule"}


class GateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.outside_temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.outside = Path(self.outside_temporary.name)
        for relative in (".brain", "wiki/entities", "raw/_inbox", "notes", "views"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.outside_temporary.cleanup()

    def write_policy(self, document: Any, *, agent: str = "claude") -> Path:
        path = self.root / ".brain" / f"agent-{agent}.json"
        text = document if isinstance(document, str) else json.dumps(document, indent=2)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    def decide(self, target: str | Path, **kwargs: Any) -> GateDecision:
        return check_write(self.root, target, **kwargs)

    def assertDenied(self, target: str | Path, rule: str = "wiki/") -> GateDecision:
        decision = self.decide(target)
        self.assertFalse(decision.allowed, f"{target!r} should have been denied")
        self.assertEqual(decision.rule, rule)
        self.assertIsInstance(decision.reason, str)
        return decision

    def assertAllowed(self, target: str | Path) -> GateDecision:
        decision = self.decide(target)
        self.assertTrue(decision.allowed, f"{target!r} should have been allowed")
        return decision


class GateMatchingTest(GateFixture):
    def test_default_prefixes_are_wiki_and_raw(self) -> None:
        self.assertEqual(DEFAULT_DENY_PREFIXES, ("wiki/", "raw/"))

    def test_gated_directories_are_denied(self) -> None:
        self.assertDenied("wiki/index.md", "wiki/")
        self.assertDenied("raw/_inbox/note.md", "raw/")

    def test_nested_paths_are_denied(self) -> None:
        decision = self.assertDenied("wiki/entities/deep/nested/page.md", "wiki/")
        self.assertEqual(decision.path, "wiki/entities/deep/nested/page.md")

    def test_bare_gated_directory_is_denied(self) -> None:
        # The directory itself is inside the governed subtree.
        self.assertDenied("wiki", "wiki/")
        self.assertDenied("wiki/", "wiki/")

    def test_matching_is_on_segment_boundaries(self) -> None:
        # The near-miss the contract calls out by name: a prefix must not match
        # a longer first segment.
        self.assertAllowed("wikipedia/a.md")
        self.assertAllowed("wiki-notes/a.md")
        self.assertAllowed("rawdata/x.txt")
        self.assertAllowed("wikis/a.md")

    def test_ungated_vault_paths_are_allowed(self) -> None:
        self.assertAllowed("views/map/overview.md")
        self.assertAllowed("notes/scratch.md")
        self.assertAllowed(".brain/freshness.json")

    def test_dot_slash_prefix_is_folded(self) -> None:
        decision = self.assertDenied("./wiki/index.md", "wiki/")
        self.assertEqual(decision.path, "wiki/index.md")

    def test_absolute_path_inside_the_vault_is_denied(self) -> None:
        decision = self.assertDenied(self.root / "wiki" / "index.md", "wiki/")
        self.assertEqual(decision.path, "wiki/index.md")

    def test_backslash_is_read_as_a_separator(self) -> None:
        # brainskit is POSIX-only, so a backslash is a stray Windows separator
        # rather than a filename character. Reading it as one over-denies at
        # worst; ignoring it would be a bypass.
        self.assertDenied("wiki\\index.md", "wiki/")

    def test_case_differences_do_not_bypass_the_gate(self) -> None:
        # macOS filesystems are case-insensitive, so WIKI/x.md is the same file.
        self.assertDenied("WIKI/index.md", "wiki/")
        self.assertDenied("Wiki/Entities/Page.MD", "wiki/")
        self.assertDenied("RAW/_inbox/note.md", "raw/")

    def test_the_vault_root_itself_is_not_gated(self) -> None:
        self.assertAllowed(self.root)
        self.assertAllowed(".")


class GateEscapeTest(GateFixture):
    def test_paths_outside_the_vault_are_allowed(self) -> None:
        # The gate governs the vault, not the filesystem.
        decision = self.assertAllowed("/etc/passwd")
        self.assertEqual(decision.path, "/etc/passwd")
        self.assertAllowed(self.outside / "wiki" / "index.md")

    def test_traversal_leaving_the_vault_is_allowed(self) -> None:
        # It starts with "wiki/" textually but lands outside the vault, so it is
        # not the gate's business. The input is echoed back unchanged.
        decision = self.assertAllowed("wiki/../../etc/passwd")
        self.assertEqual(decision.path, "wiki/../../etc/passwd")

    def test_traversal_re_entering_the_vault_is_denied(self) -> None:
        for target in (
            "notes/../wiki/index.md",
            "wiki/../wiki/index.md",
            "views/map/../../raw/_inbox/note.md",
        ):
            with self.subTest(target=target):
                decision = self.decide(target)
                self.assertFalse(decision.allowed)

    def test_traversal_leaving_and_returning_by_absolute_path(self) -> None:
        target = f"../{self.root.name}/wiki/index.md"
        decision = self.assertDenied(target, "wiki/")
        self.assertEqual(decision.path, "wiki/index.md")

    def test_symlink_into_a_gated_directory_is_denied(self) -> None:
        (self.root / "wiki" / "index.md").write_text("# index\n", encoding="utf-8")
        link = self.root / "notes" / "sneaky.md"
        link.symlink_to(Path("..") / "wiki" / "index.md")
        decision = self.assertDenied("notes/sneaky.md", "wiki/")
        # The resolved target is reported, because that is the file that would
        # actually change.
        self.assertEqual(decision.path, "wiki/index.md")

    def test_symlink_leaving_a_gated_directory_is_still_denied(self) -> None:
        # Writing through wiki/escape.md changes what the vault serves as a wiki
        # page, so the lexical form has to be checked as well as the resolved
        # one. Otherwise this is a one-symlink bypass of the apply gate.
        target = self.outside / "planted.md"
        target.write_text("planted\n", encoding="utf-8")
        (self.root / "wiki" / "escape.md").symlink_to(target)
        decision = self.assertDenied("wiki/escape.md", "wiki/")
        self.assertEqual(decision.path, "wiki/escape.md")

    def test_symlinked_directory_into_a_gated_tree_is_denied(self) -> None:
        (self.root / "notes" / "mirror").symlink_to(
            self.root / "wiki", target_is_directory=True
        )
        self.assertDenied("notes/mirror/new-page.md", "wiki/")

    def test_symlinked_vault_root_still_matches(self) -> None:
        link = self.outside / "vault-link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(check_write(link, "wiki/index.md").allowed)
        # A caller may hand in the real path while naming the vault by its link.
        decision = check_write(link, self.root / "wiki" / "index.md")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.path, "wiki/index.md")

    def test_broken_symlink_is_resolved_rather_than_skipped(self) -> None:
        link = self.root / "notes" / "dangling.md"
        link.symlink_to(Path("..") / "wiki" / "missing.md")
        self.assertDenied("notes/dangling.md", "wiki/")


class GateDecisionShapeTest(GateFixture):
    def test_allowed_decision_carries_no_reason(self) -> None:
        decision = self.assertAllowed("views/map/overview.md")
        self.assertIsNone(decision.reason)
        self.assertIsNone(decision.remediation)
        self.assertIsNone(decision.rule)
        self.assertEqual(decision.path, "views/map/overview.md")

    def test_to_dict_carries_exactly_the_contract_keys(self) -> None:
        for target in ("views/map/overview.md", "wiki/index.md"):
            with self.subTest(target=target):
                payload = self.decide(target).to_dict()
                self.assertEqual(set(payload), DECISION_KEYS)
                self.assertIsInstance(payload["allowed"], bool)
                self.assertIsInstance(payload["path"], str)

    def test_denied_decision_is_json_serialisable(self) -> None:
        payload = self.decide("wiki/index.md").to_dict()
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["rule"], "wiki/")
        self.assertIn("wiki/index.md", payload["reason"])
        # The hook joins reason and remediation into one stderr line.
        self.assertTrue(payload["reason"].endswith("."))

    def test_decisions_are_frozen(self) -> None:
        decision = self.decide("wiki/index.md")
        # A frozen dataclass raises FrozenInstanceError, not any exception:
        # asserting the precise type is what proves the field is really frozen.
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.allowed = True  # type: ignore[misc]


class GatePolicyLoadingTest(GateFixture):
    def test_missing_policy_file_falls_back_to_defaults(self) -> None:
        policy = load_gate_policy(self.root)
        self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)
        self.assertIsNotNone(policy.degraded)
        self.assertFalse(self.decide("wiki/index.md").allowed)

    def test_missing_vault_falls_back_to_defaults(self) -> None:
        policy = load_gate_policy(self.root / "does" / "not" / "exist")
        self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)

    def test_malformed_json_falls_back_to_defaults(self) -> None:
        self.write_policy("{ this is not json ")
        policy = load_gate_policy(self.root)
        self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)
        self.assertIn("valid JSON", str(policy.degraded))
        self.assertFalse(self.decide("wiki/index.md").allowed)

    def test_non_object_document_falls_back_to_defaults(self) -> None:
        self.write_policy("[1, 2, 3]")
        self.assertEqual(load_gate_policy(self.root).deny_prefixes, DEFAULT_DENY_PREFIXES)

    def test_old_shape_adapter_without_gate_falls_back_to_defaults(self) -> None:
        # Exactly what _install_hooks() writes today.
        self.write_policy(
            {
                "agent": "claude",
                "rules": [
                    "Read evidence with bk context --json --consumer local",
                    "Write wiki pages only with bk apply",
                    "Never edit raw content",
                ],
            }
        )
        policy = load_gate_policy(self.root)
        self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)
        self.assertIn("no gate section", str(policy.degraded))
        self.assertFalse(self.decide("wiki/index.md").allowed)

    def test_wrong_types_inside_valid_json_fall_back_to_defaults(self) -> None:
        for document in (
            {"gate": "wiki/"},
            {"gate": ["wiki/"]},
            {"gate": {}},
            {"gate": {"deny_prefixes": "wiki/"}},
            {"gate": {"deny_prefixes": {"wiki/": True}}},
            {"gate": {"deny_prefixes": [1, 2, 3]}},
            {"gate": {"deny_prefixes": ["", "  ", "/"]}},
            {"gate": None},
        ):
            with self.subTest(document=document):
                self.write_policy(document)
                policy = load_gate_policy(self.root)
                self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)
                self.assertFalse(self.decide("wiki/index.md").allowed)

    def test_unreadable_policy_file_falls_back_to_defaults(self) -> None:
        # A directory where a file is expected raises IsADirectoryError.
        (self.root / ".brain" / "agent-claude.json").mkdir()
        self.assertEqual(load_gate_policy(self.root).deny_prefixes, DEFAULT_DENY_PREFIXES)
        self.assertFalse(self.decide("wiki/index.md").allowed)

    def test_undecodable_policy_file_falls_back_to_defaults(self) -> None:
        (self.root / ".brain" / "agent-claude.json").write_bytes(b"\xff\xfe\x00garbage")
        self.assertEqual(load_gate_policy(self.root).deny_prefixes, DEFAULT_DENY_PREFIXES)

    def test_partial_entries_are_dropped_without_losing_the_rest(self) -> None:
        self.write_policy({"gate": {"deny_prefixes": ["views/", 7, None, "  "]}})
        policy = load_gate_policy(self.root)
        self.assertEqual(policy.deny_prefixes, ("views/",))

    def test_custom_deny_prefixes_replace_the_defaults(self) -> None:
        self.write_policy({"version": 1, "gate": {"deny_prefixes": ["views/", "graph/"]}})
        policy = load_gate_policy(self.root)
        self.assertEqual(policy.deny_prefixes, ("views/", "graph/"))
        self.assertIsNone(policy.degraded)
        self.assertEqual(policy.source, self.root / ".brain" / "agent-claude.json")
        self.assertDenied("views/map/overview.md", "views/")
        self.assertDenied("graph/graph.json", "graph/")
        # The defaults are replaced, not extended.
        self.assertAllowed("wiki/index.md")

    def test_multi_segment_prefixes_match_on_boundaries(self) -> None:
        self.write_policy({"gate": {"deny_prefixes": ["raw/_inbox/"]}})
        self.assertDenied("raw/_inbox/note.md", "raw/_inbox/")
        self.assertDenied("raw/_inbox", "raw/_inbox/")
        self.assertAllowed("raw/_inboxes/note.md")
        self.assertAllowed("raw/10-work/note.md")

    def test_prefix_without_a_trailing_slash_still_matches_segments(self) -> None:
        self.write_policy({"gate": {"deny_prefixes": ["views"]}})
        self.assertDenied("views/map/overview.md", "views")
        self.assertAllowed("viewsource/a.md")

    def test_explicitly_empty_deny_list_denies_nothing(self) -> None:
        # An empty list is a deliberate opt-out, distinguishable from a missing
        # or malformed key.
        self.write_policy({"gate": {"deny_prefixes": []}})
        policy = load_gate_policy(self.root)
        self.assertEqual(policy.deny_prefixes, ())
        self.assertIsNone(policy.degraded)
        self.assertAllowed("wiki/index.md")
        self.assertAllowed("raw/_inbox/note.md")


class GateRemediationTest(GateFixture):
    def test_policy_remediation_is_surfaced(self) -> None:
        self.write_policy(
            {
                "gate": {
                    "deny_prefixes": ["wiki/", "raw/"],
                    "remediation": {
                        "wiki/": "Wiki pages are written only by the apply gate. Use: bk apply",
                        "raw/": "Sources are immutable and hash-identified. Use: bk capture",
                    },
                }
            }
        )
        decision = self.assertDenied("wiki/index.md", "wiki/")
        self.assertEqual(
            decision.remediation,
            "Wiki pages are written only by the apply gate. Use: bk apply",
        )
        self.assertEqual(
            self.decide("raw/_inbox/note.md").remediation,
            "Sources are immutable and hash-identified. Use: bk capture",
        )

    def test_policy_remediation_overrides_the_built_in_text(self) -> None:
        self.write_policy(
            {"gate": {"deny_prefixes": ["wiki/"], "remediation": {"wiki/": "Ask Raul."}}}
        )
        self.assertEqual(self.decide("wiki/index.md").remediation, "Ask Raul.")

    def test_built_in_remediation_fills_a_gap(self) -> None:
        self.write_policy({"gate": {"deny_prefixes": ["wiki/", "views/"]}})
        self.assertIn("bk apply", str(self.decide("wiki/index.md").remediation))
        # No built-in guidance exists for a custom prefix.
        self.assertIsNone(self.decide("views/map/overview.md").remediation)

    def test_defaults_carry_remediation_without_a_policy_file(self) -> None:
        self.assertIn("bk apply", str(self.decide("wiki/index.md").remediation))
        self.assertIn("bk capture", str(self.decide("raw/_inbox/n.md").remediation))

    def test_non_string_remediation_entries_are_ignored(self) -> None:
        self.write_policy(
            {"gate": {"deny_prefixes": ["views/"], "remediation": {"views/": 42}}}
        )
        self.assertIsNone(self.decide("views/map/overview.md").remediation)


class GateAgentSelectionTest(GateFixture):
    def test_the_agent_selects_the_adapter_file(self) -> None:
        self.write_policy({"gate": {"deny_prefixes": []}}, agent="codex")
        self.assertTrue(check_write(self.root, "wiki/index.md", agent="codex").allowed)
        # claude has no adapter, so it keeps the defaults.
        self.assertFalse(check_write(self.root, "wiki/index.md", agent="claude").allowed)

    def test_unsafe_agent_names_cannot_redirect_the_loader(self) -> None:
        for agent in ("../../etc/passwd", "a/b", "", "..\\x", "cla ude", "x\x00y"):
            with self.subTest(agent=agent):
                policy = load_gate_policy(self.root, agent=agent)
                self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)
                self.assertIsNone(policy.source)
                self.assertFalse(check_write(self.root, "wiki/x.md", agent=agent).allowed)


class GateNeverRaisesTest(GateFixture):
    def test_hostile_inputs_return_a_decision(self) -> None:
        targets: list[str | Path] = [
            "",
            ".",
            "..",
            "/",
            "//wiki//index.md",
            "wiki//////index.md",
            "wiki/./././index.md",
            "../" * 64 + "wiki/index.md",
            "wiki/" + "a" * 4096 + ".md",
            "wiki/naïve—page.md",
            Path("wiki/index.md"),
        ]
        for target in targets:
            with self.subTest(target=target):
                decision = check_write(self.root, target)
                self.assertIsInstance(decision, GateDecision)
                self.assertIsInstance(decision.to_dict(), dict)

    def test_repeated_separators_do_not_hide_a_gated_path(self) -> None:
        for target in ("wiki//////index.md", "wiki/./index.md", "wiki/.//./index.md"):
            with self.subTest(target=target):
                self.assertFalse(self.decide(target).allowed)

    def test_a_leading_slash_means_absolute_and_lands_outside(self) -> None:
        # "/wiki/x" and "//wiki/x" are absolute POSIX paths naming the
        # filesystem root's wiki, not this vault's, so the gate lets them pass.
        for target in ("/wiki/index.md", "//wiki//index.md"):
            with self.subTest(target=target):
                self.assertTrue(self.decide(target).allowed)

    def test_a_file_where_the_brain_directory_belongs_is_tolerated(self) -> None:
        root = Path(self.outside) / "not-a-vault"
        root.mkdir()
        (root / ".brain").write_text("not a directory\n", encoding="utf-8")
        policy = load_gate_policy(root)
        self.assertEqual(policy.deny_prefixes, DEFAULT_DENY_PREFIXES)
        self.assertFalse(check_write(root, "wiki/index.md").allowed)


class _StubVault:
    """Minimal stand-in: the gate only ever reads the vault root."""

    def __init__(self, root: Path) -> None:
        self.root = root


class _StubIndex:
    pass


class GateServiceDelegationTest(GateFixture):
    def service(self) -> BrainskitService:
        return BrainskitService(_StubVault(self.root), _StubIndex())  # type: ignore[arg-type]

    def test_service_returns_the_decision_as_a_dict(self) -> None:
        payload = self.service().gate_check_write("wiki/index.md")
        self.assertEqual(set(payload), DECISION_KEYS)
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["rule"], "wiki/")
        self.assertIn("bk apply", payload["remediation"])

    def test_service_allows_an_ungated_path(self) -> None:
        payload = self.service().gate_check_write("views/map/overview.md")
        self.assertTrue(payload["allowed"])
        self.assertIsNone(payload["reason"])

    def test_service_forwards_the_agent(self) -> None:
        self.write_policy({"gate": {"deny_prefixes": ["views/"]}}, agent="codex")
        service = self.service()
        self.assertTrue(service.gate_check_write("wiki/index.md", agent="codex")["allowed"])
        self.assertFalse(service.gate_check_write("wiki/index.md")["allowed"])

    def test_service_matches_the_module_function(self) -> None:
        target = os.path.join("wiki", "entities", "page.md")
        self.assertEqual(
            self.service().gate_check_write(target),
            check_write(self.root, target).to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
