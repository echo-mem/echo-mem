"""What `assemble` must not do.

The interesting assertions here are all negative. It must not change the
single-scope answer, because every number in BENCHMARKS.md is measured through
that path. It must not exceed a budget. It must not reject a scope without
saying why. And above all it must not let a fact from an unauthorised scope in,
at any budget, even when that fact is the best answer to the question.
"""

from __future__ import annotations

import hashlib
import math

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval import routing
from echo_memory.retrieval.across import query_across
from echo_memory.retrieval.context import assemble

A = "memory:a"
B = "memory:b"
C = "memory:c"

QUERY = "which deploy branch does dugout-be use"

# A question that names something in A and something in C, so the fan out is
# genuinely a fan out: with QUERY alone, C scores zero and the fusion is
# skipped, which would leave the weighted and budgeted paths untested.
BOTH = "deploy branch and clock skew on Colima"

# A holds the answer. B holds a fact phrased to beat it lexically on the same
# question, and B is the scope no caller in this file is authorised for.
A_FACTS = [
    ("deploy branch", "dugout-be", "the deploy branch is master, never main"),
    ("deploy branch", "echo-mem", "the deploy branch is main, with a DCO gate"),
    ("Caddy", "echo-mem", "TLS terminates at Caddy on the host compose"),
]
B_FACTS = [
    ("deploy branch", "dugout-be",
     ("the deploy branch for dugout-be, which deploy branch does dugout-be "
      "use, is staging")),
]
C_FACTS = [
    ("Colima", "clock skew", "the VM clock drifts by about 24 milliseconds"),
]


class _HashEmbedder:
    """A unit vector per string from its digest.

    Deterministic and unregistered, so the seed does not have to list every
    string twice. Similarity between two of these vectors is arbitrary, which
    is fine for every assertion here: none of them is about ranking quality,
    only about which scopes were asked and what came back.
    """

    dimension = 384

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b - 127.5 for b in (digest * 12)[: self.dimension]]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _seed(conn) -> None:
    embedder = _HashEmbedder()
    for group_id, facts in ((A, A_FACTS), (B, B_FACTS), (C, C_FACTS)):
        for source, target, text in facts:
            write_episode(
                conn, group_id, "s1",
                [{"name": source, "type": "thing"}, {"name": target, "type": "thing"}],
                [{"source": source, "target": target, "relation_type": "is",
                  "fact": text, "confidence": "extracted"}],
                None, embedder, agent_id="claude-code", assume_new=True,
            )


def test_one_scope_and_no_budget_is_query_across(migrated_db):
    """The guarantee that keeps every published number the baseline."""
    with connect(migrated_db) as conn:
        _seed(conn)
        expected = query_across(conn, [A], QUERY, 10, _HashEmbedder())
        actual = assemble(conn, [A], QUERY, 10, _HashEmbedder())

    assert actual == expected
    assert "routing" not in actual


def test_an_unreadable_query_still_matches_query_across(migrated_db):
    """The same equality on the error path, which is where a wrapper usually
    stops being transparent: a validation refusal has no facts to budget and no
    routing to explain."""
    with connect(migrated_db) as conn:
        _seed(conn)
        expected = query_across(conn, [A], QUERY, 0, _HashEmbedder())
        actual = assemble(conn, [A], QUERY, 0, _HashEmbedder())

    assert actual == expected
    assert "error" in actual


def test_a_token_budget_is_never_exceeded(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        full = assemble(conn, [A, C], BOTH, 10, _HashEmbedder())
        assert len({f["source"] for f in full["facts"]}) == 2
        for budget in (1, 5, 10, 20, 40):
            result = assemble(
                conn, [A, C], BOTH, 10, _HashEmbedder(), token_budget=budget
            )
            report = result["routing"]["budget"]
            assert report["budget"] == budget
            assert report["estimated_tokens"] <= budget
            assert routing.estimate_tokens(result["facts"]) <= budget
            assert report["dropped"] == len(full["facts"]) - len(result["facts"])


def test_the_budget_keeps_the_best_ranked_facts(migrated_db):
    """Truncation from the tail, seen from outside: whatever survives a budget
    is a prefix of the unbudgeted answer."""
    with connect(migrated_db) as conn:
        _seed(conn)
        full = assemble(conn, [A, C], QUERY, 10, _HashEmbedder())
        tight = assemble(
            conn, [A, C], QUERY, 10, _HashEmbedder(), token_budget=20
        )

    ids = [f["fact_id"] for f in full["facts"]]
    assert [f["fact_id"] for f in tight["facts"]] == ids[: len(tight["facts"])]


def test_the_explanation_names_the_rejected_scopes_and_why(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        result = assemble(conn, [A, B, C], QUERY, 10, _HashEmbedder())

    block = result["routing"]
    assert A in block["chosen"]
    rejected = {r["group_id"]: r["reason"] for r in block["rejected"]}
    assert C in rejected
    assert "names none of the" in rejected[C]
    assert "deploy" in rejected[C]
    assert {s["group_id"] for s in block["scores"]} == {A, B, C}


def test_the_default_scope_is_asked_even_when_it_scores_nothing(migrated_db):
    with connect(migrated_db) as conn:
        _seed(conn)
        result = assemble(
            conn, [A, C], QUERY, 10, _HashEmbedder(),
            max_scopes=1, default_group=C,
        )

    assert result["routing"]["chosen"] == [C]


def test_an_unrouteable_query_falls_back_and_says_that_it_did(migrated_db):
    """A question whose words name no entity anywhere. Returning nothing would
    be indistinguishable from an empty store, and the vector channel can still
    answer it."""
    with connect(migrated_db) as conn:
        _seed(conn)
        result = assemble(
            conn, [A, B, C], "what are the arrangements", 10, _HashEmbedder()
        )

    block = result["routing"]
    assert block["chosen"] == [A, B, C]
    assert "no scope scored above" in block["fallback"]


def test_an_unauthorised_scope_never_enters_at_any_budget(migrated_db):
    """B's fact repeats the question almost verbatim, so it is the best lexical
    match in the store, and B is not passed. No budget may let it in: a smaller
    budget means fewer facts, never other facts."""
    with connect(migrated_db) as conn:
        _seed(conn)
        intruder = query_across(conn, [B], QUERY, 10, _HashEmbedder())
        assert intruder["facts"], "the seed must make B answerable or this proves nothing"
        intruder_ids = {f["fact_id"] for f in intruder["facts"]}

        for budget in (None, 0, 1, 5, 20, 100, 10_000):
            result = assemble(
                conn, [A, C], QUERY, 10, _HashEmbedder(), token_budget=budget
            )
            returned = {f["fact_id"] for f in result["facts"]}
            assert not (returned & intruder_ids)
            assert all(f["source"] in (A, C) for f in result["facts"])
            assert B not in (result.get("routing") or {}).get("chosen", [])


def test_plain_rrf_is_reachable(migrated_db):
    """The ablation switch. An untuned weight on by default is only defensible
    while the configuration it replaced is still one call away."""
    with connect(migrated_db) as conn:
        _seed(conn)
        weighted = assemble(conn, [A, C], BOTH, 10, _HashEmbedder())
        plain = assemble(
            conn, [A, C], BOTH, 10, _HashEmbedder(), propose_weights=False
        )

    assert weighted["routing"]["weights"] != plain["routing"]["weights"]
    assert set(plain["routing"]["weights"].values()) == {1.0}
