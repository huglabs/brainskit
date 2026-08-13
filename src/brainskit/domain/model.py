from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
CITATION_RE = re.compile(r"\[\^source:([0-9a-f]{64})\]")

#: A claim about code, pinned to the exact bytes it was true of.
#:
#: The hash is a SHA-256 over the file's contents, which is the same identity
#: `capture` gives a raw source — so the freshness rule the vault already runs
#: applies unchanged: the file changes, the hash stops matching, and every page
#: citing it goes to `review`. That is the whole reason to spell a code citation
#: as a hash rather than as a path and a line number, which would silently point
#: somewhere else after the next commit.
#:
#: The optional `#L17` or `#L17-L42` narrows the claim to a line range. It is
#: deliberately outside the captured group: it locates the reader, it does not
#: identify the source, and two claims about different lines of one file are two
#: claims about the same source.
CODE_CITATION_RE = re.compile(r"\[\^code:([0-9a-f]{64})(?:#L\d+(?:-L?\d+)?)?\]")

INTEGRATION_NAMES = {"obsidian", "neo4j", "postgres", "web"}


class BrainskitError(Exception):
    """Base error with a stable machine-facing code."""

    code = "brainskit_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class ValidationError(BrainskitError):
    """The request itself is wrong. Fix it and send it again.

    The four below narrow that answer without changing it. Each is a
    `ValidationError`, so every `except ValidationError` still catches it and
    every exit code stays what it was; only `code` sharpens. That matters
    because a caller reading one code for every refusal has to parse English to
    decide what to do next, and the right next move differs completely: retry
    the same request, change the request, change the machine, or give up.
    """

    code = "validation_error"


class ConflictError(ValidationError):
    """The request was well-formed, and the vault has moved since it was built.

    A stale `base_hash`, a `proposal_id` reused for a different payload, a
    proposal already decided. The distinguishing property is that the *same*
    intent can still succeed: re-read the current state, rebuild the request
    against it, send it again. This is the one an agent hits most, and the one
    it most needs to tell apart from "your input was malformed".
    """

    code = "conflict"


class NotConfiguredError(ValidationError):
    """Nothing is wrong with the request; this installation cannot serve it.

    No model mapped to the job, an optional dependency absent, an environment
    variable unset, an integration switched off. Retrying is pointless and
    rephrasing is pointless -- someone has to change the configuration or the
    machine. Kept apart from `validation_error` so an agent stops retrying and
    says what the operator has to do.
    """

    code = "not_configured"


class RefusalError(ValidationError):
    """A well-formed request the situation forbids, not a malformed one.

    Nesting a vault inside another, scanning a tree past its bound, stopping a
    process this vault does not own. Changing the request does not help; the
    circumstance has to change, and several of these name the escape hatch
    (`--force`, a narrower scope) in their message.
    """

    code = "refused"


class ModelResponseError(ValidationError):
    """A provider's *output* failed validation, not anything the caller sent.

    Invalid JSON, a truncated completion, an empty body, a payload that never
    satisfied the schema across the repair loop's attempts. Reported apart from
    the caller's own mistakes because the remedy is unrelated to the request:
    retry, raise the token ceiling, or route the job to another model.
    """

    code = "model_response_invalid"


class NotFoundError(BrainskitError):
    code = "not_found"


class PolicyError(BrainskitError):
    code = "policy_denied"


class PrivacyMode(StrEnum):
    CLOUD = "cloud"
    LOCAL_ONLY = "local-only"
    NEVER_INGEST = "never-ingest"


class FilingMode(StrEnum):
    AUTO_REVIEW = "auto+digest-review"
    APPROVE_EACH = "approve-each"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"


class PageKind(StrEnum):
    SOURCE = "source"
    ENTITY = "entity"
    CONCEPT = "concept"
    SYNTHESIS = "synthesis"

    @property
    def directory(self) -> str:
        return PAGE_DIRECTORIES[self]


#: Wiki directory per page kind. Never derive these by appending "s": English
#: plurals are irregular ("synthesis" -> "syntheses", "entity" -> "entities")
#: and a naive rule silently files pages into a directory the vault scaffold
#: does not create. Adding a PageKind member without an entry here fails at
#: import time (see the completeness guard below).
PAGE_DIRECTORIES: dict[PageKind, str] = {
    PageKind.SOURCE: "sources",
    PageKind.ENTITY: "entities",
    PageKind.CONCEPT: "concepts",
    PageKind.SYNTHESIS: "syntheses",
}

WIKI_DIRECTORIES: tuple[str, ...] = tuple(
    PAGE_DIRECTORIES[kind] for kind in PageKind
)

#: Directories produced by the historical `f"{kind.value}s"` rule that never
#: matched the scaffold, mapped to the directory that owns those pages now.
LEGACY_WIKI_DIRECTORIES: dict[str, str] = {
    f"{kind.value}s": PAGE_DIRECTORIES[kind]
    for kind in PageKind
    if f"{kind.value}s" != PAGE_DIRECTORIES[kind]
}


def _guard_page_directories() -> None:
    """Fail loudly if page kinds and wiki directories ever drift apart."""

    missing = sorted(kind.value for kind in PageKind if kind not in PAGE_DIRECTORIES)
    if missing:
        raise RuntimeError(f"PageKind members without a wiki directory: {missing}")
    if len(set(WIKI_DIRECTORIES)) != len(WIKI_DIRECTORIES):
        raise RuntimeError("Two page kinds share one wiki directory")
    collisions = sorted(set(LEGACY_WIKI_DIRECTORIES) & set(WIKI_DIRECTORIES))
    if collisions:
        raise RuntimeError(f"Legacy wiki directories shadow live ones: {collisions}")


_guard_page_directories()


@dataclass(frozen=True, slots=True)
class ContentHash:
    value: str

    def __post_init__(self) -> None:
        if not HASH_RE.fullmatch(self.value):
            raise ValidationError("Content hash must be a lowercase SHA-256 value")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CodeSource:
    """A file outside the vault that a page cites, and its hash at the time.

    Unlike a raw source, the bytes are not copied in — a repository is not
    evidence to be preserved, it is a moving thing to be pointed at. What makes
    the pointer trustworthy is the hash: `lint` re-reads the file and compares,
    so "this page describes code that has since changed" is a fact the vault can
    state rather than a thing the reader has to remember to check.
    """

    path: str
    content_hash: str

    def __post_init__(self) -> None:
        ContentHash(self.content_hash)
        if not self.path.strip():
            raise ValidationError("Code source path cannot be empty")
        candidate = PurePosixPath(self.path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError(
                "Code source paths must be relative to the code root and may not "
                "escape it",
                details={"path": self.path},
            )

    @classmethod
    def from_dict(cls, raw: Any) -> CodeSource:
        if not isinstance(raw, dict):
            raise ValidationError(
                "Each code source must be an object with `path` and `hash`",
                details={"value": raw},
            )
        missing = sorted({"path", "hash"} - raw.keys())
        if missing:
            raise ValidationError(
                "Code source is missing required fields", details={"missing": missing}
            )
        return cls(path=str(raw["path"]), content_hash=str(raw["hash"]))

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "hash": self.content_hash}


#: See `VaultConfig.code_scan_limit`. Chosen above the largest repository
#: anyone has pointed brainskit at and far below the 55,295-file accident that
#: motivated it, so it refuses the mistake without ever refusing the work.
DEFAULT_CODE_SCAN_LIMIT = 20_000


def _code_scan_limit(raw: dict[str, Any]) -> int:
    if "code_scan_limit" not in raw or raw["code_scan_limit"] is None:
        return DEFAULT_CODE_SCAN_LIMIT
    value = raw["code_scan_limit"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("code_scan_limit must be an integer")
    if value < 1:
        raise ValidationError(
            "code_scan_limit must be positive",
            details={"code_scan_limit": value, "hint": "Omit the key for the default"},
        )
    return int(value)


@dataclass(frozen=True, slots=True)
class GrammarNeed:
    """One tree-sitter grammar a scan would use, and whether it is installed.

    Exists because the extractor answers this question only by *printing*: a
    file whose grammar is absent yields `{"nodes": [], "error": "… not
    installed"}`, the extractor counts those into a stderr warning, and the
    counts never leave the function. So `bk code build` could report success,
    write a two-node graph for a four-file repository, and `bk code status`
    could then call that graph `fresh`.
    """

    module: str
    distribution: str
    installed: bool
    extensions: tuple[str, ...] = ()
    files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "distribution": self.distribution,
            "installed": self.installed,
            "extensions": list(self.extensions),
            "files": self.files,
        }


@dataclass(frozen=True, slots=True)
class ScanSurvey:
    """What a scan of `root` would cover, measured before committing to it."""

    root: str
    files: int
    grammars: tuple[GrammarNeed, ...] = ()
    #: Files with a known extension that exist on disk under `root`, counted by
    #: a plain walk that honours no ignore rules. `files` is what the extractor
    #: agreed to collect. The two diverging is the only evidence that a scan
    #: root is being *ignored* rather than being empty.
    present: int = 0
    #: Files the extractor will decline on purpose (data-shaped JSON). A third
    #: outcome, and the shortfall check must not read it as a loss.
    skipped: int = 0

    @property
    def missing(self) -> tuple[GrammarNeed, ...]:
        return tuple(g for g in self.grammars if not g.installed)

    def _grouped_by_extensions(self) -> dict[tuple[str, ...], list[GrammarNeed]]:
        """`self.grammars`, grouped by the exact extension set each needs.

        One extractor function dispatched across several extensions can need
        more than one grammar -- a TypeScript extractor covering `.js`,
        `.mjs`, `.ts` and `.tsx` through a single function reports that same
        639-file count under *both* `tree_sitter_javascript` and
        `tree_sitter_typescript`, because that function's bytecode names
        both (`survey()` derives each `GrammarNeed` from the same
        per-extension walk, so two entries sharing an identical `extensions`
        tuple are, by construction, the same files). Grouping first, rather
        than summing `.files` per grammar, is what keeps such a file counted
        once instead of once per grammar it happens to need.
        """

        groups: dict[tuple[str, ...], list[GrammarNeed]] = {}
        for need in self.grammars:
            groups.setdefault(need.extensions, []).append(need)
        return groups

    @property
    def unreachable_files(self) -> int:
        """Files whose language is supported but whose grammar is absent."""

        return sum(
            needs[0].files
            for needs in self._grouped_by_extensions().values()
            if any(not need.installed for need in needs)
        )

    @property
    def parseable_files(self) -> int:
        """Files an extractor claims *and* whose every needed grammar is installed here."""

        total = sum(
            needs[0].files
            for needs in self._grouped_by_extensions().values()
            if all(need.installed for need in needs)
        )
        return max(total - self.skipped, 0)

    @property
    def ignored_entirely(self) -> bool:
        """The extractor collected nothing from a root that is not empty."""

        return self.files == 0 and self.present > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": self.files,
            "grammars_missing": [g.to_dict() for g in self.missing],
            "unreachable_files": self.unreachable_files,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentEdge:
    """A relationship a model argued for, kept apart from the derived graph.

    The vault's own graph is a *projection*: `sourced_from` comes from a page's
    frontmatter, `links_to` from its `[[wiki-links]]`, and `bk graph` rewrites
    the whole file from those every time. An edge written into it would be
    destroyed by the next build — and, worse, would be unclassifiable: the
    privacy filter decides by the branch a *source record* lives in, so an edge
    with nothing behind it gives the filter nothing to act on, in a graph where
    filtering deliberately runs after expansion so an edge cannot pull a
    restricted node back into view.

    `derived_from` is what makes an inferred edge admissible at all. It names
    the sources the claim was made from, so the edge inherits their branches
    and therefore their privacy — the strictest of them, the same rule the
    judgment router already applies to evidence spanning several branches.

    Identity is the triple, not a random id: proposing the same relationship
    twice is one edge, so an agent that re-runs does not inflate the graph.
    """

    source: str
    target: str
    relation: str
    #: Content hashes of the evidence the claim was drawn from. Never empty —
    #: `Enrichment.apply` rejects an edge that cannot name its sources.
    derived_from: tuple[str, ...]
    model: str
    created_at: str
    note: str = ""

    @property
    def id(self) -> str:
        digest = hashlib.sha256(
            "\u0000".join((self.source, self.relation, self.target)).encode("utf-8")
        )
        return digest.hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "derived_from": list(self.derived_from),
            "model": self.model,
            "created_at": self.created_at,
            #: Stamped on the way out, never inferred by a reader.
            "provenance": "model",
            **({"note": self.note} if self.note else {}),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> EnrichmentEdge:
        if not isinstance(raw, dict):
            raise ValidationError("An enrichment edge must be an object")
        missing = sorted(
            {"source", "target", "relation", "derived_from", "model"} - raw.keys()
        )
        if missing:
            raise ValidationError(
                "Enrichment edge is incomplete", details={"missing": missing}
            )
        sources = raw["derived_from"]
        if not isinstance(sources, list) or not sources:
            raise ValidationError(
                "An enrichment edge must name the sources it was derived from",
                details={"hint": "Without them the privacy boundary cannot be decided"},
            )
        source, target = str(raw["source"]).strip(), str(raw["target"]).strip()
        relation = str(raw["relation"]).strip()
        if not source or not target or not relation:
            raise ValidationError("source, target and relation cannot be empty")
        if source == target:
            raise ValidationError(
                "An enrichment edge cannot point a node at itself",
                details={"node": source},
            )
        return cls(
            source=source,
            target=target,
            relation=relation,
            derived_from=tuple(str(item) for item in sources),
            model=str(raw["model"]).strip() or "unknown",
            created_at=str(raw.get("created_at") or utc_now()),
            note=str(raw.get("note") or ""),
        )


def _code_root(raw: dict[str, Any]) -> str | None:
    """Read an explicit code root, distinguishing absent from empty.

    Absent means "discover it". An explicitly empty string means "the vault
    root", which is the honest way to say a vault that sits at the top of the
    repository it documents — the same absent-versus-empty rule the ignore
    patterns follow.
    """

    if "code_root" not in raw:
        return None
    value = raw["code_root"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("code_root must be a string path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        raise ValidationError(
            "code_root must be relative to the vault", details={"code_root": value}
        )
    return value


def _branch_policy(raw: Any, *, source: str) -> BranchPolicy:
    """Attach the offending branch to a rejected policy."""
    try:
        return BranchPolicy.from_dict(raw)
    except ValidationError as exc:
        raise ValidationError(
            str(exc), details={"branch": source, **exc.details}
        ) from exc


def _policy_field_details(
    field: str, raw: dict[str, Any], choices: type[StrEnum]
) -> dict[str, Any]:
    """Name the offending field so a rejected policy is actionable."""
    details: dict[str, Any] = {
        "field": field,
        "choices": [member.value for member in choices],
    }
    if field in raw:
        details["value"] = raw[field]
    else:
        details["missing"] = True
    return details


@dataclass(frozen=True, slots=True)
class BranchPolicy:
    privacy: PrivacyMode
    filing: FilingMode

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BranchPolicy:
        if not isinstance(raw, dict):
            raise ValidationError(
                "Each branch requires valid privacy and filing policies",
                details={"expected": "object", "observed": type(raw).__name__},
            )
        try:
            privacy = PrivacyMode(raw["privacy"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationError(
                "Each branch requires valid privacy and filing policies",
                details=_policy_field_details("privacy", raw, PrivacyMode),
            ) from exc
        try:
            filing = FilingMode(raw["filing"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationError(
                "Each branch requires valid privacy and filing policies",
                details=_policy_field_details("filing", raw, FilingMode),
            ) from exc
        return cls(privacy=privacy, filing=filing)


@dataclass(frozen=True, slots=True)
class NoveltyPolicy:
    duplicate_similarity_threshold: float
    min_new_token_ratio: float
    stale_after_days: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NoveltyPolicy:
        try:
            policy = cls(
                duplicate_similarity_threshold=float(
                    raw["duplicate_similarity_threshold"]
                ),
                min_new_token_ratio=float(raw["min_new_token_ratio"]),
                stale_after_days=int(raw["stale_after_days"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("Novelty policy is incomplete") from exc
        if not 0 <= policy.duplicate_similarity_threshold <= 1:
            raise ValidationError(
                "duplicate_similarity_threshold must be between 0 and 1"
            )
        if not 0 <= policy.min_new_token_ratio <= 1:
            raise ValidationError("min_new_token_ratio must be between 0 and 1")
        if policy.stale_after_days < 1:
            raise ValidationError("stale_after_days must be positive")
        return policy

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicate_similarity_threshold": self.duplicate_similarity_threshold,
            "min_new_token_ratio": self.min_new_token_ratio,
            "stale_after_days": self.stale_after_days,
        }


@dataclass(frozen=True, slots=True)
class IntegrationPolicy:
    enabled: bool
    managed: bool
    options: dict[str, Any]

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> IntegrationPolicy:
        if name not in INTEGRATION_NAMES:
            raise ValidationError(
                "Unknown integration", details={"integration": name}
            )
        if not isinstance(raw, dict):
            raise ValidationError(
                "Integration policy must be an object",
                details={"integration": name},
            )
        enabled = raw.get("enabled")
        managed = raw.get("managed", False)
        options = raw.get("options", {})
        if not isinstance(enabled, bool) or not isinstance(managed, bool):
            raise ValidationError(
                "Integration enabled and managed flags must be booleans",
                details={"integration": name},
            )
        if not isinstance(options, dict):
            raise ValidationError(
                "Integration options must be an object",
                details={"integration": name},
            )
        return cls(enabled=enabled, managed=managed, options=dict(options))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "managed": self.managed,
            "options": self.options,
        }


#: Patterns excluded from a watched source folder unless the vault overrides
#: `ignore`. A watch walks a folder recursively and captures every file it
#: finds, and capture is irreversible by design: a source is identified by the
#: hash of its bytes and `raw/` is immutable. So the cost of a missing default
#: is not noise, it is a vault permanently holding a dependency tree.
#:
#: These are the directories that are large, machine-generated and reproducible
#: — never evidence — plus the editor and OS droppings that accompany them.
DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".gradle",
    ".terraform",
    "target",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".cache",
    ".idea",
    ".vscode",
    ".DS_Store",
    "Thumbs.db",
    "*.pyc",
    "*.pyo",
    "*.so",
    "*.o",
    "*.class",
    "*.lock",
    "*.log",
    "*.tmp",
    "*.swp",
)


def _ignore_patterns(raw: dict[str, Any]) -> tuple[str, ...]:
    """Read the vault's ignore list, distinguishing absent from empty.

    An older vault has no `ignore` key at all and must inherit the defaults, or
    upgrading the engine would start capturing dependency trees it never did.
    A vault that stores `[]` has answered the question, and gets what it asked
    for.
    """

    if "ignore" not in raw:
        return DEFAULT_IGNORE_PATTERNS
    value = raw["ignore"]
    if not isinstance(value, list):
        raise ValidationError(
            "ignore must be a list of glob patterns",
            details={"observed": type(value).__name__},
        )
    patterns: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValidationError(
                "Each ignore pattern must be a string",
                details={"observed": type(entry).__name__},
            )
        candidate = entry.strip().strip("/")
        if candidate and candidate not in patterns:
            patterns.append(candidate)
    return tuple(patterns)


def is_ignored(relative_path: str, patterns: Sequence[str]) -> bool:
    """True when any segment of ``relative_path`` matches an ignore pattern.

    Matching is per segment, the way a person means it: `node_modules` excludes
    the whole tree beneath it rather than only a file with that exact name, and
    one pattern therefore prunes a directory instead of being re-tested against
    every file inside it. A pattern containing a separator is matched against
    the whole relative path instead, so `docs/build` can be excluded without
    also excluding every other `build`.

    Patterns are shell globs, matched case-insensitively — the primary
    deployment target is a case-insensitive filesystem, where `Node_Modules`
    and `node_modules` name the same directory.
    """

    if not patterns:
        return False
    normalized = PurePosixPath(str(relative_path).replace("\\", "/"))
    segments = [part for part in normalized.parts if part not in ("", ".", "/")]
    if not segments:
        return False
    whole = "/".join(segments).casefold()
    for pattern in patterns:
        candidate = str(pattern).strip().strip("/")
        if not candidate:
            continue
        folded = candidate.casefold()
        if "/" in folded:
            # A rooted pattern prunes a tree, so it has to match the directory
            # itself *and* everything under it. Matching only the exact path
            # would exclude `docs/build` while still capturing every file in it.
            if fnmatch(whole, folded) or fnmatch(whole, f"{folded}/*"):
                return True
            continue
        if any(fnmatch(segment.casefold(), folded) for segment in segments):
            return True
    return False


@dataclass(frozen=True, slots=True)
class VaultConfig:
    wiki_language: str
    inbox_policy: BranchPolicy
    branches: dict[str, BranchPolicy]
    providers: dict[str, dict[str, Any]]
    job_models: dict[str, dict[str, Any]]
    sources: list[str]
    schedule: dict[str, str]
    taxonomy_seed: list[str]
    novelty: NoveltyPolicy
    integrations: dict[str, IntegrationPolicy]
    #: Absent means "use the defaults", which is what an existing vault wants.
    #: An explicitly empty list is honoured as a deliberate "ignore nothing".
    ignore: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    #: Where `code_sources` paths resolve. Absent means "discover it": walk up
    #: from the vault for a `.git` directory, since a vault at `repo/.brainskit`
    #: wants repo-relative paths and nothing else in the layout says so.
    code_root: str | None = None
    #: Most files `bk code build` will scan before refusing. Not a performance
    #: knob -- a backstop against the code root resolving to something far
    #: larger than a project, which is how one vault came to index 55,295 files
    #: from every unrelated repository on the machine.
    code_scan_limit: int = DEFAULT_CODE_SCAN_LIMIT
    version: int = 3

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VaultConfig:
        version = int(raw.get("version", 2))
        required = {
            "wiki_language",
            "inbox_policy",
            "branches",
            "providers",
            "job_models",
            "sources",
            "schedule",
            "taxonomy_seed",
            "novelty",
        }
        if version >= 3:
            required.add("integrations")
        missing = sorted(required - raw.keys())
        if missing:
            raise ValidationError(
                "Vault policy is incomplete", details={"missing": missing}
            )
        language = str(raw["wiki_language"]).strip()
        if not language:
            raise ValidationError("wiki_language cannot be empty")
        if not isinstance(raw["branches"], dict) or not raw["branches"]:
            raise ValidationError("At least one configured branch is required")
        branches: dict[str, BranchPolicy] = {}
        for name, policy in raw["branches"].items():
            normalized = normalize_branch(name)
            branches[normalized] = _branch_policy(policy, source=normalized)
        for collection in ("sources", "taxonomy_seed"):
            if not isinstance(raw[collection], list):
                raise ValidationError(f"{collection} must be a list")
        if not isinstance(raw["schedule"], dict):
            raise ValidationError("schedule must be an object")
        if not isinstance(raw["providers"], dict):
            raise ValidationError("providers must be an object")
        if not isinstance(raw["job_models"], dict):
            raise ValidationError("job_models must be an object")
        raw_integrations = raw.get(
            "integrations",
            {
                name: {"enabled": False, "managed": False, "options": {}}
                for name in sorted(INTEGRATION_NAMES)
            },
        )
        if not isinstance(raw_integrations, dict):
            raise ValidationError("integrations must be an object")
        missing_integrations = sorted(INTEGRATION_NAMES - raw_integrations.keys())
        unknown_integrations = sorted(raw_integrations.keys() - INTEGRATION_NAMES)
        if missing_integrations or unknown_integrations:
            raise ValidationError(
                "Integration policy set is incomplete",
                details={
                    "missing": missing_integrations,
                    "unknown": unknown_integrations,
                },
            )
        return cls(
            wiki_language=language,
            inbox_policy=_branch_policy(raw["inbox_policy"], source="inbox_policy"),
            branches=branches,
            providers=dict(raw["providers"]),
            job_models=dict(raw["job_models"]),
            sources=[str(item) for item in raw["sources"]],
            schedule={str(k): str(v) for k, v in raw["schedule"].items()},
            taxonomy_seed=[str(item) for item in raw["taxonomy_seed"]],
            novelty=NoveltyPolicy.from_dict(raw["novelty"]),
            ignore=_ignore_patterns(raw),
            code_root=_code_root(raw),
            code_scan_limit=_code_scan_limit(raw),
            integrations={
                name: IntegrationPolicy.from_dict(name, value)
                for name, value in raw_integrations.items()
            },
            version=version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "wiki_language": self.wiki_language,
            "inbox_policy": {
                "privacy": self.inbox_policy.privacy.value,
                "filing": self.inbox_policy.filing.value,
            },
            "branches": {
                name: {
                    "privacy": policy.privacy.value,
                    "filing": policy.filing.value,
                }
                for name, policy in self.branches.items()
            },
            "providers": self.providers,
            "job_models": self.job_models,
            "sources": self.sources,
            "schedule": self.schedule,
            "taxonomy_seed": self.taxonomy_seed,
            "novelty": self.novelty.to_dict(),
            "ignore": list(self.ignore),
            **({"code_root": self.code_root} if self.code_root is not None else {}),
            **(
                {"code_scan_limit": self.code_scan_limit}
                if self.code_scan_limit != DEFAULT_CODE_SCAN_LIMIT
                else {}
            ),
            "integrations": {
                name: policy.to_dict()
                for name, policy in sorted(self.integrations.items())
            },
        }


@dataclass(slots=True)
class SourceRecord:
    content_hash: str
    path: str
    original_name: str
    media_type: str
    size: int
    captured_at: str
    status: str = "pending"

    def __post_init__(self) -> None:
        ContentHash(self.content_hash)
        if PurePosixPath(self.path).is_absolute() or ".." in PurePosixPath(
            self.path
        ).parts:
            raise ValidationError("Source registry paths must be vault-relative")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SourceRecord:
        return cls(
            content_hash=str(raw["content_hash"]),
            path=str(raw["path"]),
            original_name=str(raw["original_name"]),
            media_type=str(raw["media_type"]),
            size=int(raw["size"]),
            captured_at=str(raw["captured_at"]),
            status=str(raw.get("status", "pending")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "path": self.path,
            "original_name": self.original_name,
            "media_type": self.media_type,
            "size": self.size,
            "captured_at": self.captured_at,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class PageOperation:
    action: str
    kind: PageKind
    slug: str
    title: str
    aliases: tuple[str, ...]
    source_hashes: tuple[str, ...]
    body: str
    links: tuple[str, ...]
    metadata: dict[str, Any]
    base_hash: str | None
    #: Defaulted, so every existing caller and stored proposal stays valid.
    code_sources: tuple[CodeSource, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PageOperation:
        try:
            operation = cls(
                action=str(raw["action"]),
                kind=PageKind(str(raw["kind"])),
                slug=str(raw["slug"]),
                title=str(raw["title"]).strip(),
                aliases=tuple(str(v) for v in raw.get("aliases", [])),
                source_hashes=tuple(str(v) for v in raw.get("source_hashes", [])),
                body=str(raw["body"]).strip(),
                links=tuple(str(v) for v in raw.get("links", [])),
                metadata=dict(raw.get("metadata", {})),
                base_hash=(
                    str(raw["base_hash"]) if raw.get("base_hash") is not None else None
                ),
                code_sources=tuple(
                    CodeSource.from_dict(entry)
                    for entry in raw.get("code_sources", [])
                ),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationError(
                "Invalid page operation", details={"operation": raw}
            ) from exc
        operation.validate()
        return operation

    def validate(self) -> None:
        if self.action != "upsert":
            raise ValidationError("Only the upsert action is supported")
        if not SLUG_RE.fullmatch(self.slug):
            raise ValidationError(
                "Page slug must be lowercase kebab-case", details={"slug": self.slug}
            )
        if not self.title:
            raise ValidationError("Page title cannot be empty")
        if not self.body:
            raise ValidationError("Page body cannot be empty")
        for value in self.source_hashes:
            ContentHash(value)
        if self.base_hash is not None:
            ContentHash(self.base_hash)
        if len(self.aliases) != len(set(self.aliases)):
            raise ValidationError("Page aliases must be unique")
        if len(self.source_hashes) != len(set(self.source_hashes)):
            raise ValidationError("Page source hashes must be unique")
        if len(self.links) != len(set(self.links)):
            raise ValidationError("Page links must be unique")
        code_paths = [entry.path for entry in self.code_sources]
        if len(code_paths) != len(set(code_paths)):
            raise ValidationError("Page code sources must be unique by path")
        reserved = {
            "id",
            "type",
            "title",
            "aliases",
            "sources",
            "code_sources",
            "updated_at",
        }
        if reserved & self.metadata.keys():
            raise ValidationError(
                "Custom metadata cannot override core frontmatter fields",
                details={"reserved": sorted(reserved & self.metadata.keys())},
            )
        declared = set(self.links)
        embedded = {match.strip() for match in WIKI_LINK_RE.findall(self.body)}
        if not embedded.issubset(declared):
            raise ValidationError(
                "Every embedded wiki link must be declared in links",
                details={"undeclared": sorted(embedded - declared)},
            )
        cited = set(CODE_CITATION_RE.findall(self.body))
        declared_code = {entry.content_hash for entry in self.code_sources}
        if not cited.issubset(declared_code):
            raise ValidationError(
                "Every code citation must be declared in code_sources",
                details={"undeclared": sorted(cited - declared_code)},
            )

    @property
    def relative_path(self) -> str:
        return f"wiki/{self.kind.directory}/{self.slug}.md"

    @property
    def page_id(self) -> str:
        return f"{self.kind.value}:{self.slug}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "kind": self.kind.value,
            "slug": self.slug,
            "title": self.title,
            "aliases": list(self.aliases),
            "source_hashes": list(self.source_hashes),
            "body": self.body,
            "links": list(self.links),
            "metadata": self.metadata,
            "base_hash": self.base_hash,
            "code_sources": [entry.to_dict() for entry in self.code_sources],
        }


@dataclass(frozen=True, slots=True)
class ApplyProposal:
    operations: tuple[PageOperation, ...]
    proposal_id: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ApplyProposal:
        operations = raw.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ValidationError("Proposal must contain at least one operation")
        parsed = tuple(PageOperation.from_dict(item) for item in operations)
        paths = [item.relative_path for item in parsed]
        if len(paths) != len(set(paths)):
            raise ValidationError("Proposal contains duplicate page targets")
        proposal_id = raw.get("proposal_id")
        if proposal_id is not None:
            proposal_id = str(proposal_id).strip()
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", proposal_id):
                raise ValidationError("proposal_id has an invalid format")
        return cls(parsed, proposal_id)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operations": [operation.to_dict() for operation in self.operations]
        }
        if self.proposal_id:
            payload["proposal_id"] = self.proposal_id
        return payload


@dataclass(frozen=True, slots=True)
class FilingProposal:
    proposal_id: str
    source_hash: str
    destination_branch: str
    filing_mode: FilingMode
    apply_proposal: dict[str, Any]
    reason: str
    status: ProposalStatus
    created_at: str
    decided_at: str | None = None
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        ContentHash(self.source_hash)
        normalize_branch(self.destination_branch)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.proposal_id):
            raise ValidationError("Filing proposal id has an invalid format")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FilingProposal:
        try:
            return cls(
                proposal_id=str(raw["proposal_id"]),
                source_hash=str(raw["source_hash"]),
                destination_branch=str(raw["destination_branch"]),
                filing_mode=FilingMode(raw["filing_mode"]),
                apply_proposal=dict(raw["apply_proposal"]),
                reason=str(raw.get("reason", "")),
                status=ProposalStatus(raw["status"]),
                created_at=str(raw["created_at"]),
                decided_at=(
                    str(raw["decided_at"]) if raw.get("decided_at") else None
                ),
                result=dict(raw["result"]) if raw.get("result") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("Stored filing proposal is invalid") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "source_hash": self.source_hash,
            "destination_branch": self.destination_branch,
            "filing_mode": self.filing_mode.value,
            "apply_proposal": self.apply_proposal,
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class LintFinding:
    code: str
    message: str
    severity: str = "error"
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    kind: str
    title: str
    excerpt: str
    score: float
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "title": self.title,
            "excerpt": self.excerpt,
            "score": self.score,
            "content_hash": self.content_hash,
        }


def normalize_branch(value: str) -> str:
    branch = str(value).strip().strip("/")
    path = PurePosixPath(branch)
    if (
        not branch
        or path.is_absolute()
        or ".." in path.parts
        or branch.startswith("_")
    ):
        raise ValidationError("Invalid raw branch", details={"branch": value})
    return branch


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


