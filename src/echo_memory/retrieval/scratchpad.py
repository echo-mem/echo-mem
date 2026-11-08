"""What this scope currently believes, without asking a model.

BEAM's authors measured something worth acting on. Their LIGHT framework
combines three memories - episodic retrieval, a working buffer of recent
turns, and a **scratchpad** of salient facts - and their ablation at 100K
tokens says that removing retrieval entirely "does not change performance
and it remains steady, since the scratchpad alone suffices". Retrieval only
begins to matter at 10M.

The scratchpad is also their most expensive component. LIGHT runs
Qwen2.5-32B over **every dialogue pair** to extract key-value facts, merges
them iteratively, and calls GPT-4.1-nano to compress the result from 30K
tokens to 15K whenever it overflows. Per conversation, that is thousands of
model calls to reconstruct a structure from unstructured text.

This store already has that structure. A temporal property graph **is** a
key-value record with validity: nodes are the keys, edges are the values,
and `t_invalid IS NULL` is the definition of "currently believed". So the
scratchpad can be derived rather than generated - no extraction model, no
compression model, no drift between the summary and what is stored, and
nothing to re-run when a fact is superseded.

It is compact for the same reason. A generated summary of a conversation
that changed its mind twice carries all three versions unless a model
notices; this carries the surviving one because the other two have a
`t_invalid`.

## What "salient" means here, and what it does not

Ranked by the summed degree of a fact's two endpoints: a fact about the two
things this scope talks about most, first. Degree is a crude centrality
measure and is chosen because it is **free and fixed** - it needs no model,
no training and no recall log, and it gives the same answer on every run, so
a benchmark scores the same thing twice.

It is explicitly not a claim about importance. A fact can matter enormously
and touch two entities mentioned once. `salience.py` exists for the other
idea - how often a fact has actually been recalled - and is off because the
eval said it loses. This is a different quantity: not what gets used, but
what the conversation is *about*.
"""

from __future__ import annotations

from echo_memory.infra.db import GRAPH_NAME as GRAPH

#: How many facts a scratchpad carries by default. LIGHT compresses its own
#: to a 15K token budget; at this store's fact lengths that is some hundreds
#: of facts, and a conversation's worth of current belief is usually far
#: fewer. Small on purpose: the whole argument for a scratchpad is that it is
#: cheaper than the context it replaces.
DEFAULT_FACTS = 40

_GROUP = """(properties ->> '"group_id"'::agtype)"""


def scratchpad(
    conn,
    group_id: str,
    *,
    limit: int = DEFAULT_FACTS,
    about: list[str] | None = None,
    as_of: int | None = None,
) -> list[dict]:
    """The scope's currently believed facts, most central first.

    `about` restricts to facts touching named entities, which is the query
    scoped form: when a question names what it is about, the scratchpad for
    that question is the current belief about those things.

    `as_of` reads the scratchpad as it stood at an instant, because a
    scratchpad that cannot time travel would contradict every other read
    path in this store.

    Returns dicts shaped like `query_memory`'s facts, so a caller can put the
    two together without knowing which produced which.
    """
    live = (
        "(e.properties ->> '\"t_invalid\"'::agtype) IS NULL"
        if as_of is None
        else (
            "(e.properties ->> '\"t_valid\"'::agtype)::bigint <= %(as_of)s "
            "AND ((e.properties ->> '\"t_invalid\"'::agtype) IS NULL "
            "     OR (e.properties ->> '\"t_invalid\"'::agtype)::bigint > %(as_of)s)"
        )
    )
    named = ""
    params: dict = {"gid": group_id, "limit": limit, "as_of": as_of}
    if about is not None:
        if not about:
            # An empty list is a filter that matches nothing, not an absent
            # filter. The opposite reading returns the whole scope to a caller
            # who asked for two named entities and got none.
            return []
        named = """
            AND (a.properties ->> '"name"'::agtype = ANY(%(about)s)
                 OR b.properties ->> '"name"'::agtype = ANY(%(about)s))"""
        params["about"] = list(about)

    rows = conn.execute(
        f"""
        WITH live_facts AS (
            SELECT e.id, e.start_id, e.end_id,
                   (e.properties ->> '"fact"'::agtype) AS fact,
                   (e.properties ->> '"t_valid"'::agtype)::bigint AS t_valid,
                   (e.properties ->> '"agent_id"'::agtype) AS agent_id
              FROM {GRAPH}."FACT" e
             WHERE {_GROUP.replace("properties", "e.properties")} = %(gid)s
               AND {live}
        ),
        degree AS (
            SELECT node, count(*) AS n FROM (
                SELECT start_id AS node FROM live_facts
                UNION ALL
                SELECT end_id AS node FROM live_facts
            ) touched GROUP BY node
        )
        SELECT f.id::text, f.fact, f.t_valid, f.agent_id,
               (a.properties ->> '"name"'::agtype) AS source,
               (b.properties ->> '"name"'::agtype) AS target,
               (coalesce(da.n, 0) + coalesce(db.n, 0)) AS centrality
          FROM live_facts f
          JOIN {GRAPH}."Node" a ON a.id = f.start_id
          JOIN {GRAPH}."Node" b ON b.id = f.end_id
          LEFT JOIN degree da ON da.node = f.start_id
          LEFT JOIN degree db ON db.node = f.end_id
         WHERE TRUE {named}
         -- Centrality first, then the newest, then edge id. The last one is
         -- arbitrary as an ordering and that is the point: it is arbitrary
         -- and FIXED, so a rerun produces the same scratchpad and a
         -- measurement over it means something.
         ORDER BY centrality DESC, f.t_valid DESC, f.id
         LIMIT %(limit)s
        """,
        params,
    ).fetchall()

    return [
        {
            "fact_id": str(r[0]),
            "fact": r[1],
            "t_valid": r[2],
            "source": r[4],
            "target": r[5],
            "centrality": r[6],
            "provenance": {"agent_id": r[3]},
            "matched": ["scratchpad"],
        }
        for r in rows
    ]


def render(facts: list[dict]) -> str:
    """The scratchpad as text, for a model to read.

    One fact per line with its subject, because a model reading "uses
    Razorpay" without knowing what does is being asked to guess. No ranking
    numbers: centrality is how this list was chosen and says nothing a reader
    of it should weigh.
    """
    return "\n".join(
        f"- {f['source']} {f['fact']}" if f["fact"] and not f["fact"].startswith(f["source"])
        else f"- {f['fact']}"
        for f in facts
    )
