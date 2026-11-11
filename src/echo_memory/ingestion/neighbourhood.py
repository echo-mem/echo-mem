"""What this episode should probably have connected to.

The store this was built in has 246 entities in its shared scope and a median
degree of 1: 147 of them (60%) appear in exactly one fact. The shape is a
handful of project hubs carrying 14 to 35 edges and a long tail of leaves that
each turn up once and are never mentioned again. The solo scope is the same,
63% at degree <= 1.

That is a write-time outcome, not a retrieval one. Every episode arrives with
its own freshly invented names, gets two new nodes, joins them to each other,
and stops. Nothing in the write path ever tells the agent that the graph
already has a word for what it is describing, so nothing pulls successive
episodes onto shared vocabulary.

It matters because multi-hop retrieval - the next version's whole argument - is
a traversal, and a traversal across a median-degree-1 graph leaves a leaf,
reaches its project hub and stops. The premise of the next phase is not
supported by the data this phase produces, and no amount of work on the read
path fixes that.

So: after an episode is written, find the facts already in the scope that are
nearest to what was just written, and name the entities they connect.

**Similarity between fact texts, not between names.** Entity resolution scores
name against name and was calibrated on this store at AUC 0.666, 95% CI
[0.421, 0.881] - an interval containing chance. Fact-text retrieval on the same
store measures R@1 0.949 and MRR 0.970 on the prose query shape. One of those
two signals is worth building on. This rides the one that works.

**Advisory, and only advisory.** The episode is already committed when this
runs. It cannot block a write, change a resolution, or add an edge. Inventing a
connection nobody asserted would be worse than the sparsity it is trying to
fix: a memory graph is only worth reading if every edge in it was claimed by
somebody.
"""

from __future__ import annotations

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import agtype_str

# Enough for an agent to notice a word it should have used, few enough that the
# response does not become something to skim. Suggestions past the fifth have
# never been the reason anyone reused a name.
DEFAULT_LIMIT = 5

# How many neighbouring facts to look at before picking entities out of them.
# Larger than the entity limit because several facts usually name the same
# entity, and because the nearest fact's two endpoints are often the ones
# already excluded.
FACT_DEPTH = 20

# Facts per episode that get their own query. An episode of forty facts is a
# bulk import, not somebody recording what just happened, and running forty
# vector searches to advise it would put the cost on exactly the caller who
# least needs the advice.
MAX_FACTS_CONSULTED = 3


def _clean(value) -> str:
    """AGE returns strings as JSON encoded agtype.

    Delegates rather than stripping quotes, because this one decodes fact text
    as well as entity names and a fact is where free-form content lives. See
    infra.db.agtype_str: four copies of this had drifted, and the two that
    handled facts returned every escape sequence literally.
    """
    return agtype_str(value) or ""


def _endpoints(conn, edge_ids: list[str]) -> list[dict]:
    """The two entities each of these facts connects, in one query.

    Ordered by the caller's list rather than by the database, so the nearest
    fact's entities come first and rank the result.

    Read off the edge and node tables rather than through Cypher, for exactly
    the reason _adjacent gives twelve lines below. `MATCH (s)-[e:FACT]->(t)
    WHERE id(e) = eid` cannot use an index: AGE expands the match and filters
    afterwards, so every call walked every FACT edge in the database, every
    scope and in the hosted service every tenant, once per id.

    That made the cost of a write grow with the size of the whole store rather
    than the caller's own. Measured 2026-09-18 while ingesting LongMemEval:
    29ms per write at 1,057 nodes, 129ms at 24,054, with this function 55% of
    the time at the larger size and 141ms per call. Write throughput fell from
    28/s to 8/s over one run, which had been read as the benchmark being large
    rather than as a defect.

    id is the primary key of the edge table and start_id/end_id are indexed, so
    the same answer comes back as three index lookups.
    """
    if not edge_ids:
        return []
    rows = conn.execute(
        f"""SELECT e.id::text,
                   e.properties ->> '"fact"'::agtype,
                   s.id::text,
                   s.properties ->> '"name"'::agtype,
                   t.id::text,
                   t.properties ->> '"name"'::agtype
            FROM {GRAPH}."FACT" e
            JOIN {GRAPH}."Node" s ON s.id = e.start_id
            JOIN {GRAPH}."Node" t ON t.id = e.end_id
            WHERE e.id = ANY(SELECT unnest(%s::text[])::graphid)""",
        ([str(i) for i in edge_ids],),
    ).fetchall()

    by_edge = {
        str(edge_id): {
            "fact": _clean(fact),
            "ends": [(str(source_id), _clean(source)), (str(target_id), _clean(target))],
        }
        for edge_id, fact, source_id, source, target_id, target in rows
    }
    return [dict(by_edge[e], edge_id=e) for e in edge_ids if e in by_edge]


def _adjacent(conn, group_id: str, node_ids: list[str]) -> set[str]:
    """Node ids already one hop from any of these.

    Suggesting an entity the episode is already connected to is noise: the edge
    exists, so reusing the name changes nothing about the graph's shape.

    Read straight off the edge table rather than through Cypher. The pattern
    `MATCH (n)-[:FACT]-(m) WHERE id(n) = nid` cannot use an index: AGE expands
    the match first and filters after, so it walked every FACT edge in the
    database - every scope, not just this one - once per id. Four node ids took
    2.34 seconds, which was 98% of the time write_episode spent, for a result
    the write does not even depend on.

    start_id and end_id are indexed alongside group_id, so the same answer
    comes back in about a millisecond.
    """
    if not node_ids:
        return set()
    ids = [str(i) for i in node_ids]
    rows = conn.execute(
        f"""SELECT end_id::text FROM {GRAPH}."FACT"
            WHERE (properties ->> '"group_id"'::agtype) = %s
              AND start_id = ANY(SELECT unnest(%s::text[])::graphid)
            UNION
            SELECT start_id::text FROM {GRAPH}."FACT"
            WHERE (properties ->> '"group_id"'::agtype) = %s
              AND end_id = ANY(SELECT unnest(%s::text[])::graphid)""",
        (group_id, ids, group_id, ids),
    ).fetchall()
    return {str(r[0]) for r in rows}


def related_entities(
    conn,
    group_id: str,
    embeddings: list[list[float]],
    *,
    written_node_ids: list[str],
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Entities already in this scope that the episode arguably belongs near.

    Returns, for each, its node id, its name, and the existing fact that made
    it relevant - the fact matters more than the name, because "you already
    have an entity called X" is an assertion the agent has no reason to trust,
    while "this fact is about the same thing and it uses X" is evidence.

    Excludes the episode's own entities and anything already adjacent to them,
    so every suggestion is a connection that does not yet exist.

    Takes one embedding per fact rather than one per episode. An episode's
    facts share entities but not necessarily a subject - "X caused Y" and "X is
    owned by Z" are one call and two topics - so collapsing them into a single
    query would answer for whichever fact happened to be first.
    """
    from echo_memory.retrieval.query_memory import _vector_candidates

    edge_ids: list[str] = []
    for embedding in embeddings[:MAX_FACTS_CONSULTED]:
        for edge_id in _vector_candidates(conn, group_id, embedding, FACT_DEPTH):
            if edge_id not in edge_ids:
                edge_ids.append(edge_id)
    if not edge_ids:
        return []

    written = set(written_node_ids)
    skip = written | _adjacent(conn, group_id, written_node_ids)

    out: list[dict] = []
    seen: set[str] = set()
    for neighbour in _endpoints(conn, edge_ids):
        for node_id, name in neighbour["ends"]:
            if node_id in skip or node_id in seen:
                continue
            seen.add(node_id)
            out.append({
                "node_id": node_id,
                "name": name,
                "because": neighbour["fact"],
            })
            if len(out) >= limit:
                return out
    return out
