"""One answer, from the scopes worth asking, inside a token budget.

Three things already existed and had never been put in a line: `query_across`
fans out and fuses, `routing` decides which scopes are worth the fan out, and
`routing.fit_to_budget` cuts a ranked list to what a caller can afford. This is
the composition and nothing else. It does not re-rank, it does not re-fuse and
it does not reach into a scope.

Composing rather than extending `query_across` is the same choice `across.py`
made for the same reason, and the reason is measurement. With one scope and no
budget this returns exactly what `query_across` returns, byte for byte, which
is pinned by a test. That is what keeps every number in BENCHMARKS.md the
baseline for whatever this does next. A pipeline that quietly changed the
single-scope answer would have thrown all of them away.

## Routing runs only when there is a choice to make

With one scope there is nothing to route: the only decision available is to
reject the single scope the caller has, which is the data-losing failure
`routing.select` is built to refuse. So one scope skips the scorer entirely and
costs nothing extra.

## When routing picks nothing, it falls back and says so

`routing.choose` can honestly return no scopes: a query whose words name no
entity in any scope is a query the scorer cannot place. Returning empty there
would be the worst of both worlds, because `query_memory`'s vector channel
exists precisely to answer questions that share no words with their answer, and
an empty result is indistinguishable from an empty store.

So this falls back to the caller's scopes and records that it did, under
`routing.fallback`. Loud, not silent: that is the same rule `across.py` applies
to an unreadable scope and to an erasure exclusion. The scorer is a cost
optimiser, and abstaining is `query_memory`'s job, which it already does
through the adaptive floor.

## What the explanation is for

A customer whose answer came from two of their five memories cannot otherwise
tell whether the other three were asked and had nothing, or were never asked.
Those are different bugs on their side, and only one of them is theirs. So the
rejected scopes travel with the answer, each with the sentence that rejected
it, and so does the budget report, because an answer thinned by a budget looks
exactly like a thin store.

Two keys in that block are easy to confuse and mean different things.
`chosen` is what routing picked. `considered` is `query_across`'s, and is
`chosen` minus any scope that turned out to be unreadable, which is why the
weights are keyed on it. An empty `scores` means the scorer never ran, not
that nothing matched: with one scope there was no decision to make.
"""

from __future__ import annotations

from echo_memory.retrieval import routing
from echo_memory.retrieval.across import query_across
from echo_memory.retrieval.query_memory import DEFAULT_TOP_K


def assemble(
    conn,
    group_ids: list[str],
    query: str | None,
    top_k: int = DEFAULT_TOP_K,
    embedder=None,
    *,
    token_budget: int | None = None,
    max_scopes: int = 3,
    min_score: float = 0.0,
    default_group: str | None = None,
    weights: dict[str, float] | None = None,
    propose_weights: bool = True,
    explain: bool = True,
    **kwargs,
) -> dict:
    """Route, fan out, fuse, fit, and explain.

    `group_ids` must already be the scopes the caller is authorised for, which
    is `query_across`'s contract and is not weakened here: routing only ever
    narrows that list. `default_group` is kept whatever it scores and must be
    one of `group_ids`.

    `weights` overrides `routing.weights_for`'s proposals, and
    `propose_weights=False` turns them off for plain RRF. Both exist for the
    same reason query_memory's `use_mmr`, `floor` and `expansion_weight` do: a
    change to what a query returns has to be ablatable one at a time or the
    harness cannot say which part helped.

    The proposals are on by default here, which is a weaker standard than this
    project usually applies to a ranking change, and the reason it is allowed
    is that there is no baseline to protect. `query_memory` and single-scope
    `query_across` answers are pinned and untouched; multi-scope fusion is new
    on this branch and has never been measured, so there is no number that a
    default weight invalidates. The first eval over several scopes should settle
    it, and until then `propose_weights=False` is the configuration that
    reproduces unweighted RRF.

    `token_budget` of 0 or None means no budget. `explain=False` drops the
    per-scope scores and rejection reasons but keeps the budget report, because
    a truncated answer that does not say it was truncated is the silent
    thinning this whole module is written against.

    Unrecognised keyword arguments go to `query_memory` through
    `query_across`, so every ablation switch the harness uses still reaches it.
    """
    group_ids = list(group_ids)

    if len(group_ids) == 1 and not token_budget:
        # The byte-identical path. Nothing is added, not even an explanation:
        # a routing block here would be a shape change for an answer that had
        # no routing decision in it.
        return query_across(
            conn, group_ids, query, top_k, embedder, weights=weights, **kwargs
        )

    scores: list[routing.ScopeScore] = []
    rejected: list[dict] = []
    fallback: str | None = None

    if len(group_ids) == 1:
        chosen = list(group_ids)
    else:
        scores = routing.score_scopes(conn, group_ids, query or "")
        chosen, rejected = routing.choose(
            scores,
            max_scopes=max_scopes,
            min_score=min_score,
            default=default_group,
        )
        if not chosen:
            chosen = group_ids[:max_scopes]
            fallback = (
                f"no scope scored above the {min_score:.3f} floor, so the "
                f"first {len(chosen)} of the scopes given were asked anyway. "
                f"A query whose words name no entity can still be answered by "
                f"the vector channel, which this scorer cannot see."
            )
            rejected = [r for r in rejected if r["group_id"] not in chosen]

    proposed = weights
    if proposed is None and propose_weights and len(chosen) > 1:
        proposed = routing.weights_for([s for s in scores if s.group_id in chosen])

    result = query_across(
        conn, chosen, query, top_k, embedder, weights=proposed, **kwargs
    )
    if "error" in result:
        return result

    kept, report = routing.fit_to_budget(result.get("facts") or [], token_budget)
    result["facts"] = kept

    block = dict(result.get("routing") or {})
    if token_budget:
        block["budget"] = report
    if explain:
        block["chosen"] = chosen
        block["rejected"] = rejected
        block["scores"] = [s.as_dict() for s in scores]
        if fallback is not None:
            block["fallback"] = fallback
    if block:
        result["routing"] = block
    return result
