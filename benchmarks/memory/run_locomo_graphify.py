#!/usr/bin/env python3
"""The same LOCOMO measurement, run against graphify instead of brainkit.

Published benchmark tables compare systems that each ran on their own harness,
and the earlier comparison in this repo inherited that problem: brainkit scored
0.519 and graphify's published figure was 0.497, but the two numbers came from
different question samples, different retrieval units and different scoring
code, so the gap was uninterpretable. This runs both through *one* harness.

Everything that can be held constant is:

- **the same conversations and the same questions** — whichever subset is
  selected here is re-scored for brainkit by `--brainkit` in the same process;
- **the same retrieval unit** — one document per dialogue turn, so a retrieved
  node maps to exactly one `dia_id` for both systems;
- **the same scorer** — `run_locomo.score`, imported rather than reimplemented;
- **the same k** — the first 10 *distinct* source turns a system surfaces.

What cannot be held constant, and must be read alongside any number:

- **graphify needs an LLM to index prose; brainkit needs none.** That is a real
  difference in kind, not a nuisance parameter, so it is reported as tokens and
  wall-clock rather than hidden. The backend is named in the output because the
  result depends on it: piloted with `qwen2.5:3b`, graphify extracted **two**
  distinct entities from 30 turns (both speaker names) and the graph was
  useless. A weak model does not measure graphify.
- **graphify retrieves entities, brainkit retrieves turns.** Its `query` is a
  BFS over an extracted graph, so one hit can carry a turn that keyword search
  would never rank. The `--budget` is set high and the list truncated to 10
  afterwards, which gives graphify the most generous reading of "top 10".

    python benchmarks/memory/run_locomo_graphify.py --conversations 3 --index
    python benchmarks/memory/run_locomo_graphify.py --conversations 3 --json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from run_locomo import (  # noqa: E402
    ADVERSARIAL_CATEGORY,
    DATA,
    Question,
    build_vault,
    load,
    questions_of,
    retrieved_ids,
    score,
    turns_of,
)

#: Where the per-conversation corpora and their graphs live. Indexing is the
#: expensive step, so it is a separate `--index` pass and cached on disk: a
#: scoring change must never mean paying for extraction again.
WORK = ROOT / "graphify-corpora"

_SRC = re.compile(r"\bsrc=([^\s\]]+)")
_SLUG = re.compile(r"\bD?(\d+)-(\d+)\b", re.IGNORECASE)


@dataclass
class SystemResult:
    system: str
    asked: int = 0
    recall_at_k: float = 0.0
    #: The best recall this system could reach with a *perfect* ranker over
    #: what it actually indexed. Separating this from `recall_at_k` is what
    #: turns a horse race into a diagnosis: brainkit indexes every turn, so its
    #: ceiling is 1.0 and its score is purely ranking; graphify condenses turns
    #: into entities, so part of its score is decided before ranking begins.
    ceiling: float = 1.0
    #: `recall_at_k / ceiling` — how well a system ranks *what it kept*.
    ranking_efficiency: float = 0.0
    unreachable_questions: int = 0
    hit_rate: float = 0.0
    mrr: float = 0.0
    index_seconds: float = 0.0
    query_ms_median: float = 0.0
    llm_input_tokens: int = 0
    llm_calls: str = ""
    per_category: dict[str, float] = field(default_factory=dict)


def corpus_dir(sample_id: str) -> Path:
    return WORK / re.sub(r"[^A-Za-z0-9_-]", "_", sample_id)


def write_turns(sample: dict) -> Path:
    """One file per dialogue turn — the same unit the brainkit run indexes."""

    target = corpus_dir(str(sample["sample_id"])) / "turns"
    target.mkdir(parents=True, exist_ok=True)
    for turn in turns_of(sample["conversation"]):
        text = str(turn.get("text", ""))
        if not text:
            continue
        name = str(turn["dia_id"]).replace(":", "-")
        (target / f"{name}.md").write_text(
            f"[{turn['dia_id']}] {turn.get('speaker', '')}: {text}\n", encoding="utf-8"
        )
    return target


def index(sample: dict, backend: str, model: str | None) -> tuple[float, int]:
    """Build the graph for one conversation. Returns (seconds, input tokens).

    The cost is recorded to disk beside the graph, because indexing is cached
    and a cached re-run would otherwise report **zero** — making the expensive
    half of graphify's pipeline invisible in exactly the table that exists to
    compare cost. A number that is only true the first time you run it is not
    a measurement.
    """

    target = write_turns(sample)
    receipt = target / "graphify-out" / "index-cost.json"
    if (target / "graphify-out" / "graph.json").is_file():
        try:
            paid = json.loads(receipt.read_text(encoding="utf-8"))
            return float(paid["seconds"]), int(paid["input_tokens"])
        except (OSError, ValueError, KeyError):
            # An older graph with no receipt: report nothing rather than
            # guessing, and say so where it is printed.
            return 0.0, 0
    command = ["graphify", "extract", str(target), "--backend", backend,
               "--max-concurrency", "1"]
    if model:
        command += ["--model", model]
    started = time.monotonic()
    # Fixed argv, no shell. The only caller-supplied value is a path this
    # module wrote itself; `graphify` is resolved from PATH deliberately,
    # because the point is to measure whatever `graphify` the operator has.
    completed = subprocess.run(command, capture_output=True, text=True)  # noqa: S603
    elapsed = time.monotonic() - started
    output = completed.stdout + completed.stderr
    tokens = 0
    found = re.search(r"tokens:\s*([\d,]+)\s*in", output)
    if found:
        tokens = int(found.group(1).replace(",", ""))
    if not (target / "graphify-out" / "graph.json").is_file():
        raise SystemExit(f"graphify produced no graph for {sample['sample_id']}:\n{output[-1500:]}")
    receipt.write_text(
        json.dumps({"seconds": round(elapsed, 1), "input_tokens": tokens}) + "\n",
        encoding="utf-8",
    )
    return elapsed, tokens


def graphify_ids(graph: Path, question: str, k: int, budget: int) -> list[str]:
    """The first k distinct source turns graphify's traversal surfaces.

    `--budget` is deliberately generous and the truncation happens here, so a
    token cap can never be what limits graphify to fewer than k candidates.
    """

    completed = subprocess.run(  # noqa: S603 -- see `index`
        ["graphify", "query", question, "--graph", str(graph), "--budget", str(budget)],  # noqa: S607
        capture_output=True,
        text=True,
    )
    ordered: list[str] = []
    for raw in _SRC.findall(completed.stdout):
        slug = _SLUG.search(Path(raw).stem)
        if not slug:
            continue
        identifier = f"D{slug.group(1)}:{slug.group(2)}"
        if identifier not in ordered:
            ordered.append(identifier)
        if len(ordered) >= k:
            break
    return ordered


def reachable_turns(graph: Path) -> set[str]:
    """Turn ids that exist as a node in `graph` — the retrievable universe."""

    try:
        payload = json.loads(graph.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    found = set()
    for node in payload.get("nodes", []):
        source = node.get("source_file")
        if not source:
            continue
        slug = _SLUG.search(Path(str(source)).stem)
        if slug:
            found.add(f"D{slug.group(1)}:{slug.group(2)}")
    return found


def ceiling_for(questions: list[Question], reachable: dict[str, set[str]]) -> tuple[float, int]:
    """Recall a perfect ranker could reach, and how many questions are hopeless.

    A system that condenses 419 turns into 91 entities cannot retrieve a turn
    it never kept, however good its ranking is. Reporting the achieved number
    without this makes a coverage decision look like a retrieval failure.
    """

    scores = []
    hopeless = 0
    for question in questions:
        universe = reachable.get(question.conversation, set())
        gold = set(question.evidence)
        overlap = gold & universe
        scores.append(len(overlap) / len(gold) if gold else 0.0)
        if not overlap:
            hopeless += 1
    return (statistics.fmean(scores) if scores else 0.0), hopeless


def evaluate(
    name: str,
    questions: list[Question],
    lookup,
    index_seconds: float,
    tokens: int,
    calls: str,
    *,
    ceiling: float = 1.0,
    hopeless: int = 0,
) -> SystemResult:
    result = SystemResult(system=name, asked=len(questions))
    recalls, hits, rrs, latencies = [], [], [], []
    by_category: dict[int, list[float]] = {}
    for question in questions:
        started = time.perf_counter()
        retrieved = lookup(question)
        latencies.append((time.perf_counter() - started) * 1000)
        recall, hit, rr = score(question.evidence, retrieved)
        recalls.append(recall)
        hits.append(hit)
        rrs.append(rr)
        by_category.setdefault(question.category, []).append(recall)
    result.recall_at_k = round(statistics.fmean(recalls), 4) if recalls else 0.0
    result.hit_rate = round(statistics.fmean(hits), 4) if hits else 0.0
    result.mrr = round(statistics.fmean(rrs), 4) if rrs else 0.0
    result.query_ms_median = round(statistics.median(latencies), 2) if latencies else 0.0
    result.index_seconds = round(index_seconds, 1)
    result.llm_input_tokens = tokens
    result.llm_calls = calls
    result.per_category = {
        str(c): round(statistics.fmean(v), 4) for c, v in sorted(by_category.items())
    }
    result.ceiling = round(ceiling, 4)
    result.unreachable_questions = hopeless
    result.ranking_efficiency = (
        round(result.recall_at_k / ceiling, 4) if ceiling else 0.0
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversations", type=int, default=3)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--budget", type=int, default=8000)
    parser.add_argument("--backend", default="claude-cli")
    parser.add_argument("--model", default=None)
    parser.add_argument("--index", action="store_true", help="Build graphs, then stop")
    parser.add_argument("--include-adversarial", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    samples = load(DATA)[: args.conversations]
    WORK.mkdir(parents=True, exist_ok=True)

    index_seconds = 0.0
    tokens = 0
    for sample in samples:
        seconds, used = index(sample, args.backend, args.model)
        index_seconds += seconds
        tokens += used
        if seconds:
            print(
                f"  indexed {sample['sample_id']}: {seconds/60:.1f} min, "
                f"{used:,} input tokens",
                file=sys.stderr,
            )
    if args.index:
        print(f"\nindexed {len(samples)} conversation(s) in {index_seconds/60:.1f} min")
        return 0

    questions: list[Question] = []
    for sample in samples:
        for question in questions_of(sample):
            if args.include_adversarial or question.category != ADVERSARIAL_CATEGORY:
                questions.append(question)

    graphs = {
        str(sample["sample_id"]): corpus_dir(str(sample["sample_id"]))
        / "turns"
        / "graphify-out"
        / "graph.json"
        for sample in samples
    }

    graphify_result = evaluate(
        "graphify",
        questions,
        lambda q: graphify_ids(graphs[q.conversation], q.question, args.k, args.budget),
        index_seconds,
        tokens,
        f"{args.backend} (semantic extraction)",
        **dict(zip(("ceiling", "hopeless"),
                   ceiling_for(questions, {c: reachable_turns(g) for c, g in graphs.items()}),
                   strict=True)),
    )

    # Brainkit, re-scored on exactly these questions rather than reusing an
    # earlier figure computed over a different sample.
    import tempfile

    brainkit_seconds = 0.0
    services = {}
    scratch = tempfile.TemporaryDirectory()
    for sample in samples:
        started = time.monotonic()
        services[str(sample["sample_id"])] = build_vault(
            turns_of(sample["conversation"]),
            Path(scratch.name) / re.sub(r"\W", "_", str(sample["sample_id"])),
        )
        brainkit_seconds += time.monotonic() - started
    brainkit_result = evaluate(
        "brainkit",
        questions,
        lambda q: retrieved_ids(services[q.conversation], q.question, args.k),
        brainkit_seconds,
        0,
        "none",
    )
    scratch.cleanup()

    payload = {
        "conversations": len(samples),
        "k": args.k,
        "questions": len(questions),
        "systems": [asdict(graphify_result), asdict(brainkit_result)],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print()
    print(f"  LOCOMO · one harness · {len(samples)} conversations · n={len(questions)} · k={args.k}")
    print()
    header = (
        f"  {'system':<11}{'recall@k':>10}{'ceiling':>9}{'rank.eff':>10}"
        f"{'hit@k':>8}{'MRR':>7}{'index':>9}{'query':>9}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for item in (graphify_result, brainkit_result):
        print(
            f"  {item.system:<11}{item.recall_at_k:>10.3f}{item.ceiling:>9.3f}"
            f"{item.ranking_efficiency:>10.3f}{item.hit_rate:>8.3f}{item.mrr:>7.3f}"
            f"{item.index_seconds/60:>8.1f}m{item.query_ms_median:>8.0f}ms"
        )
    print()
    print("  ceiling  = best recall a perfect ranker could reach over what the system indexed")
    print("  rank.eff = recall / ceiling, i.e. how well it ranks what it kept")
    print(f"  graphify left {graphify_result.unreachable_questions} question(s) with no evidence turn in its graph at all")
    if graphify_result.llm_input_tokens:
        print(f"\n  graphify indexing consumed {graphify_result.llm_input_tokens:,} input tokens; brainkit consumed 0.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
