"""Whether the vault still holds together, and what `bk status` reports.

Lint is where the engine's claims are checked against the disk rather than
assumed: that raw bytes still hash to their registered identity, that every
wiki page was written by the apply gate, that provenance resolves, and that
derived artefacts still describe the inputs they were built from.

`status` is a lint plus counts, which is why they live together -- every
`bk status` runs a full lint, so anything that is per-page here is per-page in
status too.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from brainskit.application.codegraph import CODE_PROJECTION, CodeGraph
from brainskit.application.freshness import (
    PROJECTION_ANCHORS,
    PROJECTION_COMMANDS,
    PROJECTION_LINT_CODES,
    PROJECTION_RAW_FIELDS,
    _age_in_days,
    _freshness_summary,
    _orphaned_freshness,
    _projection_source_hash,
)
from brainskit.application.gate import INSTRUCTION_START
from brainskit.application.judgment import JudgmentRunner
from brainskit.application.pages import parse_frontmatter
from brainskit.application.ports import SearchIndexPort, VaultPort
from brainskit.application.privacy import _context_branches
from brainskit.application.retrieval import Retrieval
from brainskit.domain.model import (
    CITATION_RE,
    CODE_CITATION_RE,
    WIKI_LINK_RE,
    CodeSource,
    LintFinding,
    SourceRecord,
    ValidationError,
    utc_now,
    validate_schema,
)

#: Where git reads hooks from when `core.hooksPath` says nothing.
DEFAULT_GIT_HOOKS = Path(".git") / "hooks"


def redirected_git_hooks_path(root: Path) -> Path | None:
    """Where git actually runs hooks from, when that is not `.git/hooks`.

    `core.hooksPath` moves the directory git reads -- Husky sets it on every
    install -- and once it is set git never looks at `.git/hooks/pre-commit`
    again. A hook written there is dead code, and reporting it as an active
    layer is reporting a guard that does not run: the artefact exists, but it
    is not the thing executing. That is the precise failure `_enforcement_state`
    exists to catch, so it has to be caught here too.

    Asks git rather than parsing `.git/config`, because the setting may come
    from the local, global or system file and git is the only thing that
    resolves all three the way a commit will. `None` means git runs the default
    directory, or could not be asked at all -- and a machine with no usable git
    runs no hook of any kind, so there is no claim left to correct.

    Answered only for a repository root. Asked from a subdirectory git answers
    about the repository above, which would read here as a redirect away from a
    `.git/hooks` that was never this directory's to begin with. `.git` is a file
    rather than a directory in a worktree or submodule, and both are roots.
    """
    if not (root / ".git").exists():
        return None
    try:
        # A fixed argument vector with no shell, asking `git` where it reads
        # hooks from -- the same reasoning `cli.py`'s S607 ignore already
        # states for this file, extended to the untrusted-input check.
        completed = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    reported = completed.stdout.strip()
    if not reported:
        return None
    # Reported relative to the cwd the question was asked from, which is `root`.
    hooks = Path(reported)
    if not hooks.is_absolute():
        hooks = root / hooks
    default = root / DEFAULT_GIT_HOOKS
    try:
        same = hooks.resolve() == default.resolve()
    except OSError:
        same = hooks == default
    return None if same else hooks


class Health:
    """Structural lint, vault status, and the derived-artefact report."""

    def __init__(
        self,
        vault: VaultPort,
        index: SearchIndexPort,
        retrieval: Retrieval,
        judgment_runner: JudgmentRunner,
    ):
        self.vault = vault
        self.index = index
        self.retrieval = retrieval
        self.judgment_runner = judgment_runner


    def lint(self, *, semantic: bool = False) -> dict[str, Any]:
        findings = self._mechanical_lint()
        self._review_drifted_code_citations(findings)
        semantic_report: dict[str, Any] | None = None
        if semantic:
            # `lint-semantic` judges consistency, never proposes a write (see
            # `Jobs.ask` for why the apply-proposal shape stays off here).
            context = self.retrieval.context(
                "contradictions unsupported claims", limit=20, include_apply_contract=False
            )
            semantic_report = self.judgment_runner.run(
                job="lint-semantic",
                branches=_context_branches(context),
                variables={"context": json.dumps(context, ensure_ascii=False)},
            )
        return {
            "ok": not any(item.severity == "error" for item in findings),
            "findings": [item.to_dict() for item in findings],
            "semantic_report": semantic_report,
        }

    def status(self) -> dict[str, Any]:
        records = self.vault.registry()
        pages = self.vault.wiki_pages()
        raw_counts: dict[str, int] = defaultdict(int)
        for record in records.values():
            parts = PurePosixPath(record.path).parts
            branch = parts[1] if len(parts) > 1 else "unknown"
            raw_counts[branch] += 1
        lint_result = self.lint()
        # Read after lint: `_mechanical_lint` refreshes page staleness in place,
        # so reading first would report the state that lint just superseded.
        freshness = self.vault.read_state("freshness")
        enforcement = self._enforcement_state()
        return {
            "vault": str(self.vault.root),
            "sources": len(records),
            "pending": sum(record.status == "pending" for record in records.values()),
            "wiki_pages": len(pages),
            "by_branch": dict(sorted(raw_counts.items())),
            "index": self.index.stats(),
            "freshness": _freshness_summary(freshness, present=set(pages)),
            "projections": {
                **self._projection_report(freshness, records),
                # Reported alongside the vault's own projections because it is
                # one: derived, regenerable, and worth nothing once the thing it
                # describes has moved on.
                CODE_PROJECTION: CodeGraph(self.vault).staleness(),
            },
            "enforcement": enforcement,
            # `healthy` used to be `lint_result["ok"]` alone, so `bk status`
            # printed a green headline directly above three red enforcement
            # rows. A vault whose write gate is not running is not healthy just
            # because the pages it already has happen to lint; the headline sits
            # above those rows and has to mean them too.
            #
            # Advisory layers (the CLAUDE.md block) are excluded deliberately:
            # they inform, they do not enforce, and failing the headline on one
            # would make it fire for something no mechanism was ever going to
            # stop.
            "healthy": lint_result["ok"]
            and all(
                layer.get("active")
                for layer in enforcement.get("layers", [])
                if not layer.get("advisory")
            ),
            "lint_errors": sum(
                finding["severity"] == "error" for finding in lint_result["findings"]
            ),
        }

    def enforcement(self) -> dict[str, Any]:
        """Which enforcement layers are live, read from disk.

        Public because `bk doctor` reports the same four layers and must not
        pay for a full lint (`status` runs one) to ask a question about the
        installation rather than about the vault's contents.
        """

        return self._enforcement_state()

    def _lint_enrichment(self, findings: list[LintFinding]) -> None:
        """Report inferred edges whose evidence is gone.

        Enrichment inherits its privacy from the sources it was derived from,
        so a source that has been forgotten leaves an edge nothing can classify.
        `Enrichment.privacy_of` already fails closed and treats it as
        `never-ingest`; this is what turns that silent restriction into
        something an operator can repair.
        """

        from brainskit.application.enrichment import Enrichment

        for edge in Enrichment(self.vault).orphaned():
            findings.append(
                LintFinding(
                    "enrichment.unresolved_source",
                    "Enrichment edge cites evidence this vault no longer holds",
                    path=f"{edge.get('source')} --{edge.get('relation')}--> {edge.get('target')}",
                )
            )

    def _mechanical_lint(self) -> list[LintFinding]:
        findings: list[LintFinding] = []
        self._lint_enrichment(findings)
        freshness = self._refresh_staleness()
        records = self.vault.registry()
        raw_files = set(self.vault.raw_files())
        registered_paths: dict[str, str] = {}
        for content_hash, record in records.items():
            if record.path not in raw_files:
                findings.append(
                    LintFinding(
                        "registry.missing_file",
                        "Registered raw source is missing",
                        path=record.path,
                    )
                )
            else:
                observed_hash = self.vault.content_hash(record.path)
                if observed_hash != content_hash:
                    findings.append(
                        LintFinding(
                            "raw.content_modified",
                            "Raw source content no longer matches its immutable hash",
                            path=record.path,
                        )
                    )
            prior = registered_paths.get(record.path)
            if prior and prior != content_hash:
                findings.append(
                    LintFinding(
                        "registry.path_collision",
                        "Two hashes reference the same raw path",
                        path=record.path,
                    )
                )
            registered_paths[record.path] = content_hash
        for path in sorted(raw_files - set(registered_paths)):
            findings.append(
                LintFinding(
                    "registry.untracked_file",
                    "Raw source is not registered; run bk reconcile",
                    path=path,
                )
            )
        slugs = self.vault.existing_wiki_slugs()
        # Read once, not once per page: `schema()` re-reads and re-parses
        # `.brain/schema.json` from disk on every call, so a vault-wide lint
        # was paying for the same file as many times as it had pages.
        schema = self.vault.schema()
        for path in self.vault.wiki_pages():
            text = self.vault.read_text(path)
            metadata, body = parse_frontmatter(text)
            freshness_entry = freshness.get("pages", {}).get(path)
            is_system_page = metadata.get("type") == "system"
            if not is_system_page and not isinstance(freshness_entry, dict):
                findings.append(
                    LintFinding(
                        "wiki.outside_apply",
                        "Wiki page is not tracked by the apply gate",
                        path=path,
                    )
                )
            elif isinstance(freshness_entry, dict):
                expected_hash = freshness_entry.get("content_hash")
                wiki_observed_hash = self.vault.wiki_version(path)
                if expected_hash and expected_hash != wiki_observed_hash:
                    findings.append(
                        LintFinding(
                            "wiki.outside_apply",
                            "Wiki page changed outside the apply gate",
                            path=path,
                        )
                    )
            for failure in validate_schema(metadata, schema):
                findings.append(
                    LintFinding(
                        failure["code"],
                        failure["message"],
                        path=path,
                    )
                )
            for field in ("id", "type", "title", "aliases", "sources", "updated_at"):
                if field not in metadata:
                    findings.append(
                        LintFinding(
                            "wiki.missing_frontmatter",
                            f"Required frontmatter field is missing: {field}",
                            path=path,
                        )
                    )
            sources = metadata.get("sources", [])
            if not isinstance(sources, list):
                sources = []
                findings.append(
                    LintFinding(
                        "wiki.invalid_sources",
                        "Frontmatter sources must be a list",
                        path=path,
                    )
                )
            for content_hash in sources:
                if content_hash not in records:
                    findings.append(
                        LintFinding(
                            "wiki.unknown_source",
                            f"Unknown source hash: {content_hash}",
                            path=path,
                        )
                    )
            citations = set(CITATION_RE.findall(body))
            for content_hash in citations - set(sources):
                findings.append(
                    LintFinding(
                        "wiki.undeclared_citation",
                        f"Citation is not declared in sources: {content_hash}",
                        path=path,
                    )
                )
            findings.extend(self._code_citation_findings(path, metadata, body))
            for link in WIKI_LINK_RE.findall(body):
                target = PurePosixPath(link.strip()).name
                if target not in slugs:
                    findings.append(
                        LintFinding(
                            "wiki.unresolved_link",
                            f"Unresolved wiki link: {link.strip()}",
                            path=path,
                        )
                    )
        for path, entry in freshness.get("pages", {}).items():
            if isinstance(entry, dict) and entry.get("status") == "stale":
                findings.append(
                    LintFinding(
                        "wiki.stale",
                        f"Wiki page is stale ({entry.get('age_days', '?')} days)",
                        severity="warning",
                        path=path,
                    )
                )
        for path in _orphaned_freshness(freshness, set(self.vault.wiki_pages())):
            findings.append(
                LintFinding(
                    "freshness.orphaned",
                    "Freshness state tracks a page that no longer exists; "
                    "run bk reconcile",
                    severity="warning",
                    path=path,
                )
            )
        # `freshness` is the state `_refresh_staleness` just committed and
        # `records` the registry this run already read, so the comparison sees
        # exactly the inputs lint reported on without re-reading either. Note
        # the fingerprint leaves out the status and age fields that refresh
        # rewrites on every run, or every lint would invent a stale projection.
        for artifact, report in self._projection_report(freshness, records).items():
            if not report["stale"]:
                continue
            findings.append(
                LintFinding(
                    PROJECTION_LINT_CODES[artifact],
                    f"Derived {artifact} was built from a different set of wiki "
                    f"pages; run {PROJECTION_COMMANDS[artifact]}",
                    severity="warning",
                )
            )
        return findings

    def _code_citation_findings(
        self, path: str, metadata: dict[str, Any], body: str
    ) -> list[LintFinding]:
        """Re-read every cited file and compare it with the hash on the page.

        This is the whole point of spelling a code citation as a hash. A page
        claiming something about `db.ts:L17` cannot know the file moved on; a
        page carrying the hash of the bytes it was written against can be told.
        Three distinct outcomes, and each means something different to a reader:
        the file changed, the file is gone, or the citation was never declared
        and so cannot be checked at all.
        """

        findings: list[LintFinding] = []
        declared = metadata.get("code_sources", [])
        if not isinstance(declared, list):
            return [
                LintFinding(
                    "wiki.invalid_code_sources",
                    "Frontmatter code_sources must be a list",
                    path=path,
                )
            ]

        by_hash: dict[str, str] = {}
        for entry in declared:
            try:
                source = CodeSource.from_dict(entry)
            except ValidationError as exc:
                findings.append(
                    LintFinding("wiki.invalid_code_sources", str(exc), path=path)
                )
                continue
            by_hash[source.content_hash] = source.path

            observed = self.vault.code_hash(source.path)
            if observed is None:
                findings.append(
                    LintFinding(
                        "wiki.missing_code_source",
                        f"Cited file is not under the code root: {source.path}",
                        path=path,
                    )
                )
            elif observed != source.content_hash:
                findings.append(
                    LintFinding(
                        "wiki.stale_code_citation",
                        f"{source.path} has changed since this page cited it; "
                        "re-read it and update the claim",
                        path=path,
                        severity="warning",
                    )
                )

        for content_hash in set(CODE_CITATION_RE.findall(body)) - by_hash.keys():
            findings.append(
                LintFinding(
                    "wiki.undeclared_code_citation",
                    "Code citation is not declared in code_sources: "
                    f"{content_hash}",
                    path=path,
                )
            )
        return findings

    def _review_drifted_code_citations(self, findings: list[LintFinding]) -> None:
        """Move a page whose cited code has changed into the review queue.

        A lint warning is read once, by whoever ran lint. `review` is a state the
        vault carries until someone acts on it, and it is already how a page
        answers "is this still backed by what it was compiled from" — so code
        drift joins the same queue rather than inventing a second one.
        """

        drifted = {
            finding.path: finding.message
            for finding in findings
            if finding.code == "wiki.stale_code_citation" and finding.path
        }
        if not drifted:
            return

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            pages = state.setdefault("pages", {})
            for path, message in drifted.items():
                entry = pages.setdefault(path, {})
                # Never downgrade: a page already `stale` has a stronger claim
                # on attention than one that has merely drifted.
                if entry.get("status") == "stale":
                    continue
                entry["status"] = "review"
                entry["review_reason"] = f"code changed: {message.split(' has changed')[0]}"
                entry["review_requested_at"] = utc_now()
            return state

        self.vault.mutate_state("freshness", mutate)

    def _refresh_staleness(self) -> dict[str, Any]:
        stale_after_days = self.vault.config().novelty.stale_after_days
        now = datetime.now(UTC)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            for entry in state.setdefault("pages", {}).values():
                if not isinstance(entry, dict) or entry.get("status") == "review":
                    continue
                updated_at = entry.get("updated_at")
                if not isinstance(updated_at, str):
                    continue
                try:
                    age_days = (now - datetime.fromisoformat(updated_at)).days
                except ValueError:
                    continue
                entry["status"] = (
                    "stale" if age_days >= stale_after_days else "fresh"
                )
                entry["age_days"] = age_days
            return state

        return self.vault.mutate_state("freshness", mutate)

    def _projection_report(
        self, freshness: dict[str, Any], records: dict[str, SourceRecord]
    ) -> dict[str, Any]:
        """Compare every derived artefact against the inputs it was built from.

        Three outcomes, and they are not the same thing:

        - `missing` — the artefact is not on disk. Nothing derives from the
          vault yet, so nothing can be out of date. `bk graph` and `bk views`
          are on-demand, and a vault that never ran them is not in error.
        - `stale` — the artefact exists but was built from different inputs, or
          from an unrecorded set. A projection whose provenance is unknown is
          treated as out of date rather than trusted.
        - `fresh` — the recorded fingerprint matches the current inputs.

        Each artefact is compared against its own inputs. A shared fingerprint
        would have to cover the union, so a change only one artefact renders
        would age both — and a projection that cries wolf gets ignored, which
        loses the signal by a different route than having no signal at all.
        """
        pages = freshness.get("pages", {})
        recorded = freshness.get("projections", {})
        recorded = recorded if isinstance(recorded, dict) else {}
        now = datetime.now(UTC)
        report: dict[str, Any] = {}
        for artifact, anchor in PROJECTION_ANCHORS.items():
            expected = _projection_source_hash(
                pages, records, PROJECTION_RAW_FIELDS[artifact]
            )
            entry = recorded.get(artifact)
            entry = entry if isinstance(entry, dict) else {}
            generated_at = entry.get("generated_at")
            if not isinstance(generated_at, str):
                generated_at = None
            if self.vault.wiki_version(anchor) is None:
                state = "missing"
            elif entry.get("source_hash") != expected:
                state = "stale"
            else:
                state = "fresh"
            item: dict[str, Any] = {
                "state": state,
                "stale": state == "stale",
                "generated_at": generated_at,
            }
            age_days = _age_in_days(generated_at, now)
            if age_days is not None:
                item["age_days"] = age_days
            report[artifact] = item
        return report

    def _record_projection(self, artifact: str) -> None:
        """Stamp a derived artefact with the inputs it was just built from.

        The page half of the fingerprint is taken inside the mutator, so it is
        computed from the state the write actually commits: an apply landing
        between a read and a write cannot leave a projection claiming to cover
        pages it never saw.

        The registry is read *before* the mutator on purpose. `commit_wiki_batch`
        takes the registry lock before the freshness lock, and both are blocking
        `flock`s, so reading the registry while holding freshness would invert
        the order and deadlock. Reading it first is also the safe direction: a
        capture landing in between is simply absent from the recorded
        fingerprint, and the next lint compares against a registry that has it
        and reports stale. The error can only be a false `stale`, never a false
        `fresh`.
        """
        records = self.vault.registry()
        raw_fields = PROJECTION_RAW_FIELDS[artifact]

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            projections = state.setdefault("projections", {})
            projections[artifact] = {
                "generated_at": utc_now(),
                "source_hash": _projection_source_hash(
                    state.get("pages", {}), records, raw_fields
                ),
            }
            return state

        self.vault.mutate_state("freshness", mutate)

    def _enforcement_state(self) -> dict[str, Any]:
        """Report which enforcement layers are live for this vault, from disk.

        `hooks install` says what it wrote at the moment it ran; this says what
        is guarding the vault now. They diverge whenever a hook is edited away,
        a settings file is rewritten, or the vault is copied without its git
        directory -- and a layer that is off while everything still reads like
        success is precisely how an invariant ends up guarded by nothing.

        It looks where the agent's configuration actually is, which the adapter
        records: reading the vault unconditionally would report every layer off
        for any vault nested inside the project it guards.
        """
        root = self._agent_workspace()
        settings_path = root / ".claude" / "settings.json"
        registered: set[str] = set()
        events: dict[str, set[str]] = {}
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks = settings.get("hooks")
            if isinstance(hooks, dict):
                for event, groups in hooks.items():
                    commands = {
                        str(hook.get("command", ""))
                        for group in groups
                        if isinstance(group, dict)
                        for hook in group.get("hooks", [])
                        if isinstance(hook, dict)
                    }
                    events[event] = commands
                    registered |= commands
        except (OSError, ValueError, AttributeError, TypeError):
            # An unreadable or malformed settings file means "nothing is
            # registered", never an exception out of `bk status`.
            pass

        def registered_under(event: str, path: Path) -> bool:
            """Is ``path`` the script some command on ``event`` actually runs?

            Compared against the resolved path as well as the literal one: on
            macOS a vault under /var resolves to /private/var, so a settings
            file written with either spelling must still read as registered.
            A command may also wrap the script in a shell guard rather than
            naming it alone, so containment counts.
            """
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            candidates = {str(path), str(resolved)}
            for command in events.get(event, set()):
                if command in candidates or any(c in command for c in candidates):
                    return True
                # The command may spell the same file a different way. Compare
                # resolved forms, guarded because a command is often a shell
                # snippet rather than a bare path.
                try:
                    if Path(command).resolve() == resolved:
                        return True
                except (OSError, ValueError):
                    continue
            return False

        def hook_layer(
            name: str, script: str, event: str, mechanism: str
        ) -> dict[str, Any]:
            path = root / ".claude" / "hooks" / script
            active = path.is_file() and registered_under(event, path)
            detail = "active"
            if not path.is_file():
                detail = f"{script} is not installed"
            elif not active:
                detail = f"{script} exists but is not registered under {event}"
            return {
                "layer": name,
                "mechanism": mechanism,
                "active": active,
                "detail": detail,
                # Named so a reader -- and `bk doctor`, which runs it -- can
                # reach the exact file this verdict is about instead of
                # rebuilding the path from assumptions about the workspace.
                "script": str(path),
            }

        pre_commit = root / ".git" / "hooks" / "pre-commit"
        # A redirected hooks directory disqualifies the layer no matter what the
        # file says: git will not run it, so its contents prove nothing.
        redirected_hooks = redirected_git_hooks_path(root)
        try:
            commit_active = (
                redirected_hooks is None
                and pre_commit.is_file()
                and "lint" in pre_commit.read_text(encoding="utf-8")
            )
        except OSError:
            commit_active = False
        if commit_active:
            commit_detail = "active"
        elif redirected_hooks is not None:
            commit_detail = (
                f"git runs hooks from {redirected_hooks}, not .git/hooks, so a "
                "pre-commit hook installed there never runs"
            )
        elif not (root / ".git").is_dir():
            commit_detail = f"{root} is not a git repository"
        else:
            commit_detail = f"{root} has no brainskit pre-commit hook"
        # The sentinel is duplicated from the installer rather than imported:
        # the application layer must not depend on interfaces.
        instructions = root / "CLAUDE.md"
        try:
            advisory_active = (
                instructions.is_file()
                and INSTRUCTION_START in instructions.read_text(
                    encoding="utf-8"
                )
            )
        except OSError:
            advisory_active = False

        layers = [
            hook_layer(
                "write_gate",
                "brainskit-gate.sh",
                "PreToolUse",
                "Claude Code PreToolUse hook on Write|Edit|MultiEdit",
            ),
            hook_layer(
                "session_status",
                "brainskit-status.sh",
                "SessionStart",
                "Claude Code SessionStart hook reporting vault state",
            ),
            {
                "layer": "commit_lint",
                "mechanism": ".git/hooks/pre-commit running bk lint --changed",
                "active": commit_active,
                "detail": commit_detail,
            },
            {
                "layer": "instructions",
                "mechanism": "CLAUDE.md managed block",
                "active": advisory_active,
                "advisory": True,
                "detail": "active" if advisory_active else "no managed block found",
            },
        ]
        return {
            "layers": layers,
            "inactive": [layer["layer"] for layer in layers if not layer["active"]],
            # Specifically the write gate, not "any non-advisory layer is on".
            # session_status is observability and commit_lint catches a bypass
            # only after the fact; neither one keeps a write out of the wiki, so
            # letting either imply `gated` would report a guarded vault that a
            # Write tool can still walk straight into.
            "gated": any(
                layer["active"] for layer in layers if layer["layer"] == "write_gate"
            ),
        }

    def _agent_workspace(self, agent: str = "claude") -> Path:
        """Where the agent's configuration lives, per the adapter that recorded it.

        Falls back to the enclosing project when the vault sits inside one --
        the same repository `hooks install` targets with no explicit `--root`
        -- and to the vault itself only when it is not. An adapter written
        before the workspace was recorded therefore keeps reporting exactly
        as it did; a vault that has never had hooks installed at all now
        reports where they would land, instead of every layer reading "not a
        git repository" for a vault that plainly sits inside one (`bk status`
        on a vault nested under its own project's `.git`, before `hooks
        install` has ever run there).
        """
        source = self.vault.root / ".brain" / f"agent-{agent}.json"
        try:
            adapter = json.loads(source.read_text(encoding="utf-8"))
            workspace = adapter.get("workspace")
        except (OSError, ValueError, AttributeError):
            workspace = None
        if isinstance(workspace, str) and workspace:
            return Path(workspace)
        return self._enclosing_project_root()

    def _enclosing_project_root(self) -> Path:
        """The git repository the vault sits inside, or the vault itself.

        Reuses `code_root`'s own bounded walk-up rather than repeating it --
        a vault inside a project is committed through that project, and this
        is the directory an install with no explicit `--root` would pick
        (see `_project_root_for` in the CLI, the same choice made there).
        """
        root = self.vault.code_root()
        return root if (root / ".git").exists() else self.vault.root

