"""Expansion that follows links somebody asserted, instead of shared endpoints.

The graph hop in `query_memory` walks structure: from a fact that matched, to
every other fact touching either of its entities. That is not a relation
anybody stated. It is a consequence of two sentences happening to name the same
thing, and it has now failed three measured times - most clearly in the
2026-09-22 ablation, where turning it on cost 0.099 to 0.142 MRR on three of
four shapes, every interval clear of zero. The reason is visible in what it
returns: a popular entity has dozens of facts, all of them "neighbours", none
of them about the question.

The contrast worth copying is SoMem (arXiv 2611.11929), whose graph view does
work. Its links are written by the agent at construction time, one claim
pointing at another, and the walk follows those. Echo already has that
mechanism and has had it since 0.5.0: `causal_hint` is a link a caller asserted
after reading one in what a session said, and `trace_cause` walks it. What it
has never had is a retrieval channel.

This is that channel. The rule is one sentence: expansion may only traverse an
edge a caller typed as a relation between claims, never an edge that merely
shares an endpoint with one.

## What it returns, in two tiers

1. **The link itself.** An authored link touching a seed's entity is usually
   the fact that answers "why", and it is the one structural expansion buries
   among everything else about that entity.
2. **What the link points at.** The facts on the far endpoint of that link. A
   two-fact question is answered by the far fact, and reaching it is the whole
   argument for expanding at all: structural expansion needs two hops to get
   there and drags in everything either entity touches on the way.

Tier 1 before tier 2, and within a tier by seed rank then edge id. Edge id is
arbitrary as a relevance signal, which is exactly why it is used: it is
arbitrary and FIXED, so a rerun of the eval scores the same thing twice. The
same reasoning as `_graph_candidates`, and the same reason it is not a
relevance tiebreak: that would be a different change and it would have to be
measured separately.

## What it cannot do yet, measured

Authored links are rare. In the store this was written against: 6 hints across
2,001 facts, 0.3%, and 3 of those 6 in the scope with 438 facts. So on real
data today this channel returns almost nothing, and that is a finding about the
write path rather than about retrieval. A hint is only written by a caller that
read one in what a session said, and almost nothing asks them to. The two
things that change it are the write-time prompt and
`echo-memory infer-causal-hints`, which reads each fact's own sentence and
needs a model.

Which is why this ships off by default, exactly like the structural hop, and
why the number that would justify turning it on is not in this file. It is
the mechanism, built and tested; the measurement needs a corpus that has the
links in it.
"""

from __future__ import annotations

from echo_memory.infra.db import GRAPH_NAME as GRAPH

#: The property that marks an edge as a link a caller asserted between claims,
#: rather than a fact that happens to sit next to another one.
#:
#: One property today. `contradicts` is deliberately included: a fact that
#: contradicts what matched is among the most useful things an answer can
#: carry, and `trace_cause` excludes it only because a contradiction is not a
#: step in a causal chain. Retrieval is not walking a chain.
AUTHORED = """(e.properties ->> '"causal_hint"'::agtype) IS NOT NULL"""


def candidates(
    conn,
    group_id: str,
    seed_edge_ids: list[str],
    limit: int,
    as_of: int | None = None,
) -> list[str]:
    """Edge ids reachable from the seeds along an authored link, best first.

    Read off the edge table rather than through Cypher, for the reason
    `_graph_candidates` records: an unbound `MATCH (n)-[n2:FACT]-(m)` expands
    every FACT edge in the database against every seed before filtering, which
    was measured at 58 seconds against 44 milliseconds. The indexes this uses,
    `fact_group_start_idx` and `fact_group_end_idx`, have existed since
    migration 0013.
    """
    if not seed_edge_ids:
        return []

    live_link = _live("link", as_of)
    live_far = _live("far", as_of)
    rows = conn.execute(
        f"""WITH seeds AS (
                SELECT e.id AS seed, e.start_id, e.end_id
                FROM {GRAPH}."FACT" e
                WHERE e.id = ANY(SELECT unnest(%s::text[])::graphid)
                  AND (e.properties ->> '"group_id"'::agtype) = %s
            ),
            ends AS (
                SELECT seed, start_id AS node FROM seeds
                UNION ALL
                SELECT seed, end_id AS node FROM seeds
            ),
            links AS (
                SELECT DISTINCT ends.seed, link.id AS link_id,
                       CASE WHEN link.start_id = ends.node
                            THEN link.end_id ELSE link.start_id END AS far_node
                FROM ends
                JOIN {GRAPH}."FACT" link
                  ON (link.start_id = ends.node OR link.end_id = ends.node)
                WHERE (link.properties ->> '"group_id"'::agtype) = %s
                  AND {live_link}
                  AND {AUTHORED.replace("e.properties", "link.properties")}
            )
            SELECT seed::text, 1 AS tier, link_id::text FROM links
            UNION ALL
            SELECT links.seed::text, 2 AS tier, far.id::text
              FROM links
              JOIN {GRAPH}."FACT" far
                ON (far.start_id = links.far_node OR far.end_id = links.far_node)
             WHERE (far.properties ->> '"group_id"'::agtype) = %s
               AND {live_far}""",
        ([str(i) for i in seed_edge_ids], group_id, group_id, group_id),
    ).fetchall()

    order = {edge_id: i for i, edge_id in enumerate(seed_edge_ids)}
    # Best tier and best seed rank win, so a fact reached both as a link and as
    # a far fact is ranked by the stronger of the two reasons rather than by
    # whichever row Postgres produced first.
    found: dict[str, tuple[int, int]] = {}
    for seed, tier, edge_id in rows:
        edge = str(edge_id)
        if edge in order:
            continue
        rank = (int(tier), order.get(str(seed), len(order)))
        if edge not in found or rank < found[edge]:
            found[edge] = rank
    return [
        e for e, _ in sorted(found.items(), key=lambda kv: (kv[1], int(kv[0])))
    ][:limit]


def _live(alias: str, as_of: int | None) -> str:
    # Imported here rather than at module scope: query_memory imports this
    # module's caller, and a top-level import the other way is a cycle.
    from echo_memory.retrieval.query_memory import live_clause

    return live_clause(alias, as_of)
