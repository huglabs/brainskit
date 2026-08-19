"""The pure privacy rules: consumer lattice, strictest fold, branch policy.

Everything here is a function of its arguments -- no vault, no I/O, no clock.
The application layer's `PrivacyBoundary` binds these rules to a snapshot of
one vault; the judgment router in infrastructure imports them directly, which
is legal (infrastructure -> domain) and is what retired its hand-rolled copy.
Standard library only, and `tests/test_layering.py` enforces that mechanically.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from brainskit.domain.model import (
    PolicyError,
    PrivacyMode,
    SourceRecord,
    ValidationError,
)


class Consumer(StrEnum):
    """A named reader of the vault, parsed once at the boundary."""

    HUMAN = "human"
    LOCAL = "local"
    CLOUD = "cloud"

    @classmethod
    def parse(cls, value: str | Consumer) -> Consumer:
        """The one place an unknown consumer becomes a `ValidationError`."""

        if isinstance(value, Consumer):
            return value
        try:
            return cls(value)
        except ValueError:
            raise ValidationError(
                "Consumer must be human, local, or cloud",
                details={"consumer": value},
            ) from None

    def allows(self, privacy: PrivacyMode) -> bool:
        """Whether this consumer may see material under `privacy`."""

        if self is Consumer.HUMAN:
            return True
        if self is Consumer.LOCAL:
            return privacy != PrivacyMode.NEVER_INGEST
        return privacy == PrivacyMode.CLOUD


def strictest_privacy(
    modes: Iterable[PrivacyMode], *, on_empty: PrivacyMode
) -> PrivacyMode:
    """The most restrictive policy in `modes`.

    Named and shared rather than repeated: derived evidence and model-inferred
    enrichment both answer "what may this be shown to" by taking the strictest
    policy across everything that contributed, which is the same rule the
    judgment router applies when evidence spans branches. Two copies of a
    privacy rule is one copy too many -- the second is where they drift.

    `on_empty` is required, and that is the whole point. This function used to
    default an empty `modes` to `CLOUD`, justified by a docstring asserting that
    *every* caller checks provenance resolves first. `_evidence_privacy` was a
    caller that did not, so forgetting a `never-ingest` source declassified every
    page built from it. An invariant that is asserted rather than enforced is
    documentation of a bug that has not happened yet; making the answer a
    required argument means the next caller cannot omit the decision by
    accident.
    """

    collected = set(modes)
    if not collected:
        return on_empty
    if PrivacyMode.NEVER_INGEST in collected:
        return PrivacyMode.NEVER_INGEST
    if PrivacyMode.LOCAL_ONLY in collected:
        return PrivacyMode.LOCAL_ONLY
    return PrivacyMode.CLOUD


def resolve_branch_policy(config: Any, branch: str) -> Any:
    """The policy governing `branch`, or a refusal naming it.

    This was `config.branches[branch]`, a direct subscript, where
    `infrastructure/llm.py` already used `.get()` and raised `PolicyError` for
    the identical question. A file dropped into a directory that is not a
    configured branch -- then registered by the *documented* `bk reconcile` --
    made `search`, `browse_sources`, `graph_data` and `export` raise a bare
    `KeyError`. Not being a `BrainskitError`, it bypassed the JSON envelope and
    the exit codes: an unhandled traceback on the CLI, a 500 in the viewer.
    """

    if branch == "_inbox":
        return config.inbox_policy
    policy = config.branches.get(branch)
    if policy is None:
        raise PolicyError(
            "No privacy policy exists for this branch",
            details={
                "branch": branch,
                "configured": sorted(config.branches),
                "hint": (
                    "Move the source into a configured branch with bk file, "
                    "or add the branch to the vault policy"
                ),
            },
        )
    return policy


def branch_privacy(config: Any, branch: str) -> PrivacyMode:
    return PrivacyMode(resolve_branch_policy(config, branch).privacy)


def record_branch(record: SourceRecord) -> str:
    parts = PurePosixPath(record.path).parts
    if len(parts) < 2 or parts[0] != "raw":
        raise ValidationError("Source is outside raw/", details={"path": record.path})
    return parts[1]


def view_branch(record: SourceRecord) -> str:
    parts = PurePosixPath(record.path).parts
    return parts[1] if len(parts) > 1 else "unknown"


def context_branches(context: dict[str, Any]) -> list[str]:
    branches = sorted(
        {
            branch
            for evidence in context.get("evidence", [])
            for branch in evidence.get("branches", [])
        }
    )
    return branches or ["_inbox"]
