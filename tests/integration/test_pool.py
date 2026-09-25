"""Every pooled connection needs the same per-connection setup as a
standalone one (AGE loaded, pgvector registered), applied via psycopg_pool's
configure hook rather than once per checkout. See conftest.py for the
migrated_db fixture."""

import pytest

from echo_memory.infra.pool import make_pool
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode


def test_pooled_connection_runs_write_episode(migrated_db):
    pool = make_pool(migrated_db, min_size=1, max_size=2)
    pool.wait(timeout=10)
    embedder = LocalEmbedder()
    try:
        with pool.connection() as conn:
            result = write_episode(
                conn, "g1", "s1", [{"name": "Postgres", "type": "tool"}], [], {}, embedder
            )
            # This test is about the connection, not the response shape, so it
            # asserts the parts that say the write reached the database and
            # leaves the rest to test_write_path_gaps.py. It used to compare
            # the whole dict and broke the moment a key was added.
            assert result["edges_created"] == []
            assert result["ambiguous_entities"] == []
            assert result["superseded"] == []
            assert "error" not in result

        with pool.connection() as conn:
            (count,) = conn.execute("SELECT count(*) FROM public.node_embedding").fetchone()
            assert count == 1
    finally:
        pool.close()


def test_pool_checks_out_multiple_connections_concurrently(migrated_db):
    pool = make_pool(migrated_db, min_size=1, max_size=3)
    pool.wait(timeout=10)
    try:
        with pool.connection() as c1, pool.connection() as c2:
            assert c1 is not c2
            (v1,) = c1.execute("SELECT 1").fetchone()
            (v2,) = c2.execute("SELECT 1").fetchone()
            assert (v1, v2) == (1, 1)
    finally:
        pool.close()


def test_startup_does_not_wait_for_a_database_that_is_not_up_yet():
    """Claude Desktop launches at login, before Docker Desktop. The pool used to
    open a connection eagerly inside the MCP handshake, so the handshake stalled
    on a database that was not running yet - its log recorded `Couldn't start
    for Cowork and Code sessions. Error: Request timed out` three times."""
    import time

    from echo_memory.infra.pool import make_pool

    start = time.monotonic()
    pool = make_pool("postgresql://postgres:postgres@localhost:9999/nowhere")
    elapsed = time.monotonic() - start
    pool.close()

    assert elapsed < 2, f"constructing the pool blocked for {elapsed:.1f}s"


def test_an_unreachable_database_answers_rather_than_hanging():
    """30 seconds of silence before "database unavailable" is its own failure:
    the agent has given up and the user cannot tell slow from broken."""
    import time

    import psycopg

    from echo_memory.infra.pool import make_pool

    pool = make_pool("postgresql://postgres:postgres@localhost:9999/nowhere")
    start = time.monotonic()
    with pytest.raises(psycopg.OperationalError), pool.connection():
        pass
    elapsed = time.monotonic() - start
    pool.close()

    assert elapsed < 10, f"waited {elapsed:.1f}s before reporting the failure"
