"""Freshness and projection state: what is current, and what has aged out.

Freshness answers "is this page still backed by what it was compiled from",
projections answer "does this generated artefact still describe the vault".
They share a module because they share a shape: both are a stored fingerprint
compared against the live inputs, and both are read by `lint` and `status`
through the same state file.

`FreshnessLedger` owns `.brain/freshness.json`. Everything that reads or writes
it goes through the ledger, because the two rules a reader depends on are
properties of the file as a whole and cannot be enforced by any one writer:

**An entry's vocabulary.** `status` (`fresh` | `review` | `stale`, and
`unknown` for a page with no entry), `updated_at` and `content_hash` and
`source_hashes` from the apply that wrote the page, `review_reason` and
`review_requested_at` from whatever asked a human to look, `age_days` from the
ageing pass, `last_resurfaced_at` from `bk resurface`.

**`content_hash` means the apply gate wrote this page, and here is what it
wrote.** Only `mark_applied` produces one. Every other transition annotates an
entry that may or may not already have one, and an entry lacking it is an
annotation rather than proof of provenance -- so `applied_hash` answers `None`
for it and `wiki.outside_apply` still reports the page. Populating the field
outside apply would make expected equal observed for bytes apply never saw,
which blesses the tamper instead of reporting it.

**A seed record is neither.** The pages `bk init` writes are brainskit's own
bytes but not the gate's, so they are recorded in a table of their own
(`seeded`) rather than as page entries: an entry would age, count in the
freshness summary and enter the projection fingerprint, and none of those is
true of a page compiled from no sources. `seeded_hash` answers the one question
a seed record exists for -- what did init write here -- and lint compares it
exactly as it compares `applied_hash`. Lint also writes it, and only for a page
still equal to the seed template.

**Never downgrade.** `review` is a weaker claim on attention than `stale`, and
`refresh_staleness` skips `review` entries -- so writing `review` over `stale`
does not lower a badge, it removes the page from the ageing loop for good.
`mark_reviewed` carries that rule once, for every caller.

The pure helpers below take the state dict and are shared by both sides; the
ledger composes them. ADR 0002 records the reasoning.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from brainskit.application.codegraph import _malformation
from brainskit.application.ports import VaultPort
from brainskit.domain.model import PageOperation, SourceRecord, utc_now

#: The state file the ledger owns, named once so no caller spells it.
STATE = "freshness"

GRAPH_PROJECTION = "graph/graph.json"

VIEWS_PROJECTION = "views"

# `views/` is a tree whose branch maps come and go with the branches that hold
# sources, so the artefact is identified by the one file every run writes:
# `views/home.md`. The recorder, the lint check and `status` all use this anchor,
# so "the artefact exists" means the same thing in all three.
PROJECTION_ANCHORS: dict[str, str] = {
    GRAPH_PROJECTION: GRAPH_PROJECTION,
    VIEWS_PROJECTION: "views/home.md",
}

PROJECTION_COMMANDS: dict[str, str] = {
    GRAPH_PROJECTION: "bk graph",
    VIEWS_PROJECTION: "bk views",
}

# Which registry fields each artefact actually renders, measured by mutating one
# field, regenerating, and diffing the output — not read off the source. Both
# artefacts also cover the whole wiki page set, so that is not per-artefact.
#
# The graph labels a raw node with `original_name` and carries `path`; `views`
# renders both plus the Status and Captured columns. `media_type` and `size`
# reach neither, so re-extracting a document does not age a projection.
PROJECTION_RAW_FIELDS: dict[str, tuple[str, ...]] = {
    GRAPH_PROJECTION: ("path", "original_name"),
    VIEWS_PROJECTION: ("path", "original_name", "status", "captured_at"),
}

PROJECTION_LINT_CODES: dict[str, str] = {
    GRAPH_PROJECTION: "graph.stale",
    VIEWS_PROJECTION: "views.stale",
}


def _graph_integrity(text: str) -> dict[str, Any] | None:
    """The first fault that makes `graph/graph.json` unreadable, or None.

    The detector is `codegraph._malformation`, imported rather than reimplemented.
    That is not code-sharing for its own sake: both artefacts are node/edge
    graphs whose readers index `node["id"]`, `edge["source"]`, `edge["target"]`
    and `edge["type"]` with `[...]` rather than `.get`, so the fields a stored
    graph must carry for anything to traverse it are the same set for the same
    reason. `graph_data` subscripts all four, and so do `infrastructure/graph.py`
    and the Neo4j and PostgreSQL writers. A second copy of the rule would let the
    two answers drift while both looked authoritative.

    Unlike the code graph, nothing in brainskit reads this file back — it is
    written for whatever consumes it next, an export, an integration or a
    viewer. So the check cannot be anchored to a reader's refusal the way
    `bk code status` is; it is anchored to the shape `bk graph` writes and every
    downstream reader subscripts.
    """

    try:
        graph = json.loads(text)
    except ValueError:
        return {"problem": "not JSON"}
    if not isinstance(graph, dict):
        return {"problem": f"not an object, but {type(graph).__name__}"}
    return _malformation(graph)


def _artefact_hash(text: str) -> str:
    """The identity a projection is stamped with when it is written.

    Hashed from text rather than read back as bytes so the writer, which holds
    the text it just wrote, and `Health`, which reads the anchor as text, compute
    it the same way.
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Structural checks, for the artefacts that have a structure to check. Whether
# the file is the one brainskit wrote is answered for every artefact by the
# stamped `artefact_hash`; this answers the narrower "can anything read it",
# which is still worth naming precisely for a graph (which field, which row) and
# is the only check an artefact written before stamping can be given. `views/`
# is markdown and has no structure beyond what the stamp already vouches for.
PROJECTION_INTEGRITY: dict[str, Callable[[str], dict[str, Any] | None]] = {
    GRAPH_PROJECTION: _graph_integrity,
}

#: The projection states whose remedy is to regenerate the artefact, and so the
#: ones that set the `stale` boolean. An allowlist rather than a denylist: a
#: state added later has to be classified on purpose instead of defaulting into
#: looking healthy, which is the direction that costs a user something.
REGENERATE_STATES: frozenset[str] = frozenset({"stale", "malformed", "unverified"})


def _freshness_summary(
    state: dict[str, Any], *, present: set[str] | None = None
) -> dict[str, int]:
    """Count freshness entries, ignoring pages that no longer exist on disk.

    Deleting a wiki page by hand leaves its entry behind. Counting it would
    report freshness for a page nobody can open; `lint` surfaces the orphan and
    `reconcile` removes it.
    """
    summary: dict[str, int] = {"fresh": 0, "review": 0, "stale": 0, "unknown": 0}
    for path, entry in state.get("pages", {}).items():
        if present is not None and path not in present:
            continue
        status = entry.get("status", "unknown") if isinstance(entry, dict) else "unknown"
        summary[status if status in summary else "unknown"] += 1
    return summary


def _fingerprint_row(namespace: str, *fields: str) -> str:
    """Encode one input row so that no two distinct rows can encode alike.

    The namespace tag comes first, which is what keeps the wiki and raw domains
    apart: a page path can never land in the position a content hash occupies,
    so no transposition of one into the other collides. Every field is then
    length-prefixed, so a separator appearing inside a value cannot fake a field
    boundary either — `("a", "b:c")` and `("a:b", "c")` stay distinct.
    """
    return "|".join([namespace, *(f"{len(field)}:{field}" for field in fields)])


def _projection_source_hash(
    pages: Any,
    records: dict[str, SourceRecord],
    raw_fields: tuple[str, ...],
) -> str:
    """Fingerprint the inputs a derived artefact is built from.

    Deliberately not mtime. A `git checkout` rewrites every mtime in the working
    tree, which would call a current graph stale and — after a checkout that
    restores an old graph — call a stale one current. Content hashes travel with
    the content, so they survive any clone, checkout or rsync.

    Two input domains, because both artefacts render both: the wiki page set,
    and the raw registry. A page-only fingerprint let a `bk capture` add a
    `raw:` node to the graph with nothing reporting it — the exact silent drift
    this check exists to catch. `raw_fields` names the registry fields the
    calling artefact actually renders, so a field only one of them shows cannot
    age the other.

    What stays out: the `status` and `age_days` that `refresh_staleness`
    rewrites on every `bk lint`. `age_days` reaches no artefact at all, and
    while a page's freshness badge does appear in `views/map/*.md`, that badge
    moves with the clock rather than with anything a user did — folding it in
    would make `views.stale` appear spontaneously on an untouched vault.

    Serialization is pinned so two processes agree: pages sorted by path, raw
    records sorted by content hash, pages before raw, newline between rows,
    UTF-8.
    """
    rows = _page_rows(pages)
    for content_hash in sorted(records):
        record = records[content_hash]
        rows.append(
            _fingerprint_row(
                "raw",
                content_hash,
                *(str(getattr(record, field)) for field in raw_fields),
            )
        )
    return _digest(rows)


def _page_rows(pages: Any) -> list[str]:
    """The page half of every projection digest, sorted by path."""

    rows: list[str] = []
    if not isinstance(pages, dict):
        return rows
    for path in sorted(pages):
        entry = pages[path]
        content_hash = entry.get("content_hash") if isinstance(entry, dict) else None
        if not isinstance(content_hash, str):
            content_hash = ""
        rows.append(_fingerprint_row("page", path, content_hash))
    return rows


def _projection_inputs(pages: Any, records: dict[str, SourceRecord]) -> dict[str, str]:
    """Per-domain digests recorded beside the fingerprint, to say what moved.

    `source_hash` decides stale-or-fresh; these only explain it. Without them
    a raw source moved and re-linked by `bk reconcile` was reported as the graph
    having been "built from a different set of wiki pages", when no page had
    changed.
    """

    identities = sorted(records)
    return {
        "pages": _digest(_page_rows(pages)),
        "raw_sources": _digest(identities),
        "raw_paths": _digest(
            [_fingerprint_row("raw", key, records[key].path) for key in identities]
        ),
    }


def _drift_reason(recorded: Any, current: Mapping[str, str]) -> str:
    """The fragment `lint` prints after "Derived <artefact>" for a stale one."""

    if not isinstance(recorded, dict) or not all(key in recorded for key in current):
        return "was built from different wiki pages or raw sources"
    changes = []
    if recorded["pages"] != current["pages"]:
        changes.append("wiki pages changed")
    if recorded["raw_sources"] != current["raw_sources"]:
        changes.append("raw sources were captured or forgotten")
    elif recorded["raw_paths"] != current["raw_paths"]:
        changes.append("a raw source moved")
    if not changes:
        changes.append("a raw source it shows was renamed or changed status")
    return "was built before " + " and ".join(changes)


def _digest(rows: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _age_in_days(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).days


def _orphaned_freshness(state: dict[str, Any], present: set[str]) -> list[str]:
    return sorted(path for path in state.get("pages", {}) if path not in present)


class FreshnessSnapshot:
    """One read of the ledger, and every question asked of that read.

    Request-scoped by the same convention `PrivacyBoundary` carries: a snapshot
    is taken, questioned, and dropped -- never held across a write. Holding one
    is what lets `lint` ask about a thousand pages while opening the state file
    once, which is the reason this is a value rather than more methods on the
    ledger.

    Every accessor tolerates a malformed entry, because the file is on disk and
    a hand edit is exactly the condition `lint` exists to survive. A non-dict
    entry answers the same as a missing one.
    """

    __slots__ = ("_state",)

    def __init__(self, state: dict[str, Any]):
        self._state = state

    @property
    def state(self) -> dict[str, Any]:
        """The raw state, for the one caller that reports the file itself."""

        return self._state

    def pages(self) -> dict[str, Any]:
        pages = self._state.get("pages", {})
        return pages if isinstance(pages, dict) else {}

    def projections(self) -> dict[str, Any]:
        recorded = self._state.get("projections", {})
        return recorded if isinstance(recorded, dict) else {}

    def entry(self, path: str) -> dict[str, Any] | None:
        entry = self.pages().get(path)
        return entry if isinstance(entry, dict) else None

    def applied_hash(self, path: str) -> str | None:
        """The hash `bk apply` recorded for this page, or None if it never did.

        The tracked/annotation question, asked in one place so no reader has to
        re-derive it. `content_hash` is written by `mark_applied` and by nothing
        else, so its presence is what distinguishes a page the gate produced
        from a page some other writer merely annotated.

        `None` therefore covers three cases that are the same case to a reader:
        no entry at all, an entry that is not even a dict, and an entry that
        exists but carries no hash. All three mean the ledger cannot say what
        this page looked like when it was written, so it cannot vouch for what
        is on disk now. Reading the third as "tracked" is what let a
        hand-written page under `wiki/` disappear from `wiki.outside_apply` the
        moment any capture happened to relate to it -- the page was laundered by
        an annotation, past the very check that backstops the write gate failing
        open.
        """

        entry = self.entry(path)
        if entry is None:
            return None
        content_hash = entry.get("content_hash")
        return content_hash if isinstance(content_hash, str) and content_hash else None

    def seeded_hash(self, path: str) -> str | None:
        """The hash `bk init` recorded for a page it seeded, or None.

        A separate question from `applied_hash`, and asked after it: once the
        gate writes a seeded path, what the gate wrote is the newer claim.
        """

        seeded = self._state.get("seeded")
        record = seeded.get(path) if isinstance(seeded, dict) else None
        content_hash = record.get("hash") if isinstance(record, dict) else None
        return content_hash if isinstance(content_hash, str) and content_hash else None

    def status(self, path: str) -> str:
        entry = self.entry(path)
        status = entry.get("status") if entry else None
        return str(status) if isinstance(status, str) else "unknown"

    def updated_at(self, path: str) -> str | None:
        entry = self.entry(path)
        return entry.get("updated_at") if entry else None

    def stale_pages(self) -> list[tuple[str, Any]]:
        """Every page currently aged out, with the age lint reports."""

        return [
            (path, entry.get("age_days", "?"))
            for path, entry in self.pages().items()
            if isinstance(entry, dict) and entry.get("status") == "stale"
        ]

    def summary(self, *, present: set[str] | None = None) -> dict[str, int]:
        return _freshness_summary(self._state, present=present)

    def orphans(self, present: set[str]) -> list[str]:
        return _orphaned_freshness(self._state, present)


class FreshnessLedger:
    """The one owner of `.brain/freshness.json`.

    Five callers used to read and write this file directly, each restating as
    much of the entry vocabulary as it happened to need. Two of them wrote the
    same `review` transition and only one carried the never-downgrade rule; two
    created bare entries that a third then read as proof the apply gate had
    written the page. Both defects are the same shape -- a rule that lives in a
    writer rather than in the thing being written -- so both are fixed by giving
    the file an owner and naming the transitions after the intent that reaches
    them.

    Built once, at the composition root, and handed to its collaborators. It
    holds no state of its own: every method takes a fresh read, so the ledger is
    safe to keep for a process's lifetime while a `FreshnessSnapshot` is not.
    """

    def __init__(self, vault: VaultPort):
        self.vault = vault

    def snapshot(self) -> FreshnessSnapshot:
        return FreshnessSnapshot(self.vault.read_state(STATE))

    # ------------------------------------------------------------ transitions

    def mark_applied(
        self,
        operations: Sequence[PageOperation],
        pages: Mapping[str, str],
    ) -> dict[str, dict[str, Any]]:
        """The complete entry, and the only writer that records provenance.

        Returned rather than committed, and that is not an inconsistency with
        the other transitions. These entries belong to the apply transaction:
        `commit_wiki_batch` takes the registry lock before the freshness lock
        and both are blocking `flock`s, so a second writer reaching for
        freshness from inside that transaction would invert the order and
        deadlock -- the same ordering `record_projection` documents from the
        other side. The gate hands what this builds straight to the transaction,
        which merges it over any prior entry, so an annotation already on the
        page survives the apply that supersedes it.

        `review_reason` is cleared explicitly: an apply answers the request a
        reviewer was asked to look at, and leaving the old reason behind would
        keep pointing a human at work that is done.
        """

        now = utc_now()
        return {
            operation.relative_path: {
                "status": "fresh",
                "updated_at": now,
                "content_hash": hashlib.sha256(
                    pages[operation.relative_path].encode("utf-8")
                ).hexdigest(),
                "source_hashes": list(operation.source_hashes),
                "review_reason": None,
            }
            for operation in operations
        }

    def record_seeded(self, hashes: Mapping[str, str]) -> None:
        """Record what `bk init` seeded at these paths, as path -> content hash.

        Not `mark_applied`: the gate did not write these pages, and an applied
        entry carries freshness (status, age, a row in every projection
        fingerprint) that a page compiled from no sources does not have. A seed
        record holds only the bytes, so later lints compare against them.

        Reached from lint, and only for a page equal to the template some
        release of init wrote (`is_seed_template`) -- that equality is what makes
        this a record of init's output rather than a blessing of whatever is on
        disk. Not from `FileVault.initialize`: infrastructure does not reach up
        into the application layer, and it needs no second route, because
        `bk init` generates the views before it returns and that runs a lint.
        A vault initialised before seeds were recorded takes the same path on
        its first `bk lint` or `bk status`.
        """

        if not hashes:
            return
        recorded_at = utc_now()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            seeded = state.get("seeded")
            if not isinstance(seeded, dict):
                seeded = {}
                state["seeded"] = seeded
            for path, content_hash in hashes.items():
                seeded[path] = {"hash": content_hash, "recorded_at": recorded_at}
            return state

        self.vault.mutate_state(STATE, mutate)

    def mark_reviewed(self, reasons: Mapping[str, str]) -> list[str]:
        """Ask a human to look at these pages, without ever lowering a badge.

        Reached from two places that had no idea they were the same transition:
        a capture whose content relates to a page (`bk capture`), and a page
        whose cited code has moved on (`bk lint`). Both want the same durable
        state -- a lint warning is read once by whoever ran lint, `review` is
        carried by the vault until someone acts on it -- so code drift joins the
        existing queue rather than inventing a second one.

        Never downgrades. A page already `stale` has a stronger claim on
        attention, and `refresh_staleness` skips `review`, so overwriting would
        have taken it out of the ageing loop until the next `bk apply`. That
        left `review` reachable only from `fresh`, which is the coherent
        reading: a current page flagged for a human should not also age.

        Takes a reason per path because the two callers differ there and only
        there -- a capture names one source for every page it touched, code
        drift names the file that moved for each. One call, one lock.

        Returns the paths actually moved, so a caller can report what it did.
        """

        if not reasons:
            return []
        moved: list[str] = []
        requested_at = utc_now()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            pages = state.setdefault("pages", {})
            for path, reason in reasons.items():
                entry = pages.get(path)
                if not isinstance(entry, dict):
                    entry = {}
                    pages[path] = entry
                if entry.get("status") == "stale":
                    continue
                entry["status"] = "review"
                entry["review_reason"] = reason
                entry["review_requested_at"] = requested_at
                moved.append(path)
            return state

        self.vault.mutate_state(STATE, mutate)
        return moved

    def record_resurfaced(self, page: str) -> bool:
        """Note that `bk resurface` put this page in front of someone.

        An annotation: it records an event about the page and says nothing about
        whether the page still matches its sources, so it never touches
        `status` and never invents a `content_hash`. A page the ledger has never
        heard of gets an entry only if it really exists under `wiki/` -- a model
        naming a path that is not there must not conjure one.
        """

        known = page in self.snapshot().pages() or page in self.vault.wiki_pages()
        if not known:
            return False
        recorded_at = utc_now()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            pages = state.setdefault("pages", {})
            entry = pages.get(page)
            if not isinstance(entry, dict):
                entry = {}
                pages[page] = entry
            entry["last_resurfaced_at"] = recorded_at
            return state

        self.vault.mutate_state(STATE, mutate)
        return True

    def refresh_staleness(self) -> FreshnessSnapshot:
        """Re-age every entry against the clock, and return what was committed.

        `review` entries are skipped: a page a human has been asked to look at
        is already at the top of the queue, and ageing it would either overwrite
        that request or race it. With `mark_reviewed` refusing to downgrade,
        `review` is only ever reached from `fresh`, so nothing that had aged out
        can hide here.
        """

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
                entry["status"] = "stale" if age_days >= stale_after_days else "fresh"
                entry["age_days"] = age_days
            return state

        return FreshnessSnapshot(self.vault.mutate_state(STATE, mutate))

    def record_projection(self, artifact: str, anchor_text: str) -> None:
        """Stamp a derived artefact with the inputs it was just built from.

        And with its own identity: `artefact_hash` is the hash of the anchor
        text the caller just wrote, so "is this the file brainskit wrote" is
        answered exactly rather than by the file's shape. A shape match let a
        `views/home.md` gutted to its marker line read `fresh`.

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
            pages = state.get("pages", {})
            projections[artifact] = {
                "generated_at": utc_now(),
                "source_hash": _projection_source_hash(pages, records, raw_fields),
                "artefact_hash": _artefact_hash(anchor_text),
                "inputs": _projection_inputs(pages, records),
            }
            return state

        self.vault.mutate_state(STATE, mutate)

    def drop(self, paths: Iterable[str]) -> None:
        """Forget entries for pages that are gone.

        Freshness is keyed by path while the registry is keyed by content hash,
        so a wiki page removed outside the gate leaves an entry that can never
        be revived. `bk reconcile` is where that is healed; `bk lint` reports it
        as `freshness.orphaned` in the meantime.
        """

        dropped = list(paths)
        if not dropped:
            return

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            pages = state.setdefault("pages", {})
            for path in dropped:
                pages.pop(path, None)
            return state

        self.vault.mutate_state(STATE, mutate)
