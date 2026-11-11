"""A graph wide write gate, and lock keys wide enough not to collide.

## What this protects

`write_episode` takes `pg_advisory_xact_lock(hashtext('episode|' || group_id))`
as the first statement in its transaction, so two writers into ONE scope
serialise, and `test_two_agents_writing_one_triple_leave_one_active_edge` pins
that. Two writers in DIFFERENT scopes of the same graph take different keys and
proceed genuinely concurrently, which is correct for everything this package
owns: the facts, the nodes, the attestation chain and the corpus statistics are
all per scope, and nothing crosses.

Apache AGE's label catalogue is not per scope. `ag_catalog.ag_label` and each
backend's cache of it are per GRAPH, every scope's vertices live in the one
`echo_memory."Node"` table, and a graphid packs a label id and an entry id into
one int8. Under concurrent writers AGE has been observed building a relation
name out of garbage memory:

    relation "echo_memory.-775.1utterance" does not exist

Observed repeatedly when four benchmark shards wrote four scopes at once;
three of the four died, and the run still looked complete because each shard
reported its own progress. `-775.1` is a mangled graphid and `utterance` is an
entity TYPE string, so two unrelated values reached the same buffer.

So the gate is a graph wide key. One writer per graph, taken instead of
trusting that scope isolation at our layer means isolation at AGE's.

## What it costs

Write throughput becomes one writer per graph, full stop, and the measured
price is higher than "one writer's worth". Eight writer threads, 500 one-fact
episodes each into its own scope, 4,000 episodes per run, migrated from base
before every run, on Postgres 16 with AGE under Colima:

    configuration                      run 1     run 2   episodes/s
    no gate                            46.5s     40.2s     86 to 100
    gate, ECHO_MEMORY_MAX_GRAPH_WRITERS=8 (off)        57.2s        70
    gate, limit 1                     122.1s    112.8s     33 to 35
    one writer alone, 500 episodes              11.1s             45

2.6x to 2.8x slower in aggregate. The row that matters most is the last one:
with the gate on, eight writers achieve 33 to 35 episodes/s while a single
writer with no contention at all achieves 45. Serialising through one lock is
about 25% WORSE than not having the writers, because the handoff is not free.
Anyone sizing an ingress should plan for one writer's throughput minus a
quarter, not for one writer's throughput.

The third row isolates the wrapper from the lock: with the limit set to 8 the
gate takes nothing, but the outer `with conn.transaction():` it has to be
called inside is still paid, and that alone accounts for roughly a quarter of
the gap. The lock is the rest.

No failures in any configuration, which is the honest version of this: at 4, 8
and 16 concurrent cross-scope writers, and about 30,000 episodes, the
corruption did not reproduce on this hardware (see
tests/integration/test_graph_concurrency.py for exactly what was tried). The
gate is bought on the production report, not on a local repro. An ingress that
needs more than one writer per graph needs a different answer, and there are
two:

  1. A graph per tenant. AGE's catalogue is per graph, so separate graphs do
     not share the cache that corrupts. Costs a `create_graph` per tenant and
     makes cross scope retrieval a cross graph query.
  2. Separate databases. What cloud already does per customer.

Neither is in scope here, and neither is a thing this module should pretend to
decide.

## Why this module does not take the lock for you

`graph_write_gate` is not wired into `write_episode`. Where it belongs is a
one line decision in a shared file, and the gate has to be the FIRST statement
in the transaction, outside and before the per-scope lock, or two writers can
each hold one lock and wait for the other's.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager

from echo_memory.infra.db import GRAPH_NAME
from echo_memory.infra.logging import get_logger

_logger = get_logger("graphlock")

#: How many writers may be inside AGE's per graph write path at once. 1 is the
#: only value this module implements a limit for, and anything higher turns the
#: gate off rather than admitting exactly that many: a counting semaphore over
#: advisory locks would need a slot to poll for and a release path to forget,
#: and an operator who sets this to 4 is not asking for 4, they are saying they
#: accept the risk. Read per call, not at import, so a process can be told to
#: change its mind and so a test does not need to reload the module.
MAX_GRAPH_WRITERS_ENV = "ECHO_MEMORY_MAX_GRAPH_WRITERS"


def max_graph_writers() -> int:
    raw = os.environ.get(MAX_GRAPH_WRITERS_ENV, "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        _logger.warning(
            "max_graph_writers_unparseable",
            extra={"value": raw, "detail": "defaulting to 1"},
        )
        return 1


def lock_key(namespace: str, value: str) -> int:
    """A 64 bit advisory lock key for this value within this namespace.

    `hashtext()` returns int4, so the existing keys live in 2^32 values and
    two scopes can land on one. Over a store's scopes, by the birthday bound:

        scopes    32 bit (hashtext)    64 bit (here)
         1,000              0.0116%        1 in 3.7e11
        10,000               1.157%        1 in 3.7e11
       100,000              68.781%        1 in 3.7e7 at 1e6 scopes

    1.2% at ten thousand scopes is not a corruption risk. It is a tenancy
    one, and it falsifies a claim this package makes out loud: attestation's
    `attest()` says it locks "per scope rather than global because scopes are
    tenants, and one account's writes must not be able to block another's",
    which two colliding group_ids make untrue. They would block each other
    for the duration of a write, silently, and look like ordinary latency.

    Namespaced because `attest()` and `write_episode` take two different locks
    in the same lock space for the same scope, and a third caller that reused
    one of their keys would deadlock against nothing visible in its own code.
    The namespace goes into the digest rather than being XORed in, so there is
    no arithmetic relationship between two namespaces' keys for the same value.

    Signed, because `pg_advisory_xact_lock(bigint)` is signed and the whole
    point is to use the range: folding the top bit away would halve it.
    """
    digest = hashlib.sha256(f"{namespace}\x00{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def graph_write_gate(conn, graph: str = GRAPH_NAME):
    """Hold the graph wide write lock for the rest of this transaction.

    Must be called inside an open transaction and as early in it as possible.
    Transaction scoped like every other lock in this package, so it releases on
    commit or rollback with no unlock path to forget.

    Raises if the connection is not in a transaction. `pg_advisory_xact_lock`
    on an autocommit connection with no transaction block open is scoped to the
    implicit single statement transaction and is released before the next
    statement runs, so it reads as protection and provides none. Every
    connection this package opens is autocommit, so that mistake is one
    forgotten `with conn.transaction():` away, and this package has already
    paid for two silent no-ops that returned success.
    """
    from psycopg.pq import TransactionStatus

    if max_graph_writers() > 1:
        yield False
        return

    if conn.info.transaction_status == TransactionStatus.IDLE:
        raise RuntimeError(
            "graph_write_gate needs an open transaction: a transaction scoped "
            "advisory lock taken outside one is released before the next "
            "statement and protects nothing. Wrap the write in "
            "`with conn.transaction():` and take the gate as its first statement."
        )

    conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (lock_key("graph", graph),))
    yield True
