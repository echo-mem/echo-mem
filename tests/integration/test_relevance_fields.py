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
FAR = "LEXQUERY the office coffee machine was replaced"
# Reached only by the one hop expansion. It shares the "checkout" node with
# NEAR, so a hop from NEAR finds it, and it is far enough from the query for
# the vector floor to reject it and shares no term with the query for full
# text search to match, so neither content channel returns it.
HOP = "the ledger reconciles nightly against the bank"
# Vector-matches NEAR exactly and matches nothing lexically, so NEAR is found
# by the vector channel alone. A query that matched NEAR by its words would
# also match HOP through the shared "checkout" entity name, and the hop would
# never be the only thing that found it.
GRAPHSEED = "GRAPHSEED"


def _embedder():
    return VectorEmbedder({
        "pool size": REFERENCE,
        "checkout": unit_vector_at_angle(0.9),
        "coffee machine": unit_vector_at_angle(-0.8),
        "the office": unit_vector_at_angle(-0.7),
        # The query in the lexical-only test. Deliberately far from both
        # stored facts so the vector channel's floor rejects them and only
        # full text search can match.
        "LEXQUERY": REFERENCE,
        GRAPHSEED: REFERENCE,
        "ledger": unit_vector_at_angle(-0.85),
        NEAR: REFERENCE,
        FAR: unit_vector_at_angle(0.0),
        HOP: unit_vector_at_angle(-0.9),
    })


def _seed(conn, embedder, with_neighbour: bool = False):
    edges = [
        ("pool size", "checkout", NEAR),
        ("coffee machine", "the office", FAR),
    ]
    if with_neighbour:
        edges.append(("checkout", "ledger", HOP))
    for src, tgt, fact in edges:
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


def test_a_lexical_only_fact_still_gets_a_real_score(migrated_db):
    """The null here cost a customer a correct answer and put a hallucination
    in its place.

    score was omitted when only full text search found a fact, because only
    the vector channel computes a similarity. A customer sorted by score with
    nulls last, so every lexically-found fact went to the back of the list,
    where a character budget truncated it - and the model filled the gap by
    inventing a number. The dropped fact was correct and was in the store.

    Their own calibration says the ordering was backwards as well as
    arbitrary: over ten questions with every fact judged against every
    question, lexical-only scored R@3 0.640 against vector-only's 0.460. The
    channel being demoted had the better recall.

    So the number is computed rather than omitted. Which channel retrieved a
    fact is an implementation detail and has no business reaching a field
    callers read as relevance.
    """
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, "LEXQUERY", 10, embedder)

    lexical_only = [f for f in result["facts"] if f["matched"] == ["lexical"]]
    assert lexical_only, "precondition: this query has to produce a lexical-only hit"
    for fact in lexical_only:
        assert fact["score"] is not None, fact


def test_no_returned_fact_is_missing_a_score(migrated_db):
    """The general form, so the next channel added cannot reintroduce it.
    Sorting by a field some rows lack is a trap whatever the reason."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    for query in ("LEXQUERY", NEAR):
        facts = query_memory(conn, GROUP, query, 10, embedder)["facts"]
        assert facts, query
        missing = [f for f in facts if f.get("score") is None]
        assert not missing, f"{query!r} returned facts with no score: {missing}"


def test_a_digest_ranks_nothing_so_it_claims_nothing(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, None, 10, embedder, digest=True)

    assert result["facts"]
    for fact in result["facts"]:
        assert "score" not in fact
        assert "rank" not in fact


def test_a_fact_the_graph_hop_reached_says_which_channel_found_it(migrated_db):
    """The hop was not a channel, so its facts came back claiming nothing.

    `_annotate` built `matched` from the vector and lexical id sets and
    skipped anything in neither. A fact the one hop expansion added is in
    neither by construction - adding what the content channels missed is the
    entire point of it - so it fell through with no rank, no score and no
    matched, while the README said every returned fact carries all three.

    That exact shape has already cost this project a customer answer once: a
    fact with a null score sorted to the back of a list, got truncated by a
    character budget, and the model invented a replacement. The null here was
    waiting to do it again behind a flag.
    """
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder, with_neighbour=True)

    facts = query_memory(conn, GROUP, GRAPHSEED, 10, embedder, graph_hops=1)["facts"]

    by_graph = [f for f in facts if f["matched"] == ["graph"]]
    assert by_graph, (
        "precondition: the hop has to be the only channel that found something. "
        f"got {[(f['fact'], f.get('matched')) for f in facts]}"
    )
    assert any(f["fact"] == HOP for f in by_graph)
    for fact in by_graph:
        assert fact["rank"] is not None, fact
        assert fact["score"] is not None, fact


def test_nothing_the_hop_adds_is_missing_rank_score_or_matched(migrated_db):
    """The general form over CHANNELS, not just over queries.

    `test_no_returned_fact_is_missing_a_score` already claimed to be the
    general form so the next channel could not reintroduce the null. It
    generalised over queries and never turned the hop on, so the next channel
    reintroduced it and the test stayed green. Turning the hop on is the part
    that matters.
    """
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder, with_neighbour=True)

    for query in (GRAPHSEED, NEAR, "LEXQUERY"):
        facts = query_memory(conn, GROUP, query, 10, embedder, graph_hops=1)["facts"]
        assert facts, query
        for field in ("rank", "score", "matched"):
            missing = [f["fact"] for f in facts if f.get(field) is None]
            assert not missing, f"{query!r} returned facts with no {field}: {missing}"
        assert [f["rank"] for f in facts] == list(range(1, len(facts) + 1)), query


def test_the_hop_being_on_does_not_make_a_digest_claim_anything(migrated_db):
    """A digest ranks nothing against nothing, and the new channel must not
    change that. `graph_ids` is empty on every path but a hop that ran, which
    is what keeps the skip in `_annotate` doing its other job."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder, with_neighbour=True)

    result = query_memory(
        conn, GROUP, None, 10, embedder, digest=True, graph_hops=1
    )

    assert result["facts"]
    for fact in result["facts"]:
        assert "score" not in fact
        assert "rank" not in fact
        assert "matched" not in fact
