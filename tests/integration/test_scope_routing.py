"""Routing against a real store: does it pick the scope that holds the answer,
and does it stay cheap enough to be worth doing.

The cost claims are the ones worth a test. Routing only pays for itself if it
is cheaper than the fan out it replaces, so "no embedding call" and "one query
per scope" are not implementation details here, they are the premise.
"""

from __future__ import annotations

import hashlib
import inspect
import math

import pytest

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval import routing

DEV = "memory:dev"
PROD = "memory:prod"
SCRATCH = "memory:scratch"

QUERY = "which deploy branch does dugout-be use"

# One fact per scope, with only the first naming anything the query does.
SEED = {
    DEV: [("deploy branch", "dugout-be", "the deploy branch is master, never main")],
    PROD: [("Razorpay", "recurring payments", "recurring is enabled on the live key")],
    SCRATCH: [("Colima", "clock skew", "the VM clock drifts by about 24 milliseconds")],
}


class _HashEmbedder:
    """A unit vector per string, derived from its digest.

    Routing never embeds anything, so these vectors only have to exist: they
    are what `write_episode` stores, not what anything under test reads. A
    registered-vector embedder would mean listing every entity name and fact in
    the seed twice.
    """

    dimension = 384

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b - 127.5 for b in (digest * 12)[: self.dimension]]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class _CountingConn:
    """Delegates to a real connection and records every statement.

    A wrapper rather than a logging hook because the claim being checked is
    about the number of round trips, and the number of round trips is exactly
    the number of times something calls execute.
    """

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql, params=None, *args, **kwargs):
        self.statements.append(sql)
        return self._conn.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _seed(conn) -> None:
    embedder = _HashEmbedder()
    for group_id, facts in SEED.items():
        for source, target, text in facts:
            write_episode(
                conn, group_id, "s1",
                [{"name": source, "type": "thing"}, {"name": target, "type": "thing"}],
                [{"source": source, "target": target, "relation_type": "is",
                  "fact": text, "confidence": "extracted"}],
                None, embedder, agent_id="claude-code", assume_new=True,
            )


def test_the_scope_holding_the_answer_is_the_one_chosen(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        chosen, rejected = routing.select(conn, [DEV, PROD, SCRATCH], QUERY)

    assert chosen == [DEV]
    assert {r["group_id"] for r in rejected} == {PROD, SCRATCH}


def test_a_scope_naming_none_of_the_query_terms_is_rejected_with_a_reason(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        _, rejected = routing.select(conn, [DEV, PROD, SCRATCH], QUERY)

    reasons = {r["group_id"]: r["reason"] for r in rejected}
    for group_id in (PROD, SCRATCH):
        assert "names none of the" in reasons[group_id]
        assert "deploy" in reasons[group_id]
        assert "floor" in reasons[group_id]


def test_the_scores_say_how_much_of_the_query_each_scope_answers_to(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        scores = routing.score_scopes(conn, [DEV, PROD, SCRATCH], QUERY)

    by_scope = {s.group_id: s for s in scores}
    # deploy, branch and dugout-be all name a node in dev; "use" and "does"
    # are dropped by prompt_terms before any query runs.
    assert by_scope[DEV].matched_terms == 3
    assert by_scope[DEV].candidate_facts == 1
    assert by_scope[DEV].score > 0
    assert by_scope[PROD].matched_terms == 0
    assert by_scope[PROD].score == 0.0


def test_a_scope_that_names_the_query_and_holds_no_fact_scores_zero(migrated_db):
    """An episode that declares entities and asserts no facts writes the
    entities and stores nothing else, which leaves exactly this: a scope that
    answers to the word with nothing current to say about it."""
    empty = "memory:entities-only"
    with connect(migrated_db) as conn:
        result = write_episode(
            conn, empty, "s1",
            [{"name": "deploy branch", "type": "thing"}],
            [], None, _HashEmbedder(), agent_id="claude-code", assume_new=True,
        )
        assert result.get("not_stored") == "no_facts"
        scores = routing.score_scopes(conn, [empty], QUERY)

    assert scores[0].matched_terms == 2
    assert scores[0].candidate_facts == 0
    assert scores[0].score == 0.0
    assert "holds no live fact" in scores[0].reason


def test_the_scorer_takes_no_embedder_and_constructs_none(migrated_db, monkeypatch):
    """The expensive mistake, guarded at both doors it could come in by.

    A vector probe per scope is a query_memory per scope, which is the cost
    routing exists to avoid, and there are only two places a scorer could get
    an embedder from: a parameter, or the module default. The signature covers
    the first and the exploding constructor covers the second.
    """
    def _explode():
        raise AssertionError("routing constructed an embedder")

    monkeypatch.setattr("echo_memory.ingestion.embeddings.LocalEmbedder", _explode)

    assert "embedder" not in inspect.signature(routing.score_scopes).parameters
    assert "embedder" not in inspect.signature(routing.select).parameters

    with connect(migrated_db) as conn:
        _seed(conn)
        assert routing.score_scopes(conn, [DEV, PROD, SCRATCH], QUERY)


def test_scoring_costs_one_query_per_scope(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        counting = _CountingConn(conn)
        routing.score_scopes(counting, [DEV, PROD, SCRATCH], QUERY)

    assert len(counting.statements) == 3


def test_live_facts_only(migrated_db):
    """A superseded fact must not keep a scope in the fan out. The count is
    what separates "this scope is about the question" from "this scope was
    about the question in September"."""
    with connect(migrated_db) as conn:
        _seed(conn)
        write_episode(
            conn, DEV, "s2",
            [{"name": "deploy branch", "type": "thing"},
             {"name": "dugout-be", "type": "thing"}],
            [{"source": "deploy branch", "target": "dugout-be", "relation_type": "is",
              "fact": "the deploy branch is release, as of November",
              "confidence": "extracted"}],
            None, _HashEmbedder(), agent_id="claude-code", assume_new=True,
        )
        versions = conn.execute(
            f"""SELECT count(*) FROM {GRAPH}."FACT" e
                 WHERE (e.properties ->> '"group_id"'::agtype) = %s""",
            (DEV,),
        ).fetchone()[0]
        scores = routing.score_scopes(conn, [DEV], QUERY)

    assert versions == 2
    assert scores[0].candidate_facts == 1


def test_a_default_scope_outside_the_authorised_list_is_refused(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        with pytest.raises(Exception, match="authorised"):
            routing.select(conn, [DEV], QUERY, default=PROD)
