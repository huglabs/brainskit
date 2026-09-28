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
import shlex
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from brainskit.application.codegraph import CODE_PROJECTION, CodeGraph
from brainskit.application.freshness import (
    PROJECTION_ANCHORS,
    PROJECTION_COMMANDS,
    PROJECTION_INTEGRITY,
    PROJECTION_LINT_CODES,
    PROJECTION_RAW_FIELDS,
    REGENERATE_STATES,
    FreshnessLedger,
    FreshnessSnapshot,
    _age_in_days,
    _projection_source_hash,
)
from brainskit.application.gate import HOOK_SENTINEL, INSTRUCTION_START
from brainskit.application.install import (
    COMMIT_LINT,
    COMMIT_LINT_MECHANISM,
    DEFAULT_AGENT,
    DEFAULT_GIT_HOOKS,
    INSTRUCTIONS,
    WRITE_GATE,
    AgentHook,
    adapter_path,
    agent_install,
    installed_agents,
    redirected_git_hooks_path,
    redirected_hooks_hint,
)
from brainskit.application.installer import (
    command_script,
    is_generated_pre_commit,
    pre_commit_lints,
    pre_commit_vault,
    render_hook_script,
    render_pre_commit,
)
from brainskit.application.judgment import JudgmentRunner
from brainskit.application.pages import parse_frontmatter
from brainskit.application.ports import SearchIndexPort, VaultPort
from brainskit.application.retrieval import Retrieval
from brainskit.application.schema import validate_schema
from brainskit.domain.model import (
    CITATION_RE,
    CODE_CITATION_RE,
    WIKI_LINK_RE,
    CodeSource,
    LintFinding,
    NotConfiguredError,
    PolicyError,
    SourceRecord,
    ValidationError,
)
from brainskit.domain.privacy import context_branches

#: Returned by `_artefact_fault` when the anchor could not be opened at all.
#: Compared by identity, never by value, so it can never be confused with a real
#: fault a detector reports.
_ARTEFACT_ABSENT: dict[str, Any] = {"problem": "no artefact"}

#: The two pages `bk init` writes before any apply has run, so they exist with
#: no entry in the freshness ledger. `Vault.initialize` writes them from
#: `_system_page` and nothing writes them again -- not `bk apply`, which only
#: ever writes the pages a proposal names, and not `bk views`, which writes
#: `views/`. They are named here rather than recognised by their `type: system`
#: frontmatter because that field is written by whatever wrote the file: keying
#: the exemption on it let any page opt itself out of `wiki.outside_apply`
#: forever, which is the one thing an integrity check must not let its subject
#: decide. `SeededSystemPageTest` builds a page with the real `_system_page` and
#: asserts lint stays quiet, so this list cannot drift from what init writes.
SEEDED_SYSTEM_PAGES: frozenset[str] = frozenset({"wiki/index.md", "wiki/log.md"})


def _is_seeded_shape(body: str) -> bool:
    """Whether a page body is still the heading `bk init` seeded and nothing else.

    The seeded pages have no sources and no ledger entry, so there is no hash to
    compare against and nothing to say what they looked like when they were
    written. What can be checked is the shape init gives them -- a single
    heading -- which holds across every version that has ever seeded them,
    including the ones that spelled the title differently.

    That makes this narrower than the hash comparison a tracked page gets: an
    edit that replaced the heading with another heading would pass. It is the
    strongest claim available without inventing state, and it catches the case
    that actually happens, which is content appended below.
    """

    lines = [line for line in body.strip().splitlines() if line.strip()]
    return len(lines) == 1 and lines[0].startswith("# ")


def _reinstall_hint(agent: str, workspace: Path, vault_root: Path) -> str:
    """The command that rewrites an agent's install where it already is.

    `--root` only when the adapter recorded a workspace other than the vault:
    without it `hooks install` writes beside the vault, and a nested vault
    would gain a second, unloaded copy while the stale one stayed in use.
    """

    command = f"bk hooks install --agent {agent}"
    if workspace != vault_root:
        command += f" --root {shlex.quote(str(workspace))}"
    return command


def enforcement_ok(enforcement: dict[str, Any]) -> bool:
    """Whether every enforcement layer that actually stops a write is live.

    An enforcement report's layers are not all the same kind. `write_gate`,
    `session_status` and `commit_lint` are mechanisms that run and either let
    a write through or refuse it; `instructions` (the CLAUDE.md managed block)
    only tells an agent what the rules are and stops nothing by itself, which
    is why `_enforcement_state` marks it `advisory: True`. A vault whose
    CLAUDE.md block went stale is not unhealthy in the sense this predicate is
    about; a vault whose write gate stopped running is -- so advisory layers
    are read past rather than folded into the same `all(...)`, and failing the
    headline on one would make it fire for something no mechanism was ever
    going to catch.

    Shared by `Health.status` (`bk status`) and `Reader.reader_status`
    (`/api/status`) so both surfaces report the same headline for the same
    installation, rather than each computing its own answer from a different
    slice of the same report. `ReaderStatusMatchesCanonicalTest` in
    `tests/test_fix_services.py` pins the two callers together by driving real
    enforcement states through both and asserting they agree.
    """

    return all(
        layer.get("active")
        for layer in enforcement.get("layers", [])
        if not layer.get("advisory")
    )


class Health:
    """Structural lint, vault status, and the derived-artefact report."""

    def __init__(
        self,
        vault: VaultPort,
        index: SearchIndexPort,
        retrieval: Retrieval,
        judgment_runner: JudgmentRunner,
        ledger: FreshnessLedger,
    ):
        self.vault = vault
        self.index = index
        self.retrieval = retrieval
        self.judgment_runner = judgment_runner
        self.ledger = ledger


    def lint(self, *, semantic: bool = False) -> dict[str, Any]:
        findings = self._mechanical_lint()
        self._review_drifted_code_citations(findings)
        semantic_report: dict[str, Any] | None = None
        withheld = 0
        if semantic:
            # `lint-semantic` judges consistency, never proposes a write (see
            # `Jobs.ask` for why the apply-proposal shape stays off here).
            #
            # Read as `local`, the boundary that excludes exactly what the
            # router refuses, for the reason `Jobs._judgment_context` gives: as
            # `human`, one never-ingest page anywhere in recall refused the
            # whole lint, and a page whose provenance does not resolve went to
            # the model. When the route those branches take is not local, read
            # again as `cloud`: a `local-only` page is then withheld and counted
            # instead of the router refusing the whole lint. The router's
            # refusal stays as the last defence.
            def read(consumer: str) -> dict[str, Any]:
                return self.retrieval.context(
                    "contradictions unsupported claims",
                    limit=20,
                    consumer=consumer,
                    include_apply_contract=False,
                )

            context = read("local")
            local_withheld = int(context["redacted"])
            if (
                self.judgment_runner.consumer_for(
                    job="lint-semantic", branches=context_branches(context)
                )
                == "cloud"
            ):
                context = read("cloud")
            withheld = int(context["redacted"])
            if withheld and not context["evidence"]:
                hint = (
                    "Run bk lint without --semantic, which checks every "
                    "page, or review the withheld pages yourself with "
                    "bk search -- they are withheld from models, not "
                    "from you"
                )
                if withheld > local_withheld:
                    hint += (
                        "; to let a model read local-only pages, map "
                        "job_models.lint-semantic.local-only to a local provider"
                    )
                raise PolicyError(
                    "Every page semantic lint would read is withheld from models "
                    "by its branch privacy policy",
                    details={"withheld_sources": withheld, "hint": hint},
                )
            if not context["evidence"]:
                self.judgment_runner.refuse_without_evidence(
                    job="lint-semantic",
                    branches=context_branches(context),
                    withheld=withheld,
                    nothing="No page in the vault matched what semantic lint reads",
                    next_step=(
                        "Run bk lint without --semantic, which checks every page, "
                        "or look for pages with bk search"
                    ),
                )
            semantic_report = self.judgment_runner.run(
                job="lint-semantic",
                branches=context_branches(context),
                variables={"context": json.dumps(context, ensure_ascii=False)},
            )
        result: dict[str, Any] = {
            "ok": not any(item.severity == "error" for item in findings),
            "findings": [item.to_dict() for item in findings],
            "semantic_report": semantic_report,
        }
        if semantic:
            # A count only: a path would name the page and its branch.
            result["withheld_sources"] = withheld
        return result

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
        freshness = self.ledger.snapshot()
        enforcement = self._enforcement_state()
        return {
            "vault": str(self.vault.root),
            "sources": len(records),
            "pending": sum(record.status == "pending" for record in records.values()),
            "wiki_pages": len(pages),
            "by_branch": dict(sorted(raw_counts.items())),
            "index": self.index.stats(),
            "freshness": freshness.summary(present=set(pages)),
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
            # above those rows and has to mean them too. See `enforcement_ok`
            # for why advisory layers are excluded from that half of the check.
            "healthy": lint_result["ok"] and enforcement_ok(enforcement),
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
        findings.extend(self._duplicate_slug_findings(self.vault.wiki_pages()))
        freshness = self.ledger.refresh_staleness()
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
            # `applied_hash` is the tracked/annotation question, asked of the
            # ledger rather than derived here from the shape of an entry. A
            # bare entry -- one an annotation created, carrying no hash -- is
            # not provenance, so it falls to the untracked branch instead of
            # buying the page silence in both.
            expected_hash = freshness.applied_hash(path)
            if expected_hash is None:
                findings.extend(self._untracked_page_findings(path, body))
            elif expected_hash != self.vault.wiki_version(path):
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
        for path, age_days in freshness.stale_pages():
            findings.append(
                LintFinding(
                    "wiki.stale",
                    f"Wiki page is stale ({age_days} days)",
                    severity="warning",
                    path=path,
                )
            )
        for path in freshness.orphans(set(self.vault.wiki_pages())):
            findings.append(
                LintFinding(
                    "freshness.orphaned",
                    "Freshness state tracks a page that no longer exists; "
                    "run bk reconcile",
                    severity="warning",
                    path=path,
                )
            )
        # `freshness` is the state `refresh_staleness` just committed and
        # `records` the registry this run already read, so the comparison sees
        # exactly the inputs lint reported on without re-reading either. Note
        # the fingerprint leaves out the status and age fields that refresh
        # rewrites on every run, or every lint would invent a stale projection.
        for artifact, report in self._projection_report(freshness, records).items():
            if not report["stale"]:
                continue
            # `stale` now covers `malformed` too, and the two need different
            # sentences: "built from a different set of wiki pages" is a lie
            # about a file that is not JSON, and it sends a reader looking for a
            # page that changed. The malformed report already carries the
            # sentence that describes it, so the code takes it rather than
            # restating it; the remedy is the same command either way.
            reason = report.get("reason")
            message = (
                f"Derived {artifact} {reason}; run {PROJECTION_COMMANDS[artifact]}"
                if isinstance(reason, str)
                else f"Derived {artifact} was built from a different set of wiki "
                f"pages; run {PROJECTION_COMMANDS[artifact]}"
            )
            findings.append(
                LintFinding(
                    PROJECTION_LINT_CODES[artifact],
                    message,
                    severity="warning",
                )
            )
        return findings

    def _untracked_page_findings(self, path: str, body: str) -> list[LintFinding]:
        """Report a `wiki/` page the freshness ledger has never heard of.

        Every page under `wiki/` is one of three things, and only the first two
        are legitimate: written by `bk apply`, which records a ledger entry;
        seeded by `bk init`, which records nothing; or written by something else,
        which is the bypass the write gate exists to stop.

        The seeded pages used to be recognised by their `type: system`
        frontmatter and skipped entirely. Both halves of that were wrong. Keying
        on frontmatter meant the file being checked decided whether it would be
        checked -- writing `type: "system"` into any path under `wiki/` bought
        permanent silence -- and skipping meant the two pages `bk init` really
        does write were never looked at either, so appending a fabricated claim
        to `wiki/index.md` produced no finding at all while `bk gate check-write`
        refused the same path. This is what the gate hook's header comment names
        as the reason it may fail open, so it has to be true for every page the
        gate covers, not for seven of nine.

        A seeded page that later gains a ledger entry -- an apply naming
        `wiki/index.md` in a proposal -- never reaches here: the caller's ledger
        branch handles it, and the hash comparison there is strictly stronger.
        So making these pages visible cannot make `bk apply` report findings
        against its own output.
        """

        if path not in SEEDED_SYSTEM_PAGES:
            return [
                LintFinding(
                    "wiki.outside_apply",
                    "Wiki page is not tracked by the apply gate",
                    path=path,
                )
            ]
        if _is_seeded_shape(body):
            return []
        return [
            LintFinding(
                "wiki.outside_apply",
                "Wiki page changed outside the apply gate",
                path=path,
            )
        ]

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

    def _duplicate_slug_findings(self, pages: list[str]) -> list[LintFinding]:
        """Pages sharing a stem across kinds, which mis-route every wiki link."""

        by_slug: dict[str, list[str]] = defaultdict(list)
        for path in pages:
            by_slug[PurePosixPath(path).stem].append(path)
        findings: list[LintFinding] = []
        for slug, paths in sorted(by_slug.items()):
            if len(paths) < 2:
                continue
            for path in sorted(paths):
                findings.append(
                    LintFinding(
                        "wiki.duplicate_slug",
                        f"Slug {slug!r} is used by {len(paths)} pages, so "
                        f"[[{slug}]] resolves to only one of them: "
                        + ", ".join(sorted(paths)),
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

        The never-downgrade rule that used to sit inside this mutator now lives
        in `mark_reviewed`, where the capture path reaches it too.
        """

        self.ledger.mark_reviewed(
            {
                finding.path: (
                    f"code changed: {finding.message.split(' has changed')[0]}"
                )
                for finding in findings
                if finding.code == "wiki.stale_code_citation" and finding.path
            }
        )

    def _projection_report(
        self, freshness: FreshnessSnapshot, records: dict[str, SourceRecord]
    ) -> dict[str, Any]:
        """Compare every derived artefact against the inputs it was built from.

        Four outcomes, and they are not the same thing:

        - `missing` — the artefact is not on disk. Nothing derives from the
          vault yet, so nothing can be out of date. `bk graph` and `bk views`
          are on-demand, and a vault that never ran them is not in error.
        - `malformed` — the artefact is on disk but is not the artefact. A
          `graph/graph.json` that is not JSON, or whose nodes and edges cannot
          be traversed; a `views/home.md` that no `bk views` wrote.
        - `stale` — the artefact exists, is usable, and was built from different
          inputs or from an unrecorded set. A projection whose provenance is
          unknown is treated as out of date rather than trusted.
        - `fresh` — the recorded fingerprint matches the current inputs.

        `malformed` exists because the other three answer a question the artefact
        can pass while being worthless. The fingerprint lives in
        `freshness.json`, so overwriting `graph/graph.json` with `{{{ not json
        at all` left every input untouched and this reported `fresh` — the same
        shape as a hook reported active from the existence of its file, and the
        same fault `bk code status` was fixed for in 0.6.1. Freshness and
        integrity are independent, so both are asked and integrity is asked
        first: comparing the inputs of an artefact nothing can read answers
        nothing whichever way it comes out.

        The read costs no I/O the check was not already paying. Existence used
        to be `wiki_version(anchor)`, which opens the file and hashes every byte
        of it; this opens the same file once and looks at what it read.

        `stale: True` on `malformed`, deliberately, and for the reason
        `CodeGraph.staleness` gives: the boolean is what a caller too terse to
        read `state` branches on, so the one answer it must never give for a
        broken artefact is the reassuring one. It is set from an allowlist of
        the states that mean "regenerate this", never from a denylist, so a
        state added later cannot default into looking healthy.

        Reported, never raised. `bk status` is the command you run when things
        are already broken, so an unreadable or vanished artefact has to arrive
        as a state.

        Each artefact is compared against its own inputs. A shared fingerprint
        would have to cover the union, so a change only one artefact renders
        would age both — and a projection that cries wolf gets ignored, which
        loses the signal by a different route than having no signal at all.
        """
        pages = freshness.pages()
        recorded = freshness.projections()
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
            detail: dict[str, Any] = {}
            fault = self._artefact_fault(artifact, anchor)
            if fault is _ARTEFACT_ABSENT:
                state = "missing"
            elif fault is not None:
                state = "malformed"
                detail = {
                    **fault,
                    # A fragment, not a sentence, and the same shape
                    # `CodeGraph.staleness` reports: `lint` prefixes it with the
                    # artefact it is about, and repeating the name there would
                    # print it twice.
                    "reason": (
                        f"is not what {PROJECTION_COMMANDS[artifact]} writes, "
                        "so it answers nothing"
                    ),
                    "command": PROJECTION_COMMANDS[artifact],
                }
            elif entry.get("source_hash") != expected:
                state = "stale"
            else:
                state = "fresh"
            item: dict[str, Any] = {
                "state": state,
                "stale": state in REGENERATE_STATES,
                "generated_at": generated_at,
                **detail,
            }
            age_days = _age_in_days(generated_at, now)
            if age_days is not None:
                item["age_days"] = age_days
            report[artifact] = item
        return report

    def _artefact_fault(self, artifact: str, anchor: str) -> dict[str, Any] | None:
        """Open a projection's anchor and say what is wrong with it.

        Three answers: `_ARTEFACT_ABSENT` for a file that is not there, a fault
        dict for one that cannot be what the artefact is, and `None` for one
        that can. A sentinel rather than a second return value because "absent"
        is not a degree of "malformed" — one is a vault that has not generated
        yet and is healthy, the other is a file to be replaced.

        Every read failure is absence. A directory where the anchor should be,
        a permission error, bytes that are not text: none of them is an artefact,
        and the remedy for all of them is the one command that would have written
        it. Nothing here may raise, so the guard is the broad one on purpose.
        """

        try:
            text = self.vault.read_text(anchor)
        except (OSError, ValueError):
            return _ARTEFACT_ABSENT
        return PROJECTION_INTEGRITY[artifact](text)

    def _enforcement_state(self) -> dict[str, Any]:
        """Report which enforcement layers are live for this vault, from disk.

        `hooks install` says what it wrote at the moment it ran; this says what
        is guarding the vault now. They diverge whenever a hook is edited away,
        a settings file is rewritten, or the vault is copied without its git
        directory -- and a layer that is off while everything still reads like
        success is precisely how an invariant ends up guarded by nothing.

        It answers for the agents that were actually installed, which the
        adapters under `.brain/` record, and for each of them from the workspace
        that agent's adapter names. Reading one hardcoded agent was the same
        class of failure this method exists to catch, one level up: an install
        for any other agent had its live layers reported off, against a workspace
        nobody chose and an instruction file nobody wrote.

        A vault with no adapter at all is reported as `DEFAULT_AGENT` -- nothing
        has been installed yet, and the useful answer is where the layers would
        land rather than an empty report that reads like a vault with no rules.
        `agent` is stamped on a layer only when more than one is installed,
        because it exists to disambiguate and in the overwhelmingly common case
        there is nothing to disambiguate.
        """

        agents = installed_agents(self.vault.root) or (DEFAULT_AGENT,)
        named = len(agents) > 1
        layers: list[dict[str, Any]] = []
        for agent in agents:
            for layer in self._agent_enforcement(agent):
                layers.append({**layer, "agent": agent} if named else layer)
        # Names, not rows: two agents sharing one repository see the same
        # `commit_lint` and listing it twice would read as two faults.
        inactive: list[str] = []
        for layer in layers:
            name = str(layer["layer"])
            if not layer["active"] and name not in inactive:
                inactive.append(name)
        # Beside `inactive` rather than folded into it: an outdated gate is in
        # both, an outdated session-status script only here, because it still
        # runs and only its report is suspect.
        outdated: list[str] = []
        for layer in layers:
            name = str(layer["layer"])
            if layer.get("outdated") and name not in outdated:
                outdated.append(name)
        return {
            "layers": layers,
            "inactive": inactive,
            "outdated": outdated,
            # Specifically the write gate, not "any non-advisory layer is on".
            # session_status is observability and commit_lint catches a bypass
            # only after the fact; neither one keeps a write out of the wiki, so
            # letting either imply `gated` would report a guarded vault that a
            # Write tool can still walk straight into.
            "gated": any(
                layer["active"] for layer in layers if layer["layer"] == WRITE_GATE
            ),
        }

    def _agent_enforcement(self, agent: str) -> list[dict[str, Any]]:
        """The layers one installed agent has, and whether each is live.

        Which layers those are is not this method's to decide: `hooks install`
        writes what `application.install` says an agent gets, and brainskit ships
        Claude Code hooks for `claude` and nothing equivalent for the others. An
        agent with no hooks therefore reports no `write_gate` row rather than an
        inactive one -- naming a layer that was never offered reads as a guard
        that fell off, which is a different and more alarming claim than the true
        one, and `gated` stays False either way.
        """

        install = agent_install(agent)
        root = self._agent_workspace(agent)
        settings_path = root / ".claude" / "settings.json"
        events: dict[str, list[dict[str, Any]]] = {}
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks = settings.get("hooks")
            if isinstance(hooks, dict):
                for event, groups in hooks.items():
                    events[event] = [
                        hook
                        for group in groups
                        if isinstance(group, dict)
                        for hook in group.get("hooks", [])
                        if isinstance(hook, dict)
                    ]
        except (OSError, ValueError, AttributeError, TypeError):
            # An unreadable or malformed settings file means "nothing is
            # registered", never an exception out of `bk status`.
            pass

        def registered_under(event: str, path: Path) -> dict[str, Any] | None:
            """The entry on ``event`` whose command runs ``path``, if any.

            Compared against the resolved path as well as the literal one: on
            macOS a vault under /var resolves to /private/var, so a settings
            file written with either spelling must still read as registered.
            A command may also wrap the script in a shell guard rather than
            naming it alone, so containment counts.

            The entry itself is returned, not a yes: `bk doctor` runs the
            command as registered, because a registration that does not survive
            the shell fails open while the script it names works fine by hand.
            """
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            candidates = {str(path), str(resolved)}
            for hook in events.get(event, []):
                command = str(hook.get("command", ""))
                entry = {
                    key: hook[key] for key in ("command", "args", "shell") if key in hook
                }
                if command in candidates or any(c in command for c in candidates):
                    return entry
                # The installer shell-quotes the path, and a quoted `'` is no
                # longer a substring of the command.
                if command_script(command) in candidates:
                    return entry
                # The command may spell the same file a different way. Compare
                # resolved forms, guarded because a command is often a shell
                # snippet rather than a bare path.
                try:
                    if Path(command).resolve() == resolved:
                        return entry
                except (OSError, ValueError):
                    continue
            return None

        recorded = (self.vault.root / install.adapter).is_file()

        def hook_layer(hook: AgentHook) -> dict[str, Any]:
            path = root / ".claude" / "hooks" / hook.script
            registration = registered_under(hook.event, path)
            active = path.is_file() and registration is not None
            detail = "active"
            # A registration naming a missing script is not "not installed":
            # the agent still runs the command, the shell exits 127, and
            # Claude Code does not treat that as a block.
            dangling = registration is not None and not path.is_file()
            if dangling:
                detail = (
                    f"{hook.script} is not installed but is still registered under "
                    f"{hook.event}, so the registered command cannot run"
                )
                if hook.layer == WRITE_GATE:
                    detail += " and every write goes through"
            elif not path.is_file():
                detail = f"{hook.script} is not installed"
            elif not active:
                detail = (
                    f"{hook.script} exists but is not registered under {hook.event}"
                )
            layer: dict[str, Any] = {
                "layer": hook.layer,
                "mechanism": hook.mechanism,
                "active": active,
                "detail": detail,
                # Named so a reader -- and `bk doctor`, which runs it -- can
                # reach the exact file this verdict is about instead of
                # rebuilding the path from assumptions about the workspace.
                "script": str(path),
                "workspace": str(root),
            }
            if registration is not None:
                layer["registration"] = registration
            if dangling:
                layer["hint"] = _reinstall_hint(agent, root, self.vault.root)
            if recorded and self._hook_outdated(hook, path, root):
                layer["outdated"] = True
                layer["hint"] = _reinstall_hint(agent, root, self.vault.root)
                if active:
                    # The gate is the layer `gated` means, and a stale copy is
                    # not the gate this version specifies: it may deny by rules
                    # since changed, or miss a path since added. Observability
                    # stays active and warns, because an out-of-date summary
                    # misreports the vault without letting a write through.
                    if hook.layer == WRITE_GATE:
                        layer["active"] = False
                        layer["detail"] = (
                            f"{hook.script} is older than the one this version "
                            "installs, so it may enforce old rules"
                        )
                    else:
                        layer["detail"] = (
                            f"{hook.script} is older than the one this version "
                            "installs, so what it reports may be wrong"
                        )
            return layer

        pre_commit = root / DEFAULT_GIT_HOOKS / "pre-commit"
        # A redirected hooks directory disqualifies the layer no matter what the
        # file says: git will not run it, so its contents prove nothing.
        redirected_hooks = redirected_git_hooks_path(root)
        try:
            pre_commit_text = pre_commit.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pre_commit_text = None
        commit_active = (
            redirected_hooks is None
            and pre_commit_text is not None
            and "lint" in pre_commit_text
        )
        # Judged by content alone, a generated hook naming a vault that does
        # not exist read `active` while every commit failed: a pre-0.8.0 hook
        # JSON-quoted a non-ASCII path, and a repository moved since install
        # names its old location. Reading the `--vault` sh will see catches
        # both without running anything.
        lints_elsewhere = (
            commit_active
            and pre_commit_text is not None
            and is_generated_pre_commit(pre_commit_text)
            and pre_commit_lints(pre_commit_text, root, self.vault.root) is False
        )
        if lints_elsewhere:
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
        instructions = root / install.instructions
        try:
            advisory_active = (
                instructions.is_file()
                and INSTRUCTION_START in instructions.read_text(
                    encoding="utf-8"
                )
            )
        except OSError:
            advisory_active = False

        commit_layer: dict[str, Any] = {
            "layer": COMMIT_LINT,
            "mechanism": COMMIT_LINT_MECHANISM,
            "active": commit_active,
            "detail": commit_detail,
            # The file git will actually run, so `bk doctor` can run it too.
            "script": str((redirected_hooks or root / DEFAULT_GIT_HOOKS) / "pre-commit"),
            "workspace": str(root),
        }
        if redirected_hooks is not None and not (redirected_hooks / "pre-commit").exists():
            commit_layer["hint"] = redirected_hooks_hint(self.vault.root, redirected_hooks)
        if lints_elsewhere and pre_commit_text is not None:
            named = pre_commit_vault(pre_commit_text, root)
            consequence = (
                "which does not exist, so every commit fails"
                if named is not None and not named.exists()
                else "not this vault, so its wiki is never linted at commit time"
            )
            commit_layer["detail"] = f"pre-commit lints {named}, {consequence}"
            commit_layer["hint"] = _reinstall_hint(agent, root, self.vault.root)
        if (
            recorded
            and (commit_active or lints_elsewhere)
            and self._pre_commit_outdated(pre_commit)
        ):
            # An outdated hook that still lints this vault stays active: the
            # rules live in `bk lint`, not in the hook.
            commit_layer["outdated"] = True
            commit_layer["hint"] = _reinstall_hint(agent, root, self.vault.root)
            if commit_active:
                commit_layer["detail"] = (
                    "pre-commit is older than the one this version installs"
                )

        layers = [
            *(hook_layer(hook) for hook in install.hooks),
            commit_layer,
            {
                "layer": INSTRUCTIONS,
                "mechanism": install.instructions_mechanism,
                "active": advisory_active,
                "advisory": True,
                "detail": "active" if advisory_active else "no managed block found",
            },
        ]
        if recorded and not root.exists():
            # Every row above is off for the one reason none of them can see:
            # the adapter names a workspace that is gone, usually because the
            # repository was moved. Each row saying "not installed" sent the
            # operator looking for files rather than at the adapter.
            detail = (
                f"the workspace {install.adapter} records, {root}, no longer "
                "exists; was the project moved?"
            )
            hint = self._moved_workspace_hint(agent)
            for layer in layers:
                layer["detail"] = detail
                layer["hint"] = hint
                layer["workspace_missing"] = True
        return layers

    def _moved_workspace_hint(self, agent: str) -> str:
        """The reinstall for an adapter whose workspace is gone.

        `--root` is required rather than optional here: without it the install
        lands beside the vault. The project the vault now sits in is named when
        there is one to name -- the same enclosing repository an install with no
        recorded workspace is reported against -- and left as a placeholder
        otherwise, because guessing wrong writes hooks nobody loads.
        """

        project = self._enclosing_project_root()
        root = (
            shlex.quote(str(project)) if (project / ".git").exists() else "<project>"
        )
        return f"bk hooks install --agent {agent} --root {root}"

    def _hook_outdated(self, hook: AgentHook, path: Path, workspace: Path) -> bool:
        """Whether a brainskit-generated hook differs from what an install writes now.

        "Installed and registered" was the whole question, so a copy written by
        an older brainskit read `active` for as long as it existed -- including
        a session-status script that under-reported lint errors after its
        template changed. The comparison is against `render_hook_script`, the
        renderer the installer itself writes with, so there is no second notion
        of "current" here to drift.

        A script without the generated marker belongs to the operator and is
        not judged; nor is one this build cannot render a template for.
        """

        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if HOOK_SENTINEL not in existing:
            return False
        try:
            expected = render_hook_script(hook.template, self.vault.root, workspace)
        except NotConfiguredError:
            return False
        return existing != expected

    def _pre_commit_outdated(self, path: Path) -> bool:
        """`_hook_outdated` for the git pre-commit hook, which has no template.

        A hook brainskit did not write is the operator's and is not judged.
        """

        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if not is_generated_pre_commit(existing):
            return False
        return existing != render_pre_commit(self.vault.root)

    def _agent_workspace(self, agent: str) -> Path:
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
        source = self.vault.root / adapter_path(agent)
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

