"""Retrieval can be asked for facts ABOUT an entity, not facts that resemble one.

The first customer to need this had no way to ask, so they retrieved by
resemblance and filtered on the fact TEXT at four call sites, word-boundary
matching names to drop facts that were real, well ranked, and about somebody
else. Their words for what that filter prevents: an adjacent but wrong entity
fact is "the same failure mode as hallucination, just laundered through a real
fact instead of an invented one".

Resemblance cannot express identity. A fact IS an edge between two nodes, so
"about X" is structural and exact, and no ranking improvement substitutes for
it.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory

GROUP = "g1"

# Two teams whose facts read almost identically, which is the case that defeats
# ranking: the wrong one genuinely resembles the question.
SA_FACT = "the team collapsed chasing 280 in the second innings"
NZ_FACT = "the team collapsed chasing 275 in the second innings"
PAIR_FACT = "the two sides drew the series one all"


def _embedder():
    return VectorEmbedder({
        "SA": REFERENCE,
        "NZ": unit_vector_at_angle(0.05),
        "season": unit_vector_at_angle(0.1),
        "the series": unit_vector_at_angle(0.2),
        SA_FACT: REFERENCE,
        NZ_FACT: unit_vector_at_angle(0.02),
        PAIR_FACT: unit_vector_at_angle(0.3),
        # The query strings the tests below ask with. This embedder is a dict
        # lookup, so anything it has not been told about raises rather than
        # returning a plausible vector, which is the right behaviour for a test
        # double and the reason they are listed.
        "the sides met": unit_vector_at_angle(0.3),
        "met": unit_vector_at_angle(0.3),
        "collapsed": REFERENCE,
        "anything": unit_vector_at_angle(0.9),
    })


def _seed(conn, embedder):
    for src, tgt, fact in (
        ("SA", "season", SA_FACT),
        ("NZ", "season", NZ_FACT),
        ("SA", "NZ", PAIR_FACT),
    ):
        write_episode(
            conn, GROUP, "s1",
            [{"name": src, "type": "team"}, {"name": tgt, "type": "thing"}],
            [{"source": src, "target": tgt, "relation_type": "about",
              "fact": fact, "confidence": "extracted"}],
            {}, embedder, assume_new=True,
        )


def test_one_entity_returns_only_its_own_facts(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    unscoped = query_memory(conn, GROUP, SA_FACT, 10, embedder)["facts"]
    scoped = query_memory(conn, GROUP, SA_FACT, 10, embedder, about=["NZ"])["facts"]

    # Precondition: unscoped retrieval really does return the wrong team's fact,
    # otherwise this test proves nothing about scoping.
    assert any(f["fact"] == NZ_FACT for f in unscoped)
    assert any(f["fact"] == SA_FACT for f in unscoped)

    texts = {f["fact"] for f in scoped}
    assert SA_FACT not in texts, "a fact about SA alone reached an NZ scoped read"
    assert texts <= {NZ_FACT, PAIR_FACT}


def test_a_pair_returns_the_edge_between_them(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    facts = query_memory(
        conn, GROUP, "the sides met", 10, embedder, about=["SA", "NZ"]
    )["facts"]

    assert [f["fact"] for f in facts] == [PAIR_FACT]


def test_a_pair_is_direction_free(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    forward = query_memory(conn, GROUP, "met", 10, embedder, about=["SA", "NZ"])
    backward = query_memory(conn, GROUP, "met", 10, embedder, about=["NZ", "SA"])

    assert [f["fact"] for f in forward["facts"]] == [PAIR_FACT]
    assert [f["fact"] for f in backward["facts"]] == [PAIR_FACT]


def test_an_unknown_entity_is_empty_not_the_nearest_thing(migrated_db):
    """The contract the customer asked for. Returning something adjacent is the
    failure they were guarding against, so an unresolvable name answers with
    nothing rather than with the best resemblance."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, SA_FACT, 10, embedder, about=["Australia"])

    assert result["facts"] == []
    assert result.get("scoped") is True


def test_a_pair_with_no_edge_between_them_is_empty(migrated_db):
    """Both names resolve and neither is a typo, and there is still no fact. That
    is real absence and it answers empty, not with each entity's own facts."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    facts = query_memory(
        conn, GROUP, "anything", 10, embedder, about=["NZ", "season"]
    )["facts"]
    assert [f["fact"] for f in facts] == [NZ_FACT]

    # season and SA share an edge, season and NZ share one, so pick a pair that
    # genuinely has none: SA paired with itself has no self edge.
    none = query_memory(conn, GROUP, "anything", 10, embedder, about=["SA", "SA"])
    assert none["facts"] == []


def test_matching_is_exact_not_substring(migrated_db):
    """"SA" must not match "season". Substring matching is the bug being fixed:
    a short entity name is a substring of longer unrelated words, which is how a
    team query returned a fact about a season."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    facts = query_memory(conn, GROUP, "collapsed", 10, embedder, about=["SA"])["facts"]

    # season's own facts are reachable only because season is an endpoint of
    # SA's edge, never because "SA" is inside the word "season".
    assert NZ_FACT not in {f["fact"] for f in facts}, (
        "NZ's fact reached an SA scoped read, so the name matched as a substring"
    )


def test_case_is_ignored(migrated_db):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    lower = query_memory(conn, GROUP, "met", 10, embedder, about=["sa", "nz"])
    assert [f["fact"] for f in lower["facts"]] == [PAIR_FACT]


def test_every_scoped_fact_still_carries_its_relevance_fields(migrated_db):
    """Scoping must not cost the fields a caller thresholds on."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    facts = query_memory(conn, GROUP, NZ_FACT, 10, embedder, about=["NZ"])["facts"]

    assert facts
    assert [f["rank"] for f in facts] == list(range(1, len(facts) + 1))
    for f in facts:
        assert f["score"] is not None
        assert f["matched"]


@pytest.mark.parametrize("bad", [[], ["a", "b", "c"], "SA", [""], [None]])
def test_a_malformed_about_is_refused_not_coerced(migrated_db, bad):
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(conn, GROUP, "anything", 10, embedder, about=bad)
    assert "error" in result, bad


def test_about_and_digest_are_refused_together(migrated_db):
    """A digest is built by a query that does not carry the filter, so accepting
    both would return other entities' facts while the caller believed the answer
    was scoped. Refused rather than silently unscoped."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    result = query_memory(
        conn, GROUP, None, 10, embedder, digest=True, about=["SA"]
    )
    assert "error" in result


def test_scoping_honours_as_of(migrated_db):
    """The two features compose: facts about an entity, as the store stood then."""
    conn = connect(migrated_db)
    embedder = _embedder()
    _seed(conn, embedder)

    past = query_memory(conn, GROUP, NZ_FACT, 10, embedder, about=["NZ"], as_of=1)
    assert past["facts"] == []
