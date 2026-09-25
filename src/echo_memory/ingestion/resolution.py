"""Entity resolution (see the design doc's Concrete Schema "Entity resolution"
section). Runs three ways: exact/near-exact match, embedding-similarity match
above a high threshold, or genuinely ambiguous (deferred to the calling agent
via write_episode's ambiguous_entities/entity_resolutions round-trip). The
server never calls an LLM here; see the MCP tool contract's architecture
pivot note.

KNOWN v1a LIMITATION: only checks each entity against nodes already in the
database, not against other entities in the same write_episode call. Two
new, near-duplicate names in one call (e.g. "Postgres" and "PostgreSQL",
neither existing yet) both resolve as new and create two nodes; there's no
in-batch embedding available to compare against until write_episode's later
node-creation step. A later call mentioning either name will still resolve
correctly against whichever node was created first. Documented, not silently
dropped; see MATHS.local.md's open questions."""

import json
import re
from dataclasses import dataclass, field

from echo_memory.infra.db import GRAPH_NAME as GRAPH

# Measured against the real embedder (all-MiniLM-L6-v2), not guessed: a true
# duplicate ("AGE" vs "Apache AGE") scored 0.497, well below the 0.75 this
# started at, and several true non-duplicates ("PR-B3"/"PR-B2" 0.875,
# "t_valid"/"t_invalid" 0.867) scored well above it. Short technical
# identifiers don't separate cleanly on cosine similarity alone; see
# MATHS.local.md §5. LOW_THRESHOLD lowered so real near-misses like the AGE
# case get surfaced as ambiguous instead of silently missed; HIGH_THRESHOLD
# left as-is since no measured true-duplicate reached it, so it already
# behaves conservatively for this kind of short-identifier text.
#
# Calibrated against the v1a trial's data on 2026-09-11, which is what these
# were waiting for, and the answer was not a new number. 5 confirmed-same pairs
# against 155 confirmed-distinct ones give AUC 0.766 with a 95% interval of
# [0.437, 0.968]: the interval includes chance, and the two classes overlap on
# [0.477, 0.927], which is the whole usable range. Name similarity alone is not
# demonstrably a duplicate detector on this corpus, so moving either number on
# five positives would be fitting noise. Re-run it with `echo-memory calibrate`
# as the labelled set grows; the honest bar for retuning is enough positives
# that the interval clears 0.5.
#
# One threshold decision the data does support is at the silent-merge boundary,
# and it is handled by _differing_numeric_tokens rather than by moving
# HIGH_THRESHOLD - see that function.
LOW_THRESHOLD = 0.45
HIGH_THRESHOLD = 0.92

# Whether a pair above HIGH_THRESHOLD may merge without asking anybody.
#
# Off, and the evidence is one table and one query. Calibration on 2026-09-13
# reports, at the 0.92 bar, precision 50% over two reviewed pairs: no
# reassurance whatsoever for an unattended graph mutation, on a store that has
# already recorded three bad merges. A peer reviewer of the paper drawn from
# this record made the point and was right.
#
# What decided it was the cost. Every entity resolution this store has ever
# performed, by kind:
#
#   126  exact match
#    34  agent-confirmed fuzzy match
#     1  fuzzy match, similarity=0.927     <- the only silent merge, ever
#
# One in 24 days. Turning it off buys roughly one confirmation prompt a month
# and removes the only path that can join two entities with nobody watching.
# The pair is still surfaced as ambiguous, so nothing is lost except the
# silence.
#
# Turn it back on when calibration has enough positives for the AUC interval to
# clear 0.5, and precision at the bar rests on more than two pairs.
SILENT_MERGE = False

_NEGATION_TOKENS = ("in", "un", "non", "not")
_TRAILING_VERSION = re.compile(r"\d+[a-z]?$")
# Any token carrying a digit, anywhere in the name: 'v2', '0007', 'zlhv81t8'.
_DIGIT_TOKEN = re.compile(r"[a-z]*\d+[a-z0-9]*")


def _blocked_from_silent_merge(name_a: str, name_b: str) -> bool:
    """Never silently auto-merge two names that differ only by a negation
    affix or a trailing version/numeric token: these reliably score high on
    cosine similarity despite being different entities (t_valid/t_invalid,
    PR-B3/PR-B2, v1a/v1b, all measured above). A blocked pair still gets
    surfaced as ambiguous for the calling agent to judge; it just can't
    resolve silently. Deliberately narrow (negation + version only), not a
    general antonym detector."""
    a = re.sub(r"[\s_-]+", "", name_a.lower())
    b = re.sub(r"[\s_-]+", "", name_b.lower())
    if a == b:
        return False

    for neg in _NEGATION_TOKENS:
        if a == b.replace(neg, "", 1) or b == a.replace(neg, "", 1):
            return True

    a_stripped = _TRAILING_VERSION.sub("", a)
    b_stripped = _TRAILING_VERSION.sub("", b)
    return bool(a_stripped and a_stripped == b_stripped)


def _differing_numeric_tokens(name_a: str, name_b: str) -> bool:
    """Whether two names disagree about a number, anywhere in them.

    Checked only at the silent-merge boundary, and it is the one threshold
    decision this store's labelled data actually supports. Exactly one
    confirmed-distinct pair scores above HIGH_THRESHOLD -
    'prod-api.dugoutlive.com' against 'prod-api-v2.dugoutlive.com' at 0.958 -
    so a silent merge at 0.92 would have collapsed two production hosts into
    one entity. Exactly one confirmed-same pair scores above it too,
    'AWS Org SCP p-zlhv81t8' against 'AWS Organizations SCP p-zlhv81t8' at
    0.927, and it survives this rule because both names carry the same
    identifier and differ only by an abbreviation.

    _TRAILING_VERSION already did this for a digit at the END of a name, which
    catches PR-B3 against PR-B2 and misses a 'v2' in the middle of a hostname.
    Same idea, no longer anchored.

    A false positive here costs one ambiguity round trip: the pair drops to the
    path it would have taken anyway at any similarity below HIGH, and a human
    or agent decides. A false negative silently merges two entities, and this
    store has three of those on record.
    """
    return set(_DIGIT_TOKEN.findall(name_a.lower())) != set(
        _DIGIT_TOKEN.findall(name_b.lower())
    )


@dataclass
class Candidate:
    node_id: str
    name: str
    similarity: float


@dataclass
class Ambiguous:
    mention: str
    candidates: list[Candidate]


class ResolutionError(Exception):
    pass


@dataclass
class ResolutionOutcome:
    # mention -> node graphid (as text)
    resolved: dict[str, str] = field(default_factory=dict)
    ambiguous: list[Ambiguous] = field(default_factory=list)
    # entity_resolved audit rows to write: {node_id, resolution_detail}
    audit_events: list[dict] = field(default_factory=list)
    # mentions determined to be brand new entities, no audit trail needed
    new_entities: set[str] = field(default_factory=set)


# The properties expressions below have to match migration 0016's index
# definition character for character, or Postgres will not use it.
_GROUP = """(properties ->> '"group_id"'::agtype)"""
_NAME = """(properties ->> '"name"'::agtype)"""


def _exact_match(conn, group_id: str, name: str) -> tuple[str, str] | None:
    """Case-insensitive match against node.name or any alias. Returns
    (graphid, matched_name) or None.

    Read off the node table rather than through Cypher. `MATCH (n:Node
    {group_id: $gid}) WHERE toLower(n.name) = toLower($name)` is a sequential
    scan of every node in the database - every scope, and in the hosted service
    every tenant - because AGE expands the match before filtering. Confirmed by
    EXPLAIN on 2026-09-18: Seq Scan, 0.19ms at 1,057 nodes and 4.2ms at 24,054,
    called twice per write.

    Migration 0016 already created exactly the index this wants,
    node_name_per_group_idx on (group_id, lower(name)). It was unreachable from
    Cypher and has been sitting unused by the query it was made for.

    The alias lookup still scans, and it now scans only within the group rather
    than the whole store, which is the property that matters: one caller's
    write cost should not depend on how much everybody else has written.
    """
    row = conn.execute(
        f"""SELECT id::text, {_NAME}
            FROM {GRAPH}."Node"
            WHERE {_GROUP} = %s AND lower({_NAME}) = lower(%s)
            LIMIT 1""",
        (group_id, name),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"""SELECT id::text, {_NAME}
                FROM {GRAPH}."Node"
                WHERE {_GROUP} = %s
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(
                          COALESCE((properties ->> '"aliases"'::agtype)::jsonb, '[]'::jsonb)
                      ) AS alias
                      WHERE lower(alias) = lower(%s)
                  )
                LIMIT 1""",
            (group_id, name),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]).strip('"')


def _fuzzy_candidates(conn, group_id: str, embedding: list[float], limit: int = 5) -> list[Candidate]:
    # <#> (negative inner product), not <=> (cosine distance): embeddings are
    # L2-normalized on write (LocalEmbedder), so for unit vectors
    # embedding <#> query == -cosine_similarity, same ranking, cheaper (skips
    # computing both norms). See MATHS.local.md §1.
    rows = conn.execute(
        """
        SELECT ne.node_id::text, -(ne.embedding <#> %s::vector) AS similarity
        FROM public.node_embedding ne
        WHERE ne.group_id = %s
        ORDER BY ne.embedding <#> %s::vector
        LIMIT %s
        """,
        (embedding, group_id, embedding, limit),
    ).fetchall()
    if not rows:
        return []

    node_ids = [node_id for node_id, _ in rows]
    similarity_by_id = {node_id: float(similarity) for node_id, similarity in rows}

    name_rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            UNWIND $ids AS nid
            MATCH (n:Node) WHERE id(n) = nid
            RETURN id(n), n.name
        $$, %s) AS (node_id agtype, name agtype)""",
        (json.dumps({"ids": [int(nid) for nid in node_ids]}),),
    ).fetchall()
    name_by_id = {str(node_id): str(name).strip('"') for node_id, name in name_rows}

    return [
        Candidate(node_id=nid, name=name_by_id.get(nid, "?"), similarity=similarity_by_id[nid])
        for nid in node_ids
    ]


def _node_identity(conn, group_id: str, node_id: str) -> tuple[str, list[str]] | None:
    """The name and aliases of a Node this group owns, or None.

    Both halves of the ownership check matter. Existence alone would still let
    one tenant graft an edge onto another's entity; group alone cannot be
    checked without the lookup. The name comes back with it because the caller
    then has to decide whether this node has anything to do with the mention -
    see _referent_similarity."""
    try:
        wanted = int(node_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (n:Node) WHERE id(n) = $nid AND n.group_id = $gid
            RETURN n.name, n.aliases
        $$, %s) AS (name agtype, aliases agtype)""",
        (json.dumps({"nid": wanted, "gid": group_id}),),
    ).fetchone()
    if row is None:
        return None
    aliases = json.loads(str(row[1])) if row[1] is not None else []
    return str(row[0]).strip('"'), [str(a) for a in aliases or []]


def _node_in_group(conn, group_id: str, node_id: str) -> bool:
    return _node_identity(conn, group_id, node_id) is not None


def _referent_similarity(conn, group_id: str, node_id: str, mention: str, embedder) -> float:
    """How alike the mention and the named node are, on the same scale the
    candidate list is ranked by.

    Against the node's stored embedding where there is one, so the number is
    literally the one _fuzzy_candidates would have produced. A node written
    before embeddings existed, or one whose embedding failed, falls back to
    embedding its name - a slightly different number for the same question,
    which beats refusing to check at all."""
    try:
        wanted = int(node_id)
    except (TypeError, ValueError):
        return 0.0

    query = embedder.embed(mention)
    # node_id is AGE's graphid, which has no equality operator against bigint -
    # the same type mismatch that once turned a targeted DELETE into a full
    # one. Compared as text, exactly as _fuzzy_candidates selects it.
    row = conn.execute(
        """SELECT -(ne.embedding <#> %s::vector)
             FROM public.node_embedding ne
            WHERE ne.node_id::text = %s AND ne.group_id = %s""",
        (query, str(wanted), group_id),
    ).fetchone()
    if row is not None:
        return float(row[0])

    identity = _node_identity(conn, group_id, node_id)
    if identity is None:
        return 0.0
    stored = embedder.embed(identity[0])
    norm = (sum(q * q for q in query) ** 0.5) * (sum(v * v for v in stored) ** 0.5)
    return float(sum(q * v for q, v in zip(query, stored, strict=True)) / norm) if norm else 0.0


def resolve_entities(
    conn,
    group_id: str,
    entities: list[dict],
    resolutions: dict[str, dict],
    embedder,
    low_threshold: float = LOW_THRESHOLD,
    high_threshold: float = HIGH_THRESHOLD,
) -> ResolutionOutcome:
    outcome = ResolutionOutcome()

    for entity in entities:
        name = entity["name"]

        if name in resolutions:
            resolution = resolutions[name]
            resolved_to = resolution["resolved_to"]
            if resolved_to == "new":
                # "new" is an assertion by the caller, and an exact name match
                # in this scope contradicts it. The match wins, because
                # case-insensitive name equality is this system's own
                # definition of entity identity: without the resolutions dict
                # the very same mention would have resolved to this node, and a
                # caller's say-so must not create an entity the next mention
                # will silently merge back anyway.
                #
                # Taken on trust until now, and the consequence is criterion
                # 6's other bar. Two nodes named 'Eigon warm-base ALB sharing'
                # exist in this author's store, written minutes apart by one
                # session that said "new" both times - a duplicate created by
                # entity resolution, produced by the path that skips entity
                # resolution entirely.
                exact = _exact_match(conn, group_id, name)
                if exact is None:
                    outcome.new_entities.add(name)
                else:
                    node_id, _matched = exact
                    outcome.resolved[name] = node_id
                    outcome.audit_events.append({
                        "node_id": node_id,
                        "resolution_detail": (
                            "exact match; resolved_to=new was overridden because "
                            "this scope already holds a node of that name"
                        ),
                    })
            else:
                # The id has to be a real node in THIS group. It arrives from
                # the calling agent, and until 2026-09-09 it was taken entirely
                # on trust - no check that it existed, was a Node, or belonged
                # to the caller.
                #
                # In the hosted service that hole is a cross-tenant write.
                # Demonstrated before this check existed: one account passed
                # another account's node id and successfully attached an edge
                # to it, because _create_edge matches nodes by id alone with no
                # group filter. The edge carried the writer's own group_id
                # while pointing at somebody else's entity.
                identity = _node_identity(conn, group_id, resolved_to)
                if identity is None:
                    raise ResolutionError(
                        f"entity_resolutions[{name!r}] points at node "
                        f"{resolved_to!r}, which is not a node in this scope. "
                        "Pass an id from this call's own ambiguous_entities "
                        "candidates, or \"new\" - never one remembered from "
                        "an earlier turn."
                    )

                # Owning the node is not the same as it being the right node,
                # and the incident this store recorded was the second kind.
                # Ids recalled from memory rather than read from the graph
                # pointed at 'node_embedding table' and 'AGE graphid column
                # type' - both nodes the caller owned, in a different project -
                # so three facts about Indian tax compliance were attached to
                # an embedding table's lesson. Nothing objected; the facts
                # simply landed somewhere else. A scope check alone would have
                # let every one of them through.
                #
                # The test is the one the server can actually justify: would it
                # ever have offered this node as a candidate for this mention?
                # Candidates are drawn above low_threshold, so a node below it
                # cannot be an answer to a question this server asked, and an
                # id that did not come from a candidate list came from
                # somewhere that cannot be trusted with an entity's identity.
                #
                # Measured on the real embedder before choosing the bar. Three
                # bad merges are on record at 0.030, 0.041 and 0.259; the
                # hardest legitimate confirmation ("AGE" / "Apache AGE") scores
                # 0.497.
                #
                # The margin is NOT uniform, and describing it as "an order of
                # magnitude" - which this comment and a paper drawn from it both
                # did - is wrong for the case that matters. 0.497/0.030 is 16.6x
                # and 0.497/0.041 is 12.1x, but 0.497/0.259 is 1.92x. The
                # closest bad merge sits 0.19 above the bar, not an order of
                # magnitude below the hardest true positive.
                #
                # So the guard separates every case on record and its margin is
                # thin at one end. A legitimate confirmation below 0.45, or a
                # misdirected one above it, is not far-fetched on this evidence;
                # what the bar rules out is the obviously unrelated, which is
                # the claim it can carry.
                #
                # An exact name or alias match skips the check entirely, since
                # that is the one case where the id is redundant rather than
                # doubtful.
                node_name, aliases = identity
                known = {node_name.lower(), *(a.lower() for a in aliases)}
                if name.lower() not in known:
                    similarity = _referent_similarity(
                        conn, group_id, resolved_to, name, embedder
                    )
                    if similarity < low_threshold:
                        raise ResolutionError(
                            f"entity_resolutions[{name!r}] points at node "
                            f"{resolved_to!r}, which is named {node_name!r}. "
                            f"That is too unlike {name!r} (similarity "
                            f"{similarity:.3f}, below {low_threshold}) for this "
                            "server to have offered it as a candidate, so the id "
                            "did not come from one. Pass an id from this call's "
                            "own ambiguous_entities candidates, or \"new\"."
                        )
                outcome.resolved[name] = resolved_to
                outcome.audit_events.append(
                    {
                        "node_id": resolved_to,
                        "resolution_detail": "agent-confirmed fuzzy match"
                        + (f": {resolution['rationale']}" if resolution.get("rationale") else ""),
                        "append_alias": name,
                    }
                )
            continue

        exact = _exact_match(conn, group_id, name)
        if exact is not None:
            node_id, _matched_name = exact
            outcome.resolved[name] = node_id
            outcome.audit_events.append({"node_id": node_id, "resolution_detail": "exact match"})
            continue

        embedding = embedder.embed(name)
        candidates = _fuzzy_candidates(conn, group_id, embedding)
        best = candidates[0] if candidates else None
        blocked = best is not None and _blocked_from_silent_merge(name, best.name)

        if (
            SILENT_MERGE
            and best is not None
            and best.similarity >= high_threshold
            and not blocked
            and not _differing_numeric_tokens(name, best.name)
        ):
            outcome.resolved[name] = best.node_id
            outcome.audit_events.append(
                {
                    "node_id": best.node_id,
                    "resolution_detail": f"fuzzy match, similarity={best.similarity:.3f}",
                    "append_alias": name,
                }
            )
        elif best is not None and (best.similarity >= low_threshold or blocked):
            # Only the candidates at or above the bar that caused this. The
            # full top-5 used to ship, so a deferral triggered by a 0.708
            # match also listed neighbours at 0.10 and 0.067 - and a reader
            # reasonably concluded the threshold was somewhere near 0.06.
            # A caller cannot act on a list whose entries had no part in the
            # decision; `blocked` keeps the best one regardless, because then
            # the reason is the name guard rather than the score.
            near = [c for c in candidates if c.similarity >= low_threshold]
            outcome.ambiguous.append(
                Ambiguous(mention=name, candidates=near or [best])
            )
        else:
            outcome.new_entities.add(name)

    return outcome
