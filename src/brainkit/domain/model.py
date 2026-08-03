from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, NoReturn

from jsonschema import FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry
from referencing.exceptions import (
    NoSuchResource,
    Unresolvable,
)

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


class BrainkitError(Exception):
    """Base error with a stable machine-facing code."""

    code = "brainkit_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class ValidationError(BrainkitError):
    code = "validation_error"


class NotFoundError(BrainkitError):
    code = "not_found"


class PolicyError(BrainkitError):
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
    #: from the vault for a `.git` directory, since a vault at `repo/docs/brain`
    #: wants repo-relative paths and nothing else in the layout says so.
    code_root: str | None = None
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


@lru_cache(maxsize=32)
def _compiled_validator(canonical: str) -> Any:
    """Compile a validator once per distinct schema.

    `lint` validates every wiki page against the same `.brain/schema.json`, and
    compiling it per page made the cost of a lint scale with the page count for
    no reason. Keyed on the canonical serialization rather than object identity
    because the vault re-reads and re-parses the schema from disk, so the same
    schema is a different object on every call.

    A schema that fails `check_schema` raises here on every call: `lru_cache`
    does not memoize exceptions, which is what we want — an invalid schema must
    keep being reported, not be reported once and then silently pass.
    """

    schema = json.loads(canonical)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(
        schema,
        format_checker=FormatChecker(),
        registry=Registry(retrieve=_deny_remote_schema),  # type: ignore[call-arg]
    )


def _validator_for_schema(schema: dict[str, Any]) -> Any:
    try:
        canonical = json.dumps(schema, sort_keys=True)
    except (TypeError, ValueError):
        # Not serializable, so not cacheable. Compiling directly keeps the
        # caller's behaviour identical rather than turning a validation
        # question into a caching error.
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        return validator_class(
            schema,
            format_checker=FormatChecker(),
            registry=Registry(retrieve=_deny_remote_schema),  # type: ignore[call-arg]
        )
    return _compiled_validator(canonical)


def validate_schema(
    value: Any, schema: dict[str, Any], path: str = "$"
) -> list[dict[str, str]]:
    """Validate a value against its declared JSON Schema draft and formats."""

    try:
        validator = _validator_for_schema(schema)
        errors = sorted(
            validator.iter_errors(value),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                str(error.validator),
                error.message,
            ),
        )
    except SchemaError as exc:
        return [
            {
                "path": path,
                "code": "schema.invalid",
                "message": exc.message,
            }
        ]
    except Unresolvable as exc:
        return [
            {
                "path": path,
                "code": "schema.unresolvable_ref",
                "message": str(exc),
            }
        ]
    return [
        {
            "path": _schema_error_path(path, list(error.absolute_path)),
            "code": f"schema.{error.validator or 'invalid'}",
            "message": error.message,
        }
        for error in errors
    ]


def _deny_remote_schema(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]


def _schema_error_path(root: str, parts: list[Any]) -> str:
    result = root
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(part)):
            result += f".{part}"
        else:
            escaped = str(part).replace("\\", "\\\\").replace('"', '\\"')
            result += f'["{escaped}"]'
    return result
