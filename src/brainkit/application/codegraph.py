"""The code graph: a projection of a repository, and the traversal over it.

Brainkit does not parse code and should not start. Its atom is a claim with a
citation, curated and gated; a code fact is derived, wholesale, and expires on
the next commit. Putting one inside the other would churn the freshness system
it was meant to feed — so the parser stays outside and this module owns only the
two things brainkit is already good at and an extractor is not: **saying when
the artefact stopped being true**, and **deciding who may read it**.

The division of labour, stated once:

    graphify (or anything else)  →  produces a graph by static analysis
    brainkit                     →  owns the contract, the freshness, the
                                    privacy boundary, and the queries

`import_graph` is the seam. It accepts the extractor's shape, keeps only what a
code graph may contain, and records the hash of every file the graph was built
from. `staleness` re-reads those files, which is why `bk status` can say the
graph describes a repository that has moved on — the single thing the companion
analysis found no code-graph tool doing.

`build` is the same seam with the extraction step attached: it calls a
`CodeExtractorPort` in-process and hands the result straight to `import_graph`,
so a producer that runs inside brainkit is bound by exactly the same boundary
as one that ran separately and was piped in through `bk code import`. Neither
path is more trusted than the other.

Traversal is deliberately plain: breadth-first over an adjacency map, no
dependency, no database. The algorithms are trivial; what is not trivial is that
they answer under a consumer, like every other read in this vault.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from brainkit.application.ports import CodeExtractorPort, VaultPort
from brainkit.application.privacy import _validate_consumer
from brainkit.domain.model import (
    NotFoundError,
    PrivacyMode,
    ValidationError,
    utc_now,
)

# The extractor's own id recipe, imported rather than copied. Node identity has
# to agree with what the extractor minted — `import_graph` cannot re-derive an
# id from a payload that never carries enough to reconstruct one — and the
# vendoring rule (`infrastructure/codeanalysis/NOTICE`) forbids a hand-kept
# duplicate that could drift the moment the recipe changes. This is the one
# application module that reaches into infrastructure; `normalize_id` is a
# pure function (`re` + `unicodedata`, nothing else), so the dependency costs
# nothing at runtime and answers to no port.
from brainkit.infrastructure.codeanalysis import normalize_id

if TYPE_CHECKING:
    import networkx as nx  # type: ignore[import-untyped]

CODE_PROJECTION = "graph/code.json"
CODE_PROJECTION_COMMAND = "bk code import <graph.json>"

#: Node kinds a code graph may hold. An extractor that also indexed prose will
#: offer `document`, `concept` and `rationale` nodes; those are claims, and
#: claims belong to the wiki where they are cited, gated and reviewed. Dropping
#: them here rather than trusting the caller to pass `--code-only` makes the
#: boundary a property of the vault instead of a property of the command line.
CODE_NODE_KINDS = frozenset({"code"})

#: A code graph carries repository paths, so it never leaves the machine unless
#: an operator says otherwise. Stated here rather than inferred from a branch:
#: the graph is not filed under one.
CODE_PRIVACY = PrivacyMode.LOCAL_ONLY

SCHEMA_VERSION = 1


class CodeGraph:
    """Import, freshness and traversal for the repository graph."""

    def __init__(self, vault: VaultPort, extractor: CodeExtractorPort | None = None):
        self.vault = vault
        self.extractor = extractor

    # ------------------------------------------------------------------ import

    def build(self, paths: list[Path] | None = None) -> dict[str, Any]:
        """Extract in-process, then import the result through the same seam.

        Extraction is entirely the port's job — this method never touches a
        node or an edge before normalisation does, so `build` and `bk code
        import` share every boundary rule by construction rather than by two
        implementations agreeing to.

        A scoped build (`paths` given) only re-extracts that subset, so its
        result is merged into whatever graph is already stored rather than
        replacing it outright: without that, `bk code build one/file.py`
        would silently shrink a whole-repository graph down to one file, and
        every reader after it (`status`, `hubs`, `affected`, …) would report
        that narrowed graph as complete rather than as a graph missing
        everything outside the scope just asked for.
        """

        if self.extractor is None:
            raise ValidationError(
                "No code extractor is configured for this vault",
                details={"hint": f"Extract separately and use {CODE_PROJECTION_COMMAND}"},
            )
        if not self.extractor.available():
            raise ValidationError(
                "Code extraction requires the optional tree-sitter grammars",
                details={"hint": "Install them with: pip install brainkit[code]"},
            )
        payload = self.extractor.extract(
            self.vault.code_root(), paths, cache_root=self.vault.code_cache_dir
        )
        nodes, dropped = self._nodes(payload)
        edges = self._edges(payload, known=set(nodes))
        if paths is not None:
            nodes, edges = self._merge_scoped(nodes, edges, paths)
        if not nodes:
            raise ValidationError(
                "No code nodes in the imported graph",
                details={
                    "dropped_non_code_nodes": dropped,
                    "hint": "Extract with --code-only; prose belongs in the wiki",
                },
            )
        return self._write(nodes, edges, dropped)

    def _merge_scoped(
        self,
        nodes: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
        paths: list[Path],
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Fold a scoped extraction into the stored graph, not over it.

        Everything the stored graph knows about files outside `paths` is kept
        untouched; everything it knew about files inside `paths` is dropped
        and replaced wholesale by this run's result, including a file that
        now contributes zero nodes (its old ones are stale, not merely
        unmentioned). Nothing to merge into (no stored graph, or `paths`
        resolved to nothing recognisable) is not an error — it is the first
        build, scoped or not, and the fresh result stands on its own.
        """

        stored = self._maybe_read()
        if stored is None:
            return nodes, edges
        scope_roots = self._scope_roots(paths)
        if not scope_roots:
            return nodes, edges

        def in_scope(path: str) -> bool:
            return any(
                path == root or path.startswith(f"{root}/") for root in scope_roots
            )

        merged_nodes = {
            str(node["id"]): node
            for node in stored.get("nodes", [])
            if isinstance(node, dict) and not in_scope(str(node.get("path", "")))
        }
        merged_nodes.update(nodes)

        combined_edges = [
            *edges,
            *(
                edge
                for edge in stored.get("edges", [])
                if isinstance(edge, dict) and not in_scope(str(edge.get("path", "")))
            ),
        ]
        seen: set[tuple[str, str, str]] = set()
        merged_edges = []
        for edge in combined_edges:
            source, target = str(edge.get("source")), str(edge.get("target"))
            if source not in merged_nodes or target not in merged_nodes:
                continue
            key = (source, target, str(edge.get("type")))
            if key in seen:
                continue
            seen.add(key)
            merged_edges.append(edge)
        merged_edges.sort(key=lambda item: (item["source"], item["target"], item["type"]))
        return merged_nodes, merged_edges

    def _scope_roots(self, paths: list[Path]) -> list[str]:
        """`paths`, as code-root-relative posix strings a node path can match."""

        root = self.vault.code_root().resolve()
        roots = []
        for target in paths:
            candidate = (target if target.is_absolute() else root / target).resolve()
            try:
                roots.append(candidate.relative_to(root).as_posix())
            except ValueError:
                continue  # Outside code_root; the extractor already ignored it.
        return roots

    def import_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalise an extractor's graph and record what it was built from."""

        nodes, dropped = self._nodes(payload)
        if not nodes:
            raise ValidationError(
                "No code nodes in the imported graph",
                details={
                    "dropped_non_code_nodes": dropped,
                    "hint": "Extract with --code-only; prose belongs in the wiki",
                },
            )
        edges = self._edges(payload, known=set(nodes))
        return self._write(nodes, edges, dropped)

    def _write(
        self,
        nodes: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
        dropped: int,
    ) -> dict[str, Any]:
        # Hashed at import, compared at status. This is the artefact's input
        # set, and the only reason the graph can ever be called stale.
        files = {
            path: self.vault.code_hash(path) or ""
            for path in sorted({node["path"] for node in nodes.values()})
        }
        missing = sorted(path for path, digest in files.items() if not digest)

        graph = {
            "version": SCHEMA_VERSION,
            "built_at": utc_now(),
            "privacy": CODE_PRIVACY.value,
            "nodes": sorted(nodes.values(), key=lambda item: str(item["id"])),
            "edges": edges,
            "files": files,
            "fingerprint": _fingerprint(files),
        }
        self.vault.write_generated(
            CODE_PROJECTION, json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
        )
        return {
            "written": CODE_PROJECTION,
            "nodes": len(nodes),
            "edges": len(edges),
            "files": len(files),
            "dropped_non_code_nodes": dropped,
            "unreadable_files": missing,
        }

    def _vault_prefix(self) -> str:
        """Where the vault sits inside the code root, as a path prefix.

        An extractor pointed at the repository has no idea one of those
        directories is the vault asking the question, so it indexes
        `.brain/schema.json` and `graph/code.json` as source and they arrive as
        the most connected nodes in the graph. Only brainkit knows where its own
        vault is, which makes excluding it brainkit's job.
        """

        try:
            relative = self.vault.root.resolve().relative_to(self.vault.code_root().resolve())
        except ValueError:
            return ""
        prefix = relative.as_posix()
        return "" if prefix == "." else f"{prefix}/"

    def _nodes(self, payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], int]:
        nodes: dict[str, dict[str, Any]] = {}
        dropped = 0
        vault_prefix = self._vault_prefix()
        for raw in payload.get("nodes", []):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("file_type", "code")) not in CODE_NODE_KINDS:
                dropped += 1
                continue
            # Normalised on the way in, not trusted as given: a payload from
            # `bk code import` may spell the same id with different casing or
            # punctuation than the extractor's own recipe would, and an edge
            # naming the un-normalised spelling would then find no endpoint.
            node_id = normalize_id(str(raw.get("id", "")))
            path = str(raw.get("source_file", "")).strip()
            if not node_id or not path:
                dropped += 1
                continue
            if vault_prefix and path.startswith(vault_prefix):
                dropped += 1
                continue
            nodes[node_id] = {
                "id": node_id,
                "label": str(raw.get("label", node_id)),
                "path": path,
                "line": _line(raw.get("source_location")),
            }
        return nodes, dropped

    def _edges(
        self, payload: dict[str, Any], *, known: set[str]
    ) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        edges: list[dict[str, Any]] = []
        for raw in payload.get("links", []) + payload.get("edges", []):
            if not isinstance(raw, dict):
                continue
            source = normalize_id(str(raw.get("source", "")))
            target = normalize_id(str(raw.get("target", "")))
            # An edge to a node that was dropped for being prose would leave a
            # dangling endpoint that every traversal then has to guard against.
            if source not in known or target not in known:
                continue
            relation = str(raw.get("relation") or raw.get("type") or "references")
            key = (source, target, relation)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "type": relation,
                    "path": str(raw.get("source_file", "")),
                    "line": _line(raw.get("source_location")),
                }
            )
        return sorted(edges, key=lambda item: (item["source"], item["target"], item["type"]))

    # -------------------------------------------------------------- freshness

    def staleness(self) -> dict[str, Any]:
        """Whether the graph still describes the repository it was built from.

        Reported in the same three states and the same fields as every other
        projection — `state`, `stale`, `generated_at` — because a caller walking
        the block should not have to know which artefact it is looking at. What
        differs is only the input set: the vault's own projections fingerprint
        pages and the registry, this one fingerprints the files it indexed.

        Every one of those files is re-read. That is the cost of an honest
        answer: an mtime rule would call a graph stale after any `git checkout`,
        and a stored git revision would miss uncommitted edits entirely.
        """

        graph = self._maybe_read()
        if graph is None:
            return {
                "state": "missing",
                "stale": False,
                "generated_at": None,
                "command": CODE_PROJECTION_COMMAND,
            }

        generated_at = graph.get("built_at")
        files = graph.get("files", {})
        if not isinstance(files, dict):
            return {
                "state": "stale",
                "stale": True,
                "generated_at": generated_at if isinstance(generated_at, str) else None,
                "reason": "the graph records no input set, so it cannot be verified",
                "command": CODE_PROJECTION_COMMAND,
            }

        changed: list[str] = []
        removed: list[str] = []
        for path, recorded in sorted(files.items()):
            observed = self.vault.code_hash(str(path))
            if observed is None:
                removed.append(str(path))
            elif observed != recorded:
                changed.append(str(path))

        fresh = not changed and not removed
        return {
            "state": "fresh" if fresh else "stale",
            "stale": not fresh,
            "generated_at": generated_at if isinstance(generated_at, str) else None,
            "files": len(files),
            # Truncated because this rides on every `bk status`; the totals say
            # how much was withheld rather than leaving the reader to guess.
            "changed": changed[:20],
            "removed": removed[:20],
            "changed_total": len(changed),
            "removed_total": len(removed),
            **({} if fresh else {"command": CODE_PROJECTION_COMMAND}),
        }

    # -------------------------------------------------------------- traversal

    def affected(
        self, symbol: str, *, depth: int = 2, consumer: str = "local"
    ) -> dict[str, Any]:
        """What reaches `symbol`, following edges backwards.

        The question a rename actually asks. Forward edges answer "what does
        this use"; only the reverse answers "what breaks if I change it".
        """

        graph = self._read(consumer)
        start = self._resolve(graph, symbol)
        reverse: dict[str, list[dict[str, Any]]] = {}
        for edge in graph["edges"]:
            reverse.setdefault(str(edge["target"]), []).append(edge)

        nodes = {str(node["id"]): node for node in graph["nodes"]}
        seen = {start}
        found: list[dict[str, Any]] = []
        frontier = deque([(start, 0)])
        while frontier:
            current, distance = frontier.popleft()
            if distance >= max(1, depth):
                continue
            for edge in reverse.get(current, []):
                source = str(edge["source"])
                if source in seen:
                    continue
                seen.add(source)
                node = nodes.get(source, {"id": source})
                found.append({**node, "via": edge["type"], "depth": distance + 1})
                frontier.append((source, distance + 1))
        return {
            "symbol": nodes[start]["label"],
            "id": start,
            "depth": depth,
            "consumer": consumer,
            "count": len(found),
            "affected": sorted(found, key=lambda item: (item["depth"], str(item["id"]))),
        }

    def path(self, source: str, target: str, *, consumer: str = "local") -> dict[str, Any]:
        """Shortest edge chain from one symbol to another, in either direction.

        Undirected on purpose: "how are these two related" is the question, and
        an answer that exists only if you happened to name them in the right
        order is not an answer.
        """

        graph = self._read(consumer)
        start, goal = self._resolve(graph, source), self._resolve(graph, target)
        neighbours: dict[str, list[tuple[str, str]]] = {}
        for edge in graph["edges"]:
            a, b = str(edge["source"]), str(edge["target"])
            neighbours.setdefault(a, []).append((b, str(edge["type"])))
            neighbours.setdefault(b, []).append((a, str(edge["type"])))

        previous: dict[str, tuple[str, str]] = {}
        seen = {start}
        frontier = deque([start])
        while frontier and goal not in seen:
            current = frontier.popleft()
            for neighbour, relation in neighbours.get(current, []):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                previous[neighbour] = (current, relation)
                frontier.append(neighbour)

        nodes = {str(node["id"]): node for node in graph["nodes"]}
        if goal not in seen:
            return {"found": False, "from": start, "to": goal, "consumer": consumer}

        chain: list[dict[str, Any]] = []
        cursor = goal
        while cursor != start:
            parent, relation = previous[cursor]
            chain.append({**nodes.get(cursor, {"id": cursor}), "via": relation})
            cursor = parent
        chain.append(nodes.get(start, {"id": start}))
        chain.reverse()
        return {
            "found": True,
            "consumer": consumer,
            "hops": len(chain) - 1,
            "path": chain,
        }

    def hubs(self, *, top: int = 10, consumer: str = "local") -> dict[str, Any]:
        """Nodes with the most connections — what is load-bearing here."""

        graph = self._read(consumer)
        degree: dict[str, int] = {str(node["id"]): 0 for node in graph["nodes"]}
        for edge in graph["edges"]:
            for endpoint in (str(edge["source"]), str(edge["target"])):
                if endpoint in degree:
                    degree[endpoint] += 1
        nodes = {str(node["id"]): node for node in graph["nodes"]}
        ranked = sorted(degree.items(), key=lambda item: (-item[1], item[0]))[: max(1, top)]
        return {
            "consumer": consumer,
            "hubs": [
                {**nodes[node_id], "edges": count} for node_id, count in ranked
            ],
        }

    # ------------------------------------------------------------ vendored analysis

    def communities(
        self, *, resolution: float = 1.0, consumer: str = "local"
    ) -> dict[str, Any]:
        """Group the graph into structurally cohesive clusters.

        `affected`/`path`/`hubs` all answer a question about one symbol;
        this is the question brainkit's own plain traversal has no way to
        answer at all — how the repository breaks into parts — so it is
        delegated whole to the vendored `graphify.cluster` rather than
        approximated with a simpler algorithm brainkit would then own and
        have to keep matching.
        """

        cluster_mod, _ = _load_analysis()
        graph = self._read(consumer)
        G = _networkx_graph(graph)
        communities = cluster_mod.cluster(G, resolution=resolution)
        cohesion = cluster_mod.score_all(G, communities)
        labels = cluster_mod.label_communities_by_hub(G, communities)
        nodes = {str(node["id"]): node for node in graph["nodes"]}
        return {
            "consumer": consumer,
            "count": len(communities),
            "communities": [
                {
                    "id": cid,
                    "label": labels.get(cid, f"Community {cid}"),
                    "size": len(members),
                    "cohesion": round(cohesion.get(cid, 0.0), 4),
                    "members": [nodes[member] for member in members if member in nodes],
                }
                for cid, members in sorted(communities.items())
            ],
        }

    def cycles(
        self, *, max_length: int = 5, top: int = 20, consumer: str = "local"
    ) -> dict[str, Any]:
        """Import cycles among files.

        A cycle is a property of the file graph the symbol-level edges
        imply, not of one symbol's neighbourhood — `affected` stops at
        "reachable", never "reachable and back again" — so this is the other
        question brainkit's own traversal has no way to ask. Collapsing to
        file level and enumerating the cycles is
        `graphify.analyze.find_import_cycles`'s job; brainkit's contribution
        is only the boundary this reads under.
        """

        _, analyze_mod = _load_analysis()
        graph = self._read(consumer)
        G = _networkx_graph(graph)
        cycles = analyze_mod.find_import_cycles(
            G, max_cycle_length=max_length, top_n=top
        )
        return {"consumer": consumer, "count": len(cycles), "cycles": cycles}

    def diff(
        self, against: dict[str, Any] | None = None, *, consumer: str = "local"
    ) -> dict[str, Any]:
        """Structural change between the stored graph and a second one.

        `staleness` says a file's hash moved; this says what that produced —
        which nodes and edges appeared or disappeared. The second graph is
        either supplied directly (an external payload, normalised through
        the same `_nodes`/`_edges` boundary `import_graph` uses) or, when
        omitted, a fresh in-process extraction of the repository as it
        stands right now — the same extractor `build()` calls, but nothing
        is written: `diff` only ever compares against the stored graph, it
        never replaces it.
        """

        _, analyze_mod = _load_analysis()
        old_graph = self._read(consumer)

        if against is not None:
            new_nodes, _dropped = self._nodes(against)
            new_edges = self._edges(against, known=set(new_nodes))
        else:
            if self.extractor is None:
                raise ValidationError(
                    "No code extractor is configured for this vault",
                    details={"hint": "Pass a graph to diff against, or configure one"},
                )
            if not self.extractor.available():
                raise ValidationError(
                    "Code extraction requires the optional tree-sitter grammars",
                    details={"hint": "Install them with: pip install brainkit[code]"},
                )
            payload = self.extractor.extract(
                self.vault.code_root(), cache_root=self.vault.code_cache_dir
            )
            new_nodes, _dropped = self._nodes(payload)
            new_edges = self._edges(payload, known=set(new_nodes))

        old_G = _networkx_graph(old_graph)
        new_G = _networkx_graph(
            {"nodes": list(new_nodes.values()), "edges": new_edges}
        )
        result = analyze_mod.graph_diff(old_G, new_G)
        return {"consumer": consumer, **result}

    # ------------------------------------------------------------------ shared

    def _maybe_read(self) -> dict[str, Any] | None:
        try:
            raw = self.vault.read_text(CODE_PROJECTION)
        except (OSError, NotFoundError):
            return None
        if not raw.strip():
            return None
        try:
            graph = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return graph if isinstance(graph, dict) else None

    def _read(self, consumer: str) -> dict[str, Any]:
        _validate_consumer(consumer)
        # The boundary is checked before the graph is opened, not after it is
        # traversed: there is no filtering step that could be forgotten because
        # a code graph is all-or-nothing to a given consumer.
        if consumer == "cloud":
            raise ValidationError(
                "The code graph is local-only",
                details={
                    "consumer": consumer,
                    "privacy": CODE_PRIVACY.value,
                    "hint": "It carries repository paths; read it with --consumer local",
                },
            )
        graph = self._maybe_read()
        if graph is None:
            raise NotFoundError(
                "No code graph in this vault",
                details={"hint": f"Build one with {CODE_PROJECTION_COMMAND}"},
            )
        return graph

    def _resolve(self, graph: dict[str, Any], symbol: str) -> str:
        """Find a node by id, then by exact label, then case-insensitively."""

        wanted = symbol.strip()
        nodes = graph.get("nodes", [])
        for node in nodes:
            if str(node.get("id")) == wanted:
                return str(node["id"])
        matches = [node for node in nodes if str(node.get("label")) == wanted]
        if not matches:
            lowered = wanted.casefold()
            matches = [
                node for node in nodes if str(node.get("label")).casefold() == lowered
            ]
        if not matches:
            raise NotFoundError(
                "No such symbol in the code graph", details={"symbol": symbol}
            )
        if len(matches) > 1:
            # Ambiguity is the caller's to resolve; guessing would answer a
            # question about the wrong file and look authoritative doing it.
            raise ValidationError(
                "Symbol is ambiguous; name one by id",
                details={
                    "symbol": symbol,
                    "candidates": sorted(
                        f"{node['id']} ({node.get('path')})" for node in matches
                    )[:10],
                },
            )
        return str(matches[0]["id"])


def _line(value: Any) -> int | None:
    """`L17` as an integer. Anything else is no line at all, not line zero."""

    text = str(value or "").strip().lstrip("Ll")
    return int(text) if text.isdigit() else None


def _fingerprint(files: dict[str, str]) -> str:
    return hashlib.sha256(
        "\n".join(f"{len(path)}:{path}={digest}" for path, digest in sorted(files.items())).encode(
            "utf-8"
        )
    ).hexdigest()


_ANALYSIS_MODULES: tuple[Any, Any] | None = None


def _load_analysis() -> tuple[Any, Any]:
    """Import the vendored community/cycle/diff analysis, once.

    Deferred for the same reason `infrastructure/extractor.py` defers
    `graphify.extract`: `graphify.cluster` and `graphify.analyze` both
    import `graphify.build`, which imports networkx at module load, and
    every caller of `CodeGraph` — not just the three methods below — would
    otherwise pay that import, or fail outright where the `code` extra is
    not installed and no one asked for `communities`/`cycles`/`diff`.

    `find_spec` rather than a real `import networkx`, for the same reason
    `GraphifyExtractor.available()` gives that reasoning for tree-sitter: a
    static import needs an ignore comment when the package is absent and
    mypy calls it unused the moment `code` is installed.
    """

    global _ANALYSIS_MODULES
    if _ANALYSIS_MODULES is None:
        if importlib.util.find_spec("networkx") is None:
            raise ValidationError(
                "This command requires the optional `networkx` dependency",
                details={"hint": "Install it with: pip install brainkit[code]"},
            )
        # Side effect only: installs the `graphify` alias in `sys.modules`,
        # same as `infrastructure/extractor.py`'s own `_load`. A function
        # call rather than a bare `import` statement, so an import-sorter
        # cannot reorder it after the `graphify` import below — which would
        # break the alias that import depends on.
        importlib.import_module("brainkit.infrastructure.codeanalysis")

        from graphify import analyze, cluster  # type: ignore[import-not-found]

        _ANALYSIS_MODULES = (cluster, analyze)
    return _ANALYSIS_MODULES


def _networkx_graph(graph: dict[str, Any]) -> nx.Graph:
    """Brainkit's stored shape, translated to what the vendored analysis reads.

    `graphify.cluster`/`graphify.analyze` expect node attributes
    `label`/`source_file` and edge attributes `relation`/`source_file`;
    brainkit's own graph calls those `path` (on both nodes and edges) and
    `type` (on edges). Translating here keeps the vendored modules reading
    exactly the attribute names they were written against, rather than
    teaching them brainkit's spelling — the adaptation the vendoring rule
    requires to live outside `infrastructure/codeanalysis/`.

    A `DiGraph`, not a `Graph`: cycle detection is meaningless without
    direction, and `graphify.cluster.cluster` already converts a directed
    graph to undirected internally when community detection needs it.
    """

    import networkx as nx

    built = nx.DiGraph()
    for node in graph["nodes"]:
        node_id = str(node["id"])
        built.add_node(
            node_id, label=node.get("label", node_id), source_file=node.get("path", "")
        )
    for edge in graph["edges"]:
        built.add_edge(
            str(edge["source"]),
            str(edge["target"]),
            relation=edge.get("type", ""),
            source_file=edge.get("path", ""),
        )
    return built
