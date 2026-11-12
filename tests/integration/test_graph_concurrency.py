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

**That write-path failure did not reproduce on this hardware.** Everything
tried, on Postgres 16 with AGE under Colima, 2026-11-11, each episode one fact
with both mentions asserted new:

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

## A dedicated forensic attempt, 2026-11-11, which also failed to reproduce

A separate Postgres 16 + AGE 1.5.0 container was started with nothing else on
it, specifically to reproduce either error form on demand. Six experiments:

    1  warm a backend's label cache, have ANOTHER backend drop and recreate
       the graph, then use the first one                     no corruption
    2  3 DDL cyclers + 4 autocommit readers, one database    no corruption
    3  4 DDL cyclers + 5 readers holding OPEN TRANSACTIONS,
       which is what alembic does                           no corruption
    4  6 databases running the real alembic upgrade and
       downgrade cycle concurrently                          too slow to
                                                             finish; see 5
    5  6 databases, 1,500 drop/create cycles each plus
       24,000 reads, 216,000 operations in 47s               no corruption
    6  4 databases cycling POPULATED graphs: 1,000 vertices,
       their edges, a pgvector HNSW index, real CASCADE work no corruption

Every error raised in 5 and 6 was a benign race with a CORRECT name: the graph
genuinely did not exist between a drop and a create. `relation
"echo_memory.Node" does not exist` appeared 8 times in each, which is the
production error's family but with the right table name, so it is a reader
inside the drop window rather than a corrupted cache entry.

So: **two first-hand sightings, zero reproductions on demand.** The error is
real and the exact text is recorded above and below. What it is not is a
simple race, because a simple race shows up in 216,000 attempts.

What both real sightings had and none of these six did is sustained heavy work
over a long period on a loaded machine: an eleven hour benchmark ingest in one
case, a fifteen minute test suite with five concurrent peers in the other.
That points at resource pressure or long-lived backend state rather than
concurrency alone, and it is where somebody chasing this next should look.
It is a direction, not a cause, and this file does not claim otherwise.

The actionable conclusions do not depend on resolving it:

  - the write gate does not cover the DDL path at all, which is now pinned by
    `test_graph_gate_boundary.py` rather than left to be inferred
  - bulk ingest into one graph should be single writer, which is what the
    gate is for and why it exists despite not reproducing here
  - a CI runner should not migrate several databases of one instance at once,
    which is an operational rule and not something a lock can enforce, because
    advisory lock keys are scoped to a database

`test_the_premise_holds` is the one that earns its place regardless: it shows
that cross-scope writers DO overlap in time today, so the hazard window is
real whether or not this hardware can make AGE fall into it.

## The corruption that WAS captured, which is a different mechanism

On 2026-11-11 the same underlying AGE cache bug was reproduced on this project,
and it is worth recording verbatim because it is the only first-hand evidence
anyone here has:

    sqlalchemy.exc.InternalError: (psycopg.errors.InternalError_)
    label (relation) cache corrupted
    [SQL: DELETE FROM alembic_version WHERE alembic_version.version_num = '0001']

It fired during `alembic downgrade base` - this suite's own `migrated_db`
teardown - while five databases of one Postgres container were each running
their own migrate and drop_graph cycle. `pg_stat_activity` showed 13 backends
across 5 databases at that moment (echo_s1_test 6, echo_s3_across 3,
echo_s5_test 3, echo_s7_test 1, postgres 1). The same test passed in isolation.

Two things that matters for, stated carefully:

**It is DDL contention, not write contention.** Nothing in it went through
`write_episode`. So it does NOT reproduce the failure the tests below are
written for, and this file does not claim it does. What it does establish is
that the cache bug is real on this hardware and reachable by concurrency that
is not writes, which is a wider exposure than the brief for this file assumed.

**It surfaced on a statement that never mentions a graph.** A DELETE against
`alembic_version`. AGE checks its label cache on ordinary statements too, so a
backend whose cache is poisoned fails on whatever it does next, which is why
this arrives looking like a migration bug rather than a graph bug.

Whether the FIVE DATABASES part is causal is not established by one
observation, and guessing would be worse than saying so. AGE's label cache is
backend-local and Postgres filters most invalidation by database, so the six
concurrent backends on the one database being downgraded are the likelier
locus. `test_concurrent_graph_ddl` below is the harness for settling it, and is
skipped by default because running it is destructive to every other database on
the instance.

**`graph_write_gate` would not have prevented this one**, on either reading.
It is not on the DDL path, and advisory lock keys are scoped to a database
anyway, so no lock taken in one database can serialise another's DDL. See
`infra/graphlock.py`'s "What this gate CANNOT protect".

ECHO_MEMORY_CONCURRENCY_SOAK=1 raises the load from 4x40 to 8x500, which takes
about two minutes instead of about three seconds. The default is small on
purpose: a regression guard everybody skips because it is slow guards nothing.
"""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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


# --- the DDL mechanism, as a harness rather than a default run ----------


DDL_DBS = [
    name.strip()
    for name in os.environ.get("ECHO_MEMORY_DDL_CONTENTION_DBS", "").split(",")
    if name.strip()
]


def _ddl_url(database: str) -> str:
    """The configured URL with its database name swapped, so credentials and
    port are never retyped into a test and can never point somewhere else."""
    parsed = urlsplit(os.environ["ECHO_MEMORY_DATABASE_URL"])
    if parsed.port != 5434 or not database.endswith("_test"):
        raise AssertionError(
            f"refusing DDL contention against {database!r} on port {parsed.port}: "
            "this test runs `alembic downgrade base` in a loop and would drop it. "
            "Name only disposable databases ending in _test on port 5434."
        )
    return urlunsplit(parsed._replace(path=f"/{database}"))


@pytest.mark.skipif(
    not DDL_DBS,
    reason="ECHO_MEMORY_DDL_CONTENTION_DBS=a_test,b_test,... to run; DESTRUCTIVE, "
           "see the test docstring",
)
def test_concurrent_graph_ddl_does_not_corrupt_the_label_cache():
    """The harness for the one corruption this project has actually captured.

        sqlalchemy.exc.InternalError: (psycopg.errors.InternalError_)
        label (relation) cache corrupted
        [SQL: DELETE FROM alembic_version WHERE alembic_version.version_num = '0001']

    Observed during `alembic downgrade base` with 13 backends across 5
    databases of one container each running migrate and drop_graph. See the
    module docstring for the full account and for what is and is not
    established by it.

    **Destructive, and not only to the databases named.** It is a reproducer
    for a bug whose whole character is that it poisons a backend and breaks
    whatever that backend does next, so firing it on a Postgres instance other
    people are using can fail their runs, in ways that look like their own
    migration bugs. Opt in by naming disposable `_test` databases on port 5434,
    on an instance nobody else is using.

    Skipped by default for that reason, not because it is slow. A reproducer
    that runs itself is a reproducer that breaks a colleague's afternoon.
    """
    cycles = int(os.environ.get("ECHO_MEMORY_DDL_CONTENTION_CYCLES", "3"))
    urls = [_ddl_url(name) for name in DDL_DBS]
    barrier = threading.Barrier(len(urls))
    failures: list[tuple[str, str]] = []
    lock = threading.Lock()

    def churn(url: str) -> None:
        env = {**os.environ, "ECHO_MEMORY_DATABASE_URL": url}
        barrier.wait(timeout=120)
        for _ in range(cycles):
            for argv in (["upgrade", "head"], ["downgrade", "base"]):
                # check=False deliberately: a failing cycle is the result this
                # test exists to collect, and raising here would lose the
                # stderr that says whether it was the cache corruption.
                done = subprocess.run(
                    [sys.executable, "-m", "alembic", *argv],
                    cwd=Path(__file__).parent.parent.parent,
                    env=env, capture_output=True, text=True, check=False,
                )
                if done.returncode != 0:
                    with lock:
                        failures.append((url, done.stderr[-1500:]))
                    return

    threads = [threading.Thread(target=churn, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=1800)

    assert not any(t.is_alive() for t in threads), "an alembic cycle never finished"
    corrupted = [f for f in failures if "cache corrupted" in f[1]]
    assert not failures, (
        f"{len(failures)} of {len(urls)} databases failed their DDL cycle"
        + (f", {len(corrupted)} with the label cache corruption" if corrupted else "")
        + f"; first:\n{failures[0][1]}"
    )
