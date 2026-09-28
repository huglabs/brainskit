"""The ingestion path: what enters the vault, and what that entry disturbs.

Capture is the one irreversible write outside the apply gate -- a source is
identified by the hash of its bytes and `raw/` is immutable -- so everything
that decides *whether* bytes enter, and everything a capture then has to say
about the pages already here, belongs together rather than beside the facade
that calls it.

Three decisions live here, and each was a defect once:

**What a watch is allowed to walk.** Eligibility is a vault rule, not a
caller's: `ignore` prunes whole trees, a relative source resolves against the
vault rather than the process's current directory, and the vault's own
directory is never a source of itself. A configured source that resolves to
nothing is reported rather than passed over, because a watch that captures
nothing looks exactly like a watch with nothing to capture.

**What a new source is related to.** BM25 orders candidates but has no
absolute scale, so relatedness is a shared-vocabulary floor measured against
the page bodies -- not a rank, and not the name the file was saved under. The
tuning constants sit next to the one method that reads them.

**What a capture may do to a page it relates to.** It asks the ledger to mark
the page for review and nothing else. `mark_reviewed` carries the
never-downgrade rule (ADR 0002); a writer that reached past it and set the
status itself would park a stale page in `review` forever.

The ledger, the vault and the index all arrive as constructor parameters. This
module builds no siblings of its own: an earlier review found two modules each
constructing a partially-configured instance of another, and ADR 0002 exists
because a writer and a reader of the same file drifted apart.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from brainskit.application.freshness import FreshnessLedger
from brainskit.application.install import adapter_path, installed_agents
from brainskit.application.pages import (
    _content_tokens,
    _is_salient_term,
    _normalized_tokens,
    parse_frontmatter,
)
from brainskit.application.ports import SearchIndexPort, VaultPort
from brainskit.domain.model import (
    BrainskitError,
    ForgottenSourceError,
    NotConfiguredError,
    NotFoundError,
    PolicyError,
    SourceRecord,
    ValidationError,
    is_ignored,
    resolve_source_path,
)

# Relatedness budget: enough terms to describe a capture, few enough to keep the
# FTS5 MATCH bounded on the capture hot path.
_RELATED_QUERY_TERMS = 12
_RELATED_CANDIDATES = 20
_RELATED_PAGE_LIMIT = 5
_RELATED_MIN_SHARED_TERMS = 2
_RELATED_TEXT_LIMIT = 20_000

# What a confined capture -- one a model asked for, over MCP -- never copies in,
# wherever it sits (ADR 0010). Small on purpose: each entry is a file whose
# whole content is a credential, and the operator can still capture any of them
# with `bk capture`, which is the point. A model is the reader most likely to
# be told to fetch one by text it should not be obeying.
_SECRET_NAMES = frozenset({".netrc", ".npmrc", ".pypirc"})
_SECRET_SUFFIXES = frozenset({".pem", ".key"})
_ENV_TEMPLATES = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist"}
)
_PRIVATE_KEY = re.compile(r"id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?")
_SECRET_HOME_DIRECTORIES = (".ssh", ".aws", ".config/gcloud")
# Not a credential file, but git's own: `.git/config` is where a token pasted
# into a remote URL ends up, and nothing under `.git` is evidence.
_SECRET_DIRECTORY_NAMES = frozenset({".git"})


class Ingestion:
    """Capture a source, sweep the watched folders, and heal what that leaves."""

    def __init__(
        self,
        vault: VaultPort,
        index: SearchIndexPort,
        ledger: FreshnessLedger,
    ):
        self.vault = vault
        self.index = index
        self.ledger = ledger

    def capture(
        self,
        source: str | None,
        *,
        text: str | None = None,
        title: str | None = None,
        revive: bool = True,
        confined: bool = False,
    ) -> dict[str, Any]:
        """Capture text, a URL, or a file.

        `confined` is a model asking rather than the operator typing: a file
        path must then resolve inside the project and must not be a
        credential file. See `_confine`.
        """

        if text is not None:
            record, created = self.vault.capture_text(
                text, title or "captured-note", ".md"
            )
        elif source and _is_url(source):
            url_title = title or urlparse(source).netloc or "captured-link"
            body = f"# {url_title}\n\n{source}\n"
            record, created = self.vault.capture_text(body, url_title, ".md")
        elif source:
            path = self._confine(source) if confined else Path(source)
            record, created = self.vault.capture_file(path, revive=revive)
        else:
            raise ValidationError("capture requires a source path, URL, or --text")
        self.index.upsert_raw(self.vault, record)
        self._mark_related_pages_for_review(record)
        return {"created": created, "source": record.to_dict()}

    def _confine(self, source: str) -> Path:
        """`source` resolved, if a model may have it captured; a refusal if not.

        Allowed: a file under the code root or a workspace an agent was
        installed into, and outside the vault itself -- the vault's `.brain/`
        holds the search index and extracted text of every source, never-ingest
        included, so capturing it back in would launder it into a readable
        branch. Refused before the file is looked at, so the answer never says
        whether something exists outside the project, and never carries its
        content. Symlinks are resolved first, and the secret check reads both
        the name asked for and the name it resolves to.
        """

        given = Path(source).expanduser()
        resolved = given.resolve()
        if _secret_shaped(given) or _secret_shaped(resolved):
            raise _capture_refusal(
                "MCP capture never reads a credential file",
                reason="secret_shaped",
            )
        vault_root = self.vault.root.resolve()
        inside_project = any(
            resolved.is_relative_to(root) for root in self._project_roots()
        )
        if not inside_project or resolved.is_relative_to(vault_root):
            raise _capture_refusal(
                "MCP capture reads files only inside this vault's project",
                reason="outside_project",
            )
        if not resolved.is_file():
            # The caller's own spelling, not the resolved path: a relative
            # source would otherwise come back naming the project's location.
            raise NotFoundError(
                "Capture source is not a file", details={"source": source}
            )
        return resolved

    def _project_roots(self) -> list[Path]:
        """Where a confined capture may read: the code root and agent workspaces.

        The code root is already bounded below the home directory by
        `code_root_reason`. A workspace is whatever `bk hooks install --root`
        recorded, so the same bound is applied here: a root at or above home
        would confine nothing.
        """

        vault_root = self.vault.root
        candidates = [self.vault.code_root()]
        for agent in installed_agents(vault_root):
            try:
                adapter = json.loads(
                    (vault_root / adapter_path(agent)).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            workspace = adapter.get("workspace") if isinstance(adapter, dict) else None
            if isinstance(workspace, str) and workspace:
                candidates.append(Path(workspace))
        home = Path.home().resolve()
        roots = []
        for candidate in candidates:
            root = candidate.resolve()
            if root == Path(root.anchor) or home.is_relative_to(root):
                continue
            roots.append(root)
        return roots

    def watch_once(self) -> dict[str, Any]:
        """Capture every eligible file under the configured source folders.

        Eligibility is a vault rule, not a caller's: `ignore` prunes whole
        trees, and the vault's own directory is always excluded so a watch
        pointed at a parent of the vault cannot re-capture `raw/` into itself.

        **Where a source resolves is a vault rule too**, so a relative path is
        read against the vault rather than the process's current directory —
        see `resolve_source_path`.

        A source that resolves to nothing is never passed over in silence,
        because a watch that captures nothing looks exactly like a watch with
        nothing to capture:

        - **Every configured source missing** is a configuration the operator
          has to fix, and no run can ever do anything until they do. It gets
          the same refusal, and so the same exit status, as configuring no
          sources at all — the state it amounts to.
        - **Some sources missing** still leaves real work, and a capture
          declined over a typo elsewhere in the policy would be worse than the
          typo. Those are reported alongside the per-file failures and the
          walk continues.
        """

        config = self.vault.config()
        if not config.sources:
            raise NotConfiguredError("No source folders/files are configured")
        present: list[Path] = []
        missing: list[tuple[str, Path]] = []
        for value in config.sources:
            root = resolve_source_path(self.vault.root, value)
            if root.exists():
                present.append(root)
            else:
                missing.append((value, root))
        if not present:
            raise NotConfiguredError(
                "No configured source folder/file exists",
                details={
                    "sources": [
                        _missing_source(value, root) for value, root in missing
                    ],
                    "hint": (
                        "A relative source is resolved against the vault. Point "
                        "sources at paths that exist, or use absolute paths."
                    ),
                },
            )
        created = 0
        duplicates = 0
        ignored = 0
        forgotten = 0
        failures: list[dict[str, str]] = [
            {
                "path": str(root),
                "error": _missing_source_error(_missing_source(value, root)),
            }
            for value, root in missing
        ]
        for root in present:
            for candidate, skipped in _walk_source(
                root, config.ignore, self.vault.root
            ):
                ignored += skipped
                if candidate is None:
                    continue
                try:
                    result = self.capture(str(candidate), revive=False)
                    created += int(result["created"])
                    duplicates += int(not result["created"])
                except ForgottenSourceError:
                    forgotten += 1
                except (BrainskitError, OSError) as exc:
                    failures.append({"path": str(candidate), "error": str(exc)})
        return {
            "created": created,
            "duplicates": duplicates,
            "ignored": ignored,
            "forgotten": forgotten,
            "failures": failures,
        }

    def drop_orphaned_freshness(self) -> list[str]:
        """Heal freshness state after a wiki page is removed outside the gate.

        The registry is reconciled by content hash; freshness is keyed by path,
        so a deleted page leaves an entry that can never be revived.
        """
        present = set(self.vault.wiki_pages())
        dropped = self.ledger.snapshot().orphans(present)
        self.ledger.drop(dropped)
        return dropped

    def pages_citing(self, content_hash: str) -> list[str]:
        """Wiki pages whose frontmatter still declares this source.

        Informational only: forgetting the source does not touch these pages,
        and the next `bk lint` reports them as `wiki.unknown_source` — this
        just surfaces that up front instead of leaving it to be discovered.
        """
        citing = []
        for path in self.vault.wiki_pages():
            metadata, _ = parse_frontmatter(self.vault.read_text(path))
            if content_hash in metadata.get("sources", []):
                citing.append(path)
        return citing

    def _mark_related_pages_for_review(self, record: SourceRecord) -> None:
        terms = self._related_query_terms(record)
        if not terms:
            return
        candidates = [
            hit
            for hit in self.index.search(" ".join(terms), limit=_RELATED_CANDIDATES)
            if hit.kind == "wiki"
        ]
        if not candidates:
            return
        # BM25 orders the candidates but has no absolute scale, so a page only
        # counts as related when it actually shares the capture's vocabulary.
        required = min(_RELATED_MIN_SHARED_TERMS, len(terms))
        wanted = set(terms)
        related: list[str] = []
        for hit in candidates:
            shared = wanted & _content_tokens(self.vault.read_text(hit.path))
            if len(shared) < required:
                continue
            related.append(hit.path)
            if len(related) >= _RELATED_PAGE_LIMIT:
                break
        # `mark_reviewed` carries the never-downgrade rule, which this writer
        # used to lack: a page already `stale` stays `stale`, because `review`
        # is skipped by the ageing pass and would have parked it there.
        self.ledger.mark_reviewed(
            dict.fromkeys(related, f"related source:{record.content_hash}")
        )

    def _related_query_terms(self, record: SourceRecord) -> list[str]:
        """Describe a capture by its own text, not by the name it was saved under."""
        text = self.vault.raw_text(record, max_chars=_RELATED_TEXT_LIMIT)
        content = _normalized_tokens(text)
        body_terms = [
            term
            for term, _ in Counter(
                token for token in content if _is_salient_term(token)
            ).most_common(_RELATED_QUERY_TERMS)
        ]
        # The file name only contributes what the document itself corroborates,
        # so a suggestive name cannot stand in for unrelated content.
        seen = set(content)
        name_terms = [
            token
            for token in _normalized_tokens(
                PurePosixPath(record.original_name).stem.replace("-", " ")
            )
            if _is_salient_term(token) and token in seen
        ]
        terms = dict.fromkeys([*name_terms, *body_terms])
        return list(terms)[:_RELATED_QUERY_TERMS]


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def _secret_shaped(path: Path) -> bool:
    name = path.name
    if name.startswith(".env") and name not in _ENV_TEMPLATES:
        return True
    if name in _SECRET_NAMES or path.suffix in _SECRET_SUFFIXES:
        return True
    if _PRIVATE_KEY.fullmatch(name):
        return True
    if _SECRET_DIRECTORY_NAMES.intersection(path.parts[:-1]):
        return True
    home = Path.home()
    return any(
        path.is_relative_to(home / directory)
        or path.is_relative_to(home.resolve() / directory)
        for directory in _SECRET_HOME_DIRECTORIES
    )


def _capture_refusal(message: str, *, reason: str) -> PolicyError:
    # No path and no root in the details: the path is the caller's own input,
    # and the project roots are installation facts (ADR 0009).
    return PolicyError(
        message,
        details={
            "reason": reason,
            "allowed": (
                "literal text, an http(s) URL, or a file inside this vault's "
                "project that is not a credential file"
            ),
            "hint": (
                "Send the content as text instead, or ask the operator to run "
                "bk capture with the path"
            ),
        },
    )


def _missing_source(value: str, root: Path) -> dict[str, str]:
    """Describe a configured source that points at nothing.

    The `found_at_cwd` case is the upgrade note, delivered to the one person
    it concerns rather than to a changelog they may not read. A relative
    source used to resolve against the process's current directory, so a
    vault whose owner always ran `bk watch` from the same place had a
    configuration that worked by coincidence — and repointing it silently
    would be its own defect. When the old location still holds the folder,
    the message names it, and the remedy is to write that path into `sources`.
    """

    detail = {"source": value, "resolved": str(root)}
    from_cwd = Path(value).expanduser().resolve()
    if from_cwd != root and from_cwd.exists():
        detail["found_at_cwd"] = str(from_cwd)
    return detail


def _missing_source_error(detail: dict[str, str]) -> str:
    """The same fact as prose, for the `failures` list a partial run returns."""

    message = (
        f"Configured source {detail['source']!r} resolves to "
        f"{detail['resolved']}, relative to the vault, and nothing is there"
    )
    if "found_at_cwd" in detail:
        message += (
            f" — it does exist at {detail['found_at_cwd']}, where this path "
            "resolved before sources became vault-relative"
        )
    return message


def _walk_source(
    root: Path, ignore: Sequence[str], vault_root: Path
) -> Iterator[tuple[Path | None, int]]:
    """Yield capturable files under ``root``, pruning ignored directories.

    Pruning matters as much as filtering. `os.walk` lets an ignored directory
    be dropped from the traversal, so `node_modules` costs one comparison
    instead of a stat call per file inside it — on a source folder holding a
    project, that is the difference between a watch tick and a stall.

    The second element of each pair is how many entries that step skipped, so
    the caller can report what it declined without the walker holding state.
    """

    if root.is_file():
        if is_ignored(root.name, ignore):
            yield None, 1
            return
        yield (None, 1) if _inside(root, vault_root) else (root, 0)
        return
    if not root.is_dir():
        return
    for current, directories, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        if _inside(here, vault_root):
            # The vault's own directory is never a source. Pruning it here also
            # covers `raw/`, which a watch pointed at the vault's parent would
            # otherwise re-capture into itself on every tick.
            directories[:] = []
            continue
        kept = []
        for name in directories:
            if is_ignored(_relative(here / name, root), ignore):
                yield None, 1
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            candidate = here / name
            if is_ignored(_relative(candidate, root), ignore):
                yield None, 1
            elif candidate.is_file():
                yield candidate, 0


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
