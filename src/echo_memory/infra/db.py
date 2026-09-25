"""Connection helper: every AGE-aware connection needs the extension loaded
and ag_catalog on the search_path before any Cypher call (see PR0a's spike
notes in the design doc's Foundational spike section)."""

import os

import psycopg
from pgvector.psycopg import register_vector

from echo_memory.infra.logging import get_logger

GRAPH_NAME = "echo_memory"

_logger = get_logger("db")

# How hard HNSW looks before answering. Set per connection rather than per
# query because every connection from the pool wants the same behaviour, and
# because autocommit is on: SET LOCAL would expire at the end of the single
# statement it was issued in and reach nothing.
#
# 200 is measured, not guessed. Against a real 38,169 fact scope, 20 probe
# vectors, recall of the exact top 50 and the cost of getting it:
#
#     ef_search   mean recall   worst   top 10 identical   mean ms
#     40 (dflt)         0.817    0.24          10 of 20       1.85
#     100               0.944    0.74          13 of 20       1.72
#     200               0.980    0.86          17 of 20       3.85
#     400               0.985    0.90          19 of 20       8.26
#     exact             1.000    1.00          20 of 20      52.47
#
# The default of 40 is below LIST_DEPTH, which is the specific reason it is
# bad: asking an index for 50 neighbours while it keeps a candidate list of
# 40 cannot go well, and 0.24 recall on the worst probe is what that looks
# like. 200 buys 98% recall for 7% of the exact scan's time, and the exact
# scan is O(n) - at ten times this scope it is half a second, per query.
HNSW_EF_SEARCH = int(os.environ.get("ECHO_MEMORY_HNSW_EF_SEARCH", "200"))

_warned_no_iterative_scan = False


def configure_connection(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("LOAD 'age'")
        cur.execute('SET search_path = ag_catalog, "$user", public')
        _configure_vector_search(cur)
    register_vector(conn)


def _configure_vector_search(cur) -> None:
    """HNSW settings, on a database that may be too old to have them.

    iterative_scan is pgvector 0.8. Without it an index scan under a filter
    stops at the first candidate batch and returns FEWER rows than the LIMIT
    asked for - measured at 25 of 50 on the scope above, which is a silent
    recall cut rather than an error. With it the scan continues until the
    limit is met.

    A 0.7 database has no such setting and errors on SET. That is not a
    reason to refuse to start: the queries still work there, just exactly and
    slowly, which is what every version of this code did until now. Warned
    once, not per connection, because a pool opening ten of them would
    otherwise say it ten times.
    """
    global _warned_no_iterative_scan
    try:
        cur.execute("SET hnsw.iterative_scan = strict_order")
        # An int, interpolated: SET takes no parameters, and the value is
        # coerced rather than passed through so the environment cannot put
        # anything but a number into the statement.
        cur.execute(f"SET hnsw.ef_search = {int(HNSW_EF_SEARCH)}")
    except psycopg.errors.UndefinedObject:
        if not _warned_no_iterative_scan:
            _warned_no_iterative_scan = True
            _logger.warning(
                "hnsw_iterative_scan_unavailable",
                extra={"detail": "pgvector < 0.8; vector search stays exact and O(n)"},
            )


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    configure_connection(conn)
    return conn
