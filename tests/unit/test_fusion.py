from echo_memory.retrieval.fusion import K, reciprocal_rank_fusion


def test_top_of_both_lists_wins():
    vector = ["a", "b", "c"]
    lexical = ["a", "c", "b"]
    scores = reciprocal_rank_fusion([vector, lexical])
    assert scores["a"] > scores["b"]
    assert scores["a"] > scores["c"]


def test_missing_from_one_list_still_scores():
    vector = ["a", "b"]
    lexical = ["c"]
    scores = reciprocal_rank_fusion([vector, lexical])
    assert set(scores) == {"a", "b", "c"}
    assert scores["a"] == 1 / (K + 1)
    assert scores["c"] == 1 / (K + 1)


def test_appearing_in_both_lists_beats_appearing_in_one():
    # rank 3 in both lists vs rank 1 in only one list: RRF rewards showing up
    # in every ranker over winning a single race, even at a mediocre rank
    vector = ["x", "y", "z"]
    lexical = ["p", "q", "z"]
    scores = reciprocal_rank_fusion([vector, lexical])
    assert scores["z"] == 1 / (K + 3) + 1 / (K + 3)
    assert scores["x"] == 1 / (K + 1)
    assert scores["z"] > scores["x"]


def test_empty_lists_produce_empty_scores():
    assert reciprocal_rank_fusion([[], []]) == {}


def test_single_list_preserves_relative_order():
    scores = reciprocal_rank_fusion([["a", "b", "c"]])
    assert scores["a"] > scores["b"] > scores["c"]


# --- weights -------------------------------------------------------------


def test_weights_scale_a_lists_contribution():
    """The point of a weight: a list's top slot stops being worth the same as
    every other list's top slot. Unweighted RRF hands 1/(k+1) to whoever
    produced it, which is right for independent comparable rankers and wrong
    for a list ordered by proximity to what another list already found."""
    content = ["a", "b"]
    expansion = ["c"]

    equal = reciprocal_rank_fusion([content, expansion])
    weighted = reciprocal_rank_fusion([content, expansion], weights=[1.0, 0.2])

    assert equal["c"] > equal["b"], "precondition: unweighted, a lone top slot outranks a second place"
    assert weighted["c"] < weighted["b"], "the weight did not change the order"
    assert weighted["a"] == equal["a"], "a weight of 1.0 changed a content score"


def test_no_weights_is_exactly_the_old_behaviour():
    """Every caller that does not pass weights must get plain RRF, since the
    default path is the one every query takes."""
    lists = [["a", "b", "c"], ["c", "a"], ["d"]]

    assert reciprocal_rank_fusion(lists) == reciprocal_rank_fusion(
        lists, weights=[1.0, 1.0, 1.0]
    )


def test_a_weight_for_the_wrong_number_of_lists_is_refused():
    """Silently applying a weight to the wrong channel would change the
    ranking in a way no test distinguishes from a badly chosen weight."""
    import pytest

    with pytest.raises(ValueError, match="2 weights for 3 lists"):
        reciprocal_rank_fusion([["a"], ["b"], ["c"]], weights=[1.0, 0.5])


def test_a_zero_weight_removes_a_list_without_removing_its_members():
    """A fact that both retrieves and neighbours keeps its content score. The
    weight is about what the expansion contributes, not about censoring
    facts."""
    scores = reciprocal_rank_fusion([["a", "b"], ["b"]], weights=[1.0, 0.0])
    content_only = reciprocal_rank_fusion([["a", "b"]])

    assert scores == content_only
