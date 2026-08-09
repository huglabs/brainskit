#!/usr/bin/env python3
"""LOCOMO retrieval, measured against brainkit's own vault.

**What this is, stated before the numbers so nobody mis-reads them.** The
published LOCOMO figures for Graphify, mem0 and supermemory are for
*conversational memory systems*: they ingest a long dialogue and answer
questions about it. Brainkit is a different product — a curated vault whose
writes go through an apply gate with citations — and it vendors Graphify's
**code-extraction** closure only, never its memory or retrieval stack. So this
is not a re-run of that table. It measures brainkit's own retrieval
(`capture` → FTS5 → `search`) on the same task, which is the only honest
comparison available.

`recall@k` is the metric worth reporting here and it needs no LLM at all: for
each question LOCOMO names the dialogue turns that actually contain the answer
(`evidence`, e.g. `D1:3`), so retrieval is scored against ground truth rather
than against a judge's opinion. That keeps the number reproducible and free of
the confound that dominates QA accuracy — which model answered.

Protocol:

- one vault per conversation, built fresh, each dialogue turn captured as its
  own source so a retrieved hit maps back to exactly one `dia_id`;
- questions sampled deterministically (seeded) so a re-run is comparable;
- category 5 is excluded by default. In LOCOMO it is the adversarial split,
  written so the dialogue does *not* answer it; scoring retrieval against
  evidence for a question with no real answer measures the dataset, not the
  system. `--include-adversarial` puts it back.

    python benchmarks/memory/run_locomo.py --limit 300
    python benchmarks/memory/run_locomo.py --limit 300 --k 10 --json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(REPO / "src"))

#: LOCOMO's own release. Not vendored — the harness reports plainly when it is
#: absent rather than silently measuring nothing.
DATA = ROOT / "locomo10.json"

#: The adversarial split. See the module docstring.
ADVERSARIAL_CATEGORY = 5


@dataclass
class Question:
    conversation: str
    question: str
    answer: str
    evidence: tuple[str, ...]
    category: int


@dataclass
class Result:
    asked: int = 0
    recall_at_k: float = 0.0
    hit_rate: float = 0.0
    mrr: float = 0.0
    turns_indexed: int = 0
    index_seconds: float = 0.0
    query_ms_median: float = 0.0
    per_category: dict[str, float] = field(default_factory=dict)


def load(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(
            f"LOCOMO data not found at {path}.\n"
            "Fetch it with:\n"
            "  git clone --depth 1 https://github.com/snap-research/locomo /tmp/locomo\n"
            f"  cp /tmp/locomo/data/locomo10.json {path}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def turns_of(conversation: dict) -> list[dict]:
    """Every dialogue turn, in session order.

    Session keys are `session_1`, `session_2`, … alongside `session_N_date_time`
    strings, so they are selected by shape rather than by name pattern alone.
    """

    ordered = sorted(
        (key for key, value in conversation.items() if isinstance(value, list)),
        key=lambda key: int(re.sub(r"\D", "", key) or 0),
    )
    return [turn for key in ordered for turn in conversation[key]]


def questions_of(sample: dict) -> list[Question]:
    out = []
    for item in sample.get("qa", []):
        evidence = item.get("evidence") or []
        if not evidence:
            continue
        out.append(
            Question(
                conversation=str(sample.get("sample_id", "?")),
                question=str(item.get("question", "")),
                answer=str(item.get("answer") or item.get("adversarial_answer") or ""),
                evidence=tuple(str(e) for e in evidence),
                category=int(item.get("category") or 0),
            )
        )
    return out


def build_vault(turns: list[dict], scratch: Path):
    """Index one conversation, one source per turn.

    Per-turn rather than per-session because `evidence` is per-turn: a vault
    that captured whole sessions would score a hit for retrieving 40 turns of
    which one was relevant, which is not what recall@k is asking.
    """

    from brainkit.application.services import BrainkitService
    from brainkit.infrastructure.index import SqliteFtsIndex
    from brainkit.infrastructure.vault import FileVault

    vault = FileVault.initialize(scratch, _policy())
    index = SqliteFtsIndex(vault.index_path)
    service = BrainkitService(vault, index)

    for turn in turns:
        speaker = str(turn.get("speaker", ""))
        text = str(turn.get("text", ""))
        if not text:
            continue
        # The dia_id is carried in the body, not only the title: the retrieval
        # hit has to be attributable to a turn, and titles are slugified.
        service.capture(
            None,
            text=f"[{turn['dia_id']}] {speaker}: {text}",
            title=str(turn["dia_id"]),
        )
    return service


def _policy() -> dict:
    from brainkit.domain.model import DEFAULT_IGNORE_PATTERNS, INTEGRATION_NAMES

    jobs = ("digest", "file-proposal", "ingest", "lint-semantic", "query", "resurface")
    return {
        "version": 3,
        "wiki_language": "English",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {"10-chat": {"privacy": "local-only", "filing": "approve-each"}},
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
        "job_models": {j: {"provider": "ollama", "model": "qwen2.5:3b"} for j in jobs},
        "sources": [],
        "ignore": list(DEFAULT_IGNORE_PATTERNS),
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": ["10-chat"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            name: {"enabled": False, "managed": False, "options": {}}
            for name in sorted(INTEGRATION_NAMES)
        },
    }


_DIA = re.compile(r"\bD\d+:\d+\b")
#: The same id after capture slugified it: `D1:7` becomes `d1-7` in the
#: filename. Recovered from the path as a fallback, because an excerpt is a
#: fragment and FTS5 is free to start it after the id.
_SLUG = re.compile(r"\bd(\d+)-(\d+)\b")


def retrieved_ids(service, question: str, k: int) -> list[str]:
    """The dia_ids behind the top-k hits, in rank order, de-duplicated."""

    hits = service.search(question, k, consumer="local")["hits"]
    ordered: list[str] = []
    for hit in hits:
        identifier = _dia_id(hit)
        if identifier and identifier not in ordered:
            ordered.append(identifier)
    return ordered


def _dia_id(hit: dict) -> str | None:
    excerpt = _DIA.search(str(hit.get("excerpt") or ""))
    if excerpt:
        return excerpt.group(0)
    slug = _SLUG.search(str(hit.get("title") or "")) or _SLUG.search(
        str(hit.get("path") or "")
    )
    return f"D{slug.group(1)}:{slug.group(2)}" if slug else None


def score(gold: tuple[str, ...], retrieved: list[str]) -> tuple[float, float, float]:
    """Recall, hit (any gold retrieved), and reciprocal rank of the first gold.

    Recall is per-question and averaged afterwards, which is what "recall@10"
    means in the LOCOMO literature — not a corpus-wide micro-average.
    """

    gold_set = set(gold)
    if not gold_set:
        return 0.0, 0.0, 0.0
    found = gold_set.intersection(retrieved)
    recall = len(found) / len(gold_set)
    rank = next(
        (i + 1 for i, item in enumerate(retrieved) if item in gold_set), 0
    )
    return recall, (1.0 if found else 0.0), (1.0 / rank if rank else 0.0)


def run(limit: int, k: int, seed: int, include_adversarial: bool) -> Result:
    samples = load(DATA)
    pool: list[Question] = []
    for sample in samples:
        pool.extend(questions_of(sample))
    if not include_adversarial:
        pool = [q for q in pool if q.category != ADVERSARIAL_CATEGORY]

    # Seeded `random`, deliberately: this samples a benchmark split and has to
    # be reproducible across runs, which is the opposite of what a
    # cryptographic source provides.
    rng = random.Random(seed)  # noqa: S311
    chosen = pool if limit >= len(pool) else rng.sample(pool, limit)
    wanted = {q.conversation for q in chosen}

    result = Result(asked=len(chosen))
    recalls: list[float] = []
    hits: list[float] = []
    rrs: list[float] = []
    latencies: list[float] = []
    by_category: dict[int, list[float]] = {}

    for sample in samples:
        name = str(sample.get("sample_id", "?"))
        if name not in wanted:
            continue
        turns = turns_of(sample["conversation"])
        with tempfile.TemporaryDirectory() as scratch:
            started = time.monotonic()
            service = build_vault(turns, Path(scratch) / "vault")
            result.index_seconds += time.monotonic() - started
            result.turns_indexed += len(turns)

            for question in [q for q in chosen if q.conversation == name]:
                query_started = time.perf_counter()
                retrieved = retrieved_ids(service, question.question, k)
                latencies.append((time.perf_counter() - query_started) * 1000)
                recall, hit, rr = score(question.evidence, retrieved)
                recalls.append(recall)
                hits.append(hit)
                rrs.append(rr)
                by_category.setdefault(question.category, []).append(recall)

    result.recall_at_k = round(statistics.fmean(recalls), 4) if recalls else 0.0
    result.hit_rate = round(statistics.fmean(hits), 4) if hits else 0.0
    result.mrr = round(statistics.fmean(rrs), 4) if rrs else 0.0
    result.query_ms_median = round(statistics.median(latencies), 2) if latencies else 0.0
    result.index_seconds = round(result.index_seconds, 2)
    result.per_category = {
        str(category): round(statistics.fmean(values), 4)
        for category, values in sorted(by_category.items())
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=300, help="Questions to sample")
    parser.add_argument("--k", type=int, default=10, help="Retrieval depth")
    parser.add_argument("--seed", type=int, default=7, help="Sampling seed")
    parser.add_argument("--include-adversarial", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run(args.limit, args.k, args.seed, args.include_adversarial)

    if args.json:
        print(json.dumps(asdict(result), indent=2))
        return 0

    print()
    print(f"  LOCOMO  n={result.asked}  k={args.k}  seed={args.seed}")
    print(f"  {'recall@' + str(args.k):<22} {result.recall_at_k:.3f}")
    print(f"  {'hit rate@' + str(args.k):<22} {result.hit_rate:.3f}")
    print(f"  {'MRR':<22} {result.mrr:.3f}")
    print(f"  {'turns indexed':<22} {result.turns_indexed}")
    print(f"  {'index time':<22} {result.index_seconds}s")
    print(f"  {'median query':<22} {result.query_ms_median} ms")
    print(f"  {'LLM calls':<22} 0")
    print()
    for category, value in result.per_category.items():
        print(f"    category {category}: {value:.3f}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
