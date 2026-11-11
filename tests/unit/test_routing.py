"""The part of routing that decides what a caller does not get.

The default-scope tests are about data loss rather than about arithmetic. A
scorer that drops the scope the caller configured returns something shaped
exactly like a correct answer, so nothing downstream can notice.
"""

from __future__ import annotations

import pytest

from echo_memory.retrieval.query_memory import ValidationError
from echo_memory.retrieval.routing import (
    MIN_WEIGHT,
    ScopeScore,
    choose,
    select,
    weights_for,
)


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
