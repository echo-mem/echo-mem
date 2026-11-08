"""Does a retrieval change actually help? Measure it.

Every tuning decision in this project so far has been argued rather than
demonstrated. `COSINE_FLOOR` was picked, then measured a year of usage later to
be sitting inside the noise. `k=60` and `LIST_DEPTH=50` were written down as
"low-leverage" when in fact the fusion had never had two lists to fuse. The
common cause is that there was no way to tell whether a change was an
improvement, so nobody could be wrong out loud.

**The labels are free.** A fact is an edge between two named entities, and the
question an agent actually asks is entity-shaped - "what do I know about
updateSquad" - so the entity names make a query and the fact they connect is
the gold answer. That is not circular: the query is two short labels, the gold
is a sentence of prose that shares only some of their words, and the ranker has
to find it among every other fact in the store.

It is also not a benchmark. It measures one store against itself, so the
absolute numbers mean nothing outside it.

**And "invalid for absolute scores, valid for A/B" is too generous**, which a
reviewer established using this module's own evidence. A biased task can change
the DIFFERENCE between configurations, not only their level: answer-derived
wording rewards lexical retrieval, embedded entity names flip which channel
wins, and a single-entity query scores other relevant facts as failures. The
sign reversal recorded below is exactly that - the winner changed with the task
construction, which is the thing a valid A/B is supposed to be robust to.

So the question this answers is "which configuration scores higher on this
generated task", and the question it does not answer is "which configuration
should ship for the questions people actually ask". Use it as a regression
harness and a diagnostic. A claim of the form "channel X was the real win"
needs independently written questions, and does not follow from these tables.

Three metrics, and the second two matter more than the first.

  recall@k   did the gold fact come back at all
  MRR        how high, averaged - moving a hit from rank 8 to rank 2 shows here
             and is invisible to recall@10
  tokens     what the answer cost. A configuration that finds everything by
             returning everything is not better, and this is the number that
             says so.

**One metric cannot judge every change.** Each case has a single right answer,
so this measures ranking and nothing else. MMR was added for diversity among
the returned facts, which a single-answer metric is blind to by construction:
the tables show it costs ranking on every shape, which is why it is off, and
they say nothing about whether it delivered the diversity it was added for.
Read a removal here as "it costs ranking", never as "it does nothing".

**Query shape decides the answer, so it is not a detail.** The first version of
this harness asked every question as "<source> <target>". Once entity names
were embedded into each fact, that query became a literal substring of the text
it was scoring against - maximum leakage - and it inverted a real conclusion:
under it, dropping the lexical channel measured +0.112 MRR and looked like a
significant win. On queries with the entity names removed the same change is
-0.082, significantly WORSE. One default query shape produced two opposite
answers about the same code.

So shapes are plural and named, and the leaky one is labelled:

  entity_pair   both names. Maximum leakage now that names are embedded. Kept
                because it is the entity-centric lookup an agent really does
                make, but never read alone.
  entity_single one name. The realistic middle - a question usually names one
                of the things it is about, and half the prepended string.
  prose         the fact's own words with both entity names stripped. No
                overlap with what was prepended, so nothing can leak; also the
                least realistic, since nobody queries in the target's prose.

**And MRR is a mean, so it has a standard error.** At n=219 with sd ~0.35 the
SE is ~0.023, which makes any difference under ~0.047 indistinguishable from
zero. Differences of 0.001 to 0.01 were reported as findings before this was
computed. Every comparison now carries a paired bootstrap interval, and
`compare` says plainly when a difference is noise.
"""

from __future__ import annotations

import json
import random
import re
import statistics
from dataclasses import dataclass, field

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.retrieval.query_memory import query_memory

# Roughly 4 characters per token for English prose. The same constant the read
# accounting uses; exactness does not matter because it is only ever compared
# against itself across configurations.
CHARS_PER_TOKEN = 4

DEFAULT_TOP_K = 10

# Resamples for the bootstrap interval. Enough that the 2.5th and 97.5th
# percentiles are stable to the third decimal, which is finer than any
# difference worth acting on.
BOOTSTRAP_RESAMPLES = 5000

# Query shapes, most leaky first. See the module docstring.
SHAPE_ENTITY_PAIR = "entity_pair"
SHAPE_ENTITY_SINGLE = "entity_single"
SHAPE_PROSE = "prose"
SHAPE_MULTIHOP = "multihop"
SHAPES = (SHAPE_ENTITY_PAIR, SHAPE_ENTITY_SINGLE, SHAPE_PROSE, SHAPE_MULTIHOP)

# A single hub can be incident to 35 facts, which is 595 ordered pairs. Left
# uncapped, one project node would supply most of the sample and the score
# would describe that node rather than the store.
MULTIHOP_PER_HUB = 6

# Words of the fact to keep for a prose query. Long enough to carry the
# subject, short enough that it is a question rather than the answer.
PROSE_WORDS = 14


@dataclass(frozen=True)
class Case:
    """One retrieval question and its known answer."""

    query: str
    gold_edge_id: str
    gold_fact: str
    shape: str = SHAPE_ENTITY_PAIR
    # A multi-hop question is only answerable once BOTH facts are in hand, so
    # its case carries a second required edge. Scoring then asks at what rank
    # the question became answerable, not at what rank the first clue arrived:
    # a run that returns one half at rank 1 and never the other has answered
    # nothing, and a metric that rewards it would make traversal look
    # unnecessary.
    also_required: str | None = None


@dataclass
class Result:
    name: str
    cases: int = 0
    hits_at: dict[int, int] = field(default_factory=lambda: {1: 0, 3: 0, 5: 0, 10: 0})
    reciprocal_ranks: list[float] = field(default_factory=list)
    #: The rank each case became answerable at, or None when it never did.
    #: `hits_at` counts the same thing and cannot be resampled: a bootstrap
    #: needs the per-case values, not their total. Kept beside the reciprocal
    #: ranks rather than derived from them, because 1/rank loses the
    #: difference between "rank 11" and "never" once both round to 0.0 at the
    #: k being asked about.
    ranks: list[float | None] = field(default_factory=list)
    returned_chars: list[int] = field(default_factory=list)
    empty: int = 0
    shape: str = SHAPE_ENTITY_PAIR

    def recall(self, k: int) -> float:
        return self.hits_at[k] / self.cases if self.cases else 0.0

    @property
    def mrr(self) -> float:
        return sum(self.reciprocal_ranks) / self.cases if self.cases else 0.0

    @property
    def mean_tokens(self) -> int:
        if not self.returned_chars:
            return 0
        return int(sum(self.returned_chars) / len(self.returned_chars) / CHARS_PER_TOKEN)

    @property
    def abstention_rate(self) -> float:
        return self.empty / self.cases if self.cases else 0.0

    @property
    def mrr_stderr(self) -> float:
        """MRR is a mean over cases, so it has one.

        At n=219 with sd around 0.35 this is about 0.023, which makes any
        difference under roughly 0.047 indistinguishable from zero. Differences
        of 0.001 to 0.01 were reported as findings before anyone computed it.
        """
        if len(self.reciprocal_ranks) < 2:
            return 0.0
        return statistics.stdev(self.reciprocal_ranks) / len(self.reciprocal_ranks) ** 0.5


def compare(baseline: Result, variant: Result, resamples: int = BOOTSTRAP_RESAMPLES,
            at: int | None = None,
            seed: int = 7) -> dict:
    """Is variant actually different from baseline, or is it noise?

    Paired, because both scored the same cases in the same order: the variance
    that matters is that of the per-case DIFFERENCE, not of either mean. Two
    configurations agreeing on 200 of 219 cases have a far tighter interval than
    their separate standard errors suggest, and an unpaired test would call a
    real improvement noise.

    Bootstrap rather than a t-test: reciprocal ranks are neither normal nor
    continuous - they are 1, 1/2, 1/3 ... 0, with a heavy spike at 0 - so
    resampling makes no distributional claim that the data would violate.

    **It does not remove the independence assumption.** Cases are resampled one
    at a time, and cases are not independent: they share facts, they share
    entity names, and in the multihop shape up to six of them come from one
    middle entity. Dependence like that makes an interval narrower than it
    should be. A cluster bootstrap - resampling hubs or facts rather than cases
    - is the fix, and is not implemented. Read these intervals as optimistic.
    """
    if len(baseline.reciprocal_ranks) != len(variant.reciprocal_ranks):
        raise ValueError("compare needs both configurations scored on the same cases")

    # Which per-case series to resample. MRR was the only one for a long time,
    # and it is the wrong one for several of the changes measured against this
    # harness: the graph hop moved multihop recall@10 from 0.635 to 0.769 while
    # moving MRR by +0.003, and the rerank moved it 0.570 to 0.720. Reporting
    # an interval for the metric that did not move, and a bare point estimate
    # for the one that did, is the shape of a result nobody can act on.
    if at is None:
        series = (baseline.reciprocal_ranks, variant.reciprocal_ranks)
    else:
        series = tuple(
            [0.0 if r is None else float(r <= at) for r in result.ranks]
            for result in (baseline, variant)
        )
        if len(series[0]) != len(series[1]):
            raise ValueError("compare needs both configurations scored on the same cases")

    diffs = [v - b for v, b in zip(series[1], series[0], strict=True)]
    if not diffs:
        return {"delta": 0.0, "low": 0.0, "high": 0.0, "significant": False}

    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(
        sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    low = means[int(0.025 * resamples)]
    high = means[int(0.975 * resamples)]
    return {
        "delta": sum(diffs) / n,
        "low": low,
        "high": high,
        # Excludes zero at 95%. Not "true", just "not obviously nothing".
        "significant": low > 0 or high < 0,
    }


def _prose_query(fact: str, source: str, target: str) -> str:
    """The fact's own words with both entity names removed.

    Removing them is the point: entity names are embedded into every fact, so a
    query containing them overlaps the text being scored. With them gone
    nothing can leak, and what is left measures whether the ranker finds a fact
    from its subject matter rather than from a shared literal.
    """
    stripped = fact
    for name in (source, target):
        if name:
            stripped = re.sub(re.escape(name), " ", stripped, flags=re.IGNORECASE)
    return " ".join(stripped.split()[:PROSE_WORDS])



def _multihop_cases(rows, limit: int | None) -> list[Case]:
    """Two entities joined only through a third, and the two facts that join them.

    **Named for what it measures: required-set completion, not answerability.**
    The first version called it "answerable-at rank", which assumes what an
    evaluation is supposed to establish. Generating two facts that share a
    middle entity does not make them a necessary and sufficient evidence set -
    two unrelated facts about one project need not jointly answer anything, and
    some other fact or path may answer a real question without completing the
    designated pair. A reviewer drew that line and it is the right one.

    What the score does mean is exact: at what rank did the retriever surface
    both of the two facts this case designates. That is a well-defined and
    useful regression signal for traversal work. Calling it answerability
    requires questions and relevance labels written by somebody, which do not
    exist yet.

    Two entities X and Y that are NOT directly connected, but both appear in
    facts about a third entity M. "How are X and Y related?" is the question
    this system's own README uses to describe multi-hop retrieval - "how did we
    end up here?" - and it is the one shape the harness could not previously
    see, because every other case's gold answer is a single edge.

    X and Y must not already be adjacent: if one fact joins them, the question
    is single-hop and belongs in the shapes above.

    Built from the edge list in Python rather than from a pattern predicate,
    because AGE's Cypher support for NOT (x)-[]-(y) is not something to depend
    on for a measurement.
    """
    adjacency: dict[str, set[str]] = {}
    incident: dict[str, list[tuple[str, str, str, str]]] = {}
    for edge_id, source_id, source, target_id, target, fact in rows:
        if source_id == target_id:
            continue
        adjacency.setdefault(source_id, set()).add(target_id)
        adjacency.setdefault(target_id, set()).add(source_id)
        incident.setdefault(source_id, []).append((edge_id, target_id, target, fact))
        incident.setdefault(target_id, []).append((edge_id, source_id, source, fact))

    cases: list[Case] = []
    for middle in sorted(incident):
        arms = incident[middle]
        if len(arms) < 2:
            continue
        made = 0
        for i in range(len(arms)):
            for j in range(i + 1, len(arms)):
                e1, x_id, x_name, f1 = arms[i]
                e2, y_id, y_name, _f2 = arms[j]
                if x_id == y_id or e1 == e2:
                    continue
                if y_id in adjacency.get(x_id, ()):
                    continue
                if not x_name or not y_name:
                    continue
                cases.append(Case(
                    query=f"{x_name} {y_name}",
                    gold_edge_id=e1, gold_fact=f1, also_required=e2,
                    shape=SHAPE_MULTIHOP,
                ))
                made += 1
                if made >= MULTIHOP_PER_HUB:
                    break
            if made >= MULTIHOP_PER_HUB:
                break
    return cases[:limit] if limit else cases


def build_cases(
    conn, group_id: str, limit: int | None = None, shape: str = SHAPE_ENTITY_PAIR
) -> list[Case]:
    """One case per fact whose two entities have different names.

    Self-loops are skipped. A fact from a node to itself gives a query of one
    repeated word, which measures nothing about ranking and is over-represented
    in this store because agents reach for a self-edge when a fact has no
    natural second entity.

    `shape` decides what the question looks like, and it decides the answer -
    see the module docstring. Defaults to entity_pair for continuity with
    earlier numbers, but a conclusion drawn from it alone is not safe.
    """
    if shape not in SHAPES:
        raise ValueError(f"shape must be one of {SHAPES}, got {shape!r}")
    raw = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a:Node)-[e:FACT]->(b:Node)
            WHERE e.group_id = $gid AND e.t_invalid IS NULL
            RETURN id(e), id(a), a.name, id(b), b.name, e.fact
        $$, %s) AS (edge_id agtype, a_id agtype, a agtype,
                     b_id agtype, b agtype, fact agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    rows = [
        (str(edge_id), str(a_id), str(a).strip('"'), str(b_id), str(b).strip('"'),
         str(fact).strip('"'))
        for edge_id, a_id, a, b_id, b, fact in raw
    ]

    if shape == SHAPE_MULTIHOP:
        return _multihop_cases(rows, limit)

    cases: list[Case] = []
    for edge_id, _a_id, source, _b_id, target, text in rows:
        if not source or not target or source == target:
            continue
        if shape == SHAPE_ENTITY_PAIR:
            query = f"{source} {target}"
        elif shape == SHAPE_ENTITY_SINGLE:
            query = source
        else:
            query = _prose_query(text, source, target)
        if not query.strip():
            continue
        cases.append(
            Case(query=query, gold_edge_id=str(edge_id), gold_fact=text, shape=shape)
        )
    return cases[:limit] if limit else cases


def run(conn, group_id: str, embedder, cases: list[Case], name: str, **kwargs) -> Result:
    """Score one configuration over the cases. kwargs go straight to
    query_memory, so a configuration IS its keyword arguments."""
    result = Result(name=name, shape=cases[0].shape if cases else SHAPE_ENTITY_PAIR)
    for case in cases:
        response = query_memory(
            conn, group_id, case.query, DEFAULT_TOP_K, embedder, **kwargs
        )
        facts = response.get("facts") or []
        result.cases += 1
        if not facts:
            result.empty += 1
        result.returned_chars.append(sum(len(f.get("fact") or "") for f in facts))

        needed = {case.gold_edge_id}
        if case.also_required:
            needed.add(case.also_required)
        at: dict[str, int] = {}
        for i, fact in enumerate(facts, start=1):
            fid = str(fact.get("fact_id"))
            if fid in needed and fid not in at:
                at[fid] = i
        # The rank the question became answerable at: the later of the required
        # facts. For every single-hop shape this is exactly the old behaviour,
        # because `needed` holds one edge.
        rank = max(at.values()) if len(at) == len(needed) else None
        if rank is None:
            result.reciprocal_ranks.append(0.0)
            result.ranks.append(None)
            continue
        result.reciprocal_ranks.append(1.0 / rank)
        result.ranks.append(float(rank))
        for k in result.hits_at:
            if rank <= k:
                result.hits_at[k] += 1
    return result


SHAPE_NOTES = {
    SHAPE_ENTITY_PAIR: "both entity names - LEAKY: names are embedded into every fact",
    SHAPE_ENTITY_SINGLE: "one entity name - the realistic shape",
    SHAPE_PROSE: "the fact's words, entity names stripped - cannot leak",
    SHAPE_MULTIHOP: "required-set completion: both designated facts, so R@1 is 0 by construction",
}


def corpus_tokens(conn, group_id: str) -> tuple[int, int]:
    """Every live fact in the scope: how many, and what they cost to inject.

    This is the baseline a targeted recall is worth measuring against. It is
    deliberately NOT "no memory at all" - that cost is however long a human
    spends re-explaining, which is unbounded and unmeasurable. It is the thing
    people do instead: keep the project's accumulated notes in one file and put
    the whole file in context. A memory tool that answers by returning
    everything it holds is the same shape and the same cost.
    """
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a:Node)-[e:FACT]->(b:Node)
            WHERE e.group_id = $gid AND e.t_invalid IS NULL
            RETURN e.fact
        $$, %s) AS (fact agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    facts = [str(f).strip('"') for (f,) in rows]
    return len(facts), sum(len(f) for f in facts) // CHARS_PER_TOKEN


def render_context_saving(results: list[Result], facts: int, whole: int) -> str:
    """What the recall cost against what injecting the whole scope would.

    The hit rate is printed beside the saving and is the point of printing it:
    returning less context is not an improvement on its own, it is only an
    improvement if the answer is still in what came back. A configuration that
    returned nothing would score a 100% saving here.

    The percentage is a property of THIS store, not of the software. It rises
    as the store grows, because the numerator is bounded by top_k and the
    denominator is not - which is the actual claim being made, and the reason
    the fact count and corpus size are printed next to it rather than hidden.
    """
    if not results or not whole:
        return "no cases"

    lines = [
        "",
        f"whole scope      {facts:,} live facts, ~{whole:,} tokens to inject",
        "",
        f"  {'shape':<16}{'cases':>7}{'recall':>9}{'hit@10':>9}{'saving':>10}",
        "  " + "-" * 51,
    ]
    total = 0
    weighted_tokens = 0.0
    weighted_hits = 0.0
    for r in results:
        if not r.cases:
            continue
        tokens = r.mean_tokens
        total += r.cases
        weighted_tokens += tokens * r.cases
        weighted_hits += r.recall(10) * r.cases
        lines.append(
            f"  {r.shape:<16}{r.cases:>7}{tokens:>9,}{r.recall(10):>9.3f}"
            f"{1 - tokens / whole:>9.1%}"
        )
    if not total:
        return "no cases"
    mean_tokens = weighted_tokens / total
    lines += [
        "  " + "-" * 51,
        (
            f"  {'weighted':<16}{total:>7}{mean_tokens:>9,.0f}"
            f"{weighted_hits / total:>9.3f}{1 - mean_tokens / whole:>9.1%}"
        ),
        "",
        (
            f"A token is {CHARS_PER_TOKEN} characters, the constant the read "
            "accounting uses."
        ),
        "Fact text only: no framing, no ids, no formatting either side would add.",
    ]
    return "\n".join(lines)


def render(results: list[Result]) -> str:
    """A table per query shape, with each row's difference from the first.

    Deltas and intervals rather than bare means, because a bare mean is what
    let a 0.001 difference be written down as a finding. The first row of each
    shape is the baseline the rest are measured against.
    """
    if not results:
        return "no configurations run"

    lines: list[str] = []
    by_shape: dict[str, list[Result]] = {}
    for r in results:
        by_shape.setdefault(r.shape, []).append(r)

    for shape in SHAPES:
        group = by_shape.get(shape)
        if not group:
            continue
        baseline = group[0]
        lines.append("")
        lines.append(f"{shape}  ({SHAPE_NOTES[shape]})")
        head = (
            f"  {'configuration':<26}{'R@1':>7}{'R@3':>7}{'R@10':>7}"
            f"{'ΔR@10':>9}{'95% CI':>19}"
            f"{'MRR':>7}{'ΔMRR':>9}{'95% CI':>19}"
        )
        lines.append(head)
        lines.append("  " + "-" * (len(head) - 2))
        for r in group:
            def cells(c):
                if c is None:
                    return f"{'—':>9}", f"{'baseline':>19}"
                marker = "" if c["significant"] else " ?"
                return (
                    f"{c['delta']:>+9.4f}",
                    f"[{c['low']:>+6.3f},{c['high']:>+6.3f}]{marker:>2}",
                )

            hit_delta, hit_ci = cells(None if r is baseline else compare(baseline, r, at=10))
            mrr_delta, mrr_ci = cells(None if r is baseline else compare(baseline, r))
            lines.append(
                f"  {r.name:<26}"
                f"{r.recall(1):>7.3f}{r.recall(3):>7.3f}{r.recall(10):>7.3f}"
                f"{hit_delta}{hit_ci:>19}"
                f"{r.mrr:>7.3f}{mrr_delta}{mrr_ci:>19}"
            )
        lines.append(
            f"  n={baseline.cases}, SE(MRR)≈{baseline.mrr_stderr:.3f}, "
            f"so anything under ~{2 * baseline.mrr_stderr:.3f} is inside 2 SE of zero"
        )

    lines.append("")
    lines.append("? marks an interval that includes zero: the difference is noise.")
    lines.append(
        "Intervals are a seeded paired bootstrap over per-case differences "
        f"({BOOTSTRAP_RESAMPLES} resamples,"
    )
    lines.append(
        "  2.5th/97.5th percentile), not the per-shape SE below each table - "
        "both configurations"
    )
    lines.append(
        "  scored the same cases in the same order, so the variance that matters "
        "is that of"
    )
    lines.append("  the difference. Bootstrap because reciprocal ranks are 1, 1/2, 1/3 ... 0.")
    lines.append("")
    lines.append(
        "EVERY QUERY HERE IS DERIVED FROM ITS OWN ANSWER, so absolute scores measure "
        "how easy"
    )
    lines.append(
        "  that derivation is, not retrieval quality. entity_single is worse than "
        "leaky: one"
    )
    lines.append(
        "  fact is marked correct for a name that may appear in thirty, and the "
        "other twenty-"
    )
    lines.append(
        "  nine relevant ones score as failures. Compare rows. A number from this "
        "table does"
    )
    lines.append(
        "  not belong in a claim about quality without hand-written questions and "
        "relevance"
    )
    lines.append("  labels that permit more than one right answer.")
    lines.append("Absolute values describe this store only. Compare rows, not numbers.")
    return "\n".join(lines)
