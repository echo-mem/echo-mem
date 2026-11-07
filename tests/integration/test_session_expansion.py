"""Expanding to the rest of the conversation the best results came from.

Measured on BEAM 100K, 400 questions people wrote: the right conversation
came back 94.5% of the time and the right line in it 62.1%. For instruction
following those numbers are 97.5% and 10.0%. Retrieval is not lost; it
arrives in the right conversation and picks the wrong lines, because a
question like "have I been following the approach I said I would?" never
quotes the instruction it asks about.

These tests are about the mechanism rather than the benchmark: that the
expansion returns the session's other facts, that it is off unless asked,
that it cannot leak across sessions or scopes, and that it respects the same
scoping and as_of rules every other channel does.
"""

from __future__ import annotations

from fake_embedder import VectorEmbedder

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import _session_candidates, query_memory

GROUP = "g-sessions"

#: Orthogonal basis vectors. Every entity and every fact gets its own, so
#: nothing resolves onto anything else by accident and a query matches exactly
#: what it is meant to. An embedder that maps everything to one vector makes
#: every entity ambiguous, and write_episode DEFERS on ambiguity rather than
#: failing, so the second write silently stores nothing.
def basis(i: int) -> list[float]:
    return [0.0] * i + [1.0] + [0.0] * (383 - i)


QUESTION = "which branch do deploys run from"
BRANCH = "deploys run from the main branch"
#: Deliberately shares no word with the question, so neither the vector
#: channel (orthogonal) nor the lexical one can reach it. Only the session can.
INSTRUCTION = "seed the staging database by hand first"
COFFEE = "the office coffee machine is broken"

VECTORS = {
    QUESTION: basis(0),
    BRANCH: basis(0),
    INSTRUCTION: basis(1),
    COFFEE: basis(2),
    "deploys": basis(10), "main": basis(11),
    "staging": basis(12), "hand": basis(13),
    "coffee": basis(14), "office": basis(15),
}


def _write(conn, embedder, session: str, source: str, target: str, fact: str) -> str:
    result = write_episode(
        conn, GROUP, session,
        [{"name": source, "type": "x"}, {"name": target, "type": "x"}],
        [{"source": source, "target": target, "relation_type": "about",
          "fact": fact, "confidence": "extracted"}],
        {source: {"resolved_to": "new"}, target: {"resolved_to": "new"}},
        embedder, assume_new=True,
    )
    assert result["edges_created"], result
    return result["edges_created"][0]


def _seed(conn):
    """Two sessions. One holds the fact a query matches plus an instruction
    nothing would retrieve on its own; the other is an unrelated decoy."""
    embedder = VectorEmbedder(VECTORS)
    branch = _write(conn, embedder, "session-deploys", "deploys", "main", BRANCH)
    _write(conn, embedder, "session-deploys", "staging", "hand", INSTRUCTION)
    _write(conn, embedder, "session-office", "coffee", "office", COFFEE)
    return embedder, branch


def _texts(conn, edge_ids: list[str]) -> list[str]:
    if not edge_ids:
        return []
    rows = conn.execute(
        """SELECT (properties ->> '"fact"'::agtype)
             FROM "echo_memory"."FACT"
            WHERE id = ANY(%s::text[]::graphid[])""",
        (edge_ids,),
    ).fetchall()
    return [r[0] for r in rows]


def test_it_returns_the_rest_of_the_seeds_session(migrated_db):
    """The mechanism. Given a fact, the other facts written in the same
    `write_episode` session come back, and the seed itself does not: it is
    already in the content lists and re-ranking a fact against itself is what
    the fusion is for."""
    with connect(migrated_db) as conn:
        _, branch = _seed(conn)

        found = _session_candidates(conn, GROUP, [branch], 50)

        assert branch not in found, "the seed came back as its own neighbour"
        assert INSTRUCTION in _texts(conn, found)


def test_it_does_not_cross_into_another_session(migrated_db):
    """A session is the unit somebody wrote in one sitting. Pulling in a
    different one would be returning an unrelated conversation because one
    fact in it happened to rank."""
    with connect(migrated_db) as conn:
        _, branch = _seed(conn)

        found = _session_candidates(conn, GROUP, [branch], 50)

        assert COFFEE not in _texts(conn, found)


def test_no_seeds_returns_nothing_rather_than_everything(migrated_db):
    """An empty seed list must not become an unfiltered scan of the scope."""
    with connect(migrated_db) as conn:
        _seed(conn)
        assert _session_candidates(conn, GROUP, [], 50) == []


def test_expansion_brings_back_a_fact_the_question_does_not_resemble(migrated_db):
    """The failure this exists for, in miniature.

    The question retrieves the branch fact. The instruction written beside it
    shares no word with the question and no entity with the answer, so neither
    content channel nor a graph hop reaches it. The session does.

    This is the whole BEAM result in one case: session@10 0.945 against
    hit@10 0.621, and 0.975 against 0.100 on instruction following.
    """
    with connect(migrated_db) as conn:
        embedder, _ = _seed(conn)

        plain = query_memory(conn, GROUP, QUESTION, 5, embedder)
        expanded = query_memory(
            conn, GROUP, QUESTION, 5, embedder, expand_sessions=True,
        )

    assert INSTRUCTION not in [f["fact"] for f in plain["facts"]]
    assert INSTRUCTION in [f["fact"] for f in expanded["facts"]]


def test_it_is_off_unless_asked(migrated_db):
    """Like every other change to what a query returns. The flag exists so
    the eval can score it, not so it can arrive by default."""
    with connect(migrated_db) as conn:
        embedder, _ = _seed(conn)

        plain = query_memory(conn, GROUP, QUESTION, 5, embedder)

    assert INSTRUCTION not in [f["fact"] for f in plain["facts"]]


def test_the_expansion_does_not_reach_another_scope(migrated_db):
    """Tenancy. A session id is not unique across scopes - two accounts can
    both have `session-1` - so the expansion has to filter on group_id as
    well, or one account's conversation reaches another's answer."""
    with connect(migrated_db) as conn:
        _, branch = _seed(conn)
        other = VectorEmbedder(VECTORS)
        write_episode(
            conn, "g-other-tenant", "session-deploys",
            [{"name": "coffee", "type": "x"}, {"name": "office", "type": "x"}],
            [{"source": "coffee", "target": "office", "relation_type": "about",
              "fact": COFFEE, "confidence": "extracted"}],
            {"coffee": {"resolved_to": "new"}, "office": {"resolved_to": "new"}},
            other, assume_new=True,
        )

        found = _session_candidates(conn, GROUP, [branch], 50)

        assert COFFEE not in _texts(conn, found)


def test_the_expansion_respects_entity_scoping(migrated_db):
    """`about=` is an exact filter, and an expansion that ignored it would put
    facts about other entities back into a scoped answer - the whole failure
    that parameter removes."""
    with connect(migrated_db) as conn:
        embedder, _ = _seed(conn)

        scoped = query_memory(
            conn, GROUP, QUESTION, 10, embedder,
            about=["deploys"], expand_sessions=True,
        )

    assert COFFEE not in [f["fact"] for f in scoped["facts"]]
