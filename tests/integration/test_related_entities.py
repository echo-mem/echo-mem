"""A write should say what the graph already calls this.

Measured on the author's store, 2026-09-13: 246 entities in the shared scope,
median degree 1, and 147 of them (60%) appearing in exactly one fact. Each
episode arrives with its own new names, makes two nodes, joins them to each
other and stops. Nothing ever tells the writing agent that the graph already
has a word for what it is describing.

That is the reason multi-hop retrieval cannot be built yet: a traversal across
a median-degree-1 graph leaves a leaf, reaches a project hub and stops. No work
on the read path fixes a graph with nothing to walk.

These pin the three properties that make the nudge worth returning at all: it
finds an entity a related fact already uses, it does not suggest connections
that already exist, and it never changes what was written.
"""

from __future__ import annotations

import pytest
from fake_embedder import REFERENCE, VectorEmbedder, unit_vector_at_angle

from echo_memory.infra.db import connect
from echo_memory.ingestion.write_episode import write_episode

GROUP = "user:ayush:shared"

# One subject, written about twice in different words. The second episode
# should be told about the first episode's entities.
OLD_FACT = "the retry policy on the payment gateway drops the idempotency key"
NEW_FACT = "retries on checkout double-charge when the key is regenerated"

# Near, but not identical: the two facts are about the same thing, which is the
# whole premise. 0.93 keeps them comfortably above any adaptive floor.
NEAR = unit_vector_at_angle(0.93)
FAR = unit_vector_at_angle(0.10)


def _embedder():
    return VectorEmbedder({
        OLD_FACT: REFERENCE,
        NEW_FACT: NEAR,
        "unrelated: the office coffee machine is broken": FAR,
        # Entity names are embedded too, on node creation.
        "retry policy": REFERENCE, "payment gateway": REFERENCE,
        "checkout session": NEAR, "idempotency key": NEAR,
        "coffee machine": FAR, "kitchen": FAR,
    })


def _write(conn, embedder, source, target, fact, session="s1"):
    return write_episode(
        conn, GROUP, session,
        [{"name": source, "type": "concept"}, {"name": target, "type": "concept"}],
        [{"source": source, "target": target, "relation_type": "affects",
          "fact": fact, "confidence": "extracted"}],
        {source: {"resolved_to": "new"}, target: {"resolved_to": "new"}},
        embedder, project="echo-mem", agent_id="claude-code",
    )


@pytest.fixture
def conn(migrated_db):
    with connect(migrated_db) as c:
        yield c


def test_a_write_names_the_entities_a_related_fact_already_uses(conn):
    """The whole point. An agent writing about retries in checkout is told the
    graph already calls this 'retry policy' and 'payment gateway', so the next
    episode on the subject can land on those nodes instead of minting a third
    pair of leaves."""
    embedder = _embedder()
    _write(conn, embedder, "retry policy", "payment gateway", OLD_FACT)

    second = _write(conn, embedder, "checkout session", "idempotency key", NEW_FACT, "s2")

    names = {r["name"] for r in second.get("related_entities", [])}
    assert names == {"retry policy", "payment gateway"}, second.get("related_entities")


def test_the_suggestion_carries_the_fact_that_justifies_it(conn):
    """"You already have an entity called X" is an assertion the agent has no
    reason to trust. "This fact is about the same thing and it uses X" is
    evidence, and evidence is what makes a suggestion actionable rather than
    one more line to skim past."""
    embedder = _embedder()
    _write(conn, embedder, "retry policy", "payment gateway", OLD_FACT)

    second = _write(conn, embedder, "checkout session", "idempotency key", NEW_FACT, "s2")

    assert all(r["because"] == OLD_FACT for r in second["related_entities"])
    assert all(r["node_id"] for r in second["related_entities"])


def test_entities_already_connected_are_not_suggested(conn):
    """A suggestion the episode has already acted on is noise. Writing a second
    fact between the same two entities must not come back advising their reuse:
    the edge exists, so reusing the name would change nothing about the shape
    of the graph, which is the only thing this is for."""
    embedder = _embedder()
    _write(conn, embedder, "retry policy", "payment gateway", OLD_FACT)

    again = _write(conn, embedder, "retry policy", "payment gateway", OLD_FACT, "s2")

    suggested = {r["name"] for r in again.get("related_entities", [])}
    assert "retry policy" not in suggested
    assert "payment gateway" not in suggested


def test_an_unrelated_write_is_not_given_something_to_reuse(conn):
    """Retrieval has an adaptive floor for a reason. A nudge that fires on
    every write teaches the agent to ignore the field, which costs more than
    never having shipped it."""
    embedder = _embedder()
    _write(conn, embedder, "retry policy", "payment gateway", OLD_FACT)

    far = _write(conn, embedder, "coffee machine", "kitchen",
                 "unrelated: the office coffee machine is broken", "s2")

    assert far.get("related_entities", []) == []


def test_the_advice_never_changes_what_was_written(conn):
    """It runs after the episode commits and cannot add an edge, move a fact or
    create a node. A graph is only worth reading if every edge in it was
    claimed by somebody, so an inferred connection would be worse than the
    sparsity it is meant to fix."""
    embedder = _embedder()
    _write(conn, embedder, "retry policy", "payment gateway", OLD_FACT)

    second = _write(conn, embedder, "checkout session", "idempotency key", NEW_FACT, "s2")

    assert len(second["edges_created"]) == 1
    assert second["related_entities"], "precondition: this case does produce advice"

    facts = conn.execute(
        """SELECT count(*) FROM public.audit_entry
           WHERE group_id = %s AND mutation_type = 'created'""",
        (GROUP,),
    ).fetchone()[0]
    assert facts == 2, "advice must not have written a third fact"


def test_adjacency_is_read_from_the_indexed_edge_table(conn):
    """Not through Cypher. `MATCH (n)-[:FACT]-(m) WHERE id(n) = nid` cannot use
    an index - AGE expands the match and filters afterwards - so it walked every
    FACT edge in the database, in every scope, once per id. Four node ids cost
    2.34 seconds, which was 98% of the time write_episode spent, for advice the
    write does not depend on. This pins the query plan, not only the answer."""
    import json

    from echo_memory.infra.db import GRAPH_NAME as GRAPH
    from echo_memory.ingestion.neighbourhood import _adjacent

    _write(conn, _embedder(), "retry policy", "payment gateway", OLD_FACT)
    node_ids = [
        str(r[0]) for r in conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                MATCH (n) WHERE n.group_id = $g RETURN id(n) $$, %s) AS (i agtype)""",
            (json.dumps({"g": GROUP}),),
        ).fetchall()
    ]
    assert node_ids, "the fixture wrote no nodes"

    assert _adjacent(conn, GROUP, node_ids), "these two nodes are joined by OLD_FACT"

    plan = "\n".join(
        r[0] for r in conn.execute(
            f"""EXPLAIN SELECT end_id::text FROM {GRAPH}."FACT"
                WHERE (properties ->> '"group_id"'::agtype) = %s
                  AND start_id = ANY(SELECT unnest(%s::text[])::graphid)""",
            (GROUP, node_ids),
        ).fetchall()
    )
    assert "Seq Scan" not in plan, f"adjacency stopped using an index:\n{plan}"


def test_adjacency_does_not_reach_into_another_scope(conn):
    """The Cypher form filtered on node id alone and matched across every scope
    in the database. Ids happen not to collide, so nothing leaked - but it is
    why the query had to read every edge to answer."""
    import json

    from echo_memory.infra.db import GRAPH_NAME as GRAPH
    from echo_memory.ingestion.neighbourhood import _adjacent

    _write(conn, _embedder(), "retry policy", "payment gateway", OLD_FACT)
    node_ids = [
        str(r[0]) for r in conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                MATCH (n) WHERE n.group_id = $g RETURN id(n) $$, %s) AS (i agtype)""",
            (json.dumps({"g": GROUP}),),
        ).fetchall()
    ]

    assert _adjacent(conn, "user:someone-else:shared", node_ids) == set()


# A fact and an entity name that are each, on their own, valid JSON. Both
# arrive from `_endpoints`, which projects per property with `->>`, and that
# operator returns real text rather than an encoded scalar - so decoding it
# as JSON strips the quotes the writer actually stored.
#
# This is the data fidelity bug the single agtype decoder was written to fix,
# arriving from the other side. The decoder belongs on the Cypher path, which
# hands back `"a\nb"` for a two-line fact; migration 0036 states the same
# distinction from the performance end. Applying it to a `->>` projection
# undoes the fidelity it exists to protect.
QUOTED_NAME = '"retry policy"'
QUOTED_FACT = '"the idempotency key is dropped"'


def test_a_quoted_fact_and_name_come_back_with_their_quotes(conn):
    embedder = VectorEmbedder({
        QUOTED_FACT: REFERENCE,
        NEW_FACT: NEAR,
        QUOTED_NAME: REFERENCE,
        "payment gateway": REFERENCE,
        "checkout session": NEAR,
        "idempotency key": NEAR,
    })
    _write(conn, embedder, QUOTED_NAME, "payment gateway", QUOTED_FACT)

    second = _write(conn, embedder, "checkout session", "idempotency key", NEW_FACT, "s2")

    suggested = {r["name"]: r["because"] for r in second["related_entities"]}
    assert QUOTED_NAME in suggested, suggested
    assert suggested[QUOTED_NAME] == QUOTED_FACT
