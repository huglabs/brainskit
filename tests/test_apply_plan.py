"""One argument object for an apply, and a port that cannot drift from its
adapter.

`VaultPort.commit_wiki_batch` declared its sixth parameter `freshness_state`.
`FileVault.commit_wiki_batch` implemented it as `freshness_updates`, with a
narrower type. Both spellings were live, one of them was wrong depending on
which file you read, and every test passed: the sole production caller passed
all eight arguments positionally, and a positional call cannot disagree about a
name. The drift is invisible to any test that only *calls* the thing, which is
why the checks here read declarations instead.

`ApplyPlan` removes the drift by construction -- there is one place to spell
each field -- and these tests keep the shape that guarantees it: the port takes
one argument, the adapter reads every field of it, and the gate names every
field when it builds one. The parameter-name comparison is generalised across
the ports because this defect is a property of the pattern, not of this method;
a second instance of it would otherwise ship exactly as quietly as the first.
"""

from __future__ import annotations

import inspect
import unittest
from dataclasses import fields

from brainskit.application import ports
from brainskit.application.compilation import ApplyGate
from brainskit.application.ports import ApplyPlan, VaultPort
from brainskit.infrastructure.apply_transaction import ApplyTransaction
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.integrations import NativeIntegrations
from brainskit.infrastructure.vault import FileVault

#: Every port whose adapter lives in this repository. `CodeExtractorPort`,
#: `JudgmentPort`, `JobSpecPort` and `SyncBoundaryPort` are left out because
#: their implementations are selected at the composition root or supplied by a
#: caller, so there is no single adapter to compare against.
IMPLEMENTED_PORTS: tuple[tuple[type, type], ...] = (
    (ports.VaultPort, FileVault),
    (ports.SearchIndexPort, SqliteFtsIndex),
    (ports.GraphPort, MarkdownGraph),
    (ports.IntegrationPort, NativeIntegrations),
)


def _parameter_names(function: object) -> list[str]:
    return list(inspect.signature(function).parameters)  # type: ignore[arg-type]


class ApplyPlanIsTheWholeArgumentTest(unittest.TestCase):
    def test_the_port_takes_one_argument(self) -> None:
        """Eight positional parameters are what made the drift unobservable."""

        self.assertEqual(
            _parameter_names(VaultPort.commit_wiki_batch), ["self", "plan"]
        )

    def test_the_adapter_reads_every_field_of_the_plan(self) -> None:
        # Two objects make up the adapter side now: `commit_wiki_batch` owns
        # the locks and `ApplyTransaction.commit` performs the write, so the
        # plan has to be read across the pair.
        source = inspect.getsource(FileVault.commit_wiki_batch) + inspect.getsource(
            ApplyTransaction.commit
        )
        unread = [
            field.name
            for field in fields(ApplyPlan)
            if f"plan.{field.name}" not in source
        ]
        self.assertEqual(
            unread, [], msg="ApplyPlan carries a field the adapter never reads"
        )

    def test_the_gate_names_every_field_when_it_builds_the_plan(self) -> None:
        source = inspect.getsource(ApplyGate.commit)
        unset = [
            field.name for field in fields(ApplyPlan) if f"{field.name}=" not in source
        ]
        self.assertEqual(
            unset, [], msg="the gate builds a plan without naming every field"
        )


class PortsAndAdaptersDeclareTheSameParametersTest(unittest.TestCase):
    def test_every_implemented_port_method_matches_its_adapter(self) -> None:
        drift: list[str] = []
        compared = 0
        for port, adapter in IMPLEMENTED_PORTS:
            for name, declared in sorted(vars(port).items()):
                if name.startswith("_") or not callable(declared):
                    continue
                implemented = getattr(adapter, name, None)
                if implemented is None:
                    drift.append(f"{adapter.__name__} does not implement {name}")
                    continue
                compared += 1
                expected = _parameter_names(declared)
                observed = _parameter_names(implemented)
                if expected != observed:
                    drift.append(
                        f"{port.__name__}.{name}: port {expected} "
                        f"vs {adapter.__name__} {observed}"
                    )
        self.assertEqual(
            drift, [], msg="a port and its adapter name a parameter differently"
        )
        # A comparison that reaches no method passes for the wrong reason.
        self.assertGreater(compared, 25, "implausibly few port methods compared")


if __name__ == "__main__":
    unittest.main()
