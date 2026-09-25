"""The vector search has to be answerable by the vector index.

pgvector's HNSW index can serve exactly one shape: `ORDER BY <distance>
LIMIT n`. Anything else in the ORDER BY makes the clause unindexable and the
planner reads every embedding in the scope and sorts them.

That is not hypothetical and it is not cheap. A `, fe.edge_id` tiebreak was
added to this query for determinism, and it silently turned approximate
nearest neighbour into a full scan. Measured against a real 38,169 fact
scope: 52.47ms scanning against 3.85ms using the index, with the scan growing
linearly - ten times the facts is half a second, on every query.

No existing test could see it. The results were correct, the suite was green,
and the only symptom was a number nobody was looking at. So these tests do
not check results; they check that the plan uses the index.

enable_seqscan is turned off for the check, which is the point rather than a
cheat: on a small test table a sequential scan is genuinely the cheaper plan
and the planner is right to pick it. Forcing the choice asks the question
that actually matters - CAN this query use the index - and a second sort key
makes the answer no at any table size.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.retrieval.query_memory import VECTOR_CANDIDATE_SQL

GROUP = "g1"

VECTORS = {
    "Postgres": REFERENCE,
    "the pool rewrite": unit_vector_at_angle(0.4),
    "a fact about the two": unit_vector_at_angle(0.5),
}


def _seed(conn):
    embedder = VectorEmbedder(dict(VECTORS))
    write_episode(
        conn, GROUP, "s1",
        [{"name": "Postgres", "type": "tool"},
         {"name": "the pool rewrite", "type": "work"}],
        [{"source": "Postgres", "target": "the pool rewrite",
          "relation_type": "caused", "confidence": "extracted",
          "fact": "a fact about the two"}],
        {}, embedder, assume_new=True,
    )
    return embedder


def _plan(conn, sql, params) -> str:
    """The plan, with the alternatives to an index scan made expensive.

    Two settings, and both are needed. enable_seqscan stops the planner
    reading the whole table, and enable_sort stops it satisfying the ORDER BY
    by sorting whatever some other index handed it - which is what it did
    here, via the group_id btree, on a table holding two rows.

    Neither is a trick to make the test pass. On a table this small a sort
    really is cheaper and the planner is right to choose it; what is being
    asked is whether an index-ordered plan EXISTS. A second sort key means no
    such plan exists at any size, which is the regression being caught.
    """
    for setting in ("SET enable_seqscan = off", "SET enable_sort = off"):
        conn.execute(setting)
    try:
        rows = conn.execute(f"EXPLAIN {sql}", params).fetchall()
    finally:
        for setting in ("SET enable_seqscan = on", "SET enable_sort = on"):
            conn.execute(setting)
    return "\n".join(r[0] for r in rows)


def test_fact_search_can_use_the_hnsw_index(migrated_db):
    conn = connect(migrated_db)
    embedder = _seed(conn)

    plan = _plan(
        conn, VECTOR_CANDIDATE_SQL,
        (embedder.embed("a fact about the two"), GROUP,
         embedder.embed("a fact about the two"), 50),
    )

    assert "fact_embedding_hnsw_idx" in plan, plan
    assert "Sort Key" not in plan, (
        "the planner had to sort, so the ORDER BY is not answerable by the "
        f"index:\n{plan}"
    )


def test_entity_search_can_use_the_hnsw_index(migrated_db):
    """trace_cause anchors on entities through node_embedding, and shipped
    with the same second sort key in it."""
    conn = connect(migrated_db)
    embedder = _seed(conn)
    probe = embedder.embed("Postgres")

    plan = _plan(
        conn,
        """SELECT ne.node_id::text, -(ne.embedding <#> %s::vector) AS similarity
             FROM public.node_embedding ne
            WHERE ne.group_id = %s
            ORDER BY ne.embedding <#> %s::vector
            LIMIT %s""",
        (probe, GROUP, probe, 3),
    )

    assert "node_embedding_hnsw_idx" in plan, plan
    assert "Sort Key" not in plan, plan


@pytest.mark.parametrize("tiebreak", [", fe.edge_id", ", fe.edge_id DESC"])
def test_a_second_sort_key_is_what_breaks_it(migrated_db, tiebreak):
    """The other half of the claim. Without this, the tests above could pass
    for some unrelated reason and nobody would know the rule they encode is
    real.

    Both cases order by a column that actually varies. `, fe.group_id` was
    tried here and does NOT break the index, because group_id is pinned to one
    value by the WHERE clause and the planner drops a sort key it knows is
    constant. A correct rule, stated too broadly, would have been worse than
    no test: it is the varying key that costs the index.
    """
    conn = connect(migrated_db)
    embedder = _seed(conn)
    probe = embedder.embed("a fact about the two")
    broken = VECTOR_CANDIDATE_SQL.replace(
        "ORDER BY fe.embedding <#> %s::vector",
        f"ORDER BY fe.embedding <#> %s::vector{tiebreak}",
    )

    plan = _plan(conn, broken, (probe, GROUP, probe, 50))

    assert "fact_embedding_hnsw_idx" not in plan, (
        f"a {tiebreak.strip(', ')} tiebreak was expected to cost the index, "
        f"but the plan used it anyway:\n{plan}"
    )


def test_the_shipped_query_orders_by_distance_and_nothing_else(migrated_db):
    """Cheap, and it fails with a readable message the moment somebody adds a
    sort key back, rather than after they have read a query plan."""
    order_by = VECTOR_CANDIDATE_SQL.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]

    assert order_by.strip() == "fe.embedding <#> %s::vector", (
        f"ORDER BY has grown a second key, which costs the index: {order_by!r}"
    )
