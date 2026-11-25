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

`echo-memory eval --ablate`, this author's store, 2026-11-23. All four shapes,
because a channel measured only on the shape it was built for is the mistake
that cost the graph hop three attempts.

Control shapes, limit 60:

    shape           shipping MRR   + path channel   dMRR      95% CI
    entity_pair         0.734          0.734      +0.0000  [+0.000,+0.000]
    entity_single       0.698          0.698      +0.0000  [+0.000,+0.000]
    prose               0.975          0.975      +0.0000  [+0.000,+0.000]

The multihop shape at full n=259:

    configuration        R@3     R@10    MRR     dMRR      95% CI
    shipping             0.398   0.745   0.242      -       baseline
    + graph hop          0.336   0.734   0.213   -0.0290  [-0.049,-0.008]
    + path channel       0.629   0.803   0.327   +0.0847  [+0.068,+0.102]
    + path at 0.5        0.548   0.768   0.296   +0.0544  [+0.041,+0.069]
    + path at 2.0        0.660   0.807   0.347   +0.1048  [+0.085,+0.125]
    + path depth 3       0.629   0.803   0.327   +0.0847  [+0.068,+0.102]
    + path + hop         0.645   0.857   0.340   +0.0976  [+0.077,+0.119]

R@3 rises 0.398 to 0.629, and R@10 moves too, +0.0579 [+0.027, +0.089], which
the limit 60 run could not resolve.

## Two conclusions the full run took back

Both were drawn from the limit 60 run and from the earlier, mis-routed
versions, and both are recorded here rather than quietly edited, because an
ablation table that only ever shows the current answer cannot be used to check
whether a change helped.

**The weight is not settled at 1.0.** The earlier reading was that 0.5 was
worse and 2.0 identical, so no weight was needed. At n=259, 0.5 is still worse
(+0.0544) but 2.0 is HIGHER than 1.0: +0.1048 [+0.085, +0.125] against
+0.0847 [+0.068, +0.102]. Those intervals overlap, and each is measured against
shipping rather than against the other, so this does not establish that 2.0
wins; it establishes that the previous claim was wrong. Separating them needs a
paired comparison between two non-shipping configurations, which the harness
does not currently do. 1.0 stays the default because it is the untuned choice
and nothing has yet shown 2.0 is better, which is a different sentence from the
one this file used to carry.

**The hop and this channel do compose, on this shape.** The earlier reading,
taken on the mis-routed version, was that they were worse together. With the
routing corrected, `+ path + hop` is +0.0976 MRR and has the best R@10 in the
table, +0.1120 [+0.069, +0.158]. That is not a recommendation to enable both:
the hop still costs -0.099 to -0.142 MRR on the three shapes where this channel
costs exactly nothing, so the pair is better only on multihop and worse
overall. The honest form of the claim is that the hop's problem was never this
channel, it is the hop's own behaviour on single fact questions.

**Those three zeroes are exact, not noise.** The channel returns nothing at all
on a query naming one entity, or two that already share an edge, so the answer
is byte identical to shipping. That is what every previous expansion could not
do: the hop costs -0.099 to -0.142 MRR on these same three shapes, because it
runs on every query and dilutes the ones a single fact answers.

**R@3 doubling is the adjacency claim coming true**, and it is the largest move
in the table. The chain's two facts arrive together or not at all, so the rank
at which a question becomes answerable stops being the rank of whichever of the
two the content channels happened to like less.

## Two versions this measurement rejected

Kept because each one looks right and the eval disagreed, which is the only
reason either was found.

**Declining the direct edge is not the same as declining the pair.** The first
version refused a one edge chain between the two named entities and then
carried on to depth two, returning every detour joining them through some other
middle. entity_pair queries name two entities that ARE joined, so this fired on
all of them: -0.0535 MRR [-0.102,-0.009], and prose -0.0569 [-0.111,-0.011].
A question one fact answers does not become a chain question because a longer
route also exists. `_any_adjacent` is the fix.

**Asking whether a node's name contains a query word is backwards.** The second
version matched that way, which is what `needs_expansion` does and is correct
for the bool it computes. For deciding WHICH two entities a question is about
it matched five or eight nodes per query, the cap declined them all, and the
multihop gain went to +0.0000 while the three control shapes stayed clean. The
fix is `named_nodes` asking the question the other way round: does this
entity's name appear in what was typed. See that function for both failure
directions.

The lesson is the cheaper one of the two: a control shape that scores exactly
0.0000 proves the channel declined, and a control shape is the only thing that
can tell "correctly inert" from "broken".
"""

from __future__ import annotations

import re

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

#: Entities a query may name and still be asking how two things relate.
#:
#: This is a gate, and it was added because the first version did not have one
#: and the eval caught it. Measured 2026-11-23, n=60 per shape, with the
#: channel firing whenever two named nodes were found anywhere in the query:
#:
#:     shape           dMRR      95% CI
#:     entity_pair    -0.0535  [-0.102,-0.009]
#:     prose          -0.0569  [-0.111,-0.011]
#:     multihop       +0.0495  [+0.013,+0.088]
#:
#: Two shapes clear of zero in the wrong direction is the same failure the
#: neighbourhood hop has three times, arriving by a new route. `prose` is the
#: one that names the cause: its queries are a fact's own words with the entity
#: names stripped, so any node whose name merely CONTAINS a leftover word gets
#: matched, and a question about nothing in particular acquires two endpoints
#: and a chain between them.
#:
#: Two, because the question this channel answers has exactly two ends. A query
#: that appears to name five entities has matched on common substrings, and the
#: ten pairs it produces are noise with a path between them.
MAX_NAMED_NODES = 2


def _live(alias: str, as_of: int | None) -> str:
    # Deferred: query_memory imports this module, so a top level import the
    # other way is a cycle. Same reasoning as authored.py.
    from echo_memory.retrieval.query_memory import live_clause

    return live_clause(alias, as_of)


#: An entity name shorter than this is not evidence that a query named it.
#:
#: Containment is a substring test, so a node called `Q` is inside almost every
#: question ever asked. The word boundary check below is the real guard and
#: this is the cheap one that keeps the scan small.
MIN_NAME = 3


def named_nodes(conn, group_id: str, query: str) -> list[str]:
    """Node ids whose own name appears in the query.

    **The direction matters and the first version had it backwards.** It asked
    whether a node's name CONTAINS one of the query's words, which is what
    `needs_expansion` does. For deciding "is there more than one entity here at
    all", a bool, that is fine. For deciding WHICH two entities a question is
    about it is not, and the eval priced both failure directions:

      - A multi word entity name tokenises into several words, and each of them
        is a substring of other node names, so a two entity question matched
        five or eight nodes and the cap below declined it. Measured: the whole
        multihop gain went to +0.0000.
      - A `prose` query is a fact's own words with the entity names removed,
        and a leftover word like "pool" is inside `the pool`, `pool size` and
        `connection pool`. A question about nothing in particular acquired two
        endpoints. Measured: -0.0569 MRR.

    Asking the question the other way round, does this entity's name appear in
    what was typed, is both cheaper to justify and exactly what "the query
    named it" means. A two entity question matches two nodes; a prose query
    matches none, because its entity names were stripped.

    Still exact lexical containment rather than embedding similarity, for the
    reason `needs_expansion` gives: a near match would make the router its own
    retrieval problem with its own threshold to calibrate, and this system
    already has one threshold whose interval has only just stopped including
    chance.

    Ordered by id so the pair below is stable across runs.
    """
    text = " ".join(query.lower().split())
    if not text:
        return []
    rows = conn.execute(
        f"""SELECT n.id::text, lower(n.properties ->> '"name"'::agtype) AS name
              FROM {GRAPH}."Node" n
             WHERE (n.properties ->> '"group_id"'::agtype) = %s
               AND length(n.properties ->> '"name"'::agtype) >= %s
               AND position(lower(n.properties ->> '"name"'::agtype) IN %s) > 0
             ORDER BY n.id
             LIMIT 16""",
        (group_id, MIN_NAME, text),
    ).fetchall()
    # Word boundaries in Python rather than a Postgres regex, so the name does
    # not have to be escaped into one. `pool` must not count as naming the node
    # `pool` when the query says `poolsize`, and a substring test alone says it
    # does.
    found: list[str] = []
    for node_id, name in rows:
        if re.search(rf"(?<!\w){re.escape(str(name))}(?!\w)", text):
            found.append(str(node_id))
    return found


def _any_adjacent(
    conn, group_id: str, nodes: list[str], as_of: int | None
) -> bool:
    """Whether a live edge already joins any two of these nodes.

    One indexed query, and the whole channel's routing decision. If the query's
    two entities already share a fact, that fact is the answer and the chains
    around it are noise: `needs_expansion` reached the same conclusion from the
    other direction, and the eval priced getting it wrong at -0.0535 MRR on
    entity_pair and -0.0569 on prose.
    """
    if len(nodes) < 2:
        return False
    live = _live("e", as_of)
    (joined,) = conn.execute(
        f"""SELECT count(*)
              FROM {GRAPH}."FACT" e
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND {live}
               AND e.start_id::text = ANY(%s)
               AND e.end_id::text = ANY(%s)
               AND e.start_id <> e.end_id""",
        (group_id, nodes, nodes),
    ).fetchone()
    return int(joined) > 0


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
    if not 2 <= len(nodes) <= MAX_NAMED_NODES:
        return []
    # Being UNCONNECTED is the signal, which is the finding `needs_expansion`
    # records and which this channel has to apply for itself because it runs
    # independently of the hop's router.
    #
    # Skipping only the one edge chain, which is what the first version did,
    # is not the same thing and measured -0.0535 MRR on entity_pair. Those
    # queries name two entities that ARE joined; the direct edge was correctly
    # declined and the search then carried on to depth two and returned every
    # detour through some other middle. A question one fact answers does not
    # become a chain question because a longer route also exists.
    if _any_adjacent(conn, group_id, nodes, as_of):
        return []

    pairs = [(nodes[0], nodes[1])]

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
