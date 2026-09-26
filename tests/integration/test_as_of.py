"""Reading the history the store has always kept.

Every fact has carried t_valid and t_invalid since the first migration, and
the read path only ever asked "is it current". So the store paid to keep the
whole record of what a scope believed and could not answer one question about
it - including the first question a post mortem asks, which is what we
thought was true when the decision was made.

Supersession is what makes this more than a curiosity. Writing the same
(source, target, relation_type) again does not edit the old fact, it ends it:
the old edge gets a t_invalid and stays queryable. So the history is real,
complete, and until now unreachable.
"""

from __future__ import annotations

import time

from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import query_memory

GROUP = "g1"
OLD = "the deploy branch is master"
NEW = "the deploy branch is release"


def _embedder():
    return VectorEmbedder({"deploy branch": REFERENCE, "the policy": REFERENCE,
                           OLD: REFERENCE, NEW: REFERENCE})


def _write(conn, embedder, text):
    return write_episode(
        conn, GROUP, "s1",
        [{"name": "deploy branch", "type": "thing"},
         {"name": "the policy", "type": "thing"}],
        [{"source": "deploy branch", "target": "the policy",
          "relation_type": "is", "fact": text, "confidence": "extracted"}],
        {}, embedder, assume_new=True,
    )


def _supersede(conn):
    """Two writes of one triple, with the moment between them.

    t_valid is second granularity, so the second write has to land in a later
    second than the first or "before the change" and "after it" are the same
    instant and the test proves nothing.
    """
    embedder = _embedder()
    first = _write(conn, embedder, OLD)
    assert len(first["edges_created"]) == 1
    time.sleep(1.1)
    between = int(time.time())
    time.sleep(1.1)
    second = _write(conn, embedder, NEW)
    assert second["superseded"], "precondition: the same triple must supersede"
    return embedder, between


def test_a_query_without_as_of_answers_with_the_present(migrated_db):
    conn = connect(migrated_db)
    embedder, _ = _supersede(conn)

    facts = [f["fact"] for f in query_memory(conn, GROUP, OLD, 10, embedder)["facts"]]

    assert NEW in facts
    assert OLD not in facts, "a superseded fact is not the live answer"


def test_as_of_before_the_change_answers_with_what_was_believed_then(migrated_db):
    conn = connect(migrated_db)
    embedder, between = _supersede(conn)

    facts = [
        f["fact"]
        for f in query_memory(conn, GROUP, OLD, 10, embedder, as_of=between)["facts"]
    ]

    assert OLD in facts, "the fact that was true then has to come back"
    assert NEW not in facts, "a fact written later must not leak backwards"


def test_as_of_before_anything_existed_returns_nothing(migrated_db):
    """A scope has a beginning, and asking before it is a real question with
    an empty answer rather than an error."""
    conn = connect(migrated_db)
    embedder, _ = _supersede(conn)

    result = query_memory(conn, GROUP, OLD, 10, embedder, as_of=1)

    assert result["facts"] == []


def test_a_digest_can_be_taken_as_of_a_moment(migrated_db):
    """The catch-me-up path is where this question gets asked most naturally:
    not "what is true" but "what did I know by then"."""
    conn = connect(migrated_db)
    embedder, between = _supersede(conn)

    then = query_memory(conn, GROUP, None, 10, embedder, digest=True, as_of=between)
    now = query_memory(conn, GROUP, None, 10, embedder, digest=True)

    assert [f["fact"] for f in then["facts"]] == [OLD]
    assert [f["fact"] for f in now["facts"]] == [NEW]


def test_as_of_is_refused_rather_than_coerced(migrated_db):
    """A caller passing a date string or milliseconds means something
    specific, and quietly answering about a different instant is worse than
    saying no."""
    conn = connect(migrated_db)
    embedder = _embedder()

    for bad in ("2026-09-26", 12.5, True, -1):
        result = query_memory(conn, GROUP, OLD, 10, embedder, as_of=bad)
        assert "error" in result, f"{bad!r} should be refused"
        assert "as_of" in result["error"]
