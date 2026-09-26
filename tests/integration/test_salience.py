"""Base-level activation over the read log, and why it is off.

Migration 0021 began recording which fact ids each read returned, in
September, and retrieval never looked. That is ACT-R's base-level activation
sitting on disk: frequency and recency in one number, where ten recalls last
year decay below three this week.

It measured worse on every shape (see the commit message for the table), so
these tests are about the mechanism being correct rather than about it being
on. A reordering that is wrong is worth catching whether or not anybody has
enabled it, because the next person to try this idea will start here.
"""

from __future__ import annotations

import time

from fake_embedder import REFERENCE, VectorEmbedder

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval import salience

GROUP = "g1"


def _write(conn, i):
    text = f"fact number {i} about the pool"
    embedder = VectorEmbedder({f"thing {i}": REFERENCE, "the pool": REFERENCE, text: REFERENCE})
    result = write_episode(
        conn, GROUP, "s1",
        [{"name": f"thing {i}", "type": "t"}, {"name": "the pool", "type": "t"}],
        [{"source": f"thing {i}", "target": "the pool", "relation_type": "about",
          "fact": text, "confidence": "extracted"}],
        {}, embedder, assume_new=True,
    )
    return result["edges_created"][0]


def _recall(conn, edge_ids, *, seconds_ago: int = 0):
    conn.execute(
        """INSERT INTO public.read_event
                  (group_id, kind, n_facts, injected_chars, returned_fact_ids, at)
           VALUES (%s, 'query', %s, 0, %s, now() - make_interval(secs => %s))""",
        (GROUP, len(edge_ids), list(edge_ids), seconds_ago),
    )


def test_a_fact_recalled_often_outranks_one_never_recalled(migrated_db):
    conn = connect(migrated_db)
    never, often = _write(conn, 1), _write(conn, 2)
    for n in range(5):
        _recall(conn, [often], seconds_ago=n * 3600 + 10)

    assert salience.rank(conn, GROUP, [never, often]) == [often, never]


def test_recency_beats_raw_frequency(migrated_db):
    """The point of the decay, and what separates this from a counter. Ten
    recalls a year ago should sit below three this week, because a memory
    that cannot forget is a memory that cannot prioritise."""
    conn = connect(migrated_db)
    old, recent = _write(conn, 1), _write(conn, 2)
    year = 365 * 24 * 3600
    for n in range(10):
        _recall(conn, [old], seconds_ago=year + n * 3600)
    for n in range(3):
        _recall(conn, [recent], seconds_ago=n * 3600 + 10)

    assert salience.rank(conn, GROUP, [old, recent]) == [recent, old]


def test_a_burst_of_reads_counts_once(migrated_db):
    """Otherwise an agent loop asking the same question ten times in a minute
    manufactures activation, and the fact it happened to return then rises
    for every unrelated question afterwards.

    Compared against a single read at the same moment, which isolates the
    collapsing from the decay. An earlier version compared a burst against
    three reads spread over hours and asserted the spread one won. It does
    not and should not: one retrieval thirty seconds ago genuinely outweighs
    three several hours old, which is the decay working. Asserting two
    mechanisms at once measured neither, and it passed locally and failed in
    CI because the margin was arithmetic noise.
    """
    conn = connect(migrated_db)
    bursty, once = _write(conn, 1), _write(conn, 2)
    for _ in range(10):
        _recall(conn, [bursty], seconds_ago=30)
    _recall(conn, [once], seconds_ago=30)

    ranked = salience.rank(conn, GROUP, [bursty, once])

    # Equal activation, so the reordering is a no-op and the input order
    # survives. Ten reads in one window are worth exactly one.
    assert ranked == [bursty, once]


def test_it_reorders_and_never_adds_or_drops(migrated_db):
    """The design constraint. A prior on facts is not a signal about this
    query, so it must not be able to put a fact into an answer that the
    content channels did not find."""
    conn = connect(migrated_db)
    ids = [_write(conn, i) for i in range(4)]
    _recall(conn, [ids[3]], seconds_ago=60)

    ranked = salience.rank(conn, GROUP, ids[:3])

    assert sorted(ranked) == sorted(ids[:3])
    assert ids[3] not in ranked


def test_a_scope_with_no_history_is_left_alone(migrated_db):
    """A new store has no read log, and shuffling its results on no evidence
    would be worse than doing nothing."""
    conn = connect(migrated_db)
    ids = [_write(conn, i) for i in range(3)]

    assert salience.rank(conn, GROUP, ids) == ids


def test_a_very_recent_read_does_not_divide_by_zero(migrated_db):
    """Age is whole seconds, and 0 ** -0.5 is a division by zero. A fact
    returned in this same second is the common case immediately after a
    query, not an edge case."""
    conn = connect(migrated_db)
    edge = _write(conn, 1)
    _recall(conn, [edge], seconds_ago=0)
    time.sleep(0.05)

    assert salience.rank(conn, GROUP, [edge]) == [edge]
