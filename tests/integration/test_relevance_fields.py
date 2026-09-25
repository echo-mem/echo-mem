"""Retrieval says why each fact is in the answer.

It used to return an ordered list and nothing else, so a caller who wanted
"only use this if it is really about my question" had no number to test
against. The first customer to hit that wrote relevance filters at four call
sites, reconstructing from the fact text something retrieval already knew and
discarded.

score is the field that matters, and specifically NOT the fusion score: RRF
values are sums of 1/(k+rank) and mean nothing from one query to the next.
Cosine similarity to the query does, which is why it is the one exposed.
"""

from __future__ import annotations

from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory

GROUP = "g1"
NEAR = "the pool was sized 5 and checkout returned 502s"
FAR = "the office coffee machine was replaced"


def _embedder():
    return VectorEmbedder({
        "pool size": REFERENCE,
        "checkout": unit_vector_at_angle(0.9),
        "coffee machine": unit_vector_at_angle(-0.8),
        "the office": unit_vector_at_angle(-0.7),
        # The query in the lexical-only test. Deliberately far from both
        # stored facts so the vector channel's floor rejects them and only
        # full text search can match.
        "coffee machine replaced": unit_vector_at_angle(0.0),
        NEAR: REFERENCE,
        FAR: unit_vector_at_angle(-0.75),
    })


def _seed(conn, embedder):
    for src, tgt, fact in (
        ("pool size", "checkout", NEAR),
        ("coffee machine", "the office", FAR),
    ):
        write_episode(
            conn, GROUP, "s1",
            [{"name": src, "type": "thing"}, {"name": tgt, "type": "thing"}],
            [{"source": src, "target": tgt, "relation_type": "about",
              "fact": fact, "confidence": "extracted"}],
            {}, embedder, assume_new=True,
        )


def test_every_ranked_fact_says_where_it_ranked_and_why(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, NEAR, 10, embedder)
    facts = result["facts"]

    assert facts, "precondition: the query returns something"
    assert [f["rank"] for f in facts] == list(range(1, len(facts) + 1))
    assert facts[0]["fact"] == NEAR
    assert "vector" in facts[0]["matched"]


def test_score_is_similarity_to_the_query_not_a_fusion_number(migrated_db):
    """A caller thresholding on this needs it to mean the same thing every
    time. Cosine does; a sum of 1/(k+rank) does not."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, NEAR, 10, embedder)
    best = result["facts"][0]

    assert best["score"] is not None
    assert 0.99 <= best["score"] <= 1.0, best["score"]
    for fact in result["facts"][1:]:
        if fact["score"] is not None:
            assert fact["score"] <= best["score"]


def test_a_lexical_only_hit_reports_no_score_rather_than_a_fake_one(migrated_db):
    """The lexical channel computes no similarity, so there is none to
    report. null says that; a zero would read as "computed, and nothing"."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, "coffee machine replaced", 10, embedder)
    lexical_only = [
        f for f in result["facts"]
        if f.get("matched") == ["lexical"]
    ]
    for fact in lexical_only:
        assert fact["score"] is None


def test_a_digest_ranks_nothing_so_it_claims_nothing(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, None, 10, embedder, digest=True)

    assert result["facts"]
    for fact in result["facts"]:
        assert "score" not in fact
        assert "rank" not in fact
