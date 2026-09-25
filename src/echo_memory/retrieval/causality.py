"""trace_cause: the read side of causal typing.

query_memory answers "what do I know that looks like this". It ranks facts by
similarity and returns a flat list, and a flat list cannot answer "why did
this happen" - the chain is the answer, and a chain is structure, not score.

This walks it. Causal edges carry a hint saying which end is the cause (see
write_episode.VALID_CAUSAL_HINTS), so the walk runs in the direction
causality actually runs rather than the direction a sentence happened to be
phrased in, and links written by three different sessions still assemble in
order.

Nothing here infers causality. Every link was asserted by a caller that read
it in what a session stated, and a fact with no hint is simply not part of a
chain. That restraint is the design doc's, stated as "don't attempt
statistical causal discovery, ever": a guessed cause is worse than no answer,
because it is indistinguishable from a real one.

The walk is one indexed query per hop rather than a recursive CTE. Bounded by
MAX_HOPS that is at most six round trips, and the version a person can read
is the version that stays correct."""

import time

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.ingestion.write_episode import CAUSE_IS_SOURCE
from echo_memory.retrieval.query_memory import _fetch_facts

_logger = get_logger("trace_cause")

# How far a chain may run. Three is not a claim about graphs in general, it is
# about what a caller can act on: a six-link chain assembled from six
# separately-asserted hints is a claim nobody made, presented as one the
# system stands behind.
DEFAULT_MAX_HOPS = 3
MAX_HOPS = 6

# How many entities the subject may anchor on, and how far behind the best
# match the others may be.
#
# The margin is what keeps a prose subject honest. Taking the top three
# entities outright means a store with three entities always returns three
# anchors, however little they have to do with the question, and every chain
# hanging off them reads as an answer. Keeping only what scored within a
# margin of the best match means one clear winner stays one anchor, and a
# genuine tie - two names for nearly the same thing - stays two.
DEFAULT_ANCHORS = 3
MAX_ANCHORS = 10
ANCHOR_MARGIN = 0.15

UPSTREAM = "upstream"
DOWNSTREAM = "downstream"
BOTH = "both"
DIRECTIONS = (UPSTREAM, DOWNSTREAM, BOTH)


class ValidationError(Exception):
    pass


def _validate(subject: str | None, direction: str, max_hops: int, anchors: int) -> None:
    if not subject or not subject.strip():
        raise ValidationError("subject is required: name the thing to trace from")
    if direction not in DIRECTIONS:
        raise ValidationError(
            f"invalid direction: {direction!r}, expected one of {list(DIRECTIONS)}"
        )
    if not 1 <= max_hops <= MAX_HOPS:
        raise ValidationError(f"max_hops must be between 1 and {MAX_HOPS}, got {max_hops}")
    if not 1 <= anchors <= MAX_ANCHORS:
        raise ValidationError(f"anchors must be between 1 and {MAX_ANCHORS}, got {anchors}")


def _anchor_nodes(conn, group_id: str, subject: str, embedder, anchors: int) -> list[dict]:
    """The entities the walk starts from.

    Anchoring on entities rather than on facts, which is the opposite of what
    query_memory does and deliberate. A causal chain runs between things, not
    between sentences: "why did we switch to Postgres" is a question about the
    switch, and the facts are what the chain is made of, not where it starts.
    Anchoring on facts also makes a fact that is itself a causal link both the
    question and part of the answer.
    """
    embedding = embedder.embed(subject)
    rows = conn.execute(
        """SELECT ne.node_id::text, -(ne.embedding <#> %s::vector) AS similarity
            FROM public.node_embedding ne
            WHERE ne.group_id = %s
            -- A stated tiebreak, so two entities at an identical distance
            -- always resolve the same way and the same store answers the same
            -- question the same way twice.
            ORDER BY ne.embedding <#> %s::vector, ne.node_id
            LIMIT %s""",
        (embedding, group_id, embedding, anchors),
    ).fetchall()
    if not rows:
        return []

    best = float(rows[0][1])
    kept = [(nid, float(sim)) for nid, sim in rows if float(sim) >= best - ANCHOR_MARGIN]
    names = _node_names(conn, group_id, [nid for nid, _ in kept])
    return [
        {"node_id": nid, "name": names.get(nid, "?"), "similarity": round(sim, 4)}
        for nid, sim in kept
    ]


def _node_names(conn, group_id: str, node_ids: list[str]) -> dict[str, str]:
    if not node_ids:
        return {}
    rows = conn.execute(
        f"""SELECT n.id::text, (n.properties ->> '"name"'::agtype)
            FROM {GRAPH}."Node" n
            WHERE n.id = ANY(SELECT unnest(%s::text[])::graphid)
              AND (n.properties ->> '"group_id"'::agtype) = %s""",
        (list(node_ids), group_id),
    ).fetchall()
    return {str(nid): name for nid, name in rows}


def _causal_step(conn, group_id: str, nodes: list[str], upstream: bool) -> list[tuple]:
    """One hop along typed edges, oriented so the caller always gets
    (edge, from_node, to_node, hint) in the direction being walked.

    The orientation is the whole point of the hint. An edge stored as
    (A)-[led_to]->(B) and one stored as (B)-[caused_by]->(A) say the same
    thing, and a walk that trusted the arrow would follow one and miss the
    other. The CASE below folds them together, so which way a caller phrased
    the sentence stops mattering the moment it is stored.

    contradicts is excluded here and reported separately: it is symmetric, so
    walking it would grow a chain in a direction causality does not run."""
    if not nodes:
        return []
    source_is_cause = list(CAUSE_IS_SOURCE)
    cause = """CASE WHEN (e.properties ->> '"causal_hint"'::agtype) = ANY(%s)
                    THEN e.start_id ELSE e.end_id END"""
    effect = """CASE WHEN (e.properties ->> '"causal_hint"'::agtype) = ANY(%s)
                     THEN e.end_id ELSE e.start_id END"""
    # Walking upstream means finding edges whose EFFECT is a node already
    # reached and taking their cause. Downstream is the mirror.
    known, wanted = (effect, cause) if upstream else (cause, effect)
    rows = conn.execute(
        f"""SELECT e.id::text,
                   ({known})::text AS from_node,
                   ({wanted})::text AS to_node,
                   (e.properties ->> '"causal_hint"'::agtype) AS hint
            FROM {GRAPH}."FACT" e
            WHERE (e.properties ->> '"group_id"'::agtype) = %s
              AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
              AND (e.properties ->> '"causal_hint"'::agtype) IS NOT NULL
              AND (e.properties ->> '"causal_hint"'::agtype) <> 'contradicts'
              AND ({known})::text = ANY(%s)
            ORDER BY e.id""",
        # Five placeholders, in the order they appear: the two CASE
        # expressions in SELECT, the group, the same CASE re-bound in WHERE,
        # and the frontier.
        (source_is_cause, source_is_cause, group_id, source_is_cause, list(nodes)),
    ).fetchall()
    return [(str(edge), str(a), str(b), hint) for edge, a, b, hint in rows]


def _contradictions(conn, group_id: str, nodes: list[str]) -> list[str]:
    if not nodes:
        return []
    rows = conn.execute(
        f"""SELECT e.id::text
            FROM {GRAPH}."FACT" e
            WHERE (e.properties ->> '"group_id"'::agtype) = %s
              AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
              AND (e.properties ->> '"causal_hint"'::agtype) = 'contradicts'
              AND (e.start_id::text = ANY(%s) OR e.end_id::text = ANY(%s))
            ORDER BY e.id""",
        (group_id, list(nodes), list(nodes)),
    ).fetchall()
    return [str(edge) for (edge,) in rows]


def _walk(conn, group_id: str, seed_nodes: list[str], upstream: bool, max_hops: int):
    """Breadth-first over edges, returning one chain per edge reached.

    Two separate sets, because they answer different questions. `expanded` is
    nodes already used as a starting point, and it is what stops a cycle: A
    causes B causes A is a thing a store can contain, and without it the walk
    runs to max_hops reporting the same pair over and over. `used` is edges
    already reported, so a diamond - two routes to the same fact - reports the
    fact once, by the shortest route, which is the one that came first.

    Each edge records the edge that led to it, so a chain is read back by
    following those links rather than re-queried."""
    came_from: dict[str, str | None] = {}   # edge -> the edge that reached its start
    reached_by: dict[str, str] = {}         # node -> the edge that first reached it
    used: set[str] = set()
    expanded: set[str] = set()
    frontier = set(seed_nodes)
    chains: list[list[tuple[str, str]]] = []

    for _ in range(max_hops):
        frontier -= expanded
        if not frontier:
            break
        found = _causal_step(conn, group_id, sorted(frontier), upstream)
        expanded |= frontier
        next_frontier: set[str] = set()
        for edge, from_node, to_node, hint in found:
            if edge in used:
                continue
            used.add(edge)
            came_from[edge] = reached_by.get(from_node)
            reached_by.setdefault(to_node, edge)
            next_frontier.add(to_node)

            chain: list[tuple[str, str]] = [(edge, hint)]
            cursor = came_from[edge]
            while cursor is not None:
                chain.append((cursor, ""))
                cursor = came_from.get(cursor)
            # Assembled from the far end inwards, so reverse it to read from
            # the anchor outwards - the order the question was asked in.
            chains.append(list(reversed(chain)))
        frontier = next_frontier
    return chains


def _maximal(chains: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    """Drop any chain that is the beginning of a longer one.

    The walk produces one chain per edge it reaches, so a single path of three
    links arrives as three chains: the first link, the first two, all three.
    Each is a true statement and only the longest is the answer - a caller
    asking why got told the same story three times, each time stopping
    earlier.

    Branches are not prefixes of each other, so two genuinely different routes
    both survive. That is the distinction worth keeping and the reason this is
    prefix elimination rather than "keep the longest"."""
    keys = {tuple(edge for edge, _ in chain) for chain in chains}
    return [
        chain for chain in chains
        if not any(
            other != (key := tuple(edge for edge, _ in chain))
            and other[: len(key)] == key
            for other in keys
        )
    ]


def _render(chains, facts: dict[str, dict]) -> list[dict]:
    """A chain as a list of facts, anchor end first. The hint rides on each
    fact already - _fetch_facts returns causal_hint - so the walk does not
    restate it and the two cannot disagree."""
    rendered = [
        [facts[edge] for edge, _ in chain if edge in facts]
        for chain in _maximal(chains)
    ]
    rendered = [chain for chain in rendered if chain]
    # Shortest first. A one-link chain is a direct cause and is usually the
    # answer; a three-link one is the context for it. Ties break on fact id,
    # arbitrary as an ordering and fixed, so the answer is reproducible.
    rendered.sort(key=lambda c: (len(c), [link["fact_id"] for link in c]))
    return rendered


def trace_cause(
    conn,
    group_id: str,
    subject: str,
    embedder,
    direction: str = BOTH,
    max_hops: int = DEFAULT_MAX_HOPS,
    anchors: int = DEFAULT_ANCHORS,
) -> dict:
    """Causal chains through the entities that match `subject`.

    Returns the entities it anchored on, the chains running into them
    (`causes`), the chains running out of them (`effects`), and any facts
    explicitly recorded as contradicting one of them.

    An empty result is a real answer and the honest one when nothing in this
    scope was ever written with a causal_hint. It means nobody asserted a
    cause, not that there isn't one."""
    start = time.perf_counter()
    try:
        _validate(subject, direction, max_hops, anchors)
    except ValidationError as e:
        return {"error": str(e)}

    matched = _anchor_nodes(conn, group_id, subject, embedder, anchors)
    if not matched:
        return {
            "anchors": [], "causes": [], "effects": [], "contradictions": [],
            "note": f"nothing in this scope matches {subject!r}",
        }

    seed_nodes = [a["node_id"] for a in matched]
    causes = _walk(conn, group_id, seed_nodes, True, max_hops) \
        if direction in (UPSTREAM, BOTH) else []
    effects = _walk(conn, group_id, seed_nodes, False, max_hops) \
        if direction in (DOWNSTREAM, BOTH) else []
    contradicting = _contradictions(conn, group_id, seed_nodes)

    # One fetch for everything the answer mentions. A chain is edges and a
    # contradiction is an edge, so they all resolve through the same batch
    # rather than a query per link.
    wanted = set(contradicting)
    for chain in causes + effects:
        wanted.update(edge for edge, _ in chain)
    facts = _fetch_facts(conn, sorted(wanted))

    result = {
        "anchors": matched,
        "causes": _render(causes, facts),
        "effects": _render(effects, facts),
        "contradictions": [facts[e] for e in contradicting if e in facts],
    }
    if not result["causes"] and not result["effects"]:
        # Said out loud, because the failure this guards against is a caller
        # reading an empty list as "no causes exist" when the real answer is
        # "nothing here was ever written with one".
        result["note"] = (
            "no causal_hint has been recorded on facts around this subject. "
            "Pass causal_hint on a fact in write_episode to make a link "
            "traceable; without one a fact is associative, which is the "
            "default and usually correct."
        )
    _logger.info(
        "trace_cause",
        extra={
            "group_id": group_id,
            "anchors": len(result["anchors"]),
            "causes": len(result["causes"]),
            "effects": len(result["effects"]),
            "duration_ms": (time.perf_counter() - start) * 1000,
        },
    )
    return result
