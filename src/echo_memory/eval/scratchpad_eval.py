"""Does the derived scratchpad answer questions, at the spend a query costs?

`retrieval/scratchpad.py` has shipped unmeasured. Its argument is BEAM's: the
LIGHT framework's ablation at 100K tokens says removing episodic retrieval
"does not change performance and it remains steady, since the scratchpad alone
suffices". If that holds here, a store could answer from a block it already
knows how to derive, with no query embedding and no fusion.

That claim is testable and this is the test. It is also the first measurement
in this project of a retrieval component that is not a ranker, so two things
had to be decided before any number means anything.

## Matched token spend, not matched list length

A scratchpad is not top_k of anything. It is a block of context, and the only
honest way to compare a block against a ranked answer is to give them the same
number of tokens and ask which one contains the answer. So the budget is
measured, not chosen: `query_memory` is run first, its mean answer cost in
tokens becomes the budget, and `routing.fit_to_budget` cuts the scratchpad to
exactly that. Both sides then cost the same and the comparison is about content.

## Presence, not only rank

MRR and recall@10 are the published metrics and both are reported, but neither
is the metric this question needs. A scratchpad's order is centrality, which is
query-independent by design, so ranking it against a query is scoring it on
something it never claimed to do. What a model reading a context block
experiences is presence: the answer is in the block or it is not.

So `presence` is the headline here, computed as recall at a k larger than
either list, and MRR is reported beside it as the thing that will look worst.
Reporting only the favourable one would be the failure this harness exists to
prevent.

## What this cannot settle

The scratchpad is the same block for every question in a scope. So its score is
a property of the scope's fact distribution, not of the question, and a corpus
whose facts cluster on a few hub entities will flatter it. One store's number
is one store's number, which is the same caveat every figure in BENCHMARKS.md
carries, and it bites harder here.

The query-scoped form, `scratchpad(about=[...])`, is the obvious thing to
measure next and is not measured here: it needs query terms resolved to entity
names, and `scratchpad`'s `about` matches names exactly while `prompt_terms`
lowercases and splits, so wiring the two together is a resolution problem of
its own rather than a parameter.
"""

from __future__ import annotations

from echo_memory.eval.retrieval import (
    CHARS_PER_TOKEN,
    DEFAULT_TOP_K,
    SHAPE_ENTITY_PAIR,
    Case,
    Result,
    build_cases,
    compare,
    run,
)
from echo_memory.retrieval.routing import fit_to_budget
from echo_memory.retrieval.scratchpad import scratchpad

#: How deep a scratchpad to fetch before the budget cuts it.
#:
#: Deliberately far above `scratchpad.DEFAULT_FACTS`, so that what truncates
#: the block is the measured budget and not the fetch. A fetch limit doing the
#: truncating would mean the two sides were compared at whatever spend the
#: default happened to imply.
PAD_DEPTH = 200


def _score_fixed(cases: list[Case], facts: list[dict], name: str) -> Result:
    """Score the same cases against one fixed list of facts.

    Mirrors `eval/retrieval.run`'s rank arithmetic, including the
    `also_required` rule that a multi-hop case is answerable only once both of
    its facts are in hand. Reimplemented rather than reused because `run` calls
    `query_memory` itself: a configuration there IS its keyword arguments, and
    a scratchpad is not a configuration of query_memory.

    Every case sees the identical list, which is the point. A scratchpad is
    derived once per scope and costs its whole size on every question, so
    `returned_chars` is the same on every case rather than varying with the
    query.
    """
    result = Result(name=name, shape=cases[0].shape if cases else SHAPE_ENTITY_PAIR)
    chars = sum(len(f.get("fact") or "") for f in facts)
    rank_of = {str(f.get("fact_id")): i for i, f in enumerate(facts, start=1)}

    for case in cases:
        result.cases += 1
        if not facts:
            result.empty += 1
        result.returned_chars.append(chars)

        needed = {case.gold_edge_id}
        if case.also_required:
            needed.add(case.also_required)
        at = [rank_of[fact_id] for fact_id in needed if fact_id in rank_of]
        rank = max(at) if len(at) == len(needed) else None
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


def _arm(
    cases: list[Case],
    pad: list[dict],
    baseline: Result,
    budget: int,
    top_k: int,
) -> dict:
    """One scratchpad arm: cut the block to a budget and score it.

    Shared by `measure` and `sweep` so that a one-budget comparison and a
    sweep across budgets cannot disagree about what a budget means.
    """
    kept, report = fit_to_budget(pad, budget)
    variant = _score_fixed(cases, kept, f"scratchpad at {budget} tokens")
    # A k above either list's length, so "recall at this k" is "the answer was
    # in what came back at all" for both sides rather than for one of them.
    presence_at = max(len(kept), top_k) + 1
    return {
        "budget_tokens": budget,
        "variant": variant,
        "scratchpad_facts": len(kept),
        "scratchpad_tokens": report["estimated_tokens"],
        "scratchpad_dropped": report["dropped"],
        "pad_available": len(pad),
        "mrr": compare(baseline, variant),
        "recall_at_10": compare(baseline, variant, at=10),
        "presence": compare(baseline, variant, at=presence_at),
        "presence_at": presence_at,
    }


def measure(
    conn,
    group_id: str,
    embedder,
    *,
    cases: list[Case] | None = None,
    shape: str = SHAPE_ENTITY_PAIR,
    top_k: int = DEFAULT_TOP_K,
    budget: int | None = None,
    pad_depth: int = PAD_DEPTH,
    **kwargs,
) -> dict:
    """Score the scratchpad against `query_memory` at the same token spend.

    `budget` defaults to what `query_memory`'s answers actually cost on these
    cases, which is the measurement that makes the comparison fair. Passing one
    fixes the spend instead.

    `kwargs` reach `query_memory` through `run`, so the baseline can be any
    configuration the ablation switches describe rather than only the shipping
    one.

    Returns the two `Result`s and a bootstrap interval for each of MRR,
    recall@10 and presence. Read the intervals before the deltas: at the sample
    sizes a synthetic corpus gives, most of them will contain zero, and a delta
    whose interval contains zero is not a finding.
    """
    cases = build_cases(conn, group_id, shape=shape) if cases is None else cases
    if not cases:
        return {"cases": 0, "error": "no cases could be built from this scope"}

    baseline = run(conn, group_id, embedder, cases, f"query_memory top {top_k}", **kwargs)
    spend = baseline.mean_tokens if budget is None else budget
    pad = scratchpad(conn, group_id, limit=pad_depth)

    return {
        "cases": len(cases),
        "shape": shape,
        "baseline": baseline,
        **_arm(cases, pad, baseline, spend, top_k),
    }


def sweep(
    conn,
    group_id: str,
    embedder,
    *,
    multiples: tuple[int, ...] = (1, 2, 4, 8, 16),
    cases: list[Case] | None = None,
    shape: str = SHAPE_ENTITY_PAIR,
    top_k: int = DEFAULT_TOP_K,
    pad_depth: int = PAD_DEPTH,
    **kwargs,
) -> dict:
    """The same comparison at several multiples of the matched spend.

    This exists because the single-budget answer is not a property of the
    scratchpad on its own. A query-independent block of k facts drawn from a
    corpus of n, asked questions spread evenly over that corpus, contains the
    answer about k/n of the time whatever its ordering, so the one number that
    decides the comparison is how many facts the budget buys. A sweep says how
    much more context the block needs to reach the ranker rather than only that
    it did not.

    The baseline is scored once and reused across arms: it does not depend on
    the budget, and re-running it per arm would spend one query_memory per case
    per budget to get the same Result back.
    """
    cases = build_cases(conn, group_id, shape=shape) if cases is None else cases
    if not cases:
        return {"cases": 0, "error": "no cases could be built from this scope"}

    baseline = run(conn, group_id, embedder, cases, f"query_memory top {top_k}", **kwargs)
    matched = baseline.mean_tokens
    pad = scratchpad(conn, group_id, limit=pad_depth)

    return {
        "cases": len(cases),
        "shape": shape,
        "baseline": baseline,
        "matched_tokens": matched,
        "arms": [
            {"multiple": m, **_arm(cases, pad, baseline, matched * m, top_k)}
            for m in multiples
        ],
    }


def _interval(delta: dict) -> str:
    mark = "" if delta["significant"] else " ?"
    return f"{delta['delta']:+.4f} [{delta['low']:+.4f}, {delta['high']:+.4f}]{mark}"


def render(measured: dict) -> str:
    """The numbers, with the intervals attached to them.

    `?` marks an interval containing zero, which is the same convention
    BENCHMARKS.md uses, and it means the delta beside it is not a finding.
    """
    if measured.get("error"):
        return f"no measurement: {measured['error']}"
    base: Result = measured["baseline"]
    var: Result = measured["variant"]
    at = measured["presence_at"]
    head = f"{'measure':<16}{'query_memory':>14}{'scratchpad':>14}   delta [95% CI]"
    return "\n".join([
        f"scratchpad vs query_memory, shape {measured['shape']}, n={measured['cases']} cases",
        f"  matched spend: {measured['budget_tokens']} tokens at "
        + f"{CHARS_PER_TOKEN} chars/token; scratchpad kept "
        + f"{measured['scratchpad_facts']} of {measured['pad_available']} live "
        + f"facts, {measured['scratchpad_tokens']} tokens",
        "",
        f"  {head}",
        f"  {'MRR':<16}{base.mrr:>14.4f}{var.mrr:>14.4f}   {_interval(measured['mrr'])}",
        f"  {'recall@1':<16}{base.recall(1):>14.4f}{var.recall(1):>14.4f}",
        f"  {'recall@10':<16}{base.recall(10):>14.4f}{var.recall(10):>14.4f}   "
        + _interval(measured["recall_at_10"]),
        f"  {f'presence (@{at})':<16}{_rate(base, at):>14.4f}{_rate(var, at):>14.4f}   "
        + _interval(measured["presence"]),
        f"  {'mean tokens':<16}{base.mean_tokens:>14}{var.mean_tokens:>14}",
    ])


def _rate(result: Result, k: int) -> float:
    if not result.cases:
        return 0.0
    return sum(1 for r in result.ranks if r is not None and r <= k) / result.cases


def render_sweep(swept: dict) -> str:
    """How much context the block needs, arm by arm.

    The `facts` column is the one to read first, and reading it honestly means
    knowing that with `build_cases` it DETERMINES the presence column rather
    than predicting it. One case per live fact means the number of cases whose
    answer is in the block equals the number of facts in the block, so presence
    is k/n exactly, whatever order the block is in.

    That makes this sweep a measurement of block size against a spread question
    set and not of centrality. It is a limit of the case construction, not of
    the scratchpad, and the way past it is a question set nobody derived from
    its own answers.
    """
    if swept.get("error"):
        return f"no measurement: {swept['error']}"
    base: Result = swept["baseline"]
    lines = [
        f"scratchpad budget sweep, shape {swept['shape']}, n={swept['cases']} cases",
        f"  query_memory: MRR {base.mrr:.4f}, recall@10 {base.recall(10):.4f}, "
        + f"{base.mean_tokens} tokens per answer",
        "",
        f"  {'spend':>8}{'tokens':>8}{'facts':>7}{'MRR':>9}{'presence':>10}"
        + "   delta presence [95% CI]",
    ]
    for arm in swept["arms"]:
        var: Result = arm["variant"]
        at = arm["presence_at"]
        lines.append(
            f"  {str(arm['multiple']) + 'x':>8}{arm['budget_tokens']:>8}"
            + f"{arm['scratchpad_facts']:>7}{var.mrr:>9.4f}{_rate(var, at):>10.4f}"
            + f"   {_interval(arm['presence'])}"
        )
    return "\n".join(lines)
