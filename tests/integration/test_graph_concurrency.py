"""Concurrent writers in DIFFERENT scopes of one graph.

`test_two_agents_writing_one_triple_leave_one_active_edge` covers two writers
in ONE scope, and that case is genuinely safe: `write_episode` takes
`pg_advisory_xact_lock(hashtext('episode|' || group_id))` as its transaction's
first statement, so they serialise. Nothing covered two writers in two scopes,
which is a different situation entirely, because the lock they take is
different and AGE's label catalogue is not.

## What was observed in production, and is NOT reproduced here

`events/envelope.py` records the failure this file exists for: four benchmark
shards writing four scopes of one graph at once, three of the four dying with

    relation "echo_memory.-775.1utterance" does not exist

`-775.1` is a mangled graphid and `utterance` is an entity TYPE string, so two
unrelated values reached one buffer inside AGE.

**It did not reproduce on this hardware.** Everything tried, on Postgres 16
with AGE under Colima, 2026-11-11, each episode one fact with both mentions
asserted new:

    writers   episodes each   total   graph before   errors
          4              50     200              0        0
          8             300   2,400            200        0
          8           1,000   8,000          2,400        0
          8             500   4,000              0        0  (twice)
         16             300   4,800              0        0

About 30,000 cross-scope concurrent episodes, no corruption, no mangled
relation name, no label catalogue damage. So the tests below are regression
guards that assert nothing goes wrong, NOT a reproduction, and they are not
marked xfail because on this machine they genuinely pass.

That is a real limit on what this file proves, and it is worth naming rather
than hiding. The production failure had three things this does not: separate
OS processes rather than threads, a scope of thousands of LoCoMo turns per
shard instead of hundreds of synthetic facts, and a machine under a benchmark's
full load. Any of those could be the condition. What this file can honestly say
is "eight writers across sixteen scopes and thirty thousand episodes on one
laptop did not trigger it", which is useful - it bounds where to look next -
and is not the same as "the bug is gone".

`test_the_premise_holds` is the one that earns its place regardless: it shows
that cross-scope writers DO overlap in time today, so the hazard window is
real whether or not this hardware can make AGE fall into it.

## What DID reproduce, and why it points somewhere else

While integrating these streams the fault appeared on this same hardware, from
a different cause. The exact error, during the `migrated_db` fixture's
teardown:

    psycopg.errors.InternalError_: label (relation) cache corrupted
    [SQL: DELETE FROM alembic_version WHERE alembic_version.version_num = '0001']

At that moment five OTHER databases on the same Postgres container were each
running their own `alembic upgrade`/`downgrade base` cycles, which is
`create_graph` and `drop_graph` over and over. `pg_stat_activity` showed 13
backends across 5 databases. The same test passed in isolation in 9 seconds.

So on this machine the write path survived roughly 30,000 concurrent
cross-scope episodes untouched, while concurrent graph DDL corrupted the cache
on the first serious attempt. That is evidence the trigger is more likely
`create_graph`/`drop_graph` contention than concurrent INSERTs, and it widens
the exposure in a direction the write gate does not cover: a CI runner that
migrates several test databases in parallel is itself a reproducer.

It does not settle it. The production failure was writers, not migrations, and
one observation is not a characterisation. What it does mean is that anybody
chasing this next should start with DDL rather than with write volume, and that
a deployment which never creates or drops a graph at runtime - which is every
deployment, since labels are only ever created in migration 0001 - is exposed
mainly at migration time.

ECHO_MEMORY_CONCURRENCY_SOAK=1 raises the load from 4x40 to 8x500, which takes
about two minutes instead of about three seconds. The default is small on
purpose: a regression guard everybody skips because it is slow guards nothing.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
import traceback

import pytest

from echo_memory.infra import graphlock
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.infra.graphlock import graph_write_gate
from echo_memory.ingestion.write_episode import write_episode

SOAK = os.environ.get("ECHO_MEMORY_CONCURRENCY_SOAK") == "1"
N_WRITERS = 8 if SOAK else 4
N_EPISODES = 500 if SOAK else 40

#: The labels migration 0001 creates, and the only ones this package ever has.
#: The corruption signature is a relation name built from garbage, so the
#: catalogue is worth checking directly rather than inferring from "no error".
EXPECTED_LABELS = {
    "_ag_label_vertex": f"{GRAPH}._ag_label_vertex",
    "_ag_label_edge": f"{GRAPH}._ag_label_edge",
    "Node": f'{GRAPH}."Node"',
    "FACT": f'{GRAPH}."FACT"',
}


class HashEmbedder:
    """Deterministic, offline, and cheap enough not to be the bottleneck.

    The real LocalEmbedder would make this a test of sentence-transformers'
    thread safety at 400 to 4,000 embeddings per run. What is under test is
    what the DATABASE does with concurrent writers, so the embedder has to get
    out of the way. Not VectorEmbedder either: that one raises on unregistered
    text by design, and these runs generate thousands of distinct strings."""

    dimension = 384

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [(digest[i % 32] - 128) / 128.0 for i in range(self.dimension)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _episode(conn, group: str, writer: int, n: int, embedder):
    """One fact into one scope. Both mentions asserted new, so no two writers
    can contend on entity resolution and the only thing left to contend on is
    the graph itself."""
    source, target = f"svc-{group}-{n}", f"host-{group}-{n}"
    return write_episode(
        conn, group, f"sess-{writer}",
        [{"name": source, "type": "service"}, {"name": target, "type": "env"}],
        [{"source": source, "target": target, "relation_type": "runs_on",
          "fact": f"{source} runs on {target}", "confidence": "extracted"}],
        {source: {"resolved_to": "new"}, target: {"resolved_to": "new"}},
        embedder, assume_new=True,
    )


class Run:
    """What a concurrent run produced: the failures, the counts, and when each
    writer held the graph."""

    def __init__(self):
        self.errors: list[tuple[int, str]] = []
        self.refusals: list[tuple[int, str]] = []
        self.written = 0
        self.holds: list[tuple[int, float, float]] = []
        self.lock = threading.Lock()


def _run_concurrently(
    url: str, *, gated: bool, writers: int, episodes: int,
    limit: str = "1",
) -> Run:
    """Run `writers` threads, each into its own scope, optionally gated.

    `gated=True` sets ECHO_MEMORY_MAX_GRAPH_WRITERS=1 for the duration, because
    the gate is off unless an operator asks for it. That is the point rather
    than an inconvenience: this test now exercises what a person who turns the
    protection on actually gets, which is what the gate is for. Before the
    default was reversed it exercised what everybody got by accident.

    Set on the process rather than through monkeypatch because the writers are
    threads and read the same os.environ, and restored in a finally so a gated
    run cannot leave the gate on for whatever test follows it.

    `limit` is a parameter so that the one test which wants the gate REQUESTED
    and then disabled - an operator who set 8 and accepted the risk - can say
    so without fighting this function for the variable.
    """
    run = Run()
    barrier = threading.Barrier(writers)

    def writer(i: int) -> None:
        group = f"concurrent-{i}"
        embedder = HashEmbedder()
        try:
            conn = connect(url)
        except Exception:  # noqa: BLE001 - recording every failure is the point
            with run.lock:
                run.errors.append((i, traceback.format_exc()))
            barrier.abort()
            return
        try:
            barrier.wait(timeout=60)
            for n in range(episodes):
                try:
                    if gated:
                        # The gate's hold starts when it returns and ends at
                        # commit, so [entered, left] sits strictly inside it.
                        # Overlapping intervals would mean the gate let two
                        # writers in at once.
                        with conn.transaction(), graph_write_gate(conn):
                            entered = time.perf_counter()
                            result = _episode(conn, group, i, n, embedder)
                            left = time.perf_counter()
                    else:
                        entered = time.perf_counter()
                        result = _episode(conn, group, i, n, embedder)
                        left = time.perf_counter()
                    with run.lock:
                        run.holds.append((i, entered, left))
                        if result.get("edges_created"):
                            run.written += 1
                        else:
                            run.refusals.append((i, repr(result)[:300]))
                except Exception:  # noqa: BLE001 - recording every failure is the point
                    with run.lock:
                        run.errors.append((i, traceback.format_exc()))
                    return
        finally:
            conn.close()

    previous = os.environ.get(graphlock.MAX_GRAPH_WRITERS_ENV)
    if gated:
        os.environ[graphlock.MAX_GRAPH_WRITERS_ENV] = limit
    elif previous is not None:
        del os.environ[graphlock.MAX_GRAPH_WRITERS_ENV]

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=900)

    if previous is None:
        os.environ.pop(graphlock.MAX_GRAPH_WRITERS_ENV, None)
    else:
        os.environ[graphlock.MAX_GRAPH_WRITERS_ENV] = previous
    assert not any(t.is_alive() for t in threads), "a writer thread never finished"
    return run


def _overlapping(holds: list[tuple[int, float, float]]) -> list[tuple]:
    """Pairs of holds by DIFFERENT writers whose intervals intersect."""
    ordered = sorted(holds, key=lambda h: h[1])
    pairs = []
    for index, (writer, start, end) in enumerate(ordered):
        for other, other_start, other_end in ordered[index + 1:]:
            if other_start >= end:
                break
            if other != writer and other_start < end and start < other_end:
                pairs.append(((writer, start, end), (other, other_start, other_end)))
    return pairs


def _labels(conn) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name::text, relation::text FROM ag_catalog.ag_label"
        ).fetchall()
    }


def _active_facts(conn, group: str) -> int:
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->()
            WHERE e.group_id = '{group}' AND e.t_invalid IS NULL
            RETURN count(e)
        $$) AS (n agtype)"""
    ).fetchone()
    return int(str(row[0]))


# --- the premise --------------------------------------------------------


def test_the_premise_holds(migrated_db):
    """Writers in different scopes genuinely overlap in time.

    This is the fact the whole stream rests on and the only one here that can
    be shown positively: the per-scope lock does not serialise cross-scope
    writers, so AGE sees concurrent writers whatever our layer believes. If
    this ever fails, something else started serialising writes and the gate
    below is redundant - which would be good news and should be noticed."""
    run = _run_concurrently(migrated_db, gated=False, writers=4, episodes=15)

    assert not run.errors, run.errors[0][1]
    assert _overlapping(run.holds), (
        "no two cross-scope writes overlapped, so this run did not create the "
        "condition the rest of this file is about"
    )


# --- the regression guards ---------------------------------------------


def test_concurrent_writers_in_different_scopes_all_succeed(migrated_db):
    """The attempted reproduction, kept as a guard.

    Not xfail: it passes here, repeatedly, at up to 16 writers and 30,000
    episodes (see the module docstring for the full table). What it would
    catch is the failure getting WORSE - reproducible on ordinary hardware at
    ordinary load - which is exactly when somebody needs to know."""
    run = _run_concurrently(
        migrated_db, gated=False, writers=N_WRITERS, episodes=N_EPISODES
    )

    assert not run.errors, (
        f"{len(run.errors)} of {N_WRITERS} writers raised; first:\n{run.errors[0][1]}"
    )
    assert not run.refusals, f"a write was refused rather than stored: {run.refusals[0]}"
    assert run.written == N_WRITERS * N_EPISODES


def test_the_label_catalogue_survives_concurrent_writers(migrated_db):
    """The corruption signature is a relation name built out of garbage, so
    check the catalogue rather than only the absence of an exception. A
    mangled entry that no query happened to touch would leave every assertion
    above green and the graph permanently broken."""
    run = _run_concurrently(
        migrated_db, gated=False, writers=N_WRITERS, episodes=min(N_EPISODES, 60)
    )
    assert not run.errors, run.errors[0][1]

    conn = connect(migrated_db)
    try:
        assert _labels(conn) == EXPECTED_LABELS
    finally:
        conn.close()


def test_every_scope_can_still_be_read_back(migrated_db):
    """A write that returned an edge id is not a write whose scope is
    readable. The failure mode being guarded against breaks the table NAME, so
    the damage shows up on the next read, in a different session, long after
    the writer reported success."""
    episodes = min(N_EPISODES, 60)
    run = _run_concurrently(
        migrated_db, gated=False, writers=N_WRITERS, episodes=episodes
    )
    assert not run.errors, run.errors[0][1]

    conn = connect(migrated_db)
    try:
        for i in range(N_WRITERS):
            assert _active_facts(conn, f"concurrent-{i}") == episodes
    finally:
        conn.close()


# --- the mitigation ----------------------------------------------------


def test_the_gate_serialises_writers_across_scopes(migrated_db):
    """What the gate is for, shown rather than asserted: no two writers hold
    the graph at the same time.

    The interval is from the gate returning to just before commit, and the
    lock is held past that point, so genuinely non-overlapping intervals are
    necessary and slightly conservative. An off-by-one in the gate - keying on
    the scope, say - would show up here as overlaps, where
    tests/unit/test_graphlock.py can only check which statement was sent."""
    run = _run_concurrently(
        migrated_db, gated=True, writers=N_WRITERS, episodes=min(N_EPISODES, 40)
    )

    assert not run.errors, run.errors[0][1]
    overlaps = _overlapping(run.holds)
    assert not overlaps, (
        f"{len(overlaps)} overlapping holds under the gate; first pair: {overlaps[0]}"
    )


def test_the_gate_does_not_lose_or_deadlock_a_write(migrated_db):
    """Two locks taken in one transaction is a deadlock waiting for an
    ordering mistake: the gate takes the graph key and `write_episode` then
    takes the scope key, and `attest` takes a third. All three are only ever
    taken in that order, so there is no cycle - but that is an argument, and
    this is the test.

    A deadlock here surfaces as a DeadlockDetected from one writer, or as a
    thread that never finishes, which `_run_concurrently` fails on."""
    episodes = min(N_EPISODES, 40)
    run = _run_concurrently(
        migrated_db, gated=True, writers=N_WRITERS, episodes=episodes
    )

    assert not run.errors, run.errors[0][1]
    assert run.written == N_WRITERS * episodes

    conn = connect(migrated_db)
    try:
        assert _labels(conn) == EXPECTED_LABELS
        for i in range(N_WRITERS):
            assert _active_facts(conn, f"concurrent-{i}") == episodes
    finally:
        conn.close()


def test_the_gate_off_by_env_still_writes_everything(migrated_db):
    """An operator who sets the limit above 1 accepts the corruption risk, not
    a broken write path. The no-op branch still has to leave every fact
    stored."""
    episodes = min(N_EPISODES, 30)
    run = _run_concurrently(
        migrated_db, gated=True, writers=N_WRITERS, episodes=episodes, limit="8",
    )

    assert not run.errors, run.errors[0][1]
    assert run.written == N_WRITERS * episodes
    assert _overlapping(run.holds), "the gate serialised writers while disabled"


@pytest.mark.skipif(not SOAK, reason="ECHO_MEMORY_CONCURRENCY_SOAK=1 to run")
def test_sixteen_writers(migrated_db):
    """The highest concurrency tried, kept runnable rather than only recorded
    in a docstring. 4,800 episodes, about a minute."""
    run = _run_concurrently(migrated_db, gated=False, writers=16, episodes=300)

    assert not run.errors, f"{len(run.errors)} writers raised; first:\n{run.errors[0][1]}"
    assert run.written == 16 * 300

    conn = connect(migrated_db)
    try:
        assert _labels(conn) == EXPECTED_LABELS
    finally:
        conn.close()
