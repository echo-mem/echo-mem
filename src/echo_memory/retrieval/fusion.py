"""Reciprocal Rank Fusion (see the design doc's Recommended Approach and
MATHS.local.md §3). k=60 and per-ranker list depth are fixed constants, not
runtime-tunable: an external review's worked example showed k has low
leverage (identical result ordering from k=10 to k=200), while list depth
moves results far more, so it's not worth exposing either as a parameter
here until there's an eval set to tune against."""

K = 60
LIST_DEPTH = 50


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = K,
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Each ranked_list is an ordered list of ids (best first), already
    score-floored and depth-capped by the caller: RRF only sees rank
    position, never the underlying score, so filtering has to happen before
    this, not after.

    `weights` scales each list's contribution, defaulting to 1.0 for all,
    which is plain RRF and what every caller got before this existed.

    Why it exists: unweighted RRF gives **every list's top slot the same
    value**, 1/(k+1), whoever produced it. That is the right default when the
    rankers are independent and comparable, and it is the reason two separate
    attempts at graph expansion both failed. A list ordered by proximity to
    what the query already matched is neither: its first entry is a fact the
    query did not ask for, and it arrives worth exactly as much as the best
    vector hit. Measured, that cost -0.142 MRR on entity_pair and bought
    +0.003 on the shape it was built for.

    So the question a weight asks is not "should the hop run" but "what is a
    neighbour's top slot worth against a match's top slot". Nothing here
    decides that; the eval does.
    """
    if weights is not None and len(weights) != len(ranked_lists):
        raise ValueError(
            f"{len(weights)} weights for {len(ranked_lists)} lists: a weight "
            f"that silently applies to the wrong channel would change the "
            f"ranking in a way no test distinguishes from a bad weight"
        )
    scores: dict[str, float] = {}
    for i, ranked_list in enumerate(ranked_lists):
        weight = 1.0 if weights is None else weights[i]
        for rank, item_id in enumerate(ranked_list, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return scores
