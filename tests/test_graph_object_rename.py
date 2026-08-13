"""Graph objects carry the brainskit name, and a pre-rename store is migrated.

`docs/integrations.md` said `BrainskitNode` while the code wrote `BrainkitNode`,
and `CHANGELOG.md` recorded keeping the old names deliberately -- because a
plain rename creates a *second* set of objects beside the originals rather than
moving anything. That reasoning was right, which is why the rename ships with
the detection that makes it safe: a store still holding the old names is
refused, with the statement that moves them.

Deliberately *not* renamed: the PostgreSQL role, database and container names.
Those identify objects a server provisioned, not objects brainskit writes into
one, and renaming them would strand an existing deployment for no gain in
consistency that a user ever sees.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from brainskit.infrastructure import integrations
from brainskit.infrastructure.graph import MarkdownGraph

SOURCE = Path(__file__).resolve().parents[1] / "src" / "brainskit"


def _graph() -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "page:a.md", "label": "A", "kind": "concept", "path": "wiki/a.md"},
            {"id": "raw:abc", "label": "src", "kind": "raw", "path": "raw/x/a.md"},
        ],
        "edges": [
            {"source": "page:a.md", "target": "raw:abc", "type": "sourced_from"}
        ],
    }


class GraphObjectNamesTest(unittest.TestCase):
    def test_the_cypher_export_writes_the_brainskit_label(self) -> None:
        exported = MarkdownGraph().export(_graph(), "cypher")
        self.assertIn("BrainskitNode", exported)
        self.assertNotIn("BrainkitNode", exported)

    def test_no_shipped_module_still_writes_the_old_label(self) -> None:
        offenders = []
        for path in SOURCE.rglob("*.py"):
            if "codeanalysis" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            # The legacy constant and the migration statement name it on
            # purpose; a *query* that still writes it is the defect.
            for line in text.splitlines():
                if "BrainkitNode" not in line:
                    continue
                if "LEGACY_NEO4J_LABEL" in line or line.strip().startswith("#"):
                    continue
                offenders.append(f"{path.name}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_the_postgres_schema_defaults_to_brainskit(self) -> None:
        sources = (SOURCE / "infrastructure" / "integrations.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('options.get("schema", "brainskit")', sources)
        self.assertNotIn('options.get("schema", "brainkit")', sources)

    def test_provisioning_identities_are_deliberately_unchanged(self) -> None:
        """Control: the rename must not have swept role and database with it.

        Those name objects a server already provisioned. Renaming them would
        strand a running deployment, which is the failure the changelog entry
        was guarding against in the first place.
        """

        sources = (SOURCE / "infrastructure" / "integrations.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('options.get("user", "brainkit")', sources)
        self.assertIn('options.get("database", "brainkit")', sources)


class MigrationStatementsTest(unittest.TestCase):
    def test_the_neo4j_migration_moves_rather_than_copies(self) -> None:
        statement = integrations.NEO4J_MIGRATION
        self.assertIn("SET n:BrainskitNode", statement)
        self.assertIn("REMOVE n:BrainkitNode", statement)

    def test_the_postgres_migration_renames_the_schema(self) -> None:
        self.assertEqual(
            integrations.POSTGRES_MIGRATION,
            'ALTER SCHEMA "brainkit" RENAME TO "brainskit"',
        )

    def test_the_refusal_hands_back_the_statement_to_run(self) -> None:
        from brainskit.domain.model import NotConfiguredError

        with self.assertRaises(NotConfiguredError) as refused:
            integrations._refuse_unmigrated("neo4j", integrations.NEO4J_MIGRATION)
        self.assertEqual(
            refused.exception.details["migration"], integrations.NEO4J_MIGRATION
        )
        self.assertEqual(refused.exception.code, "not_configured")

    def test_the_documented_label_matches_the_one_the_code_writes(self) -> None:
        """The divergence that started this: docs said one thing, code another."""

        docs = (Path(__file__).resolve().parents[1] / "docs" / "integrations.md")
        text = docs.read_text(encoding="utf-8")
        documented = set(re.findall(r"Brains?kitNode", text))
        self.assertEqual(documented, {"BrainskitNode"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
