"""The parts of routing that decide what a caller does not get.

Two of these are about data loss rather than about arithmetic. A scorer that
drops the scope the caller configured, and a budget that drops a fact out of
the middle of a ranked list, both return something shaped exactly like a
correct answer, so nothing downstream can notice.
"""

from __future__ import annotations

import pytest

from echo_memory.retrieval.query_memory import ValidationError
from echo_memory.retrieval.routing import (
    CHARS_PER_TOKEN,
    MIN_WEIGHT,
    ScopeScore,
    choose,
    estimate_tokens,
    fit_to_budget,
    select,
    weights_for,
)


def _fact(text: str) -> dict:
    return {"fact_id": text[:4], "fact": text}


def _score(group_id: str, score: float, matched: int = 1, facts: int = 10) -> ScopeScore:
    return ScopeScore(
        group_id=group_id,
        matched_terms=matched,
        candidate_facts=facts,
        score=score,
        reason=f"{matched} terms over {facts} facts",
    )


class _Row:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _StubConn:
    """Answers score_scopes' per-scope query with canned (matched, facts).

    A stub rather than a database because what is under test here is the
    selection, and the selection has to be checkable at the boundary where a
    default scope scores zero - a case that is awkward to seed and trivial to
    state.
    """

    def __init__(self, answers: list[tuple[int, int]]):
        self._answers = list(answers)
        self.calls = 0

    def execute(self, sql, params=None):
        self.calls += 1
        return _Row(self._answers.pop(0))


def test_estimate_tokens_is_characters_over_four():
    assert CHARS_PER_TOKEN == 4
    assert estimate_tokens([_fact("a" * 40)]) == 10
    assert estimate_tokens([_fact("a" * 40), _fact("b" * 20)]) == 15
    assert estimate_tokens([]) == 0


def test_a_fact_with_no_text_costs_nothing():
    """Not an invented case: a digest row and a scratchpad row both carry a
    `fact` that can be NULL in the database, and summing None raises."""
    assert estimate_tokens([{"fact": None}, {"fact_id": "x"}]) == 0


def test_no_budget_changes_nothing_and_keeps_the_shape():
    facts = [_fact("a" * 40), _fact("b" * 40)]
    for budget in (None, 0):
        kept, report = fit_to_budget(facts, budget)
        assert kept == facts
        assert report == {"budget": budget, "estimated_tokens": 20, "dropped": 0}


def test_the_budget_truncates_from_the_tail():
    facts = [_fact("a" * 40), _fact("b" * 40), _fact("c" * 40)]
    kept, report = fit_to_budget(facts, 25)

    assert [f["fact"] for f in kept] == ["a" * 40, "b" * 40]
    assert report == {"budget": 25, "estimated_tokens": 20, "dropped": 1}


def test_a_long_fact_ends_the_list_rather_than_being_skipped():
    """The ranked order is the product. Dropping the 40-token fact at rank 2
    because the 4-token fact at rank 3 fits would promote rank 3 over rank 2,
    which is a reranking nobody asked for and no test downstream would see."""
    facts = [_fact("a" * 16), _fact("b" * 160), _fact("c" * 16)]
    kept, report = fit_to_budget(facts, 20)

    assert [f["fact"] for f in kept] == ["a" * 16]
    assert report["dropped"] == 2


def test_the_budget_is_never_exceeded():
    facts = [_fact("x" * 400)] * 5
    for budget in range(1, 400):
        kept, report = fit_to_budget(facts, budget)
        assert report["estimated_tokens"] <= budget
        assert estimate_tokens(kept) <= budget


def test_a_budget_under_the_top_fact_returns_nothing_and_says_so():
    kept, report = fit_to_budget([_fact("a" * 400)], 10)

    assert kept == []
    # The distinction that matters: an empty answer because the store holds
    # nothing reports dropped 0, and this reports 1.
    assert report["dropped"] == 1


def test_a_negative_budget_is_refused():
    with pytest.raises(ValidationError):
        fit_to_budget([_fact("a")], -1)


def test_weights_normalise_the_best_scope_to_one():
    weights = weights_for([_score("a", 0.8), _score("b", 0.4), _score("c", 0.2)])

    assert weights["a"] == 1.0
    assert weights["b"] == pytest.approx(0.5)
    assert weights["c"] == pytest.approx(0.25)


def test_no_scope_is_weighted_out_of_existence():
    weights = weights_for([_score("a", 0.8), _score("b", 0.0, matched=0, facts=0)])

    assert weights["a"] == 1.0
    assert weights["b"] == MIN_WEIGHT


def test_nothing_scoring_is_plain_rrf():
    weights = weights_for([_score("a", 0.0), _score("b", 0.0)])

    assert weights == {"a": 1.0, "b": 1.0}


def test_the_default_scope_survives_a_zero_score():
    chosen, rejected = choose(
        [_score("best", 0.9), _score("mine", 0.0, matched=0, facts=0)],
        max_scopes=3,
        default="mine",
    )

    assert set(chosen) == {"best", "mine"}
    assert rejected == []


def test_the_default_scope_survives_max_scopes_of_one():
    chosen, rejected = choose(
        [_score("best", 0.9), _score("mine", 0.0, matched=0, facts=0)],
        max_scopes=1,
        default="mine",
    )

    assert chosen == ["mine"]
    assert [r["group_id"] for r in rejected] == ["best"]
    assert "max_scopes is 1" in rejected[0]["reason"]


def test_a_default_outside_the_authorised_scopes_is_refused():
    """Appending it would let routing widen what the caller may read, which is
    the one direction this pipeline must never move in."""
    with pytest.raises(ValidationError):
        choose([_score("a", 0.9)], default="somebody-elses-memory")


def test_a_scope_that_matched_nothing_is_rejected_with_a_reason():
    chosen, rejected = choose([_score("a", 0.9), _score("b", 0.0, matched=0, facts=0)])

    assert chosen == ["a"]
    assert rejected[0]["group_id"] == "b"
    assert "0.000" in rejected[0]["reason"]
    assert "floor" in rejected[0]["reason"]


def test_nothing_above_the_floor_chooses_nothing():
    """An honest empty, not an error. What an unrouteable query means is the
    caller's decision; see assemble, which falls back and says that it did."""
    chosen, rejected = choose([_score("a", 0.0), _score("b", 0.0)])

    assert chosen == []
    assert len(rejected) == 2


def test_select_keeps_the_default_through_the_database_path():
    """The same guarantee as above, reached the way a caller reaches it: the
    scores come from the per-scope query rather than being handed in."""
    conn = _StubConn([(0, 0), (2, 20)])

    chosen, rejected = select(
        conn, ["mine", "theirs"], "deploy branch policy", max_scopes=1, default="mine"
    )

    assert chosen == ["mine"]
    assert conn.calls == 2
    assert [r["group_id"] for r in rejected] == ["theirs"]


def test_a_query_with_no_searchable_terms_costs_no_queries():
    """Every word is a stopword or shorter than three characters, so there is
    nothing to match a node name against. Scoring anyway would mean one query
    per scope to learn nothing."""
    conn = _StubConn([])

    chosen, rejected = select(conn, ["a", "b"], "what do you want?")

    assert conn.calls == 0
    assert chosen == []
    assert all("no searchable terms" in r["reason"] for r in rejected)


def test_max_scopes_must_be_positive():
    with pytest.raises(ValidationError):
        choose([_score("a", 0.9)], max_scopes=0)


def test_a_repeated_scope_is_refused_before_it_is_scored():
    """query_across refuses duplicates because a scope that votes twice
    silently doubles its weight in the fusion. Refusing here too means the
    caller is told at the step that would have chosen it twice."""
    conn = _StubConn([(1, 1), (1, 1)])

    with pytest.raises(ValidationError, match="distinct"):
        select(conn, ["mine", "mine"], "deploy branch policy")

    assert conn.calls == 0


# --- the budget is a ceiling, including in the report --------------------


def test_the_reported_spend_never_exceeds_the_budget():
    """The gate and the report must do the same arithmetic.

    The first version accumulated `estimate_tokens([fact])` per fact, which is
    a sum of floors, while the report is a floor of a sum. Three facts of ten
    characters are 2+2+2=6 one way and 30//4=7 the other, so a list passed the
    gate at 6 against a budget of 6 and the report then said 7.

    Swept rather than spot-checked at one value, because the divergence only
    appears at some lengths and budgets and a single case is how it survived
    the first time. It was found by the full suite, not by this file.
    """
    for length in range(1, 40):
        for count in range(1, 8):
            facts = [
                {"fact": "x" * length, "fact_id": str(i)} for i in range(count)
            ]
            whole = estimate_tokens(facts)
            for budget in range(1, whole + 3):
                kept, report = fit_to_budget(facts, budget)
                assert report["estimated_tokens"] <= budget, (
                    f"{count} facts of {length} chars at budget {budget}: "
                    f"reported {report['estimated_tokens']}"
                )
                assert report["estimated_tokens"] == estimate_tokens(kept), (
                    "the report disagrees with measuring the kept list"
                )
                assert report["dropped"] == count - len(kept)


def test_a_budget_smaller_than_the_first_fact_keeps_nothing():
    """And says so in the report, which is the only thing distinguishing an
    exhausted budget from a store with no answer."""
    facts = [{"fact": "x" * 200, "fact_id": "a"}]
    kept, report = fit_to_budget(facts, 1)
    assert kept == []
    assert report["dropped"] == 1
    assert report["estimated_tokens"] == 0


def test_truncation_still_comes_off_the_tail():
    """A long fact at rank 1 ends the list rather than being skipped over, so
    the order the caller was given is the order it gets."""
    facts = [
        {"fact": "x" * 400, "fact_id": "long"},
        {"fact": "y" * 4, "fact_id": "short"},
    ]
    kept, report = fit_to_budget(facts, 50)
    assert kept == [], "the short fact must not be promoted past the long one"
    assert report["dropped"] == 2
