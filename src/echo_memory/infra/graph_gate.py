"""Taking the graph wide gate from inside a transaction somebody else opened.

`graphlock.graph_write_gate` is a context manager, which is the right shape for
a caller that owns its transaction. `write_episode` does not: it is already
inside `with conn.transaction():` by the time it wants the lock, and wrapping
the rest of an 80 line block in another `with` to take a lock that is usually
off would make the common path harder to read for the uncommon one.

So this is the same lock as a statement. One function, so that the decision
about whether to take it lives in `graphlock` and not in two places.
"""

from __future__ import annotations

from echo_memory.infra.db import GRAPH_NAME
from echo_memory.infra.graphlock import (
    UNLIMITED,
    lock_key,
    max_graph_writers,
)


def enter(conn, graph: str = GRAPH_NAME) -> bool:
    """Take the graph wide write lock if one is configured. Returns whether it
    was taken, so a caller can log it.

    No transaction check here, unlike the context manager. This is only ever
    called from inside a transaction the caller already opened, and raising on
    a condition that cannot occur would be a check that only ever fires on a
    refactor - which is what the context manager is for.
    """
    limit = max_graph_writers()
    if limit == UNLIMITED or limit > 1:
        return False
    conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (lock_key("graph", graph),))
    return True
