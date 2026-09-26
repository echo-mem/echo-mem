"""The lexical channel ranks by BM25, and the point is that it discriminates.

ts_rank counts occurrences. It has no inverse document frequency, so a word
in every fact of a scope counts for exactly as much as one in three of them.
Measured on 324 real prompts in this store, the rank-3 and rank-4 scores were
identical in 59% of them, and the prompt hook injects three facts - so which
fact a user saw was decided by Postgres scan order.

That matters more than it looks because reciprocal rank fusion reads rank
position and never the score. A channel ordered by coin flip hands the fusion
a coin flip.

These tests are about discrimination, not about a formula being transcribed
correctly. The question each one asks is whether the ordering now carries
information it demonstrably did not carry before.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval import bm25
from echo_memory.retrieval.query_memory import prompt_terms

GROUP = "g1"

# One term in every fact, one term in exactly one. Under ts_rank a query for
# both scores the whole corpus about equally, because the useless term counts
# as much as the decisive one.
UBIQUITOUS = "the deploy pipeline ran at stage {i}"
RARE = "the deploy pipeline logged kohli scoring 14941 runs"


def _embedder(texts):
    return VectorEmbedder(dict.fromkeys(texts, REFERENCE))


def _seed(conn, n: int = 60):
    """Enough facts to clear MIN_DOCS, because below it the statistics
    describe noise and the code deliberately declines to use them."""
    texts = [UBIQUITOUS.format(i=i) for i in range(n)] + [RARE]
    names = [f"stage {i}" for i in range(n)] + ["kohli record"]
    embedder = _embedder(texts + names + ["deploy pipeline"])
    for name, text in zip(names, texts, strict=True):
        write_episode(
            conn, GROUP, "s1",
            [{"name": "deploy pipeline", "type": "thing"}, {"name": name, "type": "thing"}],
            [{"source": "deploy pipeline", "target": name,
              "relation_type": "about", "fact": text, "confidence": "extracted"}],
            {}, embedder, assume_new=True,
        )
    return embedder


def _fact_text(conn, edge_id: str) -> str:
    (text,) = conn.execute(
        """SELECT * FROM cypher('echo_memory', $$
               MATCH ()-[e:FACT]->() WHERE id(e) = %s RETURN e.fact
           $$) AS (f agtype)""" % edge_id
    ).fetchone()
    return str(text).strip('"')


def test_a_rare_term_outranks_a_term_in_every_fact(migrated_db):
    """The whole argument for BM25 in one assertion. Both facts contain
    "deploy"; one also contains a word the corpus has seen once."""
    conn = connect(migrated_db)
    _seed(conn)
    bm25.refresh(conn, GROUP)

    ranked = bm25.candidates(conn, GROUP, prompt_terms("deploy kohli"), 5)

    assert ranked, "precondition: the query matches something"
    assert "kohli" in _fact_text(conn, ranked[0][0])
    best, runner_up = ranked[0][1], ranked[1][1]
    assert best > runner_up * 5, (
        "a term the corpus has seen once should outweigh one it has seen in "
        f"every fact by more than a rounding error: {best} vs {runner_up}"
    )


def test_ties_are_the_exception_rather_than_the_rule(migrated_db):
    """What was actually wrong. Under ts_rank the scores collapsed onto a
    handful of values and the ordering past the top was scan order wearing a
    number."""
    conn = connect(migrated_db)
    _seed(conn)
    bm25.refresh(conn, GROUP)

    ranked = bm25.candidates(conn, GROUP, prompt_terms("deploy kohli stage"), 10)
    scores = [round(score, 6) for _, score in ranked]

    assert len(set(scores)) > 1, f"every score identical, nothing discriminated: {scores}"


def test_statistics_are_refused_rather_than_trusted_when_stale(migrated_db):
    """A scope that has grown a lot since its last refresh has inverse
    document frequencies describing a corpus that no longer exists. Falling
    back to ts_rank is the honest answer; ranking confidently on stale
    statistics is not."""
    conn = connect(migrated_db)
    embedder = _seed(conn)
    bm25.refresh(conn, GROUP)
    assert bm25.usable(conn, GROUP)

    for i in range(40):
        write_episode(
            conn, GROUP, "s2",
            [{"name": "deploy pipeline", "type": "thing"},
             {"name": f"late stage {i}", "type": "thing"}],
            [{"source": "deploy pipeline", "target": f"late stage {i}",
              "relation_type": "about", "fact": f"a later fact number {i}",
              "confidence": "extracted"}],
            {}, VectorEmbedder({**{f"a later fact number {i}": REFERENCE},
                                f"late stage {i}": REFERENCE,
                                "deploy pipeline": REFERENCE}),
            assume_new=True,
        )

    assert not bm25.usable(conn, GROUP), "a scope two thirds larger is stale"

    bm25.refresh(conn, GROUP)
    assert bm25.usable(conn, GROUP)


def test_a_scope_too_small_to_have_a_corpus_declines(migrated_db):
    """Inverse document frequency over a handful of facts is a statement
    about noise. MIN_DOCS is where the code says so."""
    conn = connect(migrated_db)
    embedder = VectorEmbedder({"a": REFERENCE, "b": REFERENCE, "only fact here": REFERENCE})
    write_episode(
        conn, GROUP, "s1",
        [{"name": "a", "type": "t"}, {"name": "b", "type": "t"}],
        [{"source": "a", "target": "b", "relation_type": "r",
          "fact": "only fact here", "confidence": "extracted"}],
        {}, embedder, assume_new=True,
    )
    bm25.refresh(conn, GROUP)

    assert not bm25.usable(conn, GROUP)


def test_a_term_the_corpus_has_never_seen_is_maximally_rare(migrated_db):
    """Not a special case, a consequence: a term with no row has document
    frequency zero, which the formula turns into the largest weight it can
    produce. The common cause is a scope that grew since the last refresh,
    and treating an unknown word as decisive is the right reading of that."""
    conn = connect(migrated_db)
    _seed(conn)
    bm25.refresh(conn, GROUP)

    known = bm25.candidates(conn, GROUP, ["deploy"], 3)
    assert known, "precondition"

    conn.execute(
        "DELETE FROM public.lexical_term WHERE group_id = %s AND lexeme = %s",
        (GROUP, "deploy"),
    )
    unknown = bm25.candidates(conn, GROUP, ["deploy"], 3)

    assert unknown[0][1] > known[0][1]


@pytest.mark.parametrize("terms", [[], ["zzzznotaword"]])
def test_nothing_to_score_returns_nothing(migrated_db, terms):
    conn = connect(migrated_db)
    _seed(conn)
    bm25.refresh(conn, GROUP)

    assert bm25.candidates(conn, GROUP, terms, 5) == []
