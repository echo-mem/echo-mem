"""The chain between two things the query named, as its own ranked list.

## The measurement this exists to answer

`multihop` is the worst shape this harness scores, and it has been for as long
as the harness has existed: MRR 0.212 against 0.672 to 0.970 for the shapes a
single fact answers. On LoCoMo the same shape reads recall@1 0.088 and MRR
0.360, the weakest row in that table too. Both numbers are published, and the
README says in as many words that they are what the rest of v1b has to beat.

The received reading is that graph expansion does not work here. That reading
is wrong in a specific and useful way. From the 2026-09-22 ablation:

    shape           shipping   + hop   dMRR      95% CI
    entity_pair        0.641   0.499   -0.142   [-0.182, -0.104]
    entity_single      0.690   0.591   -0.099   [-0.135, -0.062]
    prose              0.974   0.849   -0.125   [-0.158, -0.094]
    multihop           0.196   0.200   +0.003   [-0.022, +0.028]

and, on the same run, **multihop recall@10 rose 0.635 to 0.769**. The hop
finds the second fact. It ranks it badly. Those are different defects with
different fixes, and every attempt so far has fixed the wrong one: weighting
the expansion (0.5, 0.2) changes what a neighbour's top slot is worth, which
cannot help when the fact you want is at position 40 of a 50 long neighbour
dump.

## Why a path rather than a neighbourhood

`_graph_candidates` expands to every fact touching either endpoint of every
seed. For the shape that needs it that is the wrong object. A multihop case is
built from two entities X and Y with no edge between them, joined through some
middle M, and its gold answer is **both** edges: X-M and M-Y. Returning them
somewhere among a hundred facts about X and a hundred about Y is not retrieval,
it is recall with the ranking thrown away.

A path returns the two edges **adjacent to each other**, at positions 1 and 2
of this channel's list, because they are one object: the chain. Reciprocal rank
fusion then sees them one rank apart instead of forty, which is the entire
mechanism this module is proposing. Nothing else about the fusion changes.

That is also why this is not the designed Personalised PageRank. PPR ranks
nodes by proximity to the seeds and would have the same defect as the hop for
this shape: it scores the two edges of a chain independently, and nothing in it
says they belong together. PPR is the right tool for the general associative
case, "what else is near this", and that case is `entity_single`, where
expansion has already measured at -0.099. Build the one the evidence names.

## What it refuses to do

**Two seeds that already share an edge contribute nothing.** That fact is the
answer to a single hop question, the content channels have already found it,
and emitting it here would hand it a second top slot. Handing a fact a second
top slot in the fusion is the single mechanism behind three failed expansion
experiments in this project, and this module is not going to be the fourth.

**It never leaves the scope.** Every query filters on `group_id`, including the
frontier expansions, so the walk cannot cross into another tenant's graph and
then be filtered on the way out. "Cannot return it" is a weaker guarantee than
"never walks there", and the cost of the stronger one is one indexed predicate.

**It never returns a superseded fact.** Every expansion carries `live_clause`,
so `as_of` works here exactly as it does on the content channels. A chain as of
last Tuesday is the question a post mortem opens with, and a path channel that
ignored the bitemporal columns would answer it with today's graph.

## Cost

Bidirectional, meeting in the middle, which is what keeps this affordable on a
hub. One indexed query per frontier level per side, against
`fact_group_start_idx` and `fact_group_end_idx`, both `(group_id, node)` and
both present since migration 0013. At the default depth that is two queries for
the whole channel, whatever the number of pairs, because every pair's endpoints
expand in the same query.

Read off the edge tables rather than through Cypher, for the reason
`_graph_candidates` records at length: an unbound `MATCH (n)-[r:FACT]-(m)`
expands every FACT edge in the database before filtering, measured at 58,356ms
against 44ms.

## What it measured

`echo-memory eval --ablate --shape multihop`, this author's store, 2026-11-23,
n=259. Every other row of that table is flat or worse; this is the only
configuration that moves MRR with an interval clear of zero.

    configuration        R@3     R@10    MRR     dMRR      95% CI
    shipping             0.398   0.745   0.242      -       baseline
    + graph hop          0.336   0.734   0.213   -0.0290  [-0.049,-0.008]
    + path channel       0.494   0.764   0.280   +0.0378  [+0.024,+0.052]
    + path at 0.5        0.463   0.757   0.266   +0.0243  [+0.014,+0.036]
    + path at 2.0        0.490   0.768   0.279   +0.0373  [+0.021,+0.054]
    + path depth 3       0.490   0.768   0.280   +0.0376  [+0.024,+0.052]
    + path + hop         0.456   0.780   0.264   +0.0216  [-0.003,+0.046]

Four things that table decides, so none of them has to be argued:

**R@3 is where it lands, 0.398 to 0.494.** That is the adjacency claim coming
true and it is the largest move in the table. The chain's two facts arrive
together or not at all, so the rank at which a question becomes answerable
stops being the rank of whichever of the two the content channels happened to
like less.

**The weight is 1.0 and nothing had to be tuned to get it.** 0.5 is worse
(+0.0243) and 2.0 is the same within noise (+0.0373). fusion.py's rule is that
nothing but the eval decides a weight; the eval declined to need one, which is
the cheapest possible answer and the only one that does not leave a tuned
constant behind.

**Depth 3 buys nothing: +0.0376 against +0.0378.** Every chain this store has
is two edges long, so the third level is a query that finds what the second
already found. DEFAULT_MAX_DEPTH stays 2 on evidence rather than on caution.

**The hop and this channel do not compose.** `+ path + hop` is +0.0216 with an
interval including zero, worse than the path channel alone. The hop puts fifty
neighbours into the fusion and they outvote a two fact chain, which is the same
dilution the hop has been measured doing three times. Do not enable both.
"""

from __future__ import annotations

from echo_memory.infra.db import GRAPH_NAME as GRAPH

#: How far apart two named entities may be and still be called related.
#:
#: Two is the shape the harness scores and the shape the README describes: X
#: and Y joined through one middle M. Three is reachable with one more frontier
#: expansion per side and is left available rather than enabled, because the
#: number of paths grows with the branching factor and nothing has measured
#: whether a three edge chain is an answer or a coincidence. On a store where
#: 60% of entities appear in exactly one fact, most pairs at distance three are
#: joined through a hub that relates nothing.
DEFAULT_MAX_DEPTH = 2
MAX_DEPTH = 3

#: Nodes carried forward per frontier level, per side.
#:
#: A frontier is unbounded in principle: one hub with 400 facts makes level two
#: 400 nodes wide, and level three the whole scope. The cap makes the cost of
#: this channel a property of the code rather than of the customer's data,
#: which is the same reason LIST_DEPTH exists. Ordered by node id before
#: truncation, so which nodes survive is arbitrary and FIXED rather than
#: arbitrary and dependent on Postgres scan order: a rerun of the eval has to
#: score the same thing twice.
MAX_FRONTIER = 256

#: Distinct chains returned per pair of named entities.
#:
#: Two entities joined through eleven different middles are not eleven answers,
#: they are one hub and a question this channel cannot resolve. Returning all
#: of them would flood the list with exactly the dilution the neighbourhood hop
#: was measured failing at.
MAX_PATHS_PER_PAIR = 3

#: Pairs of named entities considered.
#:
#: `needs_expansion` stops at eight nodes, which is 28 pairs. Most queries name
#: two things; a query that appears to name eight has matched on a common
#: substring and its pairs are noise. Six is the pairs available from four
#: nodes, and four names in one question is already generous.
MAX_PAIRS = 6


def _live(alias: str, as_of: int | None) -> str:
    # Deferred: query_memory imports this module, so a top level import the
    # other way is a cycle. Same reasoning as authored.py.
    from echo_memory.retrieval.query_memory import live_clause

    return live_clause(alias, as_of)


def named_nodes(conn, group_id: str, query: str) -> list[str]:
    """Node ids whose name contains one of the query's terms.

    Exact lexical containment, not embedding similarity, for the reason
    `needs_expansion` gives: a near match would make the router its own
    retrieval problem with its own threshold to calibrate, and this system
    already has one threshold whose confidence interval includes chance.

    Ordered by id so the pairs below are stable across runs.
    """
    from echo_memory.retrieval.query_memory import prompt_terms

    words = sorted(set(prompt_terms(query)))
    if len(words) < 2:
        return []
    rows = conn.execute(
        f"""SELECT n.id::text
              FROM {GRAPH}."Node" n
             WHERE (n.properties ->> '"group_id"'::agtype) = %s
               AND EXISTS (
                   SELECT 1 FROM unnest(%s::text[]) AS w
                    WHERE position(w IN lower(n.properties ->> '"name"'::agtype)) > 0
               )
             ORDER BY n.id
             LIMIT 8""",
        (group_id, words),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _incident(
    conn, group_id: str, nodes: list[str], as_of: int | None
) -> dict[str, list[tuple[str, str]]]:
    """node -> [(edge_id, far_node)], for live in-scope edges touching `nodes`.

    One query for the whole frontier rather than one per node, which is what
    makes the depth bound cheap: the cost of this channel is two queries at the
    default depth however many pairs the query named.

    Self loops are dropped. An edge from a node to itself is not a step of a
    chain, and leaving it in makes a path that revisits its own start.
    """
    if not nodes:
        return {}
    live = _live("e", as_of)
    # Cast the frontier TOWARD graphid, never the column away from it.
    # `e.start_id::text = n.node` is the same answer and cannot use
    # fact_group_start_idx, which is what this whole channel's cost estimate
    # rests on. That is the defect `_graph_candidates` documents at 58,356ms
    # against 44ms, arriving by a different route.
    rows = conn.execute(
        f"""SELECT n.node, e.id::text,
                   CASE WHEN e.start_id = n.node::graphid
                        THEN e.end_id::text ELSE e.start_id::text END AS far
              FROM unnest(%s::text[]) AS n(node)
              JOIN {GRAPH}."FACT" e
                ON (e.start_id = n.node::graphid OR e.end_id = n.node::graphid)
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND {live}
             ORDER BY n.node, e.id""",
        (nodes, group_id),
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for node, edge_id, far in rows:
        if str(far) == str(node):
            continue
        out.setdefault(str(node), []).append((str(edge_id), str(far)))
    return out


def _expand(
    conn,
    group_id: str,
    reached: dict[str, list[str]],
    frontier: list[str],
    as_of: int | None,
) -> tuple[dict[str, list[str]], list[str]]:
    """One frontier level outward. Returns (reached, next frontier).

    `reached` maps a node to the edge ids of the shortest path found to it so
    far, from this side's origin. A node already reached is not reached again:
    the first time a BFS arrives somewhere is by a shortest route, and
    overwriting it with a longer one would report a chain nobody asked for.
    """
    incident = _incident(conn, group_id, frontier, as_of)
    nxt: list[str] = []
    for node in frontier:
        for edge_id, far in incident.get(node, ()):
            if far in reached:
                continue
            reached[far] = [*reached[node], edge_id]
            nxt.append(far)
    # Sorted before truncation so which nodes survive the cap is fixed rather
    # than left to the order rows happened to arrive in.
    nxt = sorted(set(nxt), key=int)[:MAX_FRONTIER]
    return reached, nxt


def _pair_paths(
    conn, group_id: str, a: str, b: str, max_depth: int, as_of: int | None
) -> list[list[str]]:
    """Edge id chains joining node `a` to node `b`, shortest first.

    Bidirectional: both sides expand one level at a time and the search stops
    at the first depth where they meet, because a longer chain between two
    entities that already have a shorter one is not a second answer.
    """
    fwd: dict[str, list[str]] = {a: []}
    bwd: dict[str, list[str]] = {b: []}
    f_frontier, b_frontier = [a], [b]
    depth = 0
    while depth < max_depth and (f_frontier or b_frontier):
        # Forward first, then check, then backward, then check. Expanding both
        # before looking would find chains one edge longer than the shortest.
        if depth < max_depth:
            fwd, f_frontier = _expand(conn, group_id, fwd, f_frontier, as_of)
            depth += 1
            if (found := _meet(fwd, bwd, a, b)) is not None:
                return found
        if depth < max_depth and b_frontier:
            bwd, b_frontier = _expand(conn, group_id, bwd, b_frontier, as_of)
            depth += 1
            if (found := _meet(fwd, bwd, a, b)) is not None:
                return found
    return []


def _meet(
    fwd: dict[str, list[str]], bwd: dict[str, list[str]], a: str, b: str
) -> list[list[str]] | None:
    """Chains through every node both sides have reached, or None if none have.

    None rather than an empty list, so the caller can tell "the two sides have
    not met yet, keep going" from "they met and the chain was empty", which
    cannot happen but would be indistinguishable if both returned [].
    """
    middles = sorted(set(fwd) & set(bwd), key=int)
    if not middles:
        return None
    paths: list[list[str]] = []
    for middle in middles:
        chain = [*fwd[middle], *reversed(bwd[middle])]
        # A single edge chain means these two entities are directly joined.
        # That fact is a single hop answer the content channels already hold,
        # and re-emitting it here would give it a second top slot in the
        # fusion. Three failed expansion experiments in this project share that
        # mechanism; this module declines to be the fourth.
        if len(chain) < 2:
            continue
        if len(set(chain)) != len(chain):
            continue
        paths.append(chain)
        if len(paths) >= MAX_PATHS_PER_PAIR:
            break
    return paths or None


def candidates(
    conn,
    group_id: str,
    query: str,
    limit: int,
    as_of: int | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[str]:
    """Edge ids along the chains joining the entities this query named.

    The ordering is the point, so it is stated rather than left to emerge.
    Chains come out whole and in order: a chain's first edge is immediately
    followed by its second, so reciprocal rank fusion sees the two facts of one
    answer one rank apart rather than forty. Shorter chains come before longer
    ones, pairs in the order their nodes were named, and middles by node id,
    which is arbitrary as relevance and FIXED, which is what a measurement
    needs.

    A fact reached twice keeps its first position. Emitting it again further
    down would say the second chain is a separate piece of evidence when it is
    the same fact arriving by another route.
    """
    if max_depth < 2 or max_depth > MAX_DEPTH:
        # Below two there is no chain to find, only the direct edge this
        # channel exists to decline. Above MAX_DEPTH nothing has measured
        # whether the result is an answer or a coincidence.
        raise ValueError(
            f"max_depth={max_depth} is outside 2..{MAX_DEPTH}: a depth nothing "
            f"has measured is a setting that is right while the behaviour it "
            f"names is unknown"
        )
    nodes = named_nodes(conn, group_id, query)
    if len(nodes) < 2:
        return []

    pairs = [
        (nodes[i], nodes[j])
        for i in range(len(nodes))
        for j in range(i + 1, len(nodes))
    ][:MAX_PAIRS]

    out: list[str] = []
    seen: set[str] = set()
    by_length: list[tuple[int, int, list[str]]] = []
    for order, (a, b) in enumerate(pairs):
        for chain in _pair_paths(conn, group_id, a, b, max_depth, as_of):
            by_length.append((len(chain), order, chain))
    for _, _, chain in sorted(by_length, key=lambda t: (t[0], t[1])):
        for edge_id in chain:
            if edge_id in seen:
                continue
            seen.add(edge_id)
            out.append(edge_id)
    return out[:limit]
