"""write_episode: storage, resolution, and retrieval only, never inference
(see the design doc's MCP tool contract architecture pivot). Entities/facts
arrive already extracted by the calling agent."""

import json
import time
import uuid

import psycopg

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger, log_write_episode
from echo_memory.infra.project import UNKNOWN as PROJECT_UNKNOWN
from echo_memory.ingestion.embeddings import Prefetched
from echo_memory.ingestion.neighbourhood import MAX_FACTS_CONSULTED, related_entities
from echo_memory.ingestion.resolution import (
    ResolutionError,
    _exact_match,
    resolve_entities,
)
from echo_memory.retrieval.query_memory import query_memory

MAX_ENTITIES = 50
MAX_FACTS = 200
MAX_STRING_LEN = 4000
VALID_CONFIDENCE = {"extracted", "inferred", "ambiguous"}

# First-use onboarding nudge (CEO plan scope decision #6): after exactly
# this many write_episode calls for a group_id, attach a live digest sample
# to the response so a single-user v1a audience notices the digest feature
# exists. Not "adoption risk mitigation" at this scale, just a nudge.
ONBOARDING_NUDGE_AT_COUNT = 3

_logger = get_logger("write_episode")


def embedding_text(source: str, target: str, fact: str) -> str:
    """What actually gets embedded for a fact: its two entity names, then the
    fact.

    The entity names are structure the store already has and never put into the
    vector, while the question an agent asks is entity-shaped - "what do I know
    about updateSquad". Measured over 219 cases, by query shape:

        query names both entities   MRR 0.604 -> 0.776
        query names one entity      MRR 0.579 -> 0.659
        query names neither         MRR 0.976 -> 0.959

    The first row is the eval's own query shape and therefore its most
    flattering: the query is literally a substring of the embedded text, which
    is how one games a benchmark. The second is the honest number - a real
    question usually names one of the things it is about - and the third is the
    cost, a small dilution when the query names nothing.

    Also measured and rejected: embedding only the fact's first sentence, the
    "claim" half of a claim/detail split. That scored WORSE than the full text
    (MRR 0.604 -> 0.582), because the entity names a query matches on often sit
    in the evidence half, and truncating discards them. Prepending the names
    and then shortening does beat prepending alone (0.776 vs 0.751), so the
    split may be worth revisiting - but the names are doing the work, not the
    brevity.
    """
    return f"{source} {target}. {fact}" if source and target else fact


def _not_stored(result: dict) -> dict:
    """Say plainly, in the response, when an episode changed nothing.

    Three different paths end with an empty `edges_created`, and from a
    caller's side they are indistinguishable without inspecting which other
    key happens to be populated:

      refused   validation rejected it - too long, or a source naming an
                entity the episode did not declare
      deferred  an entity mention was similar enough to an existing node to
                be worth asking about and not similar enough to merge, so the
                episode was held rather than attached to a guess
      unchanged every fact in it already exists, unaltered

    The deferred case is the one that hurts at scale and the one that looks
    most like success: it returns a well formed response, no error anywhere,
    `isError` false, and nothing stored. A customer loading 26,633 facts read
    that as 26,633 successes and stored 1,650.

    So the response now states it. A caller that checks nothing else still
    sees `stored: false` and a `not_stored` reason naming which of the three
    happened, instead of having to know that an empty list in one key and a
    populated list in another together mean "we did not write your data".
    """
    created = result.get("edges_created") or []
    superseded = result.get("superseded") or []
    if created or superseded:
        result["stored"] = True
        return result

    result["stored"] = False
    if result.get("error"):
        result["not_stored"] = "refused"
    elif result.get("ambiguous_entities"):
        mentions = ", ".join(
            str(a.get("mention")) for a in result["ambiguous_entities"][:3]
        )
        result["not_stored"] = "deferred"
        result["not_stored_detail"] = (
            f"held on ambiguous entities ({mentions}). Nothing was written. "
            "Pass entity_resolutions to say which existing node each mention "
            "is, or {\"resolved_to\": \"new\"} if it is a new one."
        )
    else:
        result["not_stored"] = "unchanged"
        result["not_stored_detail"] = (
            "every fact in this episode already exists, unaltered."
        )
    return result


def _refused(reason: str) -> dict:
    """A refusal, shaped like every other answer this function gives.

    It used to be `{"error": ...}` and nothing else, which quietly made the
    only reliable success check impossible. A caller cannot ask
    `len(result["edges_created"])` on a response that has no such key, so the
    checks people actually write are "did it return" or "did it throw" - and
    a refusal does neither. It returns, without raising, having stored
    nothing.

    That is not hypothetical. A customer bulk loading 26,633 facts counted
    every non-null response as a success and stored 1,650, because roughly
    nineteen thousand episodes were refused by validation and every refusal
    looked exactly like a write that had worked. Validation also happens
    before anything touches the graph, so those refusals left no audit entry
    either: from the outside the writes simply evaporated.

    Carrying the empty lists costs nothing and makes one check correct
    everywhere: `edges_created` is empty on every path that stored nothing,
    whether it was refused, deferred for ambiguity, or accepted and found to
    change nothing.
    """
    return _not_stored({"error": reason, "edges_created": [], "superseded": [],
                        "ambiguous_entities": []})


class ValidationError(Exception):
    pass


def _validate(entities: list[dict], facts: list[dict]) -> None:
    if len(entities) > MAX_ENTITIES:
        raise ValidationError(f"too many entities: {len(entities)} > {MAX_ENTITIES}")
    if len(facts) > MAX_FACTS:
        raise ValidationError(f"too many facts: {len(facts)} > {MAX_FACTS}")

    entity_names = set()
    for entity in entities:
        name = entity.get("name", "")
        if not name or len(name) > MAX_STRING_LEN:
            raise ValidationError(f"invalid entity name: {name[:80]!r}")
        entity_names.add(name)

    for fact in facts:
        if len(fact.get("fact", "")) > MAX_STRING_LEN:
            raise ValidationError("fact text too long")
        if fact.get("confidence") not in VALID_CONFIDENCE:
            raise ValidationError(f"invalid confidence: {fact.get('confidence')!r}")
        if fact.get("source") not in entity_names:
            raise ValidationError(f"fact source {fact.get('source')!r} not in entities")
        if fact.get("target") not in entity_names:
            raise ValidationError(f"fact target {fact.get('target')!r} not in entities")


def _create_node(conn, group_id: str, name: str, type_: str, embedder) -> str:
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            CREATE (n:Node {{name: $name, type: $type, group_id: $gid, aliases: []}})
            RETURN id(n)
        $$, %s) AS (node_id agtype)""",
        (json.dumps({"name": name, "type": type_, "gid": group_id}),),
    ).fetchone()
    node_id = str(row[0])
    embedding = embedder.embed(name)
    conn.execute(
        "INSERT INTO public.node_embedding (node_id, group_id, embedding) VALUES (%s::graphid, %s, %s)",
        (node_id, group_id, embedding),
    )
    return node_id


def _append_alias(conn, node_id: str, alias: str) -> None:
    conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (n:Node) WHERE id(n) = $nid AND NOT $alias IN n.aliases
            SET n.aliases = n.aliases + [$alias]
        $$, %s) AS (r agtype)""",
        (json.dumps({"nid": int(node_id), "alias": alias}),),
    )


def _find_active_edge(
    conn, group_id: str, source_id: str, target_id: str, relation_type: str
) -> tuple[str, str] | None:
    """Returns (edge_id, fact_text) so a supersession's audit entry can
    record before_fact, not just its id.

    Read off the edge table rather than through Cypher, for the same reason
    _adjacent is. `MATCH (a)-[e:FACT]->(b) WHERE id(a) = $sid AND id(b) = $tid`
    cannot use an index: AGE expands the match and filters afterwards, so every
    call walked every FACT edge in the scope. At 776 facts that was 651 ms per
    call and 93% of a six-fact write.

    start_id and end_id are indexed alongside group_id by migration 0013, which
    is the index this query wanted all along.
    """
    row = conn.execute(
        f"""SELECT id::text, properties ->> '"fact"'::agtype
            FROM {GRAPH}."FACT"
            WHERE (properties ->> '"group_id"'::agtype) = %s
              AND start_id = %s::text::graphid
              AND end_id = %s::text::graphid
              AND (properties ->> '"relation_type"'::agtype) = %s
              AND (properties ->> '"t_invalid"'::agtype) IS NULL
            LIMIT 1""",
        (group_id, str(source_id), str(target_id), relation_type),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), row[1]


def _invalidate_edge(conn, edge_id: str, t_invalid: int) -> None:
    conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->() WHERE id(e) = $eid
            SET e.t_invalid = $t_invalid
        $$, %s) AS (r agtype)""",
        (json.dumps({"eid": int(edge_id), "t_invalid": t_invalid}),),
    )


def _create_or_find_node(conn, group_id: str, name: str, type_: str, embedder) -> str:
    """Create the node, unless another writer created it first.

    Resolution decided this name was new by reading before writing, and nothing
    makes that atomic. One writer and the read is a good enough proxy for the
    truth; two - Claude Code and Cursor in the same second, or any two agents on
    one hosted account - and both can see "no such node" before either commits.

    Migration 0016's unique index makes the second write fail instead of
    succeeding into a split entity. Catching it here turns the loser of the
    race into a normal resolution: the row the winner committed is exactly what
    _exact_match would have returned had the read happened a moment later.

    The savepoint is what makes that possible. A unique violation aborts the
    enclosing transaction in Postgres, so without one the whole episode is lost
    to a race that has already resolved itself correctly.
    """
    try:
        with conn.transaction():
            return _create_node(conn, group_id, name, type_, embedder)
    except psycopg.errors.UniqueViolation:
        existing = _exact_match(conn, group_id, name)
        if existing is None:
            # The index fired and yet nothing matches: not a race, and not
            # something to paper over.
            raise
        _logger.info(
            "node_created_concurrently", extra={"group_id": group_id, "entity": name}
        )
        return existing[0]


def _create_edge(
    conn,
    group_id: str,
    source_id: str,
    target_id: str,
    fact: dict,
    session_id: str,
    episode_id: str,
    t_valid: int,
    embedder,
    project: str,
    agent_id: str,
) -> str:
    # Apache AGE drops a property whose value is null at CREATE time, so a None
    # here does not store `agent_id: null` - it stores no agent_id key at all,
    # silently. That is how 30 facts in the author's own store ended up
    # unattributable: a long-lived MCP server process that had imported this
    # module before agent_id existed kept writing through it for 16 days. The
    # data looks fine until something asks who wrote a fact, and by then the
    # session that could have answered is gone.
    #
    # Raising is the point. A fact whose author is unknown cannot evidence a
    # cross-tool recall save, which is the one measurement this project exists
    # to make, so losing the write is strictly better than keeping a fact that
    # quietly cannot be counted.
    if not agent_id:
        raise ValidationError(
            f"refusing to write a fact with no agent_id (got {agent_id!r}). "
            "Every fact records who wrote it; see server.py's _author_of."
        )
    # Same null-drop, one property over, and it went unnoticed because the
    # consequence is quieter than a missing author. Seven facts in this store
    # carry no project key at all - six of them written the day this was found.
    # A fact with no project gives its nodes no project, and duplicate_candidates
    # drops any pair with no project in common, so the entity becomes invisible
    # to the review queue built to catch exactly this kind of thing.
    #
    # Coerced rather than refused: 'unknown' is what migration 0003 backfilled
    # and what the project filter already understands, so the honest
    # representation of "nobody said" exists. Absent is not that value - it is
    # the absence of any value, which nothing downstream can filter on.
    project = project or PROJECT_UNKNOWN
    # Both endpoints must belong to this group. resolution.py already refuses a
    # resolved_to from another scope, so reaching here with a foreign node means
    # some other path produced the id - which is exactly when a second check
    # earns its place. Matching on id alone let one account attach an edge to
    # another account's entity, demonstrated against a real database before
    # this landed.
    row = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a), (b)
            WHERE id(a) = $sid AND id(b) = $tid
              AND a.group_id = $gid AND b.group_id = $gid
            CREATE (a)-[e:FACT {{
                relation_type: $rel, fact: $fact, confidence: $confidence,
                t_valid: $t_valid, t_invalid: null, group_id: $gid,
                project: $project, agent_id: $agent_id,
                provenance: {{session_id: $session_id, source_episode_id: $episode_id}}
            }}]->(b)
            RETURN id(e)
        $$, %s) AS (edge_id agtype)""",
        (
            json.dumps(
                {
                    "sid": int(source_id),
                    "tid": int(target_id),
                    "rel": fact["relation_type"],
                    "fact": fact["fact"],
                    "confidence": fact["confidence"],
                    "t_valid": t_valid,
                    "gid": group_id,
                    "session_id": session_id,
                    "episode_id": episode_id,
                    "project": project,
                    "agent_id": agent_id,
                }
            ),
        ),
    ).fetchone()
    edge_id = str(row[0])
    embedding = embedder.embed(
        embedding_text(fact.get("source", ""), fact.get("target", ""), fact["fact"])
    )
    conn.execute(
        "INSERT INTO public.fact_embedding (edge_id, group_id, embedding) VALUES (%s::graphid, %s, %s)",
        (edge_id, group_id, embedding),
    )
    return edge_id


def _increment_write_episode_count(conn, group_id: str) -> int:
    (count,) = conn.execute(
        """
        INSERT INTO public.group_state (group_id, write_episode_count)
        VALUES (%s, 1)
        ON CONFLICT (group_id) DO UPDATE
            SET write_episode_count = group_state.write_episode_count + 1
        RETURNING write_episode_count
        """,
        (group_id,),
    ).fetchone()
    return count


def _write_audit_entry(conn, group_id: str, session_id: str, **fields) -> None:
    # Stamped by the process doing the writing, which is the only place that
    # knows. An MCP stdio server holds the code it imported at spawn, so the
    # version on disk says nothing about what is actually running - and that
    # gap has already cost this store 30 facts with no author and 7 with no
    # project. `health` compares this against its own version and says
    # "restart your client" instead of leaving it to be found in the data.
    from echo_memory import __version__

    columns = ["group_id", "session_id", "writer_version"] + list(fields.keys())
    values = [group_id, session_id, __version__] + list(fields.values())
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO public.audit_entry ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def _prefetch_names(embedder, entities: list[dict]):
    """Embed the entity names in one pass, before resolution needs them.

    A transformer pays fixed per-call overhead that one text bears alone and
    many share: the texts a six-fact episode embeds cost 90.5 ms one call each
    and 7.9 ms batched, a quarter of a 330 ms write.

    Names only, here. Which facts get written is not known until resolution has
    said which mentions are ambiguous, and a fact touching an ambiguous mention
    is deferred and never embedded - so predicting them all up front does work
    the write then discards. The fact texts join the same batch cache later,
    once the answer exists.
    """
    return Prefetched(embedder, [e.get("name", "") for e in entities])


def write_episode(
    conn,
    group_id: str,
    session_id: str,
    entities: list[dict],
    facts: list[dict],
    resolutions: dict[str, dict] | None,
    embedder,
    project: str = PROJECT_UNKNOWN,
    agent_id: str = PROJECT_UNKNOWN,
    assume_new: bool = False,
) -> dict:
    # Every mention in this episode is a new entity, said once instead of
    # named one at a time.
    #
    # The deferral exists because two names that look alike might be one
    # thing, and guessing is worse than asking. But a caller recording "here
    # is a function I just found" already knows the answer, and making it
    # discover the question first costs a round trip per write. A store of
    # mostly-distinct names - source symbols, bug titles, player names -
    # spends that round trip on nearly every call and answers "new" every
    # time.
    #
    # This is exactly equivalent to passing resolved_to "new" for each
    # entity, which is what callers were doing reactively. Explicit
    # resolutions still win: a caller that names one entity and assumes the
    # rest is saying something more specific, and specificity should not be
    # overridden by a default.
    resolutions = dict(resolutions or {})
    if assume_new:
        for entity in entities:
            name = (entity or {}).get("name")
            if name and name not in resolutions:
                resolutions[name] = {"resolved_to": "new"}
    start = time.perf_counter()

    try:
        # Checked here, before any row is written, so a caller with a broken
        # config gets one clean error instead of a half-written episode. The
        # same condition is re-checked in _create_edge, which is the only place
        # that can actually lose the value; see the note there.
        if not agent_id:
            raise ValidationError(
                f"refusing to write a fact with no agent_id (got {agent_id!r}). "
                "Set ECHO_MEMORY_AGENT_ID, or pass agent_id explicitly."
            )
        _validate(entities, facts)
    except ValidationError as e:
        log_write_episode(
            _logger, group_id, session_id, len(entities), len(facts), 0, 0,
            (time.perf_counter() - start) * 1000, error=str(e),
        )
        return _refused(str(e))

    # After validation, never before. Prefetching reads every entity name, so
    # on a malformed episode it embedded the bad input and failed there -
    # turning clean ValidationErrors into whatever the embedder happened to
    # raise. A speed-up must not change what a caller is told went wrong.
    embedder = _prefetch_names(embedder, entities)

    episode_id = str(uuid.uuid4())
    now = int(time.time())
    edges_created: list[str] = []
    # Supersession is the commonest contradiction and it already happens here:
    # a fact with the same (source, relation_type, target) invalidates its
    # predecessor and writes an audit entry. The response never said so, so the
    # agent that just overwrote something it wrote last week had no way to know.
    # Reporting it costs nothing and is higher-frequency than anything a
    # dedicated contradiction detector would find.
    superseded: list[dict] = []
    onboarding_sample: dict | None = None

    try:
        with conn.transaction():
            # One writer per group, stated rather than inherited.
            #
            # Everything below is an unlocked read-modify-write: _find_active_edge
            # then _create_edge then _invalidate_edge, and resolve_entities has the
            # same shape around exact-match-or-create. Two agents writing one triple
            # concurrently would both see no existing edge and leave two active
            # ones - or two nodes for one entity, which IS criterion 6's duplicate
            # bar, inflated by the very command meant to make that gate measurable.
            #
            # That race does not currently happen, and the reason is an accident:
            # _increment_write_episode_count below is an upsert on group_state keyed
            # by group_id, and being the transaction's first statement it takes a
            # row lock held to commit. A review flagged the missing lock; a test
            # written to prove the race passed without one, which is how the
            # accident surfaced.
            #
            # It is made explicit here because nothing records that the counter is
            # load-bearing. Move it, make it conditional, or drop the counter, and
            # the serialisation disappears silently at exactly the moment six
            # clients start writing. Transaction-scoped, so it releases on commit or
            # rollback with no unlock path to forget, and free at one writer.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"episode|{group_id}",)
            )
            call_count = _increment_write_episode_count(conn, group_id)

            outcome = resolve_entities(conn, group_id, entities, resolutions, embedder)

            entities_by_name = {e["name"]: e for e in entities}
            name_to_node_id = dict(outcome.resolved)

            # Which facts can be written now: a fact touching an ambiguous mention
            # waits for the caller to say which candidate it meant. Computed BEFORE
            # any node is created, because it decides which nodes are worth creating.
            ambiguous_mentions = {a.mention for a in outcome.ambiguous}
            ready_facts = [
                f
                for f in facts
                if f["source"] not in ambiguous_mentions and f["target"] not in ambiguous_mentions
            ]

            # The rest of the batch, now that ambiguity has decided which facts
            # exist. Deferred facts are never embedded, which is why this could
            # not have been part of the first pass.
            embedder.extend(
                [
                    embedding_text(f.get("source", ""), f.get("target", ""), f["fact"])
                    for f in ready_facts
                ]
                + [f["fact"] for f in ready_facts[:MAX_FACTS_CONSULTED]]
            )

            # A new entity is created only if some fact being written now actually
            # uses it, or if no fact mentions it at all - the caller asked for that
            # one directly, so honour it.
            #
            # Creating every new entity regardless left orphans. An entity whose
            # only facts are held back for ambiguity has nothing to connect to, and
            # if the caller never makes the follow-up call with entity_resolutions -
            # which nothing forces it to - the node stays unreachable forever. That
            # is not hypothetical: writing five dugout facts on 2026-09-07 returned
            # ambiguous_entities and edges_created: [], reported as "nothing was
            # written", and left "ECS secret resolution at task startup" behind. The
            # response says no edges; it never said it had made a node.
            #
            # Deferring costs nothing. The follow-up call carries the same entities,
            # so anything genuinely needed is created then, alongside the fact that
            # gives it an edge.
            # Within an episode that asserts facts, an entity no fact mentions
            # is noise and gets no node.
            #
            # Deferring the ambiguous case was the original fix and it left the
            # other half open: an entity listed in `entities` that NO fact
            # mentions was still created, because the excuse only covered ones a
            # held-back fact referenced. A caller that names four entities and
            # writes facts about three got a fourth node with no edges -
            # unreachable by query_memory, which searches facts, and present in
            # every pair the duplicate scanner considers from then on. Two such
            # nodes were in this store and one was made by the session that
            # found them. Nothing rescues them later either, because nothing
            # references them.
            #
            # An episode with NO facts at all is a different act and is honoured:
            # the call's whole content is "these entities exist", and a later
            # fact mentioning one of those names resolves onto it. Refusing that
            # would make the call a silent no-op.
            used_by_ready = {f["source"] for f in ready_facts} | {f["target"] for f in ready_facts}
            registration_only = not facts
            for name in outcome.new_entities:
                if not registration_only and name not in used_by_ready:
                    continue
                entity = entities_by_name[name]
                name_to_node_id[name] = _create_or_find_node(
                    conn, group_id, entity["name"], entity["type"], embedder
                )

            for event in outcome.audit_events:
                _write_audit_entry(
                    conn,
                    group_id,
                    session_id,
                    mutation_type="entity_resolved",
                    affected_node_id=event["node_id"],
                    resolution_detail=event["resolution_detail"],
                    summary=f"entity resolved: {event['resolution_detail']}",
                )
                if "append_alias" in event:
                    _append_alias(conn, event["node_id"], event["append_alias"])

            for fact in ready_facts:
                source_id = name_to_node_id[fact["source"]]
                target_id = name_to_node_id[fact["target"]]
                existing_edge = _find_active_edge(
                    conn, group_id, source_id, target_id, fact["relation_type"]
                )

                new_edge_id = _create_edge(
                    conn, group_id, source_id, target_id, fact, session_id, episode_id, now,
                    embedder, project, agent_id,
                )
                edges_created.append(new_edge_id)

                if existing_edge is not None:
                    old_edge_id, old_fact_text = existing_edge
                    _invalidate_edge(conn, old_edge_id, now)
                    superseded.append({
                        "fact_id": old_edge_id,
                        "replaced": old_fact_text,
                        "with": fact["fact"],
                    })
                    _write_audit_entry(
                        conn,
                        group_id,
                        session_id,
                        mutation_type="fact_superseded",
                        affected_edge_ids=[old_edge_id, new_edge_id],
                        before_fact=old_fact_text,
                        after_fact=fact["fact"],
                        summary=f"invalidated {old_fact_text!r}, superseded by {fact['fact']!r}",
                    )
                else:
                    _write_audit_entry(
                        conn,
                        group_id,
                        session_id,
                        mutation_type="created",
                        affected_edge_ids=[new_edge_id],
                        summary=f"created: {fact['fact']}",
                    )

            if call_count == ONBOARDING_NUDGE_AT_COUNT:
                digest_result = query_memory(conn, group_id, None, 5, embedder, digest=True)
                onboarding_sample = digest_result.get("facts")

    except ResolutionError as e:
        # Raised inside the transaction, which therefore rolls back: a bad
        # resolved_to loses the whole episode rather than attaching some of
        # its facts to the wrong entity, which is the failure being fixed.
        # Comes out as the same {"error"} shape every other refusal uses.
        log_write_episode(
            _logger, group_id, session_id, len(entities), len(facts), 0, 0,
            (time.perf_counter() - start) * 1000, error=str(e),
        )
        return _refused(str(e))

    log_write_episode(
        _logger, group_id, session_id, len(entities), len(facts),
        len(edges_created), len(outcome.ambiguous), (time.perf_counter() - start) * 1000,
    )
    result = {
        "edges_created": edges_created,
        "superseded": superseded,
        "ambiguous_entities": [
            {
                "mention": a.mention,
                "candidates": [
                    {"node_id": c.node_id, "name": c.name, "similarity": c.similarity}
                    for c in a.candidates
                ],
            }
            for a in outcome.ambiguous
        ],
    }
    if onboarding_sample is not None:
        result["onboarding_sample"] = onboarding_sample
    _not_stored(result)

    # Computed after the transaction commits, and never inside it: this is
    # advice about the next episode, and a failure to produce it must not cost
    # the episode that was already written.
    if edges_created and ready_facts:
        try:
            # The bare fact text, not embedding_text: stored vectors carry
            # "<source> <target>. <fact>", so querying with the sentence alone
            # is the eval's `prose` shape, which measures R@1 0.949 and MRR
            # 0.970 on this store - the best-performing configuration of any
            # retrieval channel here, and the one that cannot leak through the
            # entity names.
            related = related_entities(
                conn, group_id, [embedder.embed(f["fact"]) for f in ready_facts[:MAX_FACTS_CONSULTED]],
                written_node_ids=[str(v) for v in name_to_node_id.values()],
            )
        except Exception:
            # Nothing here is load-bearing. A store mid-migration, an embedder
            # that just lost its model, a Cypher shape an older AGE dislikes -
            # none of them is a reason to make a successful write look failed.
            _logger.debug("related_entities_failed", exc_info=True)
            related = []
        if related:
            result["related_entities"] = related
    return result
