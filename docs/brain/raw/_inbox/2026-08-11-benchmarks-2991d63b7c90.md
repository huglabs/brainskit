# Benchmarks

`benchmarks/` measures two things the engine claims: that the code graph
indexes what it says it can, and that vault retrieval finds what a question is
actually about. Both are reproducible from a clean checkout.

**Coverage, not node count.** A graph can grow while an entire language falls
silently out of it — which is exactly what happened here once, and the reason
the headline metric is *files that produced at least one node ÷ files whose
extension has an extractor*. A node count would have called that build a
success.

## Code graph

Ten repositories pinned by commit, spanning the languages the shipped
extractors claim, plus a hermetic 26-language fixture that runs inside the test
suite with no network.

| | |
|---|---|
| **Coverage** | **100%** on all ten repositories and the fixture |
| Corpus | 1,766 files · 26,706 nodes · 54,745 edges · 21.7 MB of graph |
| Cost | 30.2 s total (~58 files/s), no LLM calls |
| Largest | commons-lang, 625 files in 13.6 s |

Deliberate skips are excluded from the denominator. `extract_json` declines
data-shaped JSON on purpose, and counting that as a failure once made a
repository read as 82.4% covered when every source file it owned had in fact
been parsed.

```bash
python benchmarks/run.py                    # hermetic fixture, seconds
python benchmarks/run.py --corpus           # clone and measure the ten repositories
python benchmarks/run.py --corpus --check   # fail on regression against baseline.json
```

## LOCOMO retrieval

Long-conversation question answering, scored against the gold evidence turns
LOCOMO ships — so no judge is involved and no model choice can confound the
number. Both systems run through **one** harness: the same ten conversations,
the same 1,536 questions, the same retrieval unit (one document per dialogue
turn), the same scorer, the same k.

| System | recall@10 | ceiling | ranking eff. | MRR | index | query |
|---|---|---|---|---|---|---|
| **brainkit** | **0.519** | 1.000 | 0.519 | 0.376 | 0.6 min · no LLM | 4 ms |
| graphify | 0.302 | 0.575 | 0.525 | 0.185 | 58.4 min · ~3.3M LLM tokens | 132 ms |

`ceiling` is the best recall a *perfect* ranker could reach over what each
system actually stored, and it is the column that makes the result readable.
brainkit indexes every turn, so nothing is out of reach and its score is purely
ranking. graphify condenses ~590 turns into ~70 entities per conversation,
leaving **536 of 1,536 questions with no evidence turn in its graph at all** —
scored zero before ranking began. `ranking eff.` is recall ÷ ceiling: measured
against what each system kept, the two are tied (0.525 against 0.519). **The
gap is coverage, not ranking.**

```bash
# LOCOMO's release is not vendored
git clone --depth 1 https://github.com/snap-research/locomo /tmp/locomo
cp /tmp/locomo/data/locomo10.json benchmarks/memory/

python benchmarks/memory/run_locomo.py --limit 300        # brainkit alone
python benchmarks/memory/run_locomo_graphify.py --index   # build graphify's graphs
python benchmarks/memory/run_locomo_graphify.py           # both, one harness
```

**What these numbers do not establish.** graphify scores 0.497 in its own
published harness and 0.302 here, so this is not reproducing that setup: the
per-turn document unit was chosen to make scoring exact against LOCOMO's
turn-level evidence, and that choice suits a system retrieving turns while
handicapping one retrieving entities. Turn-level recall is not what an entity
graph is built to win. Same-harness is what makes two numbers comparable; it is
not what makes one of them a verdict.

Two further cautions, both learned by getting them wrong first. A 300-question
sample of LOCOMO carries a ~95% band of roughly ±0.06 — wide enough that an
early "graphify ranks better" finding (+0.052 at n=383) vanished to +0.006 over
the full population. And graphify's result depends entirely on the model
indexing it: piloted with a 3B local model it extracted two entities from
thirty turns, so any figure for an LLM-backed system is a figure about that
LLM. The runs above use `claude-cli`.
